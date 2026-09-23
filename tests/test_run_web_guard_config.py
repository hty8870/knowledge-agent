# -*- coding: utf-8 -*-
"""run_web._validate_guard_config（公网护栏 fail-closed 启动校验）env 组合矩阵。

校验函数独立可调用（不起 uvicorn、不预热）：护栏模式（BIODATA_REQUIRE_ACCOUNT=1）缺任一
必需配置 → 打印中文错误到 stderr 并返回 2；配置齐 / 闸关 → 0。闸关零校验零行为变化。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (PROJECT_ROOT / "scripts", PROJECT_ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import run_web  # noqa: E402

_GUARD_ENVS = (
    "BIODATA_REQUIRE_ACCOUNT",
    "BIODATA_INVITE_CODE",
    "BIODATA_TRUSTED_HOSTS",
    "BIODATA_LLM_DAILY_PER_USER",
    "BIODATA_LLM_DAILY_GLOBAL",
)


def _clear(monkeypatch):
    for name in _GUARD_ENVS:
        monkeypatch.delenv(name, raising=False)


def _full_guard_env(monkeypatch):
    """护栏模式合法全配（测试值非真实秘密）。"""
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    monkeypatch.setenv("BIODATA_INVITE_CODE", "test-invite-not-a-real-secret")
    monkeypatch.setenv("BIODATA_TRUSTED_HOSTS", "203.0.113.10")  # TEST-NET-3 文档保留段
    monkeypatch.setenv("BIODATA_LLM_DAILY_PER_USER", "100")
    monkeypatch.setenv("BIODATA_LLM_DAILY_GLOBAL", "1000")


def test_guard_off_skips_all_validation(monkeypatch):
    _clear(monkeypatch)
    assert run_web._validate_guard_config() == 0


def test_guard_on_full_config_passes(monkeypatch):
    _clear(monkeypatch)
    _full_guard_env(monkeypatch)
    assert run_web._validate_guard_config() == 0


def test_guard_on_missing_invite_fails(monkeypatch, capsys):
    _clear(monkeypatch)
    _full_guard_env(monkeypatch)
    monkeypatch.delenv("BIODATA_INVITE_CODE")
    assert run_web._validate_guard_config() == 2
    err = capsys.readouterr().err
    assert "BIODATA_INVITE_CODE" in err and "拒绝启动" in err


def test_guard_on_missing_trusted_hosts_fails(monkeypatch, capsys):
    _clear(monkeypatch)
    _full_guard_env(monkeypatch)
    monkeypatch.setenv("BIODATA_TRUSTED_HOSTS", "   ")   # 空白视同未设置
    assert run_web._validate_guard_config() == 2
    assert "BIODATA_TRUSTED_HOSTS" in capsys.readouterr().err


def test_guard_on_quota_must_be_explicit_positive_int(monkeypatch, capsys):
    _clear(monkeypatch)
    _full_guard_env(monkeypatch)
    for bad in (None, "", "0", "-5", "abc"):
        if bad is None:
            monkeypatch.delenv("BIODATA_LLM_DAILY_PER_USER", raising=False)
        else:
            monkeypatch.setenv("BIODATA_LLM_DAILY_PER_USER", bad)
        assert run_web._validate_guard_config() == 2, f"per_user={bad!r} 应拒绝"
    assert "BIODATA_LLM_DAILY_PER_USER" in capsys.readouterr().err
    monkeypatch.setenv("BIODATA_LLM_DAILY_PER_USER", "100")
    monkeypatch.delenv("BIODATA_LLM_DAILY_GLOBAL", raising=False)
    assert run_web._validate_guard_config() == 2   # global 同样必须显式


def test_main_returns_2_without_serving_when_guard_config_invalid(monkeypatch):
    """main() 接线：护栏模式配置非法 → return 2，且绝不走到 warm/uvicorn.run。"""
    _clear(monkeypatch)
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")   # 其余全缺
    called = []
    monkeypatch.setattr(run_web, "start_background_warm", lambda *a, **k: called.append("warm"))
    monkeypatch.setattr(run_web.uvicorn, "run", lambda *a, **k: called.append("serve"))
    assert run_web.main() == 2
    assert called == []


def test_main_guard_off_behavior_unchanged(monkeypatch):
    """闸关：校验零介入，warm → serve 顺序与历史一致（与 test_run_web_warmup 同口径）。"""
    _clear(monkeypatch)
    order = []
    monkeypatch.setattr(run_web, "start_background_warm", lambda *a, **k: order.append("warm"))
    monkeypatch.setattr(run_web.uvicorn, "run", lambda *a, **k: order.append("serve"))
    assert run_web.main() == 0
    assert order == ["warm", "serve"]
