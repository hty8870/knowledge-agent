"use strict";
/* benchmark 采集反馈 · **纯核**（无 DOM / 无 localStorage / 无墙钟 / 无网络，node 可单测）
 *
 * 职责：把「一轮真实交互」的结构化记录做纯变换——请求脱敏、评分合并、FIFO 裁剪、
 * 导出包构造、包内容统计、会话级评分降频闸。存储、开关、界面全在 benchfb.js；
 * 这里只做可逐字节断言的数学。
 * 分层理由同 usage_core.js：聚合与脱敏逻辑在 node 里按真行为断言，不起浏览器。
 *
 * ## 这个功能采什么（与 usage_core 的聚合文本反馈是两条并行通道）
 *
 * 聚合文本反馈（usage_core）回答「大家 overall 怎么用」；本模块回答「这一句查询，
 * 系统到底干了什么、结果对不对」——为 benchmark 制作供数。所以这里记的是**全量**：
 * 用户原话、路由应答（含 agent plan.trace 逐步执行记录）、检索请求与完整响应
 * （results 全字段 + search_trace 全步骤）、工具执行回执、耗时、环境（模型名/端点主机）、
 * 用户评分（2026-08-22 起：完成度三选 + 可选原因 + 逐条有用标注 + 自由评语；
 * 此前的星级已退役——三选对 benchmark 的判定价值高于 1-5 连续分）。产品方原话：
 * 「信息量充足，宁多勿少」。
 *
 * ## 脱敏是结构性的，不靠记性
 *
 * 检索/路由请求体里带着用户自配的 `api_key` 与 `base_url`。**记录落盘前必经
 * benchfbStripRequest**：api_key 整键删除，base_url 只留主机名（复现实验只需知道
 * 「deepseek 官方端点」还是「自建代理」，路径里可能带用户私有信息，整段不留）。
 * 契约门（tests/test_benchfb_contract.py）对这条做机械断言。
 *
 * ## 纯核纪律（同 usage_core）
 *
 * 不碰墙钟（Date 一律由调用方注入）、不碰随机数（id 由记录层注入）、不碰 DOM/存储。
 * 评分校验（完成度三选、原因白名单、标注去重、评语截断）全部在这里做，界面层只传原始输入。
 */

export const BENCHFB_SCHEMA = "biodata-benchfb/1";

/* 记录条数与体量双上限。每条记录含完整检索响应（≈20-60KB），60 条 ≈ 2-3MB，
   离 localStorage 5MB 有余量（usage/历史/记忆同住）。双闸任一触发即从最旧端丢（FIFO）：
   新近的真实查询对 benchmark 价值最高。 */
export const BENCHFB_MAX_RECORDS = 60;
export const BENCHFB_MAX_CHARS = 3200000;

export const BENCHFB_MAX_COMMENT = 500;   // 评语上限（字）

/* 完成度三选（2026-08-22 评分结构改版，取代星级）：记录里存机器可读 token，
   界面文案（完成 / 部分完成 / 未完成）由 benchfb.js 映射。 */
export const BENCHFB_COMPLETIONS = ["done", "partial", "failed"];

/* 可选原因白名单（中文标签原样入档——benchmark 消费侧直接读，不再二次映射）。
   选择是可选的、可多选；不选不代表「没有原因」，只代表用户没填。 */
export const BENCHFB_REASONS = ["条件理解错", "排序不对", "缺关键文件", "下载失败", "执行没完成", "解释不可信", "其他"];

/* 会话级评分降频（2026-08-22 派发口径「就你最关注的地方出现两次就行」）：
   每 tab 会话主动完整评分卡 ≤ BENCHFB_PROACTIVE_CAP 张；收起/不评分连续
   BENCHFB_IGNORE_CAP 次 → 本会话不再主动出卡。状态存 sessionStorage
   （与 biodata_sid_v1 同生命周期）：**刷新页面会话重置是可接受的会话语义**——
   刷新后被降频的用户会重新看到主动卡，这是刻意选择而非缺陷。none/error 轮的
   折叠「评价」按钮不计配额、不受降频影响（它本来就不是主动卡）。 */
import { usagePad2 } from "#usage_core";

