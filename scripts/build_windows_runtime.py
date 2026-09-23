# -*- coding: utf-8 -*-
"""一键构建 BioData Agent 冻结运行时（Windows x64，PyInstaller onedir）。

产物（全部落在仓库外，默认 `<仓库父目录>/build-out/`，不污染工作树）：
  <out>/dist/BioDataAgent/
      BioDataAgent.exe      windowed Web 服务（entry_web.py → desktop_launcher.main 薄转发）
      BioDataAgentMCP.exe   console  MCP stdio 服务器（mcp_server.py）
      _internal/            Python 运行时 + 依赖 + 随包静态资源（= sys._MEIPASS）
  <out>/runtime-manifest.json   逐文件相对路径 + 大小 + SHA-256（无绝对用户路径）

用法：
  <build-venv-python> scripts/build_windows_runtime.py [--out <dir>] [--build-venv <dir>]
  （build-venv 也可用环境变量 BIODATA_BUILD_VENV 指定）

前置（一次性）：
  1. uv venv --python 3.12.13 <build-venv>
  2. uv pip install --python <build-venv>/Scripts/python.exe --require-hashes \
        -r packaging/requirements/runtime-win-x64.lock -r packaging/requirements/build-win-x64.lock
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC = _REPO_ROOT / "packaging" / "pyinstaller" / "biodata-agent.spec"
_APP_NAME = "BioDataAgent"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建 BioData Agent 冻结运行时（PyInstaller onedir）")
    parser.add_argument(
        "--out", default=os.environ.get("BIODATA_BUILD_OUT") or str(_REPO_ROOT.parent / "build-out"),
        help="输出目录（默认 <仓库父目录>/build-out，仓库外）",
    )
    parser.add_argument(
        "--build-venv", default=os.environ.get("BIODATA_BUILD_VENV") or str(_REPO_ROOT.parent / "build-venv"),
        help="隔离构建 venv 目录（默认 <仓库父目录>/build-venv，仓库外）",
    )
    return parser.parse_args()


def _resolve_venv_python(build_venv: str) -> Path:
    cand = Path(build_venv) / "Scripts" / "python.exe"
    if not cand.is_file():
        sys.exit(f"[build] 构建 venv 不存在：{cand}\n  请先按 packaging/requirements/ 下 README 创建并安装两个 .lock。")
    return cand


def _check_venv(python: Path) -> None:
    probe = subprocess.run(
        [str(python), "-c", "import PyInstaller, mcp, fastapi, webview; print(PyInstaller.__version__)"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        err = probe.stderr.strip()
        # 验收缺口：构建 venv 没有 pywebview，modulegraph 扫不到
        # `import webview` 而**静默不打包** → frozen 无桌面壳回退浏览器，且全程无告警。
        # 这里把 webview 单独列为必须探得（缺了要给确切修复命令），不再让缺失静默通过。
        missing_webview = ("No module named 'webview'" in err
                           or "No module named webview" in err
                           or "cannot import name 'webview'" in err)
        repair = (f"uv pip install --python {python} --require-hashes "
                  "-r packaging/requirements/runtime-win-x64.lock "
                  "-r packaging/requirements/build-win-x64.lock")
        if missing_webview:
            sys.exit(
                f"[build] 构建 venv 缺少 pywebview（webview）：PyInstaller modulegraph 找不到 "
                f"`import webview` 会静默不打包桌面壳，frozen 将回退浏览器且无原因日志。请在仓库根修复：\n"
                f"  {repair}\n"
                f"（构建 venv 必须装进 pywebview，modulegraph 才能收编；webview/lib 等数据/二进制 "
                f"由 spec collect_all + hooks 在分析期随包）"
            )
        sys.exit(
            f"[build] 构建 venv 依赖缺失（pyinstaller/mcp/fastapi/webview）：\n{err}\n"
            f"修复命令：{repair}"
        )
    print(f"[build] venv 校验通过：pyinstaller={probe.stdout.strip()}")


def _run_pyinstaller(python: Path, out: Path) -> None:
    dist, work = out / "dist", out / "build"
    cmd = [
        str(python), "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--distpath", str(dist),
        "--workpath", str(work),
        "--log-level", "WARN",
        str(_SPEC),
    ]
    print(f"[build] 运行 PyInstaller：{' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(_REPO_ROOT))
    if result.returncode != 0:
        sys.exit(f"[build] PyInstaller 失败（exit={result.returncode}）。")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_manifest(out: Path) -> None:
    """逐文件相对路径 + 大小 + SHA-256；路径一律相对 onedir 根，无绝对用户路径。"""
    app_dir = out / "dist" / _APP_NAME
    if not app_dir.is_dir():
        sys.exit(f"[build] 产物目录缺失：{app_dir}")
    files = []
    total = 0
    for p in sorted(app_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(app_dir).as_posix()
        size = p.stat().st_size
        files.append({"path": rel, "size": size, "sha256": _sha256_file(p)})
        total += size
    manifest = {
        "format": "biodata-runtime-manifest/v1",
        "app": _APP_NAME,
        "onedir_root": app_dir.name,
        "file_count": len(files),
        "total_bytes": total,
        "files": files,
    }
    dest = out / "runtime-manifest.json"
    dest.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[build] runtime-manifest.json 已生成：{dest}（{len(files)} 文件，{total/1048576:.1f} MiB）")


_LOCAL_MODEL_ASSETS = [
    "_internal/tools/uv.exe",
    "_internal/packaging/requirements/model-win-x64.lock",
    "_internal/tools/model_worker.py",
]


def _verify_local_model_assets(app_dir: Path) -> None:
    """frozen 必须带齐随包 uv.exe、在线模型 lock 与 worker 三件，缺一即 fail-closed。"""
    missing = [rel for rel in _LOCAL_MODEL_ASSETS if not (app_dir / rel).is_file()]
    if missing:
        sys.exit(f"[build] frozen 缺少在线本地模型三件（{', '.join(missing)}）。")
    print("[build] 在线本地模型三件校验通过：uv.exe + model lock + worker")


# ── MCP 真协议自检───────────────────────────────────────────────
# 审计结论：构建验证此前只查「MCP exe 随包」与 Web /api/health，没有任何 MCP 协议自检——
# BioDataAgentMCP.exe 冻出来的 mcp/langgraph 依赖或 spec 收集缺失要到装机后才发现。
# 这里在构建期直接跑冻结 exe 的 --selfcheck（真 stdio initialize → tools/list → 工具调用，
# 输出须含 SELFCHECK_OK），失败 fail-closed 拒收产物。产物存在才跑（exe 随包是双 EXE 契约，
# 缺失同样 fail-closed）；数据根重定向到临时目录——frozen 只读语料从 _MEIPASS 读，不依赖
# data_root，保证构建机不写任何实例数据（与 installer 的 Web 版本比对同策略）。
_MCP_EXE = "BioDataAgentMCP.exe"
_MCP_SELFCHECK_TIMEOUT = 120.0


def run_mcp_selfcheck(exe: Path) -> dict:
    """跑 `BioDataAgentMCP.exe --selfcheck` → {exe, exit_code, ok, output_tail}。

    `ok` = 退出码 0 **且** 输出含 `SELFCHECK_OK`（两条同时满足才算过，缺一即 fail-closed）。
    """
    tmp_root = Path(tempfile.mkdtemp(prefix="biodata-mcp-selfcheck-"))
    env = dict(os.environ)
    env["BIODATA_DATA_ROOT"] = str(tmp_root)
    try:
        result = subprocess.run(
            [str(exe), "--selfcheck"], capture_output=True, text=True,
            env=env, timeout=_MCP_SELFCHECK_TIMEOUT, errors="replace",
        )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    output = (result.stdout or "") + (result.stderr or "")
    tail = [ln for ln in output.splitlines() if ln.strip()][-3:]
    return {
        "exe": str(exe),
        "exit_code": result.returncode,
        "ok": result.returncode == 0 and "SELFCHECK_OK" in output,
        "output_tail": tail,
    }


def verify_mcp_selfcheck(app_dir: Path) -> None:
    """frozen MCP 入口真协议自检（fail-closed）：缺 exe / 超时 / 非零退出 / 无 SELFCHECK_OK 全挡。"""
    exe = app_dir / _MCP_EXE
    if not exe.is_file():
        sys.exit(f"[build] frozen 缺少 MCP 入口 {_MCP_EXE}（spec 契约：双 EXE 必须随包）。")
    try:
        probe = run_mcp_selfcheck(exe)
    except subprocess.TimeoutExpired:
        sys.exit(f"[build] {_MCP_EXE} --selfcheck 超时（>{_MCP_SELFCHECK_TIMEOUT}s）。")
    if not probe["ok"]:
        sys.exit(
            f"[build] {_MCP_EXE} --selfcheck 未通过（exit={probe['exit_code']}，"
            f"输出末行：{probe['output_tail']}）。"
        )
    print(f"[build] {_MCP_EXE} --selfcheck 通过（SELFCHECK_OK）")


# ── Web 桌面壳验证 ─────────────────────────────────────────────
# MCP 是 console 构建，stdout 可被 capture_output 捕获；Web exe 是 windowed（noconsole），
# `sys.stdout`/`sys.stderr` 为 None（desktop_launcher._guard_streams 换 _NullStream），
# 打印不可捕获——故壳验证用「退出码 + 临时文件」双通道：
#   env BIODATA_SHELL_PROBE_OUT=<file>；退出码 0=import webview 成功，1=缺依赖/导入异常；
#   文件内容 'SHELL_PROBE_OK' 或 'SHELL_PROBE_FAIL: <原因>'。构建脚本两者都校验（同 MCP 自检）。
_WEB_EXE = "BioDataAgent.exe"
_SHELL_PROBE_TIMEOUT = 60.0
_SHELL_PROBE_OK = "SHELL_PROBE_OK"


def run_web_shell_probe(exe: Path) -> dict:
    """跑 `BioDataAgent.exe --shell-probe` → {exe, exit_code, ok, recorded}。

    `ok` = 退出码 0 **且** 验证文件内容含 `SHELL_PROBE_OK`（两条同时满足才算过，fail-closed）。
    """
    tmp_root = Path(tempfile.mkdtemp(prefix="biodata-shell-probe-"))
    out_file = tmp_root / "probe.txt"
    env = dict(os.environ)
    env["BIODATA_SHELL_PROBE_OUT"] = str(out_file)
    try:
        result = subprocess.run(
            [str(exe), "--shell-probe"], capture_output=True, text=True,
            env=env, timeout=_SHELL_PROBE_TIMEOUT, errors="replace",
        )
        recorded = ""
        if out_file.is_file():
            recorded = out_file.read_text(encoding="utf-8", errors="replace").strip()
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    return {
        "exe": str(exe),
        "exit_code": result.returncode,
        "ok": result.returncode == 0 and _SHELL_PROBE_OK in recorded,
        "recorded": recorded,
    }


def verify_web_shell_probe(app_dir: Path) -> None:
    """frozen Web 桌面壳真依赖验证（fail-closed）：缺 exe / 超时 / 非零退出 / 无
    SHELL_PROBE_OK 全挡—— 的 2.5.0 正是「frozen 无壳但构建全程无告警」漏出。"""
    exe = app_dir / _WEB_EXE
    if not exe.is_file():
        sys.exit(f"[build] frozen 缺少 Web 入口 {_WEB_EXE}（spec 契约：双 EXE 必须随包）。")
    try:
        probe = run_web_shell_probe(exe)
    except subprocess.TimeoutExpired:
        sys.exit(f"[build] {_WEB_EXE} --shell-probe 超时（>{_SHELL_PROBE_TIMEOUT}s）。")
    if not probe["ok"]:
        sys.exit(
            f"[build] {_WEB_EXE} --shell-probe 未通过（exit={probe['exit_code']}，"
            f"探针记录：{probe['recorded'] or '<empty>'}）——桌面壳依赖未随包，frozen 会回退浏览器。"
        )
    print(f"[build] {_WEB_EXE} --shell-probe 通过（SHELL_PROBE_OK，import webview 随包）")


def main() -> int:
    args = _parse_args()
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    python = _resolve_venv_python(args.build_venv)
    _check_venv(python)
    _run_pyinstaller(python, out)
    _write_manifest(out)
    _verify_local_model_assets(out / "dist" / _APP_NAME)
    verify_web_shell_probe(out / "dist" / _APP_NAME)
    verify_mcp_selfcheck(out / "dist" / _APP_NAME)
    print(f"[build] 完成。产物：{out / 'dist' / _APP_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
