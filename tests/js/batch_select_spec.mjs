"use strict";

/* ============================================================================
 * batch_select_spec.mjs —— 结果覆盖策略纯逻辑核「真行为」规格（node 跑）
 * ----------------------------------------------------------------------------
 * 由 tests/test_act_frontend.py 经 node <this> 驱动（若有）或直接 node 跑。断言失败 → 非零退出。
 * 存在意义：web_smoke 只静态查字符串、node --check 只验语法，两门都测不出「初步结果被弱批顶掉」
 * 的覆盖语义红线——「严格更高级才自动换屏」「同 scope 去重不追加」「换词批作备选」「未知 trace
 * 不可比较不自动覆盖」这些设计约定 的红线，错一处就是覆盖策略事故。
 *
 * selectDisplayBatch 是两 route（search a 档 / route=tool 档）共用的纯函数：两档落地逻辑一致，
 * 故本规格直接测纯函数真行为，两 route 各自只补一条「确实调用了它」的结构钉（test_act_frontend.py）。
 * ========================================================================== */

import * as B from "../../web/static/js/core/batch_select.js";

let failures = 0;
function check(name, cond, detail) {
    if (cond) { console.log(`  ok   ${name}`); }
    else { failures++; console.log(`  FAIL ${name}${detail ? "  —— " + detail : ""}`); }
}
function end(name) {
    console.log(`\n${name}: ${failures === 0 ? "PASS" : failures + " FAIL"}`);
    if (failures) process.exit(1);
}

/* ---------- 造批工具（scope=范围指纹；levels=排序层；uids=记录键向量；noTrace=无 trace） ---------- */
function steps(levels) {
    const out = [{ id: "rule_rank", status: "used" }];
    if (levels >= 2) out.push({ id: "local_semantic", status: "used" });
    if (levels >= 3) out.push({ id: "llm_rerank", status: "used" });
    return out;
}
function mkBatch(id, kind, query, { scope, levels = 1, uids, noTrace = false, emptyUid = false } = {}) {
    const results = uids.map((u) => ({ dataset_uid: u, dataset_name: "DS" + u }));
    if (emptyUid) results.push({ dataset_uid: "", dataset_name: "DS?" });
    const payload = {
        ok: true, result_total: results.length, query, results,
        search_trace: noTrace ? null : { steps: steps(levels) },
    };
    return {
        batch_id: id, kind, label: query.slice(0, 20),
        query_effective: query, query_raw: query,
        scope_fingerprint: scope, payload,
    };
}
/* 当前屏 = 裸 payload（无 result_batches）：模拟 preliminary 先落地（applyRecommendResult 不挂批组） */
function bareView(batch) {
    return Object.assign({}, batch.payload, { ok: true });
}

const S = "scope-aaa111";   // 同一检索范围
const T = "scope-bbb222";   // 换过的检索范围

/* ================= 1. rankingLevel（设计约定：polish 不计、未知=null） ================= */
function rankingSuite() {
    check("无 trace → null（不可比较）", B.rankingLevel(null) === null);
    check("trace 无 steps → null", B.rankingLevel({}) === null);
    check("trace steps 空数组 → null（不得默认规则层 1）", B.rankingLevel({ steps: [] }) === null);
    check("规则 only → 1", B.rankingLevel({ steps: [{ id: "rule_rank", status: "used" }] }) === 1);
    check("规则+local_semantic → 2", B.rankingLevel({
        steps: [{ id: "rule_rank", status: "used" }, { id: "local_semantic", status: "used" }] }) === 2);
    check("规则+local+llm → 3", B.rankingLevel({
        steps: [{ id: "rule_rank", status: "used" }, { id: "local_semantic", status: "used" },
                { id: "llm_rerank", status: "used" }] }) === 3);
    check("polish 不计（仍 2）", B.rankingLevel({
        steps: [{ id: "rule_rank", status: "used" }, { id: "local_semantic", status: "used" },
                { id: "llm_polish", status: "used" }] }) === 2);
    check("fallback 不计入（只算 used）", B.rankingLevel({
        steps: [{ id: "rule_rank", status: "used" }, { id: "local_semantic", status: "fallback" }] }) === 1);
    check("llm_rerank 单独存在（无 local）仍 3", B.rankingLevel({
        steps: [{ id: "rule_rank", status: "used" }, { id: "llm_rerank", status: "used" }] }) === 3);
}

