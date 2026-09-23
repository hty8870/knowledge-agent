"use strict";

/* ============================================================================
 * project_updates_core_spec.mjs —— 追踪更新检查纯逻辑核心「真行为」规格（node 跑）
 * ----------------------------------------------------------------------------
 * 由 tests/test_project_updates_contract.py 经 `node <this>` 驱动；断言失败 → 非零退出。
 * 存在意义：web_smoke 只静态查字符串、node --check 只验语法，两门都测不出 diff 的
 * 语义边界——「truncated 时不得声称某条已从全部结果消失」「排序/score 变化不算」
 * 「规则升级单列」这些设计约定 的红线，错一处就是如实第一的事故。
 *
 * 纯函数直测：project_updates_core.js 只相对 import artifacts.js（同为纯模块），
 * node 直接相对路径 import；不依赖真实 Date.now。
 * ========================================================================== */

import * as U from "../../web/static/js/core/project_updates_core.js";

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
 * 1. 常量（设计约定 逐字 / 包描述原文）
 * ========================================================================== */
function constantsSuite() {
    check("spec 版本 = v1（与后端 RECORD_FINGERPRINT_SCHEMA 同值）", U.WATCH_SPEC_VERSION === "v1");
    check("截断文案（设计约定 原文口径）",
        U.WATCH_TRUNCATED_REMOVED_COPY === "结果超 200 条被截断，无法判定消失");
    check("无变化文案（包描述原文）", U.WATCH_NO_CHANGE_COPY === "本次检查无变化 · 刚检查过");
    check("规则升级单列（设计约定 原文）", U.WATCH_RULE_UPDATED_COPY === "检索规则已更新");
    check("重试基线按钮文案（包描述原文）", U.WATCH_RETRY_BASELINE_COPY === "重试生成基线");
    check("同步超时文案", U.WATCH_SYNC_TIMEOUT_COPY === "后台仍在同步，稍后再看");
    check("job 冲突文案", U.WATCH_SYNC_BUSY_COPY === "另一个更新任务进行中，请稍候");
    check("job 轮询参数（1.5s × 200 ≈ 5 分钟）", U.WATCH_SYNC_POLL_MS === 1500 && U.WATCH_SYNC_POLL_MAX === 200);
}

/* ============================================================================
 * 2. 可检查追踪过滤 / 上游同步编排 / 语料代哨兵
 *    （全体批量按钮已撤，watchCheckCount/watchBatchSlice 等一并删除）
 * ========================================================================== */
function checkableSuite() {
    const withSpec = (q) => ({ check_condition: { spec: q ? { query: q } : {} } });
    const list = [withSpec("lung"), withSpec(""), { check_condition: null }, {}, { check_condition: { spec: { sources: ["GEO"] } } }];
    const got = U.watchCheckableProjects(list);
    check("只留有可重跑条件的追踪", got.length === 2 && got[0] === list[0] && got[1] === list[4], String(got.length));
    check("无数组 → 空数组", U.watchCheckableProjects(null).length === 0);
    check("批量函数已删除（无调用方一并清理）",
        U.watchCheckCount === undefined && U.watchBatchSlice === undefined
        && U.watchBatchRestText === undefined && U.watchSummaryText === undefined
        && U.WATCH_BATCH_MAX === undefined);
}

