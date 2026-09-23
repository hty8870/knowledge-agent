"use strict";

/* ============================================================================
 * feedback_dialog_core_spec.mjs —— 意见反馈对话框纯逻辑核「真行为」规格（node 跑）
 * ----------------------------------------------------------------------------
 * 由 tests/test_feedback_dialog_contract.py 经 `node <this>` 驱动；断言失败 → 非零退出。
 * 覆盖：正文校验（必填/上限同源）、诊断摘要（遥测关闭「无可用统计」/allowlist 聚合展示）、
 * 入队条目构造（明示单次授权语义：feedback_id/authorized_at 定格、with_diag 与 diag 落法、
 * summary 剔除）、剪贴板正文（含/不含诊断块、无可用统计如实行）、feedbackNewId 形状与唯一性。
 * 本规格零 DOM、零网络、零 localStorage——纯函数直测（feedback_core 的队列/遮蔽/加密行为
 * 已由 feedback_core_spec.mjs 覆盖，不重复）。
 * ========================================================================== */

const core = await import(new URL("../../web/static/js/core/feedback_dialog_core.js", import.meta.url));
const fcore = await import(new URL("../../web/static/js/core/feedback_core.js", import.meta.url));

let failed = 0;
function check(label, ok, detail) {
    if (ok) console.log("PASS", label);
    else { failed += 1; console.error("FAIL", label, detail || ""); }
}

/* ---------- 0. 常量与同源 ---------- */

check("上限与 feedback_core.FEEDBACK_MAX_TEXT 同源", core.feedbackTextState("x").max === fcore.FEEDBACK_MAX_TEXT
    && fcore.FEEDBACK_MAX_TEXT === 2000);

/* ---------- 1. 正文校验 ---------- */

check("空正文 → 必填不通过", core.feedbackTextState("").ok === false);
check("纯空白 → 不通过", core.feedbackTextState("   ").ok === false);
check("短正文 → 通过", core.feedbackTextState("结果不对").ok === true);
{
    const long = "长".repeat(fcore.FEEDBACK_MAX_TEXT);
    check("恰好上限 → 通过", core.feedbackTextState(long).ok === true);
    check("超上限 → 不通过", core.feedbackTextState(long + "长").ok === false);
    const st = core.feedbackTextState("意见一二三");
    check("count 按码元计", st.count === 5 && st.max === 2000);
}

/* ---------- 2. 诊断摘要 ---------- */

{
    const off = core.feedbackDiagBuild(null, { version: "v1", platform: "win" });
    check("events=null → available:false", off.available === false);
    check("无可用统计文案在场", off.summary.indexOf("无可用统计") >= 0, off.summary);
    const off2 = core.feedbackDiagBuild("不是数组", {});
    check("非数组 → available:false", off2.available === false);
}
{
    const snap = core.feedbackDiagBuild([
        { k: "search", q: "10x" }, { k: "search", q: "hca" }, { k: "open" },
        { k: "err" }, { k: "ai", ok: false }, { k: "ai", ok: true },
    ], { version: "20260822-ad1", platform: "Windows" });
    check("available:true", snap.available === true);
    check("版本/平台透传", snap.version === "20260822-ad1" && snap.platform === "Windows");
    check("错误计数 = err + ai 失败", snap.errors === 2, snap.errors);
    check("摘要行含版本/平台/错误/功能", snap.summary.indexOf("版本 20260822-ad1") >= 0
        && snap.summary.indexOf("平台 Windows") >= 0
        && snap.summary.indexOf("最近错误 2 次") >= 0
        && snap.summary.indexOf("功能使用：") >= 0, snap.summary);
    check("摘要行功能计数用中文标签（按 kind 排序）", snap.summary.indexOf("AI 成功 1 次") >= 0
        && snap.summary.indexOf("打开详情 1 次") >= 0
        && snap.summary.indexOf("搜索 2 次") >= 0
        && snap.summary.indexOf("AI 失败") < 0, snap.summary);
}
{
    const empty = core.feedbackDiagBuild([], { version: "v1", platform: "x" });
    check("无功能记录 → 如实「暂无记录」", empty.available === true
        && empty.summary.indexOf("暂无记录") >= 0, empty.summary);
}

/* ---------- 3. 入队条目构造（明示单次授权语义） ---------- */

{
    const entry = core.feedbackEntryBuild("  搜索很慢  ", true,
        { available: true, version: "v1", errors: 1, features: { search: 2 }, summary: "展示行" },
        { feedback_id: "fb-abc", authorized_at: "2026-08-22T08:00:00Z" });
    check("feedback_id/授权时间定格", entry.feedback_id === "fb-abc" && entry.authorized_at === "2026-08-22T08:00:00Z");
    check("正文 trim", entry.text === "搜索很慢");
    check("with_diag=true 且 diag 附入", entry.with_diag === true && entry.diag.available === true);
    check("diag 剔除 UI 专用 summary", !("summary" in entry.diag) && entry.diag.features.search === 2,
        JSON.stringify(entry.diag));
}
{
    const entry = core.feedbackEntryBuild("好", false, { available: true, summary: "x" }, { feedback_id: "fb-1" });
    check("未勾选 → diag=null 且 with_diag=false", entry.with_diag === false && entry.diag === null);
}
{
    const entry = core.feedbackEntryBuild("好", true, null, { feedback_id: "fb-2" });
    check("勾选但无快照 → 如实 {available:false}", entry.with_diag === true
        && entry.diag && entry.diag.available === false);
}
{
    const e1 = core.feedbackEntryBuild("a", false, null, { feedback_id: "fb-x1" });
    const e2 = core.feedbackEntryBuild("b", false, null, { feedback_id: "fb-x2" });
    check("不传 feedback_id → 自动生成且各异", e1.feedback_id !== e2.feedback_id
        && e1.feedback_id.indexOf("fb-") === 0);
}

/* ---------- 4. 剪贴板正文 ---------- */

{
    const txt = core.feedbackClipboardText("你好，请修复", { available: true, version: "v1", platform: "Win",
        errors: 3, features: { search: 2, fav: 1 } });
    check("带诊断块：正文 + 版本/平台/错误/功能", txt.indexOf("你好，请修复") === 0
        && txt.indexOf("版本：v1") >= 0 && txt.indexOf("平台：Win") >= 0
        && txt.indexOf("最近错误：3 次") >= 0 && txt.indexOf("搜索 2 次") >= 0
        && txt.indexOf("收藏 1 次") >= 0, txt);
}
{
    const txt = core.feedbackClipboardText("只有正文", null);
    check("不带诊断 → 只有正文", txt === "只有正文", txt);
}
{
    const txt = core.feedbackClipboardText("复制兜底", { available: false });
    check("无可用统计如实行", txt.indexOf("无可用统计") >= 0, txt);
}

/* ---------- 5. feedbackNewId 形状 ---------- */

{
    const a = core.feedbackNewId(), b = core.feedbackNewId();
    check("id 形状 fb- 前缀", a.indexOf("fb-") === 0 && b.indexOf("fb-") === 0);
    check("两次生成不同", a !== b);
}

console.log(failed ? "\nFEEDBACK_DIALOG_CORE_SPEC_FAIL" : "\nFEEDBACK_DIALOG_CORE_SPEC_OK");
process.exit(failed ? 1 : 0);
