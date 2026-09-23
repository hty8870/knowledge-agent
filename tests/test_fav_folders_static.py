# -*- coding: utf-8 -*-
"""收藏夹功能静态契约钉（Round A ·）。

背景与 test_frontend_namespacing.py 同型：三门（node --check / web_smoke / pytest 后端）都不执行
前端 JS，「popover 文案被改没」「新 localStorage 键忘裹 nsKey」「删夹确认被删成一键」这类回归
只能在浏览器里静默发生。这里把规格的关键不变量静态钉死：

- 新 per-account 键 LS.favFolders 已定义，且读写全部经 nsKey（与 namespacing 门同口径复查）；
- fav 条目 folder 字段缺省容忍（老数据无 folder → 默认夹）有统一防御性读取；
- 心形 popover / 收藏夹 tab / 管理面板的关键文案 token 存在；
- 删除收藏夹是二段确认（armed 模式），且删夹后条目归回默认夹；
- 复用清单条有范围下拉、陈旧指纹含范围维度（dataset.f），范围名只展示不进 POST 体（keys-only）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "web" / "static" / "js"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_fav_folders_key_defined_and_namespaced() -> None:
    """LS.favFolders 必须定义，且 getFavFolders/setFavFolders 的读写都裹 nsKey。"""
    core = _read("web/static/js/core/core.js")
    assert 'favFolders: "biodata_fav_folders_v1"' in core, "core.js 未定义 LS.favFolders"
    assert re.search(r"function getFavFolders\(\)", core), "core.js 缺 getFavFolders"
    assert "readJSON(nsKey(LS.favFolders), [])" in core, "getFavFolders 读取未裹 nsKey"
    assert "writeJSON(nsKey(LS.favFolders), a)" in core, "setFavFolders 写入未裹 nsKey"
    # 反向守卫：任何对 LS.favFolders 的裸读写都不允许（与 namespacing 门同判据，钉死在本功能测试里）
    assert "readJSON(LS.favFolders" not in core and "writeJSON(LS.favFolders" not in core
    assert "localStorage.getItem(LS.favFolders" not in core and "localStorage.setItem(LS.favFolders" not in core


def test_folder_field_default_tolerant() -> None:
    """老收藏条目没有 folder 字段：必须有统一防御性读取（缺省 → 默认夹 ""），normalizeItem 也带出该字段。"""
    core = _read("web/static/js/core/core.js")
    assert "function favFolderOf(it)" in core, "缺 favFolderOf 统一防御性读取"
    assert re.search(r'function favFolderOf\(it\)\s*\{\s*return String\(\(it && it\.folder\) \|\| ""\)', core), (
        "favFolderOf 未做缺省容忍（老数据无 folder 字段会散落出 undefined）"
    )
    assert 'folder: String(it.folder || "")' in core, "normalizeItem 未携带 folder 字段"
    # 已删除/未知夹 id 的归一（分组、过滤、复用范围共用），防止悬空 id 造出幽灵分组
    assert "function favFolderIdOrDefault(id)" in core


def test_folder_crud_and_name_validation() -> None:
    """收藏夹 CRUD 齐备；名字校验：非空、≤20 字、不与既有夹（含默认夹）重名；删夹条目归默认夹。"""
    core = _read("web/static/js/core/core.js")
    for fn in ("addFavFolder", "renameFavFolder", "deleteFavFolder", "moveFavToFolder"):
        assert re.search(rf"function {fn}\(", core), f"core.js 缺 {fn}"
    assert 'DEFAULT_FAV_FOLDER_NAME = "默认收藏夹"' in core
    v = _read("web/static/js/core/core.js")
    assert "n.length > 20" in v, "名字长度校验（≤20 字）缺失"
    assert "已有同名收藏夹" in v and "不能和默认收藏夹重名" in v
    # 删除夹 → 该夹条目 folder 改回 ""（归默认夹），条目本身保留
    m = re.search(r"function deleteFavFolder\(id\)\s*\{(.*?)\n\}", v, re.DOTALL)
    assert m and 'it.folder = ""' in m.group(1), "deleteFavFolder 未把条目归回默认夹"


def test_heart_popover_contract() -> None:
    """心形点击走 popover（不再直接 toggle）；未收藏=选夹（默认夹置顶+行内新建），已收藏=取消/移到。"""
    cards = _read("web/static/js/search/cards.js")
    assert "openFavPopover(favBtn, it)" in cards, "cards.js 心形未接 popover"
    ff = _read("web/static/js/panel/fav_folders.js")
    for token in ("默认收藏夹", "＋ 新建收藏夹", "取消收藏", "移到收藏夹"):
        assert token in ff, f"fav_folders.js 缺 popover 文案：{token}"
    assert "已收藏到「" in ff and "已移到「" in ff, "缺收藏/移动 toast 文案"
    # Esc / 点外关闭 = 不收藏（无副作用关闭路径必须存在）
    assert '"Escape"' in ff and "_onFavPopoverDocClick" in ff


def test_favorites_view_tabs_and_manage_panel() -> None:
    """收藏视图：tab 条（全部/默认夹/各用户夹）+ 管理面板（重命名行内、删除二段确认、新建行）。"""
    html = _read("web/static/index.html")
    for token in ('id="favFolderBar"', 'id="favFolderTabs"', 'id="favFolderManageBtn"', 'id="favFolderManage"', "管理收藏夹"):
        assert token in html, f"index.html 收藏视图缺骨架：{token}"
    ff = _read("web/static/js/panel/fav_folders.js")
    assert '"全部"' in ff or ">全部<" in ff or 'name: "全部"' in ff, "缺「全部」tab"
    # 删除二段确认必须走通用核，文案不在功能模块里再抄一份。
    assert "armTwoStepConfirm" in ff, "删夹缺通用二段确认"
    assert "confirmDelete" in _read("web/static/js/core/copy.js")
    assert "已归到默认收藏夹" in ff, "删夹后未提示条目归默认夹"
    # tab 选中态不持久化：存在复位函数，且 shell/accounts 都接入
    assert "function resetFavFolderState()" in ff
    shell = _read("web/static/js/core/shell.js")
    assert "resetFavFolderState" in shell, "showView 进收藏视图未复位 tab 态"
    accounts = _read("web/static/js/panel/accounts.js")
    assert "resetFavFolderState" in accounts and "resetReuseScope" in accounts, "换账户未复位收藏夹/复用范围态"


def test_reuse_bar_scope_contract() -> None:
    """复用清单条：范围下拉（全部收藏+各夹）；按钮文案随范围；陈旧指纹含范围维 dataset.f；范围名只展示。"""
    html = _read("web/static/index.html")
    assert 'id="reuseScope"' in html, "复用条缺范围下拉"
    rp = _read("web/static/js/act/reuse_pack.js")
    assert "全部收藏" in rp, "范围下拉缺「全部收藏」选项"
    assert "dataset.f" in rp, "陈旧指纹缺范围维度（同账户同条数换夹必须判陈旧）"
    assert "生成投稿材料（${reuseScopeLabel()}" in rp, "按钮文案未随范围变化"
    # keys-only 红线：POST 体仍只有 uids（范围名不得进请求体）
    assert "body: JSON.stringify({ uids })" in rp, "reuse-pack 请求体不再是 keys-only"
    assert "范围：" in rp, "面板顶部缺范围回显"


def test_fav_folders_script_registered_before_boot() -> None:
    """新模块必须注册进 index.html 且插在 boot.js 前（加载顺序不变量）。"""
    html = _read("web/static/index.html")
    scripts = [s.split("/")[-1] for s in re.findall(
        r'<script(?:\s+type="module")?\s+src="/static/js/([A-Za-z0-9_/-]+)\.js(?:\?[^"]*)?"', html)]
    assert "fav_folders" in scripts, "fav_folders.js 未注册进 index.html"
    assert scripts[0] == "core" and scripts[-1] == "boot"
    assert scripts.index("fav_folders") < scripts.index("boot")
