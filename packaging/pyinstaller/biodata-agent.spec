# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir spec —— BioData Agent 冻结运行时（含启动器入口与品牌图标收口）。

产物：dist/BioDataAgent/ 一键目录
  - BioDataAgent.exe      （windowed  Web 服务入口，entry_web.py → desktop_launcher.main 薄转发）
  - BioDataAgentMCP.exe   （console   MCP stdio 服务器入口，mcp_server.py）
  - _internal/            （Python 运行时 + 依赖 + 随包静态资源；= sys._MEIPASS）

要点：
- pathex=src：`dataset_recommender` 作为顶层包进 PYZ，模块 `__file__` 落在
  _MEIPASS/dataset_recommender/... → 包内 `data/`（download_links/inspection/
  sample_supplement）datas 目标必须为 `dataset_recommender/data/...`（模块
  `__file__`-relative，见 corpus/downloads.py:18），其余按 repo-root-relative
  （resource_root/_MEIPASS + rel）落 `database/`、`prompts/`、`web/`、`使用教程/`。
- excludes 钉死重依赖（torch/sentence-transformers/modelscope/scanpy/...）：
  全部调用点函数级 import + try/except 降级（实测抽查 vector_recall.py:93），排除安全。
- --collect-all mcp/langgraph/langchain_openai/langchain_core/openai（collect_all
  同时带 copy_metadata → mcp_server `importlib.metadata.version("mcp")` 可用）。
- --noupx：upx=False（构建机无 UPX 且避免压缩误伤）。
- 入口：Web exe 改走 `desktop_launcher.main`（启动器契约 1 落地）——entry_web.py
  是薄转发（import + raise SystemExit(main())），托盘/单实例/固定端口/attach 全部由
  启动器接管；frozen 冒烟 `--tray-selfcheck` 输出 TRAY_SELFCHECK OK（冒烟验收在案）。
- 图标：两个 EXE 都内嵌 packaging/assets/BioDataAgent.ico（品牌资产，
  安装器/快捷方式侧已用同一 ico 兜底；exe 资源图标为安装器验收项 1）。
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

_REPO_ROOT = Path(SPECPATH).resolve().parents[1]  # packaging/pyinstaller → 仓库根
_SRC = str(_REPO_ROOT / "src")
_ENTRY_WEB = str(_REPO_ROOT / "packaging" / "pyinstaller" / "entry_web.py")
_ENTRY_MCP = str(_REPO_ROOT / "src" / "dataset_recommender" / "app" / "mcp_server.py")
_ICON = str(_REPO_ROOT / "packaging" / "assets" / "BioDataAgent.ico")
# PerMonitorV2 DPI-aware 应用清单（安装器边缘修复第 4 项）：高 DPI（150%/200% 缩放）下
# 让 pywebview 壳按显示器逐级缩放，而不是被系统整体拉伸导致位图/字体发虚。由 PyInstaller
# 在构建期读入并嵌入 Web exe 资源，无需进入 datas（运行时不可见）。
_DPI_MANIFEST = str(_REPO_ROOT / "packaging" / "pyinstaller" / "dpi-aware.manifest")
_UV = Path(sys.executable).resolve().parent / "uv.exe"
if not _UV.is_file():
    raise SystemExit(f"build venv 缺少 hash-locked uv.exe：{_UV}")

# ── 随包静态资源（datas 目标 = bundle 内相对 sys._MEIPASS 的路径）──────────────────
# ① repo-root-relative：database/base（冻结基准）、database/external（官方快照，**白名单**
#    逐个打包，绝不整目录递归——否则 .gitignore 的用户上传 upload_*.json（本地用户数据）会被
#    塞进安装包）、
#    prompts/*.md（7 个）、web/static/**（含 index.html/dataset.html，前端静态挂载点）、
#    使用教程/数据集上传/数据集上传规范.md（/spec/upload 端点）、
#    使用教程/MCP安装/ 与 使用教程/Skill安装/（安装版用户 MCP 接入教程 + agent 接入提示词，
#    /api/guide/agent-prompt 读取侧）、.agents/skills/biodata-dataset-discovery/
#    （随包 skill 副本，/api/guide/skill.zip 打包侧与安装版用户安装 skill 的来源）。
# ② 包内模块 __file__-relative：src/dataset_recommender/data/ 三个只读台账（downloads.py /
#    inspection.py / sample_supplement.py 的 _DEFAULT 均按 parents[1] 推导）→ 目标
#    dataset_recommender/data/...。**不打** data/inspection/snapshots/（运行时无读取点）。
# ③ 明确不打：models/、.env、tests/eval/services/docs 等非运行时目录。
# ── database/external 官方快照白名单 ────────────────────────────────────────────────
# 此前 datas 把 external 目录**整体递归**打进——用户上传的
# gitignored upload_*.json 会被一起冻进安装包（本地用户数据外泄）。这里改成**显式白名单**：只打包
# git 跟踪的官方快照这批；未列入的（upload_*.json 等）一律不进包。清单与 `git ls-files
# database/external/` 逐项核对（README.md + 10 个官方快照 JSON = 11 文件）。
_EXTERNAL_SNAPSHOTS = (
    "README.md",
    "arrayexpress.json",
    "cellxgene.json",
    "ebi_scea.json",
    "encode.json",
    "geo.json",
    "hca.json",
    "hubmap.json",
    "refinebio.json",
    "single_cell_portal.json",
    "zenodo.json",
)

