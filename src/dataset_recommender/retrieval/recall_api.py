"""可选「API 向量召回」数据源（默认关；网页版服务器形态用，本机形态逐字节不变）。

方案A（智谱单厂商，2026-08-25 选型拍板）：
- dense 后端的向量 = **语料向量文件**（离线一次性嵌入、随部署物分发、带 model+dims
  元数据，启动校验不匹配拒启用）+ **查询侧 API 嵌入**（文件未覆盖的条目——如用户上传——
  现场 API 补嵌）；
- cross_encoder 后端的打分 = 智谱 **rerank API**（relevance_score 直接做排序键）。

设计约束（与 vector_recall 同源，defense-in-depth）：
- 本模块只做「给 vector_recall 供向量/供分」，排序/融合/截断数学一行不碰；
- **fail-closed**：API 失败/超时/限流/向量文件缺失或版本不匹配/任何异常 → 返回 None，
  由调用方回退规则序——检索主路（规则）永远不因 API 挂而不可用；
- 环境变量默认全 off → 本机/安装包形态行为**逐字节不变**（零调用、零导入 httpx）；
- key 只在进程环境（`BIODATA_EMBED_API_KEY`），绝不进前端/镜像层/git；
- 不记录、不打日志输出任何 key 或用户查询原文明文（usage 落账只记 tokens/模型/时间）。

限流口径（v1）：进程级滑动窗口 QPM（单 worker 既定架构下等价于全局限流）；超限即
回退规则序而非排队——宁可降级不可阻塞。按账号 QPM 留待账号上下文贯通检索管道后再做。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Sequence

from .vector_recall import _WARNED, _warn_once_prefixed

# ------------------------------------------------------------------ 配置（全 env，缺省 off）

_EMBED_API_ENV = "BIODATA_EMBED_API"            # off|zhipu（dense 查询侧 + 文件校验）
_RERANK_API_ENV = "BIODATA_RERANK_API"          # off|zhipu（cross_encoder 打分）
_EMBED_MODEL_ENV = "BIODATA_EMBED_MODEL"
_EMBED_DIMS_ENV = "BIODATA_EMBED_DIMENSIONS"
_VECTOR_FILE_ENV = "BIODATA_EMBED_VECTOR_FILE"
_API_KEY_ENV = "BIODATA_EMBED_API_KEY"
_BASE_URL_ENV = "BIODATA_EMBED_BASE_URL"
_RERANK_MODEL_ENV = "BIODATA_RERANK_MODEL"
_EMBED_QPM_ENV = "BIODATA_EMBED_QPM"
_RERANK_QPM_ENV = "BIODATA_RERANK_QPM"

_DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
_DEFAULT_EMBED_MODEL = "embedding-3"
_DEFAULT_EMBED_DIMS = 1024
_DEFAULT_RERANK_MODEL = "rerank"
_DEFAULT_EMBED_QPM = 60
_DEFAULT_RERANK_QPM = 30
#: 红线（方案A §4.4）：API 超时上限——超时即回退规则序，检索主路永不阻塞。
_EMBED_TIMEOUT_S = 5.0
_RERANK_TIMEOUT_S = 10.0
#: API 批次上限（智谱官方：embeddings 单次数组 ≤64；rerank documents ≤128）。
_EMBED_BATCH = 64
_RERANK_BATCH = 128

# _WARNED 集合与判定收编进 vector_recall._warn_once_prefixed（三模块共享同一集合，
# reset_caches_for_test 的 _WARNED.clear() 经共享集合仍生效），此处只留带前缀的薄 wrapper。
def _warn_once(key: str, message: str) -> None:
    """同一原因只在 stderr 提示一次；绝不抛异常、绝不打断请求（不留查询明文/key）。"""
    _warn_once_prefixed("[recall_api]", key, message)


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() == "zhipu"


def api_embed_enabled() -> bool:
    """dense 的 API 数据源是否启用（仅配置层；不代表文件/密钥已就绪）。"""
    return _env_flag(_EMBED_API_ENV)


def api_rerank_enabled() -> bool:
    """cross_encoder 的 API 打分是否启用（仅配置层）。"""
    return _env_flag(_RERANK_API_ENV)


def rerank_api_ready() -> bool:
    """cross_encoder 的 API 打分是否**就绪**（配置启用 + key 在位）——供选择层判定可用性。
    只探存在性，绝不回显 key。"""
    return api_rerank_enabled() and bool(_api_key())


def embed_api_ready() -> bool:
    """dense 的 API 数据源是否**就绪**（配置启用 + key 在位 + 语料向量文件通过 model/dims 校验）。
    只探存在性，绝不回显 key；任何内部异常收窄为 False（与 api_status 同口径）。"""
    try:
        return bool(api_embed_enabled()) and bool(_api_key()) and _load_vectors() is not None
    except Exception:
        return False


def api_status() -> dict:
    """健康端点用的只读快照（additive）：前端据此把「本地下载模型」卡改成「已在线」。

    embed=True 需要 env 启用 + 语料向量文件通过 model/dims 启动校验（双就绪才算真在线）；
    rerank 无本地资产，env 启用即报。只报布尔与模型名/维度，绝不回显 key。
    任何内部异常都收窄为 False——健康探测绝不能让 /api/health 500。"""
    try:
        embed_ok = bool(api_embed_enabled()) and _load_vectors() is not None
    except Exception:
        embed_ok = False
    return {
        "embed": embed_ok,
        "rerank": bool(api_rerank_enabled()),
        "model": _embed_model() if api_embed_enabled() else "",
        "dimensions": _embed_dims() if api_embed_enabled() else 0,
    }


def _embed_model() -> str:
    return os.getenv(_EMBED_MODEL_ENV, "").strip() or _DEFAULT_EMBED_MODEL


def _embed_dims() -> int:
    raw = os.getenv(_EMBED_DIMS_ENV, "").strip()
    try:
        return int(raw) if raw else _DEFAULT_EMBED_DIMS
    except ValueError:
        return _DEFAULT_EMBED_DIMS


def _rerank_model() -> str:
    return os.getenv(_RERANK_MODEL_ENV, "").strip() or _DEFAULT_RERANK_MODEL


def _base_url() -> str:
    return (os.getenv(_BASE_URL_ENV, "").strip() or _DEFAULT_BASE_URL).rstrip("/")


def _api_key() -> str:
    return os.getenv(_API_KEY_ENV, "").strip()


def _qpm(env_name: str, default: int) -> int:
    raw = os.getenv(env_name, "").strip()
    try:
        return max(1, int(raw)) if raw else default
    except ValueError:
        return default


# ------------------------------------------------------------------ 进程级 QPM 滑动窗口

class _QpmWindow:
    """进程级每分钟调用数窗口（线程安全）。超限返回 False——调用方回退，不排队。"""

    def __init__(self, env_name: str, default: int) -> None:
        self._env_name = env_name
        self._default = default
        self._stamps: "list[float]" = []
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            self._stamps = [t for t in self._stamps if now - t < 60.0]
            if len(self._stamps) >= _qpm(self._env_name, self._default):
                return False
            self._stamps.append(now)
            return True


_EMBED_QPM = _QpmWindow(_EMBED_QPM_ENV, _DEFAULT_EMBED_QPM)
_RERANK_QPM = _QpmWindow(_RERANK_QPM_ENV, _DEFAULT_RERANK_QPM)


# ------------------------------------------------------------------ usage 落账（无查询明文）

def _usage_log_path() -> "Path | None":
    try:
        from ..app.runtime_paths import get_app_paths
        return get_app_paths().userdata_dir / "embed_usage.jsonl"
    except Exception:
        return None


def _record_usage(kind: str, model: str, usage: object) -> None:
    """逐调用累计 tokens 落账（灰度期成本监控）。只记 tokens/模型/时间，绝不记查询文本。"""
    tokens = None
    if isinstance(usage, dict):
        raw = usage.get("total_tokens")
        if isinstance(raw, (int, float)):
            tokens = int(raw)
    path = _usage_log_path()
    if path is None:
        return
    line = {"ts": int(time.time()), "kind": kind, "model": model, "total_tokens": tokens}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 落账失败绝不阻塞检索


# ------------------------------------------------------------------ HTTP（httpx 惰性导入）

def _post(path: str, payload: dict, timeout_s: float) -> "dict | None":
    """POST 智谱 API；成功返回解析后的 JSON，任何失败返回 None（调用方回退）。

    可注入替换（单测 mock 四路：成功/超时/限流/版本不匹配）。"""
    key = _api_key()
    if not key:
        _warn_once("no_key", f"{_API_KEY_ENV} 未配置——API 召回回退规则序。")
        return None
    try:
        import httpx  # 惰性：本地形态（env off）永远不会走到这里
    except Exception:
        _warn_once("no_httpx", "未安装 httpx——API 召回回退规则序。")
        return None
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(
                f"{_base_url()}{path}",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
            )
        if resp.status_code != 200:
            _warn_once(
                f"http_{resp.status_code}",
                f"智谱 API 返回 HTTP {resp.status_code}——回退规则序（不重试本次）。",
            )
            return None
        return resp.json()
    except Exception as exc:
        # 超时/网络/解析异常一律回退；异常对象只留类型不留内容（防明文泄露进日志）。
        _warn_once(f"api_exc::{type(exc).__name__}", f"智谱 API 调用异常（{type(exc).__name__}）——回退规则序。")
        return None


# ------------------------------------------------------------------ 语料向量文件（加载+校验）

_VECTOR_CACHE: "dict | None" = None        # uid -> {"h": text_sha, "v": [floats]}
_VECTOR_LOAD_FAILED = False                # 失败不缓存内容，但避免每请求重读 60MB
_VECTOR_LOCK = threading.Lock()


def candidate_text_sha(text: str) -> str:
    """候选文本指纹：向量文件与查询侧用同一 sha 校验「模板未漂移」。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _vector_file_path() -> "Path | None":
    raw = os.getenv(_VECTOR_FILE_ENV, "").strip()
    return Path(raw) if raw else None


