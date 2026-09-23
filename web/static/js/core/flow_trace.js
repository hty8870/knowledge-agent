"use strict";

/* ============================================================================
 * flow_trace.js —— 信息流 · 过程轨迹纯逻辑核（2026-08 用户重申定稿版）
 * ----------------------------------------------------------------------------
 * 输出结构（用户 2026-08-24 重申，唯一准绳）：
 *   上部·工具调用：流式时每个工具调用**最多一行**——只展示执行了哪些工具 + ✓/✗
 *     （进行中由渲染层给脉冲点），除此以外什么信息都没有（无 detail、无阶段话术）。
 *     输出结束后压缩：把各工具调用次数按类别**加和缩减为一行**（「执行了 1 次检索，
 *     1 次联网搜索。」，口径与 act_core.ACT_TOOL_KIND 一致），点击该行原地展开为
 *     压缩前的原始行（渲染层 <details>，不另加一行）。
 *   中部·唯一气泡 / 下部·结果 pill：由 board.js 负责，本文件只管工具轨迹。
 *
 * 本文件是什么 / 不是什么：
 *   - 是：① 事件 → 工具行描述（分流/执行事件钩子机械触发，react 环零改动）；
 *     ② 行状态机去重（同一次调用的 tool_start 亮 pending、step 落 done 更新**同一行**，
 *     绝不 append 两行）；③ 压缩纯函数（按 verb 归类计数加和；失败如实标注）；
 *     ④ 覆盖丢弃判定（supersede 即丢弃，连存储也丢）。
 *   - 不是：DOM、网络、墙钟、外部状态。一切决定做成纯函数，node 规格可直跑
 *     （零 `#` import、零相对 import——self-contained，不进 import 环）。
 *
 * 行分类（压缩映射的键）：
 *   - KIND_TOOL   "tool"   一次工具调用（rank/rerank/curate.x/pack.x/…——唯一入轨迹的类别）
 *   - KIND_RESULT "result" 最终结果批（保留为 pill/产物，不进工具行也不进压缩句）
 * 注意：route_consensus/分流共识**不是工具调用**（用户：除此以外最好什么信息都没有），
 *   stageFromEvent 对它返回 null——「已完成分流」行与 KIND_ROUTE/KIND_PRELIM 一并退役，
 *   初步检索就是一次 rank 调用（计 1 次检索），不再单列。
 * ========================================================================== */

export const KIND_TOOL = "tool";
export const KIND_RESULT = "result";

export const PHASE_PENDING = "pending";
export const PHASE_DONE = "done";
export const PHASE_FAILED = "failed";

/* ---------- 工具类别归并（压缩计数口径，与 act_core.ACT_TOOL_KIND 一致） ----------
   本文件 self-contained 故平铺一份拷贝；新增工具时两处同步补映射（act_core 有同口径注释）。
   复合工具（联网搜回即入库）计两个类别，如实反映一次调用干的两件事。 */
const FLOW_KIND_ORDER = ["检索", "联网搜索", "文件写入", "下载打包", "对比", "自检", "本地读取"];
const FLOW_KIND_FALLBACK = "本地处理";
const FLOW_TOOL_KIND = {
    "rank": ["检索"], "rerank": ["检索"], "search.rerun": ["检索"], "compat.find": ["检索"],
    "curate.check_updates": ["联网搜索"],
    "curate.search_online": ["联网搜索", "文件写入"],
    "curate.sync_updates": ["联网搜索", "文件写入"],
    "curate.import": ["文件写入"], "curate.remove": ["文件写入"],
    "curate.restore": ["文件写入"], "curate.rollback": ["文件写入"],
    "curate.db_status": ["本地读取"], "curate.list": ["本地读取"], "files.show": ["本地读取"],
    "pack.download": ["下载打包"], "pack.preview": ["下载打包"],
    "reuse.pack": ["下载打包"], "cite.export": ["下载打包"],
    "compare.datasets": ["对比"], "fair.check": ["自检"],
};

