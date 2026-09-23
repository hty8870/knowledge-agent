"use strict";

/* 本文件是 ES Module：core/cards/memory/progress/results/shell 经 import 取。
   board 的 ubSubmit/cbLogPush、search 的 runRecommend/setLastRecommendData、browse 的
   renderHistory/renderBrowse/applyBrowseYearRange/scrollBrowseTop/uploadFile/
   runDiagnose 与 bs、reuse_pack 的 buildReusePack/setReuseScope、fav_folders 的
   toggleFavFolderManage 经 import 取（互调成环——绑定都只在函数体内使用，ESM 允许）。
   boot 与 search/browse/shell/results 经 import 取本文件导出（绞杀桥已全退役）。 */
import { API, LS, MOTION, REDUCE_MOTION, $, clampInt, copyTextAny, escapeHtml, nsKey, openPopup, prettySource, readJSON, toast, writeJSON } from "#core";
import { closeFilesModal } from "#cards";
import { closeMemoryModal, renderMemorySuggestions, setRememberSearchAvailable } from "#memory";
import { animateConsoleWidth } from "#progress";
import { exitResultsLayout } from "#results";
import { applyPreset, closeSettings, openSettings, saveConfig, setSidebar, showView, aiGateChange, syncAiGates, syncStrategyNode,
    toggleLibWin, libWinOpen, closeLibWin, toggleHistWin, histWinOpen, closeHistWin, webGuardOn, setHealthArrivedHook } from "#shell";
import { applyBrowseYearRange, bs, renderBrowse, renderHistory, runDiagnose, scrollBrowseTop, uploadFile } from "#browse";
import { cbChatInMain, cbLogPush, ubSubmit } from "#board";
import { runRecommend, setLastRecommendData } from "#search";
import { buildReusePack, setReuseScope } from "#reuse_pack";
import { toggleFavFolderManage } from "#fav_folders";
import { COPY, armTwoStepConfirm, resetTwoStepConfirm } from "./copy.js";

export let LAST_INTERPRETATION = null;   // 最近一次后端真源；输入变化即失效，避免拿旧解析解释新句子
/* 属主写口（约定 #4）：search.js applyRecommendResult 落地时写它（原裸赋值在 getter 桥上会 TypeError）。 */
export function setLastInterpretation(v) { LAST_INTERPRETATION = v; }
let INTERPRET_PREVIEW_TIMER = null;
let INTERPRET_PREVIEW_SEQ = 0;
let INTERPRET_PREVIEW_ABORT = null;
let INTERPRET_PREVIEW_FAILS = 0;   // 连续失败计数（2026-08-15）：连败 3 次起 pill 摘要如实标注「实时识别暂不可用」
/* 识别预览失败留痕：console.warn（原 console.debug 太弱，识别服务挂掉时 pill 静默消失、排障靠猜）
   + 连败过阈值即刷一次 pill 摘要。AbortError（用户继续打字主动取消）不算失败。 */
function _interpretPreviewFail(err) {
    INTERPRET_PREVIEW_FAILS++;
    console.warn("interpret preview unavailable", err || "");
    if (INTERPRET_PREVIEW_FAILS === 3) { updateSrcSummary(); updateTimeSummary(); }
}
function _interpretNote() { return INTERPRET_PREVIEW_FAILS >= 3 ? " · 实时识别暂不可用" : ""; }
let browseSearchTimer = null;   // 浏览页搜索框（fSearch）250ms trailing debounce 挂钟（bind 内接线）
/* 输入框自动伸展（2026-08-20）：随行数自动增高、最大高度上限（超出上限内部滚动）。
   minRows/maxRows 约束行数上下限（默认 1/8 行；可经 opts 或元素 data-agrow-max 覆盖）——
   上限同步写进 inline max-height 作 CSS 兜底；「先清 height 再量」打断 max-height 钳制
   scrollHeight 的循环（scrollHeight 量的是完整内容高，与 max-height 无关）。
   空值/单行（含 placeholder）落在 min 行高，不撑高。flex 布局下高度变化不挤相邻元素：
   .cb-bar 的 align-items:flex-end 与 .console 的 align-items:stretch 已把按钮/圆钮钉在
   底部（或随行高拉伸），这里只让输入区本身变高，不引发布局抖动。 */
export function autoGrow(el, opts) {
    if (!el) return;
    opts = opts || {};
    const minRows = Math.max(1, Number(opts.minRows) || 1);
    const maxRows = Math.max(minRows, Number(opts.maxRows) || Number(el.dataset.agrowMax) || 8);
    const cs = window.getComputedStyle ? window.getComputedStyle(el) : null;
    let lh = cs ? parseFloat(cs.lineHeight) : NaN;
    if (!Number.isFinite(lh) || lh <= 0) lh = (cs ? parseFloat(cs.fontSize) : 14) * 1.5;
    const padV = (cs ? (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0) : 0)
        + (cs ? (parseFloat(cs.borderTopWidth) || 0) + (parseFloat(cs.borderBottomWidth) || 0) : 0);
    const minH = Math.ceil(lh * minRows + padV);
    const maxH = Math.ceil(lh * maxRows + padV);
    el.style.maxHeight = maxH + "px";      // CSS 兜底：任何形态下高度不超上限
    el.style.height = "auto";               // 先清 inline 高再量，否则 scrollHeight 被旧值钳制
    const h = Math.min(maxH, Math.max(minH, el.scrollHeight));
    el.style.height = h + "px";
    el.style.overflowY = el.scrollHeight > maxH ? "auto" : "hidden";   // 超限内部滚动；不超不显条
}
/* 识别落地提醒（2026-08-30）：停笔识别成功且真的收窄了范围时，首页 pill 的范围圆钮播一圈
   柔光脉冲 + pill 上方浮出一粒识别摘要（动画全在 CSS：detectPulse 1.15s / detectNote 2.2s）。
   只在 hero .console 可见时播（结果/对话态 hero 大框隐藏，提醒职责由侧栏 scopeChip 的态色承担）。
   签名去重：同一句子反复停笔不重复播；清空或识别结果变化后允许再播。 */