def _load_vectors() -> "dict | None":
    """惰性加载语料向量文件并做版本校验；不匹配/缺失/异常 → None（拒启用，防静默错版本）。

    文件格式（gzip JSON）：
      {"meta": {"model": ..., "dimensions": ..., "count": N},
       "vectors": {"<uid>": {"h": "<candidate_text_sha>", "v": [floats]}}}
    """
    global _VECTOR_CACHE, _VECTOR_LOAD_FAILED
    if _VECTOR_CACHE is not None:
        return _VECTOR_CACHE
    if _VECTOR_LOAD_FAILED:
        return None
    with _VECTOR_LOCK:
        if _VECTOR_CACHE is not None:
            return _VECTOR_CACHE
        if _VECTOR_LOAD_FAILED:
            return None
        path = _vector_file_path()
        ok = False
        try:
            if path is None or not path.exists():
                _warn_once("vf_missing", f"语料向量文件缺失（{_VECTOR_FILE_ENV}={path}）——dense-API 回退规则序。")
            else:
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    payload = json.load(fh)
                meta = payload.get("meta") or {}
                vectors = payload.get("vectors") or {}
                if (
                    meta.get("model") != _embed_model()
                    or int(meta.get("dimensions") or 0) != _embed_dims()
                    or not isinstance(vectors, dict)
                    or not vectors
                ):
                    _warn_once(
                        "vf_mismatch",
                        f"语料向量文件版本不匹配（文件 model={meta.get('model')}/"
                        f"dims={meta.get('dimensions')}，配置 {_embed_model()}/{_embed_dims()}）"
                        "——拒绝启用，dense-API 回退规则序。",
                    )
                else:
                    _VECTOR_CACHE = vectors
                    ok = True
        except Exception as exc:
            _warn_once("vf_exc", f"语料向量文件加载异常（{type(exc).__name__}）——dense-API 回退规则序。")
        if not ok:
            _VECTOR_LOAD_FAILED = True
        return _VECTOR_CACHE


