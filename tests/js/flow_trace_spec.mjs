"use strict";

/* ============================================================================
 * flow_trace_spec.mjs —— 信息流重构（用户重申定稿）· 工具轨迹纯逻辑核「真行为」规格（node 跑）
 * ----------------------------------------------------------------------------
 * 由 tests/test_flow_trace_contract.py 经 node <this> 驱动（若有）或直接 node 跑。断言失败 → 非零退出。
 * 覆盖：事件→工具行映射（一工具一行、非工具事件除名）、状态机去重（同 id 更新不 append）、
 *       计数压缩（按类别加和为一行、失败如实标注）、verb 展示名、覆盖丢弃（supersede 即丢弃）。
 * 这些是 信息流的红线，错一处就是「重复消息/摘要句撒谎/覆盖不丢」事故。
 * ========================================================================== */

import * as F from "../../web/static/js/core/flow_trace.js";

let failures = 0;
function check(name, cond, detail) {
    if (cond) { console.log(`  ok   ${name}`); }
    else { failures++; console.log(`  FAIL ${name}${detail ? "  —— " + detail : ""}`); }
}
function end(name) {
    console.log(`\n${name}: ${failures === 0 ? "PASS" : failures + " FAIL"}`);
    if (failures) process.exit(1);
}

/* ---------- 事件 → 工具行（一工具一行；分流/路由元事件不是工具，除名） ---------- */
function stageSuite() {
    check("preliminary → 初步检索（计 1 次 rank）", (() => {
        const s = F.stageFromEvent("preliminary", { result_total: 36 });
        return s && s.id === "tool:preliminary" && s.kind === "tool" && s.verb === "rank"
            && s.phase === "done" && s.text === "初步检索";
    })());
    check("tool_start → tool/pending（id=tool:展示名，与完成帧 label 同键）", (() => {
        const s = F.stageFromEvent("tool_start", { node: "rerank", verb: "rerank", label_zh: "优化检索词重查" });
        return s && s.kind === "tool" && s.phase === "pending" && s.id === "tool:优化检索词重查"
            && s.verb === "rerank" && s.text === "优化检索词重查";
    })());
    check("step rerank ok → 同 id done（与 tool_start 天然同行去重）", (() => {
        const t = F.stageFromEvent("tool_start", { node: "rerank", verb: "rerank", label_zh: "优化检索词重查" });
        const s = F.stageFromEvent("step", { node: "rerank", verb: "rerank", label_zh: "优化检索词重查", ok: true });
        return t && s && t.id === s.id && s.phase === "done" && s.kind === "tool";
    })());
    //  缺陷 0（验证截图）：真实后端 tool_start 只带 verb、step 只带 node（工具完成帧 node="execute"）
    // 且不带 verb——两者 id 曾经分歧导致「同一行两个、pending 永不落定」。此处锁死二者同行去重 + verb 保真。
    check("真后端形状：tool_start(verb) 与 step(node=execute) 同行去重，verb 保为 rank", (() => {
        let r = [];
        r = F.upsertStage(r, F.stageFromEvent("tool_start", { verb: "rank", label_zh: "执行工具 · 检索数据集" }));
        r = F.upsertStage(r, F.stageFromEvent("step", { node: "execute", label_zh: "执行工具 · 检索数据集", ok: true, detail: "命中 12 条", ms: 300 }));
        return r.length === 1 && r[0].phase === "done" && r[0].verb === "rank" && r[0].n === 1;
    })());
    check("真后端形状：两次同工具调用合并一行 n=2 且压缩计数准确", (() => {
        let r = [];
        r = F.upsertStage(r, F.stageFromEvent("tool_start", { verb: "rank", label_zh: "执行工具 · 检索数据集" }));
        r = F.upsertStage(r, F.stageFromEvent("step", { node: "execute", label_zh: "执行工具 · 检索数据集", ok: true }));
        r = F.upsertStage(r, F.stageFromEvent("tool_start", { verb: "rank", label_zh: "执行工具 · 检索数据集" }));
        r = F.upsertStage(r, F.stageFromEvent("step", { node: "execute", label_zh: "执行工具 · 检索数据集", ok: true }));
        return F.renderableStages(r).length === 1 && r[0].n === 2
            && F.compressFlow(r).summaryText === "执行了 2 次检索。";
    })());
    check("LLM 结构节点不入轨迹：tool_start verb=node → null", (() => {
        return F.stageFromEvent("tool_start", { verb: "node", label_zh: "生成说明" }) === null
            && F.stageFromEvent("tool_start", { verb: "node", label_zh: "理解意图" }) === null
            && F.stageFromEvent("tool_start", { verb: "node", label_zh: "分流共识" }) === null;
    })());
    check("LLM 结构节点完成帧 → null：node=narrate/understand/decide/repair/validate", (() => {
        return F.stageFromEvent("step", { node: "narrate", label_zh: "生成说明", ok: true }) === null
            && F.stageFromEvent("step", { node: "understand", label_zh: "理解意图", ok: true }) === null
            && F.stageFromEvent("step", { node: "decide", label_zh: "判断下一步", ok: true }) === null
            && F.stageFromEvent("step", { node: "repair", label_zh: "修复", ok: true }) === null
            && F.stageFromEvent("step", { node: "validate", label_zh: "校验", ok: true }) === null;
    })());
    check("step 失败 → failed", (() => {
        const s = F.stageFromEvent("step", { node: "rank", verb: "rank", label_zh: "检索数据集", ok: false });
        return s && s.phase === "failed";
    })());
    check("step route_consensus → null（分流不是工具调用）",
        F.stageFromEvent("step", { node: "route_consensus", label_zh: "分流共识", ok: true }) === null);
    check("tool_start route* → null（路由元事件除名）",
        F.stageFromEvent("tool_start", { node: "route_consensus", label_zh: "分流共识" }) === null);
    check("step detail 不进工具行（除此以外什么信息都没有）", (() => {
        const s = F.stageFromEvent("step", { node: "rank", verb: "rank", label_zh: "检索数据集", ok: true, detail: "命中 36 条" });
        return s && !("detail" in s);
    })());
    check("未知 evKind → null", F.stageFromEvent("final", {}) === null);
    check("空 step 无 label → null", F.stageFromEvent("step", {}) === null);
}