let _heroDetectSig = "";
let _heroDetectPulseTimer = null;
function heroDetectFx(interp) {
    const btn = $("heroScopeBtn");
    const consoleEl = btn ? btn.closest(".console") : null;
    if (!btn || !consoleEl || !consoleEl.getClientRects().length) return;
    const parts = [];
    if (interp && getSourceMode() === "auto" && !interp.automatic_skipped_reason) {
        const det = Array.isArray(interp.detected_sources) ? interp.detected_sources : [];
        if (det.length) parts.push(det.length > 2
            ? (prettySource(det[0]) + "、" + prettySource(det[1]) + " 等 " + det.length + " 个来源")
            : det.map(prettySource).join("、"));
    }
    const intent = (interp && interp.intent) || {};
    if (getTimeMode() === "auto") {
        const disp = fmtYearRange(String(intent.date_from || "").slice(0, 4), String(intent.date_to || "").slice(0, 4));
        if (disp) parts.push(disp);
    }
    if (!parts.length) { _heroDetectSig = ""; return; }   // 没有收窄（来源并检全部 + 时间不限）不打扰
    const sig = parts.join("|");
    if (sig === _heroDetectSig) return;
    _heroDetectSig = sig;
    // 圆钮脉冲：重播先摘再强制 reflow；定时摘除收口（不依赖 animationend，reduced-motion 下动画缺席也能复位）。
    if (_heroDetectPulseTimer) clearTimeout(_heroDetectPulseTimer);
    btn.classList.remove("detect-pulse");
    void btn.offsetWidth;
    btn.classList.add("detect-pulse");
    _heroDetectPulseTimer = setTimeout(() => btn.classList.remove("detect-pulse"), 1250);
    // 摘要浮签：同时只留一粒；动画结束移除，setTimeout 兜底防 animationend 缺席。
    consoleEl.querySelectorAll(".detect-note").forEach((n) => n.remove());
    const note = document.createElement("div");
    note.className = "detect-note";
    note.textContent = "已自动识别：" + parts.join(" · ");
    note.addEventListener("animationend", () => note.remove());
    consoleEl.appendChild(note);
    setTimeout(() => { if (note.parentElement) note.remove(); }, 2500);
}
function scheduleInterpretationPreview() {
    const q = (($("queryInput") && $("queryInput").value) || "").trim();
    const seq = ++INTERPRET_PREVIEW_SEQ;
    if (INTERPRET_PREVIEW_TIMER) clearTimeout(INTERPRET_PREVIEW_TIMER);
    if (INTERPRET_PREVIEW_ABORT) INTERPRET_PREVIEW_ABORT.abort();
    INTERPRET_PREVIEW_ABORT = null;
    if (!q) { LAST_INTERPRETATION = null; _heroDetectSig = ""; updateSrcSummary(); updateTimeSummary(); return; }
    // 停顿后识别（2026-08-03，500ms trailing debounce）：180ms 时代在正常打字的
    // 自然停顿里也会触发——pill 摘要随每个思考间隙反复翻转；500ms 跟手感几乎不变，
    // 但识别只在真正停笔时发生（ pill 文本与后端调用次数都安静下来）。
    INTERPRET_PREVIEW_TIMER = setTimeout(async () => {
        const controller = new AbortController();
        INTERPRET_PREVIEW_ABORT = controller;
        try {
            const res = await fetch(API.interpret, {
                method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
                body: JSON.stringify({
                    query: q,
                    sources: getSelectedSources(),
                    auto_parse_sources: getSourceMode() === "auto",
                }),
            });
            const data = await res.json();
            if (!res.ok || !data.ok) { _interpretPreviewFail("status " + res.status); return; }   // 非 ok 也算失败（此前静默 return，连日志都没有）
            INTERPRET_PREVIEW_FAILS = 0;   // 服务恢复即摘标注（下次 pill 刷新时消失）
            const current = (($("queryInput") && $("queryInput").value) || "").trim();
            if (seq !== INTERPRET_PREVIEW_SEQ || current !== q) return;
            LAST_INTERPRETATION = data.interpretation || null;
            updateSrcSummary();
            updateTimeSummary();
            heroDetectFx(LAST_INTERPRETATION);   // 首页 pill：识别收窄了范围 → 圆钮脉冲 + 浮签提醒
        } catch (err) {
            if (!(err && err.name === "AbortError")) _interpretPreviewFail(err);
        } finally {
            if (seq === INTERPRET_PREVIEW_SEQ) INTERPRET_PREVIEW_ABORT = null;
        }
    }, 500);
}

function activeFocusContainer() {
    // 介绍弹窗退役（改独立标签页 /dataset），焦点环只覆盖仍是弹窗的记忆/文件/账户三个
    // （问卷弹窗 surveyModal 2026-08-03 随执行侧全自动化退役）。
    for (const id of ["memoryModal", "filesModal", "accountModal"]) {
        const backdrop = $(id);
        if (backdrop && !backdrop.hidden) return backdrop.querySelector(".modal");
    }
    const settings = $("settings"), onboarding = $("onboarding");
    if (settings && settings.classList.contains("open") && (!onboarding || onboarding.hidden)) return settings;
    return null;
}

function trapDialogFocus(event) {
    if (event.key !== "Tab") return;
    const container = activeFocusContainer();
    if (!container) return;
    const selector = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';
    const focusable = [...container.querySelectorAll(selector)].filter((el) => !el.hidden && el.getClientRects().length);
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (!container.contains(document.activeElement)) {
        event.preventDefault(); first.focus(); return;
    }
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

/* 历史「清空」二段确认态的复位——按钮回原文案、摘 armed、清超时。renderHistory 每次渲染也会调它。 */
export function resetHistClear() {
    const b = $("histClear"); if (!b) return;
    resetTwoStepConfirm(b, COPY.common.clear);
}

/* 「复制接入提示词」单一真源（2026-08-22 帮助页首落地；后提为 export 供教程内
   onboarding.js 委托复用同一实现）：fetch /api/guide/agent-prompt 拿全文 → 剪贴板
   （写入走 core.copyTextAny，能力基元唯一实现）。
   成功把按钮文案短暂变「已复制」，失败如实 toast。 */
export function copyAgentPrompt(btn) {
    if (!btn) return;
    const done = () => {
        btn.textContent = "已复制";
        setTimeout(() => { btn.textContent = "复制接入提示词"; }, 1600);
    };
    fetch("/api/guide/agent-prompt").then((r) => {
        if (!r || !r.ok) throw new Error("agent-prompt http " + (r && r.status));
        return r.text();
    }).then((text) => {
        copyTextAny(text, { failMsg: "复制失败，请改用「下载技能包」" }).then((ok) => { if (ok) done(); });
    }).catch(() => toast("接入提示词拉取失败，请稍后再试"));
}

/* ---------- 帮助页「在线接入」（2026-08-28 在线 MCP）：令牌铸币/列表/吊销 ----------
   仅网页版公网护栏形态（webGuardOn）可见——本机形态没有在线 MCP（铸币端点 404），块保持
   hidden。health 快照到达时同步一次（setHealthArrivedHook），bind 时若快照已到也直接同步。
   剪贴板写入统一走 core.copyTextAny（能力基元唯一实现）。 */
function syncOnlineMcpBlock() {
    const block = $("onlineMcpBlock");
    if (!block) return;
    const on = webGuardOn();
    block.hidden = !on;
    // 引导语与方式二标题随形态收口：本地形态没有在线接入——不说「两种方式任选」、不挂「方式二」前缀，
    // 否则块 hidden 后用户只看到孤零零的「方式二」，不知所云。
    const leadBoth = $("mcpLeadBoth"), leadLocal = $("mcpLeadLocal"), localTitle = $("mcpLocalTitle");
    if (leadBoth) leadBoth.hidden = !on;
    if (leadLocal) leadLocal.hidden = on;
    if (localTitle) localTitle.textContent = on ? "方式二 · 本地接入（功能最全）" : "本地接入（功能最全）";
    if (on) loadMcpTokenList();
}
export async function mintOnlineMcpToken(btn) {
    if (!btn || btn.disabled) return;
    btn.disabled = true;
    try {
        const resp = await fetch(API.mcpTokenMint, {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ label: "" }),
        });
        const data = await resp.json().catch(() => null);
        if (!resp.ok || !data || !data.ok) throw new Error((data && (data.detail || data.error)) || ("http " + resp.status));
        const out = $("mcpTokenOut");
        if (out && data.config) {
            // 标准 MCP 客户端配置形状（Kimi Code / Claude 等通用）：mcpServers.<name>.url + headers
            _lastMcpConfig = data.config;   // 供「复制接入提示词（含令牌）」代入模板
            $("mcpTokenConfig").textContent = JSON.stringify({ mcpServers: { biodata: data.config } }, null, 2);
            out.hidden = false;
        }
        toast("令牌已生成——点「复制接入提示词」，粘给你的助手发送即可");
        loadMcpTokenList();
    } catch (e) {
        toast("生成失败：" + (e && e.message ? e.message : "请稍后重试"));
    } finally {
        btn.disabled = false;
    }
}
/* 铸币成功后最近一次拿到的 {url, headers.Authorization}；「复制接入提示词（含令牌）」从
   /api/guide/online-prompt 拉模板，把 __BIODATA_MCP_URL__ / __BIODATA_MCP_TOKEN__ 占位符
   代成真实值后整段复制——用户粘给助手发送即完成接入（2026-08-30：不再让用户自己配 JSON）。 */