def invalidate_vectors() -> None:
    """生产失效口：语料向量文件被语料同步 job 原子替换后调用 →
    下次 `_load_vectors` 重新读盘。失败标记一并清——新文件可能修好此前的缺失/坏档；
    若新文件仍不可用，`_load_vectors` 会重新走 warn-once + 失败标记路径，与启动时同口径。"""
    global _VECTOR_CACHE, _VECTOR_LOAD_FAILED
    with _VECTOR_LOCK:
        _VECTOR_CACHE = None
        _VECTOR_LOAD_FAILED = False


def reset_caches_for_test() -> None:
    """测试专用：清空向量文件缓存与告警集合（生产代码绝不调用）。"""
    global _VECTOR_CACHE, _VECTOR_LOAD_FAILED
    with _VECTOR_LOCK:
        _VECTOR_CACHE = None
        _VECTOR_LOAD_FAILED = False
    _WARNED.clear()


# ------------------------------------------------------------------ dense：查询侧向量组装

def _embed_texts(
    texts: "Sequence[str]",
    usage_sink: "object | None" = None,
    record_ledger: bool = True,
) -> "list[list[float]] | None":
    """查询侧 API 嵌入（分批 ≤64）；任一批失败 → None（整组回退，绝不错位）。

    usage_sink：可选回调 `usage_sink(kind, model, usage_dict)`，每次 HTTP 响应拿到
    usage 后在落账旁路同步回调（供调用方做实际用量对账）；默认 None 时行为逐位不变。
    record_ledger：False 时跳过 .userdata/embed_usage.jsonl 落账（调用方自建账本的
    场景——如 devcontext 直写共享 usage_events——从源头避免双计）；默认 True 逐位不变。"""
    out: "list[list[float]]" = []
    for start in range(0, len(texts), _EMBED_BATCH):
        chunk = list(texts[start:start + _EMBED_BATCH])
        if not _EMBED_QPM.allow():
            _warn_once("embed_qpm", "嵌入查询超进程级 QPM 上限——回退规则序。")
            return None
        payload = {"model": _embed_model(), "input": chunk, "dimensions": _embed_dims()}
        data = _post("/embeddings", payload, _EMBED_TIMEOUT_S)
        if data is None:
            return None
        rows = data.get("data")
        if not isinstance(rows, list) or len(rows) != len(chunk):
            _warn_once("embed_shape", "嵌入响应形状不符——回退规则序。")
            return None
        rows = sorted(rows, key=lambda r: r.get("index", 0))
        for row in rows:
            vec = row.get("embedding")
            if not isinstance(vec, list) or len(vec) != _embed_dims():
                _warn_once("embed_dims", "嵌入向量维度与配置不符——回退规则序。")
                return None
            out.append([float(x) for x in vec])
        if record_ledger:
            _record_usage("embed", _embed_model(), data.get("usage"))
        if usage_sink is not None:
            usage_sink("embed", _embed_model(), data.get("usage"))
    return out