/* ---------- 事件 → 工具行描述（事件钩子机械触发；react 环零改动） ----------
   evKind ∈ "preliminary" | "tool_start" | "step"。
   返回 {id, kind, verb, text, phase}；无法归类（含分流共识/LLM 结构节点等非工具事件）返回 null。
   id 统一 "tool:"+label（展示名）：同一次调用的 tool_start 与完成帧 step 的 label_zh 逐字一致
   ——天然同 id，upsertStage 更新同一行，不再出现「pending 行 + ✓ 行」并列两行。
   注意：真实后端 tool_start 只带 verb、不带 node，step 只带 node（工具完成帧 node="execute"）、
   不带 verb——按 node/verb 各取会算出两个不同的 id，正是「同一行显示两次、pending 不落定」的病灶。 */
/* LLM 结构节点（非工具调用）完成帧的 node 名：决定/校验/修复/理解/叙述/分流 都是智能体编排
   阶段，不是一次工具调用——用户重申「除此以外最好什么信息都没有」，一律不入轨迹行。
   真实工具的完成帧 node 恒为 "execute"（agent_exec._trace_entry），保留它来落定工具行。 */
const FLOW_NARRATE_NODES = new Set(["route_consensus", "understand", "decide", "validate", "repair", "narrate"]);

export function stageFromEvent(evKind, data) {
    if (!evKind) return null;
    const d = (data && typeof data === "object") ? data : {};
    if (evKind === "preliminary") {
        /* 初步检索 = 一次本地库检索（计 1 次「检索」）。 */
        return { id: "tool:preliminary", kind: KIND_TOOL, verb: "rank", text: "初步检索", phase: PHASE_DONE };
    }
    if (evKind === "tool_start") {
        const verb = String(d.verb || "").trim();
        const node = String(d.node || "").trim();
        /* LLM 阶段节点（分流共识/理解意图/生成说明…）用 verb="node" 表「即将开始」，不是工具调用。 */
        if (verb === "node") return null;
        if (/^route/i.test(node || verb)) return null;   // 分流是路由元事件，不是工具调用
        const label = String(d.label_zh || verb || node || "").trim();
        if (!label) return null;
        return { id: "tool:" + label, kind: KIND_TOOL, verb: verb || node,
            text: label, phase: PHASE_PENDING };
    }
    if (evKind === "step") {
        const node = String(d.node || "").trim();
        if (FLOW_NARRATE_NODES.has(node) || /^route/i.test(node)) return null;   // LLM 结构节点：非工具，不入轨迹
        const label = String(d.label_zh || node || "").trim();
        if (!label) return null;
        const ok = !(d.ok === false);
        const verb = String(d.verb || node || "").trim();
        return { id: "tool:" + label, kind: KIND_TOOL, verb: verb,
            text: label, phase: ok ? PHASE_DONE : PHASE_FAILED };
    }
    return null;
}

/* ---------- verb → 展示名（非流式合成行用；与后端 agent_exec 注册表的 label_zh 同口径） ----------
   流式路径的 label_zh 由后端事件自带，用不到本表；只有非流式回退（无 SSE、final 帧带
   plan.steps）合成工具行时才查它。表外 verb 原样回落（展开回看见真名，不虚构）。 */
const FLOW_VERB_LABEL = {
    "rank": "检索数据集", "rerank": "优化检索词重查", "search.rerun": "检索新查询",
    "results.show": "更新结果区", "ask.relax": "询问放宽条件",
    "compat.find": "查找兼容数据集",
    "curate.check_updates": "检查来源更新", "curate.search_online": "联网搜索入库",
    "curate.sync_updates": "检查更新并同步入库", "curate.db_status": "汇报数据库状态",
    "compare.datasets": "对比数据集", "cite.export": "导出引文",
    "fair.check": "检查 FAIR 就绪度", "curate.rollback": "回滚写操作",
};
export function flowVerbLabel(verb) {
    const v = String(verb || "").trim();
    return FLOW_VERB_LABEL[v] || v;
}