function upstreamSuite() {
    check("spec sources 提取（去空白）", (() => {
        const s = U.watchSpecSources({ sources: ["10x Genomics", " ", "GEO"] });
        return s && s.length === 2 && s[0] === "10x Genomics" && s[1] === "GEO";
    })());
    check("空/缺 sources → null（全源口径）",
        U.watchSpecSources({}) === null && U.watchSpecSources({ sources: [] }) === null
        && U.watchSpecSources(null) === null);
    check("job done → result", (() => {
        const st = U.watchSyncJobState({ status: "done", result: { imported_total: 3 } });
        return st.done === true && st.result.imported_total === 3;
    })());
    check("job failed → error", (() => {
        const st = U.watchSyncJobState({ status: "failed", error: "boom" });
        return st.done === true && st.error === "boom";
    })());
    check("job running/缺字段 → 未终态",
        U.watchSyncJobState({ status: "running" }).done === false && U.watchSyncJobState(null).done === false);
    check("上游文案：新增 N 条入库", U.watchUpstreamText({ imported_total: 5 }) === "上游同步：新增 5 条入库");
    check("上游文案：已是最新", U.watchUpstreamText({ imported_total: 0 }) === "上游同步：已是最新");
    check("上游文案：无结果 → null（不渲染）", U.watchUpstreamText(null) === null && U.watchUpstreamText({}) === null);
}

function genSuite() {
    check("语料代不同 → 需要刷新", U.watchGenChanged("abc", "def") === true);
    check("语料代相同 → 零成本跳过", U.watchGenChanged("abc", "abc") === false);
    check("current 不可得 → false（降级不报错）",
        U.watchGenChanged("abc", null) === false && U.watchGenChanged("abc", "") === false);
    check("stored 缺失（首次）→ 需要刷新", U.watchGenChanged(null, "abc") === true);
    check("自动刷新 toast：N 个追踪有新数据", U.watchAutoRefreshToast(2) === "2 个追踪有新数据");
    check("自动刷新 toast：0 → null（不打扰）", U.watchAutoRefreshToast(0) === null);
}

/* ============================================================================
 * 3. diff：material change 语义（设计约定——不比较名次，只看真实新增/消失/指纹）
 * ========================================================================== */

/* 基线：3 条（A/B/C 有指纹），结果 2 条（A/B 同名次变化 + D 新增，C 消失） */
const BASE = {
    uids: ["A", "B", "C"],
    fingerprints: { A: "fp-A1", B: "fp-B1", C: "fp-C1" },
    result_total: 3, truncated: false, generated_at: "2026-08-01T00:00:00Z",
};
const RES = {
    result_total: 3,
    uids: ["D", "B", "A"],   // 无序集合语义：D 新增、C 消失；顺序与 baseline 不同（不许按名次比）
    fingerprints: { D: "fp-D1", B: "fp-B2", A: "fp-A1" },   // B 指纹变了（信息变化）；A 没变
    truncated: false,
    executed_spec: { spec_version: "v1" },
    checked_at: "2026-08-22T00:00:00Z",
};

function diffSuite() {
    const d = U.watchDiff(RES, BASE, "v1");
    check("新增（真实：现在有、基线没有，按结果顺序）", d.added.length === 1 && d.added[0] === "D", JSON.stringify(d.added));
    check("消失（真实：基线有、现在没有，按基线顺序）", d.removed.length === 1 && d.removed[0] === "C", JSON.stringify(d.removed));
    check("指纹变化（双侧都有但指纹不同）", d.fpChanged.length === 1 && d.fpChanged[0] === "B", JSON.stringify(d.fpChanged));
    check("排序变化不算（同集合不同序 → 只有真变化）", d.kind === "diff");
    check("双侧截断开关如实", d.addedTrusted === true && d.removedTrusted === true);
    check("ruleUpdated=false（版本一致）", d.ruleUpdated === false);
    check("changed 合计 = 3（新增 1 + 指纹 1 + 消失 1）", U.watchChangedCount(d) === 3, String(U.watchChangedCount(d)));
}

function noChangeSuite() {
    const same = {
        result_total: 3,
        uids: ["C", "A", "B"],     // 与基线**同集合**但顺序不同（排序/score 变化不算 material change）
        fingerprints: { A: "fp-A1", B: "fp-B1", C: "fp-C1" },
        truncated: false,
        executed_spec: { spec_version: "v1" },
        checked_at: "2026-08-22T00:00:00Z",
    };
    const d = U.watchDiff(same, BASE, "v1");
    check("同集合不同顺序 → 无变化", d.added.length === 0 && d.removed.length === 0 && d.fpChanged.length === 0);
    check("无变化计数 = 0", U.watchChangedCount(d) === 0);
    check("无变化状态文案逐字", U.watchStatusText(0) === "本次检查无变化 · 刚检查过");
    check("changed 埋点标志 = 0", U.watchChangedFlag(d) === 0);
}

