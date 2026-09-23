from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import prompts
from .config import load_env_candidates, _parse_bool
from ..retrieval.units import format_sample_size
from ..content.labels import RAW_FASTQ_NO, raw_fastq_status


TABLE_HEADER = prompts.CURATOR_TABLE_HEADER
TABLE_SEPARATOR = prompts.CURATOR_TABLE_SEPARATOR

ZHIPU_PROVIDER_ALIASES = {"zhipuai", "zhipu", "bigmodel", "glm"}
OPENAI_PROVIDER_ALIASES = {"openai-compatible", "openai"}

OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
ZHIPU_DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
ZHIPU_DEFAULT_MODEL = "glm-5.1"
#: 限量试用通道：部署方在
#: 服务端环境变量托管 key（`BIODATA_TRIAL_API_KEY`，只在进程环境，请求级覆盖链从不注入），
#: 前端「限量试用」预设不带 key/地址/模型（锁定不可改），每日轮数由 webapp 配额闸限制。
#: base/model 另有 `BIODATA_TRIAL_BASE_URL` / `BIODATA_TRIAL_MODEL` 可覆盖；默认锁定
#: DeepSeek 端点 + deepseek-v4-flash。凭据**不**回落 `BIODATA_EMBED_API_KEY`——那是智谱
#: key，对 DeepSeek 端点无效。
#: 思考档：deepseek-v4-flash 默认思考档拒收 tool_choice="required"
#: 且更慢，故 trial 默认 thinking=False（发 disabled）；`BIODATA_TRIAL_THINKING` 逃逸口
#: （enabled/disabled/none）留给换模型时用——none=不发该参数，是始终思考模型
#: （如 glm-5.3-flash）的兼容档。
TRIAL_PROVIDER = "trial"
TRIAL_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
TRIAL_DEFAULT_MODEL = "deepseek-v4-flash"
MAX_PROVIDER_ERROR_READ_BYTES = 8192
MAX_PROVIDER_ERROR_CHARS = 2048
MAX_PROVIDER_RESPONSE_BYTES = 8 * 1024 * 1024


@dataclass(slots=True)
class LLMConfig:
    enable_llm: bool = False
    mock_llm: bool = False
    provider: str = "openai-compatible"
    api_key: str | None = None
    base_url: str = OPENAI_DEFAULT_BASE_URL
    model: str = OPENAI_DEFAULT_MODEL
    timeout: float = 60.0
    temperature: float = 0.2
    # 8000 的出处：3000 对 deepseek 预设偏小——实测 6 次检索有 2 次回答被
    # finish_reason:length 截断而判失败（润色要输出整张推荐表，3000 不够写）。
    # 与 load_llm_config 的 LLM_MAX_TOKENS 兜底必须同值（生产路径恒经它），别留两个数。
    max_tokens: int = 8000
    # DeepSeek V4 思考模式旋钮：
    # None = 不发该参数（默认，行为与旧版逐位一致）；thinking=True → extra_body 注
    # {"thinking": {"type": "enabled"}}；reasoning_effort 仅在 thinking=True 时附加，
    # 合法枚举 none/minimal/low/medium/high/xhigh/max（2026-08-08 实测 400 报文列举）。
    # 注意：思考模式拒收 tool_choice="required"（服务端 400）——auto 档无此限制，
    # 本项目的 decide/repair 本来就恒 auto，understand 的 required 档只在非思考车道。
    thinking: bool | None = None
    reasoning_effort: str | None = None


@dataclass(slots=True)
class LLMResult:
    text: str | None
    attempted: bool
    succeeded: bool
    response_used: bool
    provider: str | None
    model: str | None
    error: str | None = None
    finish_reason: str | None = None
    raw_response: Any | None = None


def _env_first(*keys: str) -> str | None:
    for key in keys:
        value = os.getenv(key)
        if value is not None and value != "":
            return value
    return None


def _is_placeholder_secret(value: str | None) -> bool:
    if value is None:
        return True
    normalized = value.strip().lower()
    if not normalized:
        return True
    placeholders = {
        "your_api_key_here",
        "your_openai_api_key_here",
        "your_zhipuai_api_key_here",
        "your_token_here",
        "replace_me",
    }
    if normalized in placeholders:
        return True
    if "your_" in normalized and ("api_key" in normalized or "token" in normalized):
        return True
    return False


def _sanitized_api_key(*keys: str) -> str | None:
    value = _env_first(*keys)
    if _is_placeholder_secret(value):
        return None
    return value


