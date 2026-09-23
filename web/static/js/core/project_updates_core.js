"use strict";

/* 追踪更新检查 · 纯逻辑核（设计 v2 §4「追踪更新检查闭环」）
 *
 * ## 本文件是什么 / 不是什么
 *
 * - 是：更新检查的**纯逻辑**——diff（相对保存的 baseline 比 `/api/watch/check` 确定性重跑
 *   结果）、material change 判定（真实新增/消失/指纹变化；排序/score/文案不算）、
 *   截断语义（>200 不得声称「某条已从全部结果消失」）、「检索规则已更新」单列、
 *   上游同步编排与语料代哨兵的纯推导（2026-08-26 全体批量按钮已撤）。
 * - 不是：DOM、IndexedDB、网络、墙钟。时间一律经参数注入（updatesSetClock 注入点，
 *   node 规格可逐字段断言）；请求 /api/watch/check、写库（baseline/候选回填）、
 *   会话内「待查看更新」内存态全在 UI 壳 project_updates.js。
 * - 零 `#` import：本文件只相对 import `./artifacts.js`（纯数据层，同零 DOM）——
 *   不进 import 图、不进环（projects_core.js 同款先例），node 规格直接相对路径 import。
 *
 * ## diff 的语义边界（逐字对照）
 *
 * - 比较「是否满足条件」，不比较名次：**真实新增/消失、规范化 sample_size 变化、
 *   raw_data_status 变化**才挂「有变化」；排序/score/文案/格式变化不算（后端语义指纹
 *   record_fingerprint_schema v1 只哈希 {dataset_uid, sample_size, raw_data_status}）。
 * - **截断不撒谎**：`r.truncated=true`（本次结果 >200）时，baseline 里没出现在本次
 *   前 200 的 uid ≠ 从全部结果消失（可能在 201+）——removed 判定关闭，UI 如实写
 * 「结果超 200 条被截断，无法判定消失」（原文）。
 * - 对称地：`baseline.truncated=true`（上次快照本身 >200、只存了前 200）时，本次
 *   uid 集合里「新出现」的 uid 可能本来就在 201+ ——added 判定关闭，如实提示
 *   「上次结果超 200 条被截断，无法判定新增」。**双侧都在前 200 的 uid 的指纹比较
 *   恒可靠**（交集不受截断影响）。
 * - **检索规则升级单列**：请求用的 spec_version 与响应 executed_spec.spec_version
 *   不一致 → `ruleUpdated=true`（「检索规则已更新」），不伪装成目录变化（§4.3）。
 */

import { artifactsUidSet } from "./artifacts.js";

/* ---------- 常量（数值与逐字对齐；文案是上屏承诺，不许漂） ---------- */

/* 检索规格版本（与后端 `RECORD_FINGERPRINT_SCHEMA` / `WatchCheckRequest.spec_version`
   校验同值，见 webapp.py:2956-2960/3008-3013）。前后端各自硬编码、契约门对拍。
   保存的 spec.spec_version 与它不一致 → 「检索规则已更新」（§4.3 单列）。 */
export const WATCH_SPEC_VERSION = "v1";

/* ---------- 2026-08-26：上游同步编排 + 语料代哨兵 ----------
 * 「检查 N 个追踪的更新」全体按钮已撤（用户：点一下太耗费资源），批量面板随之退役
 * （WATCH_BATCH_MAX/watchBatchSlice/watchBatchRestText/watchSummaryText 一并删除）；
 * 批量诉求由「登录后语料代哨兵自动刷新」承接（零 LLM 顺序重跑，见 project_updates.js）。 */

/* 单追踪「检查更新」向上追溯：轮询语料同步 job 的间隔/上限（1.5s × 200 ≈ 5 分钟）。 */
export const WATCH_SYNC_POLL_MS = 1500;
export const WATCH_SYNC_POLL_MAX = 200;
/* 轮询超时固定文案（如实：后台还在跑，本次不重跑检查） */
export const WATCH_SYNC_TIMEOUT_COPY = "后台仍在同步，稍后再看";
/* 另一个更新任务进行中（job 冲突/跨进程 sync_busy）的友好文案 */
export const WATCH_SYNC_BUSY_COPY = "另一个更新任务进行中，请稍候";

