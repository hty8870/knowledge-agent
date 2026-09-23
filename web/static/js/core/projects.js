"use strict";

/* 追踪 UI 壳（一级导航「我的库」+ 我的库浮窗追踪页签 + 追踪详情 +
 * 「存为追踪」入口 + 上下文 chip + 首次保存 coachmark）。
 *
 * 数据层全部走 `artifacts.js`（IndexedDB）；纯逻辑在 `projects_core.js`；本文件只管 DOM。
 *
 * ## 交互契约
 * - 追踪面板是「我的库」浮窗（追踪 + 收藏双页签）的 tracks 页签：浮窗骨架（开合/拖动/缩放/
 *   落位/tab 切换）唯一属主在 shell.js（initLibWin），本模块经 setLibRenderer("tracks", …) 注册
 *   渲染器（注册反转，防环），不再自管窗体；面板挂点保留 id="artifactsWinBody"
 *   （project_exports / project_updates 按它取节点，零改动继承）。
 * - 「存为追踪」按钮与「生成任务包/可行性概览」同区（results-head-acts），由 results.js
 *   渲染钩子（setAfterRenderHook，注册式反转防环）驱动显隐。
 * - 上下文 chip 经 board.js 注册反转（setArtifactCtxProvider / setArtifactCtxAfterSend）附加
 * `artifact_context` 请求体字段（独立字段、不拼进用户原话）。后端合并前
 *   extra=forbid 会 422 → 发送失败如实降级（chip 上标注「未随上一条消息发出」，不崩不装成功）。
 * - accounts.js 在 profile 切换重置点调 artifactsOnProfileSwitched()；本模块经
 *   setAccountChangedHook（注册反转，防 accounts→projects 成环）再清上下文 chip/活动追踪 UI 态。
 *
 * ## 埋点（计数型无文本，usage_log 既有通道；追踪名/query/uid 不进遥测——）
 * - project_created / project_resumed（打开追踪详情）/ context_card_used{once}
 */

import { API, $, HEART, MOTION, cacheGeneration, currentAccountScope, escapeHtml, fmtTime, toast } from "#core";
import { buildCard } from "#cards";
import { catalogLookup, ensureDatasetsLoaded } from "#browse";
import { USAGE_KINDS, usageActiveTurnId, usagePolicyRef } from "#usage_core";
import { usageLog } from "#usage_log";
import { artifactsCreateProject, artifactsDeleteProject, artifactsGetProject, artifactsListProjects,
    artifactsOnProfileSwitched, artifactsRemoveCandidate, artifactsSetBaseline,
    artifactsSetCandidateStatus, artifactsTouchCheckedAt, artifactsUpdateProject } from "#artifacts";
import { LAST_RECOMMEND_DATA, searchParamSnapshot } from "#search";
import { _facetFilters, _lenientDims, _suppressed, setAfterRenderHook } from "#results";
import { getDateRange, queryForRetrieval } from "#interactions";
import { getConfig, showView, closeLibWin, openLibWin, setLibRenderer } from "#shell";
import { setArtifactCtxAfterSend, setArtifactCtxProvider, swShowBoard } from "#board";
import { setAccountChangedHook } from "#accounts";
import { setCtxDataSetHandler } from "#fav_folders";   // 注册数据集上下文 chip 设置接口（注册反转防 fav_folders→projects 环）
import { projectsContextSerialize, projectsDraftFromSearch, projectsLastCheckedText, projectsProjectId,
    projectsSpecFromRequest, projectsStatusCounts } from "#projects_core";
import { p4DetailMount, runProjectCheck, pendingDeltaCount, checkFailed,
    setWatchesRefreshedHook } from "#project_updates";   // 更新检查交互（探测式降级，见挂载点）
import { COPY, armTwoStepConfirm } from "./copy.js";

/* ---------- 模块内状态（本文件唯一有状态区；其余函数每次从 DB 现读现渲） ---------- */
let _detailId = null;          // 详情视图打开的追踪 id（null = 列表视图）
let _saveBusy = false;         // 「存为追踪」在途闸
let _renderSeq = 0;            // 异步渲染代际闸（防晚到的 list/get 盖新屏）
let _ctxCard = null;           // 活动上下文卡：{kind:"track"|"dataset", id, name, text, omitted, note}
let _ctxCounted = new Set();   // context_card_used{once} 会话内去重（同追踪只计一次）
let _detailUI = { goalEditing: false, reasonUid: null, reasonAction: null };  // 详情内联编辑态

/* 行级操作三按钮的内联 SVG（视觉 spec §3.2：lucide 同族描边语言；行级重复操作用图标钮）。 */
const _REFRESH_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>';
const _CHAT_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
const _TRASH_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>';

/* ---------- 工具 ---------- */
function _scope() { return currentAccountScope(); }
function _esc(v) { return escapeHtml(v); }
function _fmtDate(iso) { return iso ? fmtTime(iso) : ""; }

/* 候选卡片化（视觉 spec §6）：每条候选 → .prj-cand-wrap（buildCard(variant:"library")
   + 操作条 + 理由折叠；状态徽标收进卡片 .badges 行首，不再浮贴出界）。
   目录三态（catalogLookup）：found → 卡片；not_found → 文字行 + 「已下架」；
   load_error → 文字行 + 「目录未加载」（不冒充已下架）并触发全量加载后重渲当前详情。
   操作按钮保留既有 data-cand-ok/no/back/del 语义，绑定逻辑不变。 */
