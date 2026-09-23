"use strict";

/* 追踪导出中心 UI 壳（设计 §6）。
 *
 * 纯逻辑在 project_exports_core.js（零 DOM/零网络，node 可单测）；本文件只管 DOM 与网络：
 *   - 自接线不进 boot：DOMContentLoaded 起监听 #artifactsWinBody，发现 projects.js 渲染的
 *     `[data-p5-mount-export]` 挂点就渲染导出区——导出功能未落地时
 *     projects.js 把挂点留在 hidden，这里不渲染不报错（探测式降级，设计 §11 纪律）。
 *   - 四个按钮：导出下载清单 / 导出引文 / 导出筛选记录 / 导出全部研究材料（kind 与后端
 *     EXPORT_KINDS 同源）；追踪无候选时按钮禁用并如实提示。
 *   - 点导出 → 组装追踪当前状态快照 → POST /api/artifacts/export-pack → blob 下载 →
 *     读 X-Biodata-Export-Meta（目录版本，实例级事实）→ 台账 diff（新增 N 候选 · 状态变化 M，
 *     零 LLM）→ artifactsAddExport 写 exports[] → 埋点 export_downloaded{kind}（计数型无文本）。
 *   - 台账：默认只展示最新一条；「导出记录」历史折叠；每条可命名（如「初筛」「投稿前复核」）
 *     可重新下载（重新生成，不存文件本体——每次导出自动再记一条台账，设计 §6）。
 *
 * 诚实约束：服务端报错如实上屏（detail 优先）；下载成功但台账写失败如实提示「已下载，台账
 * 写入失败」；埋点只在真拿到 ZIP 之后记（同 task_pack「只在真拿到产物之后记」）。
 */

import { $, currentAccountScope, escapeHtml, fmtTime, toast } from "#core";
import { dlqFilenameFrom, dlqFireBlob } from "#downloads";
import { artifactsAddExport, artifactsGetProject, artifactsUpdateProject } from "#artifacts";
import { USAGE_KINDS } from "#usage_core";
import { usageLog } from "#usage_log";
import { EXPORT_API_PATH, EXPORT_FAIL_FALLBACK_COPY, EXPORT_HISTORY_CLOSE_COPY, EXPORT_HISTORY_LABEL,
    EXPORT_HISTORY_OPEN_COPY, EXPORT_KINDS, EXPORT_LATEST_COPY, EXPORT_META_HEADER,
    EXPORT_MOUNT_SELECTOR, EXPORT_NAME_BTN_HINT_COPY, EXPORT_NAME_CANCEL_COPY, EXPORT_NAME_COPY,
    EXPORT_NAME_CLEARED_COPY, EXPORT_NAME_PLACEHOLDER_COPY, EXPORT_NAME_SAVE_COPY,
    EXPORT_NAME_TITLE_COPY, EXPORT_NO_CANDIDATE_COPY, EXPORT_REDL_COPY, EXPORT_REDL_HINT_COPY,
    EXPORT_RENAME_COPY, exportCandidateSnapshot,
    exportChanges, exportChangesText, exportDoneCopy, exportFailCopy, exportHistoryRows,
    exportKindLabel, exportLastRecord, exportLedgerFailCopy, exportLedgerRecord,
    exportNameSaveFailCopy, exportNamedCopy, exportRenamedRecord, exportRecordSummary } from "../core/project_exports_core.js";

/* ---------- 模块内状态 ---------- */
let _lastMount = null;      // 当前挂点元素（projects.js 每次重渲都重建挂点 → 元素身份变化触发重渲）
let _renderSeq = 0;         // 异步渲染代际闸（防晚到的读库盖新屏）
let _busyKind = "";         // 在途导出 kind（空 = 空闲；防重复提交）
let _historyOpen = false;   // 「导出记录」历史折叠态
let _editId = "";           // 正在命名的台账条目 id（空 = 无）

function _esc(v) { return escapeHtml(v); }
function _scope() { return currentAccountScope(); }

/* ============================================================================
 * 挂点发现（MutationObserver，自接线）
 * ========================================================================== */
export function initProjectExports() {
    const body = $("artifactsWinBody");
    if (!body) return;
    const observer = new MutationObserver(() => { pexRender(); });
    observer.observe(body, { childList: true, subtree: true });
    pexRender();
}