/* 截断文案（原文口径）：本次结果 >200 时消失不可判定 */
export const WATCH_TRUNCATED_REMOVED_COPY = "结果超 200 条被截断，无法判定消失";
/* 上次快照 >200 时新增不可判定（对称语义；如实提示，不虚构） */
export const WATCH_TRUNCATED_ADDED_COPY = "上次结果超 200 条被截断，无法判定新增";
/* 无变化状态行（包描述原文：「无变化如实写『本次检查无变化 · 刚检查过』」） */
export const WATCH_NO_CHANGE_COPY = "本次检查无变化 · 刚检查过";
/* 检索规则升级单列（原文） */
export const WATCH_RULE_UPDATED_COPY = "检索规则已更新";
/* baseline=null（基线生成失败过）时的按钮文案（包描述原文） */
export const WATCH_RETRY_BASELINE_COPY = "重试生成基线";
/* 正常检查按钮文案 */
export const WATCH_CHECK_COPY = "检查更新";
/* 待查看更新条目类型标签：新增 uid / 指纹变化 uid / 真实消失（仅提示，不提供纳入） */
export const WATCH_KIND_ADDED = "added";      // 真实新增（相对全量基线）
export const WATCH_KIND_FP = "fp";            // 语义指纹变化（sample_size/raw_data_status 变了）
export const WATCH_KIND_REMOVED = "removed";  // 真实消失（仅本次全量时可判定）

/* ---------- 时钟注入（与 projects_core/artifacts 同款：规格里时间确定性） ---------- */
let _now = null;
export function updatesSetClock(fn) { _now = (typeof fn === "function") ? fn : null; }

/* ---------- 可检查追踪（自动刷新/计数共用口径） ---------- */

/* 有可重跑检查条件的追踪（check_condition.spec 存在且含关键词/来源/分面任一）。
   spec 是保存时后端校验通过的确定性规格；空 spec 防御性排除（不拿「全库浏览」冒充检查）。 */
export function watchCheckableProjects(projects) {
    return (Array.isArray(projects) ? projects : []).filter((p) => {
        const spec = (p && p.check_condition && p.check_condition.spec) || {};
        return Boolean(String(spec.query || "").trim()
            || (Array.isArray(spec.sources) && spec.sources.length)
            || (Array.isArray(spec.facet_filters) && spec.facet_filters.length));
    });
}

/* ---------- 上游同步编排（单追踪检查先追溯语料同步） ---------- */

/* 从追踪 spec 提取来源列表（去空白；空 → null = 全源，与后端 sources=null 口径一致）。 */
export function watchSpecSources(spec) {
    const raw = (spec && Array.isArray(spec.sources)) ? spec.sources : [];
    const out = raw.map((s) => String(s || "").trim()).filter(Boolean);
    return out.length ? out : null;
}

/* 语料同步 job 状态判读（GET /api/curate/sync-updates/status 的 job 字段）：
   done → {done:true, result}；failed → {done:true, error}；其余/缺字段 → {done:false}。 */
export function watchSyncJobState(job) {
    const j = (job && typeof job === "object") ? job : {};
    if (j.status === "done") return { done: true, result: (j.result && typeof j.result === "object") ? j.result : {} };
    if (j.status === "failed") return { done: true, error: String(j.error || "未知错误") };
    return { done: false };
}

/* 上游同步结果一行（合并渲染用）：imported_total>0 → 「上游同步：新增 N 条入库」；
   0 → 「上游同步：已是最新」；无结果 → null（不渲染）。 */
export function watchUpstreamText(result) {
    if (!result || typeof result !== "object") return null;
    const n = Number(result.imported_total);
    if (!Number.isFinite(n) || n < 0) return null;
    return n > 0 ? "上游同步：新增 " + n + " 条入库" : "上游同步：已是最新";
}

/* ---------- 语料代哨兵（登录后自动刷新） ---------- */

/* 语料代比对：current 不可得（null/空串）→ false（降级：跳过自动刷新，不报错）；
   否则 stored !== current → 需要刷新。 */
export function watchGenChanged(storedGen, currentGen) {
    const cur = String(currentGen || "").trim();
    if (!cur) return false;
    return String(storedGen || "") !== cur;
}

/* 自动刷新完成后的一次性 toast：有 delta 的追踪数 >0 → 「N 个追踪有新数据」；0 → null（不打扰）。 */
export function watchAutoRefreshToast(changedCount) {
    const n = Number(changedCount) || 0;
    return n > 0 ? n + " 个追踪有新数据" : null;
}

/* ---------- diff（相对保存的 baseline 比本次确定性重跑结果） ---------- */