function truncationSuite() {
    /* 本次结果 >200（truncated=true）：消失不可判定，新增/指纹仍可靠 */
    const resTrunc = {
        result_total: 250,
        uids: ["A", "B", "D"],     // B 仍在交集里可比较指纹；C 可能在 201+，不能说消失；D 真实新增
        fingerprints: { A: "fp-A1", B: "fp-B2", D: "fp-D1" },
        truncated: true,
        executed_spec: { spec_version: "v1" },
        checked_at: "2026-08-22T00:00:00Z",
    };
    const d1 = U.watchDiff(resTrunc, BASE, "v1");
    check("truncated=true → removed 判定关闭（不声称消失）",
        d1.removedTrusted === false && d1.removed.length === 0, JSON.stringify(d1.removed));
    check("truncated=true → 新增仍判定（D 真实新增）", d1.added.length === 1 && d1.added[0] === "D");
    check("truncated=true → 指纹仍判定（交集不受截断影响）",
        d1.fpChanged.length === 1 && d1.fpChanged[0] === "B");
    check("truncated 提示（设计约定 原文）", U.watchTruncatedNote(d1) === "结果超 200 条被截断，无法判定消失");

    /* 上次基线 >200（baseline.truncated=true）：新增不可判定，消失/指纹可靠 */
    const baseTrunc = {
        uids: ["A", "B", "C"],     // 只是当时的前 200
        fingerprints: { A: "fp-A1", B: "fp-B1", C: "fp-C1" },
        result_total: 250, truncated: true, generated_at: "2026-08-01T00:00:00Z",
    };
    const resFull = {
        result_total: 3,
        uids: ["D", "B", "A"],     // C 不在现在全部结果里 = 真消失；D 可能本来就在 201+ = 不可说新增
        fingerprints: { D: "fp-D1", B: "fp-B1", A: "fp-A1" },
        truncated: false,
        executed_spec: { spec_version: "v1" },
        checked_at: "2026-08-22T00:00:00Z",
    };
    const d2 = U.watchDiff(resFull, baseTrunc, "v1");
    check("baseline.truncated=true → added 判定关闭（不声称新增）",
        d2.addedTrusted === false && d2.added.length === 0, JSON.stringify(d2.added));
    check("baseline.truncated=true → 消失仍判定（C 真消失）", d2.removed.length === 1 && d2.removed[0] === "C");
    check("baseline 截断提示", U.watchTruncatedNote(d2) === "上次结果超 200 条被截断，无法判定新增");
    check("双侧都正常 → 无截断提示", U.watchTruncatedNote(U.watchDiff(RES, BASE, "v1")) === null);
}

function baselineSuite() {
    /* baseline 缺失（保存追踪时基线生成失败）→ 只做基线生成，无 diff */
    const d = U.watchDiff(RES, null, "v1");
    check("baseline=null → kind=baseline", d.kind === "baseline");
    check("baseline 场景无变化条目", U.watchChangedCount(d) === 0);
    const d2 = U.watchDiff(RES, {}, "v1");
    check("baseline 空对象 → kind=baseline", d2.kind === "baseline");
    /* 空结果基线（uids=[] 是合法全量）→ 正常 diff */
    const emptyBase = { uids: [], fingerprints: {}, result_total: 0, truncated: false, generated_at: "x" };
    const d3 = U.watchDiff(RES, emptyBase, "v1");
    check("空结果基线 → 全部算新增", d3.kind === "diff" && d3.added.length === 3, JSON.stringify(d3.added));
}