def _sanitize_provider_error(detail: object, api_key: str | None = None) -> str:
    """Redact request credentials and bound untrusted provider error text."""
    text = str(detail or "")
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    text = re.sub(r"(?i)\bbearer\s+[^\s,;\"']+", "Bearer [REDACTED]", text)
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_PROVIDER_ERROR_CHARS:
        text = text[:MAX_PROVIDER_ERROR_CHARS].rstrip() + " …[truncated]"
    return text or "provider returned no error detail"


def _normalize_provider(provider: str | None, *, default: str = "openai-compatible",
                        extra_aliases: "frozenset[str] | set[str]" = frozenset()) -> str:
    """provider 名归一化。两种口径，由 extra_aliases 决定：

    - extra_aliases 为空（默认）：历史透传口径——内建别名集（智谱/mock/openai-compatible）
      之外的任何名字原样透传，由 call_llm 的 "unsupported provider" 分支如实拒绝。
      llm 包内全部调用点走此口径。
    - extra_aliases 非空：封闭集口径——不在内建别名也不在 extra_aliases 里的名字
      一律回落 default。webapp 设置页用此口径把用户输入收敛到已知通道集合
      （例如 `_normalize_provider(v, default="zhipuai", extra_aliases={"trial"})`），
      非法值静默回落是产品意图，不报错。
    """
    normalized = (provider or default).strip().lower()
    if normalized in ZHIPU_PROVIDER_ALIASES:
        return "zhipuai"
    if normalized == "mock":
        return "mock"
    if normalized in OPENAI_PROVIDER_ALIASES:
        return "openai-compatible"
    if extra_aliases:
        return normalized if normalized in extra_aliases else default
    return normalized


#: 用户可见归因（rerank/润色回退措辞）把 LLM 失败分两类的判据：
#: 「密钥无效/无权」（HTTP 401/403）——用户该去改设置；其余（超时/5xx/空回）是临时故障。
#: 错误串的产地就是本模块（f"LLM HTTPError {code}: …"），判据与产地同文件，不另抄第二份。
#: 只匹配串首 code 位（E-05）：服务商/网关正文回显 "HTTPError 401" 字样不得把 502/429
#: 误判成密钥无效。
def is_auth_error(error: object) -> bool:
    """LLM 调用错误是否是「密钥无效/没有权限」（HTTP 401/403）。

    401=未认证、403=无权限——重试不会自愈，唯一出路是用户去「设置」换密钥；
    超时/5xx/返回空是临时故障，稍后重试即可。两句话必须分开（2026-08-04 C3）。
    """
    return re.match(r"LLM HTTPError 40[13]\b", str(error or "")) is not None


def _build_chat_completion_url(base_url: str, default_base_url: str) -> str:
    candidate = (base_url or default_base_url).strip()
    if not candidate:
        candidate = default_base_url
    candidate = candidate.rstrip("/")
    if candidate.lower().endswith("/chat/completions"):
        return candidate
    return candidate + "/chat/completions"


def _proxy_env_value(name: str) -> str | None:
    return os.getenv(name) or os.getenv(name.lower())


def _proxy_env_detected(name: str) -> str:
    return "detected" if _proxy_env_value(name) else "missing"


def _build_proxy_handler() -> urllib.request.ProxyHandler:
    proxies: dict[str, str] = {}
    for scheme, env_name in [("http", "HTTP_PROXY"), ("https", "HTTPS_PROXY"), ("all", "ALL_PROXY")]:
        value = _proxy_env_value(env_name)
        if value:
            proxies[scheme] = value
    if proxies:
        return urllib.request.ProxyHandler(proxies)
    # Keep compatibility with default urllib env-based behavior.
    return urllib.request.ProxyHandler()


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep a validated endpoint from redirecting requests to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def _open_request(request: urllib.request.Request, timeout: float):
    opener = urllib.request.build_opener(_build_proxy_handler(), _NoRedirectHandler())
    return opener.open(request, timeout=timeout)


