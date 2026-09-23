from __future__ import annotations

import datetime
import hashlib
import hmac
import html as _html
import io
import ipaddress
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import traceback
import urllib.parse
import zipfile
from collections import OrderedDict, deque
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Annotated, Any, Callable, Iterator

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..corpus.corpus import available_sources, corpus_cache_generation, corpus_snapshot, invalidate_external_cache, known_source_values, load_full_corpus, load_normalized_corpus, locate_record, _base_source as _corpus_base_source
from ..corpus.downloads import file_count, files_for, primary_url
from . import accounts
from . import llm_quota
from . import mcp_tokens  # 在线 MCP 接入令牌库（轻量，import 零 I/O）
from ..llm import act_summary_llm, dream, search_reply_llm
from ..content import item_view
from ..llm.config import get_settings
from ..domain.registry import get_domain
from .accounts import AccountError, SESSION_COOKIE
from ..retrieval.fair import build_fair_report
from ..llm.llm_client import LLMConfig, diagnose_network, healthcheck, load_llm_config
from ..llm.llm_client import _normalize_provider as _llm_normalize_provider
from ..retrieval.query_parser import parse_query
from ..content.reuse_pack import ReusePackError, build_pack_for_uids, sanitize_uids, to_bibtex, to_ris
from ..content.reuse_pack import to_markdown as pack_to_markdown
from ..retrieval.search_request import resolve_search_request
from ..corpus.uploads import (
    UploadError,
    ingest_dataset,
    new_upload_name as _new_upload_name,
)
from .recommend_rows import rows_from_retrieved
from .workflow import (
    DatasetRecommendationWorkflow,
    RecommendParams,
    intent_projection,
    sanitize_facet_filters,
    sanitize_suppressed,
    sanitize_lenient_dims,
)
from .request_validation import (
    ParamValidationError,
    validate_date_window,
    validate_iso_date,
    validate_query,
    validate_sources,
)


def _validate_or_400(fn, *args, **kwargs):
    """共享校验束（app/request_validation）→ Web 翻译：ParamValidationError → 400 + hint
    + X-Error-Code 头（机器码此前只在 MCP 端保留，Web 端一律被剥掉，前端无法按码
    编程；本头 additive，不改变 detail 字符串文案）。"""
    try:
        return fn(*args, **kwargs)
    except ParamValidationError as exc:
        raise HTTPException(
            status_code=400, detail=exc.hint, headers={"X-Error-Code": exc.code}
        ) from exc


def _status_for(code: str, table: dict[str, int]) -> int:
    """机器码 → HTTP 状态的唯一翻译口（缺省 400）：账户/令牌/dream/下载各错误码表
    都经它查表，不再各写 `.get(code, 400)` 或内联三元。"""
    return table.get(code, 400)


from .runtime_paths import get_app_paths, instance_data_dir_for, resource_file_for
from .model_installer import cancel_model_install, model_install_status, start_model_install
from .limits import MAX_DATASETS_LIMIT  # 全库浏览分页上限单一真源（与 MCP browse_datasets 同源）

# W1 运行时路径解耦（安装器工程）：单一真源 runtime_paths.get_app_paths()。
# - PROJECT_ROOT = 实例数据根（source/portable = 项目根，历史逐字节一致；frozen = %LOCALAPPDATA%/BioDataAgent）：
#   所有**写盘**侧（accounts/upload/curate/trace/citations/oov/账本）以它为基，落 data 层。
# - CONFIG_ROOT  = LLM env 候选根（frozen = data_root/config；source = 项目根/.env 不变）：load_llm_config 专用。
# - RESOURCE_ROOT = 只读随包资源根（web/static、database/base、使用教程、prompts）：frozen = sys._MEIPASS。
# - DATA_DIR    = 冻结基准语料（只读，随包资源）：source = 项目根/database/base，frozen = resource_root/database/base。
PATHS = get_app_paths()
PROJECT_ROOT = PATHS.data_root
CONFIG_ROOT = PATHS.config_root
RESOURCE_ROOT = PATHS.resource_root
STATIC_DIR = RESOURCE_ROOT / "web" / "static"
DATA_DIR = PATHS.shipped_base_dir
# 用户上传的落盘/校验/打标核心已抽到 `uploads.py`（Web 与 MCP 共用单一真源）：
# _new_upload_name / ingest_dataset / UploadError 从那里导入（见上方 import）。
# 写入恒进 external 的**用户层**（source 下与官方快照同目录），绝不碰 base；来源缺省名/物种词表/文件名正则也在 uploads.py 内。

ENV_LOCK = threading.Lock()

WEB_API_VERSION = "3.0.0"

app = FastAPI(title=f"{get_domain().branding.product_name} Web UI", version=WEB_API_VERSION)
# 大列表/API JSON 在回环上也会占用显著的序列化与 WebView 传输时间。仅压缩 >=1KiB
# 的响应，小型 health/ack 不付压缩成本；compresslevel=6 取体积/延迟的稳健平衡。
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ---------------------------------------------------------------- 在线 MCP
# 同一 FastMCP 实例的在线形态：`/mcp` = streamable-HTTP + Bearer 令牌闸（账户级补丁作用域 +
# 纯确定性成本闸，见 mcp_server「在线形态」块）。挂载不用 app.mount 而是直挂 Route：保持
# PATH_INFO=/mcp 不变地转交子应用（其内部路由恰是 /mcp），避开 Mount 的前缀剥离语义。
# 本机形态同样挂载但铸币端点仅护栏形态开放 → 无令牌可发 → 恒 401，攻击面无变化。
# 最小运行时（requirements/requirements.txt，其文件头注释指路 使用教程/MCP安装）刻意
# 不含 mcp 包：无 mcp 的干净源码/便携安装降级为不挂 /mcp——web 本体与令牌管理 API
# （mcp_tokens 仅 stdlib）不受影响；本地 MCP 走独立 stdio 进程，不依赖此挂载。
from starlette.routing import Route as _Route  # noqa: E402

try:
    from . import mcp_server as _mcp_server  # noqa: E402  # 单点 import：MCP 域装配集中在此
except ModuleNotFoundError as exc:  # 仅「mcp 包未安装」才降级；mcp_server 自身其他断链照常抛出
    if exc.name != "mcp":
        raise
    _mcp_server = None

if _mcp_server is not None:
    _ONLINE_MCP_APP = _mcp_server.build_online_mcp_app()
    app.router.routes.append(_Route("/mcp", endpoint=_ONLINE_MCP_APP, methods=["GET", "POST", "DELETE"]))


@asynccontextmanager
async def _lifespan_with_mcp(_app):
    """宿主 lifespan：挂载不走子应用 lifespan → 在此驱动 MCP 会话管理器任务组
    （stateless 模式 handle_request 同样要求任务组已启动，SDK 实读确认）。
    session manager 每实例只能 run 一次 → 每次进入前先 reset_online_runtime() 重建，
    测试反复进出 lifespan 由此幂等。无 mcp 包的最小安装（_mcp_server=None）直接空转。"""
    if _mcp_server is None:
        yield
        return
    _mcp_server.reset_online_runtime()
    async with _mcp_server.mcp.session_manager.run():
        yield


app.router.lifespan_context = _lifespan_with_mcp

# 2026-08-15 （触发点审计 app-routes F2 / webapp 此前整体无
# logging，route_turn 兜底零堆栈零日志，是"难以定位"的最大残余盲区。这里建统一日志通道：
# 请求日志走下方 `_request_logging` middleware，异常兜底走 `logger.exception`（完整堆栈）。
# 无 handler 时挂一个 stderr handler 让日志默认可见；uvicorn/测试已配置过则不重复挂。
# 纪律：只记 method + 路径 + 状态码 + 耗时——绝不记 query string、绝不读请求体，
# api_key / 密码等敏感字段永不允许出现在日志行（与 `_redacted_validation_error` 同口径）。
logger = logging.getLogger(__name__)
if not logger.handlers:
    _stderr_handler = logging.StreamHandler()
    _stderr_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    logger.addHandler(_stderr_handler)
    logger.setLevel(logging.INFO)


@app.exception_handler(RequestValidationError)
def _redacted_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """安全：不回显用户提交的原始值（如密码 / API Key）——请求体校验失败时只保留 loc/msg/type，
    剔除 pydantic 默认带回的 `input` / `ctx`（对抗评审 #3；覆盖全部端点）。"""
    errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors}, media_type="application/json; charset=utf-8")


# 公开字符串/数组参数统一预算（体积闸）：请求模型解析期直接拦（超限 422），缺省/合法值不受影响。
# 数值都留足正常使用余量（净化逻辑里另有更严的收敛值，如 facet_filters ≤12）——这里拦的是
# 「解析期不设防」的原始体量，避免攻击者用巨型数组/超长字符串在模型解析前制造大分配。
_MAX_SOURCES_ITEMS = 50          # sources 来源池数组上限（现有来源十余个，余量充足）
_MAX_FACET_FILTERS_ITEMS = 64    # facet_filters 原始数组上限（净化后仍收敛到 ≤12，此处只挡解析放大）
_MAX_SUPPRESSED_ITEMS = 32       # suppressed_constraints 数组上限
_MAX_LENIENT_ITEMS = 16          # lenient_dims 数组上限
_MAX_CURRENT_FILTERS_ITEMS = 100  # current_filters 数组上限
_MAX_UIDS_ITEMS = 500            # task-pack selected_uids 数组上限
_MAX_API_KEY_CHARS = 512         # 请求级 API Key 长度上限
_MAX_MODEL_CHARS = 200           # 自定义模型名长度上限

# 跨端点重复的 schema 说明串单一真源：description 进 OpenAPI 文案，逐字共用，改文案只改这里。
_DESC_PROVIDER = "mock / zhipuai / openai-compatible / trial（T3 限量试用）"
_DESC_REQUEST_API_KEY = "本次请求临时 key，不持久化"
_DESC_CUSTOM_BASE_URL = "自定义 API 接口地址"
_DESC_CUSTOM_MODEL = "自定义模型名"


class _ExperimentContract(BaseModel):
    """实验归因三件套必须同在；缺一就拒绝，绝不把观察流量误标为随机实验。"""
    experiment_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    experiment_arm: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    propensity: float | None = Field(default=None, gt=0, le=1)

    @model_validator(mode="after")
    def _complete_experiment(self):
        present = (self.experiment_id is not None, self.experiment_arm is not None, self.propensity is not None)
        if any(present) and not all(present):
            raise ValueError("experiment_id/experiment_arm/propensity must be provided together")
        return self


class RecommendRequest(_ExperimentContract):
    query: str = Field(..., min_length=1, description="用户查询")
    provider: str = Field(default="mock", description=_DESC_PROVIDER)
    use_llm: bool = Field(default=False, description="大模型总开关：门控润色与一切请求级 LLM 能力")
    mock_llm: bool = Field(default=False)
    polish: bool = Field(default=True, description="AI 润色推荐说明（只改说明文字，不动结果与排序）。总开关 use_llm 之下的独立子开关")
    api_key: str | None = Field(default=None, max_length=_MAX_API_KEY_CHARS, description=_DESC_REQUEST_API_KEY)
    top_k: int | None = Field(default=None, ge=1, le=50, description="返回结果最大数量（默认 10，最大 50）")
    rerank: str = Field(default="off", description="可选 LLM 重排：off / llm")
    rerank_top_n: int | None = Field(default=None, ge=1, le=50, description="启用重排时喂给 LLM 的候选池大小")
    recall: str = Field(default="off", description="可选向量召回：off / dense（稠密嵌入） / cross_encoder（本地重排器，推荐）")
    strategy: str = Field(default="fixed", description="检索策略：fixed=使用显式 recall/rerank；auto=综合候选数量、语义信息量和可用后端，选择纯规则、本地语义、LLM 或两层组合。auto 会覆盖 recall/rerank 的显式取值")
    auto_allow_llm: bool = Field(default=False, description="strategy=auto 时是否允许在复杂查询中自动使用已配置 LLM；默认 false 避免意外联网")
    date_from: str | None = Field(default=None, description="发表时间范围起（ISO YYYY-MM-DD，含）；空=不限；格式或日历不合法 → 400（不静默忽略）")
    date_to: str | None = Field(default=None, description="发表时间范围止（ISO YYYY-MM-DD，含）；空=不限；格式或日历不合法 → 400（不静默忽略）")
    sources: list[str] | None = Field(default=None, max_length=_MAX_SOURCES_ITEMS, description="在哪些数据来源中检索（如 ['10x Genomics','CELLxGENE Discover']）；None=仅基础语料（官方评测不受影响）")
    auto_parse_sources: bool = Field(default=False, description="是否从 query 自动识别数据来源专名；前端自动来源模式开启，手动模式关闭")
    base_url: str | None = Field(default=None, description="自定义 API 接口地址（base_url）；留空则用服务器/预设默认")
    model: str | None = Field(default=None, max_length=_MAX_MODEL_CHARS, description="自定义模型名；留空则用服务器/预设默认")
    facet_filters: list[dict] | None = Field(default=None, max_length=_MAX_FACET_FILTERS_ITEMS, description="分面细化：在当前结果集上按维度精确收窄。value 取 facets[].values[].value 里回传的**分面键**（物种/组织/疾病为小写归一键，如 'homo sapiens'）；None=不细化")
    suppressed_constraints: list[str] | None = Field(default=None, max_length=_MAX_SUPPRESSED_ITEMS, description="忽略查询里已识别出的某个筛选条件（对应网页「查询条件」标签上的「忽略」）：取 query_constraints[].dim（物种/组织/疾病/平台/技术 + has_raw_data + date）。传入则该条件在检索前被放宽；None/空=全部条件照常生效，返回结果与不传本参数完全一致")
    lenient_dims: list[str] | None = Field(default=None, max_length=_MAX_LENIENT_ITEMS, description="把「未标注」的也纳入（对应网页结果区的「也纳入」按钮）：对这些维度（species/tissue/disease/platform/assay/modality）**字段为空的记录视作通过**（无法核验≠不匹配），已知是别的值仍然排除。取 coverage_caveats[].dim；None/空=不放宽，返回结果与不传本参数完全一致")


class TaskPackPreviewRequest(RecommendRequest):
    """一句话任务包的预览入参。沿用检索入参，另加条数与范围。"""
    limit: int | None = Field(default=None, description="打进包里的数据集条数（10 / 20 / 50，默认 10）。服务端在全部命中里按当前排序取前 N，与页面上显示几张卡无关")
    scope: str = Field(default="primary", description="primary=每个数据集只取一个代表性主文件（默认）；all=取全部文件")
    query_effective: str | None = Field(default=None, description="本工具实际用于检索的句子（开了关键词审核且被采纳时与原句不同）")
    keep_selected: list[str] | None = Field(default=None, description="重新预览时保留的勾选；新候选里还在的会继续勾上")


class TaskPackBuildRequest(BaseModel):
    """产包入参。**必须原样回传预览时拿到的那一整套**，服务端会用同样的参数重跑一遍再比对。"""
    model_config = ConfigDict(extra="forbid")

    plan_token: str = Field(..., description="预览时返回的 plan_token")
    selected_uids: list[str] = Field(..., max_length=_MAX_UIDS_ITEMS, description="用户勾选的数据集编号；必须是候选池的子集（少选合法）")
    snapshot_id: str = Field(default="", description="预览时的目录快照编号；指纹三件套缺一即 400（空=没走过预览，绝不放行）")
    content_digest: str = Field(default="", description="预览时的内容指纹；同上")
    retrieval_date: str = Field(default="", description="预览时的检索日期（ISO YYYY-MM-DD）；非法即 400——该值会拼进响应头文件名，绝不原样信任")
    scope: str = Field(default="primary")
    retrieval_params: dict = Field(..., description="预览时返回的 retrieval_params，原样回传")
    format: str = Field(default="zip", description="zip=直接下载压缩包；json=逐个文件的文本，由调用方自行落盘")


class BoardPlanRequest(BaseModel):
    """「再说一句话改条件」的规划入参。**无状态**：当前条件由调用方每次原样带上来。

    服务端刻意不存会话——存了就等于事实上的使用行为采集，而「要不要做使用数据采集」
    是产品方明确保留的未决策项。
    """
    query: str = Field(default="", description="产生当前这批结果的那句话（开了关键词审核时是改写后那句），不是输入框里的当前值")
    utterance: str = Field(default="", description="用户这一句原话，例如「换成小鼠」「去掉组织限制」")
    forced_op: str = Field(default="", description="点按钮时直接指定改动类型：replace / widen / restart / suggest / add / remove / lenient；留空则由 utterance 判断")
    dim: str = Field(default="", description="点按钮时直接指定要改哪一项（species / tissue / disease / platform / assay / modality / has_raw_data / date）")
    candidate_override: str = Field(default="", description="用户在预览框里手改后的整句；给了就以它为准")
    current_filters: list[dict] | None = Field(default=None, max_length=_MAX_CURRENT_FILTERS_ITEMS, description="上一次 /api/recommend 返回的 active_filters 原样。这是「现在按什么在筛」的唯一真源")
    resolution: dict | None = Field(default=None, description="上一次 /api/recommend 返回的 interpretation.resolution 原样，用于核对数据来源专名")
    suppressed_constraints: list[str] | None = Field(default=None, max_length=_MAX_SUPPRESSED_ITEMS, description="当前被忽略的筛选项编号")
    lenient_dims: list[str] | None = Field(default=None, max_length=_MAX_LENIENT_ITEMS, description="当前已放宽的项")
    facet_filters: list[dict] | None = Field(default=None, max_length=_MAX_FACET_FILTERS_ITEMS, description="当前「在结果里再缩小」的选择")
    coverage_dims: list[str] | None = Field(default=None, max_length=_MAX_CURRENT_FILTERS_ITEMS, description="上一次返回的 coverage_caveats 里的项，用来判断「放宽某一项」有没有意义")
    date_from: str | None = Field(default=None, description="年份下拉框的起始值")
    date_to: str | None = Field(default=None, description="年份下拉框的结束值")


class ActionPlanRequest(BaseModel):
    """一句话执行层的入参。**只规划，不执行**——见 `/api/action/plan` 的 docstring。"""
    model_config = ConfigDict(extra="forbid")

    utterance: str = Field(..., min_length=1, description="用户这一句原话")
    has_results: bool = Field(default=False, description="调用方屏幕上当前是否已有一批检索结果")
    result_total: int = Field(default=0, ge=0, description="当前这批结果的命中总数（调用方自述）")
    provider: str = Field(default="mock", description=_DESC_PROVIDER)
    use_llm: bool = Field(default=False)
    mock_llm: bool = Field(default=False)
    api_key: str | None = Field(default=None, max_length=_MAX_API_KEY_CHARS, description=_DESC_REQUEST_API_KEY)
    base_url: str | None = Field(default=None, description=_DESC_CUSTOM_BASE_URL)
    model: str | None = Field(default=None, max_length=_MAX_MODEL_CHARS, description=_DESC_CUSTOM_MODEL)


class UtteranceRequest(_ExperimentContract):
    """统一路由端点 `/api/utterance` 的入参（turn pipeline：规则匹配 → LLM 分流）。

    无状态：LLM 分流所需的现场（有无结果 / 当前查询 / 当前条件 / 来源池）由调用方
    每次原样带上来；LLM 配置覆盖沿用 `/api/recommend` 的请求级契约。
    """
    model_config = ConfigDict(extra="forbid")

    utterance: str = Field(..., min_length=1, description="用户这一句原话")
    has_results: bool = Field(default=False, description="调用方屏幕上当前是否已有一批检索结果")
    result_total: int = Field(default=0, ge=0, description="当前这批结果的命中总数（调用方自述）")
    # ---- LLM 分流上下文 ----
    query: str = Field(default="", description="产生当前这批结果的那句话（refine 改写以它为底）")
    current_filters: list[dict] | None = Field(default=None, max_length=_MAX_CURRENT_FILTERS_ITEMS, description="上一次 /api/recommend 返回的 active_filters 原样")
    sources: list[str] | None = Field(default=None, max_length=_MAX_SOURCES_ITEMS, description="当前选中的来源池（规则匹配概览按它取）")
    # ---- 课题上下文卡（2026-08-22 additive）----
    artifact_context: str | None = Field(
        default=None,
        max_length=2000,
        description="用户附加上下文（追踪/数据集快照，前端已截断 ≤2000 Unicode 字符）。"
                    "独立字段、不拼进用户原话：只进入 agent prompt 作结构化上下文块（首行带类型，标注「仅供参考」），"
                    "不进 identifier 快速道/query parser/quoted 证据/检索 query；本地演示/无 AI 模式被安全忽略",
    )
    # ---- 下一步行动建议动作（2026-08-22 additive）----
    suggested_recipe: str | None = Field(
        default=None,
        max_length=64,
        description="结果页阶梯 chip / 任务卡**未经编辑**的模板文本随请求携带的建议动作 id。"
                    "allowlist 校验在 action_plan.SUGGESTED_RECIPES（单一真源）：不在表 → "
                    "忽略回普通路由并如实记录（响应 recipe_note）；合法 → 只缩小到既有能力"
                    "（动词选择面收窄），不得绕过参数校验/执行开关/安全闸。编辑过模板 = 普通路由不携带",
    )
    # ---- 放松卡作答（2026-09-14 ask 暂停/重启批，additive）----
    ask_answer: dict | None = Field(
        default=None,
        description="用户在环内放松卡（ask.relax）上的作答：{kind: 恒 \"ask.relax\", "
                    "choice_key, label, message, suppress}。在场时本回合语义恒为「按所选忽略条件"
                    "重跑原查询」：agent 可用 → 续跑 ReAct 环（route_consensus 免投票直定 search、"
                    "understand 零 LLM 强置首步 rank、decide 注入作答事实段）；不可用 → 回退纯检索"
                    "路由（等同旧零 LLM 重搜）。suppress 清单过与 suppressed_constraints 同一净化闸",
    )
    # ---- LLM 配置覆盖（请求级，不持久化）----
    provider: str = Field(default="mock", description=_DESC_PROVIDER)
    use_llm: bool = Field(default=False)
    mock_llm: bool = Field(default=False)
    api_key: str | None = Field(default=None, max_length=_MAX_API_KEY_CHARS, description=_DESC_REQUEST_API_KEY)
    base_url: str | None = Field(default=None, description=_DESC_CUSTOM_BASE_URL)
    model: str | None = Field(default=None, max_length=_MAX_MODEL_CHARS, description=_DESC_CUSTOM_MODEL)
    # ---- 「AI 执行」开关（维度；2026-08-03 起 = LLM 分流器的总闸）----
    agent: bool = Field(
        default=True,
        description="开：所有消息 100% 过 LLM 分流（langgraph agent 优先、单次分类保底）；"
                    "关：LLM 分流器永不启动——一切输入按规则检索处理（操作句只回降级气泡）",
    )
    # ---- 流式开关----
    stream: bool = Field(
        default=False,
        description="true 时改发 text/event-stream：agent 规划节点 step* → final（final 体与非流式逐位同形）",
    )
    # ---- 幂等请求号（2026-08-08 修复）----
    req_id: str | None = Field(
        default=None,
        description="调用方生成的请求号：断流重发**同一句**时原样回传，服务端占用去重（重发拿缓存结果，"
                    "不二次执行写工具）；两次独立提交必须各自新号。None=无幂等，行为与不传逐位一致",
    )
    # ---- 当前检索参数（2026-08-16 ，additive；缺省=现状行为）----
    # 前端 ubRouteBody 与 runRecommend 发 /api/recommend 同源构造；后端只用于 pre-loop
    # 确定性检索与 preliminary_final 判定，校验口径与 /api/recommend 完全相同。
    top_k: int | None = Field(default=None, ge=1, le=50, description="返回结果最大数量（默认 10，最大 50）")
    rerank: str = Field(default="off", description="可选 LLM 重排：off / llm")
    recall: str = Field(default="off", description="可选向量召回：off / dense / cross_encoder")
    strategy: str = Field(default="fixed", description="检索策略：fixed / auto（同 /api/recommend 口径）")
    facet_filters: list[dict] | None = Field(default=None, max_length=_MAX_FACET_FILTERS_ITEMS, description="分面细化（同 /api/recommend）")
    suppressed_constraints: list[str] | None = Field(default=None, max_length=_MAX_SUPPRESSED_ITEMS, description="忽略的筛选条件 dim（同 /api/recommend）")
    lenient_dims: list[str] | None = Field(default=None, max_length=_MAX_LENIENT_ITEMS, description="「也纳入未标注」的维度（同 /api/recommend）")
    date_from: str | None = Field(default=None, description="发表时间范围起（ISO YYYY-MM-DD，含）；非法 → 400（同 /api/recommend）")
    date_to: str | None = Field(default=None, description="发表时间范围止（ISO YYYY-MM-DD，含）；非法 → 400（同 /api/recommend）")
    polish: bool = Field(default=True, description="AI 润色推荐说明子开关（同 /api/recommend 缺省口径）；只用于 preliminary_final 判定——polish 实际会跑 = use_llm ∧ polish")


class SearchRescueRequest(BaseModel):
    """检索救回端点 `/api/agent/search-rescue` 的入参（2026-08-16 检索工具化 Phase 1）。

    当前查询零命中时，让 agent 在**收敛面**（只允许 search.rerun / none）下尝试
    换一组查询词重跑本地检索；采纳与否由工具内机械择优闸裁定。LLM 配置覆盖沿用
    `/api/utterance` 的请求级契约。"""
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, description="当前（零命中的）查询句")
    sources: list[str] | None = Field(default=None, max_length=_MAX_SOURCES_ITEMS, description="当前选中的来源池（重检按它取，不因改写漂移换池）")
    current_filters: list[dict] | None = Field(default=None, max_length=_MAX_CURRENT_FILTERS_ITEMS, description="当前生效条件（现场回显用）")
    result_total: int = Field(default=0, ge=0, description="当前结果的命中总数（调用方自述，仅回显）")
    # 2026-08-18 screen-scope：字段名、默认值与 UtteranceRequest 逐位同义；老调用方
    # 不传时全空，保持原行为。换词只准换 query，不准静默清掉这些筛选条件。
    facet_filters: list[dict] | None = Field(default=None, max_length=_MAX_FACET_FILTERS_ITEMS, description="分面细化（同 /api/recommend）")
    suppressed_constraints: list[str] | None = Field(default=None, max_length=_MAX_SUPPRESSED_ITEMS, description="忽略的筛选条件 dim（同 /api/recommend）")
    lenient_dims: list[str] | None = Field(default=None, max_length=_MAX_LENIENT_ITEMS, description="「也纳入未标注」的维度（同 /api/recommend）")
    date_from: str | None = Field(default=None, description="发表时间范围起（ISO YYYY-MM-DD，含）；非法 → 400（同 /api/recommend）")
    date_to: str | None = Field(default=None, description="发表时间范围止（ISO YYYY-MM-DD，含）；非法 → 400（同 /api/recommend）")
    # ---- LLM 配置覆盖（请求级，不持久化，与 /api/utterance 同约）----
    provider: str = Field(default="mock", description=_DESC_PROVIDER)
    use_llm: bool = Field(default=False)
    mock_llm: bool = Field(default=False)
    api_key: str | None = Field(default=None, max_length=_MAX_API_KEY_CHARS, description=_DESC_REQUEST_API_KEY)
    base_url: str | None = Field(default=None, description=_DESC_CUSTOM_BASE_URL)
    model: str | None = Field(default=None, max_length=_MAX_MODEL_CHARS, description=_DESC_CUSTOM_MODEL)


class InterpretRequest(BaseModel):
    query: str = Field(..., min_length=1, description="待预览解析的用户查询")
    sources: list[str] | None = Field(default=None, max_length=_MAX_SOURCES_ITEMS, description="自动来源模式下的允许来源池，或手动模式下的显式来源")
    auto_parse_sources: bool = Field(default=False, description="是否从 query 自动识别数据来源专名")


class DiagnoseRequest(BaseModel):
    provider: str = Field(default="zhipuai", description=_DESC_PROVIDER)
    use_llm: bool = Field(default=True)
    mock_llm: bool = Field(default=False)
    api_key: str | None = Field(default=None, max_length=_MAX_API_KEY_CHARS, description="本次诊断临时 key，不持久化")
    base_url: str | None = Field(default=None, description="待诊断的 API 接口地址")
    model: str | None = Field(default=None, max_length=_MAX_MODEL_CHARS, description="待诊断的模型名")


class CuratePlanRequest(BaseModel):
    """对话式数据库管护的 plan 入参（preview，**零写盘**；search_online 的 plan 会联网查官方源并记请求账本）。"""
    model_config = ConfigDict(extra="forbid")

    action: str = Field(..., description="管护动作：list / import / search_online / remove / restore")
    query: str | None = Field(default=None, description="search_online 的搜索关键词")
    source: str | None = Field(default=None, description="import 的归属来源名 / search_online 的官方源键（默认 arrayexpress）")
    species: str | None = Field(default=None, description="search_online 的物种过滤（本地子串过滤）")
    limit: int | None = Field(default=None, ge=1, le=100, description="search_online 候选上限（默认 20，最大 100）")
    filename: str | None = Field(default=None, description="import 的落盘名 / remove 的 external 文件名 / restore 的回收站文件名（含时间戳前缀）")
    payload_json: str | None = Field(default=None, description="import 的数据集 JSON 文本（记录数组或 {\"records\":[…]} 对象）")


class CurateApplyRequest(CuratePlanRequest):
    """管护 apply 入参：**必须回传 plan 给的 confirm_token**；search_online 还需原样回传 plan_result。"""
    confirm_token: str = Field(..., description="plan 返回的 confirm_token；重算比对不一致 → token_mismatch，零写入")
    force: bool = Field(default=False, description="import 撞内容重复时仍确认入库（默认拒绝）")
    plan_result: dict | None = Field(default=None, description="search_online apply：plan 返回的完整结果（含 candidates），原样回传")


class CurateCheckUpdatesRequest(BaseModel):
    """`POST /api/curate/check-updates` 入参。**只读**：无 confirm_token、不落盘。"""
    model_config = ConfigDict(extra="forbid")

    sources: list[str] | None = Field(
        default=None,
        description="要检查的来源名（口语名即可，如「10x」「ArrayExpress」）；null/缺省 = 检查全部已注册来源",
    )


class CurateSyncUpdatesRequest(BaseModel):
    """`POST /api/curate/sync-updates` 入参（2026-08-06 `curate.sync_updates`）。

    检查更新 → 有新增则自动入库的复合流：写侧是 uploads 管线 + 账本 + 回收站可撤回
    （与 search_online 同一授权口径），故无 confirm_token——原子调用没有信任边界要跨。"""
    model_config = ConfigDict(extra="forbid")

    sources: list[str] | None = Field(
        default=None,
        description="要检查并同步的来源名（口语名即可）；null/缺省 = 全部已注册来源",
    )


class CurateRecallRequest(BaseModel):
    """`POST /api/curate/recall` 入参：要撤回的 sync operation_id。

    整次撤回 = 把该 operation 的 created_files[] 全部移入回收站（可重入；operation 不存在 →
    unknown_operation 400）。operation_id 从 sync_updates 返回的 receipt 或 `GET /api/curate/sync-status`
    的 last_operation_id 取。"""
    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(
        ...,
        description="sync_updates 返回的 operation_id（sync_ 开头）；最近一次可用 /api/curate/sync-status 查",
    )


class WatchCheckRequest(BaseModel):
    """`POST /api/watch/check` 入参：课题保存的**确定性检索 spec**。

    与 /api/recommend 入参子集同构（关键词/来源/分面/已删约束/宽放维度/日期 + spec_version）。
    端点**强制** strategy=fixed、recall=off、rerank=off、polish=false 重跑（基线语义 §4.1：
    不拿可能经 LLM/语义重排的显示结果当基线）。spec_version 当前只支持 v1（与
    record_fingerprint_schema 同版——检索规格与指纹 schema 一起版本化，未来升级同步加档）。"""
    model_config = ConfigDict(extra="forbid")

    spec_version: str = Field(
        default="v1",
        description="确定性检索规格版本（当前支持 v1；与 record_fingerprint_schema 同版）",
    )
    query: str = Field(default="", description="检索关键词（display_query 的解析结果）")
    sources: list[str] | None = Field(default=None, max_length=_MAX_SOURCES_ITEMS, description="来源白名单；null/缺省 = 默认检索池")
    facet_filters: list[dict] | None = Field(
        default=None, max_length=_MAX_FACET_FILTERS_ITEMS, description="分面细化过滤（同 /api/recommend 口径）")
    suppressed_constraints: list[str] | None = Field(
        default=None, max_length=_MAX_SUPPRESSED_ITEMS, description="「已命中里被删掉的」维度（同 /api/recommend 口径）")
    lenient_dims: list[str] | None = Field(
        default=None, max_length=_MAX_LENIENT_ITEMS, description="宽放维度——字段为空的记录视作通过（同 /api/recommend 口径）")
    date_from: str = Field(default="", description="发表时间起（YYYY-MM-DD）")
    date_to: str = Field(default="", description="发表时间止（YYYY-MM-DD）")


class ExportPackRequest(BaseModel):
    """`POST /api/artifacts/export-pack` 入参（2026-08-22 导出中心）。

    入参是课题**当前状态快照**：导出类型 + 课题的键与状态（name/goal/纳入排除条件/
    candidates（uid+status+reason+verified_at）/check_condition/provenance）。
    **数据集元数据全文由服务端从本地语料解析**（reuse-pack 的 keys-only 哲学）——
    前端不传元数据，杜绝「把数据集描述贴进来」吃进未发表工作的路径。
    `project` 是自由 dict、由 `export_pack.sanitize_snapshot` 逐字段归一（课程快照结构
    由前端 artifacts.js 保证，服务端只做防御性清洗，不复制一遍数据层 schema）。"""
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(
        ..., description="导出类型：download_list（下载清单）/ citations（引文）/ "
                         "screening_record（筛选记录）/ full（全部研究材料）")
    project: dict = Field(
        ..., description="课题当前状态快照（manifest 所需 uid+状态+理由+核验时间 + "
                         "check_condition + provenance；数据集内容由服务端从本地语料读取）")


