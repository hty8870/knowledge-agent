"use strict";

/* 追踪导出中心纯逻辑核心（导出按钮/台账 diff/命名/折叠/重新下载）。
 *
 * ## 本文件是什么 / 不是什么
 *
 * - 是：导出中心的**纯逻辑**——导出类型与文案常量、追踪候选快照与「相对上次导出的变化」
 * diff（零 LLM，「新增 N 候选 · 状态变化 M」）、台账条目构造（与 `artifacts.js`
 *   `_normExport` 字段逐一对齐）、最新一条/历史折叠推导、重命名变换、无候选提示文案。
 * - 不是：DOM、IndexedDB、网络、墙钟。写台账走 `artifacts.js` 的 CRUD、出网走 `project_exports.js`
 *   （薄 DOM 壳），本文件不碰。时间一律经 `exportSetClock` 注入点（node 规格可逐字段断言）。
 * - 零 `#` import、零相对 import：完全自包含（不进 import 图、不进环），node 直跑。
 *
 * ## 台账 diff 的口径（变化摘要「新增 N 候选 · 状态变化 M」）
 *
 * - `changes` 记录 {added, statusChanged} 供展示，同时携带**本次候选快照**（prevUids/prevStatuses）
 *   作为下一次导出的 diff 基准——台账条目自给自足，不依赖追踪现状倒推「上次是什么样」。
 * - 比较的是候选 **uid 集合 + 状态**（真实新增/状态变化），不比名次/排序/文案
 * （同 material change 的语义家族）。
 * - 移出候选的 uid 不计入摘要（原文只给「新增 N 候选 · 状态变化 M」两个数）。
 */

/* ---------- 常量（的四种导出动作，按钮文案逐字） ---------- */
export const EXPORT_KIND_LABELS = {
    download_list: "导出下载清单",
    citations: "导出引文",
    screening_record: "导出筛选记录",
    full: "导出全部研究材料",
};
/* 导出类型枚举（与后端 content/export_pack.py EXPORT_KINDS 逐字同源——注释里注明后端常量，
   前端不自己造口径）。 */
export const EXPORT_KINDS = ["download_list", "citations", "screening_record", "full"];
/* 导出端点路径：与 webapp.py `@app.post("/api/artifacts/export-pack")` 同源。
   不进 core.js 的 API 表（保持本文件零 import 自包含），用常量注明后端出处。 */
export const EXPORT_API_PATH = "/api/artifacts/export-pack";
/* 导出区挂点（projects.js 详情区内渲染，壳层用选择器发现 + 渲染）。 */
export const EXPORT_MOUNT_SELECTOR = "[data-p5-mount-export]";
/* 台账 meta 响应头：后端把「目录版本（实例级事实）」经响应头回传，前端不自己造。 */
export const EXPORT_META_HEADER = "X-Biodata-Export-Meta";
/* 无候选时的如实提示（按钮禁用 + 本句）。 */
export const EXPORT_NO_CANDIDATE_COPY = "追踪还没有候选数据集，暂时没有可导出的内容。";
/* 台账区文案。 */
export const EXPORT_LATEST_COPY = "最新一次";
export const EXPORT_HISTORY_LABEL = "导出记录";
export const EXPORT_HISTORY_OPEN_COPY = "展开导出记录";
export const EXPORT_HISTORY_CLOSE_COPY = "收起导出记录";

/* ---------- 台账行与导出入口的文案（壳层 project_exports.js 的唯一真源） ---------- */
/* 命名内联编辑。 */
export const EXPORT_NAME_PLACEHOLDER_COPY = "命名这个导出节点";
export const EXPORT_NAME_SAVE_COPY = "保存";
export const EXPORT_NAME_CANCEL_COPY = "取消";
export const EXPORT_NAME_TITLE_COPY = "导出节点命名";
export const EXPORT_NAME_BTN_HINT_COPY = "给这次导出起个名字，方便以后找到它";
export const EXPORT_RENAME_COPY = "改命名";
export const EXPORT_NAME_COPY = "命名";
export const EXPORT_NAME_CLEARED_COPY = "已清除命名";
/* 重新下载（重新生成，不存文件本体）。 */
export const EXPORT_REDL_HINT_COPY = "按追踪当前状态重新生成（不保存文件本体）";
export const EXPORT_REDL_COPY = "重新下载";
/* 导出流程 toast：服务端报错 detail 缺失时的缺省、失败/成功与台账写入失败的措辞。 */
export const EXPORT_FAIL_FALLBACK_COPY = "导出失败";
export function exportNamedCopy(name) { return "已命名「" + name + "」"; }
export function exportNameSaveFailCopy(errMsg) { return "命名保存失败：" + errMsg; }
export function exportLedgerFailCopy(kindLabel, errMsg) {
    return "已下载「" + kindLabel + "」，但台账写入失败：" + errMsg;
}
export function exportDoneCopy(kindLabel) { return "已导出「" + kindLabel + "」，可重新命名或再次下载"; }
export function exportFailCopy(errMsg) { return "导出失败：" + errMsg; }

/* ---------- 时钟注入（与 artifacts/projects 同款：规格里时间确定性） ---------- */
let _now = null;
export function exportSetClock(fn) { _now = (typeof fn === "function") ? fn : null; }
export function exportNow() { return _now ? _now() : Date.now(); }
export function exportIsoNow(ts) {
    const t = (ts === undefined || ts === null) ? exportNow() : ts;
    const d = (t instanceof Date) ? t : new Date(Number(t) || 0);
    return Number.isFinite(d.getTime()) ? d.toISOString() : "";
}