let _lastMcpConfig = null;
let _onlinePromptTemplate = null;   // 模板拉取一次后缓存（纯静态资源）
export async function copyOnlineMcpPrompt(btn) {
    if (!btn || btn.disabled) return;
    if (!_lastMcpConfig || !_lastMcpConfig.url || !_lastMcpConfig.headers) {
        toast("请先点「生成接入令牌」"); return;
    }
    btn.disabled = true;
    try {
        if (!_onlinePromptTemplate) {
            const r = await fetch("/api/guide/online-prompt");
            if (!r || !r.ok) throw new Error("online-prompt http " + (r && r.status));
            _onlinePromptTemplate = await r.text();
        }
        const bearer = String(_lastMcpConfig.headers.Authorization || "").replace(/^Bearer\s+/i, "");
        const text = _onlinePromptTemplate
            .split("__BIODATA_MCP_URL__").join(_lastMcpConfig.url)
            .split("__BIODATA_MCP_TOKEN__").join(bearer);
        const ok = await copyTextAny(text);
        if (ok) { btn.textContent = "已复制"; setTimeout(() => { btn.textContent = "复制接入提示词（含令牌）"; }, 1600); }
        // 失败 toast 由 copyTextAny 统一报（通用句单锚点）
    } catch (_e) {
        toast("接入提示词拉取失败，请稍后再试");
    } finally {
        btn.disabled = false;
    }
}
export async function loadMcpTokenList() {
    const box = $("mcpTokenList");
    if (!box || !webGuardOn()) return;
    try {
        const resp = await fetch(API.mcpTokenList);
        const data = await resp.json().catch(() => null);
        if (!resp.ok || !data || !data.ok) return;
        const rows = (data.tokens || []).map((t) =>
            `<div class="mcp-token-row"><span class="mcp-token-meta">${escapeHtml(t.prefix || "")} · ${escapeHtml(t.label || "未命名")} · ${escapeHtml(t.created_at || "")}</span>` +
            `<button class="btn btn-ghost mcp-token-revoke" data-tid="${escapeHtml(t.token_id || "")}" type="button">吊销</button></div>`);
        box.innerHTML = rows.length ? `<h4 class="mcp-token-list-title">已生成的令牌（吊销立即生效）</h4>${rows.join("")}` : "";
    } catch (_e) { /* 列表失败静默：不挡铸币主流程 */ }
}
export async function revokeOnlineMcpToken(tokenId, btn) {
    if (!tokenId || (btn && btn.disabled)) return;
    if (btn) btn.disabled = true;
    try {
        const resp = await fetch(API.mcpTokenRevoke, {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token_id: tokenId }),
        });
        const data = await resp.json().catch(() => null);
        if (!resp.ok || !data || !data.ok) throw new Error((data && (data.detail || data.error)) || ("http " + resp.status));
        toast("已吊销");
        loadMcpTokenList();
    } catch (e) {
        toast("吊销失败：" + (e && e.message ? e.message : "请稍后重试"));
        if (btn) btn.disabled = false;
    }
}