export const BENCHFB_RATE_SESSION_KEY = "biodata_benchfb_rate_v1";
export const BENCHFB_PROACTIVE_CAP = 2;
export const BENCHFB_IGNORE_CAP = 2;

/* 两位补零唯一定义在 usage_core.usagePad2（usage/benchfb 日期戳同族），本模块直接 import 用。 */

/* base_url → 主机名。非法/空 → ""。只留主机（api.deepseek.com 这一级），
   路径/查询串可能带用户私有代理信息，整段不采。 */
export function benchfbEndpointHost(baseUrl) {
    const s = String(baseUrl || "").trim();
    if (!s) return "";
    try { return new URL(s).host || ""; } catch (_e) { return ""; }
}

/* 请求体脱敏：返回**新对象**（不改原引用——原对象还要发往后端）。
   api_key 整键删除；base_url 换成主机名。其余字段（模型名、开关、筛选、原话）原样保留——
   它们是复现这次检索所必需的全部现场。 */
export function benchfbStripRequest(req) {
    const out = {};
    const src = (req && typeof req === "object") ? req : {};
    Object.keys(src).forEach(function (k) {
        if (k === "api_key") return;
        if (k === "base_url") { out.base_url = benchfbEndpointHost(src[k]); return; }
        out[k] = src[k];
    });
    return out;
}

/* 记录 id：注入时间戳与随机串（纯核不自己碰墙钟/随机源）。 */
export function benchfbMakeId(ts, rand) {
    return "r" + Number(ts || 0).toString(36) + String(rand || "").replace(/[^a-z0-9]/gi, "").slice(0, 6).toLowerCase();
}

/* 评分合并：返回**新记录对象**（旧记录不被改——导出与提示卡各拿各的快照，不漂移）。
   2026-08-22 起记录形状（additive，不再写 stars）：
     rating = {completion, reasons, useful_idx, comment, rated_at}
   patch.completion："done"/"partial"/"failed" 入库；null/"" 显式清除；undefined/非法值保留旧值。
   patch.reasons：原因标签数组（限 BENCHFB_REASONS 白名单，去重、按白名单序）；undefined 保留旧值。
   patch.usefulIdx：1-based 结果名次数组（用户点的是卡片，卡片名次=结果数组序号），去重、剔非正整数。
   patch.comment：自由评语，截断到 BENCHFB_MAX_COMMENT。
   patch.ratedAt：评分时刻（ms，注入）。 */
export function benchfbRate(record, patch) {
    const out = Object.assign({}, record);
    const prev = (record && record.rating && typeof record.rating === "object") ? record.rating : {};
    const p = patch || {};
    let completion = prev.completion || null;
    if (p.completion === null || p.completion === "") completion = null;
    else if (p.completion !== undefined && BENCHFB_COMPLETIONS.indexOf(p.completion) >= 0) completion = p.completion;
    let reasons = (Array.isArray(prev.reasons) ? prev.reasons : []).filter(function (r) { return BENCHFB_REASONS.indexOf(r) >= 0; });
    if (Array.isArray(p.reasons)) {
        const picked = new Set();
        (p.reasons || []).forEach(function (r) { if (BENCHFB_REASONS.indexOf(r) >= 0) picked.add(r); });
        reasons = BENCHFB_REASONS.filter(function (r) { return picked.has(r); });
    }
    const seen = new Set();
    const usefulIdx = [];
    (Array.isArray(p.usefulIdx) ? p.usefulIdx : (prev.useful_idx || [])).forEach(function (v) {
        const n = Number(v);
        if (!Number.isInteger(n) || n < 1 || seen.has(n)) return;
        seen.add(n);
        usefulIdx.push(n);
    });
    usefulIdx.sort(function (a, b) { return a - b; });
    const comment = String(p.comment !== undefined ? p.comment : (prev.comment || "")).slice(0, BENCHFB_MAX_COMMENT);
    out.rating = {
        completion: completion,
        reasons: reasons,
        useful_idx: usefulIdx,
        comment: comment,
        rated_at: Number(p.ratedAt) || prev.rated_at || 0,
    };
    return out;
}

