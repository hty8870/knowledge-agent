"use strict";

/* ============================================================================
 * project_exports_core_spec.mjs —— 课题导出中心纯逻辑核心「真行为」规格（node 跑）
 * ----------------------------------------------------------------------------
 * 由 tests/test_project_exports_contract.py 经 `node <this>` 驱动；断言失败 → 非零退出。
 * 存在意义：web_smoke 只静态查字符串、node --check 只验语法，两门都测不出「台账 diff
 * 是否零 LLM 如实计算」「命名/折叠推导是否对」「台账条目是否与 artifacts _normExport
 * 字段对齐」这些用户可见核心逻辑——错一处就是设计约定 台账语义的红线事故。
 *
 * 纯函数直测：project_exports_core.js 零 import（自包含），node 直接相对路径 import；
 * 时钟经 exportSetClock 注入固定基准，不依赖真实 Date.now。
 * ========================================================================== */

import * as P from "../../web/static/js/core/project_exports_core.js";

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
 * 1. 常量（设计约定 的四种导出动作，按钮文案逐字；端点与后端同源）
 * ========================================================================== */
function constantsSuite() {
    check("四种导出动作文案（设计约定 逐字）",
        P.EXPORT_KIND_LABELS.download_list === "导出下载清单"
        && P.EXPORT_KIND_LABELS.citations === "导出引文"
        && P.EXPORT_KIND_LABELS.screening_record === "导出筛选记录"
        && P.EXPORT_KIND_LABELS.full === "导出全部研究材料",
        JSON.stringify(P.EXPORT_KIND_LABELS));
    check("导出类型枚举与后端 EXPORT_KINDS 同源",
        JSON.stringify(P.EXPORT_KINDS) === JSON.stringify(["download_list", "citations", "screening_record", "full"]));
    check("端点路径与 webapp.py @app.post 同源", P.EXPORT_API_PATH === "/api/artifacts/export-pack");
    check("无候选如实提示文案在场",
        P.EXPORT_NO_CANDIDATE_COPY.indexOf("还没有候选") !== -1);
    check("台账区文案在场（最新一次 / 导出记录折叠）",
        P.EXPORT_LATEST_COPY === "最新一次" && P.EXPORT_HISTORY_LABEL === "导出记录");
    check("挂点选择器与 projects.js ENG-P5-MOUNT 一致",
        P.EXPORT_MOUNT_SELECTOR === "[data-p5-mount-export]");
}

/* ============================================================================
 * 2. 候选快照（diff 基准）
 * ========================================================================== */
function snapshotSuite() {
    const s = P.exportCandidateSnapshot({
        candidates: [
            { uid: "a:1", status: "已核验" },
            { uid: "b:2", status: "待核验" },
            { uid: "a:1", status: "已排除" },   // 同 uid 去重（首见为准）
            null,
        ],
    });
    check("uid 集合保序去重", JSON.stringify(s.uids) === JSON.stringify(["a:1", "b:2"]), JSON.stringify(s));
    check("状态表逐 uid", s.statuses["a:1"] === "已核验" && s.statuses["b:2"] === "待核验");
    check("空候选 → 空快照", (() => {
        const e = P.exportCandidateSnapshot({ candidates: [] });
        return e.uids.length === 0 && Object.keys(e.statuses).length === 0;
    })());
    check("非对象防御", (() => {
        const e = P.exportCandidateSnapshot(null);
        return e.uids.length === 0;
    })());
}

/* ============================================================================
 * 3. 台账 diff（零 LLM，设计约定「新增 N 候选 · 状态变化 M」）
 * ========================================================================== */
