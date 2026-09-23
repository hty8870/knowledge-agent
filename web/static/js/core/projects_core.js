"use strict";

/* 追踪 UI 纯逻辑核心（追踪浮窗/存为追踪/上下文卡/首页条共用）。
 *
 * ## 本文件是什么 / 不是什么
 *
 * - 是：追踪**纯逻辑**——「存为追踪」的规格构造（check_condition.spec 与 /api/watch/check
 * 入参逐字段对齐）、追踪草稿构造、上下文卡序列化与 ≤2000 字截断
 *   「上次检查于 X 天前」文案、「候选状态计数」。
 * - 不是：DOM、IndexedDB、网络、墙钟。时间一律经参数注入（projectsSetClock 注入点，
 *   node 规格可逐字段断言）；写库走 `artifacts.js` 的 CRUD，本文件不碰。
 * - 零 `#` import：本文件只相对 import `./artifacts.js`（纯数据层，同零 DOM）——
 *   不进 import 图、不进环（feedback_core.js 同款先例），node 规格直接相对路径 import。
 *
 * ## 上下文卡序列化口径
 *
 * - 字段优先级（截断顺序＝从低到高砍）：候选 → 排除条件 → 纳入条件 → 研究目标（目标最高保）。
 * - 硬 cap 序列化后 ≤2000 Unicode 字符；目标 ≤300；纳入/排除各 ≤8 条（与 artifacts.js
 *   ARTIFACTS_LIMITS 同源）；候选 ≤20 且只含 uid+状态。
 * - 超限按字段优先级截断并显示「另有 N 项未注入」——**不静默截断**（如实第一）。
 */

import { ARTIFACTS_LIMITS, artifactsProvenance, artifactsUidSet } from "./artifacts.js";
import { COPY } from "./copy.js";

/* ---------- 上下文卡常量（的数值上限，UI 与规格共读） ---------- */
export const PROJECTS_CTX_MAX_CHARS = 2000;      // 序列化后硬 cap（Unicode 字符数）
export const PROJECTS_CTX_MAX_GOAL = 300;        // 研究目标 ≤300
export const PROJECTS_CTX_MAX_INCLUDE = ARTIFACTS_LIMITS.MAX_INCLUDE;   // 纳入 ≤8（与数据层同源）
export const PROJECTS_CTX_MAX_EXCLUDE = ARTIFACTS_LIMITS.MAX_EXCLUDE;   // 排除 ≤8（与数据层同源）
export const PROJECTS_CTX_MAX_CANDIDATES = 20;   // 候选 ≤20 且只含 uid+状态

/* ---------- 时钟注入（与 artifacts.js 同款：规格里时间确定性） ---------- */
let _now = null;
export function projectsSetClock(fn) { _now = (typeof fn === "function") ? fn : null; }
export function projectsNow() { return _now ? _now() : Date.now(); }

/* ---------- 追踪 id 生成 ---------- */
export function projectsProjectId(nowMs, rand) {
    /* 默认值可注入（node 规格断言形状）；crypto.randomUUID 缺席时回退时间戳+随机数。 */
    const t = (nowMs === undefined) ? projectsNow() : Number(nowMs) || 0;
    const r = (rand === undefined)
        ? (typeof crypto !== "undefined" && crypto && typeof crypto.randomUUID === "function"
            ? crypto.randomUUID() : Math.random().toString(36).slice(2, 12))
        : String(rand);
    return "prj-" + t.toString(36) + "-" + r;
}

/* ---------- 候选状态计数（浮窗卡片徽标/首页条数字） ---------- */
export function projectsStatusCounts(candidates) {
    /* 四种状态键恒在（0 也显式列出），UI 不猜键。 */
    const out = { 候选: 0, 待核验: 0, 已核验: 0, 已排除: 0 };
    (Array.isArray(candidates) ? candidates : []).forEach((c) => {
        const st = String((c && c.status) || "");
        if (Object.prototype.hasOwnProperty.call(out, st)) out[st] += 1;
    });
    return out;
}

/* ---------- 确定性检索规格构造（check_condition.spec） ----------
   parts 字段（与 /api/recommend 入参 / /api/watch/check 入参逐一对齐）：
   {query, sources, facet_filters[{dim,value}], suppressed_constraints[], lenient_dims[],
    date_from, date_to}。spec_version 恒 "v1"（与 record_fingerprint_schema 同版，端点校验）。 */
