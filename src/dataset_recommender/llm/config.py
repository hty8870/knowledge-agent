from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - fallback when dependency is unavailable
    def load_dotenv(*args: object, **kwargs: object) -> bool:  # type: ignore[misc]
        return False

from ..app.runtime_paths import get_app_paths


EXTERNAL_LLM_ENV_VAR = "BIODATA_LLM_ENV_FILE"

logger = logging.getLogger(__name__)


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_int_env(name: str, default: int) -> int:
    """读整型环境变量的容错版。

    背景：`TOP_K`/`PORT` 是**无前缀的通用变量名**，陌生机器上其他软件的 ambient 残留值
    可能非数字——裸 `int()` 会让服务启动即 ValueError 崩溃（实测复现）。这里非数字/空值
    一律回落默认并 warning 点名（不静默：静默吞配置错误与崩溃一样难排查，但至少能起服）。
    合法数字（含负数，语义由调用方约束）逐位透传，行为与历史一致。
    """
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        logger.warning("环境变量 %s=%r 不是整数，回落默认 %s（可能是其他软件的同名残留值）",
                       name, str(raw)[:40], default)
        return default


_AMBIENT_GENERIC_ENV_EFFECTS: "list[tuple[str, str]]" = [
    # 五个无前缀通用名在陌生机器上可能被其他软件残留劫持，每个都说清「设了会怎样」。
    # 只警告不改行为——DATA_DIR 覆盖是文档化历史口径（frozen 布局测试在用），语义保留。
    ("DATA_DIR", "覆盖基础语料目录（相对 project_root 解析）——若非本意，检索将读到错误/空语料"),
    ("TOP_K", "覆盖默认返回条数"),
    ("MOCK_LLM", "真值时 LLM 降级为 mock 输出"),
    ("ENABLE_LLM", "显式开关 LLM 润色（优先于「有 key 默认开」的产品口径）"),
    ("KEYWORD_MAPPING_PATH", "合并额外关键词映射文件"),
]


def _warn_ambient_generic_env() -> None:
    """get_settings 内调用（lru_cache → 每进程恰一次）：点名被 ambient 设置的通用名变量。

    这些名字不带 BIODATA_ 前缀、在别的软件里也常见（DATA_DIR 尤甚）；陌生环境下被残留
    劫持的失败形态要么阴森（DATA_DIR→静默 0 语料）要么直接（TOP_K=abc→曾崩溃）。警告不
    打值（可能含路径/敏感内容），只打名与效果。
    """
    for name, effect in _AMBIENT_GENERIC_ENV_EFFECTS:
        if (os.getenv(name) or "").strip():
            logger.warning("检测到环境变量 %s 已设置：%s。若非本产品有意配置，请检查是否为其他软件残留。",
                           name, effect)


def load_env_file(env_path: Path) -> None:
    """Load a simple .env file into process env if keys are missing."""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def external_llm_env_status() -> dict[str, Any]:
    """Describe the optional project-external LLM env file without exposing its path or values."""
    raw_path = (os.getenv(EXTERNAL_LLM_ENV_VAR) or "").strip()
    if not raw_path:
        return {
            "configured": False,
            "absolute": False,
            "exists": False,
            "readable": False,
        }

    path = Path(raw_path).expanduser()
    absolute = path.is_absolute()
    exists = absolute and path.is_file()
    readable = False
    if exists:
        try:
            with path.open("r", encoding="utf-8"):
                readable = True
        except OSError:
            readable = False
    return {
        "configured": True,
        "absolute": absolute,
        "exists": exists,
        "readable": readable,
    }


def load_env_candidates(project_root: Path) -> Path | None:
    # `project_root` 语义 = LLM env 候选根（`config_root`）：source/portable = 项目根
    # （.env 在项目根，兼容现状）；frozen = data_root/config（.env 随实例数据走）。
    # 调用方（webapp/cli/mcp）统一传 `get_app_paths().config_root`；本函数不自行解析根。
    # Client/process env has highest priority because every file loader uses setdefault.
    # The project-external file is loaded before project-local fallbacks, so 实现 and
    # 实现 Code can share one secret file without storing the key in either client config.
    # 所有存在的候选**都加载**（setdefault 语义下排前者优先，
    # 既有优先级不变）——此前在第一个存在的文件处即 return，.env 与 .env.zhipu 并存时
    # 后者静默失效且零日志。返回第一个被加载的文件（供既有调用点/测试断言）。
    first_loaded: Path | None = None
    external_raw = (os.getenv(EXTERNAL_LLM_ENV_VAR) or "").strip()
    if external_raw:
        external = Path(external_raw).expanduser()
        if external.is_absolute() and external.is_file():
            try:
                load_dotenv(external, override=False, encoding="utf-8")
            except Exception:
                pass
            load_env_file(external)
            first_loaded = external
            # 只记来源变量名，不记路径/键值（与 external_llm_env_status 的脱敏口径一致）。
            logger.info("loaded external LLM env file (%s)", EXTERNAL_LLM_ENV_VAR)

    candidates = [
        project_root / ".env",
        project_root / ".env.zhipu",
    ]
    for candidate in candidates:
        if candidate.exists():
            try:
                load_dotenv(candidate, override=False, encoding="utf-8")
            except Exception:
                pass
            load_env_file(candidate)
            if first_loaded is None:
                first_loaded = candidate
            logger.info("loaded LLM env file: %s", candidate.name)
    return first_loaded