def _provider_api_key(provider: str) -> str | None:
    """当前 provider 应读哪组 key 变量的单一真源（placeholder 已被 _sanitized_api_key 脱敏为 None）。

    load_llm_config 的 api_key 取值与 resolve_enable_llm 的「有 key 默认开」判定同走这里，
    两处永不漂移；新增 provider 时同步补本函数与 load_llm_config 的 base_url/model 分支。
    """
    if provider == "zhipuai":
        return _sanitized_api_key("LLM_API_KEY", "ZAI_API_KEY", "ZHIPUAI_API_KEY", "ZHIPUAI_TOKEN")
    if provider == TRIAL_PROVIDER:
        # 试用通道只认部署方专用变量，绝不回落 LLM_API_KEY/OPENAI_API_KEY——
        # 否则请求级 _temporary_env 注入的 key 会静默变成试用通道的凭据。
        # 也不回落 BIODATA_EMBED_API_KEY：那是智谱 key，对默认 DeepSeek 端点无效
        # （2026-08-27 换型 GLM 时曾短暂回落共用，2026-09-01 改回 DeepSeek 时移除）。
        return _sanitized_api_key("BIODATA_TRIAL_API_KEY")
    if provider == "mock":
        return _sanitized_api_key("LLM_API_KEY", "OPENAI_API_KEY", "ZAI_API_KEY", "ZHIPUAI_API_KEY", "ZHIPUAI_TOKEN")
    return _sanitized_api_key("LLM_API_KEY", "OPENAI_API_KEY")


def resolve_enable_llm(provider: str | None = None) -> bool:
    """ENABLE_LLM 的有效开关（** · 产品方 2026-07-19 决策**：「如果填了apikey就默认开启，否则默认关闭」）。

    - 显式设为 true/false → 逐字服从（显式优先，行为与旧版一致）。
    - 未设置 → 默认 = 当前（或指定）provider 下存在真实（非 placeholder）API key：
      有 key 默认开、无 key 默认关。空串按「未设置」处理（与 _env_first 既有口径一致）。
    本函数是该默认值的**单一真源**：load_llm_config 与 config.get_settings 共用，防两处接线漂移。
    只读 os.environ，不碰文件——调用方（load_llm_config / get_settings）都已先 load_env_candidates。
    """
    explicit = _env_first("ENABLE_LLM")
    if explicit is not None:
        return _parse_bool(explicit, default=False)
    return _provider_api_key(_normalize_provider(provider)) is not None


def load_llm_config(
    project_root: Path | None = None,
    provider_override: str | None = None,
) -> LLMConfig:
    root = project_root or Path(__file__).resolve().parents[3]
    load_env_candidates(root)

    provider = _normalize_provider(provider_override or _env_first("LLM_PROVIDER"))
    api_key = _provider_api_key(provider)

    if provider == "zhipuai":
        base_url = _env_first("LLM_BASE_URL", "ZHIPUAI_BASE_URL", "BIGMODEL_BASE_URL") or ZHIPU_DEFAULT_BASE_URL
        model = _env_first("LLM_MODEL", "ZHIPUAI_MODEL", "BIGMODEL_MODEL") or ZHIPU_DEFAULT_MODEL
    elif provider == TRIAL_PROVIDER:
        # 试用通道端点/模型锁定：只读部署方专用变量（缺省官方值），请求级
        # LLM_BASE_URL/LLM_MODEL 注入一律不适用（webapp `_build_request_overrides`
        # 对 trial 不注入这两项，双保险）。
        base_url = _env_first("BIODATA_TRIAL_BASE_URL") or TRIAL_DEFAULT_BASE_URL
        model = _env_first("BIODATA_TRIAL_MODEL") or TRIAL_DEFAULT_MODEL
    elif provider == "mock":
        base_url = _env_first("LLM_BASE_URL", "OPENAI_BASE_URL") or OPENAI_DEFAULT_BASE_URL
        model = _env_first("LLM_MODEL", "OPENAI_MODEL") or OPENAI_DEFAULT_MODEL
    else:
        base_url = _env_first("LLM_BASE_URL", "OPENAI_BASE_URL") or OPENAI_DEFAULT_BASE_URL
        model = _env_first("LLM_MODEL", "OPENAI_MODEL") or OPENAI_DEFAULT_MODEL

    return LLMConfig(
        enable_llm=resolve_enable_llm(provider),
        mock_llm=_parse_bool(_env_first("MOCK_LLM"), default=False),
        provider=provider,
        api_key=api_key,
        base_url=base_url.strip(),
        model=model.strip(),
        timeout=float(_env_first("LLM_TIMEOUT", "LLM_TIMEOUT_SECONDS") or "60"),
        temperature=float(_env_first("LLM_TEMPERATURE") or "0.2"),
        max_tokens=int(_env_first("LLM_MAX_TOKENS") or "8000"),   # 与 LLMConfig 字段默认同值（8000 的出处见字段注释）
        # trial 默认关思考档（False → 发 {"type":"disabled"}）：deepseek-v4-flash
        # 默认思考档拒收 tool_choice="required"（2026-08-25 实测 400）且更慢。
        # `BIODATA_TRIAL_THINKING=enabled|disabled|none` 是换模型时的逃逸口
        # （none=不发该参数，始终思考的模型用）——见 TRIAL_PROVIDER 头注释。
        thinking=_trial_thinking_env() if provider == TRIAL_PROVIDER else None,
    )


