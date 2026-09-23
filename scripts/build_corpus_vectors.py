# -*- coding: utf-8 -*-
"""语料向量文件离线生成脚本（方案A「智谱单厂商」的离线嵌入产物）。

**为什么需要这个脚本**：`src/dataset_recommender/retrieval/recall_api.py` 的 dense 后端
把「语料文档向量」做成**离线一次性嵌入的 gzip JSON 文件**（随部署物分发、启动校验
model+dims 不匹配拒启用），查询侧只对文件未覆盖的条目现场 API 补嵌。本脚本就是生成那份
向量文件的唯一入口——把全语料逐条按**生产候选文本模板**编码成向量、带上文本指纹，产出
`recall_api._load_vectors` 能直接读的格式。

**设计纪律（why）**：
- **候选文本模板复用生产代码，绝不自己拼**：这里不复制 `_candidate_text` 的字符串格式，
  而是构造一个只有 `.record` 属性的轻量对象（`types.SimpleNamespace(record=rec)`）传给
  `vector_recall._candidate_text`。理由与 `recall_api.api_dense_vectors` 一致——离线向量与
  查询侧打分的文本必须**逐字同源**，否则文本指纹（`candidate_text_sha`）对不上，文件条目
  会在运行期被判「模板漂移」而整体失效、退化成每次查询全量补嵌。
- **文本指纹用 `recall_api.candidate_text_sha`**：与查询侧同一 sha 口径，保证「模板未漂移」
  校验成立。
- **向量文件格式与 `recall_api._load_vectors` 完全对齐**：
  `{"meta": {"model", "dimensions", "created_at", "count"}, "vectors": {"<uid>": {"h", "v"}}}`，
  gzip 写出。`_load_vectors` 只认 model/dimensions 与 vectors dict，多写 `created_at`/`count`
  是元信息、零风险。
- **key 只在进程环境 `BIODATA_EMBED_API_KEY`**：脚本绝不接受命令行明文 key 参数、绝不打印
  key；缺失时报错退出码 1。
- **fail-closed 的构建侧镜像**：单批失败最多 3 次指数退避重试（2s/4s/8s），仍失败记入失败
  清单继续，结尾汇总并给退出码 2——宁可留缺口让运行期补嵌，也绝不把错向量写进文件。
- **语料口径**：`database/base/` + `database/external/` 官方快照，**默认剔除 `upload_*.json`**
  （用户上传不进入随包离线向量产物，运行期由查询侧 API 补嵌覆盖）；`--include-uploads`
  才纳入 upload_*——服务器运营重建专用：网页形态下共享写层的 upload_* 全部是 curate sync
  产物（用户上传都进了各自补丁包、不在共享写层），同步新数据入库后必须含它们重建，
  否则新条目每次查询都现场补嵌。

用法：
  set BIODATA_EMBED_API_KEY=<key>          # 只读进程环境变量
  py scripts/build_corpus_vectors.py                       # 全量生成
  py scripts/build_corpus_vectors.py --dry-run             # 只打印条数与第一批文本样本
  py scripts/build_corpus_vectors.py --limit 100           # 只嵌前 100 条（调试）
  py scripts/build_corpus_vectors.py --resume              # 断点续传，跳过已有 uid
  py scripts/build_corpus_vectors.py --out <path>          # 覆盖输出路径
  py scripts/build_corpus_vectors.py --include-uploads     # 纳入 upload_*（服务器运营重建用）
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "src"))
sys.path.insert(0, str(AGENT_ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dataset_recommender.corpus.corpus import load_full_corpus  # noqa: E402
from dataset_recommender.retrieval import recall_api  # noqa: E402
from dataset_recommender.retrieval.vector_recall import _candidate_text  # noqa: E402

#: 语料根：load_full_corpus 需要 data_dir（base）+ project_root（external 双层解析）。
DATA_DIR = AGENT_ROOT / "database" / "base"
PROJECT_ROOT = AGENT_ROOT

#: 单条候选文本的截断防御（字符数）。候选文本模板已把描述截到 ≤400，正常远低于智谱 3072
#: tokens 上限；此阈值只在异常长描述/脏数据时兜底，避免单条超限拖垮整批。
MAX_TEXT_CHARS = 12000
#: 单批嵌入条数（智谱官方：embeddings 单次数组 ≤64）。
_EMBED_BATCH = 64
#: API 超时秒数（构建侧可以比查询侧 recall_api 的 5s 更宽：离线批任务可容忍慢）。
_TIMEOUT_S = 30.0
#: 单批最大重试次数（不含首次），退避 2s/4s/8s。
_MAX_RETRIES = 3


def _usage_tokens(usage: object) -> int:
    """从 API usage 对象里取 total_tokens（容错：任何异常返回 0）。"""
    if isinstance(usage, dict):
        raw = usage.get("total_tokens")
        if isinstance(raw, (int, float)):
            return int(raw)
    return 0


def _load_existing(path: Path, model: str, dims: int) -> "dict | None":
    """读已有输出文件用于断点续传；meta 与当前 model/dims 不匹配返回 None（视为不可续传）。"""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return {}
    meta = payload.get("meta") or {}
    if meta.get("model") != model or int(meta.get("dimensions") or 0) != dims:
        return None
    vectors = payload.get("vectors")
    return vectors if isinstance(vectors, dict) else {}


def _write_output(path: Path, model: str, dims: int, vectors: dict) -> None:
    """gzip 写出与 recall_api._load_vectors 对齐的向量文件。"""
    payload = {
        "meta": {
            "model": model,
            "dimensions": dims,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "count": len(vectors),
        },
        "vectors": vectors,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)


def _embed_batch(
    client: object,
    base_url: str,
    key: str,
    model: str,
    dims: int,
    texts: list[str],
) -> "tuple[list[list[float]] | None, object]":
    """嵌入单批（≤64）文本；返回 (vectors, usage)。失败做 3 次指数退避重试，仍失败返回 (None, None)。"""
    url = f"{base_url}/embeddings"
    headers = {"Authorization": f"Bearer {key}"}
    payload = {"model": model, "input": texts, "dimensions": dims}
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                rows = data.get("data")
                if isinstance(rows, list) and len(rows) == len(texts):
                    rows = sorted(rows, key=lambda r: r.get("index", 0))
                    vecs: list[list[float]] = []
                    for row in rows:
                        emb = row.get("embedding")
                        if not isinstance(emb, list) or len(emb) != dims:
                            raise ValueError("embedding 形状不符")
                        vecs.append([float(x) for x in emb])
                    return vecs, data.get("usage")
            # 非 200 / 形状不符：落到下方退避后重试（不打印 key、不打印明文）。
        except Exception:
            pass
        if attempt < _MAX_RETRIES:
            time.sleep(2 * (2 ** attempt))  # 2s / 4s / 8s
    return None, None


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="离线生成语料向量文件（方案A 智谱 embedding-3）。")
    ap.add_argument("--out", help="输出文件路径（相对路径相对于项目根；默认 database/corpus_vectors.<model>.<dims>.json.gz）")
    ap.add_argument("--limit", type=int, default=None, help="只嵌前 N 条（调试用）")
    ap.add_argument("--resume", action="store_true", help="断点续传：读入已有输出文件，跳过已有 uid")
    ap.add_argument("--dry-run", action="store_true", help="不调 API，只打印将要嵌入的条数与第一批文本样本")
    ap.add_argument("--include-uploads", action="store_true",
                    help="纳入 upload_* 命名空间（服务器运营重建用——网页形态下共享写层的 upload_* "
                         "全部是 sync 产物；随包产物纪律不动，默认仍剔除）")
    return ap.parse_args()


def main() -> int:
    args = _parse_args()
    # 复用 recall_api 的单一配置真源（model/dims/base_url/key 解析口径与运行期完全一致）。
    model = recall_api._embed_model()
    dims = recall_api._embed_dims()
    base_url = recall_api._base_url()

    if args.limit is not None and args.limit < 0:
        print("[错误] --limit 不能为负。", file=sys.stderr)
        return 1

    out_path = Path(args.out) if args.out else (AGENT_ROOT / "database" / f"corpus_vectors.{model}.{dims}.json.gz")
    if not out_path.is_absolute():
        out_path = AGENT_ROOT / out_path

    # 语料：base + external 官方快照；默认剔除用户 upload_ 上传（运行期由查询侧 API 补嵌
    # 覆盖），--include-uploads 才纳入（服务器运营重建口径，见文件头「语料口径」）。
    recs = [r for r in load_full_corpus(DATA_DIR, PROJECT_ROOT)
            if args.include_uploads or not (r.source_file or "").startswith("upload_")]
    if args.limit is not None:
        recs = recs[: args.limit]

    # 组装 (uid, text, sha)：候选文本复用生产模板，指纹复用 recall_api.candidate_text_sha。
    rows: list[tuple[str, str, str]] = []
    for rec in recs:
        uid = str((rec.raw or {}).get("dataset_uid") or "").strip()
        if not uid:
            # 官方快照应全带 uid；缺失则跳过（无键可写、运行期也无法命中文件），如实提示。
            print(f"[警告] 跳过无 dataset_uid 的记录：{rec.dataset_name!r}", file=sys.stderr)
            continue
        text = _candidate_text(types.SimpleNamespace(record=rec))
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS]
        rows.append((uid, text, recall_api.candidate_text_sha(text)))

    total = len(rows)
    if args.dry_run:
        print(f"[dry-run] 待嵌入语料 {total} 条（model={model}, dims={dims}）")
        print(f"[dry-run] 输出路径：{out_path}")
        if rows:
            print("[dry-run] 第一批文本样本：")
            for uid, text, _ in rows[: min(total, _EMBED_BATCH)]:
                print(f"  - {uid}: {text[:120]}{'...' if len(text) > 120 else ''}")
        return 0

    key = recall_api._api_key()
    if not key:
        print("[错误] 未配置 BIODATA_EMBED_API_KEY（只读进程环境变量；脚本绝不打印 key）。", file=sys.stderr)
        return 1

    existing: dict = {}
    if args.resume and out_path.exists():
        loaded = _load_existing(out_path, model, dims)
        if loaded is None:
            print("[警告] 已有输出文件 meta 与当前 model/dims 不匹配，忽略续传、全量重算。", file=sys.stderr)
        else:
            existing = loaded
            print(f"[续传] 已有 {len(existing)} 条向量，跳过这些 uid。")

    # 分批（只保留需要嵌入的）。
    batches: list[list[tuple[str, str, str]]] = []
    for start in range(0, total, _EMBED_BATCH):
        chunk = rows[start:start + _EMBED_BATCH]
        todo = [row for row in chunk if row[0] not in existing]
        if todo:
            batches.append(todo)

    vectors = dict(existing)
    failed_uids: list[str] = []
    total_tokens = 0

    import httpx  # 惰性导入：--dry-run 不碰 httpx
    with httpx.Client(timeout=_TIMEOUT_S) as client:
        for bi, batch in enumerate(batches):
            texts = [t for _, t, _ in batch]
            vecs, usage = _embed_batch(client, base_url, key, model, dims, texts)
            if vecs is None:
                failed_uids.extend(uid for uid, _, _ in batch)
            else:
                for (uid, _, sha), vec in zip(batch, vecs):
                    vectors[uid] = {"h": sha, "v": vec}
                total_tokens += _usage_tokens(usage)
            if (bi + 1) % 10 == 0 or (bi + 1) == len(batches):
                print(f"[进度] 批 {bi + 1}/{len(batches)}，新增 {len(vectors) - len(existing)} 条，"
                      f"累计 tokens {total_tokens}")

    _write_output(out_path, model, dims, vectors)

    print("\n===== 汇总 =====")
    print(f"语料总数（{'含 upload_' if args.include_uploads else '官方快照，剔 upload_'}）：{total}")
    print(f"已写出向量：{len(vectors)} 条（其中续传复用 {len(existing)} 条，本次新增 {len(vectors) - len(existing)} 条）")
    print(f"失败批次：{len(failed_uids) // _EMBED_BATCH + (1 if len(failed_uids) % _EMBED_BATCH else 0)} 个，"
          f"涉及 {len(failed_uids)} 条 uid")
    print(f"usage total_tokens 合计：{total_tokens}")
    print(f"输出文件：{out_path}")
    if failed_uids:
        print("\n失败 uid（运行期将由查询侧 API 补嵌）：")
        for uid in failed_uids:
            print(f"  - {uid}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