function _buildCandNodes(p, ui) {
    const nodes = [];
    let needCatalog = false;
    (p.candidates || []).forEach((c) => {
        const st = String(c.status || "待核验");
        const metaBits = [];
        if (c.reason) metaBits.push("理由：" + _esc(c.reason));
        if (c.verified_at && (st === "已核验" || st === "已排除")) metaBits.push("核验于 " + _esc(_fmtDate(c.verified_at)));
        let lookup = null;
        try { lookup = (typeof catalogLookup === "function") ? catalogLookup(c.uid) : null; } catch (_e) { lookup = null; }
        const found = lookup && lookup.status === "found";
        const notFound = lookup && lookup.status === "not_found";
        const loadErr = lookup && lookup.status === "load_error";
        if (loadErr) needCatalog = true;

        const wrap = document.createElement("div");
        wrap.className = "prj-cand-wrap";
        wrap.dataset.uid = c.uid;
        const badge = document.createElement("span");
        badge.className = "prj-cand-st st-" + _esc(st);
        badge.textContent = st;

        /* 状态徽标不再绝对定位浮贴在卡片外（top:-8px 探出卡片上沿被截），
           收进卡片内部——卡片态插进 .badges 行首（与来源/FASTQ pill 同一行、同一语言）；
           文字行兜底态插进 .prj-cand-main 行首。 */
        if (found) {
            try {
                const cardEl = buildCard(lookup.item, { variant: "library" });
                const badgesRow = cardEl.querySelector(".badges");
                if (badgesRow) badgesRow.prepend(badge); else wrap.appendChild(badge);
                wrap.appendChild(cardEl);
            } catch (_e) {
                const row = _candTextRow(c, metaBits, notFound, loadErr);
                const main = row.querySelector(".prj-cand-main");
                if (main) main.prepend(badge); else wrap.appendChild(badge);
                wrap.appendChild(row);
            }
        } else {
            const row = _candTextRow(c, metaBits, notFound, loadErr);
            const main = row.querySelector(".prj-cand-main");
            if (main) main.prepend(badge); else wrap.appendChild(badge);
            wrap.appendChild(row);
        }

        const acts = document.createElement("div");
        acts.className = "prj-cand-acts";
        let actsHtml = "";
        if (st === "待核验") {
            actsHtml = '<button class="btn prj-act-ok" type="button" data-cand-ok="' + _esc(c.uid) + '" title="核验通过，纳入追踪">标记已核验</button>'
                + '<button class="btn prj-act-no" type="button" data-cand-no="' + _esc(c.uid) + '" title="核验不通过，排除出追踪">标记已排除</button>';
        } else if (st === "已核验" || st === "已排除") {
            actsHtml = '<button class="prj-mini-btn" type="button" data-cand-back="' + _esc(c.uid) + '" title="改回待核验（重新审视）">改回待核验</button>';
        }
        acts.innerHTML = actsHtml + '<button class="prj-mini-btn prj-cand-del" type="button" data-cand-del="' + _esc(c.uid) + '" title="从追踪移除这条候选">移除</button>';

        const editing = ui.reasonUid === c.uid;
        if (editing) {
            const reasonForm = document.createElement("div");
            reasonForm.className = "prj-reason-form";
            reasonForm.innerHTML = '<textarea class="prj-input" id="prjReasonInput" maxlength="500" placeholder="必填：为什么这样核验？"></textarea>'
                + '<div class="prj-edit-acts"><button class="btn btn-primary" type="button" data-reason-confirm="' + _esc(c.uid) + '">' + (ui.reasonAction === "exclude" ? "确认排除" : "确认核验") + "</button>"
                + '<button class="btn" type="button" data-reason-cancel>取消</button></div>';
            wrap.appendChild(reasonForm);
        } else {
            wrap.appendChild(acts);
        }
        nodes.push(wrap);
    });
    if (needCatalog && typeof ensureDatasetsLoaded === "function") {
        ensureDatasetsLoaded().then(() => { if (_detailId) projectsRenderWindow(); }).catch(() => {});
    }
    return nodes;
}
function _candTextRow(c, metaBits, notFound, loadErr) {
    const row = document.createElement("div");
    row.className = "prj-cand";
    row.innerHTML = '<div class="prj-cand-main"><span class="prj-cand-uid">' + _esc(c.uid) + "</span>"
        + (notFound ? '<span class="gone-badge">已下架</span>' : "")
        + (loadErr ? '<span class="prj-cand-meta">目录未加载，稍后重试</span>' : "")
        + (metaBits.length ? '<span class="prj-cand-meta">' + metaBits.join(" · ") + "</span>" : "")
        + "</div>";
    return row;
}

/* ============================================================================
 * 浮窗开合（骨架属主在 shell.js——这里是薄封装，只负责追踪页签语义与渲染触发）
 * ========================================================================== */

/* 关闭档案浮窗。导出名保留（模块内多处调用）；骨架与计时器都在 shell.js。 */
/* 关闭我的库浮窗。导出名保留（模块内多处调用）；骨架与计时器都在 shell.js。 */
export function projectsCloseWindow() { closeLibWin(); }

/* 打开并直达某追踪详情。 */
export async function projectsOpenWindowTo(projectId) {
    _detailId = String(projectId || "") || null;
    usageLog(USAGE_KINDS.project_resumed, {});   // 打开追踪详情（列表点击在 projectsRenderList 处计）
    openLibWin("tracks");
    await projectsRenderWindow();
}

/* 浮窗渲染分发（列表/详情）。渲染结果以 DB 现读为准，每次全量重渲（数据量小，无虚拟列表必要）。
   也作为档案浮窗 projects tab 的注册渲染器被 shell.js 调用（切 tab/账户切换重渲）。 */
async function projectsRenderWindow() {
    const body = $("artifactsWinBody");
    if (!body) return;
    const mySeq = ++_renderSeq;
    try {
        if (_detailId) {
            const p = await artifactsGetProject(_scope(), _detailId);
            if (mySeq !== _renderSeq) return;
            if (!p) { _detailId = null; projectsRenderList(); return; }
            renderProjectDetail(p);
        } else {
            await projectsRenderList();
        }
    } catch (_e) {
        if (mySeq !== _renderSeq) return;
        body.innerHTML = '<div class="hw-empty"><div class="hw-ei">⚠️</div><p>追踪数据读取失败：' + _esc((_e && _e.message) || "未知错误") + "</p></div>";
    }
}

