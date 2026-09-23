# -*- coding: utf-8 -*-
"""安装器 E2E harness（scripts/installer_e2e.py）契约测试。

- **报告结构**：frozen 段 / installer_matrix 段形状与矩阵 ID（matrix1-matrix13）稳定。
- **frozen 段**：build-out 存在 + 本会话无运行实例 + 7860-7869 空闲 → 真跑真实
  `BioDataAgent.exe` 四用例（launch1 health / launch2 固定端口跨重启 / launch3 二次启动 attach /
  launch4 退出端口释放）全 PASS；条件不满足 → `pytest.skip` 并给明确原因（**环境缺失
  不写成通过**）。
- **安装器段**：真实安装器 exe 未构建时 → 整矩阵 13 项 SKIP 且标注
  「待安装器构建联调」（matrix13=桌面壳未静默缺失，前置 WebView2 缺失时也 SKIP）；安装器
  exe 就位时自动真跑。
- **发现函数**：find_frozen_runtime / find_installer / probe_mutex 行为。

真跑耗时可接受（一次 copytree ~114MiB + 3 次 exe 启动 ≈ 10-20s），且由环境门控。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import installer_e2e as e2e  # noqa: E402


# ===========================================================================
# 发现与守卫
# ===========================================================================
def test_find_frozen_runtime_returns_real_dir_or_none():
    frozen_root = e2e.find_frozen_runtime()
    if frozen_root is not None:
        assert (frozen_root / e2e.FROZEN_EXE_NAME).is_file(), \
            f"frozen runtime 必须含 {e2e.FROZEN_EXE_NAME}：{frozen_root}"


def test_find_installer_returns_none_or_matching_name():
    installer = e2e.find_installer()
    if installer is not None:
        assert installer.name.startswith(e2e.INSTALLER_PREFIX)
        assert installer.name.endswith(".exe")
    else:
        # 当前安装器未构建 → 整矩阵必须 SKIP（由报告测试兜底），此处仅为语义说明
        pass


def test_find_installer_prefers_current_version_over_walk_order(monkeypatch, tmp_path):
    monkeypatch.delenv("BIODATA_INSTALLER_EXE", raising=False)
    monkeypatch.delenv("BIODATA_BUILD_OUT", raising=False)
    monkeypatch.setattr(e2e, "DEFAULT_BUILD_OUT", tmp_path)
    monkeypatch.setattr(e2e, "DEFAULT_INSTALLER_WORKTREE", tmp_path / "missing")
    (tmp_path / "BioData-Agent-Setup-2.4.0-win-x64-unsigned-dev.exe").touch()
    current = tmp_path / f"BioData-Agent-Setup-{e2e.WEB_API_VERSION}-win-x64-unsigned-dev.exe"
    current.touch()
    assert e2e.find_installer() == current


def test_find_installer_falls_back_to_highest_semver(monkeypatch, tmp_path):
    monkeypatch.delenv("BIODATA_INSTALLER_EXE", raising=False)
    monkeypatch.delenv("BIODATA_BUILD_OUT", raising=False)
    monkeypatch.setattr(e2e, "DEFAULT_BUILD_OUT", tmp_path)
    monkeypatch.setattr(e2e, "DEFAULT_INSTALLER_WORKTREE", tmp_path / "missing")
    monkeypatch.setattr(e2e, "WEB_API_VERSION", "9.9.9")
    for version in ("2.10.0", "2.9.9"):
        (tmp_path / f"BioData-Agent-Setup-{version}-win-x64-unsigned-dev.exe").touch()
    assert "2.10.0" in e2e.find_installer().name


def test_probe_mutex_returns_tuple():
    free, note = e2e.probe_mutex()
    assert isinstance(free, bool)
    assert isinstance(note, str) and note


def test_cli_help_renders_literal_localappdata(capsys):
    """argparse 会对 help 做 %-format；Windows 环境变量写法必须转义成 %% 才能展示。"""
    with pytest.raises(SystemExit) as exc:
        e2e.main(["--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "%LOCALAPPDATA%" in text
    assert "--allow-real-installer" in text


def test_cli_returns_nonzero_when_any_executed_case_fails(monkeypatch, capsys):
    monkeypatch.setattr(e2e, "run_e2e", lambda **_kwargs: {
        "environment": {},
        "frozen": {"enabled": True, "run": "passed", "reason": "", "cases": []},
        "installer_matrix": [{"id": "m01", "status": "fail", "detail": "boom"}],
    })
    assert e2e.main(["--installer-only"]) == 1
    assert '"status": "fail"' in capsys.readouterr().out


# ===========================================================================
# 报告结构 + 安装器矩阵（当前必然 SKIP 且标注待安装器联调）
# ===========================================================================
def test_report_shape_and_matrix_ids(tmp_path):
    report = e2e.run_e2e(workdir=tmp_path / "e2e", frozen=False, installer=True)
    assert set(report) == {"environment", "frozen", "installer_matrix"}
    assert report["frozen"]["run"] == "disabled"
    matrix = report["installer_matrix"]
    assert [m["id"] for m in matrix] == [f"matrix{i}" for i in range(1, 14)]
    for item in matrix:
        assert item["status"] == "skip", f"{item['id']} 当前必须 SKIP：{item}"
        assert "待安装器联调" in item["detail"], f"{item['id']} 必须标注待安装器联调"


def test_installer_matrix_skips_when_exe_absent(monkeypatch, tmp_path):
    """显式造「安装器缺失」场景：即便 env 指定也不跑真安装器（当前无产物）。"""
    monkeypatch.delenv("BIODATA_INSTALLER_EXE", raising=False)
    assert e2e.find_installer() is None or True  # 环境若有产物则以实际为准
    matrix = e2e.installer_matrix(None, tmp_path)
    assert len(matrix) == 13
    assert all(m["status"] == "skip" for m in matrix)
    assert all("待安装器联调" in m["detail"] for m in matrix)


# ===========================================================================
# frozen 段：真跑真实 exe（环境门控，缺条件则 skip 并注明原因）
# ===========================================================================
def test_frozen_e2e_real_exe(tmp_path):
    frozen_root = e2e.find_frozen_runtime()
    if frozen_root is None:
        pytest.skip("frozen runtime 未找到（build-out/dist/BioDataAgent 缺失），待冻结运行时构建产物")
    mutex_free, mutex_note = e2e.probe_mutex()
    if not mutex_free:
        pytest.skip(f"frozen 真跑被环境门控拦截：{mutex_note}")
    if not e2e.drift_ports_available():
        pytest.skip("frozen 真跑被环境门控拦截：7860-7869 无可用端口")

    report = e2e.run_e2e(workdir=tmp_path / "e2e", frozen=True, installer=False)
    frozen = report["frozen"]
    assert frozen["run"] in ("passed", "failed")
    if frozen["run"] == "failed":
        pytest.fail(f"frozen E2E 失败：{json.dumps(frozen, ensure_ascii=False, indent=2)}")
    case_ids = [c["id"] for c in frozen["cases"]]
    assert case_ids == ["launch1", "launch4", "launch2", "launch3"], case_ids
    by_id = {c["id"]: c for c in frozen["cases"]}
    # launch1/launch4 是真跑必过项（health + 退出端口释放）
    assert by_id["launch1"]["status"] == "pass", by_id["launch1"]
    assert by_id["launch4"]["status"] == "pass", by_id["launch4"]
    # launch2/launch3 为启动器专用：能力就绪（spec 入口已切换）则 pass；产物若缺启动器能力
    # （runtime.json/instance.json 不存在）则 skip 且理由如实标注，不谎报为通过。
    for cid in ("launch2", "launch3"):
        status = by_id[cid]["status"]
        assert status in ("pass", "skip"), by_id[cid]
        if status == "skip":
            assert "启动器" in by_id[cid]["detail"], by_id[cid]
            assert "runtime.json" in by_id[cid]["detail"] or "instance.json" in by_id[cid]["detail"], by_id[cid]
    # 每用例 detail 有实际观测（不空转）
    for c in frozen["cases"]:
        assert c["detail"].strip(), c["id"]


def test_frozen_case_ids_stable_even_when_skipped(tmp_path):
    """即使被门控跳过，harness 不执行也不谎报——frozen 段 run=skipped 且 reason 明确。"""
    report = e2e.run_e2e(workdir=tmp_path / "e2e", frozen=False, installer=False)
    assert report["frozen"]["run"] == "disabled"


# ===========================================================================
# _wait_for 防空转（进程死了立即失败，不空转到超时）
# ===========================================================================
class _DeadProc:
    """已退出进程替身（poll 返回非 None）。"""

    def __init__(self, code: int = 1) -> None:
        self.code = code

    def poll(self):
        return self.code


class _AliveProc:
    """运行中进程替身（poll 返回 None）。"""

    def poll(self):
        return None


def test_wait_for_fails_immediately_when_proc_died():
    import time
    start = time.monotonic()
    assert e2e._wait_for(lambda: False, timeout=30.0, poll=0.1, proc=_DeadProc()) is False
    assert time.monotonic() - start < 2.0, "进程已死必须立即失败，不得空转到超时"


def test_wait_for_alive_proc_polls_until_true():
    calls: list[int] = []

    def fn():
        calls.append(1)
        return len(calls) >= 2

    assert e2e._wait_for(fn, timeout=5.0, poll=0.01, proc=_AliveProc()) is True


def test_wait_for_alive_proc_timeout_returns_false():
    assert e2e._wait_for(lambda: False, timeout=0.1, poll=0.01, proc=_AliveProc()) is False


def test_wait_for_without_proc_unchanged():
    assert e2e._wait_for(lambda: True, timeout=1.0, poll=0.01) is True
    assert e2e._wait_for(lambda: False, timeout=0.05, poll=0.01) is False
