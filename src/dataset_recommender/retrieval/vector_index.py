"""语料级向量索引（vector 召回后端的本地数据源，确定性可复现）。

与 recall_api 的 API 语料向量文件同构的**本地**对应物：把全语料（基础 + 外部库）的候选
打分文本用本地 MiniLM 嵌成向量，持久化到 userdata_dir/vector_index/；查询时按
「uid + 文本指纹」双校验命中取用，未覆盖/指纹漂移的条目现场补嵌（**仅内存、不持久化**，
与 recall_api.api_dense_vectors 同一契约）。

设计约束（与 vector_recall / recall_api 同源，defense-in-depth）：
- 文本序列化唯一真源是 vector_recall._candidate_text（外加与 API 侧逐字同源的 12000 截断），
  索引条目与查询侧打分文本指纹一致；模板漂移由 sha 检出并按条目重建。
- 首用构建、按条目指纹**增量**重建：model/dimensions 不符或坏档 → 整档重建；语料增删/文本
  变化 → 只重嵌受影响条目、剔除已消失的 uid。
- 任何失败（模型不可用/嵌入畸形/写盘异常）→ None：调用方 fail-closed 回退规则序，绝不抛错、
  绝不阻塞请求、绝不联网。
- 官方评测不传 recall 参数 → 结构性不触碰本模块；本模块导入期不引入任何重依赖
  （sentence-transformers 仅在真正建索引/补嵌时经 load_embedder 惰性加载）。
"""
from __future__ import annotations

import gzip
import json
import os
import threading
import time
from pathlib import Path
from typing import Sequence

from .recall_api import candidate_text_sha
from .model_worker import MAX_TEXTS
from .retriever import RetrievedCandidate
from .vector_recall import (
    DEFAULT_EMBEDDING_MODEL,
    Embedder,
    _candidate_text,
    _warn_once_prefixed,
    load_embedder,
)
from ..app.runtime_paths import get_app_paths

#: paraphrase-multilingual-MiniLM-L12-v2 的输出维度（模型选型钉死在 vector_recall.DEFAULT_EMBEDDING_MODEL）。
EMBED_DIMENSIONS = 384
#: 与 recall_api.api_dense_vectors / scripts/build_corpus_vectors.py 同款的文本截断防御（逐字同源）。
MAX_TEXT_CHARS = 12000

_INDEX_LOCK = threading.Lock()
#: 进程内索引缓存：path → ((mtime_ns, size) | None, vectors)。文件被外部改写 → 指纹变 → 重读。
_INDEX_CACHE: "dict[str, tuple[tuple[int, int] | None, dict]]" = {}
#: 进程内本地嵌入器缓存（单例）：frozen 形态 external_embedder() 每次新建常驻模型子进程
#（model_runtime._WORKERS 持有到进程退出），不缓存会在连续 vector 检索间累积子进程耗尽内存。
#None 不缓存：用户稍后装好模型即可生效，无需重启进程。
_EMBEDDER_CACHE: "list[Embedder]" = []


def _warn_once(key: str, message: str) -> None:
    _warn_once_prefixed("[vector_index]", key, message)


def index_path(paths=None) -> Path:
    """索引文件唯一位置：userdata_dir/vector_index/corpus_vectors.<model>.<dims>.json.gz。"""
    p = paths or get_app_paths()
    return (Path(p.userdata_dir) / "vector_index"
            / f"corpus_vectors.{DEFAULT_EMBEDDING_MODEL}.{EMBED_DIMENSIONS}.json.gz")