/* ---------- verb 展示名（非流式合成行用） ---------- */
function labelSuite() {
    check("rank → 检索数据集", F.flowVerbLabel("rank") === "检索数据集");
    check("rerank → 优化检索词重查", F.flowVerbLabel("rerank") === "优化检索词重查");
    check("curate.search_online → 联网搜索入库", F.flowVerbLabel("curate.search_online") === "联网搜索入库");
    check("表外 verb 原样回落（不虚构）", F.flowVerbLabel("pack.download") === "pack.download");
}

/* ---------- 状态机去重 ---------- */
function dedupSuite() {
    let r = [];
    r = F.upsertStage(r, { id: "tool:preliminary", kind: "tool", verb: "rank", text: "初步检索", phase: "done" });
    r = F.upsertStage(r, { id: "tool:rerank", kind: "tool", verb: "rerank", text: "优化检索词重查", phase: "pending" });
    check("两行 → 两行", r.length === 2);
    // 同 id 更新（tool_start pending → step done）：不 append
    r = F.upsertStage(r, { id: "tool:rerank", kind: "tool", verb: "rerank", text: "优化检索词重查", phase: "done" });
    check("同 id 更新（pending→done）不 append（修重复消息）", r.length === 2 && r[1].phase === "done" && r[1].n === 1);
    // 已落定后同 id 再来 = 环内又一次真实调用：行仍一行，调用数 n+1
    r = F.upsertStage(r, { id: "tool:rerank", kind: "tool", verb: "rerank", text: "优化检索词重查", phase: "pending" });
    check("落定后再调用：行不增、n+1、回到 pending", r.length === 2 && r[1].n === 2 && r[1].phase === "pending");
    r = F.upsertStage(r, { id: "tool:rerank", kind: "tool", verb: "rerank", text: "优化检索词重查", phase: "done" });
    check("第二次调用落定：n 不再加", r.length === 2 && r[1].n === 2 && r[1].phase === "done");
    // 原数组不被改（不可变）
    check("upsert 不可变（原数组不变）", F.upsertStage([{ id: "a" }], { id: "b" }).length === 2);
    const ref = [{ id: "a" }];
    check("缺 id 直接返回原引用", F.upsertStage(ref, null) === ref);
    check("renderableStages 去重兜底", F.renderableStages([{ id: "a" }, { id: "a" }, { id: "b" }]).length === 2);
}

