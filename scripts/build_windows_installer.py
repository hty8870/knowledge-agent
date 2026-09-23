# -*- coding: utf-8 -*-
"""一键构建 BioData Agent 图形化安装器（Inno Setup 6.7.3）。

流程（fail-closed）：
  1. 解析版本单一真源 `src/dataset_recommender/app/webapp.py` 的 `WEB_API_VERSION`
     （必须且只能匹配一个 `X.Y.Z`，否则非零退出——版本不匹配 fail-closed）。
  2. 确认 PyInstaller onedir 产物存在（`<out>/dist/BioDataAgent/BioDataAgent.exe`）；
     `--build-runtime` 时先触发 `scripts/build_windows_runtime.py`（复用既有 build-venv 约定）。
  3. 启动产物 exe 探测 `/api/health` 的 `version` 字段，与步骤 1 解析值逐字比对；
     不一致 → 非零退出（防旧运行时被新安装器打包；`--skip-runtime-version-check` 可关）。
  4. ISCC 编译 `packaging/inno/biodata-agent.iss`：
     `/DAppVersion=...` `/DRuntimeDir=<onedir 绝对路径>` `/O<out>` `/F<base>`；
     iss 内 `#ifndef` + `#error` 双保险（构建脚本没注入时编译期即失败）。
  5. 产物 `BioData-Agent-Setup-<version>-win-x64-unsigned-dev.exe` 落 `<out>/`（仓库外），
     生成 `.sha256` sidecar（`<sha256>  <文件名>`），并打印体积摘要。

   批新增校验（均在 ISCC 前、fail-closed）：
    ②b 在线本地模型三件（uv.exe / model lock / worker）齐全；
    ②c `BioDataAgentMCP.exe --selfcheck` 真协议探测，输出须含 SELFCHECK_OK。

用法：
  <python> scripts/build_windows_installer.py [--out <dir>] [--build-venv <dir>]
      [--build-runtime] [--skip-runtime-version-check] [--skip-build] 
  环境变量：BIODATA_BUILD_OUT / BIODATA_BUILD_VENV（与冻结运行时构建脚本同键）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ISS = _REPO_ROOT / "packaging" / "inno" / "biodata-agent.iss"
_WEBAPP = _REPO_ROOT / "src" / "dataset_recommender" / "app" / "webapp.py"
_ISCC = Path(os.environ.get("ISCC_PATH") or r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe")
_RUNTIME_SCRIPT = _REPO_ROOT / "scripts" / "build_windows_runtime.py"

# 产物命名模板（unsigned-dev 表明未签名开发构建；签名属后续批）
_EXE_NAME = "BioData-Agent-Setup-{version}-win-x64-unsigned-dev.exe"

#: frozen onedir 必须带齐的在线本地模型三件（随 PyInstaller spec 作 data 进 _internal/）。
_LOCAL_MODEL_ASSETS = [
    "_internal/tools/uv.exe",
    "_internal/packaging/requirements/model-win-x64.lock",
    "_internal/tools/model_worker.py",
]

#: MCP 真协议自检：frozen MCP 入口 + 自检超时。构建/打包前必须跑
#: `BioDataAgentMCP.exe --selfcheck` 且输出含 SELFCHECK_OK（fail-closed），否则拒收——防把
#: MCP 依赖缺失的旧/残 onedir 静默编译进 Setup（与 `verify_local_model_assets` 同姿态）。
_MCP_EXE = "BioDataAgentMCP.exe"
_MCP_SELFCHECK_TIMEOUT = 120.0

#: runtime-manifest（build_windows_runtime.py:91 写出，落 <out>/runtime-manifest.json）契约常量。
#: 此前安装器只认「runtime 目录存在」，不校验其内容与构建产物一致——把旧/残 onedir 静默编进
#: Setup。另增 manifest 校验（存在性 + 关键字段 + 实物逐文件对账，
#: fail-closed）；`--skip-runtime-version-check` 只旁路 exe /api/health 版本比对，不旁路本校验。
_MANIFEST_NAME = "runtime-manifest.json"
_MANIFEST_FORMAT = "biodata-runtime-manifest/v1"
_RUNTIME_MANIFEST_FIELDS = ("format", "app", "onedir_root", "file_count", "total_bytes", "files")

_VERSION_RE = re.compile(r'^WEB_API_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*$')
_VERSION_SHAPE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def parse_webapi_version(webapp: Path) -> str:
    """从 webapp.py 解析 WEB_API_VERSION（fail-closed：不唯一/形状不对即抛）。"""
    text = webapp.read_text(encoding="utf-8")
    hits = [_VERSION_RE.match(line) for line in text.splitlines()]
    hits = [m for m in hits if m]
    if len(hits) != 1:
        raise ValueError(
            f"WEB_API_VERSION 唯一性校验失败：期望恰好 1 处定义，实际 {len(hits)} 处（{webapp}）"
        )
    version = hits[0].group(1)
    if not _VERSION_SHAPE.match(version):
        raise ValueError(f"WEB_API_VERSION 形状非法（期望 X.Y.Z）：{version!r}")
    return version


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def check_runtime_version(exe: Path, expected: str) -> None:
    """启动产物 exe 探测 /api/health.version，与期望值逐字比对；不匹配 fail-closed。"""
    tmp_root = Path(tempfile.mkdtemp(prefix="biodata-inst-vercheck-"))
    port = find_free_port()
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["BIODATA_DATA_ROOT"] = str(tmp_root)
    proc = subprocess.Popen([str(exe)], env=env, cwd=str(tmp_root))
    try:
        url = f"http://127.0.0.1:{port}/api/health"
        version = None
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"运行中退出（code={proc.returncode}）：{exe}")
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                version = str(payload.get("version", ""))
                break
            except Exception:
                time.sleep(0.5)
        if version is None:
            raise RuntimeError(f"30s 内未从 {url} 取到 /api/health（版本比对失败）")
        if version != expected:
            raise RuntimeError(
                f"运行时版本与单一真源不匹配（fail-closed）：webapp.py={expected}，"
                f"实际运行 exe={version} —— 请先重建 runtime（--build-runtime）"
            )
        print(f"[installer] 运行时版本校验通过：{exe} → /api/health version={version}")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmp_root, ignore_errors=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_local_model_assets(runtime_dir: Path) -> list[str]:
    """frozen onedir 必须带齐随包 uv.exe、在线模型 lock 与 worker 三件，缺一即 fail-closed。

    即使跳过 `--build-runtime`（直接打包既有 onedir），也要防把旧/缺件的运行时
    静默编译进 Setup——基础安装不受影响，但设置页「在线安装本地模型」会失去支撑。
    """
    return [rel for rel in _LOCAL_MODEL_ASSETS if not (runtime_dir / rel).is_file()]


def verify_mcp_selfcheck(runtime_dir: Path) -> "tuple[bool, str]":
    """frozen MCP 入口 --selfcheck 真协议探测（批，fail-closed 语义）。

    返回 (ok, detail)。产物存在才跑（缺失 exe 本身就是 onedir 双 EXE 契约违例，算失败）；
    退出码非 0 或输出不含 SELFCHECK_OK 均判失败。数据根重定向到临时目录（frozen 只读
    语料从 _MEIPASS 读，不依赖 data_root，打包机不被写任何实例数据）。
    """
    exe = runtime_dir / _MCP_EXE
    if not exe.is_file():
        return False, f"frozen 缺少 MCP 入口 {_MCP_EXE}（双 EXE 是 onedir 契约）。"
    tmp_root = Path(tempfile.mkdtemp(prefix="biodata-inst-mcp-selfcheck-"))
    env = dict(os.environ)
    env["BIODATA_DATA_ROOT"] = str(tmp_root)
    try:
        try:
            result = subprocess.run(
                [str(exe), "--selfcheck"], capture_output=True, text=True,
                env=env, timeout=_MCP_SELFCHECK_TIMEOUT, errors="replace",
            )
        except subprocess.TimeoutExpired:
            return False, f"{_MCP_EXE} --selfcheck 超时（>{_MCP_SELFCHECK_TIMEOUT}s）。"
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    output = (result.stdout or "") + (result.stderr or "")
    tail = [ln for ln in output.splitlines() if ln.strip()][-3:]
    if result.returncode != 0 or "SELFCHECK_OK" not in output:
        return False, f"{_MCP_EXE} --selfcheck 未通过（exit={result.returncode}，输出末行：{tail}）。"
    return True, f"{_MCP_EXE} --selfcheck 通过（SELFCHECK_OK）"


def verify_runtime_manifest(runtime_dir: Path) -> "tuple[bool, str]":
    """校验 `build_windows_runtime.py:91` 写出的 runtime-manifest.json（fail-closed）。

    此前 `--build-runtime` 只确认 onedir 目录/两个 exe 存在，不校验
    runtime-manifest.json——把旧/残/内容漂移的 onedir 静默编进 Setup。这里在 ISCC 前拦下：
      1. manifest 必须存在（<out>/runtime-manifest.json，缺则拒收）；
      2. `format`/`app`/`onedir_root` 必须与当前构建契约一致；
      3. `files[]` 的路径必须是规范化、无重复的 onedir 相对 POSIX 路径；
      4. 每个文件的大小与 SHA-256 必须与实物逐一一致，且文件集合完全相同。

    返回 `(ok, detail)`。任何不一致判失败。**`--skip-runtime-version-check` 不旁路本校验**——
    它只关 exe /api/health 版本比对，清单对账仍在。
    """
    out = runtime_dir.parents[1]
    manifest = out / _MANIFEST_NAME
    if not manifest.is_file():
        return False, (
            f"缺少运行时清单 {_MANIFEST_NAME}（{manifest}）。"
            "请先 --build-runtime 重建运行时，或确认 --out 指向含 manifest 的正确 build-out。"
        )
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"运行时清单 {_MANIFEST_NAME} 无法解析：{exc}"
    for field in _RUNTIME_MANIFEST_FIELDS:
        if field not in data:
            return False, f"运行时清单 {_MANIFEST_NAME} 缺关键字段：{field}"
    if data.get("format") != _MANIFEST_FORMAT:
        return False, f"运行时清单 format 不符：{data.get('format')!r}（期望 {_MANIFEST_FORMAT}）"
    if data.get("app") != runtime_dir.name:
        return False, f"运行时清单 app 不符：{data.get('app')!r}（期望 {runtime_dir.name}）"
    if data.get("onedir_root") != runtime_dir.name:
        return False, (
            f"运行时清单 onedir_root 不符：{data.get('onedir_root')!r}（期望 {runtime_dir.name}）"
        )
    files = data.get("files")
    if not isinstance(files, list) or not files:
        return False, "运行时清单 files 为空或非法——不能为空 onedir。"
    declared_count = data.get("file_count")
    declared_total = data.get("total_bytes")
    if not isinstance(declared_count, int) or not isinstance(declared_total, int):
        return False, "运行时清单 file_count/total_bytes 类型非法。"
    if declared_count != len(files):
        return False, (
            f"运行时清单自相矛盾：file_count={declared_count}，files 实际 {len(files)} 条。"
        )
    manifest_files: dict[str, tuple[int, str]] = {}
    seen_casefold: set[str] = set()
    manifest_total = 0
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            return False, f"运行时清单 files[{index}] 不是对象。"
        rel = entry.get("path")
        size = entry.get("size")
        digest = entry.get("sha256")
        if not isinstance(rel, str) or not rel:
            return False, f"运行时清单 files[{index}].path 为空或非法。"
        pure = PurePosixPath(rel)
        if (
            "\\" in rel
            or rel.startswith("/")
            or ":" in rel
            or any(part in {"", ".", ".."} for part in rel.split("/"))
            or pure.as_posix() != rel
        ):
            return False, f"运行时清单路径不是规范化 onedir 相对路径：{rel!r}"
        folded = rel.casefold()
        if rel in manifest_files or folded in seen_casefold:
            return False, f"运行时清单路径重复（Windows 大小写不敏感）：{rel!r}"
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            return False, f"运行时清单文件大小非法：{rel!r} → {size!r}"
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            return False, f"运行时清单 SHA-256 非法：{rel!r}"
        manifest_files[rel] = (size, digest)
        seen_casefold.add(folded)
        manifest_total += size

    if manifest_total != declared_total:
        return False, (
            f"运行时清单自相矛盾：total_bytes={declared_total}，files 大小合计 {manifest_total}。"
        )

    actual_files = {
        p.relative_to(runtime_dir).as_posix(): p
        for p in runtime_dir.rglob("*")
        if p.is_file()
    }
    actual_count = len(actual_files)
    actual_total = sum(p.stat().st_size for p in actual_files.values())
    if set(manifest_files) != set(actual_files):
        missing = sorted(set(manifest_files) - set(actual_files))[:3]
        extra = sorted(set(actual_files) - set(manifest_files))[:3]
        return False, (
            "运行时清单与 onedir 文件集合不一致："
            f"缺失={missing or '无'}，额外={extra or '无'} —— 请先 --build-runtime 重建。"
        )
    if declared_count != actual_count or declared_total != actual_total:
        return False, (
            f"运行时清单与 onedir 实物不一致：清单 {declared_count} 文件 / {declared_total} B，"
            f"实物 {actual_count} 文件 / {actual_total} B —— 请先 --build-runtime 重建。"
        )
    for rel, (expected_size, expected_digest) in manifest_files.items():
        actual = actual_files[rel]
        actual_size = actual.stat().st_size
        if actual_size != expected_size:
            return False, (
                f"运行时文件大小不符：{rel}，清单 {expected_size} B，实物 {actual_size} B。"
            )
        actual_digest = sha256_file(actual)
        if actual_digest != expected_digest:
            return False, (
                f"运行时文件 SHA-256 不符：{rel}，清单 {expected_digest}，实物 {actual_digest}。"
            )
    return True, (
        f"运行时清单校验通过：{_MANIFEST_NAME}（{declared_count} 文件，"
        f"{actual_total / 1048576:.1f} MiB）路径/大小/SHA-256 与 onedir 逐文件一致"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="一键构建 BioData Agent Inno Setup 安装器")
    parser.add_argument("--out", default=os.environ.get("BIODATA_BUILD_OUT") or str(_REPO_ROOT.parent / "build-out"))
    parser.add_argument("--build-venv", default=os.environ.get("BIODATA_BUILD_VENV") or str(_REPO_ROOT.parent / "build-venv"))
    parser.add_argument("--build-runtime", action="store_true", help="先触发 scripts/build_windows_runtime.py 重建冻结运行时")
    parser.add_argument("--skip-runtime-version-check", action="store_true", help="跳过 exe /api/health 版本比对（默认开启 fail-closed）")
    parser.add_argument("--skip-build", action="store_true", help="仅做检查并打印计划（不实际编译，测试用）")
    args = parser.parse_args()

    out = Path(args.out).resolve()
    build_venv = Path(args.build_venv).resolve()

    # ① 版本单一真源解析（fail-closed）
    try:
        version = parse_webapi_version(_WEBAPP)
    except (OSError, ValueError) as exc:
        print(f"[installer] 版本解析失败：{exc}", file=sys.stderr)
        return 2
    print(f"[installer] 版本单一真源：{_WEBAPP} → WEB_API_VERSION={version}")

    # ② 运行时产物确认 / 触发重建
    runtime_dir = out / "dist" / "BioDataAgent"
    runtime_exe = runtime_dir / "BioDataAgent.exe"
    if args.build_runtime:
        venv_python = build_venv / "Scripts" / "python.exe"
        if not venv_python.is_file():
            print(f"[installer] 构建 venv 缺失：{venv_python}", file=sys.stderr)
            return 2
        rc = subprocess.run(
            [str(venv_python), str(_RUNTIME_SCRIPT), "--out", str(out), "--build-venv", str(build_venv)]
        ).returncode
        if rc != 0:
            print(f"[installer] runtime 重建失败（exit={rc}）。", file=sys.stderr)
            return rc
    if not runtime_exe.is_file():
        print(
            f"[installer] 运行时产物缺失：{runtime_exe}\n"
            "  请先构建冻结运行时（--build-runtime），或确认 --out 指向正确的 build-out。",
            file=sys.stderr,
        )
        return 2

    # ②a runtime-manifest 校验（fail-closed）——
    # 防把与 manifest 漂移的旧/残 onedir 静默编进 Setup。本校验不被 --skip-runtime-version-check 旁路。
    manifest_ok, manifest_detail = verify_runtime_manifest(runtime_dir)
    if not manifest_ok:
        print(f"[installer] {manifest_detail}", file=sys.stderr)
        return 2
    print(f"[installer] {manifest_detail}")

    # ②b 在线本地模型三件校验（fail-closed；跳过 --build-runtime 打包旧 onedir 时兜底）
    missing = verify_local_model_assets(runtime_dir)
    if missing:
        print(
            f"[installer] frozen 缺少在线本地模型三件（{', '.join(missing)}）。"
            "请先重建 runtime（--build-runtime）。",
            file=sys.stderr,
        )
        return 2

    # ②c MCP 真协议自检（批，fail-closed）：跑 BioDataAgentMCP.exe --selfcheck
    # 且输出含 SELFCHECK_OK——防把 MCP 依赖缺失的旧/残 onedir 静默编译进 Setup。
    mcp_ok, mcp_detail = verify_mcp_selfcheck(runtime_dir)
    if not mcp_ok:
        print(f"[installer] {mcp_detail}\n请先重建 runtime（--build-runtime）。", file=sys.stderr)
        return 2
    print(f"[installer] {mcp_detail}")

    # ③ exe 真实版本比对（fail-closed；缺省开启）
    if args.skip_runtime_version_check:
        # 旁路要醒目警告：只关 exe /api/health 版本比对，**不旁路**
        # ②a 的 runtime-manifest 对账与 ②c 的 MCP 自检——正式发布不得带本旁路。
        print(
            "[installer] !!! 警告：--skip-runtime-version-check 已启用，跳过了 exe /api/health 版本比对，"
            "可能把运行版本与 webapp.py 单一真源不一致的运行时编进安装器。仅限调试/CI/受控验证，"
            "正式发布物严禁使用本旁路。runtime-manifest 对账与 MCP 自检仍生效。",
            file=sys.stderr,
        )
    else:
        try:
            check_runtime_version(runtime_exe, version)
        except RuntimeError as exc:
            print(f"[installer] {exc}", file=sys.stderr)
            return 2

    # ④ ISCC 编译
    if not _ISCC.is_file():
        print(f"[installer] 未找到 ISCC：{_ISCC}（需安装 Inno Setup 6，官方 jrsoftware.org）", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    base = f"BioData-Agent-Setup-{version}-win-x64-unsigned-dev"
    final_exe = out / f"{base}.exe"
    iscc_log = out / f"iscc-{version}.log"
    cmd = [
        str(_ISCC),
        f"/DAppVersion={version}",
        f"/DRuntimeDir={runtime_dir}",
        f"/O{out}",
        f"/F{base}",
        str(_ISS),
    ]
    print(f"[installer] ISCC：{' '.join(cmd)}")
    if args.skip_build:
        print("[installer] --skip-build：跳过实际编译（检查通过）。")
    else:
        # ISCC 无 /LOG 开关（那是安装器运行期参数）；编译输出落 iscc 日志供审计
        with open(iscc_log, "w", encoding="utf-8") as logf:
            result = subprocess.run(cmd, cwd=str(_REPO_ROOT), stdout=logf, stderr=subprocess.STDOUT)
        if result.returncode != 0:
            print(f"[installer] ISCC 编译失败（exit={result.returncode}）；日志：{iscc_log}", file=sys.stderr)
            return result.returncode
        if not final_exe.is_file():
            print(f"[installer] 编译声称成功但产物缺失：{final_exe}", file=sys.stderr)
            return 2

        # ⑤ SHA-256 sidecar
        digest = sha256_file(final_exe)
        sidecar = final_exe.with_suffix(".exe.sha256")
        sidecar.write_text(f"{digest}  {final_exe.name}\n", encoding="utf-8")
        size_mib = final_exe.stat().st_size / 1048576
        print(f"[installer] 完成：{final_exe}")
        print(f"[installer] 体积：{size_mib:.1f} MiB；SHA-256：{digest}；sidecar：{sidecar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