export function bind() {
    // 移动端选完导航顺手收抽屉（与 shell.js closeSidebarOnMobile 同口径；那边是 showView 的路径，
    // 这边的早退分支——历史浮窗、重击当前页签——不走 showView，得自己收，否则抽屉一直盖着）。
    const closeMobileDrawer = () => { if (window.innerWidth <= 780 && !document.body.classList.contains("side-closed")) setSidebar(false); };
    // 我的库 / 历史记录（拆回两独立浮窗，均是浮窗不是视图）：导航点击切换**浮窗**，不切视图；
    // 其余项照原样切视图。showView 永不收到 "lib"/"history"（与旧 history 同规）。
    document.querySelectorAll(".side-nav .nav-item[data-view]").forEach((n) => n.addEventListener("click", () => {
        if (n.dataset.view === "lib") { toggleLibWin(); closeMobileDrawer(); return; }
        if (n.dataset.view === "history") { toggleHistWin(); closeMobileDrawer(); return; }
        // 已在智能查询页再点一次「智能查询」＝**在新标签页另开一段新对话**（2026-08-13 用户点图：
        // 取代 2026-08-03 的「清空当前窗口」——原对话在原窗口里原样保留，检索/对话不被打断；
        // 旧对话本来就按次落在历史记录里，两个窗口都能回看）。
        if (n.dataset.view === "query" && document.body.classList.contains("on-query")) {
            const hadSession = !!qi.value.trim()
                || document.querySelector('.view[data-view="query"]').classList.contains("has-results");
            if (hadSession) {
                openPopup(location.pathname);   // 用户手势内开新标签页；万一被拦由 openPopup 统一提醒
            } else {
                qi.focus();   // 已是空白查询页：不开重复标签，原地聚焦即可
            }
            closeMobileDrawer();
            return;
        }
        showView(n.dataset.view);
    }));
    document.querySelectorAll("[data-goto]").forEach((b) => b.addEventListener("click", () => showView(b.dataset.goto)));
    $("sideCollapse").addEventListener("click", () => setSidebar(false));
    $("sideFab").addEventListener("click", () => setSidebar(true));
    $("sideBackdrop").addEventListener("click", () => setSidebar(false));
    $("settingsBtn").addEventListener("click", openSettings);
    $("settingsClose").addEventListener("click", closeSettings);
    // 帮助页「接入 AI 助手」（2026-08-22）：接线走 copyAgentPrompt 单一真源
    //（教程「接进你自己的 AI 助手」步的 #tourAgentPromptCopyBtn 经 onboarding.js 委托复用同一实现）。
    const apcBtn = $("agentPromptCopyBtn");
    if (apcBtn) apcBtn.addEventListener("click", () => copyAgentPrompt(apcBtn));
    // 在线接入（2026-08-28）：铸币 / 复制提示词 / 吊销 三键接线 + 形态同步（health 快照到达时再刷一次）
    const mintBtn = $("mcpTokenMintBtn");
    if (mintBtn) mintBtn.addEventListener("click", () => mintOnlineMcpToken(mintBtn));
    const mcpCopyBtn = $("mcpTokenCopyBtn");
    if (mcpCopyBtn) mcpCopyBtn.addEventListener("click", () => copyOnlineMcpPrompt(mcpCopyBtn));
    const mcpCopyJsonBtn = $("mcpTokenCopyJsonBtn");
    if (mcpCopyJsonBtn) mcpCopyJsonBtn.addEventListener("click", () => {
        const cfg = $("mcpTokenConfig");
        if (!cfg || !cfg.textContent) return;
        copyTextAny(cfg.textContent).then((ok) => {
            if (ok) { mcpCopyJsonBtn.textContent = "已复制"; setTimeout(() => { mcpCopyJsonBtn.textContent = "仅复制配置 JSON"; }, 1600); }
        });
    });
    const tokenList = $("mcpTokenList");
    if (tokenList) tokenList.addEventListener("click", (ev) => {
        const revokeBtn = ev.target.closest(".mcp-token-revoke");
        if (revokeBtn) revokeOnlineMcpToken(revokeBtn.dataset.tid, revokeBtn);
    });
    setHealthArrivedHook(syncOnlineMcpBlock);
    syncOnlineMcpBlock();
    // 结果摘要卡不再有「查看每一步」折叠，无需接线其展开/收起。
    $("overlay").addEventListener("click", closeSettings);

    const qi = $("queryInput");
    // 输入即失效旧解析（LAST_INTERPRETATION 置空供检索侧判旧），但**不再同步刷两枚 pill 摘要**
    // （2026-08-03 缺陷修复·防跳）：打字期间 pill 文本与框宽完全静止——摘要在两处更新：
    // ① 停笔 500ms 后识别成功（scheduleInterpretationPreview 回调）一次性换新；② 文字删光
    // （该函数的空查询分支）立即回「自动识别」待命。用户手动改来源/时间模式仍即时刷新（apply*Mode）。
    // 清空输入 → 退出结果态：hero 从 slim 恢复完整首屏（L1，exitResultsLayout 复用 has-results 机制）。
    // 例外（2026-08-04）：chat-in-main 时主框是**对话起草器**（发送即清空），在里面删字
    // 只是改草稿，不得触发退出——exitResultsLayout 的 cbClear 会把整段对话记录清掉（用户报的
    // 「删干净消息聊天记录没了」）。forceReset=true 是显式「新开对话」入口（导航重击智能查询），
    // 那条路照旧清。
    const onQueryInput = (forceReset) => { LAST_INTERPRETATION = null; setLastRecommendData(null); setRememberSearchAvailable(false); autoGrow(qi); renderMemorySuggestions(); scheduleInterpretationPreview(); if (!qi.value.trim() && (forceReset === true || !cbChatInMain())) exitResultsLayout(); };
    qi.addEventListener("input", () => onQueryInput());
    // maxlength 上限提示（2026-08-04）：500 字截断是无声的——贴长文时用户以为整段都进来了。
    // 越过上限那一刻轻提示一次，回落后重新武装（不逐键轰炸）。主框与「继续说」框同一上限同一提示。
    const bindMaxHint = (el) => {
        if (!el) return;
        const cap = Number(el.getAttribute("maxlength")) || 0;
        if (!cap) return;
        let armed = true;
        el.addEventListener("input", () => {
            if (el.value.length >= cap) {
                if (armed) { armed = false; toast("最多输入 " + cap + " 字，超出的部分没有进来"); }
            } else armed = true;
        });
    };
    bindMaxHint(qi);
    // 回车即提交（搜索框通用预期）；Shift+Enter 换行；Ctrl/Cmd+Enter 旧习惯已并入同一回车。
    // isComposing 守卫：中文输入法组词中回车用于上屏选词，绝不触发提交。
    // 统一对话窗口：一个框，提交先问后端统一路由 /api/utterance 再分发（board.js ubSubmit）。
    qi.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" || e.isComposing) return;
        if (e.shiftKey) return;   // Shift+Enter = 换行（走 textarea 默认行为）
        e.preventDefault();
        ubSubmit();
    });
    document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => { qi.value = c.dataset.q || ""; onQueryInput(); qi.focus(); }));
    $("submitBtn").addEventListener("click", () => ubSubmit());

    // 微信式统一输入行：侧栏工作卡最下方的「继续说」框，结果态唯一输入入口。
    // 与主框同一条 ubSubmit 路由（来源参数 "chat"：发送即清空、默认为空）。
    const ci = $("chatInput");
    if (ci) {
        ci.addEventListener("input", () => autoGrow(ci));
        bindMaxHint(ci);
        ci.addEventListener("keydown", (e) => {
            if (e.key !== "Enter" || e.isComposing) return;
            if (e.shiftKey) return;   // Shift+Enter = 换行（与主框同约定）
            e.preventDefault();
            ubSubmit("chat");
        });
    }
    const csb = $("chatSendBtn");
    if (csb) csb.addEventListener("click", () => ubSubmit("chat"));

    // 设置三维度（2026-08-03）：LLM 依赖开关统一过禁点闸 aiGateChange（未配 key 弹回并指路）；
    // 非依赖开关直接 sync。API 配置项的变动会翻转 llmCapable → 门控视觉随之重算。
    ["cfgRerank", "cfgAutoLlm", "cfgPolish", "cfgAgentExec"].forEach((id) => {
        const b = $(id);
        if (b) b.addEventListener("change", () => aiGateChange(b));
    });
    $("cfgStrategy").addEventListener("change", syncStrategyNode);
    $("cfgRecall").addEventListener("change", syncStrategyNode);
    ["cfgApiKey", "cfgBaseUrl", "cfgModel"].forEach((id) => {
        const b = $(id);
        if (b) b.addEventListener("input", syncAiGates);
    });
    // 填数题：失焦时把越界值收敛到边界（与提交时 getConfig 的 clampInt 同口径），让输入框显示的就是真正生效的值
    $("cfgTopK").addEventListener("change", () => { $("cfgTopK").value = String(clampInt($("cfgTopK").value, 10, 1, 50)); });
    $("cfgRerankTopN").addEventListener("change", () => { $("cfgRerankTopN").value = String(clampInt($("cfgRerankTopN").value, 12, 1, 50)); });
    $("cfgProvider").addEventListener("change", () => applyPreset($("cfgProvider").value, { force: true }));
    // 记住设置 / 记住api key 联动（2026-08-25）：key 落盘的前提是设置落盘，
    // 文案精简后这层关系用行为表达——勾 key 自动带上设置；撤设置一并撤 key（与 saveConfig 的存储口径一致）。
    { const rk = $("cfgRememberApiKey"); if (rk) rk.addEventListener("change", () => {
        if (rk.checked) $("cfgSaveSession").checked = true;
    }); }
    { const ss = $("cfgSaveSession"); if (ss) ss.addEventListener("change", () => {
        if (!ss.checked) $("cfgRememberApiKey").checked = false;
    }); }
    // 收藏视图 →「生成复用出处清单」（reuse_pack.js）。用 if 守卫而非直接绑：
    // 该按钮只在收藏视图存在，且清单是可选能力，缺元素不该掀翻整个 bind 流程。
    { const rp = $("reusePackBtn"); if (rp) rp.addEventListener("click", () => buildReusePack()); }
    // 复用清单范围下拉（全部收藏 / 各收藏夹）与「管理收藏夹」按钮——同为收藏视图专属，缺元素不掀翻 bind
    { const rs = $("reuseScope"); if (rs) rs.addEventListener("change", () => setReuseScope(rs.value)); }
    { const fm = $("favFolderManageBtn"); if (fm) fm.addEventListener("click", () => toggleFavFolderManage()); }
    $("saveConfigBtn").addEventListener("click", saveConfig);
    $("diagnoseBtn").addEventListener("click", runDiagnose);

    // 浏览搜索防抖：参照本文件识别预览 500ms debounce 的先例（scheduleInterpretationPreview，
    // 约 71 行），fSearch 每击键曾同步触发 filteredDatasets 全库扫描 + liveYearCounts 二次扫描 +
    // 时间线 DOM 重建；250ms 停顿触发跟手感几乎不变。只防抖 fSearch 的 input——其他筛选器是
    // change 事件（离散选择），照旧即时生效。
    $("fSearch").addEventListener("input", () => {
        if (browseSearchTimer) clearTimeout(browseSearchTimer);
        browseSearchTimer = setTimeout(() => { bs.q = $("fSearch").value.trim(); bs.page = 1; renderBrowse(); }, 250);
    });
    $("fSource").addEventListener("change", () => { bs.source = $("fSource").value; bs.page = 1; renderBrowse(); });
    $("fSpecies").addEventListener("change", () => { bs.species = $("fSpecies").value; bs.page = 1; renderBrowse(); });
    $("fPlatform").addEventListener("change", () => { bs.platform = $("fPlatform").value; bs.page = 1; renderBrowse(); });
    $("fFastq").addEventListener("change", () => { bs.fastq = $("fFastq").checked; bs.page = 1; renderBrowse(); });
    $("fYearFrom").addEventListener("change", () => applyBrowseYearRange("from"));
    $("fYearTo").addEventListener("change", () => applyBrowseYearRange("to"));
    $("timelineClear").addEventListener("click", () => { bs.yearFrom = ""; bs.yearTo = ""; $("fYearFrom").value = ""; $("fYearTo").value = ""; bs.page = 1; renderBrowse(); });
    // 一键复位全部筛选维度（关键词/来源/物种/平台/FASTQ/年份范围）并同步控件值；
    // 时间线「清除」只管年份范围，这个是全量入口。renderBrowse → syncBrowseTimelineState 会重算柱 active 态与 timelineClear disabled。
    $("browseFilterClear").addEventListener("click", () => {
        bs.q = ""; bs.source = ""; bs.species = ""; bs.platform = ""; bs.fastq = false;
        bs.yearFrom = ""; bs.yearTo = ""; bs.page = 1;
        $("fSearch").value = ""; $("fSource").value = ""; $("fSpecies").value = ""; $("fPlatform").value = "";
        $("fFastq").checked = false; $("fYearFrom").value = ""; $("fYearTo").value = "";
        renderBrowse();
    });
    $("browsePrev").addEventListener("click", () => { if (bs.page > 1) { bs.page--; renderBrowse(); scrollBrowseTop(); } });
    $("browseNext").addEventListener("click", () => { bs.page++; renderBrowse(); scrollBrowseTop(); });
    $("fPageSize").addEventListener("change", () => { bs.pageSize = clampInt($("fPageSize").value, 24, 1, 200); bs.page = 1; renderBrowse(); scrollBrowseTop(); });

    const uz = $("uploadZone");
    uz.addEventListener("click", () => $("uploadInput").click());
    uz.addEventListener("dragover", (e) => { e.preventDefault(); uz.classList.add("drag"); });
    uz.addEventListener("dragleave", () => uz.classList.remove("drag"));
    uz.addEventListener("drop", (e) => { e.preventDefault(); uz.classList.remove("drag"); const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]; if (f) { try { const dt = new DataTransfer(); dt.items.add(f); $("uploadInput").files = dt.files; } catch (_e) {} $("uploadStatus").textContent = "已选择：" + f.name; } });
    $("uploadInput").addEventListener("change", () => { const f = $("uploadInput").files[0]; if (f) $("uploadStatus").textContent = "已选择：" + f.name; });
    $("uploadBtn").addEventListener("click", () => uploadFile($("uploadInput").files[0]));

    // 清空历史必须写**当前账户命名空间**键（与 getHist/pushHist 的 nsKey 对齐）——
    // 写裸 LS.hist 会既清不掉登录用户自己的历史、又误清匿名命名空间（账户隔离漏洞）。
    // 二段确认——第一次点进入确认态（3 秒内再点才执行，超时自动复位），空历史时禁用（renderHistory 同步）。
    $("histClear").addEventListener("click", () => {
        const b = $("histClear");
        if (!armTwoStepConfirm(b, { idleText: COPY.common.clear,
                                   confirmText: COPY.common.confirmClear })) return;
        resetHistClear();
        writeJSON(nsKey(LS.hist), []); renderHistory(); toast("历史已清空");
    });

    // 文件弹窗：关闭按钮 / 点遮罩空白处 / Esc（Esc 也顺手关设置抽屉）
    $("filesModalClose").addEventListener("click", closeFilesModal);
    $("filesModal").addEventListener("click", (e) => { if (e.target === $("filesModal")) closeFilesModal(); });
    // 介绍弹窗退役——「查看介绍」是 <a target=_blank> 直接开新标签页 /dataset，首页不再有 introModal 接线。
    document.addEventListener("keydown", (e) => {
        trapDialogFocus(e);
        if (e.key !== "Escape") return;
        // 逐层关，最上层优先：记忆/文件弹窗 → 设置抽屉 → 来源/时间浮面板 → 档案浮窗 → 移动端侧栏。
        if ($("memoryModal") && !$("memoryModal").hidden) { closeMemoryModal(); return; }
        if ($("filesModal") && !$("filesModal").hidden) { closeFilesModal(); return; }
        if ($("settings").classList.contains("open")) { closeSettings(); return; }
        // 来源/时间面板被 placeScopeControls 零复制搬进 #scopePop 后 hidden 恒=false（弹层内恒展开是
        // 刻意设计：可见性由 scopePop 自身的 hidden 遮蔽，弹层由 board.js 自己的 Esc 监听负责关）。
        // 托管态下它们不算「打开着的浮层」——旧链第一环只看 panel.hidden，结果态每次 Esc 都命中
        // openSrcPanel(false)（托管态拒关、hidden 恒 false 不复位）然后 return：事件被吞，
        // 上面三处弹窗/抽屉的 Esc 永远走不到。故只在面板**不**寄生于 scopePop 时
        // 才把「未 hidden」当作「有浮面板开着」。
        const hosted = (p) => !!(p && p.closest(".scope-pop"));   // 弹层托管判据泛化（2026-08-30）：侧栏 #scopePop 与首页 #heroScopePop 同为 .scope-pop
        const srcP = $("srcPanel"), timeP = $("timePanel");
        if (srcP && !srcP.hidden && !hosted(srcP)) { openSrcPanel(false); return; }
        if (timeP && !timeP.hidden && !hosted(timeP)) { openTimePanel(false); return; }
        // 我的库 / 历史浮窗也进 Esc 逐层链（拆回两窗后各自吃 Esc——非模态浮层里
        // 没有比它更上层的了，不关它就会一路落到移动端抽屉分支——桌面档 Esc 眼睁睁看着什么都不发生。
        if (libWinOpen()) { closeLibWin(); return; }
        if (histWinOpen()) { closeHistWin(); return; }
        closeMobileDrawer();   // Esc 逐层收尾：浮层都关完后，移动端抽屉也收（同导航早退分支一口径）
    });
}
/* 首屏问候（用户 2026-08-18）：h1 从「用一句话，找到对的数据集」换成按时段的情景问候
   （Claude 首页式），「能做什么」交给副标题讲。按本地小时分桶、全天 24h 无缝覆盖，
   不会出现中午说晚上好的错位；整句统一为同一文字样式（不再给后半句挂 .grad
   渐变——前后半句两种颜色观感割裂）。HTML 里留有静态兜底文案，JS 不可用也不失态；
   出结果后 h1 随 slim hero 一并隐藏（app.css .has-results 规则），问候不会抢结果的戏。 */