/* ---------- 行状态机去重（更新同一行，绝不 append 两行） ----------
   同 id 只保留一行：pending → done/failed 时**更新**该行，不新增第二行。
   调用计数（n）：同一 id 在已落定（done/failed）后再次 pending/done 到达 = 环内又一次
   真实调用（如多 query 各调一次 rank）——行仍一行（用户：一工具最多一行），n+1，
   压缩句按 n 加和（「执行了 2 次检索」是真调用数，不是行数）。
   返回新数组（不可变，node 规格好断言）；入参缺 id / 空 → 原样。 */
export function upsertStage(records, stage) {
    if (!stage || !stage.id) return records;
    const copy = (records || []).slice();
    const i = copy.findIndex((r) => r && r.id === stage.id);
    if (i >= 0) {
        const prev = copy[i];
        const prevSettled = prev.phase === PHASE_DONE || prev.phase === PHASE_FAILED;
        const n = (prev.n || 1) + (prevSettled ? 1 : 0);   // 已落定后再来 = 新一次调用
        const merged = Object.assign({}, prev, stage);
        // 保真 verb：真实后端完成帧（node="execute"/"narrate"）只带结构节点名、丢了真实工具 verb
        // （tool_start 阶段才带 rank/rerank…）——沿用 tool_start 捕获的真实 verb，否则压缩计数
        // 会把一次检索误落「本地处理」兜底。只有 stage 带来的是**已知工具类** verb 才覆盖。
        if (!FLOW_TOOL_KIND[String(stage.verb || "").trim()] && prev.verb) merged.verb = prev.verb;
        copy[i] = Object.assign(merged, { n });
    } else {
        copy.push(Object.assign({ n: 1 }, stage));
    }
    return copy;
}

/* 待渲染的工具行列表（有序、按 id 去重兜底；upsertStage 已保证，这里是防御）。 */
export function renderableStages(records) {
    const recs = (records || []).slice();
    const seen = new Set();
    const out = [];
    recs.forEach((r) => {
        if (!r || !r.id || seen.has(r.id)) return;
        seen.add(r.id);
        out.push(r);
    });
    return out;
}

/* ---------- 压缩（用户重申：工具调用次数按类别加和，缩减为一行） ----------
   records = 未被覆盖的完整行（含 result）。
   返回：
   - summaryText：「执行了 1 次检索，1 次联网搜索。」式一行（灰字，不是气泡）；
     无工具行时断串；有失败调用时句尾如实补「（N 次失败）」。
   - kept：保留为产物的行（kind===KIND_RESULT，→ pill）。
   - expanded：可展开回看的完整工具行（= records 全量，只减不增的最完备视图）。
   计数口径：每次调用按 FLOW_TOOL_KIND 归类别（复合工具计多类）；未知 verb 落「本地处理」兜底。 */
export function compressFlow(records) {
    const recs = (records || []).slice();
    const kept = recs.filter((r) => r.kind === KIND_RESULT);
    const tools = recs.filter((r) => r.kind === KIND_TOOL);
    const counts = {};
    let failedTotal = 0;
    tools.forEach((r) => {
        const n = r.n || 1;   // 真实调用次数（同 id 行可能合并了多次调用）
        if (r.phase === PHASE_FAILED) failedTotal += n;
        const kinds = FLOW_TOOL_KIND[String(r.verb || "").trim()] || [FLOW_KIND_FALLBACK];
        kinds.forEach((k) => { counts[k] = (counts[k] || 0) + n; });
    });
    const bits = [];
    FLOW_KIND_ORDER.forEach((k) => { if (counts[k]) bits.push(counts[k] + " 次" + k); });
    if (counts[FLOW_KIND_FALLBACK]) bits.push(counts[FLOW_KIND_FALLBACK] + " 次" + FLOW_KIND_FALLBACK);
    const summaryText = bits.length
        ? "执行了 " + bits.join("，") + (failedTotal ? "（" + failedTotal + " 次失败）" : "") + "。"
        : "";
    return { summaryText, kept, expanded: recs };
}

