# -*- coding: utf-8 -*-
"""环境变量防御批（实现 接手）。

四类断言，守护「裸名环境变量被 ambient 残留劫持」的五种失败形态并证明既有语义逐位不改：

① `parse_int_env` 容错：合法（含负数）逐位透传；非数字回落默认 + warning；空值/纯空白回落默认且不警告。
② `_warn_ambient_generic_env` 对五个无前缀通用名逐个触发，全未设置时保持静默。
③ 行为保留：合法 `DATA_DIR` / `TOP_K` 覆盖语义、`TOP_K` 默认值都不因新增警告而改变。
④ corpus `_load_base` 空装载 warn-once（同一空目录警告恰好一次），非空装载零警告。
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.llm.config import get_settings, parse_int_env, _warn_ambient_generic_env  # noqa: E402
from dataset_recommender.corpus import corpus as C  # noqa: E402


# 五个无前缀通用名（与 config._AMBIENT_GENERIC_ENV_EFFECTS 同步）。
GENERIC_NAMES = ("DATA_DIR", "TOP_K", "MOCK_LLM", "ENABLE_LLM", "KEYWORD_MAPPING_PATH")


@pytest.fixture
def _fresh_settings_cache():
    """get_settings 带 lru_cache(maxsize=1)：每个用例前后清缓存，防跨用例串环境。"""
    get_settings.cache_clear()
    yield get_settings
    get_settings.cache_clear()


@pytest.fixture
def _clear_generic_env(monkeypatch):
    """掐断一切通用名环境变量，避免宿主机残留值干扰断言。"""
    for name in GENERIC_NAMES:
        monkeypatch.delenv(name, raising=False)


# ------------------------------------------------------------- ① parse_int_env 容错


def test_parse_int_env_valid_value_passthrough(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("BIODATA_TEST_INT", "42")
    assert parse_int_env("BIODATA_TEST_INT", 7860) == 42
    assert not caplog.records, "合法整型不得产生警告"


def test_parse_int_env_negative_value_passthrough(monkeypatch):
    """负数按 int() 原样透传——语义由调用方约束（如端口号），本函数不设上下限。"""
    monkeypatch.setenv("BIODATA_TEST_INT", "-5")
    assert parse_int_env("BIODATA_TEST_INT", 7860) == -5


def test_parse_int_env_non_numeric_falls_back(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("BIODATA_TEST_INT", "not-a-number")
    assert parse_int_env("BIODATA_TEST_INT", 7860) == 7860
    assert any("BIODATA_TEST_INT" in r.message for r in caplog.records), "非数字应点名变量回落默认"


def test_parse_int_env_empty_value_falls_back(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("BIODATA_TEST_INT", "   ")
    assert parse_int_env("BIODATA_TEST_INT", 7860) == 7860
    assert not caplog.records, "空值/纯空白按未设置处理，静默回落默认而非警告"


def test_parse_int_env_unset_falls_back(monkeypatch):
    monkeypatch.delenv("BIODATA_TEST_INT", raising=False)
    assert parse_int_env("BIODATA_TEST_INT", 7860) == 7860


# --------------------------------------------- ② ambient 警告对五个通用名逐个触发


@pytest.mark.parametrize("name", GENERIC_NAMES)
def test_ambient_warning_fires_for_each_generic_name(monkeypatch, caplog, _clear_generic_env, name):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv(name, "some-value")
    _warn_ambient_generic_env()
    assert any(name in r.message for r in caplog.records), f"环境变量 {name} 设置后应触发 ambient 警告"


def test_ambient_warning_silent_when_all_unset(monkeypatch, caplog, _clear_generic_env):
    caplog.set_level(logging.WARNING)
    _warn_ambient_generic_env()
    assert not caplog.records, "五个通用名全未设置时不得产生任何警告"


# --------------------------------------------------------- ③ 行为保留（覆盖语义不改）


def test_data_dir_override_semantics_preserved(monkeypatch, _fresh_settings_cache, _clear_generic_env):
    """DATA_DIR 显式设置 → data_dir = project_root / DATA_DIR（文档化历史口径，只警告不改值）。"""
    import dataset_recommender.llm.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "load_env_candidates", lambda *a, **k: None)
    monkeypatch.setenv("DATA_DIR", "my_custom_base")
    settings = _fresh_settings_cache()
    assert settings.data_dir == Path(settings.project_root) / "my_custom_base"
    assert settings.top_k == 10, "未设 TOP_K 时默认值不受影响"


def test_top_k_override_semantics_preserved(monkeypatch, _fresh_settings_cache, _clear_generic_env):
    """TOP_K 显式合法值 → top_k 逐位透传（默认 10 → 5，语义与历史一致）。"""
    import dataset_recommender.llm.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "load_env_candidates", lambda *a, **k: None)
    monkeypatch.setenv("TOP_K", "5")
    settings = _fresh_settings_cache()
    assert settings.top_k == 5


def test_top_k_invalid_falls_back_with_override_off(monkeypatch, _fresh_settings_cache, _clear_generic_env):
    """非数字 TOP_K 不再崩，回落默认 10——行为从「崩溃」变「容错+警告」。"""
    import dataset_recommender.llm.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "load_env_candidates", lambda *a, **k: None)
    monkeypatch.setenv("TOP_K", "abc")
    settings = _fresh_settings_cache()
    assert settings.top_k == 10


# --------------------------------------------------------- ④ corpus 空装载 warn-once


def test_empty_base_load_warns_once(monkeypatch, caplog, tmp_path):
    caplog.set_level(logging.WARNING, logger="dataset_recommender.corpus.corpus")
    empty = tmp_path / "database" / "base"
    empty.mkdir(parents=True)
    assert C._load_base(empty) == []
    assert C._load_base(empty) == [], "二次装载命中空缓存，仍返回空列表"
    msgs = [r.message for r in caplog.records]
    assert len(msgs) == 1, f"同一空目录空装载警告应恰好一次，got {len(msgs)}"
    assert "DATA_DIR" in msgs[0]


def test_nonempty_base_load_no_warning(monkeypatch, caplog, tmp_path):
    caplog.set_level(logging.WARNING, logger="dataset_recommender.corpus.corpus")
    data_dir = tmp_path / "database" / "base"
    data_dir.mkdir(parents=True)
    payload = [{"dataset_uid": "a1", "dataset_name": "数据集1", "species": "Human"}]
    (data_dir / "base.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert len(C._load_base(data_dir)) == 1
    assert not caplog.records, "非空基语料装载不应出现空装载警告"