/* ============================================================================
 * 渲染导出区（挂点元素身份变了才渲染——防止自己的 innerHTML 写触发死循环）
 * ========================================================================== */
async function pexRender() {
    const mount = document.querySelector(EXPORT_MOUNT_SELECTOR);
    if (!mount) { _lastMount = null; return; }
    if (mount === _lastMount) return;   // 挂点没被重建（自己的写操作）→ 不重渲
    _lastMount = mount;
    const prjId = mount.getAttribute("data-prj-id");
    if (!prjId) { mount.hidden = true; return; }
    const mySeq = ++_renderSeq;
    let p = null;
    try { p = await artifactsGetProject(_scope(), prjId); } catch (_e) { p = null; }
    if (mySeq !== _renderSeq) return;
    if (!mount.isConnected) return;      // 读库期间已切走
    if (!p) { mount.hidden = true; return; }
    mount.hidden = false;
    const section = mount.closest("[data-p5-mount-section]");
    if (section) section.hidden = false;   // 展开 projects.js 默认隐藏的导出区（挂点缺席时保持隐藏）
    renderExportArea(mount, p);
}

function renderExportArea(mount, p) {
    const candidates = (p.candidates || []).filter((c) => c && String(c.uid || "").trim());
    const hasCandidates = candidates.length > 0;
    const exportsList = (p.exports || []).slice();
    const latest = exportLastRecord(exportsList);
    const history = exportHistoryRows(exportsList);

    /* 四个导出按钮（无候选 → 禁用 + 如实提示）。 */
    const acts = '<div class="pex-acts">'
        + EXPORT_KINDS.map((kind) => '<button class="btn pex-run" type="button" data-pex-kind="' + kind + '"'
            + (hasCandidates ? "" : " disabled") + '>' + _esc(exportKindLabel(kind)) + "</button>").join("")
        + "</div>"
        + (hasCandidates ? "" : '<div class="pex-note">' + _esc(EXPORT_NO_CANDIDATE_COPY) + "</div>");

    /* 台账：默认只展示最新一条；历史折叠「导出记录」。 */
    let ledger = "";
    if (latest) {
        ledger = '<div class="pex-latest"><div class="pex-latest-badge">' + _esc(EXPORT_LATEST_COPY) + "</div>"
            + pexRowHtml(latest, true) + "</div>";
    }
    if (history.length) {
        const open = _historyOpen;
        ledger += '<div class="pex-history"><button class="pex-history-toggle" type="button" data-pex-history-toggle>'
            + _esc(open ? EXPORT_HISTORY_CLOSE_COPY : EXPORT_HISTORY_OPEN_COPY)
            + "（" + history.length + "）</button>"
            + '<div class="pex-history-list" data-pex-history-list' + (open ? "" : " hidden") + ">"
            + history.map((r) => pexRowHtml(r, false)).join("") + "</div></div>";
    }

    mount.innerHTML = '<div class="pex">' + acts
        + (ledger ? '<div class="pex-ledger">' + ledger + "</div>" : "")
        + "</div>";

    bindExportEvents(mount, p);
}

function pexRowHtml(record, isLatest) {
    const name = String(record.name || "").trim();
    const changes = (record.changes && typeof record.changes === "object") ? record.changes : null;
    const time = fmtTime(record.at);
    let main = '<span class="pex-row-kind">' + _esc(exportKindLabel(record.kind)) + "</span>"
        + (time ? '<span class="pex-row-time">' + _esc(time) + "</span>" : "")
        + (changes ? '<span class="pex-row-changes">' + _esc(exportChangesText(changes)) + "</span>" : "");
    if (_editId === record.id) {
        main += '<span class="pex-row-edit"><input class="prj-input pex-name-input" type="text" maxlength="40"'
            + ' placeholder="' + EXPORT_NAME_PLACEHOLDER_COPY + '" value="' + _esc(name) + '" data-pex-name-input="' + _esc(record.id) + '">'
            + '<button class="btn btn-primary" type="button" data-pex-name-save="' + _esc(record.id) + '">' + EXPORT_NAME_SAVE_COPY + '</button>'
            + '<button class="btn" type="button" data-pex-name-cancel>' + EXPORT_NAME_CANCEL_COPY + '</button></span>';
    } else {
        main += (name ? '<span class="pex-row-name" title="' + EXPORT_NAME_TITLE_COPY + '">' + _esc(name) + "</span>" : "");
    }
    return '<div class="pex-row' + (isLatest ? " pex-row-latest" : "") + '" data-pex-record="' + _esc(record.id) + '">'
        + '<div class="pex-row-main">' + main + "</div>"
        + '<div class="pex-row-acts">'
        + '<button class="prj-mini-btn" type="button" data-pex-name="' + _esc(record.id) + '" title="' + EXPORT_NAME_BTN_HINT_COPY + '">'
        + (name ? EXPORT_RENAME_COPY : EXPORT_NAME_COPY) + "</button>"
        + '<button class="prj-mini-btn" type="button" data-pex-redl="' + _esc(record.id) + '" title="' + EXPORT_REDL_HINT_COPY + '">' + EXPORT_REDL_COPY + '</button>'
        + "</div></div>";
}