/* ---------- 列表视图 ---------- */
async function projectsRenderList() {
    const body = $("artifactsWinBody");
    if (!body) return;
    const mySeq = ++_renderSeq;
    let projects = [];
    try { projects = await artifactsListProjects(_scope()); } catch (_e) {
        if (mySeq !== _renderSeq) return;
        body.innerHTML = '<div class="hw-empty"><div class="hw-ei">⚠️</div><p>追踪列表读取失败：' + _esc((_e && _e.message) || "未知错误") + "</p></div>";
        return;
    }
    if (mySeq !== _renderSeq) return;
    if (!projects.length) {
        body.innerHTML = '<div class="hw-empty"><div class="hw-ei">🗂</div><p>还没有追踪。检索出结果后，点结果上方的「存为追踪」即可保存。</p></div>';
        return;
    }
    body.innerHTML = projects.map((p) => {
        const counts = projectsStatusCounts(p.candidates);
        const checked = projectsLastCheckedText(p);
        const pending = (typeof pendingDeltaCount === "function") ? pendingDeltaCount(p.project_id) : 0;
        const fail = (typeof checkFailed === "function") ? checkFailed(p.project_id) : false;
        const meta = ["候选 " + p.candidates.length,
            counts["待核验"] ? "待核验 " + counts["待核验"] : "",
            counts["已核验"] ? "已核验 " + counts["已核验"] : "",
            counts["已排除"] ? "已排除 " + counts["已排除"] : "",
        ].filter(Boolean).join(" · ");
        const timeBits = [checked, "更新于 " + _fmtDate(p.updated_at)].filter(Boolean).join(" · ");
        // 视觉 spec §3.1：行拆 .prj-card-main（button 语义承载点击进详情）+ .prj-row-acts（sibling 三按钮），
        // 杜绝交互嵌套；三个按钮 click + keydown 都 stopPropagation（不误触进详情）。
        return '<div class="prj-card" data-prj="' + _esc(p.project_id) + '" tabindex="0" role="button" aria-label="打开追踪：' + _esc(p.name) + '">'
            + '<div class="prj-card-main">'
            + '<div class="prj-card-name">' + _esc(p.name) + (pending > 0 ? '<span class="prj-badge-new">待查看 ' + pending + '</span>' : "") + "</div>"
            + '<div class="prj-card-meta">' + _esc(meta) + "</div>"
            + (timeBits ? '<div class="prj-card-time">' + _esc(timeBits) + (fail ? '<span class="prj-check-fail">检查失败，可重试</span>' : "") + "</div>" : "")
            + "</div>"
            + '<div class="prj-row-acts">'
            + '<button class="ra-btn" type="button" data-prj-check="' + _esc(p.project_id) + '" title="检查更新" aria-label="检查更新">' + _REFRESH_SVG + "</button>"
            + '<button class="ra-btn" type="button" data-prj-use="' + _esc(p.project_id) + '" title="在对话中使用" aria-label="在对话中使用">' + _CHAT_SVG + "</button>"
            + '<button class="ra-btn ra-del" type="button" data-prj-del="' + _esc(p.project_id) + '" title="删除追踪" aria-label="删除追踪">' + _TRASH_SVG + "</button>"
            + "</div>"
            + "</div>";
    }).join("");
    body.querySelectorAll("[data-prj]").forEach((el) => {
        const id = el.getAttribute("data-prj");
        const open = () => {
            _detailId = id;
            usageLog(USAGE_KINDS.project_resumed, {});
            projectsRenderWindow();
        };
        el.addEventListener("click", open);
        el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
        const checkBtn = el.querySelector("[data-prj-check]");
        /* 设计 §3.1：行按钮检查完要重渲列表行——检查时间 / 「待查看 N」徽章 / 行内失败标记
           都靠这次重渲上屏（此前不调 outcome、不重渲，时间戳要重开窗才刷新）。
           outcome.checking=重入短路，不重渲。 */
        if (checkBtn) checkBtn.addEventListener("click", async (e) => {
            e.stopPropagation();
            if (typeof runProjectCheck === "function") {
                const outcome = await runProjectCheck(id, checkBtn);
                if (outcome && !outcome.checking) projectsRenderWindow();
            }
        });
        const useBtn = el.querySelector("[data-prj-use]");
        if (useBtn) useBtn.addEventListener("click", (e) => { e.stopPropagation(); const p = projects.find((x) => x.project_id === id); if (p) projectsUseInChat(p); });
        const delBtn = el.querySelector("[data-prj-del]");
        if (delBtn) {
            delBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                if (!armTwoStepConfirm(delBtn, { idleHtml: _TRASH_SVG, confirmText: "再点一次确认删除" })) return;
                artifactsDeleteProject(_scope(), id).then(() => {
                    if (_ctxCard && _ctxCard.id === id) { _ctxCard = null; _renderCtxCard(); }
                    if (_detailId === id) _detailId = null;
                    const p = projects.find((x) => x.project_id === id);
                    toast("已删除追踪「" + (p ? p.name : id) + "」");
                    projectsRenderWindow();
                }).catch((err) => toast("删除失败：" + ((err && err.message) || "未知错误")));
            });
            el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { delBtn.getAttribute("data-prj-del") && (e.stopPropagation(), e.preventDefault()); } });
        }
    });
}

/* ============================================================================
 * 详情视图（研究目标/纳入排除可编辑、候选状态流转、检查条件展示、导出记录区挂点）
 * ========================================================================== */