def api_dense_vectors(
    query: str,
    items: "Sequence[object]",
) -> "list[list[float]] | None":
    """dense-API 数据源：返回 [查询向量, *文档向量]（与 vector_recall 下游契约同形）。

    文档向量优先查语料向量文件（按 uid + 候选文本指纹双校验，防模板漂移静默错版）；
    文件未覆盖/指纹不符的条目（用户上传、模板更新）现场 API 补嵌。任何环节失败 → None。
    """
    from .vector_recall import _candidate_text  # 惰性：避免循环导入

    vectors_file = _load_vectors()
    if vectors_file is None:
        return None

    texts = [_candidate_text(c) for c in items]
    # 与离线构建脚本 scripts/build_corpus_vectors.py 的 MAX_TEXT_CHARS 截断防御同款：
    # 两侧必须逐字同源，否则文本指纹对不上、文件条目全部误判「模板漂移」走补嵌。
    texts = [t[:12000] if len(t) > 12000 else t for t in texts]
    doc_vecs: "list[list[float] | None]" = []
    missing: "list[int]" = []
    for i, cand in enumerate(items):
        # uid 在归一记录的 raw 字典里（DatasetRecord 是 slots dataclass，无 dataset_uid
        # 属性；retriever.py:546 等同款口径）——取错位置会让文件命中分支整体空转。
        raw = getattr(getattr(cand, "record", None), "raw", None)
        uid = str(raw.get("dataset_uid") or "") if isinstance(raw, dict) else ""
        entry = vectors_file.get(uid) if uid else None
        if (
            isinstance(entry, dict)
            and entry.get("h") == candidate_text_sha(texts[i])
            and isinstance(entry.get("v"), list)
            and len(entry["v"]) == _embed_dims()
        ):
            doc_vecs.append(entry["v"])
        else:
            doc_vecs.append(None)
            missing.append(i)

    embed_queue = [query] + [texts[i] for i in missing]
    fresh = _embed_texts(embed_queue)
    if fresh is None or len(fresh) != len(embed_queue):
        return None
    for pos, i in enumerate(missing):
        doc_vecs[i] = fresh[pos + 1]
    return [fresh[0], *doc_vecs]