/* ---------- 会话级评分降频闸（纯函数；状态存取在 benchfb.js）----------
   状态形状：{shown: [recId…]（本会话已主动出过的完整卡，按序）, pending: recId|null
   （最近一张尚未被评分/收起的主动卡）, ignored: n（连续忽略计数）}。
   全部函数返回**新状态对象**，不改入参；recId 一律字符串化。 */

export function benchfbRateSession(raw) {
    const s = (raw && typeof raw === "object") ? raw : {};
    const shown = [];
    (Array.isArray(s.shown) ? s.shown : []).forEach(function (id) {
        id = String(id || "");
        if (id && shown.indexOf(id) < 0) shown.push(id);
    });
    const pending = String(s.pending || "");
    return { shown: shown, pending: (pending && shown.indexOf(pending) >= 0) ? pending : null, ignored: Math.max(0, Number(s.ignored) || 0) };
}

/* 这张卡能否**主动**出完整卡：已计过额的卡（重画挂回）一律放行；否则受双重闸——
   会话主动卡 < BENCHFB_PROACTIVE_CAP 且连续忽略 < BENCHFB_IGNORE_CAP。 */
export function benchfbProactiveAllowed(state, recId) {
    const s = benchfbRateSession(state);
    const id = String(recId || "");
    if (id && s.shown.indexOf(id) >= 0) return true;
    return s.shown.length < BENCHFB_PROACTIVE_CAP && s.ignored < BENCHFB_IGNORE_CAP;
}

/* 一张新主动卡上屏：上一张 pending 卡到那时仍未评分 → 忽略 +1（评过分则清零重来）；
   本卡计入 shown 并成为新 pending。已计过额的卡重复上屏（重画）不改变状态。 */
export function benchfbNoteShown(state, recId, prevPendingRated) {
    const s = benchfbRateSession(state);
    const id = String(recId || "");
    if (!id || s.shown.indexOf(id) >= 0) return s;
    let ignored = s.ignored;
    if (s.pending) ignored = prevPendingRated ? 0 : ignored + 1;
    return { shown: s.shown.concat([id]), pending: id, ignored: ignored };
}

/* 任何一张卡被评分（完成度/原因/评语/标有用任一写入）：连续忽略清零；
   若评的正是 pending 卡，pending 核销。 */
export function benchfbNoteRated(state, recId) {
    const s = benchfbRateSession(state);
    const id = String(recId || "");
    return { shown: s.shown, pending: (s.pending === id) ? null : s.pending, ignored: 0 };
}

/* 主动卡被「收起」：未评分即收起 = 忽略 +1；已评分的收起只是 UI 偏好，不计。 */
export function benchfbNoteDismissed(state, recId, wasRated) {
    const s = benchfbRateSession(state);
    const id = String(recId || "");
    if (wasRated) return s;
    return { shown: s.shown, pending: (s.pending === id) ? null : s.pending, ignored: s.ignored + 1 };
}

/* FIFO 裁剪：条数与总字符双闸，从最旧端丢。返回新数组。 */
export function benchfbTrim(records, maxRecords, maxChars) {
    const maxR = Number(maxRecords) > 0 ? Number(maxRecords) : BENCHFB_MAX_RECORDS;
    const maxC = Number(maxChars) > 0 ? Number(maxChars) : BENCHFB_MAX_CHARS;
    const list = (Array.isArray(records) ? records : []).slice();
    while (list.length > maxR) list.shift();
    let size = 0;
    try { size = JSON.stringify(list).length; } catch (_e) { size = maxC + 1; }
    while (list.length && size > maxC) {
        list.shift();
        try { size = JSON.stringify(list).length; } catch (_e) { size = 0; }
    }
    return list;
}

/* 导出前把「标注的名次」解析成「标注的数据集」：rating.useful_idx（1-based 名次）
   对照 record.search.res.results 取 dataset_uid/名称。解析不出的名次如实丢弃（结果数组
   可能在那之后变了——记录的是当时那一屏）。
   返回 [{idx, uid, name}]，无评分或无检索段 → []。 */