function renderProjectDetail(p) {
    const body = $("artifactsWinBody");
    if (!body) return;
    const counts = projectsStatusCounts(p.candidates);
    const checked = projectsLastCheckedText(p);
    const hasCheck = !!(p.check_condition && p.check_condition.spec);
    const cc = p.check_condition || {};
    const spec = cc.spec || {};
    const ui = _detailUI;

    /* 检查条件展示区：展示实际保存的规范化检索规格 + display_query。
       本节内容为空时不渲染（不伪造条件）；更新检查交互挂点（data-p4-mount-check）在卡末。 */
    let checkHtml = "";
    if (hasCheck) {
        const rows = [];
        if (spec.query) rows.push("<span><b>关键词</b>" + _esc(spec.query) + "</span>");
        if (spec.sources && spec.sources.length) rows.push("<span><b>来源</b>" + _esc(spec.sources.join("、")) + "</span>");
        if (spec.facet_filters && spec.facet_filters.length) {
            rows.push("<span><b>分面</b>" + _esc(spec.facet_filters.map((f) => f.dim + "=" + f.value).join("；")) + "</span>");
        }
        if (spec.suppressed_constraints && spec.suppressed_constraints.length) {
            rows.push("<span><b>已忽略</b>" + _esc(spec.suppressed_constraints.join("、")) + "</span>");
        }
        if (spec.lenient_dims && spec.lenient_dims.length) {
            rows.push("<span><b>宽放</b>" + _esc(spec.lenient_dims.join("、")) + "</span>");
        }
        if (spec.date_from || spec.date_to) rows.push("<span><b>年份</b>" + _esc([spec.date_from, spec.date_to].filter(Boolean).join(" 至 ")) + "</span>");
        checkHtml = '<section class="prj-sec"><div class="prj-sec-title">检查条件'
            + (checked ? '<span class="prj-sec-note">' + _esc(checked) + "</span>" : "")
            + "</div>"
            + (cc.display_query ? '<div class="prj-check-query">原始检索句：' + _esc(cc.display_query) + "</div>" : "")
            + '<div class="prj-check-spec">' + rows.join("") + "</div>"
            + '<div class="prj-check-mount" data-p4-mount-check hidden></div><!-- ENG-P4-MOUNT：P4 在此挂「检查更新/重试基线」交互；无 P4 时不渲染 -->'
            + "</section>";
    }

    /* 导出区：挂点 data-p5-mount-export。区域默认整段隐藏——导出模块（project_exports.js）
       经 MutationObserver 发现 `[data-p5-mount-export]` 后渲染并展开本节；导出模块未加载时不渲染
       （保持「空时不渲染」语义，不出现空标题）。挂点带追踪 id 供导出模块读库。 */
    const exportHtml = '<section class="prj-sec" data-p5-mount-section hidden>'
        + '<div class="prj-sec-title">导出</div>'
        + '<div class="prj-export-mount" data-p5-mount-export data-prj-id="' + _esc(p.project_id) + '"></div>'
        + '<!-- ENG-P5-MOUNT：P5 在此挂导出按钮与导出记录台账；无 P5 时不渲染 --></section>';

    /* 研究目标（可编辑） */
    const goalHtml = ui.goalEditing
        ? '<div class="prj-edit"><textarea class="prj-input" id="prjGoalInput" maxlength="1000" placeholder="这个追踪想做什么">' + _esc(p.goal) + '</textarea>'
            + '<div class="prj-edit-acts"><button class="btn btn-primary prj-save" type="button">保存</button><button class="btn prj-cancel" type="button">取消</button></div></div>'
        : '<div class="prj-goal">' + (p.goal ? _esc(p.goal) : '<span class="prj-muted-text">还没有写研究目标。</span>')
            + '<button class="prj-mini-btn" type="button" data-goal-edit>✎ 编辑</button></div>';

    /* 纳入/排除（各 ≤8 条，可增删） */
    const condList = (label, arr, key) => {
        const rows = arr.map((c, i) => '<div class="prj-cond-row"><span class="prj-cond-idx">' + (i + 1) + '</span><span class="prj-cond-text">' + _esc(c) + '</span>'
            + '<button class="prj-mini-btn prj-cond-del" type="button" data-cond-del="' + key + '" data-cond-idx="' + i + '" title="删除这条">✕</button></div>').join("");
        const full = arr.length >= 8;
        return '<div class="prj-sec"><div class="prj-sec-title">' + _esc(label) + (full ? '<span class="prj-sec-note">已达上限 8 条</span>' : "") + "</div>"
            + (rows || '<div class="prj-muted-text">暂无。</div>')
            + (full ? "" : '<div class="prj-cond-add"><input class="prj-input" type="text" maxlength="120" placeholder="添加一条' + _esc(label) + '条件" data-cond-add="' + key + '"><button class="btn" type="button" data-cond-add-btn="' + key + '">添加</button></div>')
            + "</div>";
    };

    /* 候选列表（卡片化，视觉 spec §6）：目录能解析到 → buildCard(variant:"library") 包 .prj-cand-wrap
       （状态徽标收进卡片 .badges 行首 + 操作条 + 理由输入折叠）；not_found → 文字行 + 「已下架」；
       load_error → 文字行 + 「目录未加载」（不冒充已下架）并触发重载后再重渲。待核验 → 已核验/已排除（必填理由）；可移除。 */
    const candNodes = _buildCandNodes(p, ui);

    body.innerHTML = '<div class="prj-detail">'
        + '<div class="prj-detail-head">'
        + '<button class="btn" type="button" data-prj-back title="返回追踪列表">← 列表</button>'
        + '<div class="prj-detail-title">' + _esc(p.name)
        + '<button class="prj-mini-btn" type="button" data-prj-rename title="重命名追踪">✎ 重命名</button></div>'
        + '<div class="prj-detail-acts">'
        + '<button class="btn btn-primary" type="button" data-prj-use title="把追踪内容放进输入框里的上下文 chip，随下一条消息发给你配置的 AI">在对话中使用</button>'
        + '<button class="btn prj-del" type="button" data-prj-del title="删除这个追踪">删除追踪</button>'
        + "</div></div>"
        + exportHtml   // 导出区从详情底部挪到页面顶部（头卡之下第一区）——批量导出不再要翻过长候选列表
        + checkHtml   // 检查条件卡靠前（追踪的可检查规格是复查/更新的入口，先于研究目标）
        + '<div class="prj-sec"><div class="prj-sec-title">研究目标<span class="prj-sec-note">候选 ' + (p.candidates || []).length + " · 待核验 " + counts["待核验"] + " · 已核验 " + counts["已核验"] + " · 已排除 " + counts["已排除"] + "</span></div>"
        + goalHtml + "</div>"
        + condList(COPY.conditions.include, p.include_conditions, "include")
        + condList(COPY.conditions.exclude, p.exclude_conditions, "exclude")
        + '<section class="prj-sec"><div class="prj-sec-title">候选</div>'
        + '<div class="prj-cand-list">' + (candNodes.length ? "" : '<div class="prj-muted-text">还没有候选。</div>') + "</div></section>"
        + "</div>";

    /* 候选卡片化（DOM 追加）：目录能解析 → buildCard；不能 → 文字行 + 下架/未加载标记。 */
    const candList = body.querySelector(".prj-cand-list");
    if (candList) candNodes.forEach((node) => { if (node) candList.appendChild(node); });

    /* ---- 详情事件绑定（一次性，挂在 body 上做事件委托更稳，但本项目风格是逐节点绑定） ---- */
    bindProjectDetailEvents(body, p);
    // 理由输入框聚焦（重渲后光标归位；原失焦问题：重渲重建 textarea，用户要点两下才能输入）
    if (ui.reasonUid) {
        const ri = body.querySelector("#prjReasonInput");
        if (ri) { try { ri.focus(); } catch (_e) {} }
    }

    /* 更新检查交互：检查条件区挂点 div 由 project_updates 模块填充——「检查更新/
       重试生成基线」按钮 + 双时间戳 + 待查看更新逐条处理；模块未加载
       （p4DetailMount 不存在）时探测式降级：挂点保持 hidden，不渲染不报错。 */
    if (typeof p4DetailMount === "function") { try { p4DetailMount(p); } catch (_e) {} }
}