def _file_sig(path: Path) -> "tuple[int, int] | None":
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def load_index(path: "Path | None" = None) -> "dict | None":
    """读索引文件并做版本校验（model/dimensions 不匹配、坏档 → None，触发整档重建）。

    文件格式（gzip JSON，与 API 语料向量文件同构）：
      {"meta": {"model", "dimensions", "created_at", "count"},
       "vectors": {"<uid>": {"h": "<candidate_text_sha>", "v": [floats]}}}
    """
    p = Path(path) if path is not None else index_path()
    try:
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
        meta = payload.get("meta") or {}
        vectors = payload.get("vectors") or {}
        if (
            meta.get("model") != DEFAULT_EMBEDDING_MODEL
            or int(meta.get("dimensions") or 0) != EMBED_DIMENSIONS
            or not isinstance(vectors, dict)
        ):
            return None
        return vectors
    except Exception:
        return None


def _record_text(record) -> str:
    """索引文本 = 查询侧打分文本（_candidate_text 唯一真源）+ API 侧同款 12000 截断。"""
    t = _candidate_text(RetrievedCandidate(record=record, score=0.0))
    return t[:MAX_TEXT_CHARS] if len(t) > MAX_TEXT_CHARS else t


def _local_embedder() -> "Embedder | None":
    """本地嵌入器：source/portable 走主进程 load_embedder()；frozen 主进程无重依赖时走
    model-runtime 隔离 venv 的外部嵌入器（与 cross_encoder 的外部运行时同族接口）。
    进程内单例缓存（竞态下至多多建一个，append 原子可接受）。"""
    if _EMBEDDER_CACHE:
        return _EMBEDDER_CACHE[0]
    try:
        enc = load_embedder()
        if enc is not None:
            _EMBEDDER_CACHE.append(enc)
            return enc
    except Exception:
        pass
    try:
        from .model_runtime import external_embed_ready, external_embedder
        if external_embed_ready():
            enc = external_embedder()
            _EMBEDDER_CACHE.append(enc)
            return enc
    except Exception:
        pass
    return None


def _load_corpus(paths=None) -> list:
    """语料装载唯一真源（corpus.load_full_corpus）：基础语料 + 全部外部库（shipped+user 双层）。"""
    from ..corpus.corpus import load_full_corpus
    p = paths or get_app_paths()
    root = Path(p.data_root)
    return load_full_corpus(root / "database" / "base", root)