class ActSummaryRequest(BaseModel):
    """执行结果 LLM 总结的入参（p10）。**只总结，不执行**——done/gap/policy 行由调用方
    从真实返回值构造后上报，本端点不核实、也不新增任何事实；LLM 缺席/失败 → fail-open，
    `summary_zh=None`，调用方原样保留自己的事实句。"""
    model_config = ConfigDict(extra="forbid")

    verb_zh: str = Field(..., min_length=1, max_length=100, description="动作的中文名（如「联网检索并入库」）")
    utterance: str = Field(default="", max_length=500, description="用户那句原话")
    ok: bool = Field(..., description="本次动作是否成功；False 时总结绝不能说「已」完成")
    done_lines: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=30, description="做到的事实行（每条 ≤500 字）")
    gap_lines: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=30, description="没做到的事实行（每条 ≤500 字）")
    policy_lines: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=30, description="口径说明行（每条 ≤500 字）")
    # ---- 一句话模式开关----
    brief: bool = Field(
        default=False,
        description="true 时走一句话模式（≤35 字、只用事实、ok=false 直说没做成，铁律写死在 prompt）；响应形状不变",
    )
    # ---- LLM 配置覆盖（与 /api/action/plan 同契约，请求级、不持久化）----
    provider: str = Field(default="mock", description=_DESC_PROVIDER)
    use_llm: bool = Field(default=False)
    mock_llm: bool = Field(default=False)
    api_key: str | None = Field(default=None, max_length=_MAX_API_KEY_CHARS, description=_DESC_REQUEST_API_KEY)
    base_url: str | None = Field(default=None, description=_DESC_CUSTOM_BASE_URL)
    model: str | None = Field(default=None, max_length=_MAX_MODEL_CHARS, description=_DESC_CUSTOM_MODEL)


class SearchReplyRequest(BaseModel):
    """检索回执 LLM 改写的入参（2026-08-30 p11）。**只解说，不检索、不执行**——命中数/展示数/
    命中关键词/解析状态由调用方（前端 board.js，数字全部取自真实检索响应）构造后上报，
    本端点不核实、也不新增任何事实；LLM 缺席/失败 → fail-open，`reply_zh=None`，
    调用方原样保留自己的确定性事实句。`can_suggest` 是下一步建议的**白名单**：
    LLM 只许从中原样挑一条，为空则禁止给任何建议（护栏写进 `search_reply_llm._SEARCH_REPLY_RULES_ZH`）。"""
    model_config = ConfigDict(extra="forbid")

    utterance: str = Field(default="", max_length=500, description="用户那句原话")
    query: str = Field(default="", max_length=500, description="实际检索词（可能被改写过）")
    note: str = Field(default="", max_length=200, description="前置说明（如「我把这句按『X』检索」/改条件留痕）；空=无")
    total: int = Field(default=0, ge=0, description="库中命中总条数")
    shown: int = Field(default=0, ge=0, description="结果区实际展示条数")
    hit_keywords: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=30, description="命中的关键词（原文）")
    resolution_status: str = Field(default="", max_length=50, description="解析状态（空=正常；abstained / clarification_required 等）")
    has_relax: bool = Field(default=False, description="结果区是否给出了可以直接点的放宽方式")
    can_suggest: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list, max_length=5, description="可建议动作白名单（空=禁止给建议）")
    # ---- LLM 配置覆盖（与 /api/act/summary 同契约，请求级、不持久化）----
    provider: str = Field(default="mock", description=_DESC_PROVIDER)
    use_llm: bool = Field(default=False)
    mock_llm: bool = Field(default=False)
    api_key: str | None = Field(default=None, max_length=_MAX_API_KEY_CHARS, description=_DESC_REQUEST_API_KEY)
    base_url: str | None = Field(default=None, description=_DESC_CUSTOM_BASE_URL)
    model: str | None = Field(default=None, max_length=_MAX_MODEL_CHARS, description=_DESC_CUSTOM_MODEL)


# 净化逻辑已上移到 workflow.sanitize_facet_filters / sanitize_suppressed（Web+MCP 单一真源）。
# 这里保留私有名作薄委托：既有调用点与测试无需改动，行为逐位不变。
def _sanitize_facet_filters(raw: object) -> list[dict]:
    """委托 workflow.sanitize_facet_filters（单一真源）。"""
    return sanitize_facet_filters(raw)


def _sanitize_suppressed(raw: object) -> list[str]:
    """委托 workflow.sanitize_suppressed（单一真源）。"""
    return sanitize_suppressed(raw)


def _sanitize_ask_answer(raw: dict | None) -> dict | None:
    """放松卡作答净化（2026-09-14 ask 暂停/重启批）：kind 恒 "ask.relax"——
    不符即 400（与端点其它入参校验同径；合法前端只产这一种）。suppress 过
    `_sanitize_suppressed`（与 suppressed_constraints 同一真源）；label/message/
    choice_key 收编为定长截断的纯文本（注入 decide prompt 的机械事实段，先裁再用）。
    None → None（未携带，绝大多数回合）。"""
    if raw is None:
        return None
    if not isinstance(raw, dict) or str(raw.get("kind") or "") != "ask.relax":
        raise HTTPException(
            status_code=400,
            detail="ask_answer.kind 目前只支持 \"ask.relax\"（环内放松卡作答）。",
            headers={"X-Error-Code": "bad_ask_answer"})
    return {
        "kind": "ask.relax",
        "choice_key": str(raw.get("choice_key") or "")[:64],
        "label": str(raw.get("label") or "")[:200],
        "message": str(raw.get("message") or "")[:500],
        "suppress": _sanitize_suppressed(raw.get("suppress")),
    }


def _sanitize_lenient_dims(raw: object) -> list[str]:
    """委托 workflow.sanitize_lenient_dims（单一真源）；回传排序 list 供 JSON 回显稳定。"""
    return sorted(sanitize_lenient_dims(raw))


def _require_iso_date(value: str | None, *, name: str) -> str:
    """发表时间入参：空 → ""（不限）；否则必须是格式与日历都合法的 YYYY-MM-DD，非法 → 400。

    为什么不能静默吞（旧行为：非「年份打头」一律当没传）：用户给了筛选条件、系统悄悄丢掉，
    结果和预期对不上却无任何提示；更糟的是 "2020-13-45" 这种不存在的日期曾被当作已生效
    条件回显上屏。诚实方向只有一个：给了就校验，不合法就明说。
    （本函数为薄委托：真源在 app/request_validation.validate_iso_date，与 MCP 同一份。）"""
    return _validate_or_400(validate_iso_date, value, name=name)


def _normalize_provider(provider: str | None) -> str:
    """webapp 设置面口径（封闭集）：默认 zhipuai、额外收编 trial，其余未知值一律回落 zhipuai
    （产品意图：用户输入静默收敛到已知通道，不报错）。真源在 llm_client._normalize_provider，
    勿再抄映射表。trial 必须独立成类——漏掉会落进 zhipuai 兜底，试用请求错烧正式 key。"""
    return _llm_normalize_provider(provider, default="zhipuai", extra_aliases={"trial"})


# 2026-08-16 检索工具化 Phase 1：卡片行投影的真源已迁入 app.recommend_rows.rows_from_retrieved
#（search.rerun 的采纳载荷与 /api/recommend 共用一份；落 recommend_rows 而非 workflow 是
# 冻结 767 评测路径的 import 闭包不许碰 introduction → summary_genre → provenance 链），
# 本模块保留同名别名——既有调用点与测试 import 零漂移。
_rows_from_retrieved = rows_from_retrieved


# 上传落盘/校验/打标核心已抽到 `uploads.py`（Web + MCP 共用单一真源）：
# `_new_upload_name` = uploads.new_upload_name（保留本模块名，安全测试 monkeypatch 此 seam）；
# 落盘 + 逐条打标 + 校验提示由 `uploads.ingest_dataset` 承担（见 api_upload）。


_LLM_SECRET_ENV_KEYS = (
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "ZAI_API_KEY",
    "ZHIPUAI_API_KEY",
    "ZHIPUAI_TOKEN",
)
_NONPUBLIC_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
_HOST_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_LEGACY_NUMERIC_LABEL_PATTERN = re.compile(r"^(?:[0-9]+|0x[0-9a-f]+)$")


def _endpoint_error(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=f"不安全的 API 接口地址：{message}")