/* ============================================================================
 * 事件绑定（导出 / 折叠 / 命名 / 重新下载）
 * ========================================================================== */
function bindExportEvents(mount, p) {
    const prjId = p.project_id;

    /* 四个导出按钮 */
    mount.querySelectorAll("[data-pex-kind]").forEach((btn) => {
        btn.addEventListener("click", () => {
            const kind = btn.getAttribute("data-pex-kind");
            if (EXPORT_KINDS.indexOf(kind) === -1 || _busyKind) return;
            pexExport(p, kind);
        });
    });

    /* 历史折叠开关 */
    const toggle = mount.querySelector("[data-pex-history-toggle]");
    if (toggle) toggle.addEventListener("click", () => {
        _historyOpen = !_historyOpen;
        pexRerender(prjId);
    });

    /* 命名：点「命名」→ 内联输入（保存在 pexRowHtml 的编辑态里） */
    mount.querySelectorAll("[data-pex-name]").forEach((btn) => {
        btn.addEventListener("click", () => {
            _editId = btn.getAttribute("data-pex-name");
            pexRerender(prjId);
        });
    });
    mount.querySelectorAll("[data-pex-name-save]").forEach((btn) => {
        btn.addEventListener("click", () => {
            const rid = btn.getAttribute("data-pex-name-save");
            const input = mount.querySelector('[data-pex-name-input="' + rid + '"]');
            const name = input ? String(input.value || "").trim() : "";
            _editId = "";
            artifactsUpdateProject(_scope(), prjId, (np) => {
                np.exports = (np.exports || []).map((r) => (String(r.id) === rid ? exportRenamedRecord(r, name) : r));
                return np;
            }).then(() => {
                toast(name ? exportNamedCopy(name) : EXPORT_NAME_CLEARED_COPY);
                pexRerender(prjId);
            }).catch((e) => {
                toast(exportNameSaveFailCopy((e && e.message) || "未知错误"));
                pexRerender(prjId);
            });
        });
    });
    mount.querySelectorAll("[data-pex-name-cancel]").forEach((btn) => {
        btn.addEventListener("click", () => { _editId = ""; pexRerender(prjId); });
    });
    const nameInput = mount.querySelector("[data-pex-name-input]");
    if (nameInput) {
        nameInput.addEventListener("keydown", (e) => {
            e.stopPropagation();
            if (e.key === "Enter") { const s = mount.querySelector('[data-pex-name-save="' + nameInput.getAttribute("data-pex-name-input") + '"]'); if (s) s.click(); }
            else if (e.key === "Escape") { _editId = ""; pexRerender(prjId); }
        });
        try { nameInput.focus(); } catch (_e) {}
    }

    /* 重新下载：按该条记录的 kind 用追踪当前状态重新生成（每次导出自动再记一条台账） */
    mount.querySelectorAll("[data-pex-redl]").forEach((btn) => {
        btn.addEventListener("click", () => {
            const rid = btn.getAttribute("data-pex-redl");
            const record = (p.exports || []).find((r) => String(r.id) === rid);
            if (!record || _busyKind) return;
            pexExport(p, record.kind);
        });
    });
}