export function benchfbResolveUseful(record) {
    const r = record || {};
    const idxList = (r.rating && Array.isArray(r.rating.useful_idx)) ? r.rating.useful_idx : [];
    if (!idxList.length) return [];
    const results = (r.search && r.search.res && Array.isArray(r.search.res.results)) ? r.search.res.results : [];
    const out = [];
    idxList.forEach(function (idx) {
        const it = results[idx - 1];
        if (!it) return;
        out.push({
            idx: idx,
            uid: String(it.dataset_uid || ""),
            name: String(it.dataset_name || it.title || ""),
        });
    });
    return out;
}

/* 单条记录的导出形态：原记录 + rating.useful_resolved（标注名次 → 数据集）
   + rating.useful_uids（同一解析的纯 uid 清单，2026-08-22 起随新评分形状带出）。
   响应/轨迹全字段原样带上（采什么发什么，所见即所发在导出弹窗里有原文预览兜底）。 */
export function benchfbForExport(record) {
    const out = Object.assign({}, record);
    if (out.rating) {
        const resolved = benchfbResolveUseful(record);
        out.rating = Object.assign({}, out.rating, {
            useful_resolved: resolved,
            useful_uids: resolved.map(function (x) { return x.uid; }),
        });
    }
    return out;
}

/* 导出包构造。opts.exportedAt（ISO 串）与 opts.installId 由记录层注入；
   opts.app = {cache_generation, ua, lang} 采集侧环境（复现用）。 */
export function benchfbBuildPackage(records, opts) {
    opts = opts || {};
    return {
        schema: BENCHFB_SCHEMA,
        exported_at: String(opts.exportedAt || ""),
        install_id: String(opts.installId || ""),
        client_id: String(opts.clientId || ""),
        profile_id: String(opts.profileId || ""),
        app: {
            cache_generation: String((opts.app && opts.app.cache_generation) || ""),
            ua: String((opts.app && opts.app.ua) || ""),
            lang: String((opts.app && opts.app.lang) || ""),
        },
        records: (Array.isArray(records) ? records : []).map(benchfbForExport),
    };
}

/* 导出文件名：biodata-反馈包-2026-08-13.json。UTC（同 usage_core 的纪律：
   本地时区会让同一份包在不同机器上文件名不同，node 规格无法逐字节断言）。 */
export function benchfbFileName(date) {
    const d = date instanceof Date ? date : new Date(0);
    return "biodata-反馈包-"
        + d.getUTCFullYear() + "-" + usagePad2(d.getUTCMonth() + 1) + "-" + usagePad2(d.getUTCDate())
        + ".json";
}

/* 包内容统计（导出弹窗的「大白话清单」数据源）。
   返回 {turns, search, tool, none, error, rated, comp_done, comp_partial, comp_failed, marked, first_ts, last_ts}。
   2026-08-22：完成度计数取代星级均值（星级已退役）；旧形状记录（带 stars 无 completion）
   仍计入 rated，只是不贡献完成度分项。 */
export function benchfbPackageSummary(records) {
    const s = { turns: 0, search: 0, tool: 0, none: 0, error: 0, rated: 0, comp_done: 0, comp_partial: 0, comp_failed: 0, marked: 0, first_ts: 0, last_ts: 0 };
    (Array.isArray(records) ? records : []).forEach(function (r) {
        if (!r || typeof r !== "object") return;
        s.turns += 1;
        const k = String(r.kind || "");
        if (k === "search") s.search += 1;
        else if (k === "tool") s.tool += 1;
        else if (k === "none") s.none += 1;
        else if (k === "error") s.error += 1;
        const rating = r.rating || {};
        if (rating.completion || rating.stars || (rating.reasons || []).length || (rating.useful_idx || []).length || rating.comment) {
            s.rated += 1;
            if (rating.completion === "done") s.comp_done += 1;
            else if (rating.completion === "partial") s.comp_partial += 1;
            else if (rating.completion === "failed") s.comp_failed += 1;
            s.marked += (rating.useful_idx || []).length;
        }
        const t = Number(r.t) || 0;
        if (t) { if (!s.first_ts || t < s.first_ts) s.first_ts = t; if (t > s.last_ts) s.last_ts = t; }
    });
    return s;
}

/* ESM 出口：本文件全部经 export 提供；node 规格（tests/js/benchfb_core_spec.mjs）直接 import。
   纯核自己不依赖任何界面层——与 usage_core 同一套零依赖纯度门。 */