# 受控词表已迁移到 vocabulary.CATALOG（单一真源）。
# 这里保留一个空的默认映射：仅在需要通过 KEYWORD_MAPPING_PATH 注入额外条目时作为合并基底。
# parse_query 会把此处的 override 合并进 vocabulary.CATALOG。
DEFAULT_KEYWORD_MAPPING: dict[str, list[dict[str, Any]]] = {}


@dataclass(slots=True)
class Settings:
    project_root: Path
    data_dir: Path
    output_dir: Path
    top_k: int
    enable_llm: bool
    mock_llm: bool
    keyword_mapping: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


# LLM 连接配置（provider / api_key / base_url / model / timeout / temperature / max_tokens）的
# 单一真源是 llm_client.load_llm_config —— 它做了 placeholder-key 脱敏与 mock 分支。Settings 只保留
# 全流程共享的运行参数（top_k、enable_llm、mock_llm、keyword_mapping）；不要在此重建 LLM 明细字段
# （历史上曾有一套死字段，未脱敏 placeholder key，已删除）。


def _load_keyword_mapping_override(path_str: str | None) -> dict[str, list[dict[str, Any]]]:
    if not path_str:
        return {}
    path = Path(path_str)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _merge_keyword_mapping(
    default_mapping: dict[str, list[dict[str, Any]]],
    override_mapping: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    merged: dict[str, list[dict[str, Any]]] = {
        key: list(value) for key, value in default_mapping.items()
    }
    for key, entries in override_mapping.items():
        if not isinstance(entries, list):
            continue
        merged.setdefault(key, [])
        merged[key].extend(entry for entry in entries if isinstance(entry, dict))
    return merged


def _default_enable_llm() -> bool:
    """ENABLE_LLM 的有效开关（**产品决策**：有 key 默认开 / 无 key 默认关；显式设置优先）。

    判定逻辑的单一真源是 `llm_client.resolve_enable_llm`（两处接线点共用、防漂移）；此处惰性 import
    以打破循环依赖（llm_client 顶层即 `from .config import load_env_candidates`）。本函数只读
    os.environ——调用点 get_settings 已先 load_env_candidates。
    """
    from .llm_client import resolve_enable_llm

    return resolve_enable_llm()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    paths = get_app_paths()
    # project_root = 实例数据根（source/portable = 项目根，历史逐字节一致；frozen = data_root）。
    # config_root 是 LLM env 候选根（frozen = data_root/config；source = 项目根/.env 不变）。
    project_root = paths.data_root
    load_env_candidates(paths.config_root)

    # DATA_DIR 是既有更具体覆盖（优先于 AppPaths）：显式设置时按历史口径相对
    # project_root 解析；未设置时 frozen 用随包冻结基准（shipped_base_dir）、
    # source/portable 用项目根 database/base（与历史默认逐字节一致）。
    data_dir_name = os.getenv("DATA_DIR", "").strip()
    data_dir = paths.shipped_base_dir if not data_dir_name else Path(project_root) / data_dir_name
    # 默认返回条数 5→10（与前端 cfgTopK 默认值、launch_web.ps1 同源联动；
    # 冻结评测自带显式 top_k=5，口径不受本默认值影响）。（容错解析：见 parse_int_env。）
    top_k = parse_int_env("TOP_K", 10)
    enable_llm = _default_enable_llm()
    mock_llm = _parse_bool(os.getenv("MOCK_LLM"), default=False)
    # 通用名 ambient 劫持点名（每进程一次，不打值）。
    _warn_ambient_generic_env()

    override = _load_keyword_mapping_override(os.getenv("KEYWORD_MAPPING_PATH"))
    keyword_mapping = _merge_keyword_mapping(DEFAULT_KEYWORD_MAPPING, override)

    return Settings(
        project_root=project_root,
        data_dir=data_dir,
        output_dir=paths.export_root,
        top_k=top_k,
        enable_llm=enable_llm,
        mock_llm=mock_llm,
        keyword_mapping=keyword_mapping,
    )