function bindProjectDetailEvents(body, p) {
    const back = body.querySelector("[data-prj-back]");
    if (back) back.addEventListener("click", () => { _detailId = null; _detailUI = { goalEditing: false, reasonUid: null, reasonAction: null }; projectsRenderWindow(); });

    /* 重命名（二段：点击变内联输入，回车/失焦保存） */
    const renameBtn = body.querySelector("[data-prj-rename]");
    if (renameBtn) renameBtn.addEventListener("click", () => {
        const titleEl = body.querySelector(".prj-detail-title");
        const old = p.name;
        titleEl.textContent = "";
        const input = document.createElement("input");
        input.className = "prj-input prj-rename-input";
        input.value = old; input.maxLength = 80;
        const done = async (commit) => {
            const v = String(input.value || "").trim();
            if (commit && v && v !== old) {
                try {
                    await artifactsUpdateProject(_scope(), p.project_id, (np) => { np.name = v; return np; });
                    toast("已重命名");
                } catch (e) { toast("重命名失败：" + ((e && e.message) || "未知错误")); }
            }
            await projectsRenderWindow();
        };
        input.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Enter") done(true); else if (e.key === "Escape") done(false); });
        input.addEventListener("blur", () => done(true));
        titleEl.appendChild(input);
        input.focus(); input.select();
    });

    /* 删除追踪（二段确认：armed 模式，3 秒内再点才执行） */
    const delBtn = body.querySelector("[data-prj-del]");
    if (delBtn) delBtn.addEventListener("click", () => {
        if (!armTwoStepConfirm(delBtn, { idleText: "删除追踪" })) return;
        artifactsDeleteProject(_scope(), p.project_id).then(() => {
            if (_ctxCard && _ctxCard.id === p.project_id) { _ctxCard = null; _renderCtxCard(); }
            if (_detailId === p.project_id) _detailId = null;
            toast("已删除追踪「" + p.name + "」");
            projectsRenderWindow();
        }).catch((e) => toast("删除失败：" + ((e && e.message) || "未知错误")));
    });

    /* 研究目标编辑 */
    const goalEdit = body.querySelector("[data-goal-edit]");
    if (goalEdit) goalEdit.addEventListener("click", () => { _detailUI.goalEditing = true; projectsRenderWindow(); });
    const goalSave = body.querySelector("#prjGoalInput");
    const goalSaveBtn = body.querySelector(".prj-save");
    const goalCancelBtn = body.querySelector(".prj-cancel");
    if (goalSaveBtn && goalSave) goalSaveBtn.addEventListener("click", async () => {
        const v = String(goalSave.value || "").trim();
        _detailUI.goalEditing = false;
        try { await artifactsUpdateProject(_scope(), p.project_id, (np) => { np.goal = v; return np; }); toast("已保存研究目标"); }
        catch (e) { toast("保存失败：" + ((e && e.message) || "未知错误")); }
        projectsRenderWindow();
    });
    if (goalCancelBtn) goalCancelBtn.addEventListener("click", () => { _detailUI.goalEditing = false; projectsRenderWindow(); });

    /* 纳入/排除：添加 + 删除 */
    body.querySelectorAll("[data-cond-add-btn]").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const key = btn.getAttribute("data-cond-add-btn");
            const input = body.querySelector('[data-cond-add="' + key + '"]');
            const v = input ? String(input.value || "").trim() : "";
            if (!v) { toast("条件不能为空"); return; }
            try {
                await artifactsUpdateProject(_scope(), p.project_id, (np) => {
                    if (key === "include") np.include_conditions = (np.include_conditions || []).concat([v]).slice(0, 8);
                    else np.exclude_conditions = (np.exclude_conditions || []).concat([v]).slice(0, 8);
                    return np;
                });
                toast("已添加" + (key === "include" ? "纳入" : "排除") + "条件");
            } catch (e) { toast("添加失败：" + ((e && e.message) || "未知错误")); }
            projectsRenderWindow();
        });
    });
    body.querySelectorAll("[data-cond-del]").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const key = btn.getAttribute("data-cond-del");
            const idx = Number(btn.getAttribute("data-cond-idx"));
            try {
                await artifactsUpdateProject(_scope(), p.project_id, (np) => {
                    if (key === "include") np.include_conditions = (np.include_conditions || []).filter((_, i) => i !== idx);
                    else np.exclude_conditions = (np.exclude_conditions || []).filter((_, i) => i !== idx);
                    return np;
                });
            } catch (e) { toast("删除失败：" + ((e && e.message) || "未知错误")); }
            projectsRenderWindow();
        });
    });

    /* 候选：状态流转（必填理由）/ 改回待核验 / 移除 */
    const openReason = (uid, action) => { _detailUI.reasonUid = uid; _detailUI.reasonAction = action; projectsRenderWindow(); };
    body.querySelectorAll("[data-cand-ok]").forEach((btn) => btn.addEventListener("click", () => openReason(btn.getAttribute("data-cand-ok"), "verify")));
    body.querySelectorAll("[data-cand-no]").forEach((btn) => btn.addEventListener("click", () => openReason(btn.getAttribute("data-cand-no"), "exclude")));
    const reasonInput = body.querySelector("#prjReasonInput");
    const reasonConfirm = body.querySelector("[data-reason-confirm]");
    if (reasonConfirm && reasonInput) reasonConfirm.addEventListener("click", async () => {
        const r = String(reasonInput.value || "").trim();
        if (!r) { toast("理由必填——核验决定需要有据可查"); return; }
        const st = _detailUI.reasonAction === "exclude" ? "已排除" : "已核验";
        const uid = reasonConfirm.getAttribute("data-reason-confirm");
        _detailUI.reasonUid = null; _detailUI.reasonAction = null;
        try { await artifactsUpdateProject(_scope(), p.project_id, (np) => artifactsSetCandidateStatus(np, uid, st, r)); toast("已标记为「" + st + "」"); }
        catch (e) { toast("状态更新失败：" + ((e && e.message) || "未知错误")); }
        projectsRenderWindow();
    });
    const reasonCancel = body.querySelector("[data-reason-cancel]");
    if (reasonCancel) reasonCancel.addEventListener("click", () => { _detailUI.reasonUid = null; _detailUI.reasonAction = null; projectsRenderWindow(); });
    body.querySelectorAll("[data-cand-back]").forEach((btn) => btn.addEventListener("click", async () => {
        const uid = btn.getAttribute("data-cand-back");
        try { await artifactsUpdateProject(_scope(), p.project_id, (np) => artifactsSetCandidateStatus(np, uid, "待核验")); toast("已改回待核验"); }
        catch (e) { toast("更新失败：" + ((e && e.message) || "未知错误")); }
        projectsRenderWindow();
    }));
    body.querySelectorAll("[data-cand-del]").forEach((btn) => btn.addEventListener("click", async () => {
        const uid = btn.getAttribute("data-cand-del");
        try { await artifactsUpdateProject(_scope(), p.project_id, (np) => artifactsRemoveCandidate(np, uid)); toast("已移除候选 " + uid); }
        catch (e) { toast("移除失败：" + ((e && e.message) || "未知错误")); }
        projectsRenderWindow();
    }));

    /* 「在对话中使用」→ 上下文卡 */
    const useBtn = body.querySelector("[data-prj-use]");
    if (useBtn) useBtn.addEventListener("click", () => projectsUseInChat(p));
}

/* ============================================================================
 * 上下文 chip（注入文本 + kind 追踪/数据集；双挂点 + 发送即清）
 *  - 主输入框 #queryInput（.console-main 内 #artifactCtxMain）：完整胶囊 chip（图标 + 名称截断 + ✕），
 *    绝对定位叠在输入框第一行左侧、与首行文本同一中线；textarea 首行 text-indent 按 chip 实测宽度同步。
 *  - 侧栏继续对话 #chatInput（.cb-bar 内 #artifactCtx）：窄框态缩成 20px 小圆徽章 .ctx-dot，
 *    对齐第一行左侧；hover/focus/点击浮出 popover（来源类型 + 名称 + 预览 + 移除 + 隐私口径）。
 *  - 发送即清：消息发出成功 → 两个挂点随输入框一起清空（上下文已随这条 query 发出）；
 *    发送失败 → chip 保留 + 如实标注未随消息发出。
 * ========================================================================== */