/* ---------- 压缩（工具调用次数按类别加和，缩减为一行） ---------- */
function compressSuite() {
    const recs = [
        { id: "tool:preliminary", kind: "tool", verb: "rank", text: "初步检索", phase: "done" },
        { id: "tool:rerank", kind: "tool", verb: "rerank", text: "优化检索词重查", phase: "done" },
        { id: "result", kind: "result", text: "检索完成", phase: "done" },
    ];
    const c = F.compressFlow(recs);
    check("kept 只有 result", c.kept.length === 1 && c.kept[0].kind === "result");
    check("expanded = 全量（展开回看）", c.expanded.length === recs.length);
    check("初步检索+环内重检 = 「执行了 2 次检索。」", c.summaryText === "执行了 2 次检索。", c.summaryText);
    // 复合工具计两类（一次调用干两件事，如实报）
    const cc = F.compressFlow([{ id: "tool:curate.search_online", kind: "tool", verb: "curate.search_online", text: "联网搜索入库", phase: "done" }]);
    check("复合工具计两类", cc.summaryText === "执行了 1 次联网搜索，1 次文件写入。", cc.summaryText);
    // 多类别按固定顺序、真计数
    const cm = F.compressFlow([
        { id: "tool:a", kind: "tool", verb: "rank", text: "x", phase: "done" },
        { id: "tool:b", kind: "tool", verb: "curate.check_updates", text: "y", phase: "done" },
    ]);
    check("检索+联网搜索 两类同句", cm.summaryText === "执行了 1 次检索，1 次联网搜索。", cm.summaryText);
    // 失败如实标（计数照算，句尾补失败数）
    const cf = F.compressFlow([
        { id: "tool:a", kind: "tool", verb: "rank", text: "x", phase: "done" },
        { id: "tool:b", kind: "tool", verb: "rerank", text: "y", phase: "failed" },
    ]);
    check("含失败 → 句尾补（1 次失败）", cf.summaryText === "执行了 2 次检索（1 次失败）。", cf.summaryText);
    // 未登记 verb 落「本地处理」兜底（防漏报）
    const cu = F.compressFlow([{ id: "tool:z", kind: "tool", verb: "magic.new", text: "z", phase: "done" }]);
    check("未知 verb → 本地处理兜底", cu.summaryText === "执行了 1 次本地处理。", cu.summaryText);
    // 多 query 各调一次 rank（同 id 合并行，n=2）→ 计数按真实调用数加和
    let multi = [];
    multi = F.upsertStage(multi, F.stageFromEvent("tool_start", { node: "rank", verb: "rank", label_zh: "检索数据集" }));
    multi = F.upsertStage(multi, F.stageFromEvent("step", { node: "rank", verb: "rank", label_zh: "检索数据集", ok: true }));
    multi = F.upsertStage(multi, F.stageFromEvent("tool_start", { node: "rank", verb: "rank", label_zh: "检索数据集" }));
    multi = F.upsertStage(multi, F.stageFromEvent("step", { node: "rank", verb: "rank", label_zh: "检索数据集", ok: true }));
    check("两次 rank 合并一行", F.renderableStages(multi).length === 1);
    check("压缩按真实调用数：「执行了 2 次检索。」", F.compressFlow(multi).summaryText === "执行了 2 次检索。", F.compressFlow(multi).summaryText);
    // 无工具行 → 空摘要（不虚构流程）
    check("无工具行 → 空摘要", F.compressFlow([{ id: "result", kind: "result", text: "r", phase: "done" }]).summaryText === "");
    check("空记录 → 空摘要", F.compressFlow([]).summaryText === "");
}

