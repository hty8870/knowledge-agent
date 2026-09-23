"""前端账户命名空间静态契约（三门测不出前端行为，这里静态钉死）。

背景：账户功能把「每用户私有」数据（收藏 / 历史 / 用户记忆）的 localStorage 键按登录账户
namespace（core.js 的 `nsKey(base)`），让共用一台电脑/浏览器的多人不互相看到对方数据。
上线后验证发现 `interactions.js` 清空历史误写**裸** `LS.hist`（绕过 nsKey）——
登录用户的历史清不掉、还误清匿名命名空间。根因是「某处直接用裸键」这类漏改三门测不出。

本测试把不变量钉死：**per-account 键（fav/favFolders/hist/memory）的每一次 localStorage 读写都必须经 `nsKey(...)`**。
机器级键（cfg/timeMode/sourceMode/sourcesOff/onboarding/sidebarWidth/**memoryEnabled**）刻意**不** namespace，
不在此约束内（memoryEnabled 是开关、随机器而非随账户）。
favFolders（收藏夹列表， 收藏夹功能新增）与 fav 同为 per-account 用户数据，同约束。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "web" / "static" / "js"

# 只审**真实存储访问**：readJSON / writeJSON / localStorage.getItem·setItem 的首参引用了
# per-account 键（fav/favFolders/hist/memory）。这样只查代码里的实际读写、不会误伤注释里提到键名的散文。
# 捕获组 1 = 可选的 `nsKey(`；命中却缺它 = 裸用 = 账户隔离漏洞。\b 收尾避免 LS.memory 吞 LS.memoryEnabled、
# LS.fav 吞 LS.favFolders（交替顺序不影响：fav 后随 \b 失败会回溯到 favFolders）。
_STORAGE_ACCESS = re.compile(
    r"(?:readJSON|writeJSON|localStorage\.(?:get|set)Item)\(\s*(nsKey\(\s*)?LS\.(fav|hist|memory|favFolders)\b"
)
# 注释剥离（// 行注释 + /* */ 块注释）——避免注释里引用代码样式的键访问造成误报。
_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _js_files() -> list[Path]:
    files = sorted(JS_DIR.glob("*/*.js"))
    assert files, "未找到任何前端 JS 模块"
    return files


def _strip_comments(text: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))


def test_per_account_storage_access_always_goes_through_nskey() -> None:
    """per-account 键（fav/favFolders/hist/memory）的每次 readJSON/writeJSON/localStorage 访问都必须经 nsKey——
    一处裸访问即账户隔离漏洞（登录用户数据落到匿名共享桶 / 清不掉自己的数据）。"""
    offenders: list[str] = []
    seen = 0
    for path in _js_files():
        code = _strip_comments(path.read_text(encoding="utf-8"))
        for m in _STORAGE_ACCESS.finditer(code):
            seen += 1
            if not m.group(1):   # 缺 nsKey( 包裹
                line_no = code.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.name}:~{line_no}: {m.group(0)}")
    assert seen > 0, "未见任何 per-account 存储访问——测试基线可能已失效"
    assert not offenders, (
        "发现未经 nsKey 的 per-account localStorage 访问（账户隔离漏洞）：\n"
        + "\n".join(offenders)
    )


def test_memory_enabled_switch_is_not_namespaced() -> None:
    """反向守卫：memoryEnabled 是机器级开关，刻意**不** namespace（若被误 nsKey 会破坏关闭态跨账户保留）。"""
    text = (JS_DIR / "panel" / "memory.js").read_text(encoding="utf-8")
    assert "LS.memoryEnabled" in text
    assert "nsKey(LS.memoryEnabled" not in text
    assert "nsKey( LS.memoryEnabled" not in text


def test_history_clear_targets_account_namespace() -> None:
    """回归：清空历史必须写 nsKey(LS.hist)，不得写裸 LS.hist（本轮 CONFIRMED 漏洞的直接钉死）。"""
    text = (JS_DIR / "core" / "interactions.js").read_text(encoding="utf-8")
    assert "writeJSON(nsKey(LS.hist), [])" in text
    assert "writeJSON(LS.hist, [])" not in text


def test_logout_verifies_server_response_before_clearing_state() -> None:
    """回归：登出必须校验响应（res.ok）后才清本地登录态，不得吞错误后无条件报成功。"""
    text = (JS_DIR / "panel" / "accounts.js").read_text(encoding="utf-8")
    m = re.search(r"async function accountLogout\(\)\s*\{(.*?)\n\}", text, re.DOTALL)
    assert m, "未找到 accountLogout 函数体"
    body = m.group(1)
    assert "res.ok" in body, "accountLogout 未检查响应 ok 即清登录态"
    # 不得再是「try{fetch}catch{} 后无条件置 null」的吞错误模式。
    assert "catch (_e) {}" not in body


def test_account_change_refreshes_results_view() -> None:
    """回归：账户切换后需重渲 query 视图结果（否则结果卡收藏心形仍显示上一账户态、跨账户泄漏）。"""
    text = (JS_DIR / "panel" / "accounts.js").read_text(encoding="utf-8")
    m = re.search(r"function onAccountChanged\(\)\s*\{(.*?)\n\}", text, re.DOTALL)
    assert m, "未找到 onAccountChanged 函数体"
    body = m.group(1)
    assert 'view === "query"' in body and "applyRecommendResult" in body, (
        "onAccountChanged 未在 query 视图刷新结果，结果卡收藏态会滞留上一账户"
    )
