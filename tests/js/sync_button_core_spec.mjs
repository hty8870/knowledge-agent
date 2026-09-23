"use strict";

/* ============================================================================
 * sync_button_core_spec.mjs —— 数据集页一键同步纯逻辑核心「真行为」规格（node 跑）
 * ----------------------------------------------------------------------------
 * 由 tests/test_sync_button_contract.py 经 `node <this>` 驱动；断言失败 → 非零退出。
 * 存在意义：web_smoke 只静态查字符串、node --check 只验语法，两门都测不出「上次同步
 * 文案是否如实」「结果摘要是否绝不写『更新 Y』」「sync_busy 是否按设计约定 文案上屏」
 * 这些用户可见核心逻辑——错一处就是如实第一的红线事故。
 *
 * 纯函数直测：sync_button_core.js 零 import（不依赖任何其它模块），node 直接相对路径
 * import；时钟经 syncSetClock 注入固定基准，不依赖真实 Date.now。
 * ========================================================================== */

import * as S from "../../web/static/js/search/sync_button_core.js";

const T0 = 1753000000000;   // 固定基准（与 artifacts_spec/projects_spec 同源）
let failures = 0;
function check(name, cond, detail) {
    if (cond) { console.log(`  ok   ${name}`); }
    else { failures++; console.log(`  FAIL ${name}${detail ? "  —— " + detail : ""}`); }
}
function end(name) {
    console.log(`\n${name}: ${failures === 0 ? "PASS" : failures + " FAIL"}`);
    if (failures) process.exit(1);
}

/* ============================================================================
 * 1. 副文案 / 常量（设计约定 逐字）
 * ========================================================================== */
function copySuite() {
    check("副文案明示行为（仅入外部库、可一键撤销）",
        S.SYNC_SUB_COPY === "检查官方源更新并导入（仅入外部库，可一键撤销）", S.SYNC_SUB_COPY);
    check("sync_busy 上屏文案（设计约定）",
        S.SYNC_BUSY_COPY === "另一个同步任务进行中，请稍候", S.SYNC_BUSY_COPY);
    check("批量联动已退役（常量不再导出）",
        S.SYNC_P4_HOOK === undefined && S.syncP4MountText === undefined && S.syncP4InfoN === undefined);
    check("进行中进度文案在场", typeof S.SYNC_RUNNING_COPY === "string" && S.SYNC_RUNNING_COPY.length > 0);
}

/* ============================================================================
 * 2. 空闲态「上次同步」文案（设计约定：实例级事实，从未同步如实写）
 * ========================================================================== */
function lastSyncedSuite() {
    S.syncSetClock(() => T0);
    check("从未同步 → 如实写「从未同步」", S.syncLastSyncedText(null) === "从未同步");
    check("空串 → 从未同步", S.syncLastSyncedText("") === "从未同步");
    check("坏值 → 从未同步", S.syncLastSyncedText("not-a-date") === "从未同步");
    check("今天（0 天）→ 上次同步：今天", S.syncLastSyncedText(new Date(T0 - 3600e3).toISOString()) === "上次同步：今天");
    check("1 天前 → 上次同步：1 天前", S.syncLastSyncedText(new Date(T0 - 86400e3).toISOString()) === "上次同步：1 天前");
    check("3 天前 → 上次同步：3 天前", S.syncLastSyncedText(new Date(T0 - 3 * 86400e3).toISOString()) === "上次同步：3 天前");
    check("未来时间 → 按今天（不做负天数）", S.syncLastSyncedText(new Date(T0 + 86400e3).toISOString()) === "上次同步：今天");
    S.syncSetClock(null);   // 还原真实时钟
}

/* ============================================================================
 * 3. 结果摘要（设计约定：新增 X / 已存在 Z / 失败 W，绝不写「更新 Y」）
 * ========================================================================== */
function receiptSuite() {
    const r = S.syncReceipt({
        imported_total: 3,
        skipped_existing: 2,
        failed_sources: [{ source: "a", error: "X" }],
        created_files: ["f1.json"],
    });
    check("三计数提取（added/skipped/failed）",
        r.added === 3 && r.skipped === 2 && r.failed === 1, JSON.stringify(r));
    check("摘要格式：新增 X / 已存在 Z / 失败 W",
        S.syncReceiptText(r) === "新增 3 / 已存在 2 / 失败 1", S.syncReceiptText(r));
    check("摘要字符串不含「更新」（sync 无更新既有记录语义）",
        S.syncReceiptText(r).indexOf("更新") === -1);
    check("全零也如实呈现（空结果不是错误）",
        S.syncReceiptText(S.syncReceipt({})) === "新增 0 / 已存在 0 / 失败 0");
    check("imported_total 缺失时回退 created_files 条数",
        S.syncReceipt({ created_files: ["a", "b"] }).added === 2);
    check("负数/坏值防御", (() => {
        const s = S.syncReceipt({ imported_total: -5, skipped_existing: "x", failed_sources: null });
        return s.added === 0 && s.skipped === 0 && s.failed === 0;
    })());
    check("receipt 无 updated 字段（结构上杜绝「更新 Y」）",
        !("updated" in S.syncReceipt({ imported_total: 1 })));
}

