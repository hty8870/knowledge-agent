from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dataset_recommender.llm.config import external_llm_env_status, load_env_candidates
from dataset_recommender.llm.llm_client import load_llm_config


LLM_ENV_KEYS = (
    "BIODATA_LLM_ENV_FILE",
    "ENABLE_LLM",
    "MOCK_LLM",
    "LLM_PROVIDER",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "ZAI_API_KEY",
    "ZHIPUAI_API_KEY",
    "ZHIPUAI_TOKEN",
    "ZHIPUAI_BASE_URL",
    "ZHIPUAI_MODEL",
)


def _clean_llm_env(monkeypatch) -> None:
    for key in LLM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _isolate_llm_process_env(monkeypatch):
    """The production loader intentionally mutates os.environ; restore it after every test."""
    original = {key: os.environ.get(key) for key in LLM_ENV_KEYS}
    _clean_llm_env(monkeypatch)
    yield
    for key in LLM_ENV_KEYS:
        os.environ.pop(key, None)
        if original[key] is not None:
            os.environ[key] = original[key]


def test_external_llm_env_file_loads_before_project_env(tmp_path: Path, monkeypatch):
    external = tmp_path / "outside.env"
    external.write_text(
        "ENABLE_LLM=true\n"
        "LLM_PROVIDER=openai-compatible\n"
        "LLM_API_KEY=external-secret\n"
        "LLM_BASE_URL=https://gateway.example/v1\n"
        "LLM_MODEL=external-model\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(
        "LLM_API_KEY=project-secret\nLLM_MODEL=project-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BIODATA_LLM_ENV_FILE", str(external.resolve()))

    loaded = load_env_candidates(project)
    config = load_llm_config(project_root=project)

    assert loaded == external
    assert config.api_key == "external-secret"
    assert config.model == "external-model"
    assert config.enable_llm is True


def test_process_env_has_priority_over_external_file(tmp_path: Path, monkeypatch):
    external = tmp_path / "outside.env"
    external.write_text("LLM_API_KEY=file-secret\nLLM_MODEL=file-model\n", encoding="utf-8")
    monkeypatch.setenv("BIODATA_LLM_ENV_FILE", str(external.resolve()))
    monkeypatch.setenv("LLM_API_KEY", "process-secret")
    monkeypatch.setenv("LLM_MODEL", "process-model")

    config = load_llm_config(project_root=tmp_path)

    assert config.api_key == "process-secret"
    assert config.model == "process-model"