function pexRerender(prjId) {
    /* 台账/命名/折叠态变化后重渲导出区（不动追踪详情其它部分）。 */
    const mount = document.querySelector(EXPORT_MOUNT_SELECTOR);
    if (!mount || mount.getAttribute("data-prj-id") !== prjId) return;
    const mySeq = ++_renderSeq;
    artifactsGetProject(_scope(), prjId).then((p) => {
        if (mySeq !== _renderSeq) return;
        if (p && mount.isConnected) renderExportArea(mount, p);
    }).catch(() => {});
}

/* ============================================================================
 * 导出主流程：快照 → 后端 → blob 下载 → 台账 diff/写入 → 埋点
 * ========================================================================== */
async function pexExport(p, kind) {
    if (_busyKind) return;
    const candidates = (p.candidates || []).filter((c) => c && String(c.uid || "").trim());
    if (!candidates.length) { toast(EXPORT_NO_CANDIDATE_COPY); return; }
    _busyKind = kind;
    const mount = document.querySelector(EXPORT_MOUNT_SELECTOR);
    if (mount) {
        mount.querySelectorAll("[data-pex-kind]").forEach((b) => { b.disabled = true; });
        mount.querySelectorAll("[data-pex-redl]").forEach((b) => { b.disabled = true; });
    }
    try {
        const payload = {
            kind: kind,
            project: {
                project_id: p.project_id,
                name: p.name,
                goal: p.goal,
                include_conditions: p.include_conditions || [],
                exclude_conditions: p.exclude_conditions || [],
                candidates: candidates,
                check_condition: p.check_condition || null,
                provenance: p.provenance || null,
            },
        };
        const res = await fetch(EXPORT_API_PATH, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        if (!res.ok) {
            let detail = EXPORT_FAIL_FALLBACK_COPY;
            try { const j = await res.json(); detail = j.message_zh || j.detail || detail; } catch (_e) {}
            toast(detail);
            return;
        }
        const blob = await res.blob();
        const name = dlqFilenameFrom(res.headers.get("content-disposition")) || "biodata-export.zip";
        dlqFireBlob(name, blob, { kind: "export" });
        const meta = pexParseMeta(res.headers.get(EXPORT_META_HEADER));

        /* 台账 diff（零 LLM）：相对上次导出的变化「新增 N 候选 · 状态变化 M」。
           diff 基准取上一次台账条目里保存的快照（prevUids/prevStatuses），不依赖别处。 */
        const prevRec = exportLastRecord(p.exports || []);
        const prevSnap = (prevRec && prevRec.changes) ? {
            uids: Array.isArray(prevRec.changes.prevUids) ? prevRec.changes.prevUids : [],
            statuses: (prevRec.changes.prevStatuses && typeof prevRec.changes.prevStatuses === "object")
                ? prevRec.changes.prevStatuses : {},
        } : { uids: [], statuses: {} };
        const changes = exportChanges(prevSnap, exportCandidateSnapshot(p));
        const record = exportLedgerRecord({
            kind: kind,
            datasetVersion: (meta && meta.dataset_version) || "",
            changes: changes,
        });
        try {
            await artifactsUpdateProject(_scope(), p.project_id, (np) => artifactsAddExport(np, record));
        } catch (e) {
            toast(exportLedgerFailCopy(exportKindLabel(kind), (e && e.message) || "未知错误"));
            return;
        }
        usageLog(USAGE_KINDS.export_downloaded, { kind: kind });
        toast(exportDoneCopy(exportKindLabel(kind)));
    } catch (err) {
        const msg = String((err && err.message) || err);
        toast(exportFailCopy(msg));
    } finally {
        _busyKind = "";
        const m = document.querySelector(EXPORT_MOUNT_SELECTOR);
        if (m) pexRerender(m.getAttribute("data-prj-id"));
    }
}

/* ---------- 小工具：文件名取自 Content-Disposition（唯一实现：#downloads.dlqFilenameFrom） ---------- */

function pexParseMeta(raw) {
    /* X-Biodata-Export-Meta：JSON（ASCII 转义），后端把目录版本等实例级事实回传。
       解析失败 → null（台账 dataset_version 如实留空，不猜）。 */
    if (!raw) return null;
    try { return JSON.parse(raw); } catch (_e) { return null; }
}


/* 自接线（不进 boot——boot 的 # import 会牵动两页 importmap；两键只在本页 importmap 登记，
   与 feedback.js 同哲学）。 */
if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initProjectExports);
    } else {
        initProjectExports();
    }
}