/* ================= 2. 记录键向量 / scope / 同批 ================= */
function identitySuite() {
    const a = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"] });
    const b = mkBatch("b2", "rank", "lung", { scope: S, uids: ["A", "B"] });
    const c = mkBatch("b3", "rank", "lung", { scope: S, uids: ["B", "A"] });   // 同 uid 不同序
    const d = mkBatch("b4", "rank", "lung", { scope: T, uids: ["A", "B"] });   // 换 scope
    const e = mkBatch("b5", "rank", "lung", { scope: S, uids: ["A", ""] });    // 空 uid
    const f = mkBatch("b6", "rank", "lung", { scope: S, uids: ["A", "B"], noTrace: true });

    check("同 scope + 同序同 uid → sameBatch", B.sameBatch(a, b) === true);
    check("同 scope + 同 uid 不同序 → 不同批", B.sameBatch(a, c) === false, JSON.stringify(B.recordKeyVector(a).keys) + " vs " + JSON.stringify(B.recordKeyVector(c).keys));
    check("不同 scope → 不同批", B.sameBatch(a, d) === false);
    check("空 uid → 记录键不稳（保守不同批）", B.sameBatch(a, e) === false);
    check("空 uid 批自身不稳", B.recordKeyVector(e).stable === false);
    check("无 trace 不影响判同（只看 scope+记录键）", B.sameBatch(a, f) === true);
    check("裸视图（无指纹）判同走保守", B.sameBatch(a, Object.assign({}, a.payload)) === false);
}