export function renderHeroGreeting() {
    const el = $("heroGreeting");
    if (!el) return;
    const h = new Date().getHours();
    let text;
    if (h >= 5 && h < 11) { text = "早上好呀，今天想做点什么"; }
    else if (h >= 11 && h < 14) { text = "中午好呀，吃过饭了吗"; }
    else if (h >= 14 && h < 18) { text = "下午好呀，今天想做点什么"; }
    else if (h >= 18 && h < 23) { text = "晚上好呀，今天辛苦啦"; }
    else { text = "夜深了，别太累着自己"; }
    el.textContent = text;
}
/* 首页 hero 轮播（视觉 spec §8.3）：三条文案（时段问候/标语一/标语二）同网格位叠放交叉淡化；
   5s 间隔、hover/focusin/visibilitychange 暂停、REDUCE_MOTION 下静止第一条（不轮播）。
   `renderHeroGreeting` 仍负责把 slide 1 的问候文案按小时更新——两函数顺序无关，互不触碰。 */
const HERO_ROT_MS = 5000;
let _heroIdx = 0, _heroTimer = null;
function _heroGoto(n) {
    const slides = Array.from(document.querySelectorAll("#heroRot .hero-slide"));
    if (!slides.length) return;
    slides.forEach((s, i) => {
        const on = i === n;
        if (!on && s.classList.contains("is-on")) {
            s.classList.add("is-done");                    // 离场向上飘
            setTimeout(() => s.classList.remove("is-done"), 480);
        }
        s.classList.toggle("is-on", on);
        s.setAttribute("aria-hidden", on ? "false" : "true");
    });
    _heroIdx = n;
}
function _heroRotStop() { if (_heroTimer) { clearInterval(_heroTimer); _heroTimer = null; } }
function _heroRotPlay() {
    _heroRotStop();
    _heroTimer = setInterval(() => {
        const n = document.querySelectorAll("#heroRot .hero-slide").length;
        if (n > 1) _heroGoto((_heroIdx + 1) % n);
    }, HERO_ROT_MS);
}
export function initHeroRot() {
    const rot = $("heroRot");
    if (!rot || REDUCE_MOTION) return;                     // 减弱动效：静止第一条，不轮播
    rot.addEventListener("mouseenter", _heroRotStop);      // hover 暂停
    rot.addEventListener("mouseleave", _heroRotPlay);
    rot.addEventListener("focusin", _heroRotStop);         // 键盘聚焦同样暂停
    rot.addEventListener("focusout", _heroRotPlay);
    document.addEventListener("visibilitychange", () => {  // 后台标签页不计时
        if (document.hidden) _heroRotStop(); else _heroRotPlay();
    });
    _heroRotPlay();
}
/* 首屏入场时间线：侧栏、主标题、副标题、搜索框、示例 chips 依次浮现。只跑一次。 */
export function playHero() {
    if (!MOTION) return;
    const tl = gsap.timeline({ defaults: { ease: "power3.out" } });
    // autoAlpha 系的 tween 一律 clearProps:"all"（2026-08-03 侧边栏顽疾根因）：
    // 只清 transform 会把 autoAlpha 写下的内联 opacity/visibility 永久留在元素上，
    // 内联优先级压过 body.side-closed 的淡出规则——侧栏收起时以全不透明姿态滑出，
    // 正是「没有按常规折叠」的观感来源。第二套显隐机制（内联样式）就此拆除。
    if (!document.body.classList.contains("side-closed"))
        tl.from(".sidebar", { x: -18, autoAlpha: 0, duration: 0.5, clearProps: "all" }, 0);
    // hero 三条问候/标语合一容器轮播，出场只以 .hero-rot 为整体入场（内部 slide 由 hero-rot CSS
    // 各自过渡，不与入场抢 transform/opacity——与侧栏顽疾同根因，clearProps:"all" 必须保留，
    // 否则内联 opacity/transform 压住 .hero-slide 的 CSS transition）。
    tl.from(".hero-rot", { y: 22, autoAlpha: 0, duration: 0.7, clearProps: "all" }, 0.05)
        .from(".hero .console", { y: 18, autoAlpha: 0, duration: 0.6, clearProps: "all" }, 0.26)
        .from(".hero .chip", { y: 12, autoAlpha: 0, duration: 0.5, stagger: 0.05, clearProps: "all" }, 0.36);
}
/* 发表时间范围：年份可**手动输入**（2026-08-05 用户点3：下拉 2016 地板太死），datalist 给
   2000–今年 的快捷选项；非法输入（非四位年份/超出 1900–今年+1）清空并 toast。读取为 ISO 起止。 */