/* 纯函数。入参：
 *   r             /api/watch/check 的 result（result_total/uids[]/fingerprints{uid:fp}/
 *                 truncated/executed_spec/checked_at；缺字段防御为空）
 *   baseline      check_condition.baseline（{uids[], fingerprints{}, result_total,
 *                 truncated, generated_at}；null/缺 uids → 视作「基线不存在」，输出
 *                 kind="baseline"，调用方走「重试生成基线」路径）
 *   sentSpecVersion  本次请求实际携带的 spec.spec_version（null/undefined 缺省不判
 *                 ruleUpdated——旧数据没有该字段时不虚构）
 * 返回：
 *   { kind: "diff"|"baseline",
 *     added[], fpChanged[], removed[],   // 各自保持确定性顺序（r.uids / r.uids / baseline.uids）
 *     addedTrusted, removedTrusted,      // 截断语义开关（见文件头注释）
 *     ruleUpdated, resultTotal, truncated, baselineTruncated }
 * 语义：集合比较（artifactsUidSet 去重归一），**绝不按数组下标比名次**（§4.3 不比较名次）；
 * 指纹变化只比双侧交集（同 uid 才可比，交集不受截断影响）。 */
export function watchDiff(r, baseline, sentSpecVersion) {
    const res = (r && typeof r === "object") ? r : {};
    const resUids = artifactsUidSet(res.uids);
    const resFps = (res.fingerprints && typeof res.fingerprints === "object") ? res.fingerprints : {};
    const base = (baseline && typeof baseline === "object") ? baseline : {};
    const baseUids = artifactsUidSet(base.uids);

    /* 基线不存在（保存追踪时基线生成失败 / 老数据无 baseline）：只做基线生成，无 diff。 */
    if (!baseUids.length && !(base && Array.isArray(base.uids))) {
        return {
            kind: "baseline",
            added: [], fpChanged: [], removed: [],
            addedTrusted: true, removedTrusted: true,
            ruleUpdated: false,
            resultTotal: Number(res.result_total) || 0,
            truncated: res.truncated === true,
            baselineTruncated: false,
        };
    }

    const baseFps = (base.fingerprints && typeof base.fingerprints === "object") ? base.fingerprints : {};
    const resSet = new Set(resUids);
    const baseSet = new Set(baseUids);

    /* 真实新增 = 本次全量结果里有、保存的 baseline 里没有。仅当保存的基线是全量快照
       （!baseline.truncated）时才可判定——基线本身 >200 时它只是前 200，多出的 uid
       可能本来就在 201+，不能声称「新增」（如实提示，见文件头）。不可判定时数组置空，
       调用方只渲染截断提示行，不假装列「新增」。 */
    const baselineTruncated = base.truncated === true;
    const addedTrusted = !baselineTruncated;
    const added = addedTrusted ? resUids.filter((u) => !baseSet.has(u)) : [];

    /* 真实消失 = 保存的 baseline 里有、本次全量结果里没有。仅当本次结果全量
       （!res.truncated）时才可判定——本次 >200 时 baseline 里的 uid 可能仍在 201+，
       「消失」是错误断言（原文「不得声称某条已从全部结果消失」）。
       不可判定时数组置空，调用方只渲染「结果超 200 条被截断，无法判定消失」。 */
    const truncated = res.truncated === true;
    const removedTrusted = !truncated;
    const removed = removedTrusted ? baseUids.filter((u) => !resSet.has(u)) : [];

    /* 指纹变化 = 双侧都在结果里（交集）且语义指纹不同的 uid——交集不受截断影响，
       恒可判定；sample_size/raw_data_status 变化是 material change（§4.3）。 */
    const fpChanged = resUids.filter((u) => baseSet.has(u) && String(resFps[u] || "") && String(resFps[u]) !== String(baseFps[u] || ""));

    /* 检索规则升级单列：请求携带的 spec_version 与后端规范化后的 executed_spec.spec_version
       不一致 → 「检索规则已更新」（§4.3），不伪装成目录变化。sentSpecVersion 缺省不判。 */
    const executed = (res.executed_spec && typeof res.executed_spec === "object") ? res.executed_spec : {};
    const ruleUpdated = Boolean(sentSpecVersion !== null && sentSpecVersion !== undefined && executed.spec_version)
        && String(sentSpecVersion) !== String(executed.spec_version);

    return {
        kind: "diff",
        added: added,
        fpChanged: fpChanged,
        removed: removed,
        addedTrusted: addedTrusted,
        removedTrusted: removedTrusted,
        ruleUpdated: ruleUpdated,
        resultTotal: Number(res.result_total) || 0,
        truncated: truncated,
        baselineTruncated: baselineTruncated,
    };
}

/* 待查看更新条目数（material change 合计，只看可判定的部分）：
   新增 + 指纹变化 + 真实消失。排序/score/文案变化不在其中（§4.3）。 */