const _CTX_ICONS = {
    track: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 21l-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>',
    dataset: HEART,
};
function _ctxIcon(kind) { return _CTX_ICONS[kind === "dataset" ? "dataset" : "track"]; }
function _ctxKindLabel(kind) { return kind === "dataset" ? "收藏数据集" : "追踪"; }

function projectsUseInChat(p) {
    const ser = projectsContextSerialize(p);
    _ctxCard = {
        kind: "track", id: p.project_id, name: p.name,
        text: ser.text, omitted: ser.omitted, note: "",
    };
    if (!_ctxCounted.has(p.project_id)) { _ctxCounted.add(p.project_id); usageLog(USAGE_KINDS.context_card_used, {}); }
    _renderCtxCard();
    // 切回对话视图：浮窗收起，输入条（含 chip 挂点）随侧栏工作卡/交换态可见
    showView("query");
    if (typeof swShowBoard === "function") swShowBoard();
    projectsCloseWindow();
    toast("已放进输入框里的上下文 chip，将随下一条消息发出" + (ser.omitted ? "（另有 " + ser.omitted + " 项未注入）" : ""));
}

export function projectsCtxRemove() {
    _ctxCard = null;
    _renderCtxCard();
}

/* 数据集上下文（fav_folders 收藏操作条「在对话中使用」→ 设置 ctx chip，kind="dataset"）。
   由 fav_folders 构建好 {kind, id, name, text, note} 传入；本函数只负责接管并入卡。 */
export function projectsCtxSetData(card) {
    if (!card || !card.text) return;
    _ctxCard = card;
    _renderCtxCard();
    // 切回对话视图：浮窗收起，输入条（含 chip 挂点）随侧栏工作卡/交换态可见
    showView("query");
    if (typeof swShowBoard === "function") swShowBoard();
    projectsCloseWindow();
    toast("已放进输入框里的上下文 chip，将随下一条消息发出" + (card.omitted ? "（另有 " + card.omitted + " 项未注入）" : ""));
}

/* 发送收尾钩子（board.js ubSubmit 经 setArtifactCtxAfterSend 注册调用）：
   ok=true → chip 随这条消息发出去，两个挂点一并清空（发送即清，表达「上下文已随 query 发出」）；
   ok=false → chip 保留 + 如实标注未随消息发出。 */
function projectsCtxAfterSend(ok) {
    if (!_ctxCard) return;
    if (ok) { _ctxCard = null; _renderCtxCard(); return; }
    _ctxCard.note = "上一条消息未能携带本芯片内容发出（服务端暂不支持或网络异常）；chip 已保留，可移除后重发。";
    _renderCtxCard();
}

/* 渲染上下文 chip 到两个挂点：
   - #artifactCtx（侧栏 #chatComposer 的 .cb-bar 内，textarea 之前）→ 小圆徽章变体；
   - #artifactCtxMain（主区 .console-main 内，#queryInput 之前）→ 完整胶囊变体。
   挂点不可见（无结果无对话时输入条整体隐藏）→ chip 状态保留，结果落地后由渲染钩子重挂。
   两个挂点同一状态真源 _ctxCard，移除/发送即清同时生效。 */
let _ctxPopOpen = { side: false, main: false };
let _ctxMainRO = null;         // 主框 chip 的 ResizeObserver（挂点从隐藏祖先里露出时补测宽度 → 同步 text-indent）
function _ctxTruncate(name) { const s = String(name || ""); return s.length > 18 ? s.slice(0, 18) + "…" : s; }
function _ctxPrivacy() {
    const cfg = getConfig();
    // 隐私口径：远端模型明示「会发往你配置的 AI 服务商」；本地模型/本地演示「不出本机」。
    const isLocal = cfg.provider === "mock" || String(cfg.preset || "") === "local";
    return isLocal ? "本地模型 · 内容不出本机" : "该内容会发往你配置的 AI 服务商";
}
function _ctxPopHtml(ctx) {
    return '<div class="ctx-pop-kind">' + _ctxKindLabel(ctx.kind) + " · 随下一条消息发出，发出后自动移除</div>"
        + '<div class="ctx-pop-title">' + _esc(ctx.name) + "</div>"
        + '<pre class="actx-preview">' + _esc(ctx.text) + (ctx.omitted ? "\n\n（另有 " + ctx.omitted + " 项未注入）" : "") + "</pre>"
        + '<div class="ctx-pop-foot">'
        + '<span class="actx-hint">发出前可移除，不影响追踪/收藏本身</span>'
        + '<button class="prj-mini-btn ctx-pop-remove" type="button">移除</button>'
        + "</div>"
        + '<div class="actx-privacy">' + _esc(_ctxPrivacy()) + "</div>"
        + (ctx.note ? '<div class="actx-note">' + _esc(ctx.note) + "</div>" : '<div class="actx-note" hidden></div>');
}
function _renderCtxCard() {
    _renderCtxSide();
    _renderCtxMain();
}

/* 侧栏变体：20px 小圆徽章（带类型图标）+ hover/focus/点击浮出 popover。 */
function _renderCtxSide() {
    const mount = $("artifactCtx");
    if (!mount) return;
    const ctx = _ctxCard;
    if (!ctx) { mount.hidden = true; mount.innerHTML = ""; mount.classList.remove("pop-open"); _ctxPopOpen.side = false; return; }
    mount.hidden = false;
    mount.innerHTML = '<button class="ctx-dot ctx-dot-' + (ctx.kind === "dataset" ? "dataset" : "track") + '" type="button"'
        + ' aria-expanded="' + (_ctxPopOpen.side ? "true" : "false") + '" aria-controls="ctxPop"'
        + ' title="上下文：' + _ctxKindLabel(ctx.kind) + "「" + _esc(ctx.name) + '」——悬停或点击查看，将随下一条消息发出">'
        + _ctxIcon(ctx.kind) + "</button>"
        + '<div class="ctx-pop ctx-pop-dot" id="ctxPop" role="dialog" aria-label="上下文预览">' + _ctxPopHtml(ctx) + "</div>";
    const dot = mount.querySelector(".ctx-dot");
    // 点击/键盘：toggle .pop-open（触屏无 hover 的等价通道）；hover 展开走 CSS（.artifact-ctx:hover）。
    if (dot) dot.addEventListener("click", (e) => {
        e.stopPropagation();
        _ctxPopOpen.side = !_ctxPopOpen.side;
        mount.classList.toggle("pop-open", _ctxPopOpen.side);
        dot.setAttribute("aria-expanded", _ctxPopOpen.side ? "true" : "false");
    });
    const rm = mount.querySelector(".ctx-pop-remove");
    if (rm) rm.addEventListener("click", (e) => { e.stopPropagation(); projectsCtxRemove(); toast("已移除上下文"); });
    if (!mount.dataset.ctxBound) {
        mount.dataset.ctxBound = "1";
        document.addEventListener("click", (e) => {
            if (_ctxPopOpen.side && mount && !mount.contains(e.target)) {
                _ctxPopOpen.side = false; mount.classList.remove("pop-open");
                const d = mount.querySelector(".ctx-dot"); if (d) d.setAttribute("aria-expanded", "false");
            }
        });
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape" && _ctxPopOpen.side) {
                _ctxPopOpen.side = false; mount.classList.remove("pop-open");
                const d = mount.querySelector(".ctx-dot"); if (d) d.setAttribute("aria-expanded", "false");
            }
        });
    }
}