/* ---------- 覆盖丢弃（supersede 即丢弃；跨 query 不丢） ---------- */
function discardSuite() {
    const mkBatch = (id, kind, scope, levels, empty) => {
        const t = { steps: [{ id: "rule_rank", status: "used" }] };
        if (levels >= 2) t.steps.push({ id: "local_semantic", status: "used" });
        if (levels >= 3) t.steps.push({ id: "llm_rerank", status: "used" });
        // empty=true → 0 命中批（results=[]；检索引擎实跑出空集，是事实不是故障）。
        const results = empty ? [] : [{ dataset_uid: "A" }];
        return { batch_id: id, kind, scope_fingerprint: scope,
            payload: { ok: true, results, search_trace: t } };
    };
    // 初步批被后续结果覆盖 → 丢弃
    const prelim = mkBatch("b1", "preliminary", "S", 2);
    const final = mkBatch("b2", "rank", "S", 2);
    check("初步批被覆盖 → discard", F.shouldDiscardOutcome(prelim, final) === true);
    // 同 scope 更高层 → 丢弃
    const weak = mkBatch("b1", "rank", "S", 1);
    const strong = mkBatch("b2", "rerank", "S", 3);
    check("同 scope 更强 → discard", F.shouldDiscardOutcome(weak, strong) === true);
    // 同 scope 同层 → 丢弃（重检成功）
    check("同 scope 同层重检 → discard", F.shouldDiscardOutcome(weak, mkBatch("b3", "rank", "S", 1)) === true);
    // 不同 scope → 不丢
    check("异 scope（换词）→ 不丢", F.shouldDiscardOutcome(weak, mkBatch("b4", "rank", "T", 3)) === false);
    // 跨 query（sameQuery=false）→ 不丢
    check("跨 query → 不丢", F.shouldDiscardOutcome(prelim, final, { sameQuery: false }) === false);
    // next 无效（ok:false / 空结果）→ 不丢
    check("next 无效 → 不丢", F.shouldDiscardOutcome(prelim, Object.assign({}, final, { payload: { ok: false, results: [] } })) === false);
    // forceSupersede 兜底
    check("forceSupersede → 丢", F.shouldDiscardOutcome(weak, mkBatch("b5", "rank", "T", 1), { forceSupersede: true }) === true);
    //  ---- 用户规则：0 命中胜者只丢弃「同一 query 重检索链」的批（preliminary + 同链 re-search），
    //      跨意图独立 rank 批保留（用户铁律：跨 query 需保留） ----
    {
        const emptyT = mkBatch("b6", "rerank", "T", 1, true);   // 换词重检后 0 命中（胜者）
        const emptyS = mkBatch("b7", "rank", "S", 1, true);     // 同 scope 0 命中（rank，非重检链）
        const chainPre = mkBatch("b8", "search_rerun", "S", 2); // 同一 query 链的上一次重检（非空）
        check("条件变更 0 命中 → discard（同 query 链重检 0 命中）", F.shouldDiscardOutcome(chainPre, emptyT) === true);
        check("初步批被换词 0 命中覆盖 → discard（投诉场景 A：prelim→rerank 0 命中）", F.shouldDiscardOutcome(prelim, emptyT) === true);
        check("同 scope 0 命中 → 不丢（rank 非重检链，保守保留）", F.shouldDiscardOutcome(weak, emptyS) === false);
        check("跨 query 0 命中 → 不丢（sameQuery=false）", F.shouldDiscardOutcome(weak, emptyT, { sameQuery: false }) === false);
        // 同 query rerun 链收尾 0 命中：中间重检批与初步批都丢（用户：重检索成功含 0 命中，弃上一次结果）
        const rerunChainWin = mkBatch("b10", "search_rerun", "T", 1, true);   // rerun2 0 命中（active）
        check("同 query rerun 链 0 命中收尾 → 初步批丢", F.shouldDiscardOutcome(prelim, rerunChainWin) === true);
        check("同 query rerun 链 0 命中收尾 → 中间重检批丢", F.shouldDiscardOutcome(chainPre, rerunChainWin) === true);
        // 多意图各自成批：一意图 0 命中（active）不得丢弃另一意图的独立 rank 批
        const intentA = mkBatch("rx", "rank", "SX", 1);        // 意图1（非空）
        const intentB0 = mkBatch("ry", "rank", "SY", 1, true); // 意图2，0 命中（active）
        check("多意图 0 命中 active → 跨意图 rank 批保留", F.shouldDiscardOutcome(intentA, intentB0) === false);
        check("多意图 0 命中 active → 初步批也保守保留（re-search 胜者才弃链）", F.shouldDiscardOutcome(prelim, intentB0) === false);
    }
    // 空入参 → false
    check("空入参 → false", F.shouldDiscardOutcome(null, null) === false);
}

stageSuite();
labelSuite();
dedupSuite();
compressSuite();
discardSuite();
end("flow_trace_spec.mjs");