export function watchChangedCount(diff) {
    const d = diff || {};
    return d.added.length + d.fpChanged.length + d.removed.length;
}

/* 待查看更新逐条结构（UI 逐条渲染/处理用）：[{uid, kind}]。
   kind ∈ WATCH_KIND_ADDED / WATCH_KIND_FP / WATCH_KIND_REMOVED。
   removed 条目只提示「已不在结果中」，**不提供「纳入候选」**——它不在结果里，无法纳入
   （逐条比较后决定纳入/排除；纳入只对存在的数据集有意义）。 */
export function watchDeltaEntries(diff) {
    const d = diff || {};
    const out = [];
    d.added.forEach((uid) => out.push({ uid: uid, kind: WATCH_KIND_ADDED }));
    d.fpChanged.forEach((uid) => out.push({ uid: uid, kind: WATCH_KIND_FP }));
    d.removed.forEach((uid) => out.push({ uid: uid, kind: WATCH_KIND_REMOVED }));
    return out;
}

/* diff 三态计数（新增/信息变化/消失）——供单追踪检查结构化 outcome 与列表徽章用。 */
export function watchDiffCounts(diff) {
    if (!diff || diff.kind !== "diff") return { added: 0, fp: 0, removed: 0 };
    const entries = watchDeltaEntries(diff) || [];
    let added = 0, fp = 0, removed = 0;
    entries.forEach((d) => {
        if (d.kind === WATCH_KIND_ADDED) added++;
        else if (d.kind === WATCH_KIND_FP) fp++;
        else if (d.kind === WATCH_KIND_REMOVED) removed++;
    });
    return { added, fp, removed };
}

/* 单追踪检查后的状态行文案（/ 包描述逐字）：
   - 无变化 → 「本次检查无变化 · 刚检查过」
   - 有变化 → 「发现 N 项变化，见下方待查看更新」（逐条处理入口在挂载区）
   - 截断提示单独行（watchTruncatedNote，见下）；ruleUpdated 提示单独行
   count 为 watchChangedCount(diff) 的数值。 */
export function watchStatusText(count) {
    const n = Number(count) || 0;
    return n > 0 ? "发现 " + n + " 项变化，见下方待查看更新" : WATCH_NO_CHANGE_COPY;
}

/* 单追踪检查回执（「结果汇总如实呈现」+ 用户口径「检查了 N 个，X 个有更新
   Y 个已是最新」）：resultTotal = 本次确定性重跑的结果总数；changed = material change 数
   （watchChangedCount）；counts = {added, fp, removed}（watchDiffCounts）。
   有更新 → 「检查了 N 条记录，X 条有更新（新增a · 信息变化b · 消失c），Y 条与上次一致」；
   无更新 → 「检查了 N 条记录，均与上次一致，无更新」。Y = resultTotal − added − fp
   （removed 不在本次结果里，不占「与上次一致」名额）。 */
export function watchReceiptText(resultTotal, changed, counts) {
    const total = Number(resultTotal) || 0;
    const n = Number(changed) || 0;
    const c = counts || {};
    const unchanged = Math.max(0, total - (Number(c.added) || 0) - (Number(c.fp) || 0));
    if (n > 0) {
        const bits = [];
        if (c.added) bits.push("新增 " + c.added);
        if (c.fp) bits.push("信息变化 " + c.fp);
        if (c.removed) bits.push("消失 " + c.removed);
        return "检查了 " + total + " 条记录，" + n + " 条有更新（" + bits.join(" · ") + "），"
            + unchanged + " 条与上次一致";
    }
    return "检查了 " + total + " 条记录，均与上次一致，无更新";
}

/* 截断如实提示（diff 后按需单独渲染一行）：本次 >200 → 「结果超 200 条被截断，无法判定
   消失」；上次 >200 → 「上次结果超 200 条被截断，无法判定新增」；双侧都截断时先报本次。
   无可疑截断 → null（不渲染）。 */
export function watchTruncatedNote(diff) {
    const d = diff || {};
    if (d.truncated === true) return WATCH_TRUNCATED_REMOVED_COPY;
    if (d.baselineTruncated === true) return WATCH_TRUNCATED_ADDED_COPY;
    return null;
}

/* ---------- 自动刷新 ---------- */

/* 单追踪是否有 material change（watch_checked{changed} 埋点载荷用）：changed=1/0。 */
export function watchChangedFlag(diff) {
    return watchChangedCount(diff) > 0 ? 1 : 0;
}