def _validate_endpoint_url(raw_url: str | None) -> str:
    """Validate a request-controlled LLM endpoint without resolving DNS.

    Public endpoints must use HTTPS. Plain HTTP is intentionally limited to the
    three explicit local-development hosts supported by the UI. Literal IPs are
    classified locally, while domain names are validated syntactically only.
    The shared HTTP client separately disables automatic redirects.
    """
    value = (raw_url or "").strip()
    if not value:
        return ""
    # 公网护栏硬化：护栏模式（BIODATA_REQUIRE_ACCOUNT=1）下一律拒绝请求级
    # 自定义接口地址——上面的 SSRF 校验是「哪些地址合法」，这道是「网页版根本不接受自定义」。
    # 本函数是所有带 base_url 入口（recommend/diagnose/utterance/act/summary 等）的唯一必经
    # 校验点，收口一处即全覆盖；BYOK 的 api_key 不受影响（烧用户自己的 key）。闸关逐字节不变。
    if _account_gate_required():
        raise HTTPException(status_code=400, detail="网页版不支持自定义接口地址。")
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise _endpoint_error("地址不能包含空白或控制字符。")

    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise _endpoint_error("端口或 URL 格式无效。") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise _endpoint_error("只接受 https 地址（本机调试可用 http）。")
    if parsed.username is not None or parsed.password is not None:
        raise _endpoint_error("地址中不能包含用户名或密码。")
    if not parsed.hostname:
        raise _endpoint_error("缺少主机名。")
    if parsed.query or parsed.fragment or "?" in value or "#" in value:
        raise _endpoint_error("地址里不要带 ? 或 # 及之后的内容。")
    if port is not None and not (1 <= port <= 65535):
        raise _endpoint_error("端口必须在 1 到 65535 之间。")

    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise _endpoint_error("主机名不是有效的 IDNA 域名。") from exc

    if hostname == "localhost":
        if scheme != "http":
            raise _endpoint_error("localhost 仅允许使用 http 本地回环模式。")
        return value

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None

    if address is not None:
        explicit_loopbacks = {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")}
        if address in explicit_loopbacks:
            if scheme != "http":
                raise _endpoint_error("本机地址（127.0.0.1 等）只能用 http。")
            return value
        if scheme != "https":
            raise _endpoint_error("非回环地址必须使用 https。")
        if (
            not address.is_global
            or address.is_private
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise _endpoint_error("这个 IP 属于内网或保留地址，不能用作接口地址。")
        return value

    if scheme != "https":
        raise _endpoint_error("http 只允许 localhost、127.0.0.1 或 ::1。")
    if hostname.endswith(_NONPUBLIC_HOST_SUFFIXES):
        raise _endpoint_error("不能使用内网域名。")
    labels = hostname.split(".")
    if (
        len(labels) < 2
        or all(_LEGACY_NUMERIC_LABEL_PATTERN.fullmatch(label) for label in labels)
        or len(hostname) > 253
        or any(not _HOST_LABEL_PATTERN.fullmatch(label) for label in labels)
    ):
        raise _endpoint_error("主机名必须是有效的公网域名或公网 IP。")
    return value


def _endpoint_identity(url: str | None) -> tuple[str, str, int, str] | None:
    """Canonical comparison key for an already trusted or validated endpoint."""
    value = (url or "").strip()
    if not value:
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except (UnicodeError, ValueError):
        return None
    path = parsed.path.rstrip("/") or "/"
    return parsed.scheme.lower(), hostname, port, path


#: 部署期受信 Host 白名单（网页版灰度）：`BIODATA_TRUSTED_HOSTS` 显式给出公网 IP/域名
#: （逗号或空白分隔）。逐项严格校验（IP 字面量或 ≥2 段合法域名标签；拒绝端口/scheme/
#: userinfo/内网后缀域名），非法条目**启动即抛**（fail-closed，配置写错宁可不起服务）。
#: 缺省为空 → 与历史行为逐字节一致（仅 loopback），本机形态零影响。
_TRUSTED_HOSTS_ENV = "BIODATA_TRUSTED_HOSTS"


def _parse_trusted_request_hosts(raw: str | None) -> frozenset[str]:
    """解析 `BIODATA_TRUSTED_HOSTS` 为规范小写主机名集合；非法条目抛 ValueError。"""
    entries: list[str] = []
    for part in (raw or "").replace(",", " ").split():
        value = part.strip().lower().rstrip(".")
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            address = None
        if address is not None:
            entries.append(str(address))
            continue
        labels = value.split(".")
        if (
            not value
            or len(labels) < 2
            or len(value) > 253
            or value.endswith(_NONPUBLIC_HOST_SUFFIXES)
            or any(not _HOST_LABEL_PATTERN.fullmatch(label) for label in labels)
        ):
            raise ValueError(
                f"{_TRUSTED_HOSTS_ENV} 含非法主机名（只接受公网 IP 或 ≥2 段域名）: {part!r}"
            )
        entries.append(value)
    return frozenset(entries)


_TRUSTED_REQUEST_HOSTS: frozenset[str] = _parse_trusted_request_hosts(os.getenv(_TRUSTED_HOSTS_ENV))


def _is_supported_local_request_host(hostname: str | None) -> bool:
    """Accept loopback hosts, plus any host explicitly trusted for server deployment.

    本机形态（缺省）只认 loopback；服务器部署经 ``BIODATA_TRUSTED_HOSTS`` 显式放行
    灰度入口（公网 IP / 未来域名），白名单逐项校验过、不含通配。"""
    normalized = (hostname or "").strip().lower().rstrip(".")
    if normalized == "localhost":
        return True
    if normalized in _TRUSTED_REQUEST_HOSTS:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _raw_request_host(request: Request) -> tuple[str, int | None] | None:
    """从**原始 Host 头**解析 (hostname, port)；缺失/解析失败 → None（fail-closed）。

    为什么不读 ``request.base_url``：starlette 对匹配不上 ``_HOST_RE`` 的畸形 Host
    （如 ``127.0.0.1:not-a-port``）会静默回退成 uvicorn 绑定地址——畸形输入被「洗」成
    合法 loopback，基于 base_url 的守卫恒放行（真服已验证）。
    守卫必须自己看原始头：``urlsplit("//" + host)`` 后访问 ``port``，畸形端口在这里抛
    ValueError，一律 403，绝不 fail-open 成受信值。
    """
    raw = (request.headers.get("host") or "").strip()
    if not raw:
        return None
    try:
        parsed = urllib.parse.urlsplit(f"//{raw}")
        port = parsed.port  # 访问即校验：127.0.0.1:abc / 127.0.0.1:7981:80 等在这里抛 ValueError
    except ValueError:
        return None
    # userinfo（user@host）出现在 Host 头里**永远**是构造客户端：浏览器导航不会带。
    # 虽仍解析为 loopback、无 DNS 重绑定面，但 fail-closed 口径是「畸形即拒」，不留例外。
    if parsed.username is not None or parsed.password is not None:
        return None
    if not parsed.hostname:
        return None
    return parsed.hostname, port


def _cross_origin_detail(origin: str, *, scheme: str, hostname: str, port: int | None) -> str | None:
    """Origin 与受信请求源比对：可放行 → None；拒绝 → 403 文案。

    同源判定的单一真源：路由前 middleware（`_require_loopback_host`）与端点内纵深防御
    （`_require_same_origin`）共用，防两处规则分叉。"""
    try:
        supplied = urllib.parse.urlsplit(origin)
        supplied_port = supplied.port or (443 if supplied.scheme.lower() == "https" else 80)
    except ValueError:
        return "Origin 无效。"
    expected_port = port or (443 if scheme == "https" else 80)
    supplied_origin = (supplied.scheme.lower(), (supplied.hostname or "").lower(), supplied_port)
    expected_origin = (scheme, hostname.lower(), expected_port)
    if (
        supplied.scheme.lower() not in {"http", "https"}
        or supplied.username is not None
        or supplied.password is not None
        or supplied.path not in {"", "/"}
        or supplied.query
        or supplied.fragment
        or supplied_origin != expected_origin
    ):
        return "拒绝非同源请求。"
    return None


#: Host 闸两处（路由前 middleware + 端点内纵深防御）共用文案（与 `_cross_origin_detail`
#: 同理：同源判定的措辞只有一份，防两处分叉）。
_HOST_INVALID_DETAIL = "Host 无效。"
_UNTRUSTED_HOST_DETAIL = "仅接受本机 loopback Host。"


# ---------------------------------------------------------------- 原始 body 上限（体积闸）
# 全站统一预算：单细胞元数据 JSON 远小于 64 MB，超出的只可能是误操作或本机 DoS 试探。
# 与 /api/upload、/api/curate 的 payload 闸同值（`_MAX_UPLOAD_BYTES = _MAX_RAW_BODY_BYTES`，
# 一处常量，防口径再分叉）。上限在中间件**调用时**读取模块全局，测试 monkeypatch 常量即可生效。
_MAX_RAW_BODY_BYTES = 64 * 1024 * 1024
_UPLOAD_TOO_LARGE_DETAIL = "文件超过 64 MB 上限；单细胞元数据 JSON 一般远小于这个量级，请检查是否传错了文件。"


def _raw_body_too_large_response() -> JSONResponse:
    """413 响应体（中间件 Content-Length 预检直返用；计数路径走 HTTPException 同 detail）。"""
    return JSONResponse(
        status_code=413,
        content={"detail": _UPLOAD_TOO_LARGE_DETAIL},
        media_type="application/json; charset=utf-8",
    )


class _RawBodyLimitMiddleware:
    """原始请求 body 字节上限（体积闸）：Content-Length 预检 + 实际字节计数双闸。

    - Content-Length 头存在且超限 → 不读 body、直接 413 —— JSON/表单在 FastAPI/Pydantic
      模型解析**之前**被拒绝（payload_json 的 64 MB 检查不再发生在解析之后）。
    - chunked / 缺失 / 非数字 / 谎报 Content-Length：包一层计数 receive，累计超过上限
      立即抛 `HTTPException(413)` 中断下游 —— FastAPI 的 body 解析对 HTTPException 原样
      重抛（见 fastapi.routing 的 `except HTTPException: raise`），路由层把它转成 413
      响应；**拒绝前绝不占用完整内存**（multipart 临时文件同理不再先落满盘）。
    - 只作用于 POST/PUT/PATCH/DELETE；GET/HEAD/静态资源不拦。
    - 纯 ASGI 中间件（不走 BaseHTTPMiddleware）：计数 receive 原样透传给下游路由，端点
      读 body 的瞬间即被计数；中断发生在响应开始之前，回 413 安全无竞态。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") not in _UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return
        max_bytes = _MAX_RAW_BODY_BYTES
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break
                if declared > max_bytes:
                    await _raw_body_too_large_response()(scope, receive, send)
                    return
                break
        total = 0

        async def limited_receive() -> dict:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body") or b"")
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail=_UPLOAD_TOO_LARGE_DETAIL)
            return message

        await self.app(scope, limited_receive, send)


# 注册在 Host 守卫**之前**（源顺序第 0）→ 栈上位于 Host 守卫之内、路由之外：
# Host 守卫仍是最外层安全闸（跨源 + 非法 body 依旧 403 而非 422、DNS 改绑先拒）；
# 本中间件随后在路由分发与模型解析之前拦超限 body；它直返的 413 经过外层
# _security_headers（在其外）照常带上安全头。
app.add_middleware(_RawBodyLimitMiddleware)


# ---------------------------------------------------------------- 账号护栏
# 公网部署形态的总闸：`BIODATA_REQUIRE_ACCOUNT=1` 时，白名单外全部 `/api/*` 必须带有效
# 会话（cookie），否则 401 `{"ok":false,"error":"auth_required"}`（前端据此回登录视图）。
# 缺省关 → 本机单机形态逐字节不变。注册在 Host 守卫**之内**（源顺序更靠前 = 栈上更靠内）：
# 非法 Host / 跨源请求依旧先吃 403，与既有口径一致；只拦 `/api/` 前缀 → 静态前端与登录页
# 资源天然不拦。白名单是**精确路径**（尾部斜杠归一后比对，不可前缀误伤）。
_REQUIRE_ACCOUNT_ENV = "BIODATA_REQUIRE_ACCOUNT"
_INVITE_CODE_ENV = "BIODATA_INVITE_CODE"
_AUTH_OPEN_PATHS = frozenset({
    "/api/health",
    "/api/account/register",
    "/api/account/login",
    "/api/account/logout",
    "/api/account/whoami",
    # 2026-08-26：管理端点「开放但自认证」——token（BIODATA_ADMIN_TOKEN
    # X-Admin-Token 头，hmac.compare_digest 比对）+ 仅 loopback 对端双闸自足，不绑账户会话
    # （cron 从容器内 127.0.0.1 调用，没有也不该有登录态）。未配置 token → 403 fail-closed，
    # 放行进集合不等于放行请求（见 _require_admin）。
    "/api/admin/corpus-sync",
    "/api/admin/corpus-sync/status",
})


def _account_gate_required() -> bool:
    # 请求时读 env（与 _TRUSTED_HOSTS 的启动时读取不同）：测试 monkeypatch 即生效；
    # 一次 os.getenv 的成本相对请求处理可忽略。
    return os.getenv(_REQUIRE_ACCOUNT_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


@app.middleware("http")
async def _account_gate(request: Request, call_next):
    path = request.url.path.rstrip("/") or "/"
    user = None
    if path.startswith("/api/"):
        # 任务 3（2026-08-26 基线+补丁包）：会话解析提前到闸前、且不受护栏开关控制——只要
        # 请求带有效会话（含本机登录态），本请求的语料读写就绑定该账户的补丁包；无会话/匿名
        # → user None → 不绑定，读写路径与历史逐字节一致。resolve_session 默认是进程内 dict
        # 命中（_hydrate_sessions 一次性把会话库载入内存），每请求成本可忽略；opt-in Redis
        # 会话后端（BIODATA_SESSION_STORE=redis）下是每请求一次 Redis GET，毫秒级。
        user = accounts.resolve_session(request.cookies.get(SESSION_COOKIE), sessions_path=_sessions_store())
    if (_account_gate_required() and path.startswith("/api/")
            and path not in _AUTH_OPEN_PATHS and user is None):
        return _json_utf8(
            {"ok": False, "error": "auth_required", "detail": "请先登录后再使用。"},
            status_code=401)
    if user is not None:
        from ..corpus.patch_package import bind_patch_scope  # 惰性：webapp 模块顶层零新 import 边
        with bind_patch_scope(user.id):
            return await call_next(request)
    return await call_next(request)


@app.middleware("http")
async def _require_loopback_host(request: Request, call_next):
    """Reject DNS-rebinding Host values and cross-origin writes before any route is dispatched.

    同源闸放在这里（而不是只在端点函数体内第一行）的原因：FastAPI 的 pydantic 校验先于
    函数体执行，跨源请求带非法 body 时会先吃到 422 的参数结构细节（xss-sec）——
    middleware 在路由分发与校验之前拦，跨源 + 非法 body 得到 403 而不是 422。
    端点内的 `_require_same_origin` 保留作纵深防御（AST 门
    `test_every_post_route_checks_the_origin` 也要求每个写端点都调它）。"""
    parts = _raw_request_host(request)
    if parts is None:
        return JSONResponse(status_code=403, content={"detail": _HOST_INVALID_DETAIL})
    hostname, port = parts
    if not _is_supported_local_request_host(hostname):
        return JSONResponse(
            status_code=403,
            content={"detail": _UNTRUSTED_HOST_DETAIL},
        )
    origin = (request.headers.get("origin") or "").strip()
    if origin and request.method in _UNSAFE_METHODS:
        detail = _cross_origin_detail(
            origin, scheme=request.url.scheme.lower(), hostname=hostname, port=port,
        )
        if detail is not None:
            return JSONResponse(status_code=403, content={"detail": detail})
    return await call_next(request)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """全站安全响应头（xss-sec ：localhost 应用也有点击劫持面——Host 守卫挡不住
    浏览器带合法 loopback Host 的 <iframe> 导航）。注册在 Host 守卫之后 → 栈上更靠外，
    守卫直返的 403 也会带上这三个头。

    CSP 刻意**不加**：index/dataset 两页各有一个**内联** ``<script type="importmap">``
    （ESM 裸标识符映射；importmap 目前无法外链，只能内联），script-src 不放
    'unsafe-inline' 会直接掐死 importmap、整站起不来，放了又等于白配。点击劫持已由
    X-Frame-Options 兜住，CSP 的收益/风险不成比例。

    500 档兜底：端点里漏网的未捕获异常由**最外层** ServerErrorMiddleware 兜成 500，
    本 middleware 的 ``call_next`` 会直接抛出、走不到下面三行打头——500 曾是唯一缺
    安全头的档位。这里自己接住：完整堆栈留 stderr 供排查，客户端只见通用
    文案（异常正文可能含本机绝对路径，绝不上屏），三个头照常打上。"""
    try:
        response = await call_next(request)
    except Exception:
        traceback.print_exc()
        response = JSONResponse(status_code=500, content={"detail": "服务器内部错误，细节见服务端日志。"},
                                media_type="application/json; charset=utf-8")
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    # 静态资源缓存头：此前裸 StaticFiles 只发 etag/last-modified，
    # 浏览器走启发式缓存——指纹令牌（?v=）本就为破缓存而设（契约门强制内容变则令牌变），
    # 这里把语义写明：带指纹 → immutable 长缓存（内容变令牌必变，安全）；无指纹 → no-cache
    # 每次回源再验证（ETag 命中 304，只花一次往返）。HTML 骨架的 no-cache 在路由里另发、不受影响。
    if request.url.path.startswith("/static/"):
        if request.query_params.get("v"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
    return response


@app.middleware("http")
async def _request_logging(request: Request, call_next):
    """统一请求日志（2026-08-15 触发点/ "webapp 整体无 logging" 收口）。

    每请求一行：method + 路径 + 状态码 + 耗时。注册在最后 → 栈上最外层，Host 守卫直返的
    403、`_security_headers` 兜出的 500 也能记到最终状态码；5xx 升 WARNING 便于定位。
    脱敏口径：只取 `request.url.path`——不带 query string、绝不读 body，api_key / 密码
    等敏感字段因此结构上不可能进日志。流式端点的耗时只算到 StreamingResponse 交付
    （不含逐帧推送全程），日志行不改任何响应内容。"""
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    log = logger.warning if response.status_code >= 500 else logger.info
    log("HTTP %s %s -> %d (%.1fms)", request.method, request.url.path, response.status_code, elapsed_ms)
    return response


def _require_same_origin(request: Request) -> None:
    """Reject unsupported Host values and cross-origin browser POSTs.

    端点内的纵深防御：主闸已在路由前的 `_require_loopback_host` middleware
    （那里用**原始 Host 头**判定），这里仍按 base_url 复核一遍，双重口径。"""
    try:
        expected = urllib.parse.urlsplit(str(request.base_url))
        expected_port = expected.port  # 访问即校验畸形端口（与旧实现同语义；主闸在 middleware）
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=_HOST_INVALID_DETAIL) from exc
    if not _is_supported_local_request_host(expected.hostname):
        raise HTTPException(status_code=403, detail=_UNTRUSTED_HOST_DETAIL)

    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        return
    detail = _cross_origin_detail(
        origin,
        scheme=expected.scheme.lower(),
        hostname=expected.hostname or "",
        port=expected_port,
    )
    if detail is not None:
        raise HTTPException(status_code=403, detail=detail)


# ---------------------------------------------------------------- 简单频率限制（限流闸）
# 服务端共享 LLM Key 的可产生费用入口（`/api/introduction?llm=1`，GET）需要一道轻量闸：
# 同源检查（复用 `_require_same_origin`）+ 进程内滑动窗口频率限制。**不做权限体系**——
# 公网认证是已暂缓的独立 epic；本闸只挡「脚本高频烧服务端共享 Key」这类滥用，本机单
# 用户正常使用远低于配额。多进程/多实例部署需外置 Redis 级限流（见交接）。
_LLM_INTRO_RATE_LIMIT = 30        # 每分钟最多 N 次 llm=1 介绍
_LLM_INTRO_RATE_WINDOW = 60.0     # 秒
_rate_buckets: "dict[str, deque[float]]" = {}
_rate_lock = threading.Lock()


def _rate_limited(key: str, *, limit: int, window: float) -> bool:
    """滑动窗口频率限制：`key` 在 `window` 秒内已有 ≥`limit` 次 → 拒绝（返回 False）。

    进程内实现（报告建议的「简单频率限制」档）；成功调用会记录一次，超限调用不计数
    （不计失败，避免攻击者用超限请求把配额推满饿死正常用户）。线程安全：`_rate_lock`
    只保护这个进程内字典，不碰 ENV_LOCK——限流绝不能反过来阻塞 LLM 配置物化。"""
    now = time.monotonic()
    with _rate_lock:
        bucket = _rate_buckets.setdefault(key, deque())
        while bucket and bucket[0] <= now - window:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True


@contextmanager
def _temporary_env(overrides: dict[str, str | None]):
    old_values: dict[str, str | None] = {}
    for key, value in overrides.items():
        old_values[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, old in old_values.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _build_request_overrides(
    provider: str,
    use_llm: bool,
    mock_llm: bool,
    api_key: str | None,
    base_url: str | None = None,
    model: str | None = None,
    server_provider: str | None = None,
    server_base_url: str | None = None,
) -> dict[str, str | None]:
    overrides: dict[str, str | None] = {
        "LLM_PROVIDER": provider,
        "ENABLE_LLM": "true" if use_llm else "false",
        "MOCK_LLM": "true" if mock_llm else "false",
    }
    api_key = (api_key or "").strip() or None
    if provider in ("mock", "trial"):
        # trial（限量试用）：地址/模型锁定服务端托管值，请求带来的 base_url/model 一律
        # 丢弃（也不经 _validate_endpoint_url——锁定的通道不接受任何请求级端点）。
        base_url = ""
        model = ""
    else:
        base_url = _validate_endpoint_url(base_url)
        model = (model or "").strip()

    if api_key and provider != "trial":
        # LLM_API_KEY is deliberately first in load_llm_config's precedence.
        # Override it as well as the provider-specific key so a server secret
        # can never shadow the request-scoped credential.
        # （trial 永不接受请求级 key：试用通道凭据只能是服务端 BIODATA_TRIAL_API_KEY，
        #  请求带 key 也忽略，落到下方 else 的服务端密钥遮罩逻辑。）
        overrides["LLM_API_KEY"] = api_key
        if provider == "zhipuai":
            overrides["ZAI_API_KEY"] = api_key
        elif provider == "openai-compatible":
            overrides["OPENAI_API_KEY"] = api_key
    else:
        # No request-scoped credential.  A server secret is scoped to the
        # server's *actually configured* provider+endpoint, resolved without any
        # request-provider override (``load_llm_config(PROJECT_ROOT)`` at the call
        # site).  Mask every server secret whenever this request would otherwise
        # route it away from that configuration:
        #   * an explicit request base_url that differs from the server endpoint
        #     (the long-standing base_url-differs contract), or
        #   * a provider switch away from the server's configured provider, which
        #     would otherwise send a generic ``LLM_API_KEY`` to the new provider's
        #     default endpoint — a vendor the server never configured — even when
        #     the request carries no base_url.
        # Scoping against the server's *real* config (not the request-provider's
        # resolved endpoint) is what closes that provider-switch gap; comparing
        # the effective endpoint against the same server endpoint keeps a matching
        # provider with no request base_url from being masked (the server key then
        # flows to the server's own endpoint, as configured).
        # Empty strings intentionally shadow process/.env secrets while this
        # request runs because the dotenv loaders use setdefault/override=False.
        effective_endpoint = base_url or server_base_url
        provider_differs = provider != _normalize_provider(server_provider)
        endpoint_differs = _endpoint_identity(effective_endpoint) != _endpoint_identity(server_base_url)
        if provider_differs or endpoint_differs:
            overrides.update({key: "" for key in _LLM_SECRET_ENV_KEYS})

    # 自定义端点/模型：只在显式提供时注入对应 env（缺省不写 → 保留服务器 .env 配置）。
    # 让「填受校验的 OpenAI 兼容端点即可用」在网页端自助生效：DeepSeek/Kimi/Qwen/OpenRouter/本地皆走此路。
    if provider == "zhipuai":
        if base_url:
            overrides["LLM_BASE_URL"] = base_url
            overrides["ZHIPUAI_BASE_URL"] = base_url
        if model:
            overrides["LLM_MODEL"] = model
            overrides["ZHIPUAI_MODEL"] = model
    elif provider == "openai-compatible":
        if base_url:
            overrides["LLM_BASE_URL"] = base_url
            overrides["OPENAI_BASE_URL"] = base_url
        if model:
            overrides["LLM_MODEL"] = model
            overrides["OPENAI_MODEL"] = model
    return overrides


def _resolve_llm_request(
    provider: str | None,
    *,
    use_llm: bool,
    mock_llm: bool,
    base_url: str | None,
) -> tuple[str, str, bool, bool]:
    """请求级 LLM 四元组归一化（各端点同径）：provider 归一化 → mock/trial 丢弃请求级
    地址 → mock 推导 → use_llm 推导，返回 ``(provider, requested_base_url, mock_llm, use_llm)``。

    provider 只在启用 LLM 时才有意义：provider==mock 仅当 use_llm 时才当作 mock，
    否则 use_llm=false 会被 "provider==mock" 强行拉回 mock（那样"关闭 LLM"就形同虚设）。
    mock 从不联网、trial 地址锁定服务端托管值，两者的请求级 base_url 一律为空
    （也不经 `_validate_endpoint_url`——锁定的通道不接受任何请求级端点）。"""
    provider = _normalize_provider(provider)
    requested_base_url = "" if provider in ("mock", "trial") else _validate_endpoint_url(base_url)
    mock_llm = bool(mock_llm) or (provider == "mock" and bool(use_llm))
    use_llm = bool(use_llm or mock_llm)
    return provider, requested_base_url, mock_llm, use_llm


def _materialize_request_llm_config(
    provider: str,
    *,
    use_llm: bool,
    mock_llm: bool,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
    project_root: Path = CONFIG_ROOT,
    within: Callable[[], None] | None = None,
) -> LLMConfig:
    """请求级 LLM 配置物化唯一通道：ENV_LOCK 内以服务器真实配置为信任基准构造 env 覆盖，
    `_temporary_env` 窗口内物化不可变 LLMConfig——请求级 provider/key/endpoint 冻结进配置
    对象，并发请求的 env 覆盖串行安全，锁外下游不再读 os.environ（锁内最小化：锁内只做
    「读配置/物化」，网络 I/O 一律在锁外）。

    `within`（可选）在同一 env 窗口内执行——需顺带物化其它对象的调用方
    （/api/recommend 的 workflow 与 auto_llm_available）经它挂上，不另开窗口。"""
    with ENV_LOCK:
        # Trust baseline is the server's *actual* configuration (no request
        # provider override), so a provider switch that would route a generic
        # server key to another vendor's default endpoint is detected too.
        server_cfg = load_llm_config(project_root=CONFIG_ROOT)
        env_overrides = _build_request_overrides(
            provider=provider,
            use_llm=use_llm,
            mock_llm=mock_llm,
            api_key=(api_key or "").strip() or None,
            base_url=base_url,
            model=model,
            server_provider=server_cfg.provider,
            server_base_url=server_cfg.base_url,
        )
        with _temporary_env(env_overrides):
            if within is not None:
                within()
            return load_llm_config(project_root=project_root)


# ---------------------------------------------------------------- LLM 日配额（账号护栏）
# 只在护栏模式（BIODATA_REQUIRE_ACCOUNT=1）生效；本机单机形态 `_gate_llm_quota` 第一行即返。
# 计数口径：只计「将真实消耗**服务端** LLM」的请求——BYOK（请求自带 key）/ mock / 未启用 /
# 服务端无 key 一律不计（烧不到服务端配额）。试用通道（provider=trial）走独立的更紧的桶。
_LLM_DAILY_PER_USER_ENV = "BIODATA_LLM_DAILY_PER_USER"
_LLM_DAILY_GLOBAL_ENV = "BIODATA_LLM_DAILY_GLOBAL"
_LLM_QUOTA_EXEMPT_ENV = "BIODATA_LLM_QUOTA_EXEMPT"
_TRIAL_DAILY_PER_USER_ENV = "BIODATA_TRIAL_DAILY_PER_USER"
_TRIAL_DAILY_GLOBAL_ENV = "BIODATA_TRIAL_DAILY_GLOBAL"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("%s=%r 不是整数，按默认值 %d 处理", name, raw, default)
        return default


def _quota_exempt_users() -> frozenset[str]:
    """豁免名单（逗号/空白分隔用户名，产品方用）；比较一律小写（用户名本就规范化小写）。"""
    return frozenset(
        part.strip().lower()
        for part in os.getenv(_LLM_QUOTA_EXEMPT_ENV, "").replace(",", " ").split()
        if part.strip()
    )


def _gate_llm_quota(
    request: Request,
    *,
    cfg: LLMConfig | None,
    provider: str,
    use_llm: bool,
    mock_llm: bool,
    api_key: str | None,
    requested_base_url: str = "",
) -> None:
    """T3 账号级 LLM 日配额闸：本请求将真实消耗**服务端** LLM → 计数 + 超限 429。

    - `cfg` 已物化 → 直接据它判定；`cfg=None`（流式分支尚未物化）→ ENV_LOCK 内按请求
      覆盖链复算一份（与端点物化同径，绝不捞到别的请求临时注入的 env）。
    - BYOK / mock / 未启用 / 服务端无 key → 不计（烧不到服务端配额）。
    - 豁免名单（`_LLM_QUOTA_EXEMPT_ENV`）与闸关闭（本机形态）直接放行。
    """
    if not _account_gate_required():
        return
    if (api_key or "").strip():
        return  # BYOK：烧的是用户自己的 key，不占服务端配额
    provider = _normalize_provider(provider)
    if provider == "mock" or mock_llm or not use_llm:
        return
    if cfg is None:
        cfg = _materialize_request_llm_config(
            provider, use_llm=use_llm, mock_llm=mock_llm,
            api_key=None, base_url=requested_base_url, model=None)
    if cfg.mock_llm or not (cfg.enable_llm and cfg.api_key):
        return  # 服务端没 key / 未启用：LLM 根本不会真烧，不计
    user = accounts.resolve_session(request.cookies.get(SESSION_COOKIE), sessions_path=_sessions_store())
    if user is None:
        return  # 中间件已 401；此处只是纵深防御
    if user.username.lower() in _quota_exempt_users():
        return
    trial = _normalize_provider(cfg.provider) == "trial"
    per_user = _env_int(_TRIAL_DAILY_PER_USER_ENV, 30) if trial else _env_int(_LLM_DAILY_PER_USER_ENV, 100)
    global_limit = _env_int(_TRIAL_DAILY_GLOBAL_ENV, 500) if trial else _env_int(_LLM_DAILY_GLOBAL_ENV, 1000)
    try:
        llm_quota.check_and_increment(
            PROJECT_ROOT, user.username,
            trial=trial, per_user_limit=per_user, global_limit=global_limit)
    except llm_quota.QuotaExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None


def _json_utf8(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    # Keep Unicode text untouched and force explicit UTF-8 content type for clients
    # that otherwise decode JSON bytes using a local legacy code page.
    return JSONResponse(content=payload, status_code=status_code,
                        media_type="application/json; charset=utf-8")


# 遥测 meta 默认只是公开占位符。部署环境通过进程环境注入，源码因而无需
# 为 private/public 各维护一份 HTML，也不会把真端点/令牌提交进 git。
_TELEMETRY_META_ENV = {
    "http://<server-ip>:8471/v1/ingest": "BIODATA_TELEMETRY_ENDPOINT",
    "<your-ingest-token>": "BIODATA_TELEMETRY_TOKEN",
    "<server-ip>": "BIODATA_TELEMETRY_ALLOW_INSECURE",
}

# 部署注入页脚占位符（index.html 唯一锚点）：备案号等合规信息属部署侧数据，
# 真实值只存在于服务器 .env，经 BIODATA_SITE_FOOTER_HTML 在运行时原样注入。
_DEPLOY_FOOTER_PLACEHOLDER = "<!--deploy-footer-->"


def _index_html() -> str:
    text = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for placeholder, env_name in _TELEMETRY_META_ENV.items():
        value = str(os.environ.get(env_name) or "").strip()
        if value:
            text = text.replace(placeholder, _html.escape(value, quote=True))
    # 页脚与遥测 meta 语义不同：管理侧要输出原始 <a> 标记，故不转义、且空值时也
    # 清掉占位符（页面不留模板痕迹）。env 与 .env 同属管理侧可信输入，威胁模型同
    # BIODATA_TRUSTED_HOSTS；非管理侧绝无写入路径。
    footer = str(os.environ.get("BIODATA_SITE_FOOTER_HTML") or "").strip()
    text = text.replace(_DEPLOY_FOOTER_PLACEHOLDER, footer)
    return text


# GET+HEAD：健康检查/预取工具会对 `/` 发 HEAD，裸 @app.get 会回 405；显式允许 HEAD 保持幂等。
# Cache-Control: no-cache——HTML 骨架必须每次回源再验证（FileResponse 自带 ETag，未变则 304，成本一次往返）。
# 缺了它浏览器会启发式缓存旧 HTML：旧骨架（没有新挂点）+ 新 JS（查询串被 StaticFiles 忽略、照样给新内容）
# 混跑 = 新功能静默退回旧样式（2026-08-03 p10 后真机踩到「过时的侧边栏样式」正是这一族）。
@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def index() -> Response:
    return Response(_index_html(), media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})


# 数据集介绍详情页（从模态弹窗改为**独立浏览器标签页**）。查询参数 uid/url/name/source 由页面 JS
# 从 location.search 读取，再调既有 /api/introduction·/api/files·/api/fair·/api/compatible·/api/reuse-pack
# 渲染——本路由只静态返回页面骨架，**不做服务端渲染、不接受任何写入**，与 index 同为只读静态资源。
@app.api_route("/dataset", methods=["GET", "HEAD"], include_in_schema=False)
def dataset_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "dataset.html", headers={"Cache-Control": "no-cache"})


# 站点图标：内联 SVG（wordmark 的「o」标记），免掉浏览器对 /favicon.ico 的自动 404。
_FAVICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 22 22">'
    b'<rect width="22" height="22" rx="5" fill="#0d9488"/>'
    b'<circle cx="11" cy="11" r="5.4" fill="none" stroke="#ffffff" stroke-width="1.9"/>'
    b'<path d="M11 5.6 C6 8.6 16 12.4 11 15.4" fill="none" stroke="#ffffff" stroke-width="1.3" opacity=".7"/>'
    b"</svg>"
)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


@app.get("/api/domain/manifest")
def api_domain_manifest() -> JSONResponse:
    """活动领域清单（additive、只读）：品牌/维度/基础来源，供前端与接入方动态化。

    骨肉分离架构（docs/agent/DOMAIN_PACKS.md）：前端品牌文案不再硬编码，经本端点
    读取当前领域包声明；缺省 biodata 与旧版逐位一致。
    """
    pack = get_domain()
    b = pack.branding
    return JSONResponse({
        "ok": True,
        "domain": {
            "domain_id": pack.domain_id,
            "name_zh": pack.name_zh,
            "tagline_zh": pack.tagline_zh,
            "base_source": pack.base_source,
            "branding": {
                "product_name": b.product_name,
                "page_title": b.page_title,
                "dataset_page_title": b.dataset_page_title,
                "download_dir_prefix": b.download_dir_prefix,
                "corpus_blurb_zh": b.corpus_blurb_zh,
            },
            "dimensions": [{"name": d.name, "label_zh": d.label_zh} for d in pack.dimensions],
            "raw_data_labels": list(pack.raw_data_labels),
        },
    })


@app.get("/api/health")
def api_health(request: Request) -> JSONResponse:
    with ENV_LOCK:
        server_cfg = load_llm_config(project_root=CONFIG_ROOT)
        zhipu_cfg = load_llm_config(project_root=CONFIG_ROOT, provider_override="zhipuai")
        trial_cfg = load_llm_config(project_root=CONFIG_ROOT, provider_override="trial")
    from ..agent import agent_exec as _agent_exec
    body = {
            "ok": True,
            "service": "dataset-recommender-web",
            "version": WEB_API_VERSION,
            # additive：本实例的安装根路径。同版本多份安装并存时（开发副本 vs
            # 提交包副本），启动器复用分支据此告诉用户「复用的是哪一份」，绝不静默吸附。
            # 仅环回监听，路径不涉密。W1 起语义 = runtime_paths.install_root（frozen = exe 所在目录；
            # source/portable = 项目根，与历史逐字节一致）。
            # 公网护栏硬化：护栏模式下整条 key 不下发（公网实例的容器内路径
            # 不暴露给匿名请求）；本机形态原样保留。
            "install_root": str(PATHS.install_root),
            # additive（W1）：运行时模式，供启动器/诊断区分 source/portable/frozen。
            "runtime_mode": PATHS.runtime_mode,
            "zhipu_config_detected": bool(zhipu_cfg.api_key),
            # additive（账号护栏，2026-08-25）：前端登录门与注册邀请框的判定源——
            # required=是否强制登录；invite=注册是否需要邀请码（只报「要不要」，绝不回显码本身）。
            "account": {
                "required": _account_gate_required(),
                "invite": bool(os.getenv(_INVITE_CODE_ENV, "").strip()),
            },
            # 服务端 LLM 配置快照（2026-08-03 设置门控）：前端 llmCapable 判据的服务端半边——
            # 只报「有没有」（key_detected）与一致性比对所需的 provider/base_url，绝不回显 key 本身。
            "llm_server": {
                "key_detected": bool(server_cfg.api_key) and server_cfg.enable_llm and server_cfg.provider != "mock",
                "provider": server_cfg.provider,
                "base_url": server_cfg.base_url,
                # additive（T3）：限量试用通道——只报可用性/锁定模型名/每账号每日轮数上限，
                # key 本身绝不回显。available=False 时前端「限量试用」预设按未配置处理。
                "trial": {
                    "available": bool(trial_cfg.api_key) and trial_cfg.enable_llm,
                    "model": trial_cfg.model,
                    "daily_limit": _env_int(_TRIAL_DAILY_PER_USER_ENV, 30),
                },
            },
            # 可选扩展可用性：langgraph/langchain 装好且未被 env 关停 → True
            # False 时前端在「AI 执行」说明里标注用基础规划（不锁开关，后端自动回退）。
            "extensions": {"agent": _agent_exec.agent_available()},
            # additive（2026-08-26 方案A 放量）：智谱 API 向量召回/重排在线状态——网页版前端
            # 据此把「本地模型未安装/在线安装」卡换成「已在线」（服务器装不了也不必装本地模型）。
            # 缺省 env off → embed/rerank 均 False，本机形态展示逐字节不变。
            "recall_api": _recall_api_health(),
            # additive：语料代变更哨兵——前端登录后拿它比对
            # 本地记录的「上次见到的语料代」，不同才自动重跑追踪检查。**只是变更哨兵，不是
            # 可复现指纹**（external 段含进程内代际计数，重启即变）；算不出 → {"gen": null}。
            "corpus": {"gen": _corpus_gen_sentinel()},
        }
    if _account_gate_required():
        # 公网护栏硬化：install_root 整条 key 移除（非置空）。
        body.pop("install_root", None)
        # 数据脱敏批：匿名请求再收敛 llm_server——provider/base_url/模型名
        # 是服务端 LLM 出口细节，登录页只需要 account 块与可用性布尔；登录后 health
        # 重取回全量（前端据此做一致性比对/试用模型名上屏，两级互不缺料）。
        if accounts.resolve_session(request.cookies.get(SESSION_COOKIE), sessions_path=_sessions_store()) is None:
            llm = body.get("llm_server") or {}
            trial = llm.get("trial") or {}
            body["llm_server"] = {
                "key_detected": bool(llm.get("key_detected")),
                "trial": {
                    "available": bool(trial.get("available")),
                    "daily_limit": trial.get("daily_limit", 30),
                },
            }
    return _json_utf8(body)


def _recall_api_health() -> dict:
    """recall_api.api_status 的防爆包装：健康端点绝不因召回探测异常 500。"""
    try:
        from ..retrieval import recall_api  # 惰性：webapp 模块顶层零新 import 边

        return recall_api.api_status()
    except Exception:
        return {"embed": False, "rerank": False, "model": "", "dimensions": 0}


def _corpus_gen_sentinel() -> "str | None":
    """语料代变更哨兵：**无绑定** corpus_cache_generation 元组
    repr 的 sha256[:12]——不绑补丁作用域（健康端点在登录请求下会带账户补丁作用域，不带掉
    会变成每账户一个哨兵、且被用户上传扰动）。只是变更哨兵，不作对外可复现指纹
    （corpus_cache_generation 注释同口径）；语料未装载/任何异常 → None，health 绝不因此 500。"""
    try:
        from ..corpus.patch_package import unbound_patch_scope  # 惰性：webapp 模块顶层零新 import 边

        with unbound_patch_scope():
            gen = corpus_cache_generation(DATA_DIR, PROJECT_ROOT)
        return hashlib.sha256(repr(gen).encode("utf-8")).hexdigest()[:12]
    except Exception:
        return None


#: 护栏形态下本地模型写端点的统一拒绝文案（install/cancel 两处；status 只读不拦）。
_LOCAL_MODEL_WEB_DETAIL = "网页版使用在线向量服务，无需安装本地模型。"


@app.get("/api/local-model/status")
def api_local_model_status() -> JSONResponse:
    """只回普通状态/体积，不暴露本机路径、uv 输出或下载源原始错误。"""
    return _json_utf8({"ok": True, **model_install_status(PATHS)})


@app.post("/api/local-model/install")
def api_local_model_install(request: Request) -> JSONResponse:
    """用户显式触发后台在线安装；单飞，失败不影响规则排序。"""
    _require_same_origin(request)
    # 公网护栏硬化：网页版走在线向量服务，服务器上装本地模型没有意义
    # （且 install 会往容器里拉数 GB 文件）；status 只读保留。
    if _account_gate_required():
        raise HTTPException(status_code=403, detail=_LOCAL_MODEL_WEB_DETAIL)
    return _json_utf8({"ok": True, **start_model_install(PATHS)})


@app.post("/api/local-model/cancel")
def api_local_model_cancel(request: Request) -> JSONResponse:
    _require_same_origin(request)
    if _account_gate_required():
        raise HTTPException(status_code=403, detail=_LOCAL_MODEL_WEB_DETAIL)
    return _json_utf8({"ok": True, **cancel_model_install(PATHS)})


class AccountCredentials(BaseModel):
    username: str = Field(..., min_length=1, max_length=64, description="用户名")
    password: str = Field(..., min_length=1, max_length=200, description="密码（仅本机 scrypt 哈希存储，绝不明文/回显/记录）")
    remember: bool = Field(True, description="记住我：30 天免登录；false = 浏览器会话级 cookie")
    invite_code: str | None = Field(default=None, max_length=200, description="邀请码（仅护栏模式 BIODATA_REQUIRE_ACCOUNT=1 的注册需要；闸关时忽略）")


class AccountSwitchPayload(BaseModel):
    token: str = Field(..., min_length=1, max_length=200, description="待切换账号的会话 token（前端按账号记住的）")


_SESSION_MAX_AGE = accounts.SESSION_TTL_DAYS * 24 * 3600   # 与服务端 _SESSION_TTL 同源（2026-08-08 起 30 天）
_ACCOUNT_ERROR_STATUS = {
    "bad_username": 400, "weak_password": 400, "store_full": 400,
    "username_taken": 409, "invalid_credentials": 401, "locked": 429,
    "store_corrupt": 503, "store_unavailable": 503,
}


def _accounts_store() -> Path:
    return accounts.default_store_path(PROJECT_ROOT)


def _sessions_store() -> Path:
    return accounts.default_sessions_path(PROJECT_ROOT)


def _account_http_error(exc: AccountError) -> HTTPException:
    return HTTPException(status_code=_status_for(exc.code, _ACCOUNT_ERROR_STATUS), detail=exc.message)


def _set_session_cookie(resp: JSONResponse, token: str, *, remember: bool = True) -> None:
    # loopback http：HttpOnly + SameSite=Strict 已足够防跨站读写；本机无 https 故不置 Secure。
    # remember=False → 不传 max_age：浏览器会话级 cookie（关浏览器即失效）；True → 30 天。
    # Secure 口（公网护栏硬化，2026-08-26）：`BIODATA_COOKIE_SECURE=1` 时置 Secure——独立于护栏
    # 开关的单独 env：灰度纯 HTTP 阶段开了会让浏览器拒存 cookie（等于全员掉登录），故默认关，
    # TLS 落地后再开。
    resp.set_cookie(key=SESSION_COOKIE, value=token,
                    max_age=_SESSION_MAX_AGE if remember else None,
                    httponly=True, samesite="strict", path="/",
                    secure=os.getenv(_COOKIE_SECURE_ENV, "").strip().lower() in {"1", "true", "yes", "on"})


#: 会话 cookie 的 Secure 开关 env（默认关；公网 TLS 落地后由部署侧置 1）。与护栏开关互相独立。
_COOKIE_SECURE_ENV = "BIODATA_COOKIE_SECURE"

# 公网护栏硬化：护栏模式下登录/注册的 per-IP 进程内节流（复用 限流闸 的
# _rate_limited 滑动窗口）。防公网批量撞库/批量占号；本机形态（闸关）完全不加、逐字节不变。
_ACCOUNT_LOGIN_RATE_LIMIT = 10      # 每分钟每 IP 登录尝试上限
_ACCOUNT_REGISTER_RATE_LIMIT = 5    # 每分钟每 IP 注册尝试上限
_ACCOUNT_RATE_WINDOW = 60.0         # 秒


@app.post("/api/account/register")
def api_account_register(payload: AccountCredentials, request: Request) -> JSONResponse:
    """注册本地账户 → 立即登录（下发会话 cookie）。密码仅 scrypt 哈希本地存储，绝不明文/回显。
    响应带 `session_token`：loopback 本地工具，前端按账号记住它用于一键切换（效力等同 cookie）；
    护栏模式（公网）不下发该字段、一键切换端点同时关闭。"""
    _require_same_origin(request)
    if _account_gate_required():
        # per-IP 注册节流（仅护栏模式）：挡在邀请码校验之前，错误/无邀请码的尝试同样计数。
        host = request.client.host if request.client else "-"
        if not _rate_limited(f"account-register:{host}",
                             limit=_ACCOUNT_REGISTER_RATE_LIMIT, window=_ACCOUNT_RATE_WINDOW):
            raise HTTPException(status_code=429, detail="注册尝试过于频繁，请稍后再试。")
    # T3 注册邀请闸（仅护栏模式生效；闸关时 invite_code 字段被忽略、行为与现状逐字节一致）：
    # - 邀请码已配置 → 必须全等匹配，不匹配一律 403 统一文案（不泄漏对错细节，compare_digest 防时序）；
    # - 邀请码未配置 → 注册**整体关闭**（宁可关死不留缝：护栏模式 = 公网，开放注册 = 任何人烧服务端 key）。
    if _account_gate_required():
        invite = os.getenv(_INVITE_CODE_ENV, "").strip()
        if not invite:
            raise HTTPException(status_code=403, detail="本站暂未开放注册，请联系管理员开通账户。")
        if not hmac.compare_digest((payload.invite_code or "").strip(), invite):
            raise HTTPException(status_code=403, detail="邀请码不正确，请向管理员核对后再试。")
    try:
        user = accounts.register(payload.username, payload.password, store_path=_accounts_store())
    except AccountError as exc:
        raise _account_http_error(exc) from exc
    token = accounts.create_session(user, sessions_path=_sessions_store())
    body: dict[str, Any] = {"ok": True, "user": user.as_dict()}
    if not _account_gate_required():
        # session_token 仅本机形态下发（前端一键切换用）；公网护栏模式整条 key 不出现（收口）。
        body["session_token"] = token
    resp = _json_utf8(body)
    _set_session_cookie(resp, token, remember=payload.remember)
    return resp


@app.post("/api/account/login")
def api_account_login(payload: AccountCredentials, request: Request) -> JSONResponse:
    """登录本地账户 → 下发会话 cookie。失败信息不区分「用户不存在 / 密码错」（防枚举）。
    `session_token` 同 register：仅本机形态下发。"""
    _require_same_origin(request)
    if _account_gate_required():
        # per-IP 登录节流（仅护栏模式）：无论成败都计数，先于任何账号判定。
        host = request.client.host if request.client else "-"
        if not _rate_limited(f"account-login:{host}",
                             limit=_ACCOUNT_LOGIN_RATE_LIMIT, window=_ACCOUNT_RATE_WINDOW):
            raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试。")
    try:
        user = accounts.authenticate(payload.username, payload.password, store_path=_accounts_store())
    except AccountError as exc:
        raise _account_http_error(exc) from exc
    token = accounts.create_session(user, sessions_path=_sessions_store())
    body = {"ok": True, "user": user.as_dict()}
    if not _account_gate_required():
        body["session_token"] = token
    resp = _json_utf8(body)
    _set_session_cookie(resp, token, remember=payload.remember)
    return resp


@app.post("/api/account/logout")
def api_account_logout(request: Request) -> JSONResponse:
    """登出：销毁服务端会话（含落盘快照）+ 清除 cookie。"""
    _require_same_origin(request)
    accounts.destroy_session(request.cookies.get(SESSION_COOKIE), sessions_path=_sessions_store())
    resp = _json_utf8({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/account/whoami")
def api_account_whoami(request: Request) -> JSONResponse:
    """据会话 cookie 返回当前登录账户（无 / 过期 → user:null）；前端据此选记忆命名空间。"""
    user = accounts.resolve_session(request.cookies.get(SESSION_COOKIE), sessions_path=_sessions_store())
    return _json_utf8({"ok": True, "user": user.as_dict() if user else None})


def _require_session_user(request: Request) -> "accounts.PublicUser":
    """护栏形态端点共用闸：护栏形态 + 有效会话，否则 404/401
    （trial-quota 与在线 MCP 令牌端点共用）。"""
    if not _account_gate_required():
        raise HTTPException(status_code=404, detail="not found")
    user = accounts.resolve_session(request.cookies.get(SESSION_COOKIE), sessions_path=_sessions_store())
    if user is None:
        raise HTTPException(status_code=401, detail="auth_required")
    return user


@app.get("/api/account/trial-quota")
def api_account_trial_quota(request: Request) -> JSONResponse:
    """T3 限量试用通道的当日额度回显（设置界面「今日剩余」数据源，2026-08-25 夜）。

    additive：仅护栏形态提供（试用通道本就只存在于部署形态；本机形态 404，前端按
    「通道不可用」隐藏额度块）。remaining=None 表示该账号不限量（豁免名单或上限≤0），
    前端显示「不限量」；其余字段只报状态，key 绝不回显。"""
    user = _require_session_user(request)
    with ENV_LOCK:
        trial_cfg = load_llm_config(project_root=CONFIG_ROOT, provider_override="trial")
    daily_limit = _env_int(_TRIAL_DAILY_PER_USER_ENV, 30)
    exempt = user.username.lower() in _quota_exempt_users()
    used = llm_quota.usage_snapshot(PROJECT_ROOT, user.username, trial=True)
    unlimited = exempt or daily_limit <= 0
    return _json_utf8({
        "ok": True,
        "available": bool(trial_cfg.api_key) and trial_cfg.enable_llm,
        "model": trial_cfg.model,
        "daily_limit": daily_limit,
        "used": used,
        "remaining": None if unlimited else max(0, daily_limit - used),
        "unlimited": unlimited,
    })


@app.post("/api/account/switch")
def api_account_switch(payload: AccountSwitchPayload, request: Request) -> JSONResponse:
    """一键切换账号：校验前端记住的会话 token → 有效则把 cookie 重设到该账号。
    token 无效/过期 → 401（前端丢弃该条记忆、退回密码登录）。
    公网护栏硬化：护栏模式下一键切换整体关闭（session_token 不再下发，
    前端也不记 token——共用浏览器的公网场景里「点一下换成别人」不成立）。"""
    _require_same_origin(request)
    if _account_gate_required():
        raise HTTPException(status_code=403, detail="网页版不支持一键切换账号。")
    user = accounts.resolve_session(payload.token, sessions_path=_sessions_store())
    if user is None:
        raise HTTPException(status_code=401, detail="这个账号的登录状态已失效，请重新登录。")
    resp = _json_utf8({"ok": True, "user": user.as_dict()})
    _set_session_cookie(resp, payload.token, remember=True)
    return resp


# ---------------------------------------------------------------- 在线 MCP 接入令牌
# 网页版「接入 AI 助手」的在线形态凭证管理。仅护栏形态开放（本机形态 404，与 trial-quota
# 同款口径：在线 MCP 只存在于部署形态）。令牌明文仅铸币响应返回一次，落盘只有 sha256 摘要；
# 每账户上限见 mcp_tokens._MAX_TOKENS_PER_ACCOUNT。


def _mcp_tokens_store() -> Path:
    return mcp_tokens.default_tokens_path(PROJECT_ROOT)


def _online_mcp_config_hint(request: Request, raw_token: str) -> dict[str, Any]:
    """给 MCP 客户端的接入配置片段（streamable-HTTP 通用形状，Kimi Code / Claude 等同构）。
    Host 头已经过了受信主机闸（护栏形态），直接回显无风险。"""
    base = f"{request.url.scheme}://{request.headers.get('host', '')}"
    return {"url": f"{base}/mcp", "headers": {"Authorization": f"Bearer {raw_token}"}}


class McpTokenMintPayload(BaseModel):
    """铸币请求。label 仅用户自辨识（列表回显用），不落敏感信息。"""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="", max_length=40)


class McpTokenRevokePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_id: str = Field(min_length=1, max_length=32)


#: 令牌机器码 → HTTP 状态（缺省 400；超上限是冲突语义 → 409）。
_MCP_TOKEN_ERROR_STATUS = {"too_many_tokens": 409}


@app.post("/api/account/mcp-token")
def api_account_mcp_token_mint(payload: McpTokenMintPayload, request: Request) -> JSONResponse:
    """铸币 → 明文令牌仅此一次返回 + 可直接复制的客户端配置片段。"""
    _require_same_origin(request)
    user = _require_session_user(request)
    try:
        raw, rec = mcp_tokens.mint_token(user.id, user.username, payload.label,
                                         store_path=_mcp_tokens_store())
    except mcp_tokens.McpTokenError as exc:
        raise HTTPException(
            status_code=_status_for(exc.code, _MCP_TOKEN_ERROR_STATUS), detail=exc.message) from exc
    return _json_utf8({"ok": True, "token": raw, "record": rec,
                       "config": _online_mcp_config_hint(request, raw)})


@app.get("/api/account/mcp-tokens")
def api_account_mcp_tokens_list(request: Request) -> JSONResponse:
    """本账户令牌列表（公开字段：token_id/label/prefix/created_at；绝无明文/摘要）。"""
    user = _require_session_user(request)
    return _json_utf8({"ok": True, "tokens": mcp_tokens.list_tokens(user.id, store_path=_mcp_tokens_store())})


@app.post("/api/account/mcp-token/revoke")
def api_account_mcp_token_revoke(payload: McpTokenRevokePayload, request: Request) -> JSONResponse:
    """吊销（仅属主）：立即生效——verifier 每次请求实时查库，无缓存延迟。"""
    _require_same_origin(request)
    user = _require_session_user(request)
    if not mcp_tokens.revoke_token(user.id, payload.token_id, store_path=_mcp_tokens_store()):
        raise HTTPException(status_code=404, detail="令牌不存在或不属于当前账户。")
    return _json_utf8({"ok": True})


class DreamRequest(BaseModel):
    """dream 记忆整理（手动「整理记忆」按钮）：对话快照 + LLM 配置覆盖。

    服务端零存储：conversations 由前端从本机历史快照组织；LLM 配置随请求带来、绝不持久化
    （与 /api/utterance 同契约：请求级 key 优先，缺省回落服务端配置）。"""

    model_config = ConfigDict(extra="forbid")

    conversations: list[dict] = Field(default_factory=list, max_length=24, description="对话快照 [{query, chat:[{k,t,n}]}]")
    provider: str = Field(default="mock", description=_DESC_PROVIDER)
    api_key: str | None = Field(default=None, max_length=_MAX_API_KEY_CHARS, description=_DESC_REQUEST_API_KEY)
    base_url: str | None = Field(default=None, description=_DESC_CUSTOM_BASE_URL)
    model: str | None = Field(default=None, max_length=_MAX_MODEL_CHARS, description=_DESC_CUSTOM_MODEL)


_DREAM_ERROR_STATUS = {"empty_input": 400, "no_key": 400, "llm_failed": 502}


@app.post("/api/dream")
def api_dream(payload: DreamRequest, request: Request) -> JSONResponse:
    """dream 记忆整理：历史对话 → 封闭 JSON 记忆候选（generated:true；解析失败=空清单，绝不编造）。"""
    _require_same_origin(request)
    if payload.api_key:
        # 请求级 key 优先（同 /api/utterance 契约）：显式按请求 provider/端点/模型组配置。
        provider = "zhipuai" if payload.provider == "zhipuai" else "openai-compatible"
        cfg = LLMConfig(
            enable_llm=True,
            provider=provider,
            api_key=payload.api_key.strip(),
            base_url=_validate_endpoint_url(payload.base_url),
            model=(payload.model or "").strip(),
        )
    else:
        # 与 /api/introduction 同口径（2026-08-10 架构评审坐实漏网）：ENV_LOCK 内加载
        # 服务端 config，阻塞到并发请求的 _temporary_env 还原为止——绝不捞到别的请求临时
        # 注入 os.environ 的请求级 provider/key/endpoint。网络调用在锁外、用已捕获的 config。
        # T3：provider=trial（无 key 的试用请求）→ 显式按试用通道组配置（端点/模型锁定）；
        # 其余维持原语义（服务端 .env 配置）。
        with ENV_LOCK:
            if _normalize_provider(payload.provider) == "trial":
                cfg = load_llm_config(project_root=CONFIG_ROOT, provider_override="trial")
            else:
                cfg = load_llm_config(project_root=CONFIG_ROOT)
        # T3 配额闸：dream 恒为 LLM 调用（服务端 key 才计；BYOK 走上面分支不经这里）。
        _gate_llm_quota(request, cfg=cfg, provider=payload.provider, use_llm=True,
                        mock_llm=False, api_key=None)
    try:
        result = dream.dream_from_conversations(payload.conversations, config=cfg)
    except dream.DreamError as exc:
        raise HTTPException(status_code=_status_for(exc.code, _DREAM_ERROR_STATUS), detail=exc.message) from exc
    return _json_utf8(result)


class CurateExamplesRequest(BaseModel):
    """成功经验库候选的勾选/忽略（2026-08-13 起用户挑选入库）：候选 id 清单 + 端点坐标。

    base_url/model 只用于算端点指纹（分区键，同口径）；api_key 永不进这个端点。"""

    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(default_factory=list, max_length=200, description="候选行 id 清单")
    base_url: str = Field(default="", description="当前 AI 端点地址（算端点指纹用）")
    model: str = Field(default="", description="当前模型名（算端点指纹用）")


def _examples_partition(request: Request, base_url: str, model: str) -> tuple[str, str]:
    """成功经验库分区键（同口径）：会话账户 principal + 端点指纹 endpoint_fp。"""
    from types import SimpleNamespace

    from ..agent import agent_exec as _ax

    return _utterance_principal(request), _ax._endpoint_fp_from_config(
        SimpleNamespace(base_url=base_url, model=model))


@app.get("/api/curate-examples/pending")
def api_curate_examples_pending(request: Request, base_url: str = "", model: str = "") -> JSONResponse:
    """成功操作样例的候选池待选清单：只有本分区（同账户 + 同端点指纹）的行可见。"""
    from ..agent import agent_exec as _ax

    principal, fp = _examples_partition(request, base_url, model)
    rows = _ax.list_example_candidates(PROJECT_ROOT, principal=principal, endpoint_fp=fp)
    return _json_utf8({"ok": True, "candidates": rows})


@app.post("/api/curate-examples/approve")
def api_curate_examples_approve(payload: CurateExamplesRequest, request: Request) -> JSONResponse:
    """勾选入库：候选迁入正式库（注入侧只读正式库）；返回 approved/duplicated 计数。"""
    _require_same_origin(request)
    from ..agent import agent_exec as _ax

    principal, fp = _examples_partition(request, payload.base_url, payload.model)
    result = _ax.approve_example_candidates(PROJECT_ROOT, payload.ids,
                                            principal=principal, endpoint_fp=fp)
    return _json_utf8({"ok": True, **result})


@app.post("/api/curate-examples/dismiss")
def api_curate_examples_dismiss(payload: CurateExamplesRequest, request: Request) -> JSONResponse:
    """忽略：候选从池里删除（不进正式库）；返回 dismissed 计数。"""
    _require_same_origin(request)
    from ..agent import agent_exec as _ax

    principal, fp = _examples_partition(request, payload.base_url, payload.model)
    result = _ax.dismiss_example_candidates(PROJECT_ROOT, payload.ids,
                                            principal=principal, endpoint_fp=fp)
    return _json_utf8({"ok": True, **result})


@app.post("/api/interpret")
def api_interpret(payload: InterpretRequest, request: Request) -> JSONResponse:
    """只解析查询，不装载语料或执行排序；供输入框实时预览来源与时间约束。"""
    _require_same_origin(request)
    query = payload.query.strip()
    # 与 /api/recommend 同源的四道闸（原「空+2000」弱口径升级，见 request_validation）。
    _validate_or_400(validate_query, query)
    workflow = DatasetRecommendationWorkflow()
    resolution = resolve_search_request(
        query,
        payload.sources,
        known_source_values(workflow.settings.data_dir, workflow.settings.project_root),
        auto_parse_sources=bool(payload.auto_parse_sources),
    )
    intent = parse_query(resolution.parsed_query, workflow.settings.keyword_mapping)
    interpretation = resolution.as_dict()
    interpretation["effective_sources"] = list(resolution.sources or ["10x Genomics"])
    interpretation["intent"] = intent_projection(intent)
    return _json_utf8({"ok": True, "interpretation": interpretation})


# policy_id ——本次检索所用「语料快照 + 来源 + 排序策略 + 模型 + 应用/路由版本」的
# 结构化指纹，随 recommend/utterance(search) 响应 additive 返回；前端把它塞进遥测包，
# 分析端据此把行为数据锚定到确切检索配置（语料变了 snapshot_id 就变，无需猜时间窗）。
POLICY_ID_SCHEMA = "biodata-policy-id/1"
# turn.py 属另一写入者包，这里只登记路由层版本串（路由逻辑变更时手动升）。
ROUTE_POLICY_VERSION = "turn-route/v1"


def _build_policy_id(*, sources, strategy: str, rerank: str, recall: str, model) -> dict:
    """组装 policy_id（additive；任何一步失败都不掀翻检索——调用方包 try）。语料快照
    复用 corpus.corpus_snapshot 单一真源（snapshot_id 由内容决定，语料变了它才变）。"""
    records = load_normalized_corpus(DATA_DIR, PROJECT_ROOT, list(sources) if sources else None)
    snap = corpus_snapshot(records)
    return {
        "schema": POLICY_ID_SCHEMA,
        "corpus": {"snapshot_id": snap.get("snapshot_id", ""), "n_records": snap.get("n_records", 0)},
        "sources": [str(s) for s in sources] if sources else [_corpus_base_source()],
        "ranking": {"strategy": str(strategy or ""), "rerank": str(rerank or ""), "recall": str(recall or "")},
        "model": str(model or ""),
        "app_version": WEB_API_VERSION,
        "router_version": ROUTE_POLICY_VERSION,
    }


def _policy_id_or_none(**kwargs) -> dict | None:
    """_build_policy_id 的安全壳：组装失败（语料装载异常等）降级 None，绝不掀翻检索。"""
    try:
        return _build_policy_id(**kwargs)
    except Exception:
        return None


def _policy_token(value: Any, limit: int = 32) -> str:
    """policy_id_str 的可读段；完整差异仍由末尾摘要兜底。"""
    token = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("_")
    return (token or "unknown")[:limit]


def _policy_id_string(policy_id: dict | None) -> str | None:
    """结构化 policy_id 的稳定紧凑引用；两者随响应同时返回。"""
    if not isinstance(policy_id, dict):
        return None
    canonical = json.dumps(policy_id, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    corpus = policy_id.get("corpus") if isinstance(policy_id.get("corpus"), dict) else {}
    ranking = policy_id.get("ranking") if isinstance(policy_id.get("ranking"), dict) else {}
    parts = (
        ("snap", _policy_token(corpus.get("snapshot_id"), 16)),
        ("strategy", _policy_token(ranking.get("strategy"))),
        ("rerank", _policy_token(ranking.get("rerank"))),
        ("recall", _policy_token(ranking.get("recall"))),
        ("model", _policy_token(policy_id.get("model"))),
        ("app", _policy_token(policy_id.get("app_version"))),
        ("router", _policy_token(policy_id.get("router_version"))),
    )
    return "bpol1:" + ";".join(f"{key}={value}" for key, value in parts) + f";h={digest}"


def _policy_response_fields(policy_id: dict | None) -> dict[str, Any]:
    return {"policy_id": policy_id, "policy_id_str": _policy_id_string(policy_id)}


def _experiment_response(payload) -> dict[str, Any] | None:
    """回显服务端实际收到的完整随机化合同；普通流量返回 None。"""
    if not getattr(payload, "experiment_id", None):
        return None
    return {"id": payload.experiment_id, "arm": payload.experiment_arm,
            "propensity": payload.propensity}


@app.post("/api/recommend")
def api_recommend(payload: RecommendRequest, request: Request) -> JSONResponse:
    _require_same_origin(request)
    query = payload.query.strip()
    # query 四道闸（空/控制字符/纯符号/超长）收敛到共享真源——Web 侧原只有「空+2000」
    # 弱口径，控制/不可见字符与纯符号 emoji 输入与 MCP 口径不一致；现两端口径同源同措辞。
    _validate_or_400(validate_query, query)

    provider, requested_base_url, mock_llm, use_llm = _resolve_llm_request(
        payload.provider, use_llm=payload.use_llm, mock_llm=payload.mock_llm,
        base_url=payload.base_url)
    # 可选 LLM 重排，与润色解耦：只接受 off/llm，其余一律 off（安全默认）。
    rerank_backend = "llm" if str(payload.rerank or "").strip().lower() == "llm" else "off"
    # 可选向量召回：只接受 dense/cross_encoder/vector，其余一律 off（安全默认）。
    _recall = str(payload.recall or "").strip().lower()
    recall_backend = _recall if _recall in ("dense", "cross_encoder", "vector") else "off"
    # 发表时间范围（前端年份选择器 → ISO 起止）：给了就必须是合法 YYYY-MM-DD，否则 400——
    # 旧行为「非年份打头一律静默当没传」会把用户的条件悄悄丢掉，非法日期还会冒充生效条件上屏。
    date_from = _require_iso_date(payload.date_from, name="date_from")
    date_to = _require_iso_date(payload.date_to, name="date_to")
    # 2026-08-05：倒挂窗口（from > to）此前被静默接受、恒零结果还冒充合法生效条件
    # 上屏——用户在替一个不可能的条件读「没数据」。起晚于止必须当场 400 点名。
    # （闸体下沉共享真源，与 feasibility/task-pack/MCP 同一份。）
    _validate_or_400(validate_date_window, date_from, date_to)
    # 2026-08-05（其二）：来源校验对齐 MCP `_validate_sources`——未知/空白来源名
    # 此前被静默过滤成恒零结果，用户无法区分「来源写错」与「来源里真没数据」。显式 400 并列出合法名。
    # （闸体下沉共享真源 app/request_validation.validate_sources。）
    if payload.sources:
        _validate_or_400(validate_sources, payload.sources,
                         known=known_source_values(DATA_DIR, PROJECT_ROOT))
    # 分面细化过滤项：收敛为白名单维度 + 非空值（缺省/非法 → 空 → run_with_meta 里整段 no-op）。
    facet_filters = _sanitize_facet_filters(payload.facet_filters)
    # 「已命中」里被删掉的原始命中维度：收敛为白名单 dim（缺省/非法 → 空 → 检索前不放宽任何约束）。
    suppressed_constraints = _sanitize_suppressed(payload.suppressed_constraints)
    # 诚实降级：被用户「也纳入未标注的」的维度（缺省/非法 → 空 → passes_hard_filter 逐位 no-op）。
    lenient_dims = _sanitize_lenient_dims(payload.lenient_dims)
    # 检索策略：只接受 fixed/auto，其余一律 fixed（安全默认）。auto → 分类器按候选压力、语义信息和可用后端选择 recall/rerank。
    strategy = "auto" if str(payload.strategy or "").strip().lower() == "auto" else "fixed"
    # auto 偏好解析（唯一真源 resolve_auto_recall_preference）：偏好序 vector > cross_encoder，
    # 传「可执行」语义——本地模型可加载或 API 数据源就绪均计为可用；两者皆无则回退确定性词面序（不报错）。fixed 时无所谓。
    recall_available = None
    preferred_recall = "cross_encoder"
    if strategy == "auto":
        from ..retrieval.vector_recall import resolve_auto_recall_preference
        preferred_recall, recall_available = resolve_auto_recall_preference()

    _workflow_box: dict[str, Any] = {}

    def _attach_workflow() -> None:
        # 与主配置同一 env 窗口执行：构造器不读 LLM env，但 auto 探测判定的是「本请求
        # 覆盖链下服务端是否有 key」，必须罩在窗口里。
        _workflow_box["workflow"] = DatasetRecommendationWorkflow()
        auto_llm_available = None
        if strategy == "auto":
            auto_llm_available = bool(load_llm_config(project_root=CONFIG_ROOT).api_key) if payload.auto_allow_llm else False
        _workflow_box["auto_llm_available"] = auto_llm_available

    # 物化请求级基准 config（与 workflow 的 `_effective_llm_config` 同根 project_root
    # = get_settings().project_root）；整条 workflow（含 60s LLM 请求）在锁外执行。
    request_llm_config = _materialize_request_llm_config(
        provider, use_llm=use_llm, mock_llm=mock_llm,
        api_key=payload.api_key, base_url=requested_base_url, model=payload.model,
        project_root=get_settings().project_root, within=_attach_workflow)
    workflow = _workflow_box["workflow"]
    auto_llm_available = _workflow_box["auto_llm_available"]
    # 配额闸：润色或 AI 重排将走服务端 key 时计数（BYOK 不计）。
    # cfg 已物化（request_llm_config），流式无涉——本端点非流式。
    _gate_llm_quota(
        request, cfg=request_llm_config, provider=provider,
        use_llm=bool(use_llm or rerank_backend == "llm"),
        mock_llm=mock_llm, api_key=payload.api_key)
    meta = workflow.run_with_meta(
        RecommendParams(
            query=query,
            top_k=payload.top_k,
            # 润色是总开关（use_llm）之下的独立子开关（polish，2026-08-03 设置重构）：
            # workflow 的 use_llm 只门控说明润色；重排由 rerank_backend 独立选择。
            use_llm=use_llm and bool(payload.polish),
            mock_llm=mock_llm,
            provider=provider,
            rerank_backend=rerank_backend,
            rerank_top_n=payload.rerank_top_n,
            recall_backend=recall_backend,
            date_from=date_from,
            date_to=date_to,
            sources=payload.sources,
            auto_parse_sources=bool(payload.auto_parse_sources),
            facet_filters=facet_filters,
            suppressed_constraints=suppressed_constraints,
            lenient_dims=lenient_dims,
            strategy=strategy,
            recall_available=recall_available,
            llm_available=auto_llm_available,
            preferred_recall=preferred_recall,
            base_llm_config=request_llm_config,
        )
    )

    # 结果直接取检索器的结构化候选（确定性、含真实 reason/score），
    # 不依赖 LLM 改写或 markdown 解析；markdown 仅作「原始输出」展示。
    results = _rows_from_retrieved(meta.retrieved_data)
    warnings: list[str] = []
    if meta.fallback_reason:
        warnings.append(meta.fallback_reason)
    # 2026-08-15 触发点use_llm=true 但 provider 缺省未传 → 被静默拽回 mock，
    # 调用方预期真 LLM 润色、实际拿假策展表。行为不变，warnings 留痕让这一步可见。
    if (provider == "mock" and bool(payload.use_llm) and not bool(payload.mock_llm)
            and "provider" not in payload.model_fields_set):
        warnings.append("未指定 provider，本次按 mock 处理（如需真实 LLM，请显式指定 provider）")

    # OOV 词表闭环第一段（2026-08-09 五机制批）：未收录词落结构化日志
    # （.userdata/oov_terms.jsonl）——词表生长的真数据源；`scripts/measure_entity_gap.py
    # --oov-report` 把它聚合成 vocabulary 候选别名报告。只在本端点（真实用户查询）记，
    # 官方评测直调 retriever 结构性不经过。日志自身失败绝不掀翻检索（与联网账本同纪律）。
    # 2026-09-10 起双触发：弃权（自动降级失败、原 intent 带 unresolved_terms）与
    # 自动降级成功（degraded_search.ignored_terms）——被忽略的词同样是词表生长数据。
    try:
        _iproj = (getattr(meta, "interpretation", {}) or {}).get("intent") or {}
        _oov: list[str] = []
        if (getattr(meta, "resolution_status", "") == "abstained"
                and _iproj.get("abstain_reason") == "unresolved_term"):
            _oov = [str(t).strip() for t in (_iproj.get("unresolved_terms") or []) if str(t).strip()]
        elif getattr(meta, "resolution_status", "") == "degraded":
            _oov = [str(t).strip()
                    for t in ((getattr(meta, "degraded_search", None) or {}).get("ignored_terms") or [])
                    if str(t).strip()]
        elif (getattr(meta, "resolution_status", "") == "vector_fallback"
                and (getattr(meta, "vector_fallback", None) or {}).get("reason") == "unresolved_term"):
            # 2026-09-12：向量兜底只是兜住了「无结果」的沉默，词表缺口本身照样是生长数据。
            _oov = [str(t).strip()
                    for t in ((getattr(meta, "vector_fallback", None) or {}).get("ignored_terms") or [])
                    if str(t).strip()]
        if _oov:
                from ..corpus.corpus_curation import _append_jsonl, _net_ledger_path, _now_iso
                _append_jsonl(_net_ledger_path(PROJECT_ROOT).parent / "oov_terms.jsonl", {
                    "ts": _now_iso(), "query": str(query or "")[:120], "terms": _oov[:8],
                })
    except Exception as exc:
        # 纪律不变：日志自身失败绝不掀翻检索。但全静默 = 词表生长机制悄悄停工无从发现
        #（2026-08-15 触发点——留一行 stderr（含异常类型），失败与「没有 OOV」可区分。
        print(f"OOV 词表日志写入失败（{type(exc).__name__}: {exc}），本次跳过，不影响检索。", file=sys.stderr)

    # N8 标识符精确反查：仅当 query 本身是一个标识符（DOI / E-MTAB / UUID / GEO / SRA）时非 None。
    # 惰性——只有识别到「本目录应含」的标识符才真正装载全库；GEO/SRA 直接 fail-closed、不装载。
    from ..content import identifiers
    identifier_lookup = identifiers.lookup(query, lambda: load_full_corpus(DATA_DIR, PROJECT_ROOT))

    # 引导式放宽：把后端预算好的每个放宽项的预览候选也转成前端卡片行，供 0 结果时一键切入。
    # suppress＝整组替换语义（2026-09-14 收编：与环内 ask.relax 卡同口径）——请求级已忽略
    # 与本项放松量在此合并去重，前端点选后原样下发即可，零合并逻辑。
    relaxation_options = [
        {
            "key": opt.get("key", ""),
            "label": opt.get("label", ""),
            "kind": opt.get("kind", "drop"),   # drop=去掉一个条件 / only=只按一个条件搜（前端分组展示）
            "count": opt.get("count", 0),
            "suppress": list(dict.fromkeys(
                suppressed_constraints + [str(s) for s in (opt.get("suppress") or [])])),
            "results": _rows_from_retrieved(opt.get("retrieved_data", [])),
        }
        for opt in getattr(meta, "relaxation_options", [])
    ]
    # 未收录词自动降级标注（2026-09-10 起）：后端已忽略未收录词继续检索（resolution_status=
    # "degraded"），此处把降级来路（忽略了哪些词、实际生效条件）透传给前端警示条。
    _deg = getattr(meta, "degraded_search", None)
    degraded_search = None
    if _deg:
        degraded_search = {
            "ignored_terms": list(_deg.get("ignored_terms", [])),
            "query": _deg.get("query", ""),
            "count": _deg.get("count", 0),
            "results": _rows_from_retrieved(_deg.get("results", [])),
            "active_filters": _deg.get("active_filters", []),
        }

    return _json_utf8(
        {
        "ok": True,
        "markdown": meta.answer,
        "pipeline": meta.pipeline,
        "llm_attempted": meta.llm_attempted,
        "llm_succeeded": meta.llm_succeeded,
        "llm_response_used": meta.llm_response_used,
        "provider": meta.llm_provider or provider,
        "llm_mode": meta.llm_mode,
        "prompt_name": meta.prompt_name,
        "fallback": meta.fallback,
        "fallback_reason": meta.fallback_reason,
        "results": results,
        "relaxation_options": relaxation_options,
        # 未收录词降级选项（仅 unresolved_term 弃权且忽略后仍剩得下条件时非 null）：
        # {ignored_terms, query, count, results, active_filters}。前端渲染成一个可点的芯片，
        # 点了才切进去；条数 + 生效条件一并展示，让用户自己判断这次忽略值不值。
        "degraded_search": degraded_search,
        # 向量语义兜底标注（2026-09-12 起）：解析弃权且降级救不回时，结果来自全库向量语义召回、
        # 未经条件核验（resolution_status="vector_fallback"）；前端据此渲染警示条。null = 未走兜底。
        "vector_fallback": (lambda _vf: {
            "reason": _vf.get("reason", ""),
            "note": _vf.get("note", ""),
            "ignored_terms": list(_vf.get("ignored_terms") or []),
        } if _vf else None)(getattr(meta, "vector_fallback", None)),
        # 解析结果状态 + 澄清载荷：clarification_required（如"不需要fastq"）须前端单独空态、两个改写选项，
        # 不与"没有匹配"混同。results/no_match/abstained/clarification_required。
        "resolution_status": getattr(meta, "resolution_status", "results"),
        "clarification": getattr(meta, "clarification", None),
        # 分面细化：命中总数（未截断）+ 可细化维度分组，供前端渲染结果上方的分面面板。
        "result_total": getattr(meta, "result_total", len(results)),
        "facets": getattr(meta, "facets", []),
        "applied_facets": facet_filters,
        # 诚实降级：缺元数据无法核验的覆盖缺口 [{dim,label,count,by_source}]（本可能相关却被静默判负），
        # 供前端提示「另有 N 条因缺 <维度> 未能核验」+ 一键「也纳入」；applied_lenient=已宽容维度（回显）。
        "coverage_caveats": getattr(meta, "coverage_caveats", []),
        # 执行类说法（打包/下载脚本/导出引文…）：只指路、不代劳。产包仍走原来的预览→确认流程。
        "action_markers": getattr(meta, "action_markers", []),
        # N1 静默丢词诚实层：无对应筛选维度、被静默丢弃的实义描述词（性别/年龄/受试者/功能类）——回显给用户。
        "unused_query_terms": getattr(meta, "unused_query_terms", []),
        # 「A 或 B」的实际处理方式（exact / superset / narrower + note_zh）。空 dict = 这句话里没有「或」。
        # 2026-07-25 起「或」不再整句弃权；引擎只能表达同维度的「或」，落到哪一档必须如实说。
        "or_handling": getattr(meta, "or_handling", {}) or {},
        # N8 标识符精确反查：query 是标识符时 {is_identifier,kind,value,indexed,match,external_url,message}；否则 None。
        "identifier_lookup": identifier_lookup,
        "applied_lenient": lenient_dims,
        # 「已命中」里被用户删掉的原始命中维度（回显；前端据此持续抑制、后端已按此放宽）。
        "applied_suppressed": suppressed_constraints,
        # 本次查询语句已命中的硬约束（放宽后的真源，供「已命中」区渲染带标记的可删 chip；已抑制的维度自然不在其中）。
        "query_constraints": getattr(meta, "active_filters", []),
        # 检索策略决策（additive；仅 strategy=auto 非 None）：{mode,tier,recall_backend,rerank_backend,reason,signals}。
        # 前端当前未读（回显/调试用）；供观测「这条查询自动选了什么后端、为什么」。
        "strategy": getattr(meta, "strategy", None),
        # Web / MCP 共用的请求解释与实际执行步骤。前端据此展示“本次检索用了什么”，不再猜后端行为。
        "interpretation": getattr(meta, "interpretation", {}),
        "search_trace": getattr(meta, "search_trace", {}),
        # （additive）：本次检索配置指纹（语料快照/来源/排序/模型/版本），前端随遥测回传。
        # 组装失败（如语料装载异常）降级为 None，绝不掀翻检索。
        **_policy_response_fields(_policy_id_or_none(
            sources=payload.sources, strategy=strategy,
            rerank=rerank_backend, recall=recall_backend,
            model=request_llm_config.model,
        )),
        "experiment": _experiment_response(payload),
        "warnings": warnings,
        }
    )


@app.post("/api/feasibility")
def api_feasibility(payload: RecommendRequest, request: Request) -> JSONResponse:
    """N12 可行性概览：一个研究问题 → 有多少可复用数据、够不够、缺口在哪。

    聚合**通过硬过滤的全部候选**（top_k 放大到 5000 抓全命中集，非 top-k 排序结果），给确定性概览：
    候选数 / 总细胞量**下限** / 物种·平台·年份·来源分布 / 可下载率 / 缺口。不调用 LLM、不写盘。
    与 MCP `assess_feasibility` 共用 `feasibility.build_report` 单一真源；检索器/评测从不 import feasibility。
    """
    _require_same_origin(request)
    query = payload.query.strip()
    _validate_or_400(validate_query, query)
    from ..retrieval import feasibility
    _df = _require_iso_date(payload.date_from, name="date_from")
    _dt = _require_iso_date(payload.date_to, name="date_to")
    # 补漏（漂移修复）：倒挂窗口与来源校验——此前只有 /api/recommend 与 MCP 的
    # assess_feasibility 有这两道闸；Web feasibility 缺席，拼错来源静默归零候选，
    # 调用方据此误判「这方向无可复用数据」（正是 MCP 注释宣称要消灭的静默判负）。
    _validate_or_400(validate_date_window, _df, _dt)
    if payload.sources:
        _validate_or_400(validate_sources, payload.sources,
                         known=known_source_values(DATA_DIR, PROJECT_ROOT))
    _TOP = 5000
    meta = DatasetRecommendationWorkflow().run_with_meta(
        RecommendParams(
            query=query,
            top_k=_TOP,
            use_llm=False,
            date_from=_df,
            date_to=_dt,
            sources=payload.sources,
            auto_parse_sources=bool(payload.auto_parse_sources),
            facet_filters=_sanitize_facet_filters(payload.facet_filters),
            suppressed_constraints=_sanitize_suppressed(payload.suppressed_constraints),
            lenient_dims=_sanitize_lenient_dims(payload.lenient_dims),
        )
    )
    # 直接喂 workflow 原生序列化（含 platform_family/sample_size/source…）——与 MCP 同源，
    # build_report 按此形状聚合；不经 _rows_from_retrieved 卡片投影，避免两侧形状分叉。
    survivors = meta.retrieved_data
    result_total = getattr(meta, "result_total", len(survivors))
    truncated = result_total > len(survivors)
    report = feasibility.build_report(survivors, result_total, truncated)
    return _json_utf8({
        "ok": True,
        "report": report,
        "resolution_status": getattr(meta, "resolution_status", "results"),
    })


def _task_pack_retrieval(params: dict) -> dict:
    """按给定的检索参数跑一次检索，把任务包需要的一切一次性算齐。

    预览与产包**走同一个函数**：产包时用回传的同一套参数重跑，再比对指纹。
    两处各写一遍检索调用，就是给「包里叙述的检索与实际跑的检索不是同一次」留口子。
    """
    from ..content import item_view, task_pack
    from ..corpus.corpus import load_normalized_corpus

    sources = params.get("sources") or None
    # 补漏（漂移修复）：倒挂窗口在检索层单点拦截（预览与产包共用本函数）——MCP
    # build_task_pack 有 `_validate_date_window`，Web 侧此前只查格式不查倒挂，
    # 同一非法窗口 Web 静默 0 命中、MCP 拒收。
    _validate_or_400(validate_date_window,
                     params.get("date_from") or "", params.get("date_to") or "")
    meta = DatasetRecommendationWorkflow().run_with_meta(
        RecommendParams(
            query=params["query_effective"] or params["query"],
            top_k=5000,
            use_llm=False,
            date_from=params.get("date_from") or "",
            date_to=params.get("date_to") or "",
            sources=sources,
            auto_parse_sources=bool(params.get("auto_parse_sources")),
            facet_filters=_sanitize_facet_filters(params.get("facet_filters")),
            suppressed_constraints=_sanitize_suppressed(params.get("suppressed_constraints")),
            lenient_dims=_sanitize_lenient_dims(params.get("lenient_dims")),
        )
    )
    records = load_normalized_corpus(DATA_DIR, PROJECT_ROOT, sources)
    by_uid = {}
    for record in records:
        raw = record.raw if isinstance(record.raw, dict) else {}
        uid = str(raw.get("dataset_uid") or "")
        if uid and uid not in by_uid:
            by_uid[uid] = record
    ordered_uids = [str(row.get("dataset_uid") or "") for row in meta.retrieved_data]
    ordered_uids = [uid for uid in ordered_uids if uid in by_uid]
    limit = task_pack.sanitize_limit(params.get("limit"))
    candidate_uids = ordered_uids[:limit]
    items = [item_view.build_item(by_uid[uid], include_introduction=True) for uid in candidate_uids]
    honesty = {
        "active_filters": meta.active_filters,
        "coverage_caveats": meta.coverage_caveats,
        "unused_query_terms": meta.unused_query_terms,
        "or_handling": meta.or_handling,
        "search_trace_summary": (meta.search_trace or {}).get("summary", ""),
        "result_total": meta.result_total,
    }
    return {"meta": meta, "records": records, "items": items,
            "candidate_uids": candidate_uids, "by_uid": by_uid,
            "result_uids": set(ordered_uids), "honesty": honesty}


def _task_pack_params(payload: "TaskPackPreviewRequest") -> dict:
    """把检索口径固化成一个可原样回传的块。auto_parse_sources 必须带上——
    少这一个字段，产包时重跑的检索就可能与预览时不是同一次。"""
    return {
        "query": (payload.query or "").strip(),
        "query_effective": (payload.query_effective or "").strip(),
        "sources": list(payload.sources) if payload.sources else None,
        "auto_parse_sources": bool(payload.auto_parse_sources),
        "facet_filters": _sanitize_facet_filters(payload.facet_filters),
        "suppressed_constraints": _sanitize_suppressed(payload.suppressed_constraints),
        "lenient_dims": _sanitize_lenient_dims(payload.lenient_dims),
        "date_from": (payload.date_from or "").strip(),
        "date_to": (payload.date_to or "").strip(),
        "limit": payload.limit,
        "scope": payload.scope,
    }


@app.post("/api/task-pack/preview")
def api_task_pack_preview(payload: TaskPackPreviewRequest, request: Request) -> JSONResponse:
    """一句话任务包 · 第一步：先看清单。

    **刻意不是一句话直接产包。** 用户没勾过任何东西就打包，等于替他决定了「哪 10 条算数」；
    先把「会包含什么、装不了什么、缺什么」摊开，他确认过的材料才敢拿去投稿。

    只读、离线、不调用 LLM、不写盘。候选池由服务端在**全部命中**里按当前排序截取，
    与页面上恰好显示了几张卡无关。
    """
    _require_same_origin(request)
    from ..content import task_pack
    query = (payload.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    _validate_pack_sources(payload.sources)
    # 与 /api/recommend、/api/feasibility 同口径：给了就校验，非法即 400。
    # 缺这道闸时垃圾日期会被拿去字面比较，再谎称「没有命中」。
    _require_iso_date(payload.date_from, name="date_from")
    _require_iso_date(payload.date_to, name="date_to")
    try:
        params = _task_pack_params(payload)
        task_pack.sanitize_limit(params["limit"])
        scope = task_pack.sanitize_scope(payload.scope)
        run = _task_pack_retrieval(params)
        if not run["items"]:
            return _json_utf8({
                "ok": True, "plan": None,
                "resolution_status": getattr(run["meta"], "resolution_status", "no_match"),
                "message_zh": "这次检索没有命中任何数据集，没有可以打包的内容。",
            })
        pack = task_pack.build_task_pack(
            query=query, items=run["items"], records=run["records"],
            scope=scope, retrieval_params=params, honesty=run["honesty"],
            membership={uid: "in_result_set" for uid in run["candidate_uids"]},
        )
    except task_pack.TaskPackError as exc:
        raise HTTPException(status_code=400, detail=str(exc),
                            headers={"X-Error-Code": exc.code}) from None
    keep = [uid for uid in (payload.keep_selected or []) if uid in run["candidate_uids"]]
    dropped = [uid for uid in (payload.keep_selected or []) if uid not in run["candidate_uids"]]
    return _json_utf8({
        "ok": True,
        "resolution_status": getattr(run["meta"], "resolution_status", "results"),
        "result_total": run["honesty"]["result_total"],
        "plan": _preview_projection(pack, run),
        "keep_selected": keep,
        "dropped_selected": dropped,
        "message_zh": ("你之前勾选的 %d 条已不在新的候选里，已自动取消勾选。" % len(dropped)) if dropped else "",
    })


def _preview_projection(pack: dict, run: dict) -> dict:
    """预览面板要的东西。**坏消息排在最前面**——装不了什么，先说。"""
    from ..corpus import download_script
    from ..content import task_pack as task_pack_module
    plan = pack["plan"]
    return {
        "plan_token": pack["plan_token"],
        "scope": pack["scope"],
        "retrieval": pack["retrieval"],
        "retrieval_params": pack["retrieval_params"],
        "cannot_include": plan["manual"],
        "primary_only_zh": download_script.primary_only_sentence(plan),
        # 面板要在用户勾勾选选的过程中一直显示「只取主文件/全部文件」这条口径，而带数字的整句
        # 一勾选就过期。所以额外给一份**不含任何数字**的政策句：它对任何勾选组合都成立，
        # 数字由面板按当前勾选自己算。口径按 scope 分岔（K8：scope=all 不许出现「只取主文件」）。
        "primary_only_policy_zh": (download_script.PRIMARY_ONLY_POLICY_ZH
                                   if pack["scope"] == "primary"
                                   else download_script.ALL_FILES_POLICY_ZH),
        "tiers": plan["tiers"],
        "estimate": plan["estimate"],
        "ledger": plan["ledger"],
        "inspection": plan["inspection"],
        "items": [{
            "dataset_uid": it["dataset_uid"], "dataset_name": it["dataset_name"],
            "source": it["source"], "tier": it["tier"], "tier_evidence": it["tier_evidence"],
            "n_files_selected": it["n_files_selected"], "n_files_total": it["n_files_total"],
            "bytes_selected": it["bytes_selected"], "rows_planned": it["rows_planned"],
            "page_url": it["page_url"],
        } for it in plan["items"]],
        "todo": pack["todo"],
        "candidate_uids": run["candidate_uids"],
        "pack_files": list(task_pack_module.PACK_FILES),
    }


def _validate_pack_sources(sources) -> None:
    """拼错的来源名必须显式报错，绝不静默判空（同型 bug 已经在可行性概览上出过一次）。
    （批起为薄委托：形状闸+空白闸+未知闸的真源在 app/request_validation.validate_sources，
    与 /api/recommend、MCP 同一份。）"""
    if not sources:
        return
    _validate_or_400(validate_sources, sources, known=known_source_values(DATA_DIR, PROJECT_ROOT))


@app.post("/api/task-pack/build")
def api_task_pack_build(payload: TaskPackBuildRequest, request: Request) -> Response:
    """一句话任务包 · 第二步：产包。

    **用回传的同一套检索参数重跑一遍，再算一次指纹。** 对不上就明确说哪里变了、
    一个字节都不产出——绝不发一份与用户看过的预览不符的材料。

    锁的是**候选池**不是勾选：少勾几条完全合法，不会触发不一致。
    """
    _require_same_origin(request)
    from ..content import task_pack
    # retrieval_date 会原样拼进 Content-Disposition 文件名：中文曾在这里炸成未捕获 500
    # （latin-1 编不了），英文引号曾注入第二个响应头参数（T3/T4）。先按 ISO 校验
    # 非法即 400，合法值只剩数字与连字符，拼 header 必然安全。
    retrieval_date = _require_iso_date(payload.retrieval_date, name="retrieval_date")
    try:
        # 指纹三件套缺一即拒：空指纹意味着调用方没走过 preview（或把字段弄丢了）。
        # 旧实现「if 给了才比」把「根本没核对」当成「核对通过」——空指纹 + 篡改 limit 照样出包
        # （T2）。先 preview 拿真指纹，是产包的唯一入口。
        missing_fp = [name for name, value in (("plan_token", payload.plan_token),
                                               ("snapshot_id", payload.snapshot_id),
                                               ("content_digest", payload.content_digest))
                      if not (value or "").strip()]
        if missing_fp:
            raise task_pack.TaskPackError(
                "bad_param",
                "缺少指纹字段：" + "、".join(missing_fp) + "；请先预览（preview）一次，再原样回传。",
            )
        selected = task_pack.sanitize_uids_for_pack(payload.selected_uids)
        scope = task_pack.sanitize_scope(payload.scope)
        params = dict(payload.retrieval_params or {})
        params.setdefault("query_effective", "")
        if not str(params.get("query") or "").strip():
            raise task_pack.TaskPackError("bad_param", "retrieval_params 里缺少 query。")
        _validate_pack_sources(params.get("sources"))
        run = _task_pack_retrieval(params)
        rebuilt = task_pack.build_task_pack(
            query=params["query"], items=run["items"], records=run["records"],
            scope=scope, retrieval_params=params, honesty=run["honesty"],
            today=retrieval_date or None,
            membership={uid: "in_result_set" for uid in run["candidate_uids"]},
        )
    except task_pack.TaskPackError as exc:
        raise HTTPException(status_code=400, detail=str(exc),
                            headers={"X-Error-Code": exc.code}) from None

    mismatch = _pack_mismatch(payload, rebuilt, run)
    if mismatch:
        return _json_utf8({"ok": False, **mismatch}, status_code=409)

    missing = [uid for uid in selected if uid not in run["candidate_uids"]]
    if missing:
        return _json_utf8({
            "ok": False, "code": "plan_mismatch",
            "message_zh": "这份清单和刚才预览的不是同一批了。请重新预览一次，再生成。",
            "hint_zh": "本次没有生成任何文件。",
        }, status_code=409)

    chosen_items = [it for it in run["items"] if it["dataset_uid"] in set(selected)]
    try:
        pack = task_pack.build_task_pack(
            query=params["query"], items=chosen_items, records=run["records"],
            scope=scope, retrieval_params=params, honesty=run["honesty"],
            today=retrieval_date or None,
            membership={uid: "in_result_set" for uid in selected},
        )
        files = task_pack.render_files(pack)
        if (payload.format or "zip").lower() == "json":
            # json 通道此前完全绕过 MAX_ZIP_BYTES——zip 要过
            # 8MB 闸、同一份内容的文本版却 unlimited 直返。同一常量同源补闸（文本未压缩，
            # 按 UTF-8 字节合计；超限 413 如实说明，不静默截断文件内容）。
            total_bytes = sum(len(f["text"].encode("utf-8")) for f in files)
            if total_bytes > task_pack.MAX_ZIP_BYTES:
                return _json_utf8({
                    "ok": False, "code": "too_large",
                    "message_zh": (f"文本版共约 {total_bytes // 1024} KB，超过上限 "
                                   f"{task_pack.MAX_ZIP_BYTES // 1024 // 1024} MB。请少选几个数据集，"
                                   "或改用 format=zip（压缩后体积小得多）。")}, status_code=413)
            return _json_utf8({"ok": True, "plan_token": rebuilt["plan_token"],
                               "files": [{"name": f["path"], "text": f["text"]} for f in files]})
        blob = task_pack.files_to_zip_bytes(files)
    except task_pack.TaskPackError as exc:
        status = 413 if exc.code == "too_large" else 400
        return _json_utf8({"ok": False, "code": exc.code, "message_zh": str(exc)}, status_code=status)
    name = f"biodata-task-pack-{pack['retrieval']['date']}-{pack['plan_token'][:8]}.zip"
    return Response(content=blob, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _pack_mismatch(payload: "TaskPackBuildRequest", rebuilt: dict, run: dict) -> "dict | None":
    """哪里变了，就说哪里变了。四种情况的处理方式对用户完全不同，不能糊成一句「变了」。

    空指纹在端点入口已被拒（bad_param），走到这里的三件套**必然非空**——直接比对，
    不再有「if 给了才比」的短路（那曾把「没核对」当成「核对通过」， T2）。"""
    retrieval = rebuilt["retrieval"]
    if payload.snapshot_id != retrieval["snapshot_id"]:
        return {"code": "snapshot_changed",
                "message_zh": "数据集清单在你预览之后变过（例如有人上传了新数据）。请重新预览一次。",
                "hint_zh": "本次没有生成任何文件。"}
    if payload.content_digest != retrieval["content_digest"]:
        return {"code": "catalog_content_changed",
                "message_zh": "数据集编号没变，但其中一些记录的内容在你预览之后被更新过"
                              "（例如来源补录了组织字段）。请重新预览一次。",
                "hint_zh": "本次没有生成任何文件。"}
    if payload.plan_token != rebuilt["plan_token"]:
        return {"code": "configuration_mismatch",
                "message_zh": "生成时用的检索条件和预览时不一样。请重新预览一次。",
                "hint_zh": "本次没有生成任何文件。"}
    return None


@app.post("/api/board/plan")
def api_board_plan(payload: BoardPlanRequest, request: Request) -> JSONResponse:
    """条件板：把「再说一句话改条件」规划成一次具体的改动。**只规划，不检索。**

    无状态、纯计算：不装载数据集目录、不落盘、不写日志、不联网、不用大模型。
    返回里的 `next_request` 才是拿去调 `/api/recommend` 的东西；
    `status` 不是 auto_apply / needs_confirm 时，`next_request` 恒为 None —— 一个字节都没改。

    与 MCP `plan_query_edit` 共用 `board.plan_edit` 单一真源；
    检索器 / 编排 / 冻结评测从不 import board → 767 基准结构性不受影响。
    """
    _require_same_origin(request)
    from . import board
    try:
        plan = board.plan_edit(
            payload.query or "",
            payload.utterance or "",
            forced_op=payload.forced_op or "",
            dim=payload.dim or "",
            candidate_override=payload.candidate_override or "",
            current_filters=payload.current_filters,
            resolution=payload.resolution,
            suppressed_constraints=payload.suppressed_constraints,
            lenient_dims=payload.lenient_dims,
            facet_filters=payload.facet_filters,
            coverage_dims=payload.coverage_dims,
            date_from=payload.date_from or "",
            date_to=payload.date_to or "",
            # 部署方通过 KEYWORD_MAPPING_PATH 注入的说法是系统**真的认识**的词；
            # 不透传的话，板会对这些词说「我不认识」——正好踩在「我不认识 ≠ 不支持」这条线上。
            keyword_mapping=get_settings().keyword_mapping,
        )
    except board.BoardError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return _json_utf8({"ok": True, **plan})


def _run_action_plan(
    utterance: str,
    *,
    has_results: bool,
    result_total: int,
    provider: str,
    use_llm: bool,
    mock_llm: bool,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
) -> dict:
    """按请求级覆盖跑 `action_plan.plan_action`（ENV_LOCK + `_temporary_env` 链）。

    `/api/action/plan` 与 `/api/utterance` 的 action/歧义分支共用这一条——LLM 配置链
    （请求级 key 隔离、endpoint 校验、server 配置对照）只有一份，不手抄。
    """
    from ..agent import action_plan as _ap

    provider, requested_base_url, mock_llm, use_llm = _resolve_llm_request(
        provider, use_llm=use_llm, mock_llm=mock_llm, base_url=base_url)

    # 锁内最小化：锁内只物化请求级 config，`plan_action`（可能调 LLM）在锁外执行——
    # 显式传 `config=cfg`，plan_action 内部 `config or load_llm_config()` 不再读 env。
    cfg = _materialize_request_llm_config(
        provider, use_llm=use_llm, mock_llm=mock_llm,
        api_key=api_key, base_url=requested_base_url, model=model)
    try:
        return _ap.plan_action(
            utterance,
            has_results=bool(has_results),
            result_total=int(result_total or 0),
            config=cfg,
        )
    except _ap.ActionPlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc),
                            headers={"X-Error-Code": exc.code}) from None


@app.post("/api/action/plan")
def api_action_plan(payload: ActionPlanRequest, request: Request) -> JSONResponse:
    """一句话 → **该做哪一个动作**（封闭动词表里的一个）。**只出 plan，不执行任何动作。**

    这条分层是整个「说了就做」方案里最值钱的判断：本端点不检索、不落盘、不产任何交付物。
    真正的执行发生在调用方（网页 `act.js` 派发既有能力、MCP 调用方自己决定）。
    于是 `tests/test_action_markers.py` 那条「检索端点永不夹带产物」的诚实性分层原样成立，
    自动执行落地**不需要重基线任何既有的门**。

    `has_results` / `result_total` 由调用方自述、服务端无从核实 —— 所以它们只用来置
    `blocked_reason`，返回体里**不生成任何带这两个数字的成句断言**。

    与 MCP `plan_action` 共用 `action_plan.plan_action` 单一真源；
    检索器 / 编排 / 冻结评测从不 import action_plan → 冻结 767 基准结构性不受影响。
    """
    _require_same_origin(request)
    # T3 配额闸（cfg=None → 闸内按请求覆盖链复算；/api/utterance 的 action 分支不走本端点，
    # 已在 utterance 内计过一次，无重复计数）。
    _gate_llm_quota(
        request, cfg=None, provider=payload.provider,
        use_llm=bool(payload.use_llm or payload.mock_llm), mock_llm=bool(payload.mock_llm),
        api_key=payload.api_key,
        requested_base_url="" if _normalize_provider(payload.provider) in ("mock", "trial")
        else _validate_endpoint_url(payload.base_url))
    plan = _run_action_plan(
        payload.utterance,
        has_results=bool(payload.has_results),
        result_total=int(payload.result_total or 0),
        provider=payload.provider,
        use_llm=bool(payload.use_llm),
        mock_llm=bool(payload.mock_llm),
        api_key=payload.api_key,
        base_url=payload.base_url,
        model=payload.model,
    )
    return _json_utf8({"ok": True, "plan": plan})


def _utterance_principal(request: Request) -> str:
    """成功经验库分区主体：会话账户 id；无会话/解析失败 → "anonymous"
    （宁可匿名不认错人——把别人的分区挂给本请求才是泄漏）。"""
    try:
        user = accounts.resolve_session(request.cookies.get(SESSION_COOKIE), sessions_path=_sessions_store())
    except Exception:
        user = None
    return str(getattr(user, "id", "") or "") or "anonymous"


# ---------------------------------------------------------------- /api/utterance 幂等占用
#
# 背景：前端断流后把同一句话以非流式**再发一次**，而 agent 会真执行写工具、worker 线程在客户端
# 断开后仍会收尾——同一句话可能真实执行两遍（重复入库、账本两行）。修复 = 客户端 req_id +
# 服务端占用注册表：同号在途 → 等 owner 收尾拿缓存体；同号已完成 → 直接回缓存体。
# 进程内即可（单实例部署）；无 req_id 的请求完全不进这里，行为与此前逐位一致。
_UTT_IDEM_TTL_SECONDS = 3600.0    # done 条目寿命：完成后缓存体保留一小时
_UTT_IDEM_RUNNING_TTL_SECONDS = 24 * 3600.0   # running 泄漏兜底寿命（owner 崩掉的占坑上限；
                                              # 长链执行/锁排队内的正常请求远低于此——
                                              # 评审：running 绝不按 1h TTL 淘汰）
_UTT_IDEM_MAX_ENTRIES = 256       # 上限 FIFO 淘汰：防无限增长
_UTT_IDEM_WAIT_TIMEOUT = 180.0    # 非 owner 等待上限（秒）：超时回 503 让客户端稍后重试
_UTT_IDEM_LOCK = threading.Lock()
_UTT_IDEM: "OrderedDict[str, dict]" = OrderedDict()   # req_id → {state, body, event, ts, fp}；插入序即淘汰序


def _utterance_request_fp(text: str, payload: Any, provider: str,
                          mock_llm: bool, use_llm: bool) -> str:
    """占用条目的请求指纹：同 req_id 但**内容不同**的请求不得
    共享幂等槽（此前只看 req_id——两个不同 utterance/config 复用同号会错等/错收响应；
    撞指纹 → 409 如实说明，不当成同一次重发）。

    2026-08-10 二轮评审补全：指纹材料加入**现场字段**（has_results/result_total/query/
    current_filters/sources/base_url）——断流重发会把它们原样带上（同指纹合法复用），
    而「同号不同现场」必是另一次请求（撞指纹 409）。api_key 只以其哈希入料——
    指纹材料虽不落盘，也绝不给任何调试/日志路径留原文的机会。"""
    api_key = str(getattr(payload, "api_key", None) or "")
    material = json.dumps({
        "text": text,
        # stream 刻意**不**进指纹：流式断流后以非流式重发同一句话是设计内的主要场景
        # （非 owner 的单帧 final 兜底路径就是为它在的），两者是同一逻辑请求。
        "provider": provider, "mock_llm": bool(mock_llm), "use_llm": bool(use_llm),
        "model": str(getattr(payload, "model", "") or ""),
        "agent": bool(getattr(payload, "agent", False)),
        "has_results": bool(getattr(payload, "has_results", False)),
        "result_total": int(getattr(payload, "result_total", 0) or 0),
        "query": str(getattr(payload, "query", "") or ""),
        "current_filters": getattr(payload, "current_filters", None),
        "sources": getattr(payload, "sources", None),
        # 课题上下文进幂等指纹——同 req_id 但上下文卡不同 = 另一次
        # 请求（撞指纹 409，不得共享幂等槽）；断流重发原样带回同卡才合法复用缓存体。
        "artifact_context": str(getattr(payload, "artifact_context", "") or ""),
        # suggested_recipe 进幂等指纹——同 req_id 但建议动作不同 =
        # 另一次请求（撞指纹 409）；断流重发原样带回同 recipe 才合法复用缓存体。
        "suggested_recipe": str(getattr(payload, "suggested_recipe", "") or ""),
        "base_url": str(getattr(payload, "base_url", "") or ""),
        #检索参数进指纹——同号但检索参数不同 = 另一次请求
        # （撞指纹 409），不得共享幂等槽；stream 不进指纹的纪律不变。
        "top_k": getattr(payload, "top_k", None),
        "rerank": str(getattr(payload, "rerank", "") or ""),
        "recall": str(getattr(payload, "recall", "") or ""),
        "strategy": str(getattr(payload, "strategy", "") or ""),
        "facet_filters": getattr(payload, "facet_filters", None),
        "suppressed_constraints": getattr(payload, "suppressed_constraints", None),
        "lenient_dims": getattr(payload, "lenient_dims", None),
        "date_from": str(getattr(payload, "date_from", "") or ""),
        "date_to": str(getattr(payload, "date_to", "") or ""),
        "polish": bool(getattr(payload, "polish", True)),
        "api_key_fp": hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16],
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _sanitize_req_id(raw: str | None) -> str | None:
    """清洗 req_id：空白/缺省 → None（=无幂等）；≤64 字符原样；**超长按完整值取定长哈希**——
    旧实现直接截 64 字符，前 64 字符相同的两条请求会错共一个幂等槽（缓存串台/重复写，
    评审裁决）。"""
    text = (raw or "").strip()
    if not text:
        return None
    if len(text) <= 64:
        return text
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:48]


def _prune_utterance_idem(now: float) -> None:
    """TTL + 上限淘汰。**只在持 `_UTT_IDEM_LOCK` 时调用**。
    TTL 分态：done 1h 淘汰（缓存体到期）；running 只在 24h 泄漏兜底线上
    淘汰（owner 崩掉占坑）——正常长链执行/锁排队远低于 24h，绝不按 1h 误杀在途 owner。
    上限淘汰只动 done 条目：FIFO 淘汰 running owner =
    同号请求随后成为新 owner、同一句话的写工具真实执行两遍——那才是重复写窗口。"""
    stale = [rid for rid, e in _UTT_IDEM.items()
             if (e["state"] == "done" and now - e["ts"] > _UTT_IDEM_TTL_SECONDS)
             or (e["state"] != "done" and now - e["ts"] > _UTT_IDEM_RUNNING_TTL_SECONDS)]
    for rid in stale:
        del _UTT_IDEM[rid]
    while len(_UTT_IDEM) > _UTT_IDEM_MAX_ENTRIES:
        done_oldest = next((rid for rid, e in _UTT_IDEM.items() if e["state"] == "done"), None)
        if done_oldest is None:
            break   # 全是 running：不再淘汰（新占用在 `_utterance_idem_claim` 被拒）
        del _UTT_IDEM[done_oldest]


def _utterance_idem_claim(req_id: str, fp: str = "") -> tuple[dict, bool]:
    """占用 req_id：没见过 → 注册 running 条目并返回 (entry, True=owner)；
    已见过 → 返回 (entry, False=非 owner，调用方等/取缓存体)。
    满员且全 running → 503 拒绝新占用（宁可拒新、绝不淘汰在途 owner）。
    同号但请求指纹不同（fp 非空且不匹配）→ 409（评审：那不是重发，是撞号）。"""
    now = time.monotonic()
    with _UTT_IDEM_LOCK:
        _prune_utterance_idem(now)   # 先清旧：过期条目不得被当成在途 owner 复用
        entry = _UTT_IDEM.get(req_id)
        if entry is not None:
            if fp and entry.get("fp") and entry["fp"] != fp:
                raise HTTPException(
                    status_code=409,
                    detail="这个 req_id 已对应另一句话（内容指纹不同）。"
                           "如果确实要重发，请换一个新的 req_id。")
            return entry, False
        if len(_UTT_IDEM) >= _UTT_IDEM_MAX_ENTRIES:
            done_oldest = next((rid for rid, e in _UTT_IDEM.items() if e["state"] == "done"), None)
            if done_oldest is not None:
                del _UTT_IDEM[done_oldest]
            else:
                raise HTTPException(status_code=503, detail="在途请求过多，请稍后重试。")
        entry = {"state": "running", "body": None, "event": threading.Event(),
                 "ts": now, "fp": fp}
        _UTT_IDEM[req_id] = entry
        return entry, True


#: route_turn 契约是永不抛——各兜底分支（流式 worker / 非流式 / 幂等收尾 / 等待方回放）
#: 给客户端的统一通用文案只有这一句；完整堆栈只进服务端日志（webobs）。
_UTTERANCE_INTERNAL_ERROR_DETAIL = "处理这句话时出了内部错误，请重试。"


def _utterance_idem_store(entry: dict, body: dict) -> None:
    """owner 收尾：存最终响应体并唤醒等待者。锁只罩字典写，event.set 放锁外（等待方不持本锁）。"""
    with _UTT_IDEM_LOCK:
        entry["body"] = body
        entry["state"] = "done"
    entry["event"].set()


def _utterance_idem_wait_response(entry: dict, *, stream: bool) -> Response:
    """非 owner 分支：done → 直接回缓存体（HTTP 200 原样）；running → 等 owner 收尾，
    超时仍无体 → 503（客户端稍后重试）。**绝不持 ENV_LOCK / 不载 env override**——
    非 owner 不执行路由，只是等一个已经跑起来的结果。"""
    body = entry["body"]
    if entry["state"] != "done" or body is None:
        entry["event"].wait(timeout=_UTT_IDEM_WAIT_TIMEOUT)
        body = entry["body"]
    if body is None:
        raise HTTPException(status_code=503, detail="同一句话还在处理中，请稍等。")
    if stream:
        # 非 owner 的 stream:true：回一个只含单帧的 SSE 流——成功体走 final（与 owner 流式
        # 的 final 帧同形同义），失败体走 error（前端视为协议失败、与 owner 断流同口径）。
        if body.get("ok"):
            frames = [_sse_line("final", body)]
        else:
            frames = [_sse_line("error", {"detail": body.get("detail") or _UTTERANCE_INTERNAL_ERROR_DETAIL})]
        return StreamingResponse(iter(frames), media_type="text/event-stream")
    return _json_utf8(body)


def _utterance_response_body(result: dict[str, Any]) -> dict[str, Any]:
    """`/api/utterance` 的响应体（2026-08-03 抽出）：非流式响应与流式 final 事件
    **共用这一个真源**——SSE 契约要求 final 体与非流式逐位同形，抄两份必漂移。"""
    from ..agent import agent_exec as _agent_exec
    body = {
        "ok": True,
        "route": result["route"],
        "query": result["query"],
        "plan": result["plan"],
        "echo_zh": result["echo_zh"],
        "retrieval": result["retrieval"],
        "via": result["via"],
        # 降级气泡专档：「AI 执行」关 + 规则检出操作意图 → True，
        # 前端据此把回音渲染成带「设置 → AI 执行」指路的美观气泡（而非普通灰泡）。
        "needs_agent": bool(result.get("needs_agent")),
        # 婉拒候选 chips（2026-08-09 五机制批，additive）：仅 LLM 真判 none 时非空，
        # [{label, utterance}]；前端渲染成可点 chip，点击即把 utterance 重新入环。
        "suggestions": list(result.get("suggestions") or []),
        # 前端置灰/行动流标注用：available=扩展装好且未被 env 关停；used=本次真的走了 agent。
        "agent": {"available": _agent_exec.agent_available(), "used": result["via"] == "agent"},
        #（2026-08-16 additive）：result_payload=环内 search.rerun 采纳的
        # /api/recommend 同形载荷（前端直接换屏，不再调 recommend）；None=无采纳。
        "result_payload": result.get("result_payload"),
        # preliminary_final=true 表示初步结果即最终结果（清徽标收尾，不调 recommend）；
        # 缺省/任一条件不明恒 False（宁可重检不跳检）。
        "preliminary_final": bool(result.get("preliminary_final")),
    }
    #（并发分流 r3 / ，breaking）：tool 路线 retrieval 恒 None +
    # additive retrieval_note（"skipped_action_marker"/"discarded_action_route"）——
    # 仅当 retrieval 为 None 且 note 非空时才带键（rule_direct/identifier 等早退路线的
    # None 不带，键集与现状逐位一致）。
    if result.get("retrieval") is None and result.get("retrieval_note"):
        body["retrieval_note"] = result["retrieval_note"]
    # （2026-08-17 结果，additive）：route_turn 组了批次才透传——flag OFF 或
    # 轮内无批时两个键都不出现，响应与现状逐位一致；流式 final 与非流式共用本真源，天然同步。
    if result.get("result_batches"):
        body["result_batches"] = result["result_batches"]
        body["active_batch"] = result.get("active_batch")
    # trace 开启时 route_turn 回了 trace_turn_id
    # 才透传（报障给号用）；AGENT_TRACE OFF 时该键不出现，响应与现状逐位一致。
    if result.get("trace_turn_id"):
        body["trace_turn_id"] = result["trace_turn_id"]
    # （additive）：route=="search" 时由端点注入的检索配置指纹；非 search 路线
    # 或组装失败（None）该键不出现，响应与现状逐位一致。
    if result.get("policy_id") is not None:
        body["policy_id"] = result["policy_id"]
        body["policy_id_str"] = result.get("policy_id_str") or _policy_id_string(result["policy_id"])
    if result.get("experiment") is not None:
        body["experiment"] = result["experiment"]
    # （additive）：suggested_recipe 处理结论如实记录（非法 → 忽略说明 /
    # 合法但被机械收窄 → 收窄说明）；无建议动作或处理正常时该键不出现，响应与现状逐位一致。
    if result.get("recipe_note"):
        body["recipe_note"] = result["recipe_note"]
    # （2026-09-14 YOLO ask.relax，additive）：环内放宽询问的选择卡载荷——仅当轮内
    # 真发起过询问时才带键（拒绝档/未发起时不出现，响应与现状逐位一致）。
    if result.get("ask_relax") is not None:
        body["ask_relax"] = result["ask_relax"]
    return body


def _inject_utterance_policy_id(result: dict[str, Any], payload, search_params: dict | None, model) -> None:
    """route=="search" 时把 policy_id 塞进 result（additive；流式/非流式共用
    注入点，透传在 `_utterance_response_body`）。组装失败降级 None（键不出现，不掀翻路由）。"""
    if not isinstance(result, dict) or result.get("route") != "search":
        return
    sp = search_params or {}
    policy_id = _policy_id_or_none(
        sources=payload.sources,
        strategy=sp.get("strategy", ""),
        rerank=sp.get("rerank", ""),
        recall=sp.get("recall", ""),
        model=model,
    )
    result.update(_policy_response_fields(policy_id))
    result["experiment"] = _experiment_response(payload)


def _sse_line(event: str, data: Any) -> str:
    """一帧 SSE：`data: {json}\\n\\n`（ensure_ascii=False：中文原样发，客户端按 utf-8 解）。"""
    return "data: " + json.dumps({"event": event, "data": data}, ensure_ascii=False) + "\n\n"


def _utterance_event_stream(
    text: str,
    payload: UtteranceRequest,
    *,
    provider: str,
    requested_base_url: str,
    mock_llm: bool,
    use_llm: bool,
    idem_entry: dict | None = None,
    principal: str = "",
    search_params: dict | None = None,
    ask_answer: dict | None = None,
) -> Iterator[str]:
    """`/api/utterance` 流式分支的 SSE 生成器。

    事件序（并发分流 v3.1 重定，r3 裁定后的真实确定性序）：
    **tool_start(共识) → step(共识) → tool_start(understand) → preliminary? → … → final**
    ——preliminary 不再保证首帧：verdict-gated 后它在 understand 节点入口（join/补跑
    完成后、构造 prompt 前）发射，恒在 tool_start(understand) 之后、final 之前；action
    路线永不发射（路由内部出错则只发 error）。2026-08-16 起 on_event 的 kind
    透传不再一律打 step。step 是 agent 规划节点的 trace 条目
    （`turn.route_turn(on_event=...)` 透传给 `plan_with_agent_events`，每节点落定时
    回调）；保底路径（无 agent）没有节点可播，**只发 final**——前端维持非流式的
    百分比画像。final 的体与非流式响应逐位同形（共用 `_utterance_response_body` 真源）。

    两处刻意的结构决策：

    1. **ENV_LOCK 只保护「读配置/物化」几行，SSE 泵送全程在锁外**（锁内最小化）：流式的路由
       发生在生成器被迭代时（端点函数早已返回）。锁内先把请求级 env 覆盖物化成不可变的
       `cfg`（LLMConfig），worker 的 `route_turn` **显式接收 `config=cfg`**（turn 内部所有
       配置读取都是 `config or load_llm_config()`，config 恒非空 → 永不读 env）——于是生成
       阶段不再需要 env override 存活，也不再把整个 SSE 循环与 `worker.join()` 关在锁里。
       此前一个慢 LLM 流会持锁到 SSE 完成，阻塞 /api/health、/api/recommend 等一切需要
       读配置的端点（队头阻塞）。
    2. **route_turn 放进 worker 线程**：on_event 回调在路由**进行中**触发，生成器
       若是同步调用就只能把步骤攒到最后一次性吐——那不叫流式。规划节点是秒级 LLM
       调用，步骤经无界队列实时泵给客户端（claudecode 式节点输出）。worker 只碰队列、
       不碰锁，无死锁面。队列无界 + daemon 线程：客户端中途断开时生成器被关闭，worker
       亦能自行收尾，不会挂住。

    `idem_entry`：本流是某个 req_id 的 owner 时由端点传入占用条目，
    **worker 的 finally 里**把最终响应体存进条目并唤醒等待者——客户端断开时生成器会被
    关闭，但 worker 照常收尾，这正是断流重发能拿到结果的幂等数据源。
    """
    from ..agent import turn

    events: "queue.Queue" = queue.Queue()
    done: Any = object()

    def on_event(kind: str, entry: dict) -> None:
        # kind 透传不再一律打 "step"——preliminary / tool_start
        # 与 step 同路进队列，_sse_line 对事件名无约束；旧前端对未知事件名天然忽略。
        events.put((kind, entry))

    def run() -> None:
        body: dict | None = None
        try:
            result = turn.route_turn(
                text,
                has_results=bool(payload.has_results),
                result_total=int(payload.result_total or 0),
                current_query=payload.query or "",
                current_filters=payload.current_filters,
                sources=payload.sources,
                config=cfg,
                keyword_mapping=get_settings().keyword_mapping,
                use_agent=bool(payload.agent),
                on_event=on_event,
                principal=principal,
                search_params=search_params,
                # 课题上下文独立透传，只进 agent prompt。
                artifact_context=str(payload.artifact_context or ""),
                # 建议动作 hint（allowlist 校验在 action_plan 单一真源）。
                suggested_recipe=str(payload.suggested_recipe or ""),
                # 放松卡作答续跑（2026-09-14）：端点已净化，原样透传。
                ask_answer=ask_answer,
            )
            _inject_utterance_policy_id(result, payload, search_params, cfg.model)
            body = _utterance_response_body(result)
            events.put(("final", body))
        except Exception:  # route_turn 契约是永不抛——这里是结构性防御，错误也要成一帧
            # webobs：此前 exc 接住即弃、零日志零堆栈，触发即无法定位。
            # 完整堆栈进日志（exception 不含请求体/参数值），客户端仍只见通用文案。
            logger.exception("/api/utterance 流式 worker 兜底触发：route_turn 抛出异常")
            body = {"ok": False, "detail": _UTTERANCE_INTERNAL_ERROR_DETAIL}
            events.put(("error", {"detail": _UTTERANCE_INTERNAL_ERROR_DETAIL}))
        finally:
            if idem_entry is not None:
                # body=None 只在 BaseException 穿透 except 时发生——同样要存错误体唤醒等待者。
                _utterance_idem_store(idem_entry, body or {"ok": False, "detail": _UTTERANCE_INTERNAL_ERROR_DETAIL})
            events.put(done)

    # 锁内最小化：锁内只做「读配置/物化」——把请求级 env 覆盖冻结成不可变 `cfg`；
    # worker 与 SSE 泵送全程在锁外（`cfg` 显式传给 route_turn，worker 不再读 env）。
    cfg = _materialize_request_llm_config(
        provider, use_llm=use_llm, mock_llm=mock_llm,
        api_key=payload.api_key, base_url=requested_base_url, model=payload.model)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    while True:
        item = events.get()
        if item is done:
            break
        event, data = item
        yield _sse_line(event, data)
    worker.join()


@app.post("/api/utterance")
def api_utterance(payload: UtteranceRequest, request: Request) -> Response:
    """统一对话窗口的**后端路由**（turn pipeline）。**路由是真，执行也是真——
    AI 执行开启时，EXEC 动词经 agent 图（langgraph）在本端点内真实执行**
    （search_online/sync_updates 会写外部库 upload_*，记账 + 回收站可撤 +
    2026-08-09 起写入汇强制流水账）；「只出计划不执行」的是 `/api/action/plan`。

    管线（2026-08-03 用户定稿，唯一设计，无并行短路）——`turn.route_turn` 单一真源：
    **规则匹配（一切指令都过；零命中/弃权 ≠ 无效）→ LLM 分流 →
    检索指令（search，带 effective_query）/ 工具调用（tool，EXEC plan）/ none（如实回音）**。
    LLM 缺席/失败时规则兜底（动作词 → tool 规则档；search_shaped → search；其余 → none），
    绝不把零命中/带执行标记的句子 fail-open 成检索。

    LLM 配置链与 `/api/recommend` 同径：ENV_LOCK + `_temporary_env` 请求级覆盖
    （请求级 key 隔离、endpoint 校验、server 配置对照），锁内只载配置，
    锁外才跑规则匹配与 LLM 分流（不把网络 I/O 关在 ENV_LOCK 里）。
    本端点不产交付物、不落盘；route 的落地由调用方决定
    （search → `/api/recommend`，tool → 前端 act 结构派发）。

    `stream:true`改发 `text/event-stream`：preliminary? →
    tool_start* → step* → final（2026-08-16）
    final 体与本响应逐位同形；细节见 `_utterance_event_stream`。

    `req_id`（2026-08-08 修复）：同号在途 → 等 owner 收尾回缓存体（不再二次
    执行路由/写工具）；同号已完成 → 直接回缓存体。占用/等待细节见 `_utterance_idem_*`。
    """
    _require_same_origin(request)
    from ..agent import action_plan as _ap
    from ..agent import turn

    principal = _utterance_principal(request)   # 成功经验库分区主体（无会话 → anonymous）

    try:
        text = _ap.normalize_utterance(payload.utterance)
    except _ap.ActionPlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc),
                            headers={"X-Error-Code": exc.code}) from None

    provider, requested_base_url, mock_llm, use_llm = _resolve_llm_request(
        payload.provider, use_llm=payload.use_llm, mock_llm=payload.mock_llm,
        base_url=payload.base_url)

    #当前检索参数收敛——与 /api/recommend **同一口径**（非法日期
    # 400、倒挂 400、垃圾值收敛安全默认，不新造规则）；只用于 pre-loop 确定性检索与
    # preliminary_final 判定。校验在幂等占用之前：400 的请求不得占幂等槽。
    sp_rerank = "llm" if str(payload.rerank or "").strip().lower() == "llm" else "off"
    _rec = str(payload.recall or "").strip().lower()
    sp_recall = _rec if _rec in ("dense", "cross_encoder", "vector") else "off"
    sp_date_from = _require_iso_date(payload.date_from, name="date_from")
    sp_date_to = _require_iso_date(payload.date_to, name="date_to")
    _validate_or_400(validate_date_window, sp_date_from, sp_date_to)
    search_params = {
        "top_k": payload.top_k,
        "rerank": sp_rerank,
        "recall": sp_recall,
        "strategy": "auto" if str(payload.strategy or "").strip().lower() == "auto" else "fixed",
        "facet_filters": _sanitize_facet_filters(payload.facet_filters),
        "suppressed_constraints": _sanitize_suppressed(payload.suppressed_constraints),
        "lenient_dims": _sanitize_lenient_dims(payload.lenient_dims),
        "date_from": sp_date_from,
        "date_to": sp_date_to,
        "polish": bool(payload.polish),
    }
    # 放松卡作答（2026-09-14 ask 暂停/重启批）：校验在幂等占用之前——400 不占槽。
    ask_answer = _sanitize_ask_answer(payload.ask_answer)

    # 幂等占用：非 owner 在这里直接等/取缓存体返回——**不碰 ENV_LOCK、不载 env
    # override、不进路由**（等的时候持 ENV_LOCK 会把 owner 的流式路由也卡住）。
    req_id = _sanitize_req_id(payload.req_id)
    idem_entry: dict | None = None
    if req_id is not None:
        idem_entry, is_owner = _utterance_idem_claim(
            req_id, _utterance_request_fp(text, payload, provider, mock_llm, use_llm))
        if not is_owner:
            return _utterance_idem_wait_response(idem_entry, stream=bool(payload.stream))

    # T3 配额闸：幂等**非 owner** 的重发在上面的等待分支直接返回缓存体，不重复计数；
    # owner（真跑路由的这个）在分流前计一次。cfg=None → 闸内按请求覆盖链复算
    # （流式分支的 cfg 物化在生成器内，这里拿不到也不该提前载——见下方决策 1）。
    _gate_llm_quota(
        request, cfg=None, provider=provider, use_llm=use_llm, mock_llm=mock_llm,
        api_key=payload.api_key, requested_base_url=requested_base_url)

    if payload.stream:
        # 流式分支：路由（含 config 加载）挪进生成器——env override 必须罩住整个生成过程，
        # 在这里提前载好反而会在迭代开始前就还原（见 `_utterance_event_stream` 决策 1）。
        return StreamingResponse(
            _utterance_event_stream(
                text, payload,
                provider=provider, requested_base_url=requested_base_url,
                mock_llm=mock_llm, use_llm=use_llm,
                idem_entry=idem_entry, principal=principal,
                search_params=search_params,
                ask_answer=ask_answer,
            ),
            media_type="text/event-stream",
        )

    cfg = _materialize_request_llm_config(
        provider, use_llm=use_llm, mock_llm=mock_llm,
        api_key=payload.api_key, base_url=requested_base_url, model=payload.model)

    # owner 收尾（含异常路径）必须把最终响应体存进占用条目——等待中的同号重发靠它拿结果；
    # 异常体（ok=False）也存，等待方拿到的口径与 owner 一致。route_turn 契约是永不抛，
    # except 与流式分支的 error 帧同为结构性防御。
    body: dict | None = None
    try:
        result = turn.route_turn(
            text,
            has_results=bool(payload.has_results),
            result_total=int(payload.result_total or 0),
            current_query=payload.query or "",
            current_filters=payload.current_filters,
            sources=payload.sources,
            config=cfg,
            keyword_mapping=get_settings().keyword_mapping,
            use_agent=bool(payload.agent),
            principal=principal,
            search_params=search_params,
            # 课题上下文独立透传，只进 agent prompt。
            artifact_context=str(payload.artifact_context or ""),
            # 建议动作 hint 独立透传——allowlist 校验在 action_plan
            # 单一真源；只缩小到既有能力，不绕过参数校验/执行开关/安全闸。
            suggested_recipe=str(payload.suggested_recipe or ""),
            # 放松卡作答续跑（2026-09-14）：端点已净化（kind 恒 ask.relax、suppress 过闸）。
            ask_answer=ask_answer,
        )
        _inject_utterance_policy_id(result, payload, search_params, cfg.model)
        body = _utterance_response_body(result)
    except Exception:
        # webobs：与流式 worker 同口径——完整堆栈进日志，客户端只见通用文案。
        logger.exception("/api/utterance 非流式兜底触发：route_turn 抛出异常")
        body = {"ok": False, "detail": _UTTERANCE_INTERNAL_ERROR_DETAIL}
    finally:
        if idem_entry is not None:
            # body=None 只可能发生在 BaseException（如 KeyboardInterrupt）穿透 except 时——
            # 也要存错误体唤醒等待者，否则同号请求会干等到 180s 超时。
            _utterance_idem_store(idem_entry, body or {"ok": False, "detail": _UTTERANCE_INTERNAL_ERROR_DETAIL})
    return _json_utf8(body)


@app.post("/api/agent/search-rescue")
def api_agent_search_rescue(payload: SearchRescueRequest, request: Request) -> JSONResponse:
    """检索救回（2026-08-16 检索工具化 Phase 1）：零命中查询的「换词重检」入口。

    agent 在 rescue 收敛面（工具面只有 search.rerun / none，validate 与裁决层各有一道
    机械闸兜底）下跑一次图；search.rerun 的机械闸裁定采纳与否：
    采纳 → `adopted=true` + /api/recommend 同形 `payload`（调用方整屏替换结果）；
    结果集同集 / 筛选条件丢失 / LLM 选 none 放弃 → `adopted=false` + 诚实 reason，
    当前结果保持不变。设计决定：**改空（新条件 0 命中）也采纳**——空结果集
    照常上屏，是条件变更重检的诚实答案，不再否决后「保持不变」。
    rescue2：原检索投影（rule_match_summary 同径）注入提示词——未收录词
    允许丢弃/映射，已收录条件必须全保留；采纳时响应带 `dropped_terms`（机械比对）且
    `report_zh` 用确定性披露句点名丢弃词/改写词/命中数，前端零改动上屏。
    **绝不 500**：agent 缺席 / LLM 未配 / 图内任何异常都回 200 fail-open（reason 如实），
    本端点不产交付物、不落盘（工具内每次重检照常记联网账本——只读检索记 0 条口径）。"""
    _require_same_origin(request)
    from ..agent import action_plan as _ap
    from ..agent import agent_exec as _ax

    # 与 /api/utterance、/api/recommend 同一净化/日期闸；给了非法值必须 400，不能把
    # 用户条件静默吞成不限。倒挂窗口同样 fail-closed。
    rescue_facets = _sanitize_facet_filters(payload.facet_filters)
    rescue_suppressed = _sanitize_suppressed(payload.suppressed_constraints)
    rescue_lenient = _sanitize_lenient_dims(payload.lenient_dims)
    rescue_date_from = _require_iso_date(payload.date_from, name="date_from")
    rescue_date_to = _require_iso_date(payload.date_to, name="date_to")
    _validate_or_400(validate_date_window, rescue_date_from, rescue_date_to)
    rescue_search_params = {
        "facet_filters": rescue_facets,
        "suppressed_constraints": rescue_suppressed,
        "lenient_dims": rescue_lenient,
        "date_from": rescue_date_from,
        "date_to": rescue_date_to,
    }

    def _fail_open(reason: str, report_zh: str, *, available: bool) -> JSONResponse:
        return _json_utf8({
            "ok": True, "attempted": False, "reason": reason, "adopted": False,
            "query": payload.query, "rewrite": "", "n_before": None, "n_after": None,
            "payload": None, "report_zh": report_zh, "trace": [], "dropped_terms": [],
            "agent": {"available": available, "used": False},
        })

    provider, requested_base_url, mock_llm, use_llm = _resolve_llm_request(
        payload.provider, use_llm=payload.use_llm, mock_llm=payload.mock_llm,
        base_url=payload.base_url)
    # LLM 配置链与 /api/utterance 同径：ENV_LOCK + `_temporary_env` 请求级覆盖，
    # 锁内只载配置，锁外才跑图（不把网络 I/O 关在 ENV_LOCK 里）。
    cfg = _materialize_request_llm_config(
        provider, use_llm=use_llm, mock_llm=mock_llm,
        api_key=payload.api_key, base_url=requested_base_url, model=payload.model)

    if not _ax.agent_available():
        return _fail_open("agent_unavailable",
                          "没有重新检索：agent 扩展不可用，当前结果未变。",
                          available=False)
    llm_ok, why = _ap.should_use_llm(cfg)
    if not llm_ok:
        return _fail_open("llm_unavailable",
                          f"没有重新检索：大模型不可用（{why}），当前结果未变。",
                          available=True)
    # T3 配额闸：过了 agent/LLM 两道可用性检查、确定要真跑图才计数（fail-open 不计）。
    _gate_llm_quota(request, cfg=cfg, provider=provider, use_llm=use_llm,
                    mock_llm=mock_llm, api_key=payload.api_key)

    # rescue 不经 route_turn——端点侧自建
    # recorder 绑 context（图内 llm_call/tool_call/finish_reason 挂钩照常落盘）；
    # AGENT_TRACE OFF 时 enabled=False——零落盘、响应不加 trace_turn_id，逐位不变。
    from ..agent import trace as _trace
    _principal = _utterance_principal(request)
    _rec = _trace.recorder_for_turn(PROJECT_ROOT, session_id=_principal or "anonymous")

    def _with_tid(body: dict) -> dict:
        # additive 回显（报障给号用）；OFF 时不加键。
        if _rec.enabled:
            body["trace_turn_id"] = _rec.turn_id
        return body

    try:
        # rescue2：原检索投影（rule_match_summary 单一真源，与 /api/utterance
        # 路由同函数同口径）——给 rescue 提示词提供「哪些词未收录」的事实与择优闸的
        # dropped_terms 比对基准。投影失败 → None（fail-open：提示词退化为无清单档）。
        try:
            from ..agent.turn import rule_match_summary as _rms
            retrieval_summary = _rms(
                payload.query, sources=payload.sources, search_params=rescue_search_params)
        except Exception:
            logger.exception("search-rescue 原检索投影失败（fail-open：按无投影继续）")
            retrieval_summary = None
        with _trace.bind_recorder(_rec):
            plan, trace = _ax.plan_with_agent_events(
                payload.query,
                has_results=False,
                result_total=int(payload.result_total or 0),
                config=cfg,
                retrieval=retrieval_summary,
                current_query=payload.query,
                current_filters=payload.current_filters,
                route_scope="rescue",
                search_sources=payload.sources,
                search_facet_filters=rescue_facets,
                search_suppressed_constraints=rescue_suppressed,
                search_lenient_dims=rescue_lenient,
                search_date_from=rescue_date_from,
                search_date_to=rescue_date_to,
                principal=_principal,
            )
    except Exception:
        # 与 /api/utterance 非流式兜底同口径：完整堆栈进日志，客户端只见通用文案。
        logger.exception("/api/agent/search-rescue 兜底触发：agent 图抛出异常")
        return _fail_open("agent_error",
                          "这次重新检索没有跑成（内部错误已记录），当前结果未变。",
                          available=True)

    rerun_step = next(
        (s for s in list(plan.get("steps") or [])
         if str(s.get("verb") or "") == "search.rerun"),
        None,
    )
    report_zh = str(plan.get("report_zh") or "")
    if rerun_step is None or not rerun_step.get("ok"):
        # LLM 选 none 如实放弃（无 search.rerun 步），或重检步本身失败——都不带 payload。
        if rerun_step is None:
            return _json_utf8(_with_tid({
                "ok": True, "attempted": True, "reason": "no_rewrite", "adopted": False,
                "query": payload.query, "rewrite": "", "n_before": None, "n_after": None,
                "payload": None, "dropped_terms": [],
                "report_zh": report_zh or "没有更合适的换法可试，当前结果未变。",
                "trace": trace, "agent": {"available": True, "used": True},
            }))
        return _json_utf8(_with_tid({
            "ok": True, "attempted": True, "reason": "agent_error", "adopted": False,
            "query": payload.query,
            "rewrite": str((rerun_step.get("slots") or {}).get("query") or ""),
            "n_before": None, "n_after": None, "payload": None, "dropped_terms": [],
            "report_zh": report_zh or (
                "重新检索没有跑成：" + str(rerun_step.get("error") or "未知原因")),
            "trace": trace, "agent": {"available": True, "used": True},
        }))
    r = rerun_step.get("result") or {}
    dropped = [str(t) for t in (r.get("dropped_terms") or []) if str(t).strip()]
    return _json_utf8(_with_tid({
        "ok": True,
        "attempted": True,
        "reason": "adopted" if r.get("adopted") else str(r.get("reason") or "no_rewrite"),
        "adopted": bool(r.get("adopted")),
        "query": payload.query,
        "rewrite": str(r.get("query") or ""),
        "n_before": r.get("n_before"),
        "n_after": r.get("n_after"),
        "payload": r.get("payload"),
        # rescue2：采纳档 report_zh 用择优闸的确定性披露句（丢弃词/改写词/命中数全实算），
        # 前端 handleSearchRescue 优先显示 report_zh → 零改动上屏；LLM narrate 留 trace。
        "report_zh": str(r.get("disclosure_zh") or "") or report_zh,
        "dropped_terms": dropped,
        "trace": trace,
        "agent": {"available": True, "used": True},
    }))


@app.post("/api/act/summary")
def api_act_summary(payload: ActSummaryRequest, request: Request) -> JSONResponse:
    """执行结果的 LLM 中文总结（p10）。**只总结，不执行、不检索、不落盘。**

    done/gap/policy 事实行由调用方（前端 act_core，数字全部取自真实返回值）构造后上报，
    本端点不核实、也绝不新增事实——护栏写进 user prompt（`act_summary_llm._RULES_ZH`），
    ok=False 时总结绝不能说「已」。LLM 缺席/失败/空回 → fail-open：`summary_zh=None`，
    调用方原样保留自己的确定性事实句。

    LLM 配置链与 `/api/action/plan` 同径：ENV_LOCK + `_temporary_env` 请求级覆盖
    （请求级 key 隔离、endpoint 校验、server 配置对照），锁内只载 config，锁外才调 provider
    （不把网络 I/O 关在 ENV_LOCK 里）。

    `brief:true`走一句话模式（≤35 字），响应形状不变。
    """
    _require_same_origin(request)
    provider, requested_base_url, mock_llm, use_llm = _resolve_llm_request(
        payload.provider, use_llm=payload.use_llm, mock_llm=payload.mock_llm,
        base_url=payload.base_url)

    cfg = _materialize_request_llm_config(
        provider, use_llm=use_llm, mock_llm=mock_llm,
        api_key=payload.api_key, base_url=requested_base_url, model=payload.model)

    # T3 配额闸：cfg 已物化，将走服务端 key 时计数（BYOK/mock 不计）。
    _gate_llm_quota(request, cfg=cfg, provider=provider, use_llm=use_llm,
                    mock_llm=mock_llm, api_key=payload.api_key)

    facts = {
        "verb_zh": payload.verb_zh,
        "utterance": payload.utterance,
        "ok": bool(payload.ok),
        "done_lines": payload.done_lines,
        "gap_lines": payload.gap_lines,
        "policy_lines": payload.policy_lines,
    }
    if payload.brief:
        # 一句话模式：与长总结同一通道同一 fail-open
        # 纪律，只换 prompt（铁律写死在 `act_summary_llm._BRIEF_RULES_ZH`）；响应形状不变。
        # llm_status 照常回报真实原因（闸口原因用 should_use_llm 的口径，provider 层失败
        # 记 failed）——前端折进 details，不因此降噪掉诊断信息。
        brief = act_summary_llm.summarize_brief_with_llm(facts, config=cfg)
        if brief:
            brief_status = "ok"
        else:
            gate_ok, gate_reason = act_summary_llm.should_use_llm(cfg)
            brief_status = "failed" if gate_ok else gate_reason
        return _json_utf8({
            "ok": True,
            "summary_zh": brief,
            "summary_source": "llm" if brief else None,
            "llm_status": brief_status,
            "llm_model": None,
        })
    result = act_summary_llm.summarize_action_with_llm(facts, config=cfg)
    return _json_utf8({
        "ok": True,
        "summary_zh": result["summary_zh"],
        "summary_source": result["summary_source"],
        "llm_status": result["llm_status"],
        "llm_model": result["llm_model"],
    })


@app.post("/api/search/reply")
def api_search_reply(payload: SearchReplyRequest, request: Request) -> JSONResponse:
    """检索回执的 LLM 中文改写（2026-08-30 p11）。**只解说，不检索、不执行、不落盘。**

    命中数/展示数/命中关键词/解析状态等事实由调用方（前端 board.js cbPushCurrent，数字全部
    取自真实检索响应）构造后上报，本端点不核实、也绝不新增事实——护栏写进 user prompt
    （`search_reply_llm._SEARCH_REPLY_RULES_ZH`），0 命中直说没找到、建议只许从 can_suggest
    白名单原样挑一条。LLM 缺席/失败/空回 → fail-open：`reply_zh=None`，调用方原样保留
    自己的确定性事实句（不挂「AI 总结」标，归因诚实）。

    LLM 配置链与 `/api/act/summary` 同径：ENV_LOCK + `_temporary_env` 请求级覆盖
    （请求级 key 隔离、endpoint 校验、server 配置对照），锁内只载 config，锁外才调 provider
    （不把网络 I/O 关在 ENV_LOCK 里）；T3 配额闸同口径。
    """
    _require_same_origin(request)
    provider, requested_base_url, mock_llm, use_llm = _resolve_llm_request(
        payload.provider, use_llm=payload.use_llm, mock_llm=payload.mock_llm,
        base_url=payload.base_url)

    cfg = _materialize_request_llm_config(
        provider, use_llm=use_llm, mock_llm=mock_llm,
        api_key=payload.api_key, base_url=requested_base_url, model=payload.model)

    # T3 配额闸：cfg 已物化，将走服务端 key 时计数（BYOK/mock 不计）。
    _gate_llm_quota(request, cfg=cfg, provider=provider, use_llm=use_llm,
                    mock_llm=mock_llm, api_key=payload.api_key)

    facts = {
        "utterance": payload.utterance,
        "query": payload.query,
        "note": payload.note,
        "total": int(payload.total),
        "shown": int(payload.shown),
        "hit_keywords": payload.hit_keywords,
        "resolution_status": payload.resolution_status,
        "has_relax": bool(payload.has_relax),
        "can_suggest": payload.can_suggest,
    }
    result = search_reply_llm.search_reply_with_llm(facts, config=cfg)
    return _json_utf8({
        "ok": True,
        "reply_zh": result["reply_zh"],
        "reply_source": result["reply_source"],
        "llm_status": result["llm_status"],
        "llm_model": result["llm_model"],
    })


# 上传/管护导入共用同一把体积闸：单细胞元数据 JSON 远小于 64MB，
# 超出的只可能是误操作或本机 DoS 试探（/api/upload 此前无闸，68.7MB 整读内存照收）。
# 数值与全站原始 body 上限（`_RawBodyLimitMiddleware`）同源，一处常量防口径分叉。
_MAX_UPLOAD_BYTES = _MAX_RAW_BODY_BYTES
# 分块流式读的块大小：1 MiB。拒绝前绝不整读进内存（体积闸）。
_MAX_UPLOAD_CHUNK_BYTES = 1024 * 1024


async def _read_upload_bounded(file: UploadFile, *, max_bytes: int) -> bytes:
    """分块流式读取上传文件，累计超过 `max_bytes` 立即关闭并中止（413），绝不整读复制。

    体积闸：旧实现 `await file.read()` 先占满内存（或 multipart 临时文件先落满盘）才检查
    `len(raw_bytes)`；缺失/非数字/谎报 Content-Length 或 chunked 的请求会在拒绝前占用完整内存。
    本函数每次只读固定块并累计，上限一到立即关闭 `UploadFile`（释放 multipart 临时文件）。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_MAX_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            await file.close()
            raise HTTPException(status_code=413, detail=_UPLOAD_TOO_LARGE_DETAIL)
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/api/upload")
async def api_upload(
    request: Request,
    file: UploadFile = File(...),
    source: str = Query(default=""),
) -> JSONResponse:
    """上传一个数据集 JSON → 落入**外部库目录** `database/external/`（**绝不入** `database/base/` 基准语料）→ 即时可检索。
    - 确定性安全：官方 54 题评测走 base-only，永不读外部库 → 上传不影响 767 基准。
    - 逐条打来源标签（每条自带 `source` > 表单 `source` > 文件包裹层 `source` > 默认「用户上传」），
      使其作为一个**可勾选的来源**出现在浏览/查询里，且计数口径正确（不被误判为 10x Genomics）。
    - 返回解析条数、归属来源、可读校验提示（缺 dataset_name / 物种非通用名），让格式错误当场可见、不被误读。"""
    _require_same_origin(request)
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名。")

    # 体积闸三道（体积闸）：① 全站 `_RawBodyLimitMiddleware` 的 Content-Length 预检 + 实际
    # 字节计数（在路由/解析前拦，见中间件）；② 这里按 multipart 外层 Content-Length 再核一次
    #（端点级纵深防御，超限 413 人话）；③ `_read_upload_bounded` 分块流式读，文件真实字节
    # 累计超限立即关闭并中止——谎报/chunked 的请求拒绝前绝不整读占用完整内存。
    declared = (request.headers.get("content-length") or "").strip()
    if declared.isdigit() and int(declared) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=_UPLOAD_TOO_LARGE_DETAIL)

    # 落盘/校验/打标核心走共用真源 uploads.ingest_dataset（与 MCP upload_dataset 同一套逻辑）。
    # `_new_upload_name` 仍在 webapp 层调用（保留安全测试 monkeypatch 的 seam）——且必须在
    # `_require_same_origin` 之后、任何写副作用之前。UploadError（机器码+人读提示）翻回 400，
    # detail 逐位沿用旧文案（bad_file/bad_encoding/invalid_json/no_records）。
    try:
        # Never let a user upload occupy a release-allowlisted public snapshot
        # filename, even if that public file is temporarily absent.
        safe_name = _new_upload_name(file.filename)
        # 分块流式读 + 累计上限（体积闸）：超限立即关闭并 413，绝不整读复制。
        raw_bytes = await _read_upload_bounded(file, max_bytes=_MAX_UPLOAD_BYTES)
        # ingest_dataset 是同步重活（≤64MB JSON 解析 + 写盘 + 缓存失效，可达秒级）——async
        # 端点里直接调会阻塞事件循环、拖停所有并发请求（含 /api/health），下沉线程池执行。
        result = await run_in_threadpool(
            ingest_dataset,
            raw_bytes=raw_bytes,
            safe_name=safe_name,
            project_root=PROJECT_ROOT,
            form_source=source,
        )
    except UploadError as exc:
        raise HTTPException(status_code=400, detail=exc.hint,
                            headers={"X-Error-Code": exc.code}) from exc

    return _json_utf8(
        {
        "ok": True,
        "filename": result.filename,
        "saved_to": result.saved_to,
        "record_count": result.record_count,
        "sources": result.sources,   # {来源名: 条数}
        "warnings": result.warnings,  # 可读校验提示（空数组=无问题）
        "joined_knowledge_base": True,
        }
    )


# ---------------------------------------------------------------- /api/curate/*（对话式数据库管护）
# 设计蓝本：设计文档（2026-08-01 用户明确授权：管护动作内允许显式联网
# 调官方公开 API）。与 MCP `curate_datasets` / CLI `scripts/curate_datasets.py` 共用
# `corpus_curation.run_curate_action` 单一真源分发；管护对象限 database/external/ 的 upload_*
# 命名空间，database/base/ 冻结基准结构性不可达。检索器/编排/冻结评测不 import corpus_curation
# （tests/test_curation_isolation.py AST 机械门钉死）→ 767 基准结构性不受影响。
_MAX_CURATE_PAYLOAD_BYTES = _MAX_UPLOAD_BYTES   # 与 /api/upload 同一把闸（常量同源，防两处口径再分叉）


def _curate_payload_bytes(payload_json: str | None) -> bytes | None:
    """import 的 JSON 文本 → 字节；空 → None（由真源报 bad_param），超上限 → bad_param。"""
    if payload_json is None or not payload_json.strip():
        return None
    raw = payload_json.encode("utf-8")
    if len(raw) > _MAX_CURATE_PAYLOAD_BYTES:
        from ..corpus.corpus_curation import CurateError
        raise CurateError(
            "bad_param",
            f"导入的 JSON 文本超过上限（64 MB）；单细胞元数据 JSON 一般远小于此。",
        )
    return raw


@app.post("/api/curate/plan")
def api_curate_plan(payload: CuratePlanRequest, request: Request) -> JSONResponse:
    """对话式数据库管护第一步：preview + confirm_token。**零写盘**
    （search_online 的 plan 会真实联网查官方源，并记一行请求账本 .userdata/curate_net_ledger.jsonl）。

    动作：list 清点 / import 本地导入（内容 hash 去重）/ search_online 联网搜官方源（候选不落盘）/
    remove 回收站式删除预览 / restore 恢复预览。拿到 preview 与 confirm_token 后念给用户确认，
    再走 `/api/curate/apply`。"""
    _require_same_origin(request)
    from ..corpus import corpus_curation as cc
    try:
        result = cc.run_curate_action(
            payload.action,
            dry_run=True,
            query=payload.query,
            source=payload.source,
            species=payload.species,
            limit=payload.limit,
            filename=payload.filename,
            payload_bytes=_curate_payload_bytes(payload.payload_json),
            project_root=PROJECT_ROOT,
        )
    except cc.CurateError as exc:
        raise HTTPException(status_code=400, detail=exc.hint,
                            headers={"X-Error-Code": exc.code}) from exc
    return _json_utf8({"ok": True, "dry_run": True, "result": result})


@app.post("/api/curate/apply")
def api_curate_apply(payload: CurateApplyRequest, request: Request) -> JSONResponse:
    """对话式数据库管护第二步：回传 confirm_token **真执行**（import/search_online 入库写盘；
    remove 移入回收站；restore 移回）。token 重算比对不一致 → token_mismatch，**一个字节不动**。
    search_online 的 apply 还需把 plan 返回的完整结果作为 plan_result 原样回传。"""
    _require_same_origin(request)
    from ..corpus import corpus_curation as cc
    try:
        result = cc.run_curate_action(
            payload.action,
            dry_run=False,
            query=payload.query,
            source=payload.source,
            species=payload.species,
            limit=payload.limit,
            filename=payload.filename,
            payload_bytes=_curate_payload_bytes(payload.payload_json),
            plan_result=payload.plan_result,
            confirm_token=payload.confirm_token,
            force=payload.force,
            project_root=PROJECT_ROOT,
        )
    except cc.CurateError as exc:
        raise HTTPException(status_code=400, detail=exc.hint,
                            headers={"X-Error-Code": exc.code}) from exc
    return _json_utf8({"ok": True, "dry_run": False, "result": result})


@app.post("/api/curate/check-updates")
def api_curate_check_updates(payload: CurateCheckUpdatesRequest, request: Request) -> JSONResponse:
    """检查来源更新（2026-08-03 新能力 `curate.check_updates`，扩在线源）。**只读**：无 confirm_token、
    不落盘（在线比对会经限速唯一出口联网，并记一行请求账本）。

    有在线通道的来源（ArrayExpress / ENCODE / 10x）真在线拉最新清单与本地库比对；
    离线快照源（CELLxGENE/EBI SCEA/HCA）如实报告本地快照条数/日期 + 官网核对入口，
    不伪造在线比对能力。网络失败不 5xx——结果里该来源的 note_zh 如实写明
    （ENCODE/10x 拉不到时 mode 如实降级 "snapshot"）。"""
    _require_same_origin(request)
    from ..corpus import corpus_curation as cc
    result = cc.check_updates(payload.sources, project_root=PROJECT_ROOT)
    return _json_utf8({"ok": True, "result": result})


# ---------------------------------------------------------------- 语料同步后台任务（2026-08-26 corpus-sync 编排批）
# 动机：sync_updates 是分钟级任务，此前 /api/curate/sync-updates 请求内阻塞——网页形态
# （guard on）下用户触发与服务器 cron 都需要「启动即返回 + 状态轮询」。这里实现**进程内单飞
# job**：后台线程跑 sync_updates（显式无补丁作用域 → 共享写层 upload_*，用户上传都在各自
# 补丁包、不经此路），完成后 invalidate_external_cache()，有新增再子进程重建语料向量文件
# （原子替换 + recall_api.invalidate_vectors()）。单飞吸收并发：running 中重复触发不新建、
# 不抛 sync_busy 给调用方，返回同一个 job 的现状。
# guard off（本机形态）不经过这里：/api/curate/sync-updates 保持请求内阻塞，逐字节不变。
#
# 2026-09-08 Redis/MQ 批（默认线程路径一行不动，两个 opt-in 增强）：
# ① durable_jobs SQLite 持久镜像——状态跃迁写穿透 + 进程重启对账（running 孤儿如实翻
# failed）+ 全新进程 idle 时回落呈现上一轮终态，均 best-effort，失败只告警不返炸；
# ② env BIODATA_JOB_BACKEND=rq → start/snapshot 委托 rq_jobs（Redis 状态 hash + RQ worker
# 跨进程执行；worker 跑完后 web 进程经 needs_web_invalidate 旗标在本进程补做缓存失效）。
# 两条路径互斥不双写；未设 env 时下列代码行为与历史逐字节一致。

_CORPUS_SYNC_JOB_LOCK = threading.Lock()
_CORPUS_SYNC_JOB: dict = {
    "status": "idle",            # idle / running / done / failed
    "started_at": None,
    "finished_at": None,
    "result": None,              # sync_updates 的 operation receipt（imported_total 等）
    "error": None,
}
#: 向量重建子进程超时（秒）：全语料嵌入是十分钟级任务，20 分钟上限兜底防挂死。
_CORPUS_SYNC_VECTOR_TIMEOUT_S = 20 * 60
#: durable_jobs 表按 kind 一行；本模块只有这一类 job。
_CORPUS_SYNC_JOB_KIND = "corpus_sync"
#: 每进程只对持久镜像 reconcile 一次（进程启动后首次触碰 job 状态时）。
_CORPUS_SYNC_RECONCILED = False


def _corpus_sync_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _corpus_sync_jobs_db() -> Path:
    """持久镜像库路径（实例 userdata 层 / env 覆盖，规则真源在 durable_jobs）。"""
    from . import durable_jobs  # 惰性：webapp 顶层零新 import 边
    return durable_jobs.default_db_path(PROJECT_ROOT)


def _corpus_sync_mirror_warn(step: str, exc: Exception) -> None:
    """持久镜像写失败只记告警，绝不反过来炸掉 job 本体/状态端点（镜像是增强，不是真源）。"""
    print(f"[corpus-sync] 持久镜像 {step} 失败（不影响任务本身）：{type(exc).__name__}: {exc}",
          file=sys.stderr)


def _corpus_sync_reconcile_once() -> None:
    """进程启动后首次触碰 job 状态时镜像对账：镜像停在 running = 上一进程被打断，如实翻
    failed（单 worker 前提下成立——本进程没发起它，running 镜像即孤儿）。"""
    global _CORPUS_SYNC_RECONCILED
    if _CORPUS_SYNC_RECONCILED:
        return
    _CORPUS_SYNC_RECONCILED = True
    from . import durable_jobs  # 惰性：webapp 顶层零新 import 边
    try:
        durable_jobs.reconcile_interrupted(
            _CORPUS_SYNC_JOB_KIND, "进程重启，上一次同步任务被中断。", db_path=_corpus_sync_jobs_db())
    except Exception as exc:
        _corpus_sync_mirror_warn("reconcile", exc)


def _corpus_sync_job_snapshot() -> dict:
    """job 状态快照。RQ 后端点亮时真源在 Redis（读不到如实 503，不伪造 idle）；默认路径
    返回内存快照，全新进程（idle 且从未跑过）时回落到持久镜像——重启不丢上一轮终态。"""
    from . import rq_jobs  # 惰性：webapp 顶层零新 import 边
    if rq_jobs.backend_active():
        try:
            snap = rq_jobs.snapshot()
        except rq_jobs.JobBackendError as exc:
            raise HTTPException(status_code=503, detail=exc.message,
                                headers={"X-Error-Code": exc.code}) from exc
        if snap["status"] in ("done", "failed") and rq_jobs.consume_web_invalidate_flag():
            # worker 在独立进程跑完：它清不了 web 进程的内存缓存，真正有效的失效在这里补做。
            invalidate_external_cache()
            from ..retrieval import recall_api  # 惰性：webapp 顶层零新 import 边
            recall_api.invalidate_vectors()
        return snap
    _corpus_sync_reconcile_once()
    with _CORPUS_SYNC_JOB_LOCK:
        snap = dict(_CORPUS_SYNC_JOB)
    if snap["status"] == "idle" and snap["finished_at"] is None:
        from . import durable_jobs  # 惰性：webapp 顶层零新 import 边
        try:
            mirrored = durable_jobs.load(_CORPUS_SYNC_JOB_KIND, db_path=_corpus_sync_jobs_db())
        except Exception as exc:
            _corpus_sync_mirror_warn("load", exc)
            mirrored = None
        if mirrored is not None:
            return mirrored
    return snap


def _corpus_sync_rebuild_vectors() -> "str | None":
    """语料向量重建（job 第二阶段，同在后台线程）：子进程跑 build_corpus_vectors.py
    --include-uploads 到临时文件，退出码 0（全成）或 2（留缺口，运行期查询侧补嵌兜住）都算
    可用 → os.replace 原子替换目标文件 → recall_api.invalidate_vectors()。
    目标路径取 env BIODATA_EMBED_VECTOR_FILE（部署侧指到 /data 持久路径；代码不硬编码）；
    未配置（本机形态默认）→ 无可重建，None 跳过不算失败。
    返回 None=成功/跳过；str=失败摘要（只留子进程输出 tail——构建脚本绝不打印 key，
    这里也不再经手 key 本身）。"""
    from ..retrieval import recall_api  # 惰性：webapp 模块顶层零新 import 边

    target = recall_api._vector_file_path()
    if target is None:
        return None
    script = Path(__file__).resolve().parents[3] / "scripts" / "build_corpus_vectors.py"
    if not script.is_file():
        return f"语料向量构建脚本缺失（{script.name} 不在安装内）"
    import subprocess  # 惰性：只在真正重建时加载

    tmp_path = target.with_name(target.name + ".rebuild-tmp")
    cmd = [sys.executable, str(script), "--include-uploads", "--out", str(tmp_path)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_CORPUS_SYNC_VECTOR_TIMEOUT_S, cwd=str(PROJECT_ROOT))
    except subprocess.TimeoutExpired:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return f"语料向量重建超时（{_CORPUS_SYNC_VECTOR_TIMEOUT_S // 60} 分钟上限）"
    tail = (((proc.stdout or "") + "\n" + (proc.stderr or "")).strip())[-1500:]
    if proc.returncode not in (0, 2):
        return f"语料向量重建失败（退出码 {proc.returncode}）：{tail or '无输出'}"
    os.replace(tmp_path, target)
    recall_api.invalidate_vectors()
    return None


def _corpus_sync_execute(sources: "list[str] | None") -> dict:
    """job 第一阶段执行体：sync_updates（显式无补丁作用域 → 共享写层 upload_*）。单通道——
    默认线程路径与 RQ worker 路径（rq_jobs.corpus_sync_entry）共用这一份实现，禁止平行
    写第二份。返回 operation receipt；异常原样上抛，由调用方收口为 failed。"""
    from ..corpus import corpus_curation as cc
    from ..corpus.patch_package import unbound_patch_scope

    with unbound_patch_scope():
        return cc.sync_updates(sources, project_root=PROJECT_ROOT)


def _corpus_sync_job_finalize(status: str, result: "dict | None", error: "str | None") -> None:
    """终态落账（默认线程路径）：内存快照更新 + 持久镜像 best-effort 写穿透。镜像失败只
    告警——运行期事实源仍是内存快照，镜像只服务重启对账。"""
    finished_at = _corpus_sync_now()
    with _CORPUS_SYNC_JOB_LOCK:
        _CORPUS_SYNC_JOB.update(
            status=status, finished_at=finished_at, result=result, error=error)
    from . import durable_jobs  # 惰性：webapp 顶层零新 import 边
    try:
        durable_jobs.record_terminal(
            _CORPUS_SYNC_JOB_KIND, status, finished_at, result, error,
            db_path=_corpus_sync_jobs_db())
    except Exception as exc:
        _corpus_sync_mirror_warn("record_terminal", exc)


def _corpus_sync_job_run(sources: "list[str] | None") -> None:
    """job 线程体（默认线程路径）：sync → 外部库缓存失效 →（有新增）向量重建。一切异常收口
    为 failed 状态，绝不漏栈到线程外；结果/错误都进 _CORPUS_SYNC_JOB 供状态端点如实呈现，
    并经 _corpus_sync_job_finalize 写穿透到持久镜像。"""
    result = None
    try:
        result = _corpus_sync_execute(sources)
        invalidate_external_cache()
        vec_err = None
        if int(result.get("imported_total") or 0) > 0:
            vec_err = _corpus_sync_rebuild_vectors()
        _corpus_sync_job_finalize("failed" if vec_err else "done", result, vec_err)
    except Exception as exc:  # 含 CurateError(sync_busy)：另一进程持整任务锁时如实 failed
        hint = getattr(exc, "hint", "") or str(exc) or type(exc).__name__
        _corpus_sync_job_finalize("failed", result, hint)


def _corpus_sync_job_start(sources: "list[str] | None") -> dict:
    """启动或附着语料同步 job（单飞：running 中不新建、不抛 sync_busy，直接返回现状快照）。

    env BIODATA_JOB_BACKEND=rq → 委托 rq_jobs（跨进程 worker 执行）；后端不可用如实 503，
    不静默回落内存路径（用户明明开了 MQ，静默降级会变成查不出来的错觉）。默认路径先对
    持久镜像做一次重启对账，置 running 后 best-effort 写穿透。"""
    from . import rq_jobs  # 惰性：webapp 顶层零新 import 边
    if rq_jobs.backend_active():
        try:
            return rq_jobs.start(sources)
        except rq_jobs.JobBackendError as exc:
            raise HTTPException(status_code=503, detail=exc.message,
                                headers={"X-Error-Code": exc.code}) from exc
    _corpus_sync_reconcile_once()
    with _CORPUS_SYNC_JOB_LOCK:
        if _CORPUS_SYNC_JOB["status"] == "running":
            return dict(_CORPUS_SYNC_JOB)
        started_at = _corpus_sync_now()
        _CORPUS_SYNC_JOB.update(
            status="running", started_at=started_at,
            finished_at=None, result=None, error=None)
    from . import durable_jobs  # 惰性：webapp 顶层零新 import 边
    try:
        durable_jobs.record_running(
            _CORPUS_SYNC_JOB_KIND, started_at, db_path=_corpus_sync_jobs_db())
    except Exception as exc:
        _corpus_sync_mirror_warn("record_running", exc)
    worker = threading.Thread(
        target=_corpus_sync_job_run, args=(sources,), name="corpus-sync-job", daemon=True)
    worker.start()
    return _corpus_sync_job_snapshot()


# ---------------------------------------------------------------- 管理端点（cron 用；token + loopback 双闸）

_ADMIN_TOKEN_ENV = "BIODATA_ADMIN_TOKEN"
_LOOPBACK_CLIENT_HOSTS = frozenset({"127.0.0.1", "::1"})


def _admin_client_host(request: Request) -> str:
    """对端地址（独立小函数：测试 monkeypatch 这里；真实部署 cron 从容器内 127.0.0.1 打来）。"""
    return str(request.client.host) if request.client else ""


def _require_admin(request: Request) -> None:
    """管理端点双闸：仅 loopback 对端 + env BIODATA_ADMIN_TOKEN（未配置 → 403 fail-closed
    「未启用」；X-Admin-Token 头经 hmac.compare_digest 比对，防时序侧信道）。
    token 绝不进日志/响应（请求日志中间件只记 method+path，不带头）。"""
    if _admin_client_host(request) not in _LOOPBACK_CLIENT_HOSTS:
        raise HTTPException(status_code=403, detail="管理端点仅接受本机回环调用。")
    token = os.getenv(_ADMIN_TOKEN_ENV, "").strip()
    if not token:
        raise HTTPException(status_code=403, detail="管理端点未启用（服务端未配置管理令牌）。")
    given = (request.headers.get("x-admin-token") or "").strip()
    if not given or not hmac.compare_digest(given, token):
        raise HTTPException(status_code=403, detail="管理令牌无效。")


@app.post("/api/admin/corpus-sync")
def api_admin_corpus_sync(request: Request) -> JSONResponse:
    """cron 用：启动/附着语料同步 job（恒全源），202 立即返回，不阻塞。双闸见 _require_admin。"""
    _require_same_origin(request)
    _require_admin(request)
    job = _corpus_sync_job_start(None)
    return _json_utf8({"ok": True, "job": job}, status_code=202)


@app.get("/api/admin/corpus-sync/status")
def api_admin_corpus_sync_status(request: Request) -> JSONResponse:
    """cron 用：查语料同步 job 状态。双闸见 _require_admin。"""
    _require_same_origin(request)
    _require_admin(request)
    return _json_utf8({"ok": True, "job": _corpus_sync_job_snapshot()})


@app.post("/api/curate/sync-updates")
def api_curate_sync_updates(payload: CurateSyncUpdatesRequest, request: Request) -> JSONResponse:
    """检查更新 → 有新增则自动入库的复合流（2026-08-06 `curate.sync_updates`）。

    双消费点：① 未装 langchain 扩展时前端 runner 从这里跑同一份复合流（agent 图内 execute
    节点调的是同一个 `corpus_curation.sync_updates` 真源）；② CLI/外部直接调用。
    写侧经 uploads 管线 + 账本，可经回收站撤回；网络失败不 5xx——该来源的 note_zh 如实写明。

    整任务跨进程锁——另一进程/线程的 sync 正在跑 → CurateError
    (sync_busy) → HTTP 400（fail-closed 拒绝并发写，不排队）；返回扩展 operation receipt
    （operation_id / created_files[] / failed_sources[] / skipped_existing，既有字段兼容）。

    2026-08-26：**仅 guard on（网页形态）**改为异步——启动/附着进程内单飞
    job（见上方「语料同步后台任务」区块），202 立即返回 `{ok, job, async: true}`（additive
    新键），前端轮询 `GET /api/curate/sync-updates/status`；guard off（本机形态）下方阻塞
    路径逐字节不变。"""
    _require_same_origin(request)
    if _account_gate_required():
        job = _corpus_sync_job_start(payload.sources)
        return _json_utf8({"ok": True, "job": job, "async": True}, status_code=202)
    from ..corpus import corpus_curation as cc
    try:
        result = cc.sync_updates(payload.sources, project_root=PROJECT_ROOT)
    except cc.CurateError as exc:
        raise HTTPException(status_code=400, detail=exc.hint,
                            headers={"X-Error-Code": exc.code}) from exc
    return _json_utf8({"ok": True, "result": result})


@app.get("/api/curate/sync-updates/status")
def api_curate_sync_updates_status(request: Request) -> JSONResponse:
    """语料同步 job 状态轮询（2026-08-26 登录即可——中间件闸，无 token 闸）。

    guard off（本机形态）下也可用：job 恒 idle（本机路径走上方阻塞端点，不起 job），
    前端据此自然降级。"""
    _require_same_origin(request)
    return _json_utf8({"ok": True, "job": _corpus_sync_job_snapshot()})


@app.get("/api/curate/sync-status")
def api_curate_sync_status(request: Request) -> JSONResponse:
    """实例级同步状态（2026-08-22 §7）：上次同步时间 / 上次 operation / 是否 busy。

    **只读、不写盘**（busy 实时探测 sync 整任务锁，跨进程准确；「上次同步」是**实例级事实**，
    不得存 per-profile localStorage——评审①#6 裁决）。"""
    _require_same_origin(request)
    from ..corpus import corpus_curation as cc
    return _json_utf8({"ok": True, "result": cc.sync_status(project_root=PROJECT_ROOT)})


@app.post("/api/curate/recall")
def api_curate_recall(payload: CurateRecallRequest, request: Request) -> JSONResponse:
    """按 operation_id **整次撤回**一次 sync 的全部成功写入（2026-08-22 §7）。

    回收站语义：把该 operation 的 created_files[] 移入 `.userdata/recycle/`（可逆、可重入、
    单文件失败不连累其余）；operation 不存在 → 400 unknown_operation。"""
    _require_same_origin(request)
    from ..corpus import corpus_curation as cc
    try:
        result = cc.recall_sync_operation(payload.operation_id, project_root=PROJECT_ROOT)
    except cc.CurateError as exc:
        raise HTTPException(status_code=400, detail=exc.hint,
                            headers={"X-Error-Code": exc.code}) from exc
    return _json_utf8({"ok": True, "result": result})


@app.post("/api/curate/status")
def api_curate_status(request: Request) -> JSONResponse:
    """数据库状态汇报（2026-08-03 `curate.db_status` 的能力端点）。**只读、离线、不抛**：
    各源条数/快照日期 + 外部库与回收站清单 + 近期联网审计摘要。

    双消费点：① 未装 langchain 扩展时前端 runner 从这里取同一份事实（agent 图内
    execute 节点调的是同一个 `corpus_status.db_status` 真源）；② CLI/外部只读探查。
    汇报措辞不归本端点（LLM 组织在 agent narrate / `/api/act/summary`）。"""
    _require_same_origin(request)
    from ..corpus import corpus_status as cs
    return _json_utf8({"ok": True, "result": cs.db_status(project_root=PROJECT_ROOT)})


# ---------------------------------------------------------------- /api/watch/check（watch 确定性重跑）
# 设计：设计文档 §4.1/§4.2（评审①阻断3 裁决）。
# 入参是**保存的确定性检索 spec**（课题 check_condition.spec），端点以 strategy=fixed /
# recall=off / rerank=off / polish=false 重跑确定性管线（零 LLM），返回：
#   {result_total, uids[]（≤200 无序）, fingerprints{uid:fp}, truncated, executed_spec（规范化后）, checked_at}
# 语义指纹 = 自定义 record_fingerprint_schema（版本化，见 RECORD_FINGERPRINT_SCHEMA）。**不许**
# 前端分页拉全库自写一套解析排序（§4.2 明确禁止）。
# 本区块是纯 additive：不触碰 /api/recommend 与既有 /api/curate/* 任何行为。

#: watch-check 返回的 uid 集合上限（与「命中 ≤200：存完整无序 uid 集合」一致）：
#: 只报告前 200 的 uid，>200 置 truncated=true 并建议收窄——此时**不得**声称「某条已从全部结果消失」。
_WATCH_CHECK_MAX_UIDS = 200

#: record_fingerprint_schema 版本（自定义 schema，版本化以便未来增字段不影响旧基线可比性）。
#: v1 = 稳定哈希 over 规范化 {dataset_uid, sample_size(count,unit), raw_data_status(code,authoritative)}。
#: 只覆盖「material change」定义的字段（§4.3：真实新增/消失、sample_size 变化、raw_data_status 变化），
#: 排序/score/文案/格式变化**不进指纹**——排序变了指纹不能变。现有数据无「元数据版本」字段，
#: 不虚构。spec_version 与指纹 schema 同版演进（WatchCheckRequest.spec_version 校验同值）。
RECORD_FINGERPRINT_SCHEMA = "v1"


def _norm_fingerprint_component(value: Any) -> str:
    """指纹组分的稳定规范化：None → ""；bool/数字 → 稳定字面量；字符串去首尾空白（大小写不归一——
    unit 等刻意归一化字段由调用方另行 .lower()，避免所有字段被无差别折叠）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return str(value).strip()


def record_fingerprint_v1(uid: Any, item: dict) -> str:
    """record_fingerprint_schema v1：一条检索结果的语义指纹（sha256 hex，全 64 位）。

    输入取 workflow `_serialize_retrieved_data` 的 payload 项（含 dataset_uid / count / unit /
    raw_data_status 结构化字段）。缺字段按空值规范化参与哈希（不抛）——语料字段演进时旧记录
    仍可与新记录比较。raw_data_status 若为字符串（老格式）按 code=该字符串、authoritative=false
    处理（本项目检索结果恒为结构化 dict，此处只是防御）。"""
    raw = item.get("raw_data_status") or {}
    code = raw.get("code") if isinstance(raw, dict) else raw
    authoritative = raw.get("authoritative") if isinstance(raw, dict) else False
    payload = {
        "schema": RECORD_FINGERPRINT_SCHEMA,
        "dataset_uid": _norm_fingerprint_component(uid),
        "sample_size": {
            "count": _norm_fingerprint_component(item.get("count")),
            "unit": _norm_fingerprint_component(item.get("unit")).lower(),   # 单位大小写归一（Cells≡cells）
        },
        "raw_data_status": {
            "code": _norm_fingerprint_component(code).lower(),
            "authoritative": bool(authoritative),
        },
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _validate_watch_spec(payload: WatchCheckRequest) -> dict:
    """watch-check 入参校验 + 规范化（返回 executed_spec 的确定性组成部分；非法 → HTTPException 400）。

    与 /api/recommend 同口径：sources 未知名/空白名显式 400 点名；非法日期/倒挂窗口 400；
    facet/suppressed/lenient 走同一 sanitize 白名单收敛；空 spec（无关键词/来源/分面）400——
    没有可重跑的检索条件时不许拿「全库浏览」冒充检查结果。"""
    spec_version = str(payload.spec_version or "").strip()
    if spec_version != RECORD_FINGERPRINT_SCHEMA:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的 spec_version「{spec_version}」（当前支持：{RECORD_FINGERPRINT_SCHEMA}）。",
        )
    query = payload.query.strip()
    if not query and not payload.sources and not payload.facet_filters:
        raise HTTPException(
            status_code=400,
            detail="检查条件为空（没有关键词/来源/分面条件），没有可重跑的确定性检索。",
        )
    if payload.sources:
        _validate_or_400(validate_sources, payload.sources,
                         known=known_source_values(DATA_DIR, PROJECT_ROOT))
    facet_filters = _sanitize_facet_filters(payload.facet_filters)
    suppressed_constraints = _sanitize_suppressed(payload.suppressed_constraints)
    lenient_dims = _sanitize_lenient_dims(payload.lenient_dims)
    date_from = _require_iso_date(payload.date_from, name="date_from")
    date_to = _require_iso_date(payload.date_to, name="date_to")
    _validate_or_400(validate_date_window, date_from, date_to)
    return {
        "spec_version": RECORD_FINGERPRINT_SCHEMA,
        "query": query,
        "sources": [str(x).strip() for x in payload.sources] if payload.sources else [],
        "facet_filters": facet_filters,
        "suppressed_constraints": suppressed_constraints,
        "lenient_dims": lenient_dims,
        "date_from": date_from,
        "date_to": date_to,
    }


@app.post("/api/watch/check")
def api_watch_check(payload: WatchCheckRequest, request: Request) -> JSONResponse:
    """课题更新检查的**确定性重跑**端点（2026-08-22 §4.1/§4.2，评审①阻断3）。

    入参为课题保存的确定性检索 spec；端点以 strategy=fixed、recall=off、rerank=off、polish=false
    重跑与 /api/recommend 同一条确定性管线（**零 LLM**，不读 LLM 配置），返回：
      result_total    未截断命中总数（作「是否收窄」判断的事实）；
      uids[]          ≤200 的 uid 集合（无序集合语义：实现保留检索器稳定顺序以支持确定性对拍，
                      调用方不得依赖顺序）；
      fingerprints    {uid: record_fingerprint_schema v1 语义指纹}（material change 比较用）；
      truncated       result_total > 200 时为 true（此时不得断言「某条已从全部结果消失」）；
      executed_spec   规范化后的执行规格（回显用户实际保存/生效的条件，不只是原始自然语言）；
      checked_at      本次检查时间（实例级事实）。
    baseline 生成（保存检查条件时）与逐次检查共用本端点口径，保证可比。"""
    _require_same_origin(request)
    spec = _validate_watch_spec(payload)
    workflow = DatasetRecommendationWorkflow(settings=get_settings())
    meta = workflow.run_with_meta(
        RecommendParams(
            query=spec["query"],
            top_k=_WATCH_CHECK_MAX_UIDS,
            use_llm=False,           # polish=false：零润色（润色是 use_llm 之下的子开关）
            mock_llm=False,
            rerank_backend="off",
            recall_backend="off",
            strategy="fixed",
            llm_available=False,     # 短路 LLM 配置构造：确定性路径不读 os.environ / 配置
            date_from=spec["date_from"],
            date_to=spec["date_to"],
            sources=spec["sources"] or None,
            facet_filters=spec["facet_filters"],
            suppressed_constraints=spec["suppressed_constraints"],
            lenient_dims=spec["lenient_dims"],
        )
    )
    uids: list[str] = []
    fingerprints: dict[str, str] = {}
    for item in meta.retrieved_data:
        uid = str(item.get("dataset_uid") or "").strip()
        if not uid:
            continue
        if len(uids) >= _WATCH_CHECK_MAX_UIDS:
            break
        uids.append(uid)
        fingerprints[uid] = record_fingerprint_v1(uid, item)
    result_total = int(meta.result_total or len(uids))
    from ..corpus.corpus_curation import _now_iso
    return _json_utf8({"ok": True, "result": {
        "result_total": result_total,
        "uids": uids,
        "fingerprints": fingerprints,
        "truncated": result_total > _WATCH_CHECK_MAX_UIDS,
        "executed_spec": {
            "spec_version": spec["spec_version"],
            "query": spec["query"],
            "sources": spec["sources"],
            "facet_filters": spec["facet_filters"],
            "suppressed_constraints": spec["suppressed_constraints"],
            "lenient_dims": spec["lenient_dims"],
            "date_from": spec["date_from"],
            "date_to": spec["date_to"],
            "strategy": "fixed",
            "recall": "off",
            "rerank": "off",
            "polish": False,
        },
        "checked_at": _now_iso(),
    }})


@app.post("/api/artifacts/export-pack")
def api_artifacts_export_pack(payload: ExportPackRequest, request: Request) -> Response:
    """课题导出中心（2026-08-22 「研究包」的落地形态）。

    入参 = 课题**当前状态快照**（前端组装：candidates 的 uid+status+reason+verified_at +
    check_condition + provenance + 导出类型 kind），服务端从**本地语料**解析数据集元数据
    （uid 的数据集内容不信任调用方、前端不传），生成研究材料 ZIP 下载：

      - manifest（稳定标识符 + 来源 URL + 最后核验时间）
      - 纳入/排除表（含理由与核验时间）
      - 下载任务包（复用 `task_pack` 的下载子集，放 `download/` 子目录）
      - 三格式引文：RIS / BibTeX（`reuse_pack` 既有）+ GB/T 7714-2015 新 formatter（[DS/OL]）
      - 检索与核验溯源（来自 provenance，只列在场字段）
      - 「数据发现与筛选方法」草稿（只从真实 provenance/search_trace 生成、明确标注草稿、
        不编造样本数/访问日期/数据库范围）
      - recipe.json（可重跑：check_condition.spec + provenance 关键参数 + 候选快照）

    `kind` 决定 ZIP 内容子集：download_list / citations / screening_record 是**单项轻量导出**
    （内容如实标注在包内 README「本包里有什么」）；full = 全部研究材料。

    **只读、离线、不调用 LLM、不写盘**（内存拼 ZIP，同 task_pack 范式）。目录版本
    （snapshot_id/content_digest）经 `X-Biodata-Export-Meta` 响应头回给前端写台账——
    那是实例级事实，前端不自己造。检索器/编排/冻结评测从不 import `export_pack`。"""
    _require_same_origin(request)
    from ..content import export_pack
    from ..content import task_pack as _task_pack
    try:
        kind = export_pack.sanitize_kind(payload.kind)
        snapshot = export_pack.sanitize_snapshot(payload.project)
    except export_pack.ExportPackError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    records = load_full_corpus(DATA_DIR, PROJECT_ROOT)
    pack = export_pack.build_export_pack(snapshot, records, today=datetime.date.today().isoformat())
    files = export_pack.render_files(pack, kind)
    try:
        blob = _task_pack.files_to_zip_bytes(files)
    except _task_pack.TaskPackError as exc:
        status = 413 if exc.code == "too_large" else 400
        return _json_utf8({"ok": False, "code": exc.code, "message_zh": str(exc)}, status_code=status)
    name = f"biodata-export-{pack['exported_at']}-{pack['corpus']['snapshot_id'][:8] or 'nocorpus'}.zip"
    # 台账用的实例级事实走响应头（ZIP 正文没有 JSON 元数据）：目录版本 = 语料快照单一真源。
    meta = json.dumps({
        "dataset_version": pack["corpus"]["snapshot_id"],
        "content_digest": pack["corpus"]["content_digest"],
        "exported_at": pack["exported_at"],
        "kind": kind,
    }, ensure_ascii=True, separators=(",", ":"))
    return Response(
        content=blob, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"',
                 "X-Biodata-Export-Meta": meta},
    )


@app.get("/spec/upload")
def upload_spec() -> FileResponse:
    """把《数据集上传规范》原文以纯文本内联呈现（前端「查看完整规范」链接指向此处）。
    只暴露这一份自撰的**公开**规范文件，**不挂载整个 `使用教程/` 目录**（避免泄漏组内其它材料）。"""
    # 使用教程是随包静态资源（只读）→ 从 resource 层取（frozen = _MEIPASS；source = 项目根）。
    spec = RESOURCE_ROOT / "使用教程" / "数据集上传" / "数据集上传规范.md"
    if not spec.exists():
        raise HTTPException(status_code=404, detail="规范文件未找到。")
    return FileResponse(spec, media_type="text/plain; charset=utf-8")


@app.post("/api/diagnose")
def api_diagnose(payload: DiagnoseRequest, request: Request) -> JSONResponse:
    _require_same_origin(request)
    normalized_provider = _normalize_provider(payload.provider)
    # mock/trial 是锁定通道：trial 地址锁定服务端托管值，请求级 base_url 一律丢弃
    #（与 `_resolve_llm_request` 同一口径——否则试用密钥可被引向攻击者端点。
    # 2026-08-31：该安全硬化经评审裁决接受，契约钉在
    # tests/test_webapp_endpoint_security.py::test_trial_ignores_request_base_url）。
    requested_base_url = "" if normalized_provider in ("mock", "trial") else _validate_endpoint_url(payload.base_url)
    # diagnose 的 mock 推导与 `_resolve_llm_request` 刻意不同（不经 use_llm 门），保持既有口径。
    mock_llm = bool(payload.mock_llm) or normalized_provider == "mock"
    use_llm = bool(payload.use_llm or mock_llm)

    # Diagnose and recommend intentionally share the same endpoint/key isolation
    # contract (now anchored in `_materialize_request_llm_config`).  The env
    # overrides already carry LLM_PROVIDER/ENABLE_LLM/MOCK_LLM, so the loaded
    # config needs neither provider_override nor post-load mutation.
    cfg = _materialize_request_llm_config(
        normalized_provider, use_llm=use_llm, mock_llm=mock_llm,
        api_key=payload.api_key, base_url=requested_base_url, model=payload.model)
    # cfg 在锁内已物化（含请求级 key/endpoint），网络探测挪到锁外——与本文件
    # /api/utterance、/api/act/summary 同一条纪律：绝不把网络 I/O 关在 ENV_LOCK 里
    #（2026-08-15 触发点一次死端点诊断曾占锁数个超时长，全进程 LLM 端点静默排队）。
    # healthcheck 里少量展示用 env 直读（各名 key 的 detected/missing）反映的是服务端环境；
    # 请求级 key 的回显走 cfg（"Effective API key"），不受挪出锁影响。
    health = healthcheck(cfg)
    network = diagnose_network(cfg)
    return _json_utf8(
        {
        "ok": True,
        "provider": normalized_provider,
        "healthcheck": health,
        "network_diagnose": network,
        }
    )


@app.get("/api/sources")
def api_sources() -> JSONResponse:
    """可选数据来源清单 + 各自数据集数（供智能查询页「按来源勾选」渲染）。
    10x Genomics 恒在且置顶；外部平台库来自静态快照。"""
    return _json_utf8({"ok": True, "sources": available_sources(DATA_DIR, PROJECT_ROOT)})


#: 记录 → 展示层 item 的**单一真源**在 `item_view.build_item`（Web 与 MCP 共用）。
#: 此前 webapp 与 mcp_server 各维护一份手抄投影，MCP 那份的 docstring 声称「同口径」
#: 却缺 modality/collection_doi —— 于是同一个 bug 在 Web 修好、在 MCP 依旧。
#: 保留本别名只为让既有调用点不必改名；新代码直接用 `item_view.build_item`。
_web_item_from_record = item_view.build_item

#: 来源交错与分面计数投影同理——单一真源在 item_view，别名仅免改既有调用点。
_facet = item_view.facet_list
_interleave_by_source = item_view.interleave_by_source

#: 全库浏览分页上限：`limit` 缺省（前端整拉）行为逐位不变；显式 `limit` 最大 100。
#: 收口：值来自 `app/limits.MAX_DATASETS_LIMIT`（与 MCP browse_datasets 同一常量源）；
#: 本别名保留历史引用点（测试/注释）不改名。
_MAX_DATASETS_LIMIT = MAX_DATASETS_LIMIT

# 前端默认一次整拉 `/api/datasets`。该响应数 MB，而基础/外部语料在同一
# 代际内是不变的：缓存**已序列化 bytes**，命中时既不重做 item 投影，也不重做
# JSON dumps。只缓存这一个默认全量响应，避免 limit/offset 组合形成无界内存。
_DATASETS_FULL_CACHE_LOCK = threading.Lock()
_DATASETS_FULL_CACHE: "tuple[tuple, bytes, str] | None" = None


def _reset_datasets_response_cache() -> None:
    """清理 Web 响应缓存（主要给契约测试；生产通过语料代际键自动失效）。"""
    global _DATASETS_FULL_CACHE
    with _DATASETS_FULL_CACHE_LOCK:
        _DATASETS_FULL_CACHE = None


def _etag_matches(request: Request, etag: str) -> bool:
    values = [part.strip() for part in (request.headers.get("if-none-match") or "").split(",")]
    return "*" in values or etag in values


def _dataset_facet_bits(record: Any) -> tuple[str, str, str, int | None]:
    """单记录 → 浏览页分面四元组 (species, platform, source, published_year)。

    PERF-M01：分面计数不再依赖构造完整展示 item（此前 limit=1 也为全库每条构造 item，
    O(N) CPU/内存/JSON 分配被公网重复请求放大）。四项派生与 `item_view.build_item`
    逐字同源——platform/source/published_year 与 build_item 同式，species 与旧浏览循环
    同式；改 build_item 派生时这里必须同步（tests/test_webapp_sec_s3.py 有 parity 钉）。"""
    raw = record.raw if isinstance(record.raw, dict) else {}
    species = (record.species or "").strip()
    platform = (record.platform_family or "").strip()
    source = str(raw.get("source") or "").strip() or "10x Genomics"
    published_date = str(raw.get("published_date") or "").strip()
    return species, platform, source, item_view.published_year(published_date)


def _datasets_payload(records: list[Any], *, limit: int | None, offset: int) -> dict[str, Any]:
    """从已交错的全库记录生成浏览载荷；分面口径与旧端点逐位一致。"""
    species_counter: dict[str, int] = {}
    platform_counter: dict[str, int] = {}
    source_counter: dict[str, int] = {}
    year_counter: dict[str, int] = {}
    unknown_year_count = 0
    for record in records:
        species, platform, source, year = _dataset_facet_bits(record)
        if species:
            species_counter[species] = species_counter.get(species, 0) + 1
        if platform:
            platform_counter[platform] = platform_counter.get(platform, 0) + 1
        source_counter[source] = source_counter.get(source, 0) + 1
        if year is not None:
            ykey = str(year)
            year_counter[ykey] = year_counter.get(ykey, 0) + 1
        else:
            unknown_year_count += 1
    selected = records[offset:] if limit is None else records[offset:offset + limit]
    items = [_web_item_from_record(r, include_introduction=False) for r in selected]
    return {
        "ok": True,
        "count": len(records),
        "facets": {
            "species": _facet(species_counter),
            "platform": _facet(platform_counter),
            "source": _facet(source_counter),
            "published_year": _facet(year_counter),
        },
        "unknown_year_count": unknown_year_count,
        "records": items,
    }


def _cached_datasets_full_response(records: list[Any], generation: tuple) -> tuple[bytes, str]:
    global _DATASETS_FULL_CACHE
    with _DATASETS_FULL_CACHE_LOCK:
        cached = _DATASETS_FULL_CACHE
        if cached is not None and cached[0] == generation:
            return cached[1], cached[2]
        payload = _datasets_payload(records, limit=None, offset=0)
        body = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        # GZip 会改变表示字节，故使用弱 ETag 表示 identity/gzip 语义等价。
        etag = f'W/"{hashlib.sha256(body).hexdigest()}"'
        _DATASETS_FULL_CACHE = (generation, body, etag)
        return body, etag


@app.get("/api/datasets")
def api_datasets(request: Request = None, limit: int | None = None, offset: int = 0) -> Response:
    """全库浏览：**所有来源并列**返回归一化记录 + 物种/平台/来源分面（前端做筛选与分页）。

    基础语料每次现算（767 条，开销可忽略），故上传新增的数据即时可见；
    外部平台库为静态快照（缓存），与基础语料并列展示、每条带来源标签。

    可选 `limit`/`offset`（2026-08-06 additive）：只截 `records` 当前页；
    `count` 与 `facets` 恒按全库计算（前端整拉一次做客户端筛选，不受影响）。
    不传时行为逐位不变。对齐 MCP `browse_datasets` 的分页入参。

    PERF-M01：`limit` 显式传入时有上限（`_MAX_DATASETS_LIMIT`，与 MCP browse_datasets 同源
    于 `app/limits.MAX_DATASETS_LIMIT`）；分面计数走
    `_dataset_facet_bits` 轻量投影（不再为全库每条构造完整 item），展示 item 只对
    当前页构造——公网重复请求不再放大 O(N) CPU/内存/JSON 分配。
    """
    if limit is not None:
        if limit < 1:
            raise HTTPException(status_code=400, detail="limit 需 ≥ 1。")
        if limit > _MAX_DATASETS_LIMIT:
            # A3：超上限是「值语义错误」→ 422（RFC 9110 语义：请求理解但无法处理）；
            # limit<1/offset<0 的格式类错误维持 400。前端按非 200 处理，不区分。
            raise HTTPException(status_code=422, detail=f"limit 最大 {_MAX_DATASETS_LIMIT}。")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset 需 ≥ 0。")
    if limit is None and offset == 0:
        generation = corpus_cache_generation(DATA_DIR, PROJECT_ROOT)
        records = _interleave_by_source(load_full_corpus(DATA_DIR, PROJECT_ROOT))
        generation_after_load = corpus_cache_generation(DATA_DIR, PROJECT_ROOT)
        if generation_after_load != generation:
            # 上传/管护刚好与本请求并发：第一份 records 可能来自失效前的 tuple。
            # 重读一次并以新代际缓存，不把旧内容错标成新代际。
            generation = generation_after_load
            records = _interleave_by_source(load_full_corpus(DATA_DIR, PROJECT_ROOT))
        body, etag = _cached_datasets_full_response(records, generation)
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if request is not None and _etag_matches(request, etag):
            return Response(status_code=304, headers=headers)
        return Response(
            content=body, media_type="application/json; charset=utf-8", headers=headers
        )
    records = _interleave_by_source(load_full_corpus(DATA_DIR, PROJECT_ROOT))
    return _json_utf8(_datasets_payload(records, limit=limit, offset=offset))


def _locate_record_or_409(uid: str, url: str, name: str, source: str):
    """introduction/fair 共用的定位前言：三键全空 → 400；`locate_record`（Web 与 MCP
    共用单一真源）按 uid 精确 > url 精确 > name 精确（source 消歧）定位，name 命中多条
    且消歧失败 → 409 + candidates（如实报歧义，绝不静默任取第一条；载荷形状是前端契约）；
    定位不到 → 404。命中则返回记录。"""
    if not (uid or url or name):
        raise HTTPException(status_code=400, detail="uid, url or name is required")
    record, ambiguous = locate_record(
        load_full_corpus(DATA_DIR, PROJECT_ROOT), uid=uid, url=url, name=name, source=source)
    if ambiguous:
        raise HTTPException(status_code=409, detail={
            "error": "ambiguous_name",
            "message": "该名称命中多条同名数据集，无法确定是哪一条；请改用 dataset_uid 精确指定。",
            "candidates": ambiguous,
        })
    if record is None:
        raise HTTPException(status_code=404, detail="dataset not found")
    return record


@app.get("/api/introduction")
def api_introduction(
    request: Request = None,  # type: ignore[assignment]  # FastAPI 按注解注入；直调（单元测试）时为 None
    uid: str = Query(default="", max_length=240),
    url: str = Query(default="", max_length=1200),
    name: str = Query(default="", max_length=600),
    source: str = Query(default="", max_length=200),
    llm: int = Query(default=0),
) -> JSONResponse:
    """按需返回单个数据集介绍，避免浏览列表为 5,000+ 条记录复制大段摘要。

    定位走 `corpus.locate_record`（Web 与 MCP 共用单一真源）：**严格优先级 uid 精确 > url 精确 >
    name 精确（source 消歧）**，每档键全扫一遍语料再退化——语料存在同名两条时（如 …-ff-ultima /
    …-ff-ultima-4），uid 全参请求绝不会被靠前那条的 name 命中截胡。name 命中多条且 source 消歧
    失败 → 409 + candidates（如实报歧义，绝不静默任取第一条）。刻意**不**建跨请求缓存索引做 O(1)：
    基础语料每次现算以保证上传即时可见（见 /api/datasets），缓存索引会在上传后静默过期，
    得不偿失（评审结论）。

    `llm=1`（N5，opt-in）：在确定性介绍上 additive 叠加 LLM 中文导读（`introduction.llm_summary`）。
    **双层门**：需服务端 `ENABLE_LLM` 开 **且** 本请求显式 `llm=1` 才会真调；否则 `llm_summary=None`、
    确定性介绍逐字不变（fail-open）。`llm=0`（默认）时响应与从前逐字节一致。

    限流闸：`llm=1` 是 GET 但会产生费用的外部请求——加与写端点同级的同源检查
    （`_require_same_origin`）与简单频率限制（`_rate_limited`）；`llm=0` 路径逐位不变
    （纯确定性只读，无成本，不加闸）。"""
    uid, url, name, source = (str(v or "").strip() for v in (uid, url, name, source))
    if llm:
        # 限流闸：llm=1 是 GET 但会产生费用的外部请求——加与写端点同级的同源检查
        # 与简单频率限制。`request` 由 FastAPI 自动注入（直调函数/单元测试不经 HTTP 时
        # 为 None，跳过同源检查——那是服务端内部调用，不是浏览器跨站请求）。
        if request is not None:
            _require_same_origin(request)
        if not _rate_limited("introduction:llm", limit=_LLM_INTRO_RATE_LIMIT, window=_LLM_INTRO_RATE_WINDOW):
            raise HTTPException(status_code=429, detail="AI 介绍生成过于频繁，请稍后再试。")
    record = _locate_record_or_409(uid, url, name, source)
    item = _web_item_from_record(record, include_introduction=True)
    intro = item["introduction"]
    if llm:
        from ..llm.intro_llm import enrich_introduction_with_llm
        # 在 ENV_LOCK 内加载**服务端** config：阻塞到任何并发请求的 _temporary_env 还原为止 →
        # 读到的是干净的服务端 env，绝不会捞到别的请求注入 os.environ 的请求级 api_key（否则会把
        # 那把 key 发到本服务器配置的 provider）。网络调用在**锁外**、用已捕获的 config 进行，不串行化。
        # 介绍端点本身不接收请求级 key，故只用服务端配置（对抗评审 minor：唯一没被 ENV_LOCK 罩住的 LLM 读取点）。
        with ENV_LOCK:
            intro_cfg = load_llm_config(project_root=CONFIG_ROOT)
        # 公网护栏硬化：护栏模式下 llm=1 介绍计入账号日配额（此前只有
        # 上面的 _rate_limited 进程内桶，挡不住「多账号轮着烧」）。直接复用 _gate_llm_quota
        # 同一口径：cfg 已物化传入；BYOK/mock/未启用/服务端无 key 不计。两道闸并行，闸关零影响。
        if request is not None:
            _gate_llm_quota(request, cfg=intro_cfg, provider=intro_cfg.provider,
                            use_llm=True, mock_llm=False, api_key=None)
        intro = enrich_introduction_with_llm(item, intro=intro, config=intro_cfg)
    return _json_utf8({"ok": True, "introduction": intro})


@app.get("/api/fair")
def api_fair(
    uid: str = Query(default="", max_length=240),
    url: str = Query(default="", max_length=1200),
    name: str = Query(default="", max_length=600),
    source: str = Query(default="", max_length=200),
) -> JSONResponse:
    """按需返回单个数据集的 **FAIR 元数据自检 + 投稿数据可用性说明（DAS）**。

    只读、纯元数据、确定性、离线（同 `/api/introduction`：不调用 LLM、不联网）；定位/消歧口径
    一致——`corpus.locate_record` 单一真源（uid 精确 > url 精确 > name 精确+source 消歧；
    name 多条消歧失败 → 409 + candidates 如实报歧义）。核心走 `fair.build_fair_report`
    （Web 与 MCP 共用单一真源）；检索器/编排/冻结评测从不 import `fair` → 冻结 767 基准结构性不受影响。"""
    uid, url, name, source = (str(v or "").strip() for v in (uid, url, name, source))
    record = _locate_record_or_409(uid, url, name, source)
    item = _web_item_from_record(record, include_introduction=True)
    return _json_utf8({"ok": True, "fair_report": build_fair_report(item)})


@app.post("/api/reuse-pack")
async def api_reuse_pack(request: Request) -> JSONResponse:
    """N 个复用的公开数据集 → 一份投稿材料（英文段落 + 补充表 + 待办清单）。

    **为什么是 POST 而不是 GET**：GET 会把用户勾选的每个 dataset_uid 打进 uvicorn 的
    access log —— 那是**事实上的埋点**，而「要不要做使用数据采集」是产品方明确保留的
    未决策项。同时也违反「个人数据不进 URL 参数」。
    这里没有权限问题（本机 loopback），纯粹是**不许把用户行为落进日志**。

    入参 **keys-only**：`{"uids": ["cxg:...", "ae:..."]}`。服务端自己按 uid 解析记录，
    **不接受**调用方传数据集内容——一旦开了「把数据集描述贴进来」的口子，产品就有了吃进
    未发表工作的路径。入参是键不是内容 → IP 红线是结构性的。

    只读、离线、不调用 LLM、**不写盘**（导出走前端 Blob）。核心走
    `reuse_pack.build_pack_for_uids`（Web 与 MCP 共用单一真源）；检索器/编排/冻结评测
    从不 import `reuse_pack` → 冻结 767 基准结构性不受影响。
    """
    _require_same_origin(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON：{\"uids\": [...]}") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象：{\"uids\": [...]}")
    try:
        uids = sanitize_uids(payload.get("uids"))
    except ReusePackError as exc:
        raise HTTPException(status_code=400, detail=str(exc),
                            headers={"X-Error-Code": exc.code}) from None
    # 装载全语料 + 按 uid 逐条构建 + 三种格式渲染是同步重活——async 端点里直接跑会阻塞
    # 事件循环拖停全部并发请求，下沉线程池执行（函数体在调用期解析模块全局名，monkeypatch 语义不变）。
    def _build_pack_payload() -> dict[str, Any]:
        pack = build_pack_for_uids(uids, load_full_corpus(DATA_DIR, PROJECT_ROOT))
        return {
            "ok": True,
            "pack": pack,
            "markdown": pack_to_markdown(pack),
            "ris": to_ris(pack),        # N10：数据集引文（RIS TY-DATA，非论文）
            "bibtex": to_bibtex(pack),  # N10：数据集引文（BibTeX @misc，非 @article）
        }

    return _json_utf8(await run_in_threadpool(_build_pack_payload))


@app.get("/api/citations/download")
def api_citations_download(f: str = Query(default="", max_length=200)) -> Response:
    """把环内 `cite.export`（图内 LOOP_TOOLS）落盘在 `.userdata/citations/` 的引文文件
    发回浏览器（2026-08-19 批；additive 契约）。

    **为什么需要它**：图内 `cite.export` 的文件写在**服务端** `.userdata/citations/`
    （write 语义由 trace 快照锚定、可被 curate.rollback 看到），浏览器拿不到——旧的
    runner 路径（`actRunCiteExport`，前端 Blob）只下 RIS、把 BibTeX 丢了。本端点把已写出
    的文件按名发回，卡片「下载」按钮直接链到这里。

    **白名单纪律**：入参 `f` 只接受**裸文件名**（basename）——含路径分隔符（`/` `\\`）、
    `..`、`.`、空值一律 400 拒绝；`resolve()` 后必须仍落在 `.userdata/citations/` 内
    （前缀判定，防符号链接/目录穿越）；文件不存在 → 404。响应 `Content-Disposition:
    attachment` + 按扩展名给 Content-Type（RIS / BibTeX / 文本兜底）。
    只读端点，不写盘、不调 LLM。"""
    name = str(f or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", "..") or Path(name).name != name:
        raise HTTPException(status_code=400, detail="f 必须是 .userdata/citations/ 下的裸文件名。")
    citations_dir = instance_data_dir_for(PROJECT_ROOT, ".userdata") / "citations"
    base = citations_dir.resolve()
    candidate = (citations_dir / name).resolve()
    if candidate == base or base not in candidate.parents:
        raise HTTPException(status_code=404, detail="引文文件不在允许的目录内。")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="引文文件不存在或已被清理。")
    ext = candidate.suffix.lower()
    media_type = {
        ".ris": "application/x-research-info-systems",
        ".bib": "application/x-bibtex",
        ".txt": "text/plain; charset=utf-8",
    }.get(ext, "text/plain; charset=utf-8")
    # 裸名校验只保证无路径分隔符；引号若混入会提前截断 Content-Disposition 的 filename
    # 参数——服务端产出的文件名按 ASCII 约束不含引号，这里滤一道是纵深防御（当前零行为变化）。
    cd_name = name.replace('"', "")
    return Response(
        content=candidate.read_bytes(),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{cd_name}"'},
    )


# ---------------------------------------------------------------- MCP 遥测与接入引导（批）
# 安装版 BioDataAgentMCP.exe 是**独立进程**，与 Web 同经 runtime_paths 解析 data_root
# （frozen = %LOCALAPPDATA%/BioDataAgent；source/portable = 项目根）——故本批读的
# `data_root/.userdata/mcp_calls.jsonl` 与 mcp_server 的 `_CALL_LOG_FILE` 是**同一物理文件**
# （tests/test_mcp_call_log.py 有路径一致回归钉；schema 单一真源在 mcp_server `_CALL_LOG_SCHEMA`）。
# 增量语义用**行号**（JSONL 每行一条记录，天然稳定）：`after` = 调用方已消费的最后一条记录
# 行号（0/缺省 = 从头）；`next_offset` = max(after, 末尾最后一条**成功解析**记录行号)。
# 解析失败的坏行跳过不计数（与 scripts/summarize_mcp_calls.load_records 同容忍口径）——
# 并发 append 时半截尾行会先被跳过、offset 未前进，下轮读到补全后的完整行。
_TELEMETRY_LOCK = threading.Lock()
_TELEMETRY_ACK_SCHEMA = "biodata-mcp-upload-cursor/v1"

# 分页参数：limit 上限对齐接收端 MAX_MCP_RECORDS（200，单包 mcp_records 条数上限）
# 默认 100；max_bytes 是 records 原文行（UTF-8 字节）的近似预算，默认 ~500KB、上限 2MB
# （与接收端 MAX_BODY_BYTES 同值——中继一拉一推不超接收端 body 上限）。
_MCP_CALLS_DEFAULT_LIMIT = 100
_MCP_CALLS_MAX_LIMIT = 200
_MCP_CALLS_DEFAULT_MAX_BYTES = 500_000
_MCP_CALLS_MAX_BYTES = 2 * 1024 * 1024


def _mcp_calls_log_path() -> Path:
    return instance_data_dir_for(PROJECT_ROOT, ".userdata") / "mcp_calls.jsonl"


def _mcp_calls_upload_cursor_path() -> Path:
    return instance_data_dir_for(PROJECT_ROOT, ".userdata") / "mcp_calls_uploaded.json"


def _legacy_mcp_call_id(line: str) -> str:
    """旧日志行（schema v0，无 call_id）按**行原文**合成稳定 call_id 并标 legacy:true——
    同一行每次读到都合成出同一个键，接收端幂等去重才有意义。"""
    return "legacy-" + hashlib.sha256(line.strip().encode("utf-8")).hexdigest()[:32]


def _parse_since_ts(raw: str) -> datetime.datetime | None:
    """since_ts 查询参数（ISO8601）；非法值 400 由调用方抛。naive 按 UTC 计。"""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="since_ts 需为 ISO8601 时间串。")
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


# 护栏形态下遥测中继两端点（读取/回执）的统一拒绝文案
_TELEMETRY_RELAY_WEB_DETAIL = "网页版不由本端点中继遥测。"


@app.get("/api/telemetry/mcp-calls")
def api_telemetry_mcp_calls(
    request: Request = None,
    after: int = Query(default=0),
    limit: int = Query(default=_MCP_CALLS_DEFAULT_LIMIT),
    max_bytes: int = Query(default=_MCP_CALLS_DEFAULT_MAX_BYTES),
    since_ts: str = Query(default=""),
) -> JSONResponse:
    """读 data_root/.userdata/mcp_calls.jsonl 的**行号增量**（批；additive 契约
    加分页与 legacy 键合成）。

    `after` = 已消费的最后一条记录行号（1-based；0/缺省 = 从头）。响应：
      - `records`：`after` 之后的记录数组（原样 JSONL 行；**无 call_id 的旧行** additive
        合成 `call_id="legacy-"+sha256(行原文)[:32]` 并标 `legacy:true`，幂等键稳定）；
      - `next_offset`：调用方下一次应传回的 `after`（幂等推进，绝不回退）；
      - `truncated`：本次因 limit/max_bytes 截断（还有未返回记录）时为 true。
    分页/过滤参数（全部 additive，缺省行为与旧版逐位一致）：
      - `limit`：单次最多返回记录数（默认 100，上限 200——对齐接收端 MAX_MCP_RECORDS）；
      - `max_bytes`：records 原文行 UTF-8 字节预算（默认 ~500KB，上限 2MB）；
      - `since_ts`：ISO8601；只返回 `ts` ≥ 该值的记录。**无 ts/不可解析 ts 的 legacy 行
        一律保留**（不过滤——它们是未消费数据，滤掉就永远传不出去了）；被 since_ts 滤掉的
        行视为已消费（offset 照常前进——传 since_ts 就是明确放弃旧数据）。
    截断/过滤都只影响 records 内容；`next_offset` 恒为本轮**实际检查过**的最后一条成功解析
    记录的行号。文件不存在 → `records: []`、`next_offset: 0`（正常业务结果，非报错）。
    仅本机 loopback 可达（middleware 强制）；同源检查与既有端点同风格（浏览器跨站不可读）。
    """
    if request is not None:
        _require_same_origin(request)
    # 公网护栏硬化：该文件在服务器上是跨账号共享的全局文件（本机形态的遥测
    # 中继通道），网页版不做中继——护栏模式下整条通道关闭，前端同步跳过拉取/回执。
    if _account_gate_required():
        raise HTTPException(status_code=403, detail=_TELEMETRY_RELAY_WEB_DETAIL)
    if after < 0:
        raise HTTPException(status_code=400, detail="after 需 ≥ 0。")
    if limit < 1 or limit > _MCP_CALLS_MAX_LIMIT:
        raise HTTPException(status_code=400, detail=f"limit 需在 1–{_MCP_CALLS_MAX_LIMIT} 之间。")
    if max_bytes < 1 or max_bytes > _MCP_CALLS_MAX_BYTES:
        raise HTTPException(status_code=400, detail=f"max_bytes 需在 1–{_MCP_CALLS_MAX_BYTES} 之间。")
    since = _parse_since_ts(since_ts)
    path = _mcp_calls_log_path()
    if not path.is_file():
        return _json_utf8({"ok": True, "records": [], "next_offset": 0, "truncated": False})
    records: list = []
    used_bytes = 0
    last_examined = after
    truncated = False
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if lineno <= after:
                continue
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # 坏行/半截尾行：跳过不计数（offset 不前进，下轮重试）
            if not isinstance(rec, dict):
                continue
            # 到limit/字节预算即停：本行**不消费**（offset 不覆盖它），下轮从它继续
            if len(records) >= limit or used_bytes + len(line.encode("utf-8")) > max_bytes:
                truncated = True
                break
            last_examined = lineno
            if since is not None:
                rec_ts = None
                if rec.get("ts"):
                    try:
                        rec_ts = _parse_since_ts(str(rec.get("ts")))
                    except HTTPException:
                        rec_ts = None  # 不可解析 ts 按 legacy 行处理：保留（不滤掉）
                if rec_ts is not None and rec_ts < since:
                    continue  # since_ts 滤掉的行视为已消费（offset 已前进）
            if not rec.get("call_id"):
                rec = dict(rec)
                rec["call_id"] = _legacy_mcp_call_id(line)
                rec["legacy"] = True
            records.append(rec)
            used_bytes += len(line.encode("utf-8"))
    return _json_utf8({"ok": True, "records": records, "next_offset": max(after, last_examined),
                       "truncated": truncated})


class TelemetryAckRequest(BaseModel):
    offset: int = Field(..., ge=0, description="已成功上传的最后一条 MCP 调用记录行号（1-based；0 = 未消费）")


@app.post("/api/telemetry/mcp-calls/ack")
def api_telemetry_mcp_calls_ack(payload: TelemetryAckRequest, request: Request) -> JSONResponse:
    """把已上传游标持久化到 data_root/.userdata/mcp_calls_uploaded.json（批
    改 CAS：**游标只前进**）。

    读-比-写整个在进程内锁内：已存 offset ≥ 请求值时不落盘（回退请求视为已达成——
    中继重放旧进度不会把游标拉回去造成整段重传），响应恒为 max(请求, 已存)。
    写盘走临时文件 + `os.replace` 原子落位，防并发 ack 互相撕扯。供中继/接收端在成功
    消费 records 后推进游标，本地进程重启后仍可续传。
    """
    _require_same_origin(request)
    # 公网护栏硬化：与上面的读取端点同闸——网页版不中继遥测，游标不落盘。
    if _account_gate_required():
        raise HTTPException(status_code=403, detail=_TELEMETRY_RELAY_WEB_DETAIL)
    target = _mcp_calls_upload_cursor_path()
    with _TELEMETRY_LOCK:
        stored = 0
        if target.exists():
            try:
                prev = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(prev, dict):
                    stored = max(0, int(prev.get("offset") or 0))
            except (OSError, ValueError, TypeError):
                stored = 0  # 游标文件损坏按 0 计（本次 ack 照旧推进修复）
        effective = max(payload.offset, stored)
        if effective > stored:
            value = {
                "schema": _TELEMETRY_ACK_SCHEMA,
                "offset": effective,
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            }
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(temporary, target)
    return _json_utf8({"ok": True, "offset": effective})


@app.get("/api/guide/agent-prompt")
def api_guide_agent_prompt(request: Request = None) -> Response:
    """返回 MCP 接入提示词全文（text/markdown；用户复制给自家 agent 代完成接入用）。

    只读资源（使用教程/MCP安装/agent接入提示词.md）经 runtime_paths 的 resource_root 解析
    （frozen = _MEIPASS；source/portable = 项目根），**绝不拼 cwd 相对路径**——与 /spec/upload 同风格。
    """
    if request is not None:
        _require_same_origin(request)
    prompt_file = RESOURCE_ROOT / "使用教程" / "MCP安装" / "agent接入提示词.md"
    if not prompt_file.is_file():
        raise HTTPException(status_code=404, detail="接入提示词文件不存在。")
    return Response(
        content=prompt_file.read_text(encoding="utf-8"),
        media_type="text/markdown; charset=utf-8",
        # filename 必须 ASCII：_security_headers middleware 对响应头做 latin-1 编码，
        # 中文文件名会 UnicodeEncodeError → 500（引文下载端点同约束）。
        headers={"Content-Disposition": 'inline; filename="agent-mcp-prompt.md"'},
    )


@app.get("/api/guide/online-prompt")
def api_guide_online_prompt(request: Request = None) -> Response:
    """在线 MCP 接入提示词模板（text/markdown；含 __BIODATA_MCP_URL__/__BIODATA_MCP_TOKEN__
    占位符，由前端在铸币成功后代入真实值再复制给用户）。

    与 api_guide_agent_prompt 同约束：只读资源经 RESOURCE_ROOT 解析、同源闸、ASCII 文件名。
    """
    if request is not None:
        _require_same_origin(request)
    prompt_file = RESOURCE_ROOT / "使用教程" / "MCP安装" / "在线接入提示词模板.md"
    if not prompt_file.is_file():
        raise HTTPException(status_code=404, detail="在线接入提示词模板文件不存在。")
    return Response(
        content=prompt_file.read_text(encoding="utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="online-mcp-prompt.md"'},
    )


@app.get("/api/guide/skill.zip")
def api_guide_skill_zip(request: Request = None) -> Response:
    """把随包 skill 目录现场打成 zip 返回（Content-Disposition 附件；批
    改**确定性构建**）。

    安装版用户不拿源码包时，从这里下载 `biodata-dataset-discovery` 技能目录解压到自家
    agent 的 skills 目录（Skill 安装教程路线 A/B）。zip 在内存构建、零落盘；只读资源目录
    缺失 → 404。arcname 以 `.agents/skills/` 为根，解压后即得可被客户端发现的目录形态。
    确定性：条目按 arcname 排序、ZipInfo 手工构造（date_time 固定 1980-01-01、
    create_system=3、external_attr=0644）、compresslevel=9——同一棵目录树两次构建
    字节完全一致，响应头 `X-SHA256` 给出本包摘要（中继/客户端可校验缓存与完整性）。
    """
    if request is not None:
        _require_same_origin(request)
    skill_dir = RESOURCE_ROOT / ".agents" / "skills" / "biodata-dataset-discovery"
    if not skill_dir.is_dir():
        raise HTTPException(status_code=404, detail="skill 目录不存在。")
    entries: list[tuple[str, bytes]] = []
    for path in sorted(skill_dir.rglob("*")):
        if path.is_file():
            entries.append((path.relative_to(skill_dir.parent).as_posix(), path.read_bytes()))
    entries.sort(key=lambda item: item[0])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for arcname, data in entries:
            zinfo = zipfile.ZipInfo(filename=arcname, date_time=(1980, 1, 1, 0, 0, 0))
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zinfo.create_system = 3
            zinfo.external_attr = (0o644 & 0xFFFF) << 16
            zf.writestr(zinfo, data)
    payload = buffer.getvalue()
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="biodata-dataset-discovery.zip"',
            "X-SHA256": hashlib.sha256(payload).hexdigest(),
        },
    )


@app.get("/api/compatible")
def api_compatible(uid: str = "", limit: int = 20) -> JSONResponse:
    """N13 元数据兼容分组：给一个数据集找**同物种 + 兼容 chemistry/platform** 的其它数据集。

    **诚实边界**：只回「元数据兼容」（可整合的必要非充分条件），始终附带 caveat，绝不说「可整合」。
    只读、确定性、离线；与 MCP `find_compatible_datasets` 共用 `compatibility.find_compatible` 单一真源。
    检索器/编排/冻结评测从不 import compatibility → 冻结 767 基准结构性不受影响。
    """
    from ..content import compatibility
    u = (uid or "").strip()
    if not u:
        raise HTTPException(status_code=400, detail="uid is required")
    try:
        lim = max(1, min(100, int(limit)))
    except (TypeError, ValueError):
        lim = 20
    result = compatibility.find_compatible(u, load_full_corpus(DATA_DIR, PROJECT_ROOT), limit=lim)
    if result is None:
        raise HTTPException(status_code=404, detail="没有找到对应的数据集")
    return _json_utf8({"ok": True, **result})


@app.get("/api/files")
def api_files(
    uid: str = Query(default="", description="dataset_uid（首选）"),
    url: str = Query(default="", description="数据集页面 url（uid 缺失时的回退键）"),
) -> JSONResponse:
    """某数据集的**全部**真实文件下载直链——前端点开卡片时按需拉取。

    默认查询/浏览响应只带代表性主文件（primary），避免每条结果拖着几十个直链。
    这里按 uid（或页面 url）现取该数据集完整 files 列表；查不到 → 空列表（降级，不报错）。
    """
    key = (uid or "").strip() or (url or "").strip()
    # 解析出 dataset_uid（活台账按 uid 索引；key 可能是页面 url，故经 downloads.get 落到 uid）。
    from dataset_recommender.corpus import downloads as _dl, inspection as _insp
    _rec = _dl.get(key)
    resolved_uid = _rec.get("dataset_uid") if _rec else None
    primary = primary_url(key)
    items: list[dict[str, Any]] = []
    for f in files_for(key):
        if not isinstance(f, dict):
            continue
        download_url = f.get("download_url")
        if not download_url:
            continue
        # 活台账最近实测状态（无记录/降级 → None）。前端仅在 problem=True 时打标记（additive，默认不标）。
        st = _insp.status_for(resolved_uid, download_url) if resolved_uid else None
        items.append(
            {
                "title": f.get("title") or f.get("filename") or "文件",
                "filename": f.get("filename") or "",
                "category": f.get("category") or "",
                "pipeline": f.get("pipeline") or "",
                "size_human": f.get("size_human") or "",
                "bytes": f.get("bytes") or 0,
                "download_url": download_url,
                "is_primary": bool(primary) and download_url == primary,
                "problem": bool(st and st.get("problem")),
                "problem_reason": (st or {}).get("problem_reason"),
                "last_verified": (st or {}).get("last_verified"),
            }
        )
    # 主文件（= 卡片「下载数据」按钮那份）排到最前，其余保持源顺序——让用户一眼对上主按钮。
    items.sort(key=lambda x: not x["is_primary"])
    return _json_utf8({"ok": True, "count": len(items), "files": items})


# ---------------------------------------------------------------- 服务端真下载（2026-08-19 批）

class DownloadPlanRequest(BaseModel):
    """POST /api/download/plan 入参。`uids: Any` 故意放宽形状、端点内手工校验成 400：
    pydantic 的 list[str] 严格校验会给 422，与「坏入参 → 400」的契约口径不一致。"""
    uids: Any = None


class DownloadStartRequest(BaseModel):
    uids: Any = None


class DownloadCancelRequest(BaseModel):
    job_id: str = ""


class DownloadUpdateRequest(BaseModel):
    """POST /api/download/update 入参（dl-auto-1 在途增删）。
    `add/remove: Any` 故意放宽形状、端点内手工校验成 400。"""
    add: Any = None
    remove: Any = None


def _sanitize_download_uids(raw: Any) -> list[str]:
    """入参严格校验：uids 必须是非空字符串数组；去重、去空白。坏形状一律 400。

    体积闸：新增最大数量上限（`_MAX_DOWNLOAD_UIDS`，超限 422）——此前 uids 只要求非空，
    攻击者可控页面可一次性塞成千上万个编号，plan/start 都会随之放大 CPU/内存/磁盘。
    """
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail="uids 必须是非空数组（数据集编号列表）。")
    if len(raw) > _MAX_DOWNLOAD_UIDS:
        raise HTTPException(
            status_code=422,
            detail=f"uids 数量超过上限（最多 {_MAX_DOWNLOAD_UIDS} 个）；请分批下载。",
        )
    out: list[str] = []
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, str):
            raise HTTPException(status_code=400,
                                detail=f"uids 里的每一项必须是数据集编号字符串，收到 {type(value).__name__}。")
        text = value.strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    if not out:
        raise HTTPException(status_code=400, detail="uids 里的数据集编号不能全为空字符串。")
    return out


_DOWNLOAD_ERROR_STATUS = {
    "bad_param": 400,
    "no_downloadable": 400,
    "disk_space_insufficient": 507,
    "job_conflict": 409,
    "job_not_running": 409,
    "unknown_job": 404,
}

#: 单次下载任务的最大数据集数量（体积闸）：uids 数组上限。下载是重 I/O 动作（逐数据集
#: 建目录 + 拉文件），超出的编号对任何正常使用都没有意义，只会放大 CPU/内存/磁盘。
_MAX_DOWNLOAD_UIDS = 100

#: 下载任务 404 的统一文案模板（查询/取消两处共用；job_id 占位由调用方 .format 填充）
_DOWNLOAD_JOB_NOT_FOUND_DETAIL = "没有这个下载任务：{job_id}。"


def _download_error_response(exc: BaseException) -> Response:
    code = getattr(exc, "code", "bad_param")
    status = _status_for(code, _DOWNLOAD_ERROR_STATUS)
    return _json_utf8({"ok": False, "code": code, "message_zh": str(exc)}, status_code=status)


def _reject_server_side_download() -> None:
    """公网护栏硬化：护栏模式下服务端代下数据整体关闭——真下载会把数据集
    拉进容器 home（不在 /data 持久卷），多用户公网形态语义错误且可被刷磁盘；网页版走任务包
    （客户端直连原站）。闸关（本机形态）零影响。"""
    if _account_gate_required():
        raise HTTPException(status_code=403, detail="网页版暂不支持服务端代下数据，请使用任务包。")


@app.post("/api/download/plan")
def api_download_plan(payload: DownloadPlanRequest, request: Request) -> Response:
    """真下载第一步：uids → 可下载清单（不落盘、不下载、不起线程，零网络）。

    响应：{ok, items:[{dataset_uid, dataset_title, source, tier, page_url, bytes,
    files:[{filename,url,bytes}]}], total_bytes, unsupported:[{dataset_uid,title,reason}]}。
    """
    _require_same_origin(request)
    _reject_server_side_download()
    from ..corpus import download_manager as DM
    uids = _sanitize_download_uids(payload.uids)
    plan = DM.build_download_plan(uids)
    return _json_utf8({"ok": True, "items": plan["items"], "total_bytes": plan["total_bytes"],
                       "unsupported": plan["unsupported"]})


@app.post("/api/download/start")
def api_download_start(payload: DownloadStartRequest, request: Request) -> Response:
    """真下载第二步：预检（无可下载项 400 / 磁盘不足 507 / 已有任务 409）→ 建目录 → 起线程。

    响应：{ok, job_id, dir, total_bytes}；目录 = ~/Downloads/BioData数据-<时间戳>/，
    每个数据集一个子文件夹（编号_标题），根目录随进度写 README.txt + manifest.tsv。
    """
    _require_same_origin(request)
    _reject_server_side_download()
    from ..corpus import download_manager as DM
    uids = _sanitize_download_uids(payload.uids)
    try:
        job = DM.start_job(uids)
    except DM.DownloadManagerError as exc:
        return _download_error_response(exc)
    return _json_utf8({"ok": True, "job_id": job["job_id"], "dir": job["dir"],
                       "total_bytes": job["total_bytes"]})


@app.get("/api/download/status")
def api_download_status(job: str = Query(default="")) -> Response:
    """真下载状态轮询：{ok, ...状态 dict}；任务不存在 → 404。"""
    _reject_server_side_download()
    from ..corpus import download_manager as DM
    job_id = (job or "").strip()
    state = DM.get_status(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail=_DOWNLOAD_JOB_NOT_FOUND_DETAIL.format(job_id=job_id or "(空)"))
    return _json_utf8({"ok": True, **state})


@app.post("/api/download/cancel")
def api_download_cancel(payload: DownloadCancelRequest, request: Request) -> Response:
    """取消下载：置取消标志，chunk 间/文件间停手；已保留的 .part 可续传。

    响应：{ok, state}；任务不存在 → 404。
    """
    _require_same_origin(request)
    _reject_server_side_download()
    from ..corpus import download_manager as DM
    job_id = (payload.job_id or "").strip()
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id 不能为空。")
    state = DM.cancel_job(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail=_DOWNLOAD_JOB_NOT_FOUND_DETAIL.format(job_id=job_id))
    return _json_utf8({"ok": True, "state": state["state"]})


@app.post("/api/download/update")
def api_download_update(payload: DownloadUpdateRequest, request: Request) -> Response:
    """在途增删（dl-auto-1）：对**当前运行中的**下载任务做增量 add/remove。

    入参 `{add: [uid...], remove: [uid...]}`（至少一个非空）。语义：
      - remove 排队中条目 → 跳过；remove 正在下载的条目 → 中止该数据集当前文件并清理它的
        未完成部分（.part 与子目录）、继续队列下一条；remove 已完成条目 → 409 级拒绝（如实说）。
      - add → 追加到当前 job 队列尾部（不可下载的如实标注）。
    无运行中任务 → 409 `job_not_running`；坏入参 → 400。响应除增删结果汇总外带当前快照，
    供前端直接刷新队列与进度。
    """
    _require_same_origin(request)
    _reject_server_side_download()
    from ..corpus import download_manager as DM
    add = _sanitize_download_uids(payload.add) if payload.add else []
    remove = _sanitize_download_uids(payload.remove) if payload.remove else []
    try:
        result = DM.update_job(add=add, remove=remove)
    except DM.DownloadManagerError as exc:
        return _download_error_response(exc)
    return _json_utf8({"ok": True, **result})