export function initTimeFilter() {
    const from = $("dateFrom"), to = $("dateTo");
    if (!from || !to) return;
    const maxY = new Date().getFullYear(), minY = 1900;   // 输入自由度上限放到今年+1（预发表/在途数据）
    const dl = $("yearChoices");
    if (dl && !dl.dataset.filled) {
        let opts = "";
        for (let y = maxY; y >= 2000; y--) opts += '<option value="' + y + '"></option>';
        dl.innerHTML = opts;
        dl.dataset.filled = "1";
    }
    const sanitize = (el) => {
        const raw = String(el.value || "").trim();
        if (!raw) return true;   // 空 = 不限
        if (!/^\d{4}$/.test(raw) || +raw < minY || +raw > maxY + 1) {
            el.value = "";
            toast("年份请输入四位数字（" + minY + "–" + (maxY + 1) + "），已清空");
            return false;
        }
        return true;
    };
    const onChange = (el) => {
        if (!sanitize(el)) return;
        el.classList.toggle("set", !!el.value); setTimeMode("custom");
        const r = customRangeYears();
        // 面板类/激活态/摘要/重检一处同步（点8：此前只写 LS 不调 applyTimeMode，面板停在 mode-auto——
        // 弹层里「自动识别」假激活、年份下拉样式与新态不符，正是「切换自动与否没反应」的另一半）。
        applyTimeMode("custom", "范围：发表时间 " + (fmtYearRange(r.from, r.to) || "不限"));
    };
    from.addEventListener("change", () => onChange(from));
    to.addEventListener("change", () => onChange(to));
}
/* ---------- 发表时间：折叠 pill + 自动识别 / 自定义（镜像来源选择器） ---------- */
// auto=从查询里识别时间（"2020年以来""近三年""2018-2022""今年"…），没提到就不限；custom=手动年份下拉。
function getTimeMode() { const m = readJSON(LS.timeMode, null); return m === "custom" ? "custom" : "auto"; }
function setTimeMode(m) { writeJSON(LS.timeMode, m === "custom" ? "custom" : "auto"); }
// 最终解析以共享后端为唯一真源。浏览器不再删时间/来源短语，避免 Web 与 MCP 对同一句话得出不同结论。
export function queryForRetrieval(q) {
    return String(q || "").replace(/\s+/g, " ").trim();
}
function fmtYearRange(from, to) {
    if (from && to) return from === to ? (from + " 年") : (from + "–" + to);
    if (from) return from + " 年起";
    if (to) return "截至 " + to + " 年";
    return "";
}
function customRangeYears() {
    const fromEl = $("dateFrom"), toEl = $("dateTo");
    let a = fromEl ? fromEl.value : "", b = toEl ? toEl.value : "";
    if (a && b && a > b) { const t = a; a = b; b = t; }   // 防呆：起 > 止则交换
    return { from: a, to: b };
}
export function getDateRange(query) {
    if (getTimeMode() === "auto") return { date_from: "", date_to: "" };   // 原句交给后端/MCP 共用 parser
    const r = customRangeYears();
    return { date_from: r.from ? r.from + "-01-01" : "", date_to: r.to ? r.to + "-12-31" : "" };
}
/* 范围控件已搬进侧栏「对话记录」时（body.scope-in-side），改动即刻重跑（keepFacets 保留细化）——
   与「数据细化」里点分面即刻生效同一约定；在主控制台态（未 scope-in-side）维持原行为：改摘要、下次检索才生效。
   顺手把这次范围改动写进对话/细化记录。空查询时不跑（还没有可细化的结果集）。 */