/* 主框变体：完整胶囊 chip（图标 + 名称截断 + ✕），点击主体展开 popover（向下）。
   chip 绝对定位叠在 #queryInput 第一行左侧；textarea 首行 text-indent 按 chip 实测宽度同步，
   挂点从隐藏祖先（结果态主框收起）里露出时经 ResizeObserver 补测。
   chip 在场时暂隐主框 placeholder：首行 text-indent 挤压会让长示例文案折行、第二行被
   单行 textarea 裁出半截灰字，chip 移除后原文恢复。 */
let _ctxSavedPlaceholder = null;
function _renderCtxMain() {
    const mount = $("artifactCtxMain");
    if (!mount) return;
    const qi = $("queryInput");
    const ctx = _ctxCard;
    if (_ctxMainRO) { _ctxMainRO.disconnect(); _ctxMainRO = null; }
    if (!ctx) {
        mount.hidden = true; mount.innerHTML = ""; _ctxPopOpen.main = false;
        if (qi) {
            qi.style.textIndent = "";
            if (_ctxSavedPlaceholder !== null) { qi.placeholder = _ctxSavedPlaceholder; _ctxSavedPlaceholder = null; }
        }
        return;
    }
    mount.hidden = false;
    if (qi && _ctxSavedPlaceholder === null) { _ctxSavedPlaceholder = qi.placeholder; qi.placeholder = ""; }
    mount.innerHTML = '<div class="ctx-chip">'
        + '<button class="ctx-chip-main" type="button" aria-expanded="' + (_ctxPopOpen.main ? "true" : "false") + '" aria-controls="ctxPopMain" title="' + _esc(ctx.name) + '">'
        + '<span class="ctx-chip-ico' + (ctx.kind === "dataset" ? " ctx-ico-dataset" : "") + '" aria-hidden="true">' + _ctxIcon(ctx.kind) + "</span>"
        + '<span class="ctx-chip-name">' + _esc(_ctxTruncate(ctx.name)) + "</span></button>"
        + '<button class="ctx-chip-x" type="button" aria-label="移除上下文" title="移除（不影响追踪/收藏本身）">✕</button>'
        + "</div>"
        + '<div class="ctx-pop ctx-pop-down" id="ctxPopMain" role="dialog" aria-label="上下文预览"' + (_ctxPopOpen.main ? "" : " hidden") + ">"
        + _ctxPopHtml(ctx) + "</div>";
    const syncIndent = () => {
        const chip = mount.querySelector(".ctx-chip");
        if (!qi || !chip) return;
        const w = chip.getBoundingClientRect().width;
        qi.style.textIndent = w > 0 ? Math.ceil(w + 10) + "px" : "";
    };
    syncIndent();
    if (typeof ResizeObserver === "function") {
        _ctxMainRO = new ResizeObserver(syncIndent);
        const chip = mount.querySelector(".ctx-chip");
        if (chip) _ctxMainRO.observe(chip);
    }
    const main = mount.querySelector(".ctx-chip-main");
    const pop = mount.querySelector("#ctxPopMain");
    if (main) main.addEventListener("click", () => {
        _ctxPopOpen.main = !_ctxPopOpen.main;
        pop.hidden = !_ctxPopOpen.main;
        main.setAttribute("aria-expanded", _ctxPopOpen.main ? "true" : "false");
    });
    const x = mount.querySelector(".ctx-chip-x");
    if (x) x.addEventListener("click", (e) => { e.stopPropagation(); projectsCtxRemove(); toast("已移除上下文"); });
    const rm = mount.querySelector(".ctx-pop-remove");
    if (rm) rm.addEventListener("click", () => { projectsCtxRemove(); toast("已移除上下文"); });
    if (!mount.dataset.ctxBound) {
        mount.dataset.ctxBound = "1";
        document.addEventListener("click", (e) => { if (_ctxPopOpen.main && mount && !mount.contains(e.target)) closeCtxPop(); });
        document.addEventListener("keydown", (e) => { if (e.key === "Escape" && _ctxPopOpen.main) closeCtxPop(); });
    }
    function closeCtxPop() { _ctxPopOpen.main = false; const p = mount && mount.querySelector("#ctxPopMain"); if (p) p.hidden = true; const m = mount && mount.querySelector(".ctx-chip-main"); if (m) m.setAttribute("aria-expanded", "false"); }
}

/* ============================================================================
 * 「存为追踪」（结果头操作区入口）+ 首次保存 coachmark
 * ========================================================================== */