def _trial_thinking_env() -> bool | None:
    """试用通道思考旋钮（`BIODATA_TRIAL_THINKING`，2026-08-27 引入，2026-09-01 默认翻回）。

    enabled/on/1/true/yes → True（发 {"type":"enabled"}）；none/omit → None
    （**不发** thinking 参数——始终思考的模型如 glm-5.3-flash 的唯一合法形态，
    它拒收 disabled，2026-08-27 实测 400）；disabled/off/0/false/no 与未设/其他
    → False（发 disabled；默认模型 deepseek-v4-flash 思考档拒收 tool_choice 且更慢）。"""
    raw = (_env_first("BIODATA_TRIAL_THINKING") or "").strip().lower()
    if raw in ("1", "on", "true", "yes", "enabled"):
        return True
    if raw in ("none", "omit"):
        return None
    return False


def complex_model_name() -> str:
    """复杂度路由的 decide 档模型名（`LLM_MODEL_COMPLEX` 环境变量，2026-08-07）。

    未配置 → 空串（路由关闭，agent 行为与单模型逐位一致）。本函数只在
    `load_llm_config` 跑过之后被调（`load_env_candidates` 的 setdefault 已把 .env
    灌进 os.environ），因此直接读环境、不再重复加载。"""
    return str(os.environ.get("LLM_MODEL_COMPLEX") or "").strip()


def _safe_cell(value: str) -> str:
    return (value or "").replace("|", r"\|").strip()


def _sample_size(count: str, unit: str) -> str:
    # DRY：委托 units（全项目规范）。仅 count 分支由旧「{count} 未说明单位」统一为「{count}」（有意统一）。
    return format_sample_size((count or "").strip(), (unit or "").strip())


def _raw_status(value: bool | None) -> str:
    return raw_fastq_status(value)


def _unit_explanation(units: set[str]) -> str:
    lines: list[str] = []
    if "cells" in units:
        lines.append(prompts.UNIT_EXPLANATION_CELLS_ZH)
    if "spots" in units:
        lines.append(prompts.UNIT_EXPLANATION_SPOTS_ZH)
    if "nuclei" in units:
        lines.append(prompts.UNIT_EXPLANATION_NUCLEI_ZH)
    return "\n".join(lines)


def _rows_from_retrieved_records(retrieved_records: list[dict[str, Any]]) -> tuple[list[str], bool, set[str]]:
    rows: list[str] = []
    has_false_fastq = False
    units: set[str] = set()

    for item in retrieved_records:
        dataset_name = _safe_cell(str(item.get("dataset_name") or "")) or "-"
        species = _safe_cell(str(item.get("species") or "")) or "-"
        tissue = _safe_cell(str(item.get("tissue") or "")) or "-"
        disease = _safe_cell(str(item.get("disease") or "")) or "-"
        chemistry = _safe_cell(str(item.get("chemistry") or "")) or "-"
        count = str(item.get("count") or "")
        unit = str(item.get("unit") or "")
        if unit:
            units.add(unit.strip().lower())
        sample_size = _safe_cell(_sample_size(count, unit))

        raw_state = item.get("has_raw_data")
        if isinstance(raw_state, str):
            lowered = raw_state.strip().lower()
            if lowered in {"true", "1", "yes", "y"}:
                raw_state = True
            elif lowered in {"false", "0", "no", "n"}:
                raw_state = False
            else:
                raw_state = None
        raw_text = _raw_status(raw_state if isinstance(raw_state, bool) else None)
        if raw_text == RAW_FASTQ_NO:
            has_false_fastq = True

        url = str(item.get("url") or "").strip()
        link = f"[点击下载]({url})" if url else "-"
        rows.append(
            f"| {dataset_name} | {species} | {tissue} | {disease} | {chemistry} | {sample_size} | {raw_text} | {link} |"
        )

    return rows, has_false_fastq, units