def _write_index(path: Path, vectors: dict) -> None:
    """原子写（tmp + os.replace）：读者永不看到半档。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {
            "model": DEFAULT_EMBEDDING_MODEL,
            "dimensions": EMBED_DIMENSIONS,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "count": len(vectors),
        },
        "vectors": vectors,
    }
    tmp = path.parent / (path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def ensure_index(records=None, *, embedder: "Embedder | None" = None, paths=None) -> "dict | None":
    """保证本地语料索引与语料一致（首用构建、按条目文本指纹增量重建），返回 uid→{"h","v"} 或 None。

    任何失败 → None（调用方 fail-closed 回退规则序），绝不抛错。语料缺省经 _load_corpus
    装载（基础 + 外部全量）；测试可注入 records/embedder 走全隔离路径。"""
    try:
        enc = embedder if embedder is not None else _local_embedder()
        if enc is None:
            _warn_once("embedder_unavailable", "本地嵌入模型不可用 —— vector 召回回退规则序。")
            return None
        p = index_path(paths)
        if records is None:
            records = _load_corpus(paths)
        with _INDEX_LOCK:
            sig = _file_sig(p)
            cached = _INDEX_CACHE.get(str(p))
            old = cached[1] if (cached is not None and cached[0] == sig) else (load_index(p) or {})
            texts: "dict[str, str]" = {}
            for r in records:
                raw = getattr(r, "raw", None)
                uid = str(raw.get("dataset_uid") or "") if isinstance(raw, dict) else ""
                if uid:
                    texts[uid] = _record_text(r)
            vectors: dict = {}
            stale: "list[str]" = []
            for uid, t in texts.items():
                h = candidate_text_sha(t)
                entry = old.get(uid)
                if (
                    isinstance(entry, dict) and entry.get("h") == h
                    and isinstance(entry.get("v"), list) and len(entry["v"]) == EMBED_DIMENSIONS
                ):
                    vectors[uid] = entry
                else:
                    stale.append(uid)
            if stale:
                # 外部嵌入 worker 对单次请求有 MAX_TEXTS 上限（model_worker），整档重建必须分批
                # 喂入；进程内注入式嵌入器同样按批工作（顺序拼接，语义不变）。
                fresh: list = []
                for start in range(0, len(stale), MAX_TEXTS):
                    chunk = stale[start : start + MAX_TEXTS]
                    part = enc([texts[u] for u in chunk])
                    if not part or len(part) != len(chunk):
                        _warn_once("build_shape", "语料索引嵌入结果畸形 —— vector 召回回退规则序。")
                        return None
                    fresh.extend(part)
                for uid, vec in zip(stale, fresh):
                    v = [float(x) for x in vec]
                    if len(v) != EMBED_DIMENSIONS:
                        _warn_once("build_dims", "语料索引嵌入维度不符 —— vector 召回回退规则序。")
                        return None
                    vectors[uid] = {"h": candidate_text_sha(texts[uid]), "v": v}
            if stale or set(vectors) != set(old):
                _write_index(p, vectors)
                sig = _file_sig(p)
            _INDEX_CACHE[str(p)] = (sig, vectors)
            return vectors
    except Exception as exc:
        _warn_once(
            f"ensure_exc::{type(exc).__name__}",
            f"语料索引构建/加载异常（{exc!r}）—— vector 召回回退规则序。",
        )
        return None


def index_dense_vectors(
    query: str,
    items: "Sequence[object]",
    *,
    embedder: "Embedder | None" = None,
    paths=None,
) -> "list[list[float]] | None":
    """vector 本地数据源：返回 [查询向量, *文档向量]（与 recall_api.api_dense_vectors 同契约）。

    文档向量按「uid + 文本指纹」双校验查语料索引；未覆盖/指纹漂移的条目（用户上传、模板
    更新）现场补嵌——**仅内存、不持久化**。任何环节失败 → None（调用方回退规则序，绝不错位）。"""
    try:
        enc = embedder if embedder is not None else _local_embedder()
        if enc is None:
            _warn_once("embedder_unavailable", "本地嵌入模型不可用 —— vector 召回回退规则序。")
            return None
        store = ensure_index(embedder=enc, paths=paths)
        if store is None:
            return None
        texts = [_record_text(getattr(c, "record", c)) for c in items]
        doc_vecs: "list[list[float] | None]" = []
        missing: "list[int]" = []
        for i, cand in enumerate(items):
            raw = getattr(getattr(cand, "record", None), "raw", None)
            uid = str(raw.get("dataset_uid") or "") if isinstance(raw, dict) else ""
            entry = store.get(uid) if uid else None
            if (
                isinstance(entry, dict) and entry.get("h") == candidate_text_sha(texts[i])
                and isinstance(entry.get("v"), list) and len(entry["v"]) == EMBED_DIMENSIONS
            ):
                doc_vecs.append(entry["v"])
            else:
                doc_vecs.append(None)
                missing.append(i)
        queue = [query] + [texts[i] for i in missing]
        fresh = enc(queue)
        if not fresh or len(fresh) != len(queue):
            return None
        qv = [float(x) for x in fresh[0]]
        if len(qv) != EMBED_DIMENSIONS:
            return None
        for pos, i in enumerate(missing):
            v = [float(x) for x in fresh[pos + 1]]
            if len(v) != EMBED_DIMENSIONS:
                return None
            doc_vecs[i] = v
        return [qv, *doc_vecs]
    except Exception as exc:
        _warn_once(
            f"query_exc::{type(exc).__name__}",
            f"vector 查询向量组装异常（{exc!r}）—— 回退规则序。",
        )
        return None


def reset_caches_for_test() -> None:
    """测试专用：清空进程内索引缓存（生产代码绝不调用）。"""
    with _INDEX_LOCK:
        _INDEX_CACHE.clear()
    _EMBEDDER_CACHE.clear()