async function projectsSaveFromSearch() {
    if (_saveBusy) return;
    const data = LAST_RECOMMEND_DATA;
    const results = (data && Array.isArray(data.results)) ? data.results : [];
    if (!results.length) { toast("还没有可保存的检索结果"); return; }
    const q = (($("queryInput") && $("queryInput").value) || "").trim();
    if (!q) { toast("没有可保存的检索语句"); return; }
    const cfg = getConfig();
    const scope = _scope();
    const uids = results.map((r) => String((r && r.dataset_uid) || "")).filter(Boolean);
    if (!uids.length) { toast("结果里没有可识别的数据集编号"); return; }
    const specParts = projectsSpecFromRequest({
        query: queryForRetrieval(q),
        sources: cfg.sources,
        facet_filters: _facetFilters.map((f) => ({ dim: f.dim, value: f.value })),
        suppressed_constraints: _suppressed.slice(),
        lenient_dims: _lenientDims.slice(),
        date_from: getDateRange(q).date_from,
        date_to: getDateRange(q).date_to,
    });
    const retrParams = Object.assign({ query: queryForRetrieval(q), sources: cfg.sources }, searchParamSnapshot(q));
    const policy = usagePolicyRef(data,
        { strategy: retrParams.strategy, rerank: retrParams.rerank, recall: retrParams.recall, gen: cacheGeneration() });
    const draft = projectsDraftFromSearch({
        query: q,
        specParts: specParts,
        uids: uids,
        provenanceParts: {
            query: q,
            retrieval_params: retrParams,
            search_trace: (data && data.search_trace) || [],
            filters: {
                active: _facetFilters.map((f) => f.dim + "=" + f.value),
                suppressed: _suppressed.slice(),
                lenient: _lenientDims.slice(),
            },
            corpus_digest: "",
            policy_id: policy,
            trace_turn_id: usageActiveTurnId() || "",
            result: { uids: uids, truncated: Number(data.result_total) > uids.length },
        },
    }, { project_id: projectsProjectId() });

    _saveBusy = true;
    try {
        await artifactsCreateProject(scope, draft.input);

        /* 基线：确定性重跑（strategy=fixed 等由后端强制）；失败不掀翻追踪——
           追踪仍保存（spec 留着供重试基线），如实提示。 */
        try {
            const res = await fetch(API.watchCheck, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(draft.spec),
            });
            const j = await res.json();
            if (!res.ok || !j || !j.ok || !j.result) throw new Error((j && j.detail) || "检查条件不可用");
            const r = j.result;
            await artifactsUpdateProject(scope, draft.input.project_id, (p) => {
                let np = artifactsSetBaseline(p, {
                    uids: r.uids, fingerprints: r.fingerprints || {},
                    result_total: Number(r.result_total) || 0, truncated: r.truncated === true,
                    generated_at: String(r.checked_at || ""),
                });
                return artifactsTouchCheckedAt(np, r.checked_at);
            });
        } catch (_e) {
            toast("追踪已保存；基线生成失败，可稍后在追踪里重试");
        }

        usageLog(USAGE_KINDS.project_created, { n: uids.length });
        toast("已存为追踪「" + draft.input.name + "」");
        if (!projectsCoachmarkSeen()) projectsShowCoachmark();
        await projectsOpenWindowTo(draft.input.project_id);
    } catch (e) {
        toast("保存追踪失败：" + ((e && e.message) || "未知错误"));
    } finally { _saveBusy = false; }
}

/* ---------- coachmark（首次保存就地引导；localStorage 只记「已看过」） ---------- */
const COACHMARK_KEY = "biodata_projects_coachmark_v1";
function projectsCoachmarkSeen() {
    try { return localStorage.getItem(COACHMARK_KEY) === "1"; } catch (_e) { return true; }
}
function projectsMarkCoachmarkSeen() {
    try { localStorage.setItem(COACHMARK_KEY, "1"); } catch (_e) {}
}
function projectsShowCoachmark() {
    const old = $("projectsCoachmark");
    if (old) old.remove();
    const card = document.createElement("div");
    card.id = "projectsCoachmark";
    card.className = "coachmark";
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-label", "追踪是什么");
    card.innerHTML = '<div class="coachmark-head"><strong>追踪是什么</strong>'
        + '<button class="icon-btn" type="button" aria-label="关闭" title="关闭">✕</button></div>'
        + '<ul class="coachmark-list">'
        + "<li><b>这是什么</b>：把这次检索连同筛选条件、候选清单存成一个「追踪」，之后可以复查、核验、重命名。</li>"
        + "<li><b>在哪找</b>：左侧导航「我的库」的「追踪」页签；结果页的「存为追踪」按钮随时可以把新检索存进来。</li>"
        + "<li><b>怎么检查更新</b>：追踪会按当时的检索条件记录检查规格，之后可复查候选并检查有没有新数据。</li>"
        + "</ul>"
        + '<div class="coachmark-acts"><button class="btn btn-primary" type="button">知道了</button></div>';
    document.body.appendChild(card);
    const dismiss = () => { projectsMarkCoachmarkSeen(); if (card.parentNode) card.parentNode.removeChild(card); };
    card.querySelector(".coachmark-head button").addEventListener("click", dismiss);
    card.querySelector(".coachmark-acts button").addEventListener("click", dismiss);
    if (MOTION) {
        try { gsap.from(card, { autoAlpha: 0, y: 12, duration: 0.35, ease: "power2.out" }); } catch (_e) {}
    }
}

/* ============================================================================
 * profile 切换接线（accounts.js 在 onAccountChanged 调用）：
 * 清内存缓存（artifacts.js 钩子）+ 清上下文卡/活动追踪 UI 态
 * ========================================================================== */
export function projectsOnProfileSwitched() {
    artifactsOnProfileSwitched();
    _ctxCard = null;
    _detailId = null;
    _detailUI = { goalEditing: false, reasonUid: null, reasonAction: null };
    _renderCtxCard();
}

/* ============================================================================
 * 结果渲染钩子（results.js renderResults 经 setAfterRenderHook 注册调用——
 * 注册式反转防环：results 不 import 本模块）
 * ========================================================================== */
export function projectsAfterResultsSync(data) {
    const hasResults = !!(data && Array.isArray(data.results) && data.results.length);
    const btn = $("saveProjectBtn");
    if (btn) btn.hidden = !hasResults;
    _renderCtxCard();            // 结果落地 → 输入条可见 → 活动 chip 挂出来（若在「在对话中使用」后）
}

/* ============================================================================
 * 启动（boot.js 调用；全部 DOM 访问 null 守卫——dataset.html 也登记 importmap 但不加载本模块）
 * ========================================================================== */
export function initProjects() {
    /* 追踪面板注册为「我的库」浮窗的 tracks 页签渲染器（骨架在 shell.js initLibWin；
       原 #projectsNav 一级导航与 #artifactsWin 拖动/缩放自接线一并退役）。 */
    setLibRenderer("tracks", () => { projectsRenderWindow(); });
    const saveBtn = $("saveProjectBtn");
    if (saveBtn) saveBtn.addEventListener("click", projectsSaveFromSearch);
    // 结果渲染钩子（results.js 注册式反转）与发送收尾钩子（board.js 注册式反转）
    if (typeof setAfterRenderHook === "function") setAfterRenderHook(projectsAfterResultsSync);
    if (typeof setArtifactCtxProvider === "function") setArtifactCtxProvider(() => (_ctxCard ? { text: _ctxCard.text, kind: _ctxCard.kind } : null));
    if (typeof setArtifactCtxAfterSend === "function") setArtifactCtxAfterSend(projectsCtxAfterSend);
    if (typeof setCtxDataSetHandler === "function") setCtxDataSetHandler(projectsCtxSetData);   // 收藏「在对话中使用」→ 数据集上下文 chip（注册反转）
    // 账户切换钩子（accounts.js 注册式反转）：切 profile 清上下文 chip/活动追踪 UI 态
    if (typeof setAccountChangedHook === "function") setAccountChangedHook(projectsOnProfileSwitched);
    // 语料代哨兵自动刷新完成钩子（2026-08-26 注册式反转防环）：重渲列表，
    // 让有 delta 的追踪行上「待查看 N」徽标（pendingDeltaCount 既有口径）
    if (typeof setWatchesRefreshedHook === "function") setWatchesRefreshedHook(() => { projectsRenderWindow(); });
}