function ruleSuite() {
    /* spec_version 与端点支持版本不一致 → 「检索规则已更新」单列 */
    const resOld = Object.assign({}, RES, { executed_spec: { spec_version: "v2" } });
    const d1 = U.watchDiff(resOld, BASE, "v1");
    check("sent v1 / executed v2 → ruleUpdated", d1.ruleUpdated === true);
    const d2 = U.watchDiff(RES, BASE, "v2");
    check("sent v2 / executed v1 → ruleUpdated（保存的规格比端点新）", d2.ruleUpdated === true);
    const d3 = U.watchDiff(RES, BASE, "v1");
    check("版本一致 → 不算规则更新", d3.ruleUpdated === false);
    /* executed_spec 缺失（老后端）→ 不判（不虚构） */
    const noExec = { result_total: 3, uids: ["A"], fingerprints: {}, truncated: false };
    const d4 = U.watchDiff(noExec, BASE, "v1");
    check("executed_spec 缺失 → 不判规则更新", d4.ruleUpdated === false);
}

/* ============================================================================
 * 4. 待查看更新条目 / 汇总文案（设计约定）
 * ========================================================================== */
function deltaSuite() {
    const d = U.watchDiff(RES, BASE, "v1");
    const entries = U.watchDeltaEntries(d);
    check("逐条结构（uid + kind）", entries.length === 3
        && entries[0].uid === "D" && entries[0].kind === "added"
        && entries[1].uid === "B" && entries[1].kind === "fp"
        && entries[2].uid === "C" && entries[2].kind === "removed", JSON.stringify(entries));
    check("kind 三态枚举在场", U.WATCH_KIND_ADDED === "added" && U.WATCH_KIND_FP === "fp" && U.WATCH_KIND_REMOVED === "removed");
    check("有变化状态文案", U.watchStatusText(3) === "发现 3 项变化，见下方待查看更新");
    check("changed 埋点标志 = 1", U.watchChangedFlag(U.watchDiff(RES, BASE, "v1")) === 1);
}

/* ============================================================================ */

constantsSuite();
checkableSuite();
upstreamSuite();
genSuite();
diffSuite();
noChangeSuite();
truncationSuite();
baselineSuite();
ruleSuite();
deltaSuite();

/* 回执：检查后如实说「检查了 N 条，X 条有更新，Y 条已是最新」 */
function receiptSuite() {
    check("有更新：数清「检查了 N 条 / 有更新 X / 一致 Y」，无 removed 时 Y=total-added-fp",
        U.watchReceiptText(30, 3, { added: 2, fp: 1, removed: 0 })
            === "检查了 30 条记录，3 条有更新（新增 2 · 信息变化 1），27 条与上次一致",
        U.watchReceiptText(30, 3, { added: 2, fp: 1, removed: 0 }));
    check("有更新且含消失：removed 计入「有更新」，不计入「一致」",
        U.watchReceiptText(30, 4, { added: 2, fp: 1, removed: 1 })
            === "检查了 30 条记录，4 条有更新（新增 2 · 信息变化 1 · 消失 1），27 条与上次一致",
        U.watchReceiptText(30, 4, { added: 2, fp: 1, removed: 1 }));
    check("无更新：明确「均与上次一致，无更新」",
        U.watchReceiptText(10, 0, { added: 0, fp: 0, removed: 0 })
            === "检查了 10 条记录，均与上次一致，无更新",
        U.watchReceiptText(10, 0, { added: 0, fp: 0, removed: 0 }));
    check("缺省安全：counts/resultTotal 缺省不抛、归一为 0",
        U.watchReceiptText(undefined, 0, {}) === "检查了 0 条记录，均与上次一致，无更新",
        U.watchReceiptText(undefined, 0, {}));
    check("负值防御：一致数不出现负数", U.watchReceiptText(1, 2, { added: 1, fp: 1 }).indexOf("-") < 0);
}

receiptSuite();
end("project_updates_core_spec.mjs");