function scopeSidebarRerun(desc) {
    if (!document.body.classList.contains("scope-in-side")) return;
    const qi = $("queryInput");
    if (!qi || !qi.value.trim()) return;
    if (desc) cbLogPush("refine", desc);
    runRecommend({ keepFacets: true });
}
export function updateTimeSummary() {
    const el = $("timeSummary");
    if (!el) return;
    animateConsoleWidth();   // 摘要将变 → 给 .console 宽度变化补过渡（rAF 延后到文字更新之后量新宽度）
    const set = (t) => { el.textContent = t; el.title = t; };   // title 恒存全文（同 updateSrcSummary）
    if (getTimeMode() === "auto") {
        const q = (($("queryInput") && $("queryInput").value) || "").trim();
        if (!q) { set("自动识别"); return; }                              // 空查询：干净待命
        const same = LAST_INTERPRETATION && String(LAST_INTERPRETATION.original_query || "").trim() === q;
        if (!same) { set("自动识别" + _interpretNote()); return; }
        const intent = LAST_INTERPRETATION.intent || {};
        const from = String(intent.date_from || "").slice(0, 4), to = String(intent.date_to || "").slice(0, 4);
        const disp = fmtYearRange(from, to);
        set(disp ? ("自动识别 · " + disp) : "自动识别 · 不限");
        return;
    }
    const disp = fmtYearRange(customRangeYears().from, customRangeYears().to);
    set(disp || "不限");
}
/* 弹层自适应（2026-08-13 用户点图）：控制台落在视口底部时（chat-in-main 态），向下展开的
   面板会被屏幕下缘截断。开面板后量一次：底部越界 → 翻成向上展开（.drop-up）；右缘越界 → 左收。
   acct-menu 的 drop-down 早已同思路（accounts.js），这里推广到来源/时间筛选面板。 */