/* ============================================================================
 * 4. 失败项明细（设计约定：失败项如实列出原因）
 * ========================================================================== */
function failureSuite() {
    const r = S.syncFailureLines({
        failed_sources: [
            { source: "geo", label: "GEO", note_zh: "联网失败：超时" },
            { source: "scp", label: "SCP", error: "ValueError" },
        ],
    });
    check("逐源 label + 原因", r.lines.length === 2
        && r.lines[0].label === "GEO" && r.lines[0].reason === "联网失败：超时"
        && r.lines[1].label === "SCP" && r.lines[1].reason === "ValueError", JSON.stringify(r.lines));
    check("无失败源 → 空明细", S.syncFailureLines({}).lines.length === 0);
    check("超过上限截断并如实计「另有 N」", (() => {
        const many = { failed_sources: [] };
        for (let i = 0; i < S.SYNC_FAIL_LINES_MAX + 3; i++) many.failed_sources.push({ label: "s" + i, error: "e" });
        const o = S.syncFailureLines(many);
        return o.lines.length === S.SYNC_FAIL_LINES_MAX && o.more === 3;
    })());
}

/* ============================================================================
 * 5. 撤销（设计约定：撤掉 N 个文件/失败 M，如实呈现）
 * ========================================================================== */
function recallSuite() {
    const r = S.syncRecallResult({
        recalled_files: ["a", "b"],
        skipped_files: ["c"],
        failed_files: [{ filename: "d", error: "E" }],
    });
    check("三计数提取", r.recalled === 2 && r.skipped === 1 && r.failed === 1, JSON.stringify(r));
    check("全部成功 → 已撤销 N 个文件", S.syncRecallText({ recalled: 2 }) === "已撤销 2 个文件");
    check("部分失败 → 已撤销 N 个文件，失败 M", S.syncRecallText({ recalled: 2, failed: 1 }) === "已撤销 2 个文件，失败 1");
    check("全失败 → 撤回失败 M 个文件", S.syncRecallText({ failed: 2 }) === "撤回失败 2 个文件");
    check("无可撤（可重入跳过）→ 如实说明", S.syncRecallText({ skipped: 1 }) === "没有可撤回的文件（1 个已不在外部库）");
    check("空回执 → 没有可撤回的文件", S.syncRecallText({}) === "没有可撤回的文件");
}

/* ============================================================================
 * 6. 失败分类（sync_busy 按设计约定 文案；其余如实透出）
 * ========================================================================== */
function classifySuite() {
    const busy = S.syncClassifyError(400, { detail: "另一个「同步数据集」正在运行（同步整任务文件锁被占用）。本次没有做任何检查、也没有写入任何内容；请稍后重试" }, null);
    check("400 + detail 含「同步」→ sync_busy + 设计约定 文案",
        busy.kind === "sync_busy" && busy.message === S.SYNC_BUSY_COPY && busy.detail.length > 0, JSON.stringify(busy));
    const other = S.syncClassifyError(500, { detail: "内部错误" }, null);
    check("其它 HTTP 错误 → http + detail 如实透出",
        other.kind === "http" && other.message === "同步失败：内部错误", JSON.stringify(other));
    const htmlErr = S.syncClassifyError(500, "<html>oops</html>", null);
    check("非 JSON 错误体 → http + 状态码（不误判成网络问题）",
        htmlErr.kind === "http" && htmlErr.message === "同步失败（HTTP 500）", htmlErr.message);
    const net = S.syncClassifyError(0, null, new Error("Failed to fetch"));
    check("网络失败 → network + 人话", net.kind === "network" && net.message.indexOf("网络请求失败") === 0, net.message);
}

/* ============================================================================
 * 7. 批量联动已退役（全体批量按钮删除，本套件无相关用例）
 * ========================================================================== */

copySuite();
lastSyncedSuite();
receiptSuite();
failureSuite();
recallSuite();
classifySuite();
end("sync_button_core_spec.mjs");