/* ---------- 类型与文案 ---------- */
export function exportKindLabel(kind) {
    return EXPORT_KIND_LABELS[kind] || "导出";
}

/* ---------- 候选快照（diff 基准） ---------- */
export function exportCandidateSnapshot(project) {
    /* 取追踪候选的 uid 集合 + 状态表。缺数组/非对象一律空快照（不抛）。 */
    const candidates = (project && Array.isArray(project.candidates)) ? project.candidates : [];
    const uids = [];
    const statuses = {};
    candidates.forEach((c) => {
        const uid = String((c && c.uid) || "").trim();
        if (!uid || uids.indexOf(uid) !== -1) return;
        uids.push(uid);
        statuses[uid] = String((c && c.status) || "");
    });
    return { uids: uids, statuses: statuses };
}

/* ---------- 相对上次导出的变化（零 LLM diff） ---------- */
export function exportChanges(prev, cur) {
    /* prev/cur 均为 exportCandidateSnapshot 形状。返回 {added, statusChanged, prevUids, prevStatuses}：
       added = 本次新增（上次没有的 uid）；statusChanged = 两次都在但状态不同的 uid 数；
       prevUids/prevStatuses = 本次快照，作为下一次导出的 diff 基准。 */
    const p = (prev && Array.isArray(prev.uids)) ? prev : { uids: [], statuses: {} };
    const c = (cur && Array.isArray(cur.uids)) ? cur : { uids: [], statuses: {} };
    const prevSet = {};
    p.uids.forEach((u) => { prevSet[u] = true; });
    const curSet = {};
    c.uids.forEach((u) => { curSet[u] = true; });
    let added = 0;
    let statusChanged = 0;
    c.uids.forEach((u) => {
        if (!prevSet[u]) { added += 1; return; }
        if (String(p.statuses[u] || "") !== String(c.statuses[u] || "")) statusChanged += 1;
    });
    return { added: added, statusChanged: statusChanged, prevUids: c.uids.slice(), prevStatuses: Object.assign({}, c.statuses) };
}

export function exportChangesText(changes) {
    /* 变化摘要（原文格式：「新增 N 候选 · 状态变化 M」）。
       两个数都为零 → 如实写「无变化」，不假装有变化。 */
    const c = (changes && typeof changes === "object") ? changes : {};
    const added = Number(c.added) || 0;
    const changed = Number(c.statusChanged) || 0;
    if (!added && !changed) return "无变化";
    return "新增 " + added + " 候选 · 状态变化 " + changed;
}

/* ---------- 台账条目（与 artifacts.js `_normExport` 字段逐一对齐） ---------- */
export function exportLedgerRecord(opts) {
    /* opts: {kind, name, datasetVersion, changes, note, at} → 台账条目。
       id 沿用 artifacts 的 `exp-<ms>-<seq>` 形状（seq 由数据层补，这里给前缀）。
       dataset_version 来自后端响应头（实例级事实）；没有就如实空串。 */
    opts = opts || {};
    const kind = EXPORT_KINDS.indexOf(opts.kind) !== -1 ? opts.kind : "export";
    const nowIso = exportIsoNow(opts.at);
    return {
        id: "exp-" + (opts.id || nowIso + "-" + Math.random().toString(36).slice(2, 6)),
        kind: kind,
        name: String(opts.name || "").trim(),
        at: nowIso,
        dataset_version: String(opts.datasetVersion || "").trim(),
        changes: (opts.changes && typeof opts.changes === "object") ? opts.changes : null,
        note: String(opts.note || "").trim(),
    };
}

export function exportRenamedRecord(record, name) {
    /* 给台账条目命名（如「初筛」「投稿前复核」）：返回新对象，不改入参。 */
    const r = (record && typeof record === "object") ? Object.assign({}, record) : { kind: "export" };
    r.name = String(name || "").trim();
    return r;
}

/* ---------- 台账展示推导（最新一条 / 历史折叠） ---------- */
export function exportLastRecord(exportsList) {
    /* 默认只展示最新一条：按 at 降序取第一条；空/非数组 → null。 */
    const list = (exportsList && Array.isArray(exportsList)) ? exportsList.slice() : [];
    if (!list.length) return null;
    list.sort((a, b) => String(b.at || "").localeCompare(String(a.at || "")));
    return list[0];
}

export function exportHistoryRows(exportsList) {
    /* 历史折叠「导出记录」：除最新一条外的全部，按 at 升序（旧 → 新）展示。 */
    const list = (exportsList && Array.isArray(exportsList)) ? exportsList.slice() : [];
    if (!list.length) return [];
    list.sort((a, b) => String(a.at || "").localeCompare(String(b.at || "")));
    return list.slice(0, -1);
}

export function exportRecordSummary(record, nowMs) {
    /* 台账一行摘要：「<类型文案> · <时间> · <变化>」（时间经 fmtTime 由壳层渲染，这里只给类型+变化）。 */
    const r = (record && typeof record === "object") ? record : {};
    const changes = (r.changes && typeof r.changes === "object") ? r.changes : null;
    const bits = [exportKindLabel(r.kind)];
    if (changes) bits.push(exportChangesText(changes));
    return bits.join(" · ");
}