def test_external_canonical_values_beat_inherited_provider_aliases(tmp_path: Path, monkeypatch):
    """A global OPENAI_* alias must not silently replace the MCP-specific external config."""
    external = tmp_path / "outside.env"
    external.write_text(
        "LLM_PROVIDER=openai-compatible\n"
        "LLM_API_KEY=mcp-file-secret\n"
        "LLM_BASE_URL=https://mcp-gateway.example/v1\n"
        "LLM_MODEL=mcp-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BIODATA_LLM_ENV_FILE", str(external.resolve()))
    monkeypatch.setenv("OPENAI_API_KEY", "inherited-global-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://global.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "global-model")

    config = load_llm_config(project_root=tmp_path)

    assert config.api_key == "mcp-file-secret"
    assert config.base_url == "https://mcp-gateway.example/v1"
    assert config.model == "mcp-model"


def test_example_file_is_never_loaded_as_runtime_config(tmp_path: Path, monkeypatch):
    (tmp_path / ".env.example").write_text(
        "ENABLE_LLM=true\nLLM_API_KEY=not-a-real-runtime-secret\n",
        encoding="utf-8",
    )

    assert load_env_candidates(tmp_path) is None
    config = load_llm_config(project_root=tmp_path)
    assert config.api_key is None
    assert config.enable_llm is False


def test_env_and_env_zhipu_are_both_loaded(tmp_path: Path, monkeypatch):
    """.env 与 .env.zhipu 并存时两份**都加载**（此前第一个
    存在的文件处即 return，.env.zhipu 静默失效、零日志）。setdefault 语义下：只在
    .env.zhipu 里的键照常生效，冲突键取排前的 .env。"""
    (tmp_path / ".env").write_text("LLM_MODEL=dotenv-model\n", encoding="utf-8")
    (tmp_path / ".env.zhipu").write_text(
        "LLM_PROVIDER=zhipuai\n"
        "ZHIPUAI_API_KEY=test-fake-zhipu-key-not-a-secret\n"
        "LLM_MODEL=zhipu-model\n",
        encoding="utf-8",
    )

    loaded = load_env_candidates(tmp_path)
    config = load_llm_config(project_root=tmp_path)

    assert loaded == tmp_path / ".env"                       # 返回值仍是第一个被加载的文件
    assert config.provider == "zhipuai"                      # 只在 .env.zhipu 里的键生效
    assert config.api_key == "test-fake-zhipu-key-not-a-secret"
    assert config.model == "dotenv-model"                    # 冲突键：排前的 .env 优先


def test_external_env_status_is_boolean_only(tmp_path: Path, monkeypatch):
    secret = tmp_path / "secret.env"
    secret.write_text("LLM_API_KEY=secret\n", encoding="utf-8")
    monkeypatch.setenv("BIODATA_LLM_ENV_FILE", str(secret.resolve()))

    status = external_llm_env_status()

    assert status == {"configured": True, "absolute": True, "exists": True, "readable": True}
    assert str(secret) not in repr(status)


# ------------------------------------------------------------- ENABLE_LLM 未设置时，默认 = 当前 provider 有真实 key
# 产品决策：「如果填了apikey就默认开启，否则默认关闭」。判定单一真源 =
# llm_client.resolve_enable_llm（config.get_settings 同源复用）。以下全程 monkeypatch 环境变量：
# 不调真 LLM、不打印任何 key 值（测试值均为非秘密占位串）。


@pytest.fixture
def _fresh_settings_cache():
    """get_settings 带 lru_cache(maxsize=1)：每个用例前后清缓存，防跨用例串环境。"""
    from dataset_recommender.llm.config import get_settings

    get_settings.cache_clear()
    yield get_settings
    get_settings.cache_clear()


def test_default_on_when_real_key_present(monkeypatch, tmp_path):
    """无 ENABLE_LLM + 当前 provider 有真实 key → 默认开。"""
    monkeypatch.setenv("LLM_API_KEY", "test-fake-key-not-a-secret")
    config = load_llm_config(project_root=tmp_path)
    assert config.api_key is not None
    assert config.enable_llm is True


def test_default_off_when_no_key(tmp_path):
    """无 ENABLE_LLM + 无任何 key → 默认关（quality_gate 清空 key 后走的正是这条路径）。"""
    config = load_llm_config(project_root=tmp_path)
    assert config.api_key is None
    assert config.enable_llm is False


def test_default_off_when_placeholder_key(monkeypatch, tmp_path):
    """placeholder key 被脱敏成 None → 视同无 key → 默认关。"""
    monkeypatch.setenv("LLM_API_KEY", "your_api_key_here")
    config = load_llm_config(project_root=tmp_path)
    assert config.api_key is None
    assert config.enable_llm is False


def test_explicit_true_overrides_missing_key(monkeypatch, tmp_path):
    """显式 ENABLE_LLM=true 压过默认：无 key 也开（随后调用层如实报 missing key，既有行为不变）。"""
    monkeypatch.setenv("ENABLE_LLM", "true")
    config = load_llm_config(project_root=tmp_path)
    assert config.api_key is None
    assert config.enable_llm is True


def test_explicit_false_overrides_present_key(monkeypatch, tmp_path):
    """显式 ENABLE_LLM=false 压过默认：有 key 也关。"""
    monkeypatch.setenv("ENABLE_LLM", "false")
    monkeypatch.setenv("LLM_API_KEY", "test-fake-key-not-a-secret")
    config = load_llm_config(project_root=tmp_path)
    assert config.api_key is not None
    assert config.enable_llm is False


def test_default_follows_current_provider(monkeypatch, tmp_path):
    """「有 key」按**当前 provider** 判定：只配智谱 key 而 provider=openai-compatible → 默认关；
    切到 zhipuai → 默认开。"""
    monkeypatch.setenv("ZHIPUAI_API_KEY", "test-fake-zhipu-key-not-a-secret")
    openai_cfg = load_llm_config(project_root=tmp_path)
    assert openai_cfg.provider == "openai-compatible"
    assert openai_cfg.api_key is None
    assert openai_cfg.enable_llm is False

    monkeypatch.setenv("LLM_PROVIDER", "zhipuai")
    zhipu_cfg = load_llm_config(project_root=tmp_path)
    assert zhipu_cfg.provider == "zhipuai"
    assert zhipu_cfg.api_key is not None
    assert zhipu_cfg.enable_llm is True


def test_empty_enable_llm_value_treated_as_unset(monkeypatch, tmp_path):
    """ENABLE_LLM 为空串按「未设置」处理（与 _env_first 既有口径一致）→ 走 key 默认。"""
    monkeypatch.setenv("ENABLE_LLM", "")
    monkeypatch.setenv("LLM_API_KEY", "test-fake-key-not-a-secret")
    assert load_llm_config(project_root=tmp_path).enable_llm is True


def test_settings_and_llm_client_share_one_default(monkeypatch, tmp_path, _fresh_settings_cache):
    """两处接线点语义一致：config.get_settings 与 llm_client.load_llm_config 同环境、同结论。

    隔离要点：get_settings 固定以真实仓库根调 load_env_candidates——在有项目 .env 的机器上
    （如开发机），delenv 清的变量会被 .env setdefault 复活，用例不再密封。load_env_candidates 加载**所有**存在的候选（.env 与 .env.zhipu 不再互斥），
    旧的「空外部文件提前 return 挡住房项目 .env」隔离法随之失效，故直接把两个接线点命名空间
    里的 load_env_candidates 钉成 no-op，环境全程由 monkeypatch 控制。"""
    get_settings = _fresh_settings_cache

    import dataset_recommender.llm.config as cfg_mod
    import dataset_recommender.llm.llm_client as client_mod

    monkeypatch.setattr(cfg_mod, "load_env_candidates", lambda *a, **k: None)
    monkeypatch.setattr(client_mod, "load_env_candidates", lambda *a, **k: None)

    monkeypatch.setenv("LLM_API_KEY", "test-fake-key-not-a-secret")
    assert get_settings().enable_llm is True
    assert load_llm_config().enable_llm is True

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    get_settings.cache_clear()
    assert get_settings().enable_llm is False
    assert load_llm_config().enable_llm is False


def test_mock_short_circuited_even_when_default_on(monkeypatch, tmp_path):
    """默认开**不得**让 mock 被当真 LLM 用：MOCK_LLM=true + 有 key + 无 ENABLE_LLM → enable_llm
    默认开成立，但介绍层 should_use_llm 仍一律 mock_not_used（绝不调真 provider）。"""
    from dataset_recommender.llm import intro_llm
    from dataset_recommender.content import summary_genre

    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("LLM_API_KEY", "test-fake-key-not-a-secret")
    config = load_llm_config(project_root=tmp_path)
    assert config.mock_llm is True
    assert config.enable_llm is True
    ok, reason = intro_llm.should_use_llm(config, summary_genre.GENRE_PROSE)
    assert ok is False and reason == "mock_not_used"


def test_default_max_tokens_is_8000_in_both_places(monkeypatch, tmp_path):
    """3000 对 deepseek 预设偏小——实测 6 次检索 2 次 finish_reason:length
    被截断判负。默认提到 8000；LLMConfig 字段默认与 load_llm_config 的 LLM_MAX_TOKENS 兜底
    必须同值（别留两个数），显式设 LLM_MAX_TOKENS 时逐字服从。"""
    from dataset_recommender.llm.llm_client import LLMConfig

    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    assert LLMConfig().max_tokens == 8000, "dataclass 字段默认不是 8000"
    assert load_llm_config(project_root=tmp_path).max_tokens == 8000, "load_llm_config 兜底不是 8000"
    monkeypatch.setenv("LLM_MAX_TOKENS", "4096")
    assert load_llm_config(project_root=tmp_path).max_tokens == 4096, "显式 LLM_MAX_TOKENS 没有生效"
