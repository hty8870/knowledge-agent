"""前端 8 项修复 + fail-open benchfb 留痕的静态测试钉。

每条对应一项已落地的前端修复：构建标记、provider 改写提示、benchfb 并段白名单、
标注守卫内容指纹、介绍页 handoff 分键、记忆默认开、澄清改写锚定、识别预览失败可见。
这些模块与 DOM 耦合，沿用项目既有的静态门范式
（同 test_dataset_page_static.py / test_usage_telemetry_contract.py）：钉关键代码形态，
防回退。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------- 构建标记（单版本化）

def test_build_marker_meta_in_both_pages() -> None:
    """<meta name="biodata-build"> 标记保留（指纹契约用）；运行时不再做版本分叉。"""
    for rel in ("web/static/index.html", "web/static/dataset.html"):
        html = _read(rel)
        assert '<meta name="biodata-build" content="benchfb">' in html, f"{rel} 缺构建标记 meta"


def test_single_versioning_is_benchfb_constant() -> None:
    """单版本化：构建分叉废弃，行为恒取单一版本。

    - `isBenchfbBuild` 导出保留（onboarding.js 顶层调用它定默认分支）但**恒 true**；
    - 不探测 meta / 按钮 DOM（meta 探测、按钮兜底两段全部删除）；
    - `usageEnabled` 未表态默认开，不走 `isBenchfbBuild()` 分叉。
    """
    log = _read("web/static/js/core/usage_log.js")
    assert "export function isBenchfbBuild()" in log
    body = log.split("export function isBenchfbBuild()", 1)[1].split("}", 1)[0]
    assert "return true;" in body, "isBenchfbBuild 必须恒 true（单版本化）"
    # 分叉探测两段必须删除：meta 显式标记与按钮 DOM 兜底都不再参与判定
    assert 'document.querySelector(\'meta[name="biodata-build"]\')' not in log
    assert "getElementById(\"benchfbExportBtn\")" not in log
    assert "return isBenchfbBuild();" not in log, "usageEnabled 不得再走构建分叉（默认：开）"
    # onboarding 仍经 isBenchfbBuild 取默认分支，且不得再自探按钮 DOM
    onboarding = _read("web/static/js/core/onboarding.js")
    assert 'from "#usage_log"' in onboarding and "isBenchfbBuild()" in onboarding
    assert 'getElementById("benchfbExportBtn")' not in onboarding, "onboarding 不得再自探按钮 DOM"


# ---------------------------------------------------------------- provider 改写与健康探测留痕

def test_preset_rewrite_toasts_and_health_failure_warns() -> None:
    shell = _read("web/static/js/core/shell.js")
    assert "已为你把接入方式切到" in shell, "静默改写 provider 预设必须 toast 告知"
    assert 'console.warn("syncAgentAvailability' in shell, "health 探测失败必须 console.warn 留痕"


# ---------------------------------------------------------------- benchfb 跨轮并段白名单

def test_merge_back_limited_to_pack_verbs() -> None:
    """120s 窗口内只有打包系动作段允许并回上一轮记录，其余动作自立一条。"""
    src = _read("web/static/js/core/benchfb.js")
    assert "MERGE_BACK_VERB_RE" in src, "并段必须过动词白名单"
    assert "MERGE_BACK_VERB_RE.test(seg.verb)" in src, "白名单必须真的拦在并段条件里"


# ---------------------------------------------------------------- 标注守卫内容指纹

def test_mark_guard_compares_fingerprint_not_just_count() -> None:
    src = _read("web/static/js/core/benchfb.js")
    assert "_gridFingerprint" in src, "结果区内容指纹函数缺失"
    assert "_gridFingerprint(g) !== _markFingerprint" in src, (
        "MutationObserver 守卫必须比内容指纹——只比 children.length 挡不住同数量新结果"
    )


# ---------------------------------------------------------------- 介绍页 handoff 按 uid 分键

def test_handoff_keyed_by_uid_with_degraded_notice() -> None:
    cards = _read("web/static/js/search/cards.js")
    # 旧单槽保留（兼容），新增按 uid 分键主通道
    assert 'localStorage.setItem("biodata_dataset_view_v1"' in cards
    assert 'localStorage.setItem("biodata_dataset_view_v1:" + it.dataset_uid' in cards, (
        "handoff 必须按 uid 分键写入，多标签才不互相覆盖"
    )
    page = _read("web/static/js/search/dataset_page.js")
    assert 'DS_VIEW_KEY + ":" + params.uid' in page, "详情页必须优先按 uid 分键取回"
    assert "_dsHandoffDegraded" in page and "完整信息未取到，部分字段缺失" in page, (
        "降级为 URL 最小记录时必须在页头如实标注"
    )


# ---------------------------------------------------------------- 记忆默认开/机器级口径写明

def test_memory_default_and_machine_scope_documented() -> None:
    html = _read("web/static/index.html")
    assert "此开关为本机所有账户共用" in html, "设置页必须写明记忆开关的机器级口径"
    mem = _read("web/static/js/panel/memory.js")
    assert 'localStorage.getItem(LS.memoryEnabled) !== "0"' in mem, (
        "用户记忆默认开（!== \"0\"，缺失/异常归 true）——改默认须同步改本钉与设置页文案"
    )


# ---------------------------------------------------------------- 澄清改写锚定

def test_clarification_rewrite_is_anchored() -> None:
    results = _read("web/static/js/search/results.js")
    assert '"不要$2"' in results, "exclude 分支必须锚定 fastq 短语改写（保留捕获组）"
    assert 'q.replace(/不需要|无需|无须|不用|不必|没必要/g, "不要")' not in results, (
        "全局替换会误改原句其它位置的「不需要」——不得回退"
    )


# ---------------------------------------------------------------- 识别预览失败可见

def test_interpret_failure_is_observable() -> None:
    src = _read("web/static/js/core/interactions.js")
    assert 'console.warn("interpret preview unavailable"' in src, "失败日志从 debug 升为 warn"
    assert 'console.debug("interpret preview unavailable")' not in src
    assert src.count("_interpretNote()") >= 3, (
        "连败标注须接入定义 + 来源/时间两个 pill 摘要（至少 3 处引用）"
    )


# ---------------------------------------------------------------- fail-open 记 benchfb 错误

def test_failopen_route_error_recorded_in_benchfb() -> None:
    """路由 fail-open 后在途轮次挂错误注记——反馈包里看得出路由层失败过。"""
    benchfb = _read("web/static/js/core/benchfb.js")
    assert "export function benchfbTurnNote" in benchfb, "benchfb 缺注记不收尾的导出"
    board = _read("web/static/js/panel/board.js")
    assert 'benchfbTurnNote("route fail-open: "' in board, "fail-open 分支必须挂 benchfb 错误注记"