_DATAS = [
    (str(_REPO_ROOT / "database" / "base"), "database/base"),
    *[(str(_REPO_ROOT / "database" / "external" / _n), "database/external") for _n in _EXTERNAL_SNAPSHOTS],
    (str(_REPO_ROOT / "prompts"), "prompts"),
    (str(_REPO_ROOT / "web" / "static"), "web/static"),
    (str(_REPO_ROOT / "使用教程" / "数据集上传" / "数据集上传规范.md"), "使用教程/数据集上传"),
    (str(_REPO_ROOT / "使用教程" / "MCP安装"), "使用教程/MCP安装"),
    (str(_REPO_ROOT / "使用教程" / "Skill安装"), "使用教程/Skill安装"),
    (str(_REPO_ROOT / ".agents" / "skills" / "biodata-dataset-discovery"),
     ".agents/skills/biodata-dataset-discovery"),
    (str(_REPO_ROOT / "src" / "dataset_recommender" / "data" / "download_links.by_uid.json"),
     "dataset_recommender/data"),
    (str(_REPO_ROOT / "src" / "dataset_recommender" / "data" / "inspection" / "current.json"),
     "dataset_recommender/data/inspection"),
    (str(_REPO_ROOT / "src" / "dataset_recommender" / "data" / "sample_supplement.by_uid.json"),
     "dataset_recommender/data"),
    # 桌面壳窗口图标（2026-08-21 壳批）：webview_shell._set_window_icon_win32 在 frozen 下
    # 从 _MEIPASS/assets/ 取（source 模式另从 packaging/assets/ 取）。exe 图标另有 _ICON。
    (str(_REPO_ROOT / "packaging" / "assets" / "BioDataAgent.ico"), "assets"),
    # 在线可选本地模型组件：uv 是单文件下载/安装器；lock 逐包 hash；worker 在隔离 venv
    # 中运行，重依赖不进入主进程。基础安装不勾选时三者只占约 uv 单文件体积、零联网。
    (str(_UV), "tools"),
    (str(_REPO_ROOT / "packaging" / "requirements" / "model-win-x64.lock"), "packaging/requirements"),
    (str(_REPO_ROOT / "src" / "dataset_recommender" / "retrieval" / "model_worker.py"), "tools"),
]

# ── 动态/命名空间导入包：collect_all（含 copy_metadata）───────────────────────────
# 过滤 mcp.cli：其模块级 `import typer` 缺失时 sys.exit(1) 会杀死收集子进程（typer 是
# mcp 的 CLI 可选依赖，不进运行时锁；server/client stdio 传输不需要它）。
# filter_submodules 只收窄模块（pure）——mcp 包的 data 文件（mcp/cli/**，约 25K）
# 仍会进 collect_all 的 datas 里，这里按目标前缀再过滤一层，与上方注释对齐实物。
_MCP_DATAS, _MCP_BINS, _MCP_HIDDEN = collect_all(
    "mcp", filter_submodules=lambda n: not n.startswith("mcp.cli"))