/* ---------- 覆盖丢弃（用户拍板：supersede 即丢弃，连存储也丢；跨 query 不丢） ----------
   shouldDiscardOutcome(prev, next, opts) → bool：next 成功 supersede prev 时丢弃 prev。
   prev / next：批对象（带 .payload / .scope_fingerprint / .kind）或裸 payload。
   判据：
     - next 必须有效（payload.ok===true 且 results 是数组）；
     - opts.sameQuery!==false（默认同 query）才可能 supersede——跨 query 不丢；
     - **空结果集**（0 命中）：只丢弃「同一 query 重检索链」的批——prev 是初步批（preliminary）
       或上一轮 re-search（search_rerun/rescue/rerank），且 next 是 re-search 档（search_rerun/
       rescue/rerank）。理由：re-search 档是「对同一 query 的重检索」，0 命中是诚实答案、旧链批撤下；
       而跨意图的**独立 rank 批**（多意图各自成批）不是同一 query 链，**保留**（用户铁律）。
       注：一轮内所有批的 `query_raw` 均为本轮用户全文（turn.py 溯源契约），无法区分意图，
       故用 kind 判别；同 scope 空批 / 跨 query（sameQuery=false）不误伤。
     - 非空结果集，满足其一即 supersede：① prev 是初步批（preliminary）且 next 是后续结果；
       ② 同 scope 且 next 排序层 ≥ prev 排序层（更强/同层重检成功）；
       ③ opts.forceSupersede===true（调用方已确定是同一 query 的重检）。
   排序层口径与 batch_select.rankingLevel 一致（规则=1 / +local_semantic=2 / +llm_rerank=3）。 */
/* re-search 档 kind（对已有 query 的重检索）：search.rerun 采纳批、rescue 救回批、rerank 重排批。
   独立的新检索 rank 批不在此列（多意图时每意图一个 rank，彼此保留）。 */
const FLOW_RE_SEARCH_KINDS = { "search_rerun": true, "rescue": true, "rerank": true };
function _isReSearchKind(k) { return FLOW_RE_SEARCH_KINDS[String(k || "")] === true; }

export function shouldDiscardOutcome(prev, next, opts) {
    opts = opts || {};
    if (!prev || !next) return false;
    const pNext = (next.payload && typeof next.payload === "object") ? next.payload : next;
    if (pNext.ok !== true) return false;
    if (!Array.isArray(pNext.results)) return false;   // 必须是数组；空数组合法（0 命中走下面分支）
    if (opts.sameQuery === false) return false;
    if (opts.forceSupersede === true) return true;

    const pPrev = (prev.payload && typeof prev.payload === "object") ? prev.payload : prev;
    const prevScope = String((prev.scope_fingerprint || pPrev.scope_fingerprint) || "");
    const nextScope = String((next.scope_fingerprint || pNext.scope_fingerprint) || "");
    const prevKind = String(prev.kind || pPrev.kind || "");
    const nextKind = String(next.kind || pNext.kind || "");
    const prevIsPrelim = prevKind === "preliminary";

    /* 空结果集（0 命中）：只丢弃同一 query 重检索链的批（preliminary / 上一轮 re-search），
       且胜者是 re-search 档；跨意图独立 rank 批保留。 */
    if (pNext.results.length === 0) {
        const prevIsChain = prevIsPrelim || _isReSearchKind(prevKind);
        return _isReSearchKind(nextKind) && prevIsChain;
    }

    const levelOf = (p) => {
        const t = p.search_trace;
        if (!t || !Array.isArray(t.steps) || !t.steps.length) return null;
        const used = new Set();
        t.steps.forEach((s) => { if (s && s.status === "used" && s.id) used.add(s.id); });
        if (used.has("llm_rerank")) return 3;
        if (used.has("local_semantic")) return 2;
        return 1;
    };
    const nextLevel = levelOf(pNext);
    const prevLevel = levelOf(pPrev);
    const sameScope = Boolean(prevScope && nextScope && prevScope === nextScope);
    const strongerOrEqual = (nextLevel != null && prevLevel != null && nextLevel >= prevLevel);
    return prevIsPrelim || (sameScope && strongerOrEqual);
}