export function projectsSpecFromRequest(parts) {
    parts = (parts && typeof parts === "object") ? parts : {};
    const uniq = (arr) => { const s = new Set(); return arr.filter((x) => { if (s.has(x)) return false; s.add(x); return true; }); };
    const facets = (Array.isArray(parts.facet_filters) ? parts.facet_filters : [])
        .map((f) => (f && typeof f === "object" && String(f.dim || "").trim() && String(f.value || "").trim())
            ? { dim: String(f.dim).trim(), value: String(f.value).trim() } : null)
        .filter(Boolean);
    const seen = new Set();
    return {
        spec_version: "v1",
        query: String(parts.query || "").trim(),
        sources: uniq((Array.isArray(parts.sources) ? parts.sources : []).map((x) => String(x || "").trim()).filter(Boolean)),
        facet_filters: facets.filter((f) => { const k = f.dim + "\u0000" + f.value; if (seen.has(k)) return false; seen.add(k); return true; }),
        suppressed_constraints: uniq((Array.isArray(parts.suppressed_constraints) ? parts.suppressed_constraints : [])
            .map((x) => String(x || "").trim()).filter(Boolean)),
        lenient_dims: uniq((Array.isArray(parts.lenient_dims) ? parts.lenient_dims : [])
            .map((x) => String(x || "").trim()).filter(Boolean)),
        date_from: String(parts.date_from || "").trim(),
        date_to: String(parts.date_to || "").trim(),
    };
}

/* ---------- 「存为追踪」草稿构造 ----------
   parts：
   { query（原始检索句，display_query 与默认名/目标来源）,
     specParts（交给 projectsSpecFromRequest）,
     uids[]（当前结果 uid 列表，默认全部「待核验」—— 硬性）,
     provenanceParts（交给 artifactsProvenance：query/retrieval_params/search_trace/filters/
       corpus_digest/policy_id/trace_turn_id/result{truncated}） }
   返回 { input（可直传 artifactsCreateProject 的追踪输入）, spec }。
   检查条件先落 spec+display_query（baseline=null）——基线失败时追踪仍保存、spec 留着供
   P4「稍后在追踪里重试」基线（artifactsSetBaseline 回填）；基线生成成功由调用方再补。 */
export function projectsDraftFromSearch(parts, opts) {
    opts = opts || {};
    parts = (parts && typeof parts === "object") ? parts : {};
    const query = String(parts.query || "").trim();
    const spec = projectsSpecFromRequest(parts.specParts || {});
    const uids = artifactsUidSet(parts.uids);
    const name = query ? (query.length > 40 ? query.slice(0, 40) + "…" : query) : "未命名追踪";
    const provenance = artifactsProvenance(parts.provenanceParts || {}, opts);
    const input = {
        project_id: opts.project_id || projectsProjectId(opts.now),
        name: name,
        goal: query,                          // 检索句即研究目标起点，详情视图可编辑
        include_conditions: [],
        exclude_conditions: [],
        candidates: uids.map((uid) => ({ uid: uid, status: "待核验" })),   // 一律默认「待核验」
        check_condition: {                    // 基线失败也保留 spec（P4 重试基线的依据）
            display_query: query,
            spec: spec,
            baseline: null,
            last_checked_at: "",
        },
        exports: [],
        activity: [],
        provenance: provenance,
    };
    return { input: input, spec: spec };
}

/* ---------- 上下文卡序列化 ----------
   返回 { text（≤2000 Unicode 字符）, omitted（「另有 N 项未注入」的数字，0 = 全量注入） }。
   字段优先级从低到高砍：候选 → 排除 → 纳入 → 目标（目标最高保；自身超 300 字截断加 …）。 */
