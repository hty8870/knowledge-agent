# -*- coding: utf-8 -*-
"""PyInstaller onedir 冻结运行时静态契约（spec / 锁文件 / 入口 / manifest）。

本测试只读仓库内文件（+ 仓库外构建产物 manifest，缺失则跳过），不执行构建、不联网：
- spec 的 datas/excludes/双 EXE 结构钉字 + Web exe 图标内嵌；
- runtime-win-x64.lock / build-win-x64.lock 的钉版 + SHA-256 格式；
- 运行时锁不含被排除的重依赖（pytest/numpy/pandas/scipy/torch/...）；
- entry_web.py（薄转发 desktop_launcher.main）与 mcp_server.py 的 frozen 入口改动；
- 构建产物 runtime-manifest.json 的形状（相对路径 + 大小 + SHA-256，无绝对用户路径）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "pyinstaller" / "biodata-agent.spec"
ENTRY_WEB = ROOT / "packaging" / "pyinstaller" / "entry_web.py"
MCP = ROOT / "src" / "dataset_recommender" / "app" / "mcp_server.py"
RUNTIME_IN = ROOT / "packaging" / "requirements" / "runtime-win-x64.in"
RUNTIME_LOCK = ROOT / "packaging" / "requirements" / "runtime-win-x64.lock"
BUILD_IN = ROOT / "packaging" / "requirements" / "build-win-x64.in"
BUILD_LOCK = ROOT / "packaging" / "requirements" / "build-win-x64.lock"
MODEL_IN = ROOT / "packaging" / "requirements" / "model-win-x64.in"
MODEL_LOCK = ROOT / "packaging" / "requirements" / "model-win-x64.lock"

REQUIRED_EXCLUDES = {
    "torch", "torchvision", "torchaudio", "sentence_transformers", "transformers",
    "tokenizers", "huggingface_hub", "safetensors", "accelerate", "onnxruntime",
    "modelscope", "playwright", "pytest", "SQLAlchemy", "psycopg2", "scanpy",
    "anndata", "h5py", "matplotlib", "scipy", "pandas", "numpy",
}

REQUIRED_DATAS_TARGETS = {
    "database/base", "database/external", "prompts", "web/static",
    "使用教程/数据集上传", "dataset_recommender/data", "dataset_recommender/data/inspection",
    "assets",   # 桌面壳窗口图标（壳批）：webview_shell 在 frozen 下从 _MEIPASS/assets/ 取
    # 安装版用户 MCP 接入教程（/api/guide/agent-prompt 读取侧）与随包 skill
    # 副本（/api/guide/skill.zip 打包侧 + Skill 安装教程的安装版来源）必须随装。
    "使用教程/MCP安装", "使用教程/Skill安装", ".agents/skills/biodata-dataset-discovery",
}

# 明确不打的内容：这些路径出现在 spec 的 datas 里即为契约违例。
# （"research" 覆盖迁移前的 "workstream"：research 是流水线归档的顶层目录。）
FORBIDDEN_DATAS_SOURCES = (
    "research", "inspection/snapshots", "/models", ".env",
    "/tests", "/eval", "/services", "/docs", "/协同", "开发日志",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _lock_packages(lock: Path) -> dict[str, list[str]]:
    """解析 lock：{规范化包名: [hash...]}。只处理钉版行与其后紧跟的 --hash 行。"""
    lines = _text(lock).splitlines()
    pkgs: dict[str, list[str]] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line or line.startswith("#"):
            i += 1
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)\s*\\?$", line)
        if m:
            name, version = m.group(1), m.group(2)
            assert version, f"{lock.name}: {name} 缺少钉版版本"
            hashes: list[str] = []
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith("--hash="):
                hm = re.match(r"\s*--hash=sha256:([0-9a-f]{64})\s*\\?$", lines[j])
                assert hm, f"{lock.name}: {name} 的 hash 行格式不符：{lines[j]!r}"
                hashes.append(hm.group(1))
                j += 1
            assert hashes, f"{lock.name}: {name} 没有 SHA-256 hash（需 --require-hashes）"
            assert name not in pkgs, f"{lock.name}: {name} 重复钉版"
            pkgs[name] = hashes
            i = j
            continue
        # 允许 # via 注释与 uv 头注释；其余行视为格式违例
        assert line.strip().startswith("#"), f"{lock.name}: 无法解析的行：{line!r}"
        i += 1
    assert pkgs, f"{lock.name}: 没有解析到任何钉版包"
    return pkgs


# ─────────────────────────────── spec 结构 ───────────────────────────────

def test_spec_exists_and_has_dual_exe_structure():
    spec = _text(SPEC)
    assert 'name="BioDataAgent"' in spec and 'name="BioDataAgentMCP"' in spec
    # windowed Web exe + console MCP exe（stdio 需要 console）
    assert 'console=False' in spec and 'console=True' in spec
    # --noupx
    assert re.search(r"(?m)^\s*upx=False", spec)
    # COLLECT 共享依赖（onedir）
    assert re.search(r"(?m)^_coll = COLLECT", spec)
    assert "exclude_binaries=True" in spec


def test_spec_pathex_points_at_src():
    spec = _text(SPEC)
    assert re.search(r"(?m)^_SRC = str\(_REPO_ROOT / \"src\"\)", spec)
    assert re.search(r"(?m)^\s*pathex=\[_SRC\],", spec)


def test_spec_datas_pinned_and_exclusions_absent():
    spec = _text(SPEC)
    for target in REQUIRED_DATAS_TARGETS:
        assert f'"{target}"' in spec, f"datas 缺少目标：{target}"
    # 只检查 _DATAS 元组块（注释里允许出现这些词做说明）
    block = spec[spec.index("_DATAS = ["):spec.index("]\n", spec.index("_DATAS = ["))]
    for needle in FORBIDDEN_DATAS_SOURCES:
        assert needle not in block, f"spec datas 不得包含：{needle}"


def test_spec_excludes_pinned():
    spec = _text(SPEC)
    for name in REQUIRED_EXCLUDES:
        assert name in spec, f"excludes 缺少：{name}"


def test_spec_database_external_is_whitelist_not_recursive():
    """database/external 必须**白名单**（只打包官方快照），
    不得整目录递归——否则 gitignored 的用户上传 upload_*.json（本地用户数据）会被一起冻进安装包。
    钉两件事：
      1. 存在显式白名单常量 `_EXTERNAL_SNAPSHOTS`；
      2. `_DATAS` 块里不再出现旧瑕疵的「整目录源 → database/external」元组，且白名单以
         逐文件 dest 形式引用（`_REPO_ROOT / "database" / "external" / <名>`）。
    """
    spec = _text(SPEC)
    assert "_EXTERNAL_SNAPSHOTS" in spec, "缺少 database/external 白名单常量"
    block = spec[spec.index("_DATAS = ["):spec.index("]\n", spec.index("_DATAS = ["))]
    # 旧瑕疵：把 external 目录作为源整体打进（形如 `(... "external"), "database/external")`）。
    assert not re.search(r'"external"\s*\),\s*"database/external"', block), \
        "database/external 仍被整目录递归打包——白名单化未落地"
    # 白名单版：_DATAS 里逐文件源引用则存在（`"database" / "external" / <name>`）。
    assert re.search(r'"external"\s*/\s*[a-zA-Z_]+', block), \
        "database/external 白名单逐文件条目缺失"


def test_spec_collect_all_covers_dynamic_packages():
    spec = _text(SPEC)
    for pkg in ("mcp", "langgraph", "langchain_openai", "langchain_core", "openai"):
        assert re.search(rf'collect_all\(\s*"{pkg}"', spec), f"缺少 collect_all({pkg})"
    # mcp.cli 需被过滤（其模块级 import typer 失败会杀收集子进程）
    assert "mcp.cli" in spec
    # collect_all 自带 copy_metadata → mcp_server importlib.metadata.version("mcp") 可用
    assert "collect_all" in spec


def test_spec_icon_set_to_brand_asset():
    # 安装器验收项 1 —— 两个 EXE 都内嵌品牌图标
    spec = _text(SPEC)
    assert re.search(r"(?m)^\s*icon=_ICON,", spec)
    assert re.search(r"BioDataAgent\.ico", spec)
    # 图标必须是两个 EXE 块各自出现（Web + MCP）
    assert spec.count("icon=_ICON") >= 2


def test_spec_web_exe_declares_per_monitor_v2_dpi_manifest():
    """边缘修复第 4 项：Web EXE 内嵌 PerMonitorV2 DPI-aware manifest，且清单文件存在、
    声明 PerMonitorV2 + true/pm（150%/200% 高 DPI 下不拉伸 pywebview 壳）。"""
    spec = _text(SPEC)
    assert "_DPI_MANIFEST" in spec
    assert "manifest=_DPI_MANIFEST" in spec
    assert "dpi-aware.manifest" in spec
    manifest_path = ROOT / "packaging" / "pyinstaller" / "dpi-aware.manifest"
    assert manifest_path.is_file(), f"缺少 DPI manifest：{manifest_path}"
    manifest = manifest_path.read_text(encoding="utf-8")
    assert "PerMonitorV2" in manifest
    assert "dpiAwareness" in manifest
    assert "true/pm" in manifest


# ─────────────────────────────── 锁文件格式 ───────────────────────────────

def test_runtime_lock_pinned_with_hashes_and_top_levels_present():
    pkgs = _lock_packages(RUNTIME_LOCK)
    for top in ("fastapi", "uvicorn", "python-multipart", "httpx", "python-dotenv",
                "mcp", "langgraph", "langchain-core", "langchain-openai"):
        assert top in pkgs, f"runtime 锁缺少顶层依赖：{top}"
    assert "mcp==1.28.1" in _text(RUNTIME_LOCK)


def test_runtime_lock_excludes_heavy_deps():
    pkgs = _lock_packages(RUNTIME_LOCK)
    for heavy in ("pytest", "numpy", "pandas", "scipy", "torch", "torchvision",
                  "sentence-transformers", "transformers", "modelscope",
                  "playwright", "matplotlib", "scanpy", "anndata", "h5py"):
        assert heavy not in pkgs, f"runtime 锁不得含重依赖：{heavy}"


def test_build_lock_carries_pinned_uv_for_optional_model_installer():
    packages = _lock_packages(BUILD_LOCK)
    assert "uv" in packages and packages["uv"], "build venv 必须由 hash-lock 提供 uv.exe"
    assert "uv>=0.11.8,<0.12" in _text(BUILD_IN)


def test_model_component_lock_is_hash_pinned_cpu_wheel_only():
    source = _text(MODEL_IN)
    lock = _text(MODEL_LOCK)
    assert "sentence-transformers==3.4.1" in source and "modelscope==1.39.1" in source
    for package in ("sentence-transformers==3.4.1", "modelscope==1.39.1", "torch==2.13.0+cpu"):
        assert package in lock
    assert "--hash=sha256:" in lock
    assert "--only-binary :all:" in lock.splitlines()[1]


def test_build_lock_pinned_with_hashes():
    pkgs = _lock_packages(BUILD_LOCK)
    assert "pyinstaller" in pkgs and "pyinstaller-hooks-contrib" in pkgs


def test_in_files_list_top_level_only():
    # .in 是顶层声明（不钉版本，钉版在 .lock）；mcp 除外（教程固定 1.28.1 契约）
    runtime_in = _text(RUNTIME_IN)
    assert "mcp==1.28.1" in runtime_in
    for name in ("fastapi", "uvicorn", "python-multipart", "httpx", "python-dotenv",
                 "langgraph", "langchain-core", "langchain-openai"):
        assert re.search(rf"(?m)^{re.escape(name)}>=", runtime_in), f".in 缺少 {name}"


# ─────────────────────────────── frozen 入口改动 ───────────────────────────────

def test_entry_web_forwards_to_desktop_launcher_main():
    entry = _text(ENTRY_WEB)
    # 入口从 uvicorn.run 直启改为 desktop_launcher.main 薄转发（启动器契约 1 落地）
    assert "desktop_launcher" in entry
    assert "from dataset_recommender.app.desktop_launcher import main" in entry
    assert "raise SystemExit(main())" in entry
    # 不再直接 import uvicorn / webapp（启动器内部 _default_app() 惰性接管）
    assert "import uvicorn" not in entry
    assert "from dataset_recommender.app.webapp import app" not in entry


def test_mcp_selfcheck_spawns_self_when_frozen_and_guards_streams():
    mcp = _text(MCP)
    # selfcheck 子进程：frozen 分支自 spawn exe（无 __file__）
    assert 'if getattr(sys, "frozen", False):' in mcp
    assert "command, args = sys.executable, []" in mcp
    assert '["-B", str(Path(__file__).resolve())]' in mcp
    # __main__ 流守卫
    assert "if sys.stdout is None:" in mcp and "if sys.stderr is None:" in mcp


# ─────────────────────────────── runtime-manifest 形状 ───────────────────────────────

def test_runtime_manifest_shape_when_present():
    """构建产物 manifest（仓库外）存在时校验形状；缺失则跳过（门禁不依赖本地构建）。"""
    out = os.environ.get("BIODATA_BUILD_OUT")
    if not out:
        out = str(ROOT.parent / "build-out")  # 默认与 build_windows_runtime.py 一致
    manifest = Path(out) / "runtime-manifest.json"
    if not manifest.is_file():
        import pytest
        pytest.skip(f"构建产物 manifest 不存在（{manifest}）；静态契约无需构建即可验")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data.get("format") == "biodata-runtime-manifest/v1"
    assert data.get("app") == "BioDataAgent"
    files = data.get("files")
    assert isinstance(files, list) and files, "manifest.files 必须非空"
    assert data.get("file_count") == len(files)
    assert data.get("total_bytes", 0) > 0
    seen = set()
    for f in files:
        path = f.get("path")
        assert isinstance(path, str) and path, f"文件条目缺 path：{f}"
        assert path not in seen, f"重复条目：{path}"
        seen.add(path)
        # 相对路径：无盘符、无前导分隔符、无绝对用户路径
        assert not re.match(r"^[A-Za-z]:", path), f"manifest 含绝对路径：{path}"
        assert not path.startswith(("/", "\\")), f"manifest 含绝对路径：{path}"
        assert "\\" not in path, f"manifest 路径未用正斜杠：{path}"
        assert "Users" not in path and "ROG" not in path, f"manifest 泄漏用户路径：{path}"
        assert isinstance(f.get("size"), int) and f["size"] >= 0
        assert re.fullmatch(r"[0-9a-f]{64}", f.get("sha256", "")), f"sha256 缺失/非法：{path}"
    # 关键产物必须进 manifest
    names = {Path(p).name for p in seen}
    assert {"BioDataAgent.exe", "BioDataAgentMCP.exe"} <= names
    # 新随包三件（MCP 教程目录 / Skill 教程目录 / skill 副本）只在**新构建**的
    # manifest 中体现（旧构建 manifest 无此三者）。一旦 manifest 已含三者之一（= 新构建），
    # 就要求三者全齐——_DATAS 是原子加的三条，缺一说明 spec 与构建产物漂移。
    new_entries = {
        "使用教程/MCP安装", "使用教程/Skill安装", ".agents/skills/biodata-dataset-discovery",
    }
    if any(any(p.startswith(prefix) for p in seen) for prefix in new_entries):
        for prefix in new_entries:
            assert any(p.startswith(prefix) for p in seen), f"新构建 manifest 缺随包条目：{prefix}"


# ─────────────────────────────── 构建脚本 ───────────────────────────────

def test_build_script_outputs_outside_repo_and_generates_manifest():
    script = _text(ROOT / "scripts" / "build_windows_runtime.py")
    assert "PyInstaller" in script
    assert "runtime-manifest.json" in script
    assert "dist" in script and "workpath" in script
    # 产物默认落仓库外（父目录 build-out），且不写仓库 build/dist
    assert "build-out" in script
    assert "--distpath" in script and "--workpath" in script


def test_spec_bundles_uv_model_lock_and_physical_worker_without_heavy_modules():
    spec = _text(ROOT / "packaging" / "pyinstaller" / "biodata-agent.spec")
    assert '"uv.exe"' in spec and '"tools"' in spec
    assert "model-win-x64.lock" in spec and "model_worker.py" in spec
    assert '"torch"' in spec and '"sentence_transformers"' in spec and "excludes=_EXCLUDES" in spec