# ------------------------------------------------------------------ 通用文本语义接口（additive）
# 供非检索的内部批处理工具复用同一 HTTP/QPM/批次/形状校验/usage 落账/
# 密钥纪律（单通道原则：不复制第二套 API 客户端）。纯别名，产品检索行为零变化。


def api_embed_ready_simple() -> bool:
    """embed API 就绪判定的「无向量文件」形态：配置启用 + key 在位；不加载语料向量文件。"""
    return bool(api_embed_enabled()) and bool(_api_key())


def api_embed_texts(
    texts: "Sequence[str]",
    usage_sink: "object | None" = None,
    record_ledger: bool = True,
) -> "list[list[float]] | None":
    """通用文本嵌入（无 dataset 耦合）；分批/QPM/形状/维度校验与 usage 落账与 _embed_texts 同源。

    usage_sink：可选回调 `usage_sink(kind, model, usage_dict)`（实际用量对账旁路）；
    record_ledger：False 跳过 .userdata 落账（调用方自建账本时防双计）；
    缺省参数下产品行为逐位不变。"""
    return _embed_texts(texts, usage_sink=usage_sink, record_ledger=record_ledger)


# ------------------------------------------------------------------ cross_encoder：rerank 打分

def api_rerank_scores(
    query: str,
    texts: "Sequence[str]",
    usage_sink: "object | None" = None,
    record_ledger: bool = True,
) -> "list[float] | None":
    """rerank-API 打分：documents 分批 ≤128，relevance_score 直接做排序键。

    失败 → None（回退规则序）。分批间分数口径同为 query-doc 绝对相关度，
    灰度期可接受（bake-off 验证后再放量）。
    usage_sink：可选回调 `usage_sink(kind, model, usage_dict)`（实际用量对账旁路）；
    record_ledger：False 跳过 .userdata 落账（调用方自建账本时防双计）；
    缺省参数下产品行为逐位不变。"""
    if not texts:
        return []
    scores: "list[float | None]" = [None] * len(texts)
    for start in range(0, len(texts), _RERANK_BATCH):
        chunk = list(texts[start:start + _RERANK_BATCH])
        if not _RERANK_QPM.allow():
            _warn_once("rerank_qpm", "重排查询超进程级 QPM 上限——回退规则序。")
            return None
        payload = {
            "model": _rerank_model(),
            "query": query[:4096],
            "documents": [t[:4096] for t in chunk],
            "top_n": 0,
            "return_documents": False,
        }
        data = _post("/rerank", payload, _RERANK_TIMEOUT_S)
        if data is None:
            return None
        rows = data.get("results")
        if not isinstance(rows, list):
            _warn_once("rerank_shape", "重排响应形状不符——回退规则序。")
            return None
        for row in rows:
            idx = row.get("index")
            score = row.get("relevance_score")
            if isinstance(idx, int) and 0 <= idx < len(chunk) and isinstance(score, (int, float)):
                scores[start + idx] = float(score)
        if record_ledger:
            _record_usage("rerank", _rerank_model(), data.get("usage"))
        if usage_sink is not None:
            usage_sink("rerank", _rerank_model(), data.get("usage"))
    if any(s is None for s in scores):
        _warn_once("rerank_partial", "重排响应未覆盖全部候选——回退规则序。")
        return None
    return [float(s) for s in scores]