export function projectsContextSerialize(project, opts) {
    opts = opts || {};
    const p = (project && typeof project === "object") ? project : {};
    const goal = String(p.goal || "").trim();
    const include = (Array.isArray(p.include_conditions) ? p.include_conditions : []).map(String).filter(Boolean);
    const exclude = (Array.isArray(p.exclude_conditions) ? p.exclude_conditions : []).map(String).filter(Boolean);
    const candidates = (Array.isArray(p.candidates) ? p.candidates : [])
        .map((c) => ({ uid: String((c && c.uid) || ""), status: String((c && c.status) || "待核验") }))
        .filter((c) => c.uid);

    let omitted = 0;
    // 候选只取前 20（多余的不注入，如实计入 omitted——「候选 ≤20」）
    let candSlice = candidates.slice(0, PROJECTS_CTX_MAX_CANDIDATES);
    if (candidates.length > PROJECTS_CTX_MAX_CANDIDATES) {
        omitted += candidates.length - PROJECTS_CTX_MAX_CANDIDATES;
    }
    // 纳入/排除各取前 8（数据层已限 ≤8，这里防御性再收一刀，超出的算 omitted）
    let incSlice = include.slice(0, PROJECTS_CTX_MAX_INCLUDE);
    if (include.length > PROJECTS_CTX_MAX_INCLUDE) omitted += include.length - PROJECTS_CTX_MAX_INCLUDE;
    let excSlice = exclude.slice(0, PROJECTS_CTX_MAX_EXCLUDE);
    if (exclude.length > PROJECTS_CTX_MAX_EXCLUDE) omitted += exclude.length - PROJECTS_CTX_MAX_EXCLUDE;

    // 目标自身 ≤300：超长截断加省略号（单字段内截断，不算「项」省略）
    const goalText = goal.length > PROJECTS_CTX_MAX_GOAL ? goal.slice(0, PROJECTS_CTX_MAX_GOAL - 1) + "…" : goal;

    const section = (title, body) => (body ? title + "：" + body : "");
    const listBody = (arr) => arr.map((x, i) => (i + 1) + ". " + x).join("；");
    const candBody = (arr) => arr.map((c) => c.uid + ":" + c.status).join("；");

    let incText = listBody(incSlice);
    let excText = listBody(excSlice);
    let candText = candBody(candSlice);

    // 精确序列化长度（含节标题与 \n 分隔），硬 cap 判据与最终 text 完全一致——
    // 不许用「只算正文」的近似预算（会漏目标与标题开销，实测超 2000 字）。
    const exactLen = () => {
        const segs = [section("研究目标", goalText), section(COPY.conditions.include, incText), section(COPY.conditions.exclude, excText), section("候选", candText)].filter(Boolean);
        return segs.join("\n").length;
    };
    // 硬 cap 2000：从优先级最低的候选开始砍（整体砍掉 → 逐条从尾部砍），再排除、再纳入。
    // 砍多少、砍到哪一档都如实计入 omitted——不静默截断。
    const cutCandidatesFromTail = () => {
        while (candSlice.length && exactLen() > PROJECTS_CTX_MAX_CHARS) {
            candSlice = candSlice.slice(0, candSlice.length - 1);
            omitted += 1;
            candText = candBody(candSlice);
        }
    };
    const cutListFromTail = (get, set) => {
        while (get().length && exactLen() > PROJECTS_CTX_MAX_CHARS) {
            const arr = get();
            set(arr.slice(0, arr.length - 1));
            omitted += 1;
        }
    };
    cutCandidatesFromTail();
    cutListFromTail(() => excSlice, (v) => { excSlice = v; excText = listBody(v); });
    cutListFromTail(() => incSlice, (v) => { incSlice = v; incText = listBody(v); });
    // 目标理论上不可能再触发（目标 ≤300，其余全砍也远小于 2000）；防御性再拦一刀。
    const finalGoal = exactLen() > PROJECTS_CTX_MAX_CHARS
        ? goalText.slice(0, PROJECTS_CTX_MAX_CHARS - (exactLen() - goalText.length) - 3) : goalText;

    const partsArr = [section("研究目标", finalGoal), section(COPY.conditions.include, incText), section(COPY.conditions.exclude, excText), section("候选", candText)];
    const text = partsArr.filter(Boolean).join("\n");
    return { text: text, omitted: omitted };
}

/* ---------- 「上次检查于 X 天前」（信息不是催促；无检查条件不显示、不伪造） ---------- */
export function projectsDaysAgo(ts, nowMs) {
    if (ts === undefined || ts === null || ts === "") return null;
    const t = new Date(String(ts));
    if (!Number.isFinite(t.getTime())) return null;
    const n = new Date(nowMs === undefined ? projectsNow() : Number(nowMs) || 0);
    if (!Number.isFinite(n.getTime())) return null;
    const day = 86400000;
    const diff = n.getTime() - t.getTime();
    if (diff < 0) return 0;                       // 时钟回拨/未来戳：如实算 0（不伪造负数）
    return Math.floor(diff / day);
}

export function projectsLastCheckedText(project, nowMs) {
    /* 无检查条件（check_condition 为 null / 无 last_checked_at）→ ""（UI 不渲染）；
       今天检查过 → 「今天检查过」；否则「上次检查于 N 天前」。 */
    const p = (project && typeof project === "object") ? project : {};
    const cc = p.check_condition;
    const ts = (cc && cc.last_checked_at) ? String(cc.last_checked_at) : "";
    if (!ts) return "";
    const days = projectsDaysAgo(ts, nowMs);
    if (days === null) return "";
    if (days <= 0) return "今天检查过";
    return "上次检查于 " + days + " 天前";
}