def call_mock_llm(prompt: str, retrieved_records: list[dict[str, Any]]) -> LLMResult:
    _ = prompt
    try:
        # 与 curator prompt 的 max_rows 上限（min(候选数, 10)）同口径：演示表格同样至多 10 行，
        # 不因 top_k 放宽（le=50）吐几十行把演示界面刷成一屏。
        rows, has_false_fastq, units = _rows_from_retrieved_records(retrieved_records[:10])
        if not rows:
            return LLMResult(
                text="",
                attempted=True,
                succeeded=False,
                response_used=False,
                provider="mock",
                model="mock-curator",
                error="no retrieved records",
            )

        blocks = ["\n".join([TABLE_HEADER, TABLE_SEPARATOR, *rows])]
        if has_false_fastq:
            blocks.append(f"**{prompts.NO_FASTQ_NOTICE_ZH}**")
        unit_text = _unit_explanation(units)
        if unit_text:
            blocks.append(unit_text)

        return LLMResult(
            text="\n\n".join(blocks),
            attempted=True,
            succeeded=True,
            response_used=False,
            provider="mock",
            model="mock-curator",
            error=None,
            finish_reason="stop",
        )
    except Exception as exc:  # pragma: no cover
        return LLMResult(
            text=None,
            attempted=True,
            succeeded=False,
            response_used=False,
            provider="mock",
            model="mock-curator",
            error=str(exc),
        )