/* ================= 3. mergeBatches：同 scope 只留更高层 ================= */
function mergeSuite() {
    const prelimL1 = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
    const loopL2 = mkBatch("b2", "rank", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
    const merged = B.mergeBatches([], [prelimL1, loopL2]);
    check("同 scope 合并 → 只留更高层（b2）", merged.length === 1 && merged[0].batch_id === "b2", JSON.stringify(merged.map((m) => m.batch_id)));
    const mixed = B.mergeBatches([], [prelimL1, mkBatch("b3", "rerank", "lung2", { scope: T, uids: ["A", "C"], levels: 1 })]);
    check("不同 scope 合并 → 两者都留", mixed.length === 2, JSON.stringify(mixed.map((m) => m.batch_id)));
}

/* ================= 4. selectDisplayBatch 行为矩阵（设计约定 逐字） ================= */
function selectSuite() {
    const trace = (b) => B.traceOf(b);

    /* 同批（同 scope 同记录同层）→ 去重（不新增 pill、不换屏） */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const loop = mkBatch("b2", "rank", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("同批 → dedupe（不换屏、不新增 pill）", d.mode === "dedupe" && d.view === null && d.activeBatchId === "b1");
        check("回执如实（不得说已更新）", d.sysText && d.sysText.indexOf("更匹配") < 0);
        check("摘徽标", d.stripPrelimBadge === true);
    }
    /* 弱批（同 scope 同记录、级别更低）→ 去重，保住更优批 */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
        const loop = mkBatch("b2", "rank", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("弱批 → dedupe（保住更优初屏）", d.mode === "dedupe" && d.view === null && d.activeBatchId === "b1");
    }
    /* 换词批（不同 scope、级别更低）→ display 整屏覆盖（条件变更：不比较级别） */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
        const loop = mkBatch("b2", "rank", "lung cancer", { scope: T, uids: ["A", "C"], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("换词批（scope 异）→ display 整屏覆盖（不再作备选）", d.mode === "display" && d.view !== null);
        check("view 指向候选批（b2）", d.view.active_batch === "b2");
        check("merged 含两批（旧屏+候选）", d.mergedBatches.length === 2);
        check("回执空串（诚实句由调用方/后端披露句给）", d.sysText === "");
    }
    /* 未知 trace = 不可比较（不得默认规则层 1 自动覆盖） */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
        const loopSame = mkBatch("b2", "rank", "lung", { scope: S, uids: ["A", "B"], noTrace: true });
        const ds = B.selectDisplayBatch({ result_batches: [prelim, loopSame], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("未知 trace+同批 → 去重（不自动覆盖）", ds.mode === "dedupe" && ds.view === null);
        const loopDiff = mkBatch("b3", "rank", "lung2", { scope: T, uids: ["A", "C"], noTrace: true });
        const dd = B.selectDisplayBatch({ result_batches: [prelim, loopDiff], active_batch: "b3", _prelimShown: true }, bareView(prelim));
        check("未知 trace+异 scope → display（条件变更不比较级别）", dd.mode === "display" && dd.view !== null);
    }
    /* preliminary 仅规则（镜像：候选即初屏）→ 去重（保留初屏、摘徽标） */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [prelim], active_batch: "b1", _prelimShown: true }, null);
        check("preliminary 仅规则 → 去重（保留初屏不重渲）", d.mode === "dedupe" && d.view === null && d.stripPrelimBadge === true);
    }
    /* 同 uid 不同序 → 不同批（记录键向量 + 序）→ alternate 保守 */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["B", "A"], levels: 2 });
        const loop = mkBatch("b2", "rank", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("同 uid 不同序 → 不同批 → alternate", d.mode === "alternate" && d.view === null);
    }
    /* 空 uid → 缺稳定键 → 保守不去重、不自动覆盖（即使候选层更高） */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const loop = mkBatch("b2", "rank", "lung", { scope: S, uids: ["A"], levels: 3, emptyUid: true });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("空 uid → 不自动覆盖（作备选）", d.mode === "alternate" && d.view === null);
    }
    /* 结构化条件丢失（指纹不同=换了 scope）→ display 覆盖（条件变更；回执由披露句说明） */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
        const loop = mkBatch("b2", "rank", "lung", { scope: T, uids: ["A", "B"], levels: 1 });  // 条件丢 → 指纹变
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("条件丢失（scope 异）→ display", d.mode === "display");
    }
    /* 严格升级（同 scope 同记录、层更高）→ display 自动换屏 */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const loop = mkBatch("b2", "rank", "lung", { scope: S, uids: ["A", "B"], levels: 3 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("严格升级 → display（自动换屏）", d.mode === "display" && d.view !== null);
        check("view.active_batch = 候选批", d.view.active_batch === "b2");
        check("merged 一层（更高层替换）", d.mergedBatches.length === 1 && d.mergedBatches[0].batch_id === "b2");
    }
    /* 首次落屏（无参考批）→ display */
    {
        const loop = mkBatch("b1", "rank", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
        const d = B.selectDisplayBatch({ result_batches: [loop], active_batch: "b1" }, null);
        check("首次落屏 → display", d.mode === "display" && d.view !== null);
        check("query = 生效检索句", d.query === "lung");
    }
    /* 跨轮回看时（currentView 已有批组）：换词批（scope 异）→ display 覆盖（条件变更优先） */
    {
        const oldStrong = mkBatch("old1", "search_rerun", "lung", { scope: T, uids: ["A", "B"], levels: 3 });
        const curView = Object.assign({}, oldStrong.payload, { result_batches: [oldStrong], active_batch: "old1" });
        const newWeak = mkBatch("new1", "rerank", "heart", { scope: T + "-x", uids: ["A", "C"], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [newWeak], active_batch: "new1" }, curView);
        check("跨轮回看换词 → display（条件变更覆盖，不再保旧批）", d.mode === "display" && d.view !== null);
        check("display 指向候选批（new1）", d.view.active_batch === "new1");
        check("merged 只含本轮候选批（不并入上一轮屏态批，防跨轮泄漏）", d.mergedBatches.length === 1 && d.mergedBatches[0].batch_id === "new1");
    }
    /*跨轮泄漏——上一轮屏态（currentView 的 result_batches）不得并入本轮 display 的 pill 组 */
    {
        const oldTurn = mkBatch("t1", "rank", "breast", { scope: "S1", uids: ["A", "B"], levels: 1 });
        const oldTurn2 = mkBatch("t2", "rank", "breast-fastq", { scope: "S2", uids: ["A"], levels: 1 });
        const curView = Object.assign({}, oldTurn2.payload, { result_batches: [oldTurn, oldTurn2], active_batch: "t2" });
        const newTurn = mkBatch("t3", "rerank", "pig", { scope: "S3", uids: [], levels: 1 });   // 换词后 0 命中（胜者）
        const d = B.selectDisplayBatch({ result_batches: [newTurn], active_batch: "t3" }, curView);
        check("跨轮 0 命中 display → mode=display（条件变更覆盖）", d.mode === "display" && d.view !== null);
        check("跨轮 display 只含本轮新批（上一轮屏态批不并入）", d.mergedBatches.length === 1 && d.mergedBatches[0].batch_id === "t3", JSON.stringify(d.mergedBatches.map((m) => m.batch_id)));
    }
}

/* ================= 5. activeBatchId 鲁棒性（批缺 batch_id 不再塌成 ""） =================
   缺陷（activeBatchId 塌成空串）：参考批/候选批缺 batch_id（preliminary 批常不带）时，旧实现 `主动batchId=裸 ref.batch_id`
   塌成 "" → 渲染高亮错位 + switchBatch 空转（pill 点不动）。新实现按归一 id（batch_id || "b(序号)"）
   解析，保证非空且指向「真正在屏/被采纳的那批」。 */
function activeBatchIdSuite() {
    /* 场景一 换词批（不同 scope）→ display：参考批缺 batch_id 不塌陷，activeBatchId = 候选批 b2 */
    {
        const prelim = mkBatch("", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
        const loop = mkBatch("b2", "rank", "lung cancer", { scope: T, uids: ["A", "C"], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("场景一 缺 id 参考批+换词 → display（整屏覆盖）", d.mode === "display" && d.view !== null);
        check("场景一 activeBatchId 指向候选批（b2，非空）", d.activeBatchId === "b2", JSON.stringify(d.activeBatchId));
        check("场景一 merged 两批（初屏+候选）", d.mergedBatches.length === 2);
    }
    /* 场景二 换词批 display：候选批缺 batch_id、参考批有 id → activeBatchId = 合成 b2（非空不塌陷） */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
        const loop = mkBatch("", "rank", "lung cancer", { scope: T, uids: ["A", "C"], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("场景二 候选缺 id+换词 → display，activeBatchId 合成非空（b2）", d.mode === "display" && d.activeBatchId === "b2", JSON.stringify(d.activeBatchId));
    }
    /* 场景三 严格升级 display：候选缺 batch_id 但可被 merged 引用 → activeBatchId 非空 */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const loop = mkBatch("", "rerank", "lung", { scope: T, uids: ["A", "B", "C"], levels: 3 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("场景三 升级候选缺 id → display，activeBatchId 非空", d.mode === "display" && !!d.activeBatchId, JSON.stringify(d.activeBatchId));
    }
    /* 场景四 跨轮回看 display：参考批缺 batch_id → activeBatchId 非空（不回落空串） */
    {
        const oldStrong = mkBatch("", "search_rerun", "lung", { scope: T, uids: ["A", "B"], levels: 3 });
        const curView = Object.assign({}, oldStrong.payload, { result_batches: [oldStrong], active_batch: "" });
        const newWeak = mkBatch("new1", "rerank", "heart", { scope: T + "-x", uids: ["A", "C"], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [newWeak], active_batch: "new1" }, curView);
        check("场景四 跨轮回看换词（缺 id）→ display，activeBatchId 非空", d.mode === "display" && !!d.activeBatchId, JSON.stringify(d.activeBatchId));
    }
}

/* ============== 6. 条件变更 0 命中必换屏（rerun-gate 前端侧，用户投诉修复） ==============
   投诉「换成猪的」：换词批 0 命中却被 alternate 档拦下、结果区保持不变 + 旧话术气泡。
   修正后：条件变更（scope 不同）→ display 整屏覆盖（含 0 命中），回执由调用方/后端披露句给；
   ALTERNATE_SYS_TEXT 只留给「同 scope 重检较弱批」且去黑话。 */
function rerunGateSuite() {
    /* 换词 0 命中批 → display 整屏覆盖（空结果集如实上屏；stable=false 不拦截） */
    {
        const prelim = mkBatch("b1", "preliminary", "人类乳腺癌 FASTQ", { scope: S, uids: ["A", "B", "C"], levels: 1 });
        const loop0 = mkBatch("b2", "rerank", "猪乳腺癌 FASTQ", { scope: T, uids: [], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop0], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("换词 0 命中批 → display（不再 alternate/保持不变）", d.mode === "display" && d.view !== null);
        check("view 空结果集如实上屏", d.view.results.length === 0);
        check("view 指向候选批（b2）", d.view.active_batch === "b2");
        check("sysText 空（诚实句由调用方/披露句给，不再假「保持不变」）", d.sysText === "");
        check("不因 0 命中（stable=false）拦截", d.view !== null);
        const v = B.recordKeyVector(loop0);
        check("0 命中 = 空键 + stable=false（事实，非不稳定）", v.keys.length === 0 && v.stable === false);
    }
    /* 参考缺指纹（legacy 裸视图）→ 保守 alternate（无法确证换词，不猜测覆盖） */
    {
        const legacy = Object.assign({}, mkBatch("b1", "rank", "lung", { scope: S, uids: ["A", "B"], levels: 1 }).payload, { ok: true });
        const loop0 = mkBatch("b2", "rank", "猪", { scope: T, uids: [], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [loop0], active_batch: "b2" }, legacy);
        check("参考缺指纹 → 保守 alternate（不猜测「换了条件」）", d.mode === "alternate" && d.view === null);
    }
    /* 同 scope 重检较弱（不同记录）→ alternate，回执去黑话、如实 */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
        const weak = mkBatch("b2", "rerank", "lung", { scope: S, uids: ["A", "C"], levels: 1 });   // 同 scope，更弱+不同结果
        const d = B.selectDisplayBatch({ result_batches: [prelim, weak], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("同 scope 弱批 → alternate（保住当前更优结果）", d.mode === "alternate" && d.view === null);
        check("回执 = 去黑话后的 ALTERNATE_SYS_TEXT", d.sysText === B.ALTERNATE_SYS_TEXT);
        check("回执不再含误导性「按新条件」", d.sysText.indexOf("按新条件") < 0);
    }
    /* 同 scope 重检更强 → display 升级（排序层择优只保留在同 scope 内） */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const strong = mkBatch("b2", "rerank", "lung", { scope: S, uids: ["A", "B", "C"], levels: 3 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, strong], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("同 scope 更强（3>1）→ display 升级", d.mode === "display" && d.view !== null);
    }
    /* 同 scope 同批 → dedupe（不追加、不换屏），回执 = DEDUPE_SYS_TEXT */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const same2 = mkBatch("b2", "rank", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, same2], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("同 scope 同批 → dedupe", d.mode === "dedupe" && d.view === null && d.sysText === B.DEDUPE_SYS_TEXT);
    }
}

/* ================= 7.：零命中放松单一通道 —— 纯逻辑核 =================
   「放松哪个条件」收编为 ask.relax 选择卡（2026-09-14）：渲染唯一（board.js askRelaxShow）、
   执行唯一（cbChoose askrelax → cbCommit suppressed）、数据源唯一（relaxation_options）。
   这里锁死三件事：① 批是否零命中（payload.results 空数组）；② 从零命中批派生 ask.relax
   卡 payload（deriveRelaxCard：query/reason/options，suppress 原样透传整组替换语义）；
   ③ 最新结果判定（最后一个带 pill 的回执 entry 的活跃批）。 */ 
function zeroHitRescueSuite() {
    function zb(id, payload, extra) {
        return Object.assign({ batch_id: id, kind: "rank", query_effective: "猪脑数据",
            label: "猪脑数据", query_raw: "猪脑数据", scope_fingerprint: "EB", payload: payload,
            disclosure_zh: "「猪脑数据」没有匹配到数据集。" }, extra || {});
    }

    /* ① isZeroHitBatch */
    check("null → 非零命中", B.isZeroHitBatch(null) === false);
    check("非零命中批 → 非零命中", B.isZeroHitBatch(mkBatch("b1", "rank", "lung", { scope: S, uids: ["A"] })) === false);
    check("零命中批（results=[]）→ true", B.isZeroHitBatch(zb("eb", { ok: true, results: [], result_total: 0 })) === true);
    check("results 非数组 → 非零命中", B.isZeroHitBatch(zb("eb", { ok: true, result_total: 0 })) === false);

    /* ② deriveRelaxCard（ask.relax 卡 payload 派生，与环内卡同形） */
    check("null → null", B.deriveRelaxCard(null) === null);
    check("非零命中批 → null", B.deriveRelaxCard(mkBatch("b1", "rank", "lung", { scope: S, uids: ["A"] })) === null);
    check("零命中但无 relaxation_options → null", B.deriveRelaxCard(zb("eb", { ok: true, results: [], result_total: 0 })) === null);
    {
        const b = zb("eb", { ok: true, results: [], result_total: 0,
            relaxation_options: [
                { key: "dim:disease", kind: "drop", label: "疾病", count: 12,
                    suppress: ["include:disease"],
                    results: [{ dataset_name: "DS-X" }, { dataset_name: "DS-Y" }, { dataset_name: "DS-Z" }] },
                { key: "only:tissue", kind: "only", label: "组织", count: 30,
                    suppress: ["include:species", "include:disease", "raw:required"],
                    results: [{ dataset_name: "DS-A" }] },
            ] });
        const c = B.deriveRelaxCard(b);
        check("派生卡 payload 三键（query 取批检索词 / reason 空）", !!c && c.query === "猪脑数据" && c.reason === "", JSON.stringify(c));
        check("选项全量保留", !!c && c.options.length === 2);
        check("选项形状 key/kind/label/count", !!c && c.options[0].key === "dim:disease" && c.options[0].kind === "drop"
            && c.options[0].label === "疾病" && c.options[0].count === 12, JSON.stringify(c && c.options[0]));
        check("suppress 原样透传（整组替换语义，前端零合并）", !!c
            && JSON.stringify(c.options[0].suppress) === '["include:disease"]'
            && JSON.stringify(c.options[1].suppress) === '["include:species","include:disease","raw:required"]');
        check("选项不带 preview（2026-09-14 评审：卡层不消费示例名，results 预览仅属 HTTP 契约）", !!c
            && !("preview" in c.options[0]) && !("preview" in c.options[1]));
        check("only 项 kind 保留", !!c && c.options[1].kind === "only");
    }
    {
        /* 缺 suppress 的项不给选（没有可执行放松量的项点下去是无操作，防死卡） */
        const b = zb("eb", { ok: true, results: [], result_total: 0,
            relaxation_options: [{ key: "dim:disease", kind: "drop", label: "疾病", count: 12 }] });
        check("无 suppress 的项被滤尽 → null", B.deriveRelaxCard(b) === null);
    }
    {
        /* 混合：空 label / 缺 suppress 的项滤掉，合法项保留 */
        const b = zb("eb", { ok: true, results: [], result_total: 0,
            relaxation_options: [
                { key: "dim:disease", kind: "drop", label: "", count: 12, suppress: ["include:disease"] },
                { key: "only:tissue", kind: "only", label: "组织", count: 30, suppress: ["include:species"] },
            ] });
        const c = B.deriveRelaxCard(b);
        check("空 label 项滤掉、合法项保留", !!c && c.options.length === 1 && c.options[0].key === "only:tissue", JSON.stringify(c));
    }

    /* ③ latestActiveBatchId */
    check("空 entries → 空串", B.latestActiveBatchId([]) === "");
    check("无 pill entry → 空串", B.latestActiveBatchId([{ kind: "sys", text: "x" }]) === "");
    {
        const entries = [
            { kind: "sys", text: "回执1", pills: [{ batchId: "a", active: true }] },
            { kind: "sys", text: "回执2", pills: [{ batchId: "b", active: true }, { batchId: "a", active: false }] },
        ];
        check("取最后一个回执 entry 的活跃批", B.latestActiveBatchId(entries) === "b");
    }
    {
        // 前面还有带 pill 的旧 entry，但最后一条无 pill → 回退到「最后一个带 pill 的 entry」
        const entries = [
            { kind: "sys", text: "回执1", pills: [{ batchId: "a", active: true }] },
            { kind: "say", text: "用户" },
        ];
        check("跳过无 pill entry，取上一个带 pill 的活跃批", B.latestActiveBatchId(entries) === "a");
    }
}

rankingSuite();
identitySuite();
mergeSuite();
selectSuite();
activeBatchIdSuite();
rerunGateSuite();
zeroHitRescueSuite();
end("batch_select_spec.mjs");