function changesSuite() {
    const prev = { uids: ["a:1", "b:2"], statuses: { "a:1": "已核验", "b:2": "待核验" } };
    const cur = { uids: ["a:1", "b:2", "c:3"], statuses: { "a:1": "已核验", "b:2": "已排除", "c:3": "待核验" } };
    const c = P.exportChanges(prev, cur);
    check("新增 1（c:3）+ 状态变化 1（b:2）",
        c.added === 1 && c.statusChanged === 1, JSON.stringify(c));
    check("diff 基准保存本次快照（供下一次导出 diff）",
        JSON.stringify(c.prevUids) === JSON.stringify(["a:1", "b:2", "c:3"])
        && c.prevStatuses["b:2"] === "已排除");
    check("无变化 → 双零", (() => {
        const n = P.exportChanges(prev, prev);
        return n.added === 0 && n.statusChanged === 0;
    })());
    check("移出候选不计入摘要（只数新增与状态变化）", (() => {
        // 纯移除场景：c:3 被移出、其余状态不变 → 双零
        const n = P.exportChanges(
            { uids: ["a:1", "b:2", "c:3"], statuses: { "a:1": "已核验", "b:2": "待核验", "c:3": "待核验" } },
            { uids: ["a:1", "b:2"], statuses: { "a:1": "已核验", "b:2": "待核验" } });
        return n.added === 0 && n.statusChanged === 0;
    })());
    check("状态文案：新增 N 候选 · 状态变化 M（设计约定 原文）",
        P.exportChangesText(c) === "新增 1 候选 · 状态变化 1", P.exportChangesText(c));
    check("双零 → 如实写「无变化」", P.exportChangesText({ added: 0, statusChanged: 0 }) === "无变化");
    check("坏值防御 → 无变化", P.exportChangesText(null) === "无变化");
}

/* ============================================================================
 * 4. 台账条目（与 artifacts.js _normExport 字段逐一对齐）
 * ========================================================================== */
function ledgerSuite() {
    P.exportSetClock(() => T0);
    const rec = P.exportLedgerRecord({
        kind: "full",
        name: "初筛",
        datasetVersion: "abc123",
        changes: { added: 1, statusChanged: 1, prevUids: [], prevStatuses: {} },
    });
    check("字段与 artifacts _normExport 对齐（id/kind/name/at/dataset_version/changes/note）",
        "id" in rec && "kind" in rec && "name" in rec && "at" in rec
        && "dataset_version" in rec && "changes" in rec && "note" in rec,
        JSON.stringify(rec));
    check("at 用注入时钟（ISO）", rec.at === new Date(T0).toISOString(), rec.at);
    check("kind/dataset_version 如实透传", rec.kind === "full" && rec.dataset_version === "abc123");
    check("未知 kind 回落 export（不猜）", P.exportLedgerRecord({ kind: "nope" }).kind === "export");
    const renamed = P.exportRenamedRecord(rec, "投稿前复核");
    check("重命名返回新对象、原名保留",
        renamed.name === "投稿前复核" && rec.name === "初筛" && renamed !== rec);
    check("重命名不清空字段", renamed.kind === "full" && renamed.at === rec.at);
    P.exportSetClock(null);
}

/* ============================================================================
 * 5. 台账展示推导（默认只展示最新一条 / 历史折叠「导出记录」）
 * ========================================================================== */
function displaySuite() {
    const r1 = { id: "1", kind: "citations", name: "", at: "2026-08-20T01:00:00Z", changes: { added: 2, statusChanged: 0 } };
    const r2 = { id: "2", kind: "full", name: "初筛", at: "2026-08-22T01:00:00Z", changes: { added: 1, statusChanged: 1 } };
    const r3 = { id: "3", kind: "download_list", name: "", at: "2026-08-21T01:00:00Z", changes: null };
    check("最新一条 = at 最大（默认只展示它，设计约定）",
        P.exportLastRecord([r1, r2, r3]).id === "2");
    check("空台账 → null", P.exportLastRecord([]) === null && P.exportLastRecord(null) === null);
    check("历史 = 除最新外，旧 → 新",
        JSON.stringify(P.exportHistoryRows([r1, r2, r3]).map((r) => r.id)) === JSON.stringify(["1", "3"]));
    check("只有一条 → 无历史", P.exportHistoryRows([r2]).length === 0);
    const summary = P.exportRecordSummary(r2);
    check("一行摘要：类型文案 + 变化", summary === "导出全部研究材料 · 新增 1 候选 · 状态变化 1", summary);
    check("无 changes → 只有类型", P.exportRecordSummary(r3) === "导出下载清单");
}

/* 职责边界（零 DOM/网络/localStorage/import）由 tests/test_project_exports_contract.py
   静态钉死（读文件断言，不在 node 里重复）；这里只测真行为。 */

constantsSuite();
snapshotSuite();
changesSuite();
ledgerSuite();
displaySuite();
end("project_exports_core_spec.mjs");