def _call_chat_completions(
    prompt: str,
    config: LLMConfig,
    provider_name: str,
    default_base_url: str,
) -> LLMResult:
    url = _build_chat_completion_url(config.base_url, default_base_url)
    payload = {
        "model": config.model,
        "messages": [
            # 铁律**写进 user prompt**（不是 system slot）：这里的 system 消息是写死的通用策展人设、
            # 会覆盖任何自定义 system——把护栏放 system 就成了不会发出去的死代码。放进确定发送的
            # user prompt 才真正接地（act_summary_llm / intro_llm / search_reply_llm 同径、同一个教训）。
            {"role": "system", "content": prompts.CURATOR_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }
    # 思考旋钮与 agent_exec 的 langchain 路径同语义：
    # None = 不发该参数（全部既有 provider 行为逐位不变）；trial 默认 False →
    # {"thinking": {"type": "disabled"}}——v4-flash 默认思考档拒收 tool_choice 且更慢，
    # 详见 TRIAL_PROVIDER 头注释。
    if config.thinking is not None:
        payload["thinking"] = {"type": "enabled" if config.thinking else "disabled"}
        if config.thinking and config.reasoning_effort:
            payload["reasoning_effort"] = config.reasoning_effort

    request = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with _open_request(request, timeout=config.timeout) as response:
            response_bytes = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
            if len(response_bytes) > MAX_PROVIDER_RESPONSE_BYTES:
                return LLMResult(
                    text=None,
                    attempted=True,
                    succeeded=False,
                    response_used=False,
                    provider=provider_name,
                    model=config.model,
                    error="LLM response exceeded the 8 MiB safety limit",
                )
            response_text = response_bytes.decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read(MAX_PROVIDER_ERROR_READ_BYTES + 1).decode(
            "utf-8", errors="ignore"
        )
        return LLMResult(
            text=None,
            attempted=True,
            succeeded=False,
            response_used=False,
            provider=provider_name,
            model=config.model,
            error=f"LLM HTTPError {exc.code}: {_sanitize_provider_error(detail, config.api_key)}",
        )
    except urllib.error.URLError as exc:
        return LLMResult(
            text=None,
            attempted=True,
            succeeded=False,
            response_used=False,
            provider=provider_name,
            model=config.model,
            error=f"LLM URL error: {_sanitize_provider_error(exc, config.api_key)}",
        )
    except Exception as exc:  # pragma: no cover
        return LLMResult(
            text=None,
            attempted=True,
            succeeded=False,
            response_used=False,
            provider=provider_name,
            model=config.model,
            error=f"LLM unexpected error: {_sanitize_provider_error(exc, config.api_key)}",
        )

    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError as exc:
        return LLMResult(
            text=None,
            attempted=True,
            succeeded=False,
            response_used=False,
            provider=provider_name,
            model=config.model,
            error=f"LLM returned non-JSON response: {exc}",
        )

    choices = parsed.get("choices") if isinstance(parsed, dict) else None
    if not isinstance(choices, list) or not choices:
        return LLMResult(
            text=None,
            attempted=True,
            succeeded=False,
            response_used=False,
            provider=provider_name,
            model=config.model,
            error="LLM response missing choices[0].message.content",
            raw_response=parsed,
        )

    first = choices[0] if isinstance(choices[0], dict) else {}
    finish_reason = first.get("finish_reason") if isinstance(first, dict) else None
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        return LLMResult(
            text=None,
            attempted=True,
            succeeded=False,
            response_used=False,
            provider=provider_name,
            model=config.model,
            error="LLM response content is empty or invalid",
            finish_reason=str(finish_reason) if finish_reason is not None else None,
            raw_response=parsed,
        )

    return LLMResult(
        text=content.strip(),
        attempted=True,
        succeeded=True,
        response_used=False,
        provider=provider_name,
        model=config.model,
        error=None,
        finish_reason=str(finish_reason) if finish_reason is not None else None,
        raw_response=parsed,
    )


def call_openai_compatible(prompt: str, config: LLMConfig) -> LLMResult:
    return _call_chat_completions(
        prompt=prompt,
        config=config,
        provider_name="openai-compatible",
        default_base_url=OPENAI_DEFAULT_BASE_URL,
    )


def call_zhipuai(prompt: str, config: LLMConfig) -> LLMResult:
    return _call_chat_completions(
        prompt=prompt,
        config=config,
        provider_name="zhipuai",
        default_base_url=ZHIPU_DEFAULT_BASE_URL,
    )


def should_use_llm(config: LLMConfig, *, pre=None, post=None, require_enabled=True) -> "tuple[bool, str]":
    """是否调用**真** LLM 的公共闸（act/intro/action_plan/dream 四处共用）。返回 (是否, 原因短标签)。

    判定序：pre(config)（返回非 None 即作为裁决，用于 action_plan 的在线成本闸这类前置条件）→
    mock（mock_llm 或 provider=="mock" 一律判否——call_mock_llm 忽略 prompt、直吐 curator
    markdown 表，让执行/总结/介绍层走它会「荒谬通过」：产出根本不是预期文本）→
    disabled（require_enabled=False 的消费方跳过本档——dream 只认 key 不认 enable 开关）→
    no_key → post(config)（返回非 None 即作为裁决，用于 intro 的体裁可译性这类附加条件）→ ready。
    """
    if pre is not None:
        verdict = pre(config)
        if verdict is not None:
            return verdict
    if config.mock_llm or _normalize_provider(config.provider) == "mock":
        return False, "mock_not_used"
    if require_enabled and not config.enable_llm:
        return False, "disabled"
    if not config.api_key:
        return False, "no_key"
    if post is not None:
        verdict = post(config)
        if verdict is not None:
            return verdict
    return True, "ready"


def call_llm(prompt: str, config: LLMConfig, retrieved_records: list[dict[str, Any]] | None = None) -> LLMResult:
    provider = _normalize_provider(config.provider)

    if config.mock_llm or provider == "mock":
        return call_mock_llm(prompt=prompt, retrieved_records=retrieved_records or [])

    if not config.enable_llm:
        return LLMResult(
            text=None,
            attempted=False,
            succeeded=False,
            response_used=False,
            provider=provider,
            model=config.model,
            error="LLM disabled",
        )

    if not config.api_key:
        if provider == "zhipuai":
            missing_key_error = "missing ZhipuAI API key"
        elif provider == TRIAL_PROVIDER:
            missing_key_error = "trial channel not configured (missing BIODATA_TRIAL_API_KEY)"
        else:
            missing_key_error = "missing OPENAI_API_KEY"
        return LLMResult(
            text=None,
            attempted=False,
            succeeded=False,
            response_used=False,
            provider=provider,
            model=config.model,
            error=missing_key_error,
        )

    if provider == "zhipuai":
        return call_zhipuai(prompt, config)

    if provider == TRIAL_PROVIDER:
        # 试用通道走 OpenAI 兼容协议（DeepSeek 官方端点），但归因如实标 trial——
        # trace/遥测里试用与 BYOK 的 openai-compatible 必须可分。
        return _call_chat_completions(
            prompt=prompt,
            config=config,
            provider_name=TRIAL_PROVIDER,
            default_base_url=TRIAL_DEFAULT_BASE_URL,
        )

    if provider == "openai-compatible":
        return call_openai_compatible(prompt, config)

    return LLMResult(
        text=None,
        attempted=False,
        succeeded=False,
        response_used=False,
        provider=provider,
        model=config.model,
        error=f"unsupported provider: {provider}",
    )


def diagnose_network(config: LLMConfig) -> dict[str, str]:
    provider = _normalize_provider(config.provider)
    if provider == "mock" or config.mock_llm:
        return {
            "Provider": "mock",
            "Target host": "not applicable",
            "Target port": "not applicable",
            "Base URL": "not applicable",
            "Chat completions URL": "not applicable",
            "API key": "not used",
            "Model": "mock-curator",
            "Timeout": str(config.timeout),
            "DNS": "skipped",
            "TCP 443": "skipped",
            "HTTPS": "skipped",
            "Proxy detected": "not checked",
            "HTTP_PROXY": "not checked",
            "HTTPS_PROXY": "not checked",
            "ALL_PROXY": "not checked",
            "NO_PROXY": "not checked",
            "API request": "skipped",
            "Error": "none (mock provider is offline)",
        }
    if _parse_bool(os.getenv("BIODATA_CI_OFFLINE"), default=False):
        return {
            "Provider": provider,
            "Target host": "not resolved in offline quality mode",
            "Target port": "not opened in offline quality mode",
            "Base URL": config.base_url,
            "Chat completions URL": _build_chat_completion_url(
                config.base_url,
                ZHIPU_DEFAULT_BASE_URL if provider == "zhipuai" else OPENAI_DEFAULT_BASE_URL,
            ),
            "API key": "detected" if config.api_key else "missing",
            "Model": config.model,
            "Timeout": str(config.timeout),
            "DNS": "skipped",
            "TCP 443": "skipped",
            "HTTPS": "skipped",
            "Proxy detected": "not checked",
            "HTTP_PROXY": "not checked",
            "HTTPS_PROXY": "not checked",
            "ALL_PROXY": "not checked",
            "NO_PROXY": "not checked",
            "API request": "skipped",
            "Error": "none (BIODATA_CI_OFFLINE=1)",
        }
    if provider == "zhipuai":
        base_url = config.base_url or ZHIPU_DEFAULT_BASE_URL
        default_base_url = ZHIPU_DEFAULT_BASE_URL
        target_host = "open.bigmodel.cn"
    else:
        base_url = config.base_url or OPENAI_DEFAULT_BASE_URL
        default_base_url = OPENAI_DEFAULT_BASE_URL
        parsed = urllib.parse.urlparse(base_url)
        target_host = parsed.hostname or "api.openai.com"

    chat_url = _build_chat_completion_url(base_url, default_base_url)
    parsed_chat = urllib.parse.urlparse(chat_url)
    host = parsed_chat.hostname or target_host
    port = parsed_chat.port or (443 if (parsed_chat.scheme or "https") == "https" else 80)

    report: dict[str, str] = {
        "Provider": provider,
        "Target host": host,
        "Target port": str(port),
        "Base URL": base_url,
        "Chat completions URL": chat_url,
        "API key": "detected" if config.api_key else "missing",
        "Model": config.model,
        "Timeout": str(config.timeout),
        "DNS": "failed",
        "TCP 443": "failed",
        "HTTPS": "failed",
        "Proxy detected": "yes" if any(_proxy_env_value(name) for name in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"]) else "no",
        "HTTP_PROXY": _proxy_env_detected("HTTP_PROXY"),
        "HTTPS_PROXY": _proxy_env_detected("HTTPS_PROXY"),
        "ALL_PROXY": _proxy_env_detected("ALL_PROXY"),
        "NO_PROXY": _proxy_env_detected("NO_PROXY"),
        "API request": "skipped",
        "Error": "none",
    }

    # 1) DNS
    try:
        socket.gethostbyname(host)
        report["DNS"] = "success"
    except Exception as exc:
        report["Error"] = f"DNS error: {_sanitize_provider_error(exc, config.api_key)}"
        return report

    # 2) TCP
    try:
        with socket.create_connection((host, port), timeout=config.timeout):
            pass
        report["TCP 443"] = "success" if port == 443 else f"success (port {port})"
    except Exception as exc:
        report["Error"] = f"TCP error: {_sanitize_provider_error(exc, config.api_key)}"
        return report

    # 3) HTTPS connectivity
    try:
        https_request = urllib.request.Request(base_url, method="GET")
        with _open_request(https_request, timeout=config.timeout) as _response:
            pass
        report["HTTPS"] = "success"
    except urllib.error.HTTPError as exc:
        # HTTP error still means HTTPS handshake and transport reached server.
        report["HTTPS"] = "success"
    except Exception as exc:
        report["Error"] = f"HTTPS error: {_sanitize_provider_error(exc, config.api_key)}"
        return report

    # 4) API request
    if not config.api_key:
        report["API request"] = "skipped"
        report["Error"] = "missing ZhipuAI API key" if provider == "zhipuai" else "missing OPENAI_API_KEY"
        return report

    test_payload = {
        "model": config.model,
        "messages": [{"role": "user", "content": "ping"}],
        "temperature": 0.2,
        "max_tokens": 10,
    }
    api_request = urllib.request.Request(
        url=chat_url,
        data=json.dumps(test_payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with _open_request(api_request, timeout=config.timeout) as response:
            _ = response.read(1)
        report["API request"] = "success"
        report["Error"] = "none"
    except urllib.error.HTTPError as exc:
        detail = exc.read(MAX_PROVIDER_ERROR_READ_BYTES + 1).decode(
            "utf-8", errors="ignore"
        )
        report["API request"] = "failed"
        report["Error"] = (
            f"API HTTPError {exc.code}: {_sanitize_provider_error(detail, config.api_key)}"
        )
    except Exception as exc:
        report["API request"] = "failed"
        report["Error"] = (
            f"API request error: {_sanitize_provider_error(exc, config.api_key)}"
        )

    return report


def healthcheck(config: LLMConfig, mock_override: bool | None = None) -> dict[str, str]:
    effective = LLMConfig(**asdict(config))
    effective.provider = _normalize_provider(effective.provider)
    if mock_override is not None:
        effective.mock_llm = mock_override

    zai_key_detected = "detected" if _sanitized_api_key("ZAI_API_KEY") else "missing"
    zhipuai_key_detected = "detected" if _sanitized_api_key("ZHIPUAI_API_KEY") else "missing"
    zhipu_token_detected = "detected" if _sanitized_api_key("ZHIPUAI_TOKEN") else "missing"
    openai_key_detected = "detected" if _sanitized_api_key("OPENAI_API_KEY") else "missing"

    result: dict[str, str] = {
        "ENABLE_LLM": "true" if effective.enable_llm else "false",
        "MOCK_LLM": "true" if effective.mock_llm else "false",
        "LLM_PROVIDER": effective.provider,
        "LLM_TIMEOUT": str(effective.timeout),
        "Connection": "skipped",
        "Error": "none",
    }

    if effective.provider == "zhipuai":
        result["ZHIPUAI_BASE_URL"] = effective.base_url
        result["ZHIPUAI_MODEL"] = effective.model
        result["ZAI_API_KEY"] = zai_key_detected
        result["ZHIPUAI_API_KEY"] = zhipuai_key_detected
        result["ZHIPUAI_TOKEN"] = zhipu_token_detected
        result["Effective API key"] = "detected" if effective.api_key else "missing"
    else:
        result["OPENAI_BASE_URL"] = effective.base_url
        result["OPENAI_MODEL"] = effective.model
        result["OPENAI_API_KEY"] = openai_key_detected
        result["Effective API key"] = "detected" if effective.api_key else "missing"

    if effective.mock_llm or effective.provider == "mock":
        result["Connection"] = "skipped"
        result["Error"] = "none"
        return result

    if not effective.enable_llm:
        result["Connection"] = "skipped"
        result["Error"] = "ENABLE_LLM=false"
        result["Hint"] = "run --network-diagnose --provider zhipuai for DNS/TCP/HTTPS/API diagnostics"
        return result

    if not effective.api_key:
        result["Connection"] = "skipped"
        result["Error"] = "missing ZhipuAI API key" if effective.provider == "zhipuai" else "missing OPENAI_API_KEY"
        result["Hint"] = "run --network-diagnose --provider zhipuai for DNS/TCP/HTTPS/API diagnostics"
        return result

    probe_prompt = "Reply with exactly one word: OK"
    if effective.provider == "zhipuai":
        probe_result = call_zhipuai(probe_prompt, effective)
    else:
        probe_result = call_openai_compatible(probe_prompt, effective)

    if probe_result.succeeded:
        result["Connection"] = "success"
        result["Error"] = "none"
    else:
        result["Connection"] = "failed"
        result["Error"] = probe_result.error or "unknown error"
        result["Hint"] = "run --network-diagnose --provider zhipuai for DNS/TCP/HTTPS/API diagnostics"

    return result