function fitPopover(p) {
    p.classList.remove("drop-up");
    p.style.left = "";
    const margin = 8;
    const rect = p.getBoundingClientRect();
    if (rect.bottom > window.innerHeight - margin) p.classList.add("drop-up");
    if (rect.right > window.innerWidth - margin) {
        const shift = Math.min(0, window.innerWidth - margin - rect.right);
        p.style.left = Math.max(shift, margin - rect.left) + "px";
    }
}
export function openTimePanel(open) {
    const t = $("timeToggle"), p = $("timePanel");
    if (!t || !p) return;
    // 弹层托管态（board.js placeScopeControls 把节点零复制搬进 .scope-pop——侧栏 #scopePop 或首页 #heroScopePop）：
    // 面板在弹层里**恒展开**——document 级「点面板外收起」与 Esc 都不得把它合上；否则弹层只剩两行标签、控件全灭（点8：无法改变筛选/切自动）。
    if (!open) { if (p.closest(".scope-pop")) { p.hidden = false; return; } }
    const refocus = !open && p.contains(document.activeElement);
    p.hidden = !open;
    if (open) fitPopover(p); else { p.classList.remove("drop-up"); p.style.left = ""; }
    t.setAttribute("aria-expanded", open ? "true" : "false");
    if (refocus) t.focus();
}
function applyTimeMode(mode, rerunDesc) {
    const p = $("timePanel");
    if (!p) return;
    const sel = p.closest(".sf-select");
    p.classList.toggle("mode-auto", mode === "auto");
    p.classList.toggle("mode-custom", mode === "custom");
    if (sel) sel.classList.toggle("is-custom", mode === "custom");
    p.querySelectorAll(".sf-mode").forEach((b) => {
        const on = b.dataset.mode === mode;
        b.classList.toggle("active", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
    });
    const hint = $("timeHint");
    if (hint) hint.textContent = mode === "auto"
        ? "从你的描述里自动识别时间范围（如「2020 年以来」「近三年」）；没提到就不限。"
        : "手动选择发表年份范围（可只填一端；留空即不限）。";
    updateTimeSummary();
    scheduleInterpretationPreview();
    scopeSidebarRerun(rerunDesc || "范围：时间" + (mode === "auto" ? "自动识别" : "自定义"));
}
export function initTimeSelector() {
    const t = $("timeToggle"), p = $("timePanel");
    if (!t || !p) return;
    t.addEventListener("click", (e) => { e.stopPropagation(); openSrcPanel(false); openTimePanel(p.hidden); });
    p.addEventListener("click", (e) => e.stopPropagation());
    p.querySelectorAll(".sf-mode").forEach((b) => b.addEventListener("click", () => {
        const m = b.dataset.mode === "custom" ? "custom" : "auto";
        setTimeMode(m);
        applyTimeMode(m);
    }));
    document.addEventListener("click", () => openTimePanel(false));
    applyTimeMode(getTimeMode());
}
/* ---------- 数据来源自由勾选（智能查询：在哪些库中检索） ---------- */
let AVAILABLE_SOURCES = [];   // [{value, count}]，来自 /api/sources（10x Genomics 恒置顶）
export let SOURCES_READY = null;     // initSourceChips() 的 promise：检索前 await 它，避免竞态兜底成仅 10x
/* 属主写口（约定 #4）：boot.js 启动时登记 promise（原 `SOURCES_READY = initSourceChips()` 裸赋值
   在 getter 桥上会 TypeError）；search.js 只经活 getter 读。 */
export function setSourcesReady(v) { SOURCES_READY = v; }
function srcChipLabel(v) { return prettySource(v); }   // 短标签：10x / CELLxGENE / HCA / EBI SCEA
// 持久化「被取消勾选」的来源集合（而非「已选」）：默认(空)=全选；日后新增平台自动选中；
// 仅用户显式取消的来源才被记住。避免旧存档把新库/全部库误置为未选。
function getSourcesOff() { const v = readJSON(LS.sourcesOff, []); return new Set(Array.isArray(v) ? v : []); }
// 模式：auto=自动识别（并检所有来源、结果里哪些命中就"识别"哪些）；custom=手动勾选。
// 首访默认 auto；但若老用户曾显式取消过来源（off 非空）→ 尊重其自定义，落到 custom。
export function getSourceMode() {
    const stored = readJSON(LS.sourceMode, null);
    if (stored === "auto" || stored === "custom") return stored;
    return getSourcesOff().size ? "custom" : "auto";
}
function setSourceMode(m) { writeJSON(LS.sourceMode, m === "custom" ? "custom" : "auto"); }
function allSourceValues() { return AVAILABLE_SOURCES.map((s) => s.value); }
function customSelectedSources() {
    const box = $("sourceChips");
    const sel = box ? [...box.querySelectorAll(".src-chip.active")].map((c) => c.dataset.source) : [];
    if (sel.length) return sel;
    if (AVAILABLE_SOURCES.length) {   // chips 未渲染但已知全集：全部减去被取消的
        const off = getSourcesOff();
        const on = allSourceValues().filter((v) => !off.has(v));
        return on.length ? on : allSourceValues();
    }
    return ["10x Genomics"];   // /api/sources 未返回时的兜底（保证是有效来源）
}
export function getSelectedSources() {
    if (getSourceMode() === "auto") {
        if (!AVAILABLE_SOURCES.length) return ["10x Genomics"];
        return allSourceValues();   // 作为允许池交给后端；是否按原句收窄由共享解析层决定
    }
    return customSelectedSources();
}
function persistSources() {
    try {
        const box = $("sourceChips");
        if (!box) return;
        const active = new Set([...box.querySelectorAll(".src-chip.active")].map((c) => c.dataset.source));
        const off = AVAILABLE_SOURCES.map((s) => s.value).filter((v) => !active.has(v));
        writeJSON(LS.sourcesOff, off);
    } catch (_e) {}
}
function renderSourceChips() {
    const box = $("sourceChips");
    if (!box) return;
    const off = getSourcesOff();
    box.innerHTML = AVAILABLE_SOURCES.map((s) => {
        const on = !off.has(s.value);   // 默认全选（并列检索所有库）；仅被显式取消的关闭
        return `<button type="button" class="src-chip ${on ? "active" : ""}" data-source="${escapeHtml(s.value)}" aria-pressed="${on ? "true" : "false"}" title="${escapeHtml(s.value)}">${escapeHtml(srcChipLabel(s.value))}<span class="c">${s.count}</span></button>`;
    }).join("");
    if (!box.querySelector(".src-chip.active")) box.querySelectorAll(".src-chip").forEach((c) => { c.classList.add("active"); c.setAttribute("aria-pressed", "true"); });
    box.querySelectorAll(".src-chip").forEach((chip) => {
        chip.addEventListener("click", () => {
            const active = box.querySelectorAll(".src-chip.active");
            if (chip.classList.contains("active") && active.length <= 1) { toast("至少保留一个数据来源"); return; }
            chip.classList.toggle("active");
            chip.setAttribute("aria-pressed", chip.classList.contains("active") ? "true" : "false");
            setSourceMode("custom");   // 手动勾选即锁定「自定义」：off-set 归空也不再被 getSourceMode 派生回 auto
            persistSources();
            // 面板类/激活态/摘要/重检一处同步（点8：此前只写 LS 不调 applySourceMode，弹层里「自动识别」假激活）。
            applySourceMode("custom", "范围：来源 " + customSelectedSources().length + " 个");
        });
    });
}
export async function initSourceChips() {
    try {
        const d = await (await fetch(API.sources)).json();
        AVAILABLE_SOURCES = (d && d.sources) || [];
    } catch (_e) { AVAILABLE_SOURCES = [{ value: "10x Genomics", count: 0 }]; }
    if (!AVAILABLE_SOURCES.length) AVAILABLE_SOURCES = [{ value: "10x Genomics", count: 0 }];
    renderSourceChips();
    updateSrcSummary();   // 来源清单就绪后回填折叠 pill 的摘要
}
/* ---------- 来源选择器：折叠 pill + 展开面板（自动识别 / 自定义） ---------- */
export function updateSrcSummary() {
    const el = $("srcSummary");
    if (!el) return;
    animateConsoleWidth();   // 摘要将变 → 给 .console 宽度变化补过渡（rAF 延后到文字更新之后量新宽度）
    // title 恒存全文（.sf-cur 省略号截断后，悬停仍读得到完整摘要）。
    const set = (t) => { el.textContent = t; el.title = t; };
    if (getSourceMode() === "auto") {
        const q = (($("queryInput") && $("queryInput").value) || "").trim();
        if (!q) { set("自动识别"); return; }                              // 空查询：干净待命
        const same = LAST_INTERPRETATION && String(LAST_INTERPRETATION.original_query || "").trim() === q;
        if (!same) { set("自动识别" + _interpretNote()); return; }
        if (LAST_INTERPRETATION.automatic_skipped_reason) { set("自动识别 · 全部来源（这次没有自动收窄）"); return; }
        const det = LAST_INTERPRETATION.detected_sources || [];
        if (!det || !det.length) {   // 未点名来源 → 并检全部；显式报出库数，让「覆盖几个数据库」一眼可见（不再只写「不限」）
            const n = AVAILABLE_SOURCES.length;
            set(n > 1 ? ("自动识别 · 全部 " + n + " 个来源") : "自动识别 · 不限");
            return;
        }
        set(det.length === 1
            ? ("自动识别 · " + prettySource(det[0]))                   // 点名单一来源 → 收窄并显示落点
            : ("自动识别 · " + det.length + " 个来源"));
        return;
    }
    const total = AVAILABLE_SOURCES.length;
    if (!total) { set("自定义"); return; }   // 来源未载入前不显示误导计数，待 initSourceChips 回填
    const on = customSelectedSources().length;
    set((on >= total) ? ("全部 " + total + " 个来源") : (on + " 个来源"));
}
export function openSrcPanel(open) {
    const t = $("srcToggle"), p = $("srcPanel");
    if (!t || !p) return;
    // 弹层托管态（board.js placeScopeControls 把节点零复制搬进 .scope-pop——侧栏 #scopePop 或首页 #heroScopePop）：
    // 面板在弹层里**恒展开**——document 级「点面板外收起」与 Esc 都不得把它合上；否则弹层只剩两行标签、控件全灭（点8：无法改变筛选/切自动）。
    if (!open) { if (p.closest(".scope-pop")) { p.hidden = false; return; } }
    const refocus = !open && p.contains(document.activeElement);   // 关闭前记录焦点是否在面板内（须在 hidden 前求值）
    p.hidden = !open;
    if (open) fitPopover(p); else { p.classList.remove("drop-up"); p.style.left = ""; }
    t.setAttribute("aria-expanded", open ? "true" : "false");
    if (refocus) t.focus();   // 键盘 Esc / 失焦收起时把焦点还给 pill；鼠标点外部时 activeElement 不在面板内→不抢焦
}
function applySourceMode(mode, rerunDesc) {
    const p = $("srcPanel");
    if (!p) return;
    const sel = p.closest(".sf-select");
    p.classList.toggle("mode-auto", mode === "auto");
    p.classList.toggle("mode-custom", mode === "custom");
    if (sel) sel.classList.toggle("is-custom", mode === "custom");
    p.querySelectorAll(".sf-mode").forEach((b) => {
        const on = b.dataset.mode === mode;
        b.classList.toggle("active", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
    });
    const hint = $("srcHint");
    if (hint) hint.textContent = mode === "auto"
        ? "根据你的描述自动匹配相关来源，无需手动挑选。"
        : "勾选想检索的数据来源（至少保留一个）。";
    updateSrcSummary();
    scheduleInterpretationPreview();
    scopeSidebarRerun(rerunDesc || "范围：来源" + (mode === "auto" ? "自动识别" : "自定义"));
}
export function initSourceSelector() {
    const t = $("srcToggle"), p = $("srcPanel");
    if (!t || !p) return;
    t.addEventListener("click", (e) => { e.stopPropagation(); openTimePanel(false); openSrcPanel(p.hidden); });
    p.addEventListener("click", (e) => e.stopPropagation());   // 面板内点击不冒泡到 document（不误关）
    p.querySelectorAll(".sf-mode").forEach((b) => b.addEventListener("click", () => {
        const m = b.dataset.mode === "custom" ? "custom" : "auto";
        setSourceMode(m);
        applySourceMode(m);
    }));
    document.addEventListener("click", () => openSrcPanel(false));   // 点面板外收起
    applySourceMode(getSourceMode());
}