_MCP_DATAS = [d for d in _MCP_DATAS if not d[1].replace("\\", "/").startswith("mcp/cli/")]
_LG_DATAS, _LG_BINS, _LG_HIDDEN = collect_all("langgraph")
_LCO_DATAS, _LCO_BINS, _LCO_HIDDEN = collect_all("langchain_openai")
_LC_DATAS, _LC_BINS, _LC_HIDDEN = collect_all("langchain_core")
_OPENAI_DATAS, _OPENAI_BINS, _OPENAI_HIDDEN = collect_all("openai")
# ── webview（pywebview 5.4）桌面壳 ───────────────────────────────────────────────
# 2026-08-24：modulegraph 只收代码不收 package data/binary——Windows 壳依赖
# webview/lib 下的 WebView2Loader.dll 等二进制与数据文件，仅靠 `import webview` 残留的
# 纯模块收集会缺壳。hooks-contrib 的 hook-webview.py 本会补收 data+bin，但它的触发依赖
# modulegraph 已捕获 `import webview`，且其存在/版本随 hooks-contrib 漂移——这里显式
# collect_all 钉死（连带 copy_metadata），不依赖运气（08-21 e04a236 能出窗是当时 venv 有全链
# + hooks 顺带；本次把「静态收集 + frozen 壳探针实测」双双 fail-closed。pythonnet/clr_loader
# 是 webview 的运行时导入，modulegraph 会沿 webview 纯模块收编，且 hooks-contrib 已有
# hook-clr.py / hook-clr_loader.py，无需在此重复收集）。
_WV_DATAS, _WV_BINS, _WV_HIDDEN = collect_all("webview")

_DATAS += _MCP_DATAS + _LG_DATAS + _LCO_DATAS + _LC_DATAS + _OPENAI_DATAS

# ── 排除清单（①）：重依赖不随包，运行时函数级 import + try/except 降级 ─────────
_EXCLUDES = [
    "torch", "torchvision", "torchaudio", "sentence_transformers", "transformers",
    "tokenizers", "huggingface_hub", "safetensors", "accelerate", "onnxruntime",
    "modelscope", "playwright", "pytest", "SQLAlchemy", "psycopg2", "scanpy",
    "anndata", "h5py", "matplotlib", "scipy", "pandas", "numpy",
]


# ── 领域包 hiddenimports（2026-09-23 骨肉分离批）：默认领域包经 `domain.registry`
# 字符串动态装载，modulegraph 摸不到这条边——两个 EXE 都必须显式收集，否则 frozen 首调
# get_domain() 即 ModuleNotFoundError。外部领域包（BIODATA_DOMAIN_DIR）按目录读盘、无需收集。
_DOMAIN_HIDDEN = [
    "dataset_recommender.domains",
    "dataset_recommender.domains.biodata",
    "dataset_recommender.domains.biodata.pack",
    "dataset_recommender.domains.biodata.dimensions",
    "dataset_recommender.domains.biodata.vocabulary",
]


# ═══════════════════ Web（BioDataAgent.exe，windowed）═══════════════════
_a_web = Analysis(
    [_ENTRY_WEB],
    pathex=[_SRC],
    binaries=_WV_BINS,                       # webview 随包 .dll/.so（Web 入口专用；MCP 不依赖壳）
    datas=_DATAS + _WV_DATAS,                # webview/lib 数据（WebView2Loader.dll 等）
    hiddenimports=["dataset_recommender.app.webapp"] + _WV_HIDDEN + _DOMAIN_HIDDEN,  # webview 子模块；uvicorn.run 字符串导入兜底
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_EXCLUDES,
    noarchive=False,
    optimize=0,
)
_pyz_web = PYZ(_a_web.pure)

_exe_web = EXE(
    _pyz_web,
    _a_web.scripts,
    [],
    exclude_binaries=True,
    name="BioDataAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed：无控制台窗口；stdout/stderr 由启动器空流守卫接管
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_ICON,
    manifest=_DPI_MANIFEST,   # PerMonitorV2 DPI-aware（高 DPI 缩放不拉伸壳内容）
)

# ═══════════════════ MCP（BioDataAgentMCP.exe，console / stdio）═══════════════════
_a_mcp = Analysis(
    [_ENTRY_MCP],
    pathex=[_SRC],
    binaries=[],
    datas=[],
    hiddenimports=_MCP_HIDDEN + _LG_HIDDEN + _LCO_HIDDEN + _LC_HIDDEN + _OPENAI_HIDDEN + _DOMAIN_HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_EXCLUDES,
    noarchive=False,
    optimize=0,
)
_pyz_mcp = PYZ(_a_mcp.pure)

_exe_mcp = EXE(
    _pyz_mcp,
    _a_mcp.scripts,
    [],
    exclude_binaries=True,
    name="BioDataAgentMCP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # stdio MCP：必须 console，子进程/客户端经标准流通信
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_ICON,
)

# ═══════════════════ COLLECT：两个 EXE 共享同一 _internal（依赖去重）════════════════
_coll = COLLECT(
    _exe_web,
    _a_web.binaries,
    _a_web.datas,
    _exe_mcp,
    _a_mcp.binaries,
    _a_mcp.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="BioDataAgent",
)
