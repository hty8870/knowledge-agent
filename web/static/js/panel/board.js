"use strict";

/* C4 起本文件是 ES Module：core 的工具、board_core 纯核（cbRowsFrom/cbSummary/cbPushFrame）、
   search 的 runRecommend/applyRecommendResult/bumpRecSeq/setLastRecommendData 与 LAST_RECOMMEND_DATA
   （活绑定只读）、results 的 enterResultsLayout/setFacetState/resetFacetState/toggleLenient 与四个
   分面状态（活绑定只读）、facets 的 toggleQueryHit/facetStageSubmit/facetStageCancel/placeFacetBar、
   progress 的 resetSubmitButton、usage_log/usage_core、shell 的 getConfig、interactions 的
   autoGrow/getDateRange 经 import 取；act（actEnabled/actDispatchPlanChain）与
   task_pack（previewTaskPack/tpCountFromUtterance）同批互转走 import（board↔act 成环，
   但绑定都只在函数体内使用，ESM 允许）。
   core/search/results/facets/interactions/browse/accounts 同样经 import 取本文件导出
   （绞杀桥全退役）。 */
import { API, $, escapeHtml, MOTION, ghostExit, nsKey, openPopup, pushHistChatOnly, setHistHooks, toast, currentAccountScope } from "#core";
import { cbPushFrame, cbRowsFrom, cbSummary, CB_MAX_FRAMES, cbMsgFbNext, cbMsgCommentText, cbMsgForkable, searchFactsReceiptText, plansNeedActReceipt, PLAN_CANCELLED_FALLBACK_ZH } from "#board_core";
import { applyRecommendResult, bumpRecSeq, LAST_RECOMMEND_DATA, landRecommendResult,
    recSeqNow, runRecommend, searchParamSnapshot, setLastRecommendData, setPrelimBadge } from "#search";
import { _facetFilters, _lenientDims, _queryHits, _suppressed,
    renderBatchSwitcher, resetFacetState, setFacetState, switchBatch, toggleLenient } from "#results";
import { deriveRelaxCard, isZeroHitBatch, latestActiveBatchId, selectDisplayBatch } from "#batch_select";
import { facetStageCancel, facetStageSubmit, placeFacetBar, toggleQueryHit } from "#facets";
import { estimateDuration, finishProgress, progressActive, resetSubmitButton, startProgress } from "#progress";
import { actCanonicalDispatchPlans, actDispatchPlanChain, actEnabled, actPrimeTurnSearchFacts } from "#act";
import { arxActive, arxFinish, arxTailHtml, arxVisible } from "#act_run";
import { previewTaskPack, tpCountFromUtterance } from "#task_pack";
import { usageLog, usageEnabled, usageSetEnabled, usageConsentGiven, requestUsageConsent, usageScope } from "#usage_log";
import { USAGE_KINDS, usageBeginTurn } from "#usage_core";
import { benchfbAfterRender, benchfbOnChatEntry, benchfbSetAutoGrow, benchfbTurnBegin, benchfbTurnEcho, benchfbTurnError, benchfbTurnNote, benchfbTurnRoute } from "#benchfb";
import { autoGrow, getDateRange, openSrcPanel, openTimePanel } from "#interactions";
import { agentExtMissing, getConfig, llmCapable, openSettings, revealSetting } from "#shell";
import { compressFlow, flowVerbLabel, KIND_TOOL, renderableStages, shouldDiscardOutcome, stageFromEvent, upsertStage } from "#flow_trace";
import { COPY, armTwoStepConfirm, resetTwoStepConfirm } from "../core/copy.js";

/* ---------- 课题上下文卡反转钩子（2026-08-22 §3.3） ----------
   projects.js 经 setArtifactCtxProvider 注册「当前活动上下文卡的注入文本」取值器、
   经 setArtifactCtxAfterSend 注册「一次发送收尾」回调（发送成功后自动移除卡）。
   刻意不 import projects（projects 反向 import 本文件的 cbChatInMain 等——留 import 边会成环），
   与 setHistHooks 同一套注册反转模式；未注册时两钩子恒空，行为与现状逐位一致。 */
let _artifactCtxProvider = null;
let _artifactCtxAfterSend = null;
/* 注册守卫用 `instanceof Function` 而非 `typeof x === "function"` 字面量——
   test_board_frontend_static 钉死 board.js 不许出现该模式（防打错的函数名被静默短路）。 */
export function setArtifactCtxProvider(fn) { _artifactCtxProvider = (fn instanceof Function) ? fn : null; }
export function setArtifactCtxAfterSend(fn) { _artifactCtxAfterSend = (fn instanceof Function) ? fn : null; }
/* ubRouteBody 构造完主体后附加 artifact_context（请求体**独立字段**、不拼进用户原话
   只进 agent prompt，不进 identifier/query parser 快速道——那是后端 B4 的事）。
   无活动卡 → 不加键（请求体与现状逐位一致）。 */
function _attachArtifactCtx(body) {
    if (!_artifactCtxProvider) return body;
    const ctx = _artifactCtxProvider();
    // 形状扩展读 .text/.kind——序列化文本首行显式带类型（追踪/数据集），后端 prompt 直读。
    if (ctx && typeof ctx.text === "string" && ctx.text) {
        const kindTag = ctx.kind === "dataset" ? "数据集" : "追踪";
        body.artifact_context = "【" + kindTag + "】" + ctx.text;
    }
    return body;
}
function _fireCtxAfterSend(ok) {
    if (_artifactCtxAfterSend) { try { _artifactCtxAfterSend(ok); } catch (_e) {} }
}

/* 条件板 + 统一对话窗口的界面层。

   设计上做三件事：
   1. 把上一次检索的返回 + 三个既有开关画成四个分区的行（纯再投影，绝不自己推断「现在按什么在筛」）；
   2. 统一对话窗口（2026-08-02 微信式输入行）：首屏 `#queryInput`（起始对话）与
      结果态侧栏工作卡最下方的 `#chatInput`（发送即清空、默认为空）共用 `ubSubmit(source)`——
      先问后端统一路由 /api/utterance（turn pipeline：「AI 执行」闸 → 规则检索直达 / LLM 分流，唯一路由脑），
      再按 route 三档分发——search 走 runRecommend 既有流（effective_query 改写如实回显；
      对话窗来的 keepConv 保对话）、tool 直接派发返回的 EXEC plan（curate.* 不需要结果、
      不过「AI 执行」开关永远直派——2026-08-03 全自动化，plan→apply 链式无人工停点）、
      none 在对话流里如实回音
      （降级气泡档 needs_agent 带「去开启 AI 执行」指路按钮）。
      `#queryInput` 仍是「当前检索句」的唯一状态真源。
   3. 侧栏「对话记录」页签承载历史/帧列表：对话记录（#cbHistory，含每条的 查看历史回复气泡按钮）、
      撤销/重做栈照旧；独立输入框 #cbInput 已退役。范围控件（来源/时间）经合并弹层 #scopePop
      收进统一输入条（图7），由 placeScopeControls 与主控制台之间整块搬家。
      #cbHistory 的归宿（p10 起）：**有检索结果**时住侧栏 #sideBoardScroll；**无结果但有对话**
      （首句是工具调用 / clarify 回音 / 检索还在路上）时住主区 hero 内的 #chatMain
      （Codex 式中央列，输入框就是它下方的 hero console）——见 cbChatInMain/placeChatLog。

   刻意不用 typeof x === "function" 这种守卫来包跨模块调用：本项目已经至少两次被这种守卫
   把打错的函数名永久静默短路（最近一次是可行性概览里拼错的来源函数，导致只统计了基础库）。
   名字对不对交给 tests/test_board_frontend_static.py 的逐符号双端断言，让它红，而不是让它静默。 */

var BOARD_COLLAPSED_KEY = "biodata_board_collapsed_v1";

let _cbStack = [];            // 撤销栈：每帧是一次真实检索的完整快照。纯内存，刷新即清空
let _cbCursor = -1;
let _cbFrameSeq = 0;          // 帧号：每帧一个单调递增 id，用来把「聊天/细化记录」的每条消息挂到它对应的那次检索结果上
// （_cbPending「待确认计划」已随「发送后不再有确认界面」删除——needs_confirm 现在直接 cbCommit，没有中间待确认态）
let _cbSeq = 0;               // 规划请求代号：晚到的旧规划一律丢弃
let _cbReplaying = false;     // 正在回放历史帧（此时不推新帧）
/* 默认收起。理由是实测出来的：展开态占 285px，而结果卡本身就有 386px 高——
   两者叠加会让首屏一张结果卡都看不全（实测卡顶被推到 704px，窗口才 720px）。
   收起态仍留一行摘要（正在按几个条件筛、当前多少条结果），它本身就是入口，点一下就展开。
   用户的展开/收起选择会记住。 */
let _cbCollapsed = true;
let _cbChoices = [];          // 同一项冲突时后端给的三个选项（含它已经拆好的取值）
let _askRelaxPending = null;  // ask.relax「放松哪个条件」选择卡的在途载荷（环内 LLM 触发或零命中自动兜底，用户作答/新一轮提交前挂起）

/* 板上现在画的这批条件是**哪一次检索**给的。
   不能拿 LAST_RECOMMEND_DATA 当这个真源：用户在主搜索框里敲任何一个字，
   interactions.js 的 onQueryInput 就会把它置成 null（那是给「解释预览」用的失效信号）。
   于是「敲了个字 → 回头点条件板的按钮」这条极常见的路径上，板上照旧显示着旧条件，
   送去规划的 current_filters 却已经是空数组，基准句子也退化成框里那半句没搜的话。
   撤销栈的当前帧就是这批条件的出处，它不会被输入事件清掉——用它。 */
function cbFrameData() {
    const frame = (_cbCursor >= 0 && _cbStack[_cbCursor]) || null;
    if (frame && frame.resp) return frame.resp;
    return LAST_RECOMMEND_DATA || null;
}

function cbFrameQuery() {
    const frame = (_cbCursor >= 0 && _cbStack[_cbCursor]) || null;
    if (frame) return String(frame.query || "");
    return cbBaselineQuery(LAST_RECOMMEND_DATA || {});
}

/* 产生当前这批结果的**那句话**。不是输入框里的当前值：
   用户可能已经在框里改了字还没搜；开了关键词审核时，产生结果的是系统改写后的句子。 */
function cbBaselineQuery(data) {
    if (data && data.audit && data.audit.used && data.audit.rewritten_query) return String(data.audit.rewritten_query);
    if (data && data.interpretation && data.interpretation.resolution && data.interpretation.resolution.original_query) {
        return String(data.interpretation.resolution.original_query);
    }
    const input = $("queryInput");
    return input ? String(input.value || "") : "";
}

function cbCoverageDims(data) {
    return ((data && data.coverage_caveats) || []).map(function (c) { return String(c.dim || ""); }).filter(Boolean);
}

function cbRowButtons(row) {
    const bits = [];
    if (row.zone === "query") {
        if (row.editable) bits.push('<button type="button" class="cb-act" data-cb-edit="' + escapeHtml(row.dim) + '">改</button>');
        if (row.lenientable) bits.push('<button type="button" class="cb-act" data-cb-lenient="' + escapeHtml(row.dim) + '">' + escapeHtml(cbLenientLabel(row.dim)) + '</button>');
        if (row.removable) bits.push('<button type="button" class="cb-act" data-cb-remove="' + escapeHtml(row.filter_id) + '">不按这条筛</button>');
    } else if (row.zone === "prefer") {
        // 措辞必须和硬条件区分开：那边是「不按这条筛」，这边根本没筛过，只能是「不按这条排」。
        bits.push('<button type="button" class="cb-act" data-cb-remove="' + escapeHtml(row.filter_id) + '">不按这条排先后</button>');
    } else if (row.zone === "facet") {
        bits.push('<button type="button" class="cb-act" data-cb-unfacet="' + escapeHtml(row.dim) + '">去掉</button>');
    } else if (row.zone === "lenient") {
        bits.push('<button type="button" class="cb-act" data-cb-strict="' + escapeHtml(row.dim) + '">改回严格</button>');
    } else if (row.zone === "suppressed" || row.zone === "prefer_off") {
        bits.push('<button type="button" class="cb-act cb-act-restore" data-cb-restore="' + escapeHtml(row.filter_id) + '">恢复</button>');
    }
    return bits.join("");
}

/* 「另有 N 条没标注，也算符合」——N 直接引用后端这一次给的数字，前端不重算。 */
let _cbCoverageCounts = {};
function cbLenientLabel(dim) {
    const n = _cbCoverageCounts[dim];
    return (typeof n === "number" && n > 0) ? ("另有 " + n + " 条没标注，也算符合") : "没标注的也算符合";
}

export function renderCondBoard(data) {
    const board = $("condBoard");
    if (!board) return;
    if (!data || !data.results) {
        // 没有结果就没有条件可编辑；此时露出面板只会让用户对着空板发呆。
        board.hidden = true;
        swSync();   // 板刚被判空 → 重算侧栏可用性：整卡收起、scope-in-side 撤下、被隐藏的主检索框回来（见下方 abstain 分支同注）
        return;
    }
    // **弃权/澄清态直接收起**：这两种状态下系统压根没有检索，条件板必然是空的，
    // 而空板的兜底文案写的是「这次检索没有用上任何筛选条件，结果是按整句话的相关程度排的」——
    // 那是在描述一次**根本没发生**的检索。屏幕上半截说「这次没有做检索」、下半截说
    // 「结果是按相关程度排的」，用户该信哪句？（注意 `[]` 在 JS 里是**真值**，
    // 上面那道 `!data.results` 拦不住空结果，这也是它此前一直漏出来的原因。）
    const _st = String(data.resolution_status || "");
    if (_st === "abstained" || _st === "clarification_required") {
        board.hidden = true;
        // 这里必须重跑 swSync：本函数在 runRecommend 成功链上排在 placeFacetBar→swSync 之后，
        // 那次 swSync 跑时板还可见（沿用上一次成功检索），于是 scope-in-side 仍开着、主检索框仍被隐藏。
        // 现在板刚判空、却不 swSync，就会留下「只剩对话框、上方主检索框不见、聊天区空白」的死态，
        // 直到下一次切页签/缩放才自愈（隐藏主检索框把这个旧的早退问题从「1px 分隔线」放大成可见死态）。
        swSync();
        return;
    }
    _cbCoverageCounts = {};
    ((data.coverage_caveats) || []).forEach(function (c) { _cbCoverageCounts[String(c.dim || "")] = c.count; });

    const rows = cbRowsFrom(data.query_constraints || [], _facetFilters, _lenientDims, _suppressed, cbCoverageDims(data));
    // 分区顺序即渲染顺序；漏写一个分区的后果是那一区的行**一条都不显示**（静默丢失，
    // 不报错也不留痕）。加分区时这里和 CB_ZONE_TITLE / CB_ZONE_NOTE 必须同批改齐。
    const zones = ["query", "prefer", "facet", "lenient", "suppressed", "prefer_off"];
    let html = "";
    zones.forEach(function (zone) {
        const mine = rows.filter(function (r) { return r.zone === zone; });
        if (!mine.length) return;
        html += '<div class="cb-zone cb-zone-' + zone + '">';
        html += '<div class="cb-zone-head">' + escapeHtml(COPY.boardZones[zone].title) + '</div>';
        mine.forEach(function (row) {
            const values = row.values.filter(Boolean).map(escapeHtml).join("、");
            html += '<div class="cb-row">'
                + '<span class="cb-name">' + escapeHtml(row.label) + "</span>"
                + (values ? '<span class="cb-vals">' + values + "</span>" : "")
                + '<span class="cb-acts">' + cbRowButtons(row) + "</span>"
                + "</div>";
            if (row.values.length > 1) html += '<p class="cb-note">同一行里的几个值，满足其中一个就算符合。</p>';
        });
        html += '<p class="cb-note">' + escapeHtml(COPY.boardZones[zone].note) + "</p>";
        html += "</div>";
    });
    if (!html) html = '<p class="cb-empty">这次检索没有用上任何筛选条件，结果是按整句话的相关程度排的。</p>';

    $("cbRows").innerHTML = html;
    $("cbSummaryText").textContent = cbSummary(rows, typeof data.result_total === "number" ? data.result_total : null);
    cbRenderSteps();
    board.hidden = false;
    cbApplyCollapsed();
    swSync();   // 条件板刚有内容 → 「对话记录」页签可用；落位由 swApplyMode → placeCondBoard 收口
}

/* ============ 侧栏工作卡：同一张卡承载「数据细化 / 对话记录」============
   落位规则与 #facetBar 同源（facets.js placeFacetBar）：侧栏展开且非移动端 → 搬进侧栏；
   否则搬回结果区原位。同一个 DOM 节点在两处间 appendChild 搬家，已绑定的监听全部保留。
   移动端/收起侧栏时回退到原位，保证任何情况下这两块都能被看到、被操作。 */
/* 侧栏工作模式偏好。与 BOARD_COLLAPSED_KEY 同性质：**纯 UI 偏好，不是用户数据**
   （不含查询、不含结果、不含撤销栈），所以允许落盘；但同样必须走 nsKey() 每账户键——
   共用机器上，下一个人不该继承上一个人的界面状态。
   v1→v2：默认值从「数据细化」改为「继续对话」（用户：出检索结果后，
   侧栏应默认展示继续对话窗口）。key 换代＝旧默认时代存下的 "facets" 不再压住新默认
   （否则老用户永远看不到这个改动）；换代后用户再显式点选仍照常持久。 */
var SIDE_MODE_KEY = "biodata_side_mode_v2";
let _swMode = null;      // 当前**显示**中的模式（含可用性自动回退，不等于用户选择）
let _swPicked = false;   // 本会话用户是否**显式**点过页签（自动回退不算选择，不得落盘）

function swAvailable() {
    const board = $("condBoard");
    const groups = $("facetGroups");
    const qview = document.querySelector('.view[data-view="query"]');
    const inResults = !!(qview && qview.classList.contains("has-results"));
    return {
        // 数据细化 pane 的真实内容是「可细化维度」#facetGroups——命中已搬到常驻栏 #swHits（两模式都在）。
        // 只有 facets-active、groups 却为空时，pane 是空的：此时不该启用该页签（否则点开是一片空白）。
        facets: document.body.classList.contains("facets-active") && !!(groups && groups.children.length),
        // cur3：对话记录的可见性不再依赖条件板。三种够格：① 条件板在（有结果的条件）；
        // ② 已进入结果态（has-results——弃权/空结果也算：检索**落地了**，对话就该可见）；
        // ③ 有 sys 回音（clarify/没听懂——不触发检索，再没有别的反馈通道）。
        // 反例（2026-08-02 图4）：首次检索**进行中**只有 say——此时落地页上侧栏工作卡
        // 提前弹出正是用户报的异常；检索中只许按钮滚进度，完成后结果+侧栏一起入场。
        board: !!(board && !board.hidden) || inResults
            || _cbLog.some(function (e) { return e.kind === "sys"; })
    };
}

/* 可用性变了就把开关、卡片可见性、两块面板重新对齐一次。
   单一真源是 swAvailable()：哪一块有内容由它说了算，别处不再各自判断。 */
export function swSync() {
    const card = $("sideWork");
    if (!card) return;
    const has = swAvailable();
    const onQuery = document.body.classList.contains("on-query");
    const inSidebar = !document.body.classList.contains("side-closed") && window.innerWidth > 780;
    // 整张卡：查询视图 + 侧栏可用 + 至少一块有内容；
    // chat-in-main（无结果、对话在主区）时整卡收起——对话界面在主区，这里不重复摆一份（p10）。
    // 视图交换（vs1）无需额外分支：它生效的前提是 has-results，此时 cbChatInMain() 恒假、
    // swAvailable().board 恒真，本卡自然显示（承载搬进侧栏的结果网格）。
    card.hidden = !(onQuery && inSidebar && (has.facets || has.board)) || cbChatInMain();
    // 双窗口（导航卡 + 本工作卡并立）→ 导航卡折叠成两列布局（2026-08-24 用户点图：凡是双窗口，
    // 左上恒两列——此前只在 facets-active【有活跃分面】时两列，无分面的对话窗口态导航傻大单列）。
    // 真源就是本卡可见性（含 onQuery/侧栏展开/桌面断点全部前提），收侧栏/移动端/单窗口自动回单列。
    document.body.classList.toggle("side-duo", !card.hidden);
    const tabF = $("swTabFacets"), tabB = $("swTabBoard");
    if (tabF) tabF.disabled = !has.facets;
    if (tabB) tabB.disabled = !has.board;
    // 模式来源分两类：**用户显式选择**（本会话点过页签，或上个会话存过 localStorage）优先；
    // 否则默认「对话记录」（2026-08-01 出检索结果后聊天是主路径，细化是进阶动作）。
    // 选中的模式若已无内容 → 自动落到另一块显示（而不是让用户对着空面板发呆）——但**自动回退只改
    // 显示、不落盘也不算选择**：否则「先出分面、后出对话」的中间态会把默认值永久顶成 facets
    // （swApplyMode 旧实现逢切必写 localStorage，视觉自查实测默认 board 永远到不了用户）。
    const picked = (_swPicked && _swMode) || localStorage.getItem(nsKey(SIDE_MODE_KEY));
    let mode = picked || "board";
    if (mode === "facets" && !has.facets && has.board) mode = "board";
    if (mode === "board" && !has.board && has.facets) mode = "facets";
    swApplyMode(mode, has);
    placeHitsBar();   // 常驻「查询条件」栏（=facets 的 #facetActive）随可用性重新落位（两模式都在）
}

/* opts.picked=true 只来自页签点击/键盘快捷键（显式选择）：记 _swPicked 并落盘持久。
   swSync 的自动路径不传 → 只更新显示状态（_swMode 供 placeScopeControls 读），不碰持久偏好。 */
function swApplyMode(mode, has, opts) {
    has = has || swAvailable();
    _swMode = mode;
    const cardEl = $("sideWork");
    if (cardEl) cardEl.dataset.swMode = mode;   // vs3：当前页签标识——交换态「检索结果」页签纯 CSS 隐藏 #swHits 用（只读显示态，不落盘）
    if (opts && opts.picked) {
        _swPicked = true;
        try { localStorage.setItem(nsKey(SIDE_MODE_KEY), mode); } catch (_e) {}
    }
    const paneF = $("sideFacets"), paneB = $("sideBoardPane");
    const tabF = $("swTabFacets"), tabB = $("swTabBoard");
    const showF = mode === "facets" && has.facets;
    const showB = mode === "board" && has.board;
    if (paneF) paneF.hidden = !showF;
    if (paneB) paneB.hidden = !showB;
    if (tabF) { tabF.classList.toggle("is-on", mode === "facets"); tabF.setAttribute("aria-selected", String(mode === "facets")); }
    if (tabB) { tabB.classList.toggle("is-on", mode === "board"); tabB.setAttribute("aria-selected", String(mode === "board")); }
    // 页签滑动指示块（Phase D 动效）：滑块跟激活页签走，弹簧缓动；reduced-motion 时 transition 已被 CSS 关。
    const glide = $("swGlide");
    if (glide) glide.style.transform = mode === "board" ? "translateX(calc(100% + 3px))" : "translateX(0)";
    placeCondBoard();
    placeScopeControls();   // 范围控件（来源/时间）随模式在「主控制台 ↔ 对话记录面板」间搬家
    placeChatLog();         // 对话记录随模式在「条件板内（telegram）↔ 主区静态家（hero）」间搬家
    placeChatSuite();       // 视图交换（vs1）：对话套件 ↔ 结果网格 换位搬家，同一条链收口
}
/* 细化「提交」后把侧栏切到「继续对话」pane：刚上屏的用户消息与即将到来的系统回复（进度泡→完成摘要）
   都在那边——不切换的话用户在「细化筛选」页签里什么都看不见（圈1 的另一半）。
   只是显示切换：不算用户显式选择、不落盘，下次 swSync 仍按持久偏好回默认。 */
export function swShowBoard() { swApplyMode("board", null); }

/* 条件板落位。注意 #sideFacets 的可见性由 facets.js 的 syncFacetsCard 用 GSAP autoAlpha 控制，
   本函数只管 #condBoard 这一个节点搬到哪儿，不去抢那套动画的控制权。 */
function placeCondBoard() {
    const board = $("condBoard");
    if (!board) return;
    // 落进「对话记录」面板的**可滚动**内层 #sideBoardScroll（不是 #sideBoardPane 本身）：
    // 范围合并弹层 #scopePop 挂在输入条内、滚动区之外，其面板才不被 overflow-y 裁掉（见 index.html 注释）。
    const pane = $("sideBoardScroll") || $("sideBoardPane");
    const inSidebar = !document.body.classList.contains("side-closed")
        && window.innerWidth > 780
        && document.body.classList.contains("on-query");
    if (inSidebar && pane) {
        if (board.parentElement !== pane) pane.appendChild(board);
        board.classList.add("cb-in-side");
    } else {
        const host = $("heroInner") || $("hero") || document.querySelector(".hero");
        const anchor = document.querySelector(".hero .chips");
        if (host && board.parentElement !== host) {
            if (anchor && anchor.parentElement === host) host.insertBefore(board, anchor);
            else host.appendChild(board);
        }
        board.classList.remove("cb-in-side");
    }
}

/* ============ 常驻「查询条件」栏（= facets 的 #facetActive，用户圈的那一栏）============
   不另造栏：把 facets.js 渲染的 #facetActive（含 忽略/× chip）整块搬到切换开关下方的 #swHits，
   两模式都在、切换不消失。它就是对话「上文」——手动细化后 facets.js 立即重建它、这里同步反映（同一 DOM 节点）。
   侧栏收起/移动端/离开查询视图 → 搬回 #facetBar（结果区上方，#facetGroups 之前）。#facetActive 由 facets.js 按 id
   重建，与它落在哪儿无关；无命中/细化时 facets.js 把它 hidden → 常驻栏一并不占位。 */
function placeHitsBar() {
    const hits = $("facetActive");
    if (!hits) return;
    const host = $("swHits"), bar = $("facetBar"), groups = $("facetGroups");
    const inSidebar = !document.body.classList.contains("side-closed")
        && window.innerWidth > 780
        && document.body.classList.contains("on-query")
        && !hits.hidden;
    if (inSidebar && host) {
        if (hits.parentElement !== host) host.appendChild(hits);
        host.hidden = false;
    } else {
        if (bar && hits.parentElement !== bar) {   // 搬回 #facetBar 顶部（#facetGroups 之前，还原原始顺序）
            if (groups && groups.parentElement === bar) bar.insertBefore(hits, groups);
            else bar.insertBefore(hits, bar.firstChild);
        }
        if (host) host.hidden = true;   // hits 已移出 → host 自然为空
    }
}

/* ============ 聊天 + 细化记录 ============
   本轮检索里说过的话（say）与做过的细化（refine）按发生顺序留痕。**本文件自己不落盘**：
   撤销栈里是完整的后端响应，落盘会撑爆配额（test_board_only_persists 钉死本文件只许存 UI 偏好）。

   2026-07-29 起，对话记录会随**历史记录**一起留存 —— 但写盘的是 core.js 的 `pushHist`，
   不是这里。这不是绕开上面那条规则，而是因为历史本来就已经在存「查询原话 + 完整结果快照」，
   走的是同一个 per-account `nsKey` 命名空间、同一套配额降级、同一个「清空历史」按钮；
   对话记录（几十条短句）与它同性质、同生命周期，不构成新的数据面。 */
const CB_LOG_MAX = 40;
let _cbLog = [];
/* ---------- 信息流 · 过程轨迹（2026-08 用户重申定稿：上工具行/中唯一气泡/下 pill）----------
   _flow：本轮的工具行记录（有序、按行 id 去重——upsertStage 状态机：同一次调用的 tool_start 亮
   pending、step 落 done/failed 只更新同一行，绝不 append 两行）。
   _flowDone：本轮输出是否已结束——结束后压缩（_flowCompressed）。
   结构纠偏：
   ① 压缩快照不再全局渲染在对话流尾部，而是**挂到本轮回执 entry 上**（_flowAttach 持件、
      cbLogPush("sys") 领取）——轨迹块渲染在气泡**上方**（顺序：工具行在上、唯一气泡在中），
      且历史轮各自保留压缩摘要（不再随 flowReset 消失）；
   ② 结果 pill 同理（_flowPills 持件、同一颗回执领取）渲染在气泡**下方**；
   ③ 唯一气泡规则：_execReceiptCovered 标记本轮检索回执已负责「最终总结」，
      act.js 的 actFinish 对纯检索计划据此抑制自己的第二颗气泡。
   只管结构与数据（DOM 类名 `.ft-*` 是样式钩子）；视觉归 app.css。 */
let _flow = [];
let _flowDone = false;
let _flowCompressed = null;   // compressFlow 结果 {summaryText, kept, expanded}
/* 视觉层：压缩过渡动画的两帧余量计数——flowFinish 置 2、渲染每帧减一。
   为什么不是「渲染即消费」的一帧：flowFinish 的压缩渲染与紧随其后的回执 cbLogPush 渲染
   同步连发，浏览器只绘制最后一帧——动画类必须活到回执那一帧才能被真正看见（真机核验发现
   一帧版动画被吞，从未显示）；第三帧起摘除，不重播。 */
let _flowAnimArmed = 0;
/* 待领取的压缩快照与结果 pill（flowFinish/_applyBatchDecision 置，下一颗 sys 回执领）。 */
let _flowAttach = null;    // {summaryText, expanded} —— 渲染在回执气泡上方
let _flowPills = null;     // [{batchId, label, count, active}] —— 渲染在回执气泡下方
/* 本轮「唯一气泡」已被批次回执接管（纯检索计划时 actFinish 据此闭嘴）。 */
let _execReceiptCovered = false;
export function cbExecReceiptCovered() { return _execReceiptCovered; }
export function flowPushEvent(evKind, data) {
    const st = stageFromEvent(evKind, data);
    if (st) { _flow = upsertStage(_flow, st); _flowCompressed = null; cbRenderHistory(); }
}
export function flowPushStage(stage) {
    if (stage) { _flow = upsertStage(_flow, stage); _flowCompressed = null; cbRenderHistory(); }
}
export function flowFinish(recordsOverride) {
    _flowDone = true;
    _flowCompressed = compressFlow(recordsOverride || _flow);
    if (_flowCompressed && _flowCompressed.summaryText) {
        _flowAnimArmed = 2;
        _flowAttach = { summaryText: _flowCompressed.summaryText, expanded: _flowCompressed.expanded || [] };
    }
}
/* 结果 pill 持件（落地/回执处把本轮存活批做成 pill；下一颗 sys 回执领取）。
   2026-08-31（用户定「pill 与工具执行绑定」）按族分治：
   - 入件里的下载 pill（dlq）：**追加**——每次下载工具执行都给回执气泡添一颗，
     已持件的检索 pill 与早前下载 pill 原样保留（混合轮先检索后下载，两族同挂一颗泡）；
   - 入件里的检索结果 pill：**同族顶替**（同轮重落地/rerank 只留最新一组），下载 pill 不动；
   序列归一：检索 pill 恒在前、下载 pill 恒在后。空入件不清场（清场只归 flowReset）。 */
export function flowSetPills(pills) {
    const incoming = (Array.isArray(pills) ? pills : []).filter(Boolean);
    if (!incoming.length) return;
    const inDlq = incoming.filter(function (p) { return p.dlq; });
    const inResult = incoming.filter(function (p) { return !p.dlq; });
    if (inDlq.length) _flowPills = (_flowPills || []).concat(inDlq);
    if (inResult.length) {
        const stagedDlq = (_flowPills || []).filter(function (p) { return p && p.dlq; });
        _flowPills = inResult.concat(stagedDlq);
    }
}
export function flowReset() {
    _flow = [];
    _flowDone = false;
    _flowCompressed = null;
    _flowAnimArmed = 0;
    _flowAttach = null;
    _flowPills = null;
    _execReceiptCovered = false;
}
/* 渲染侧取**在途**轨迹的展开快照（cbRenderHistory 拼接用）；本轮已结束→返回空（压缩摘要
   已随 _flowAttach 挂到回执 entry 上，不再全局渲染）。 */
export function flowTraceSnapshot() {
    if (_flowDone) return { done: true, stages: [] };
    return { done: false, stages: renderableStages(_flow) };
}
/* 新消息入场动效（Phase D）：cbLogPush 置位、cbRenderHistory 消费一次——回放/重画（cbReplay/
   快照回看）不置位，于是「滑上来」只属于真新消息；渲染即消费也保证后续重画不重播。 */
let _cbEnterPending = false;
/* 一条「对话」的 id。边界就是 search.js 里那句 `if (!keep) { cbLogClear(); cbLogPush("say", query); }`——
   用户**亲手发起一次新查询**＝开一条新对话；分面细化 / 放宽 / 说一句话改条件都算同一条往下走。
   所以只要把生成点挂在 cbLogClear 上，边界定义就只有一处、不会与实际行为漂开。 */
let _cbConvId = "";
export function cbConvId() {
    if (!_cbConvId) _cbConvId = "c" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 6);
    return _cbConvId;
}
/* 供 core.js 的 pushHist 取用：把这一刻的对话记录压成能落盘的最小形状（kind/text/note，**不含 frameId**——
   帧号是内存里的东西，回看时按每轮的累计长度差重新归属）。
   2026-08-28 msgfb 起多存两个可选字段：i=消息 id（逐条反馈埋点的关联锚，回看恢复时原样带回）、
   f=赞/倒赞态（""|"up"|"down"，空串省略不写）。旧历史行没有这两个字段，恢复路径按缺省处理。 */
export function cbLogForHistory() {
    return _cbLog.map(function (e) {
        const row = { k: e.kind, t: e.text, n: e.note || "", i: e.id || "" };
        if (e.msgFb) row.f = e.msgFb;
        return row;
    });
}
/* 逐条反馈（msgfb）：每条消息一个会话内唯一 id。回看恢复时旧 id 原样带回（见 cbRestoreConversation），
   并用本计数器兜底生成与避让碰撞。 */
let _cbMsgSeq = 0;
function _cbMsgIdNext() { return "m" + (++_cbMsgSeq); }
function _cbMsgIdSeen(id) {
    const m = /^m(\d+)$/.exec(String(id || ""));
    if (m && Number(m[1]) > _cbMsgSeq) _cbMsgSeq = Number(m[1]);
}
export function cbLogPush(kind, text, opts) {
    const t = String(text || "").trim();
    if (!t) return;
    // sys＝系统回音（路由回音 / 取消回音 / clarify 回音）：左对齐灰气泡，不带「查看结果/回退」按钮。
    const k = (kind === "say" || kind === "sys") ? kind : "refine";
    // 进度泡：系统的第一句真话落地＝进度泡使命完成——同一颗泡原位蜕变成这句文字（不另来一颗）。
    if (k === "sys" && _cbProg) { _cbProg = false; _cbProgHint = ""; _cbProgLabel = ""; _cbMorphPending = true; }
    const last = _cbLog[_cbLog.length - 1];
    if (last && last.kind === k && last.text === t) return;   // 连续同条去重（同一细化被重复触发时不刷屏）
    // frameId 先留空：这条消息属于**哪一次检索结果**，要等这一步真的检索落地、cbPushCurrent 推出新帧时才回填。
    // 例外是 sys：它不触发检索，永远不会等到回填——就地挂到当前帧上（没有帧就 null：无按钮、随剪枝消失）。
    // note：仅执行类（action）消息用，存「已执行注记」（如「已打开下载面板」）；其余恒空。
    // needsAgent：仅 sys 用（2026-08-03 降级气泡）——「AI 执行」关时规则检出操作指令，
    // 该回音渲染成带「去开启 AI 执行」指路按钮的美观气泡（样式见 app.css .cbh-agent-*）。
    // isError：仅 sys 用——传输/服务异常回音（非「没听懂」）渲染成错误样式气泡
    // 与正常灰泡一眼可分。两标都不落盘（cbLogForHistory 只存 k/t/n，回看降级为纯文字——同 needsAgent 口径）。
    // html：仅 sys 用（p10 执行总结），存可信 HTML（没做到的紧凑行/纠错 chips/结果卡/过程 details；
    // 动态值一律 escapeHtml 后拼接）。**不落盘**——cbLogForHistory 只存 k/t/n，历史回看降级为纯文字。
    // llmTag：该 sys 的正文已被 LLM 原位改写（/api/act/summary 执行总结，或 /api/search/reply
    // 检索回执——显示「AI 总结」小标）。
    const entry = { kind: k, text: t, frameId: null, note: "", needsAgent: !!(opts && opts.needsAgent),
        isError: !!(opts && opts.isError),
        html: String((opts && opts.html) || ""),
        llmTag: false,
        id: _cbMsgIdNext(),   // msgfb：逐条反馈（赞/倒赞/评论/分支）的关联锚，随历史落盘（cbLogForHistory 的 i 字段）
        msgFb: "" };          // ""|"up"|"down"——赞/倒赞互斥三态，落盘为 f 字段（空串省略）
    if (k === "sys") {
        entry.frameId = (_cbCursor >= 0 && _cbStack[_cbCursor]) ? _cbStack[_cbCursor].id : null;
        /* 领取本轮压缩快照与结果 pill——轨迹块渲染在这颗回执气泡**上方**、pill 在下方，
           历史轮各自保留（不随 flowReset 消失）。一颗回执领一份，领完即止。 */
        if (_flowAttach) { entry.flow = _flowAttach; _flowAttach = null; }
        if (_flowPills) { entry.pills = _flowPills; _flowPills = null; }
    }
    _cbLog.push(entry);
    while (_cbLog.length > CB_LOG_MAX) _cbLog.shift();
    _cbEnterPending = true;   // 新消息入场动效（Phase D）：下一次渲染给最后一条 turn 播 cbhIn（只播一次；后续重画不重播）
    cbRenderHistory();
    // 聊天内容的有无决定「对话记录」页签是否可用（swAvailable.board 读 _cbLog）——
    // 首次回音（如 clarify）也得让侧栏工作卡立刻出现，否则回音写在了一张看不见的卡里。
    swSync();
    return entry;
}
/* LLM 总结落地（p10）：原位改写一条还在对话流里的 sys 条目（被剪枝/清空则 no-op，静默 fail-open）。 */
export function cbUpdateEntry(entry, opts) {
    if (!entry || _cbLog.indexOf(entry) < 0) return;
    if (opts && typeof opts.text === "string" && opts.text.trim()) entry.text = opts.text;
    if (opts && opts.llmTag) entry.llmTag = true;
    cbRenderHistory();
}
/* 清空对话记录 ＝ 一条对话到此为止。conv id 一并作废，下次 cbConvId() 现开一个新的 →
   历史里这两段就分成两行，而不是黏成一条越滚越长的对话。 */
export function cbLogClear() { _cbLog = []; _cbConvId = ""; cbRenderHistory(); swSync(); }

/* A2：纯工具对话（一句检索都没跑过）从不触发 pushHist——被丢弃前先补一条
   「仅对话」历史行，否则强制新开对话（重击智能查询）时这段对话永久丢失。
   归档判据：有对话内容 ∧ 没有任何检索帧（_cbStack 非空 = 每轮检索落地时 pushHist 已归档）。
   excludeQuery＝调用方紧接着要自己发起的那句检索：记录里若只有这一句（hero 首句经
   ubSubmit 刚上屏的 say），它不是「被丢弃的对话」，而是这次检索自己的开场白，不归档。 */
export function cbArchiveChatOnly(excludeQuery) {
    if (_cbStack.length || !_cbLog.length) return;
    const ex = String(excludeQuery || "").trim();
    if (_cbLog.length === 1 && _cbLog[0].kind === "say" && ex && _cbLog[0].text === ex) return;
    pushHistChatOnly();
}

/* ---------- 进度泡：发送后立即出现的那颗系统回复气泡 ----------
   继续对话发一句 → 用户气泡下方**立刻**浮出一颗系统气泡：三点跳动（数字百分比撤下）+ 流式档
   的不确定态文案。系统的真实回复落地时它在**原位渐变成文字**（cbh-morph）：检索落地→完成摘要
   （cbPushCurrent）；回音→那句回音（cbLogPush sys 天然吸收）；执行注记/选项预览本身就是回复→静默撤下
   （cbProgressDrop）。检索失败也要如实收尾成文字（search.js catch）。
   它是**状态**而不是日志条目：不进 _cbLog（不落盘、不参与帧剪枝/回填），新检索清对话（cbLogClear）
   也清不掉它——那句新检索的完成摘要还得靠它蜕变。同一时刻至多一颗（ubSubmit 在途闸保证）。 */
let _cbProg = false;
/* 下一颗系统气泡播「蜕变」动效而非「新消息滑入」：进度泡 → 文字是同一颗泡的变化，不是新来一条消息。 */
let _cbMorphPending = false;
/* 完成摘要的前缀（cbCommit 的 plan.message：「已经不按『物种』筛了」）——比干巴巴的计数更像一句回复。 */
let _cbProgHint = "";
/* 非空 = 不确定态文案（流式规划的「规划中…」）——取代数字百分比。
   流式路径刻意不跑 startProgress 假进度：规划步骤随 SSE 实时上屏，数字是编的，步骤是真的。
   起非流式档的数字 % 也撤了——进度泡只剩三点动画，label 只在流式档出现。 */
let _cbProgLabel = "";
/* 思考指示的入场动效一次性旗标（cbProgressBegin 置位、渲染拼串时消费）——
   不走 _cbEnterPending（那会命中「最后一条真消息」；思考行有自己的 CSS 入场），
   且流式期间每次重画都会重建 DOM，必须「渲染即消费」才不会反复重播。 */
let _cbProgEnter = false;
export function cbProgressBegin(label) {
    if (_cbProg) return;   // 幂等：chat 发送在 ubSubmit 已开，runRecommend 不会再开第二颗
    _cbProg = true;
    _cbProgLabel = String(label || "");
    _cbProgHint = "";
    _cbProgEnter = true;   // 思考行入场（fade-rise 一次；后续重画不重播）
    cbRenderHistory();
}
export function cbProgressDone(text) {
    if (!_cbProg) return false;   // 无进度泡 → no-op（防御；改条件重跑自 2026-08-04 起也开泡——用户动作上屏就必须有回复）
    // 前缀自带句读（plan.message 多以「。」收尾）——剥掉再拼，不写「Mouse。；没有完全…」这种双重标点。
    const hint = _cbProgHint.replace(/[。；;\s]+$/, ""); _cbProgHint = "";
    // 返回那颗回执 entry（cbLogPush 的返回值；去重/空串时 undefined → 调用方跳过 LLM 改写），
    // 供 cbFetchSearchReply 原位改写；旧调用方只当真/假值用，行为不变。
    return cbLogPush("sys", hint ? (hint + "；" + text) : text) || false;   // sys push 天然吸收进度泡、置蜕变旗标
}
export function cbProgressDrop() {
    if (!_cbProg) return;
    _cbProg = false;
    _cbProgHint = "";
    _cbProgLabel = "";
    cbRenderHistory();   // 静默撤下：这句的回复以别的形式出现（执行注记泡 / 选项预览卡），不重复回
}
/* 不确定态文案换句（流式规划：规划落地→进入检索时把「规划中…」换成「检索中…」；
preliminary 帧落地后换成「正在更深一步思考…」——search.js 共享落地入口也调它，故导出）。
   只在流式路径起作用（非流式 _cbProgLabel 恒空，进度泡只有三点）。 */
export function cbProgressRelabel(text) {
    if (!_cbProg || !_cbProgLabel) return;
    _cbProgLabel = String(text || "");
    cbRenderHistory();
}

/* ---------- 检索回执的 LLM 改写 ----------
   产品原则：系统回复气泡 = LLM 给出的 final answer。检索落地时 cbPushCurrent 先上确定性
   事实句（数字全部取自真实响应），随后异步请 /api/search/reply 把它改写成 1–2 句自然中文——
   成功则原位替换正文并挂「AI 总结」标；任何不成（mock/无 key/网络/后端判否）都静默 fail-open，
   事实句本来就已经在泡上（且不挂标——归因诚实，与 act 总结同一纪律）。
   刻意**不加请求代号守卫**：每轮回执是独立 entry，晚到的旧回包改写的仍是它那一轮的泡，
   内容依然正确（与 act.js _sumSeq 的「只改最新一颗总结泡」语义不同——那边一颗泡位复用）。 */
function _searchReplyFacts(data, query, popts, total, shown, hint) {
    // 事实包：utterance=用户原话（sentText 优先——hero 首句被改写时 query 已是改写句）；
    // note=进度泡前缀（「我把这句按『X』检索」/改条件留痕，可能为空）；
    // canSuggest=下一步建议白名单（后端 prompt 硬约束：只许从中原样挑一条，空=禁止建议）。
    const rs = String((data && data.resolution_status) || "");
    const hasRelax = (((data && data.relaxation_options) || []).length > 0) || !!(data && data.degraded_search);
    const seen = Object.create(null);
    const keywords = [];
    ((data && data.query_constraints) || []).forEach(function (c) {
        if (!c || typeof c !== "object") return;
        if (String(c.polarity || "include") !== "include") return;   // 只报正向硬条件（同 flowHitKeywords 口径）
        (c.values || []).forEach(function (v) {
            const s = String(v || "").trim();
            if (!s || seen[s] || keywords.length >= 30) return;
            seen[s] = true;
            keywords.push(s.slice(0, 100));
        });
    });
    const suggest = [];
    if (total > 0) {
        suggest.push("继续说一句话细化条件（比如换物种、平台、疾病）");
        if (actEnabled()) suggest.push("说「打包下载」把当前结果打成任务包");   // 无执行开关时不指路进降级泡
    } else if (hasRelax) {
        suggest.push("点结果区给出的放宽方式");
    }
    return {
        utterance: String((popts && popts.sentText) || query || ""),
        query: String(query || ""),
        note: String(hint || "").replace(/[。；;\s]+$/u, ""),
        total: total, shown: shown,
        hitKeywords: keywords,
        resolutionStatus: rs,
        hasRelax: hasRelax,
        canSuggest: suggest,
    };
}
/* 公共事实构造器（补网）：除 cbPushCurrent 主路径外，回执还会从另外几处落地——
   _applyBatchDecision 的采纳留痕（_aNote）/去重与备选如实回执（decision.sysText）/
   preliminary_final b 档收尾、act.js 混合轮边界收尾。这些站点手上只有**响应对象**
   （recommend 响应或结果批 payload，二者同形），没有 cbPushCurrent 的局部 _total/_shown；
   数字口径与 cbPushCurrent 完全一致（result_total 优先、缺省回落 results.length）。
   utterance 一律传用户原话（不是检索句），note 传该站点自己的确定性披露句（可空）。 */
export function cbSearchReplyFacts(data, utterance, query, note) {
    const d = (data && typeof data === "object") ? data : {};
    const total = Number(d.result_total) || (Array.isArray(d.results) ? d.results.length : 0);
    const shown = Array.isArray(d.results) ? d.results.length : 0;
    return _searchReplyFacts(d, query, { sentText: utterance }, total, shown, note);
}
/* 按 batch_id 取结果批（_applyBatchDecision 各分支的事实源）；找不到返回 null（调用方跳过改写）。 */
function _batchById(batches, id) {
    if (!Array.isArray(batches)) return null;
    const sid = String(id || "");
    for (let i = 0; i < batches.length; i++) {
        if (batches[i] && String(batches[i].batch_id || "") === sid) return batches[i];
    }
    return null;
}
export function cbFetchSearchReply(entry, facts) {
    if (!entry || !facts) return;
    const cfg = getConfig();
    if (cfg.provider === "mock") return;   // 结构性不调（后端同判否）：省一次注定无果的往返
    fetch(API.searchReply, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            utterance: String(facts.utterance || ""), query: String(facts.query || ""),
            note: String(facts.note || ""),
            total: Number(facts.total) || 0, shown: Number(facts.shown) || 0,
            hit_keywords: facts.hitKeywords, resolution_status: facts.resolutionStatus,
            has_relax: !!facts.hasRelax, can_suggest: facts.canSuggest,
            provider: cfg.provider, use_llm: true, mock_llm: false,
            api_key: cfg.api_key, base_url: cfg.base_url, model: cfg.model,
        }),
    }).then(function (res) { return res.json(); }).then(function (d) {
        if (d && d.ok && d.reply_zh) cbUpdateEntry(entry, { text: String(d.reply_zh), llmTag: true });
    }).catch(function () { /* fail-open：事实句留存 */ });
}
/* ---------- 每条消息挂到它对应的那次检索结果（查看历史回复 → 分支 / 回退至此）---------- */
function cbFrameIndexById(id) {
    if (id == null) return -1;
    for (let i = 0; i < _cbStack.length; i++) if (_cbStack[i] && _cbStack[i].id === id) return i;
    return -1;
}
/* 查看历史回复（此前叫「查看结果」）：跳到该消息对应的那一帧、把当时的结果画回来，但**不截断**——
   栈还在，点最新的「查看历史回复」或「再往后一步」都能回到最新。（在历史位置上不许直接继续说：
   输入条已变成 分支/回退 双键，见 cbComposerSync。） */
function cbViewFrame(id) {
    const idx = cbFrameIndexById(id);
    if (idx < 0) return;
    _cbCursor = idx;
    cbReplay(_cbStack[idx]);   // cbReplay 末尾重画聊天记录（高亮随游标走）——回到上一步/查看/回退共用同一条重画
}
/* 回退至此：跳到该消息对应的那一帧，并**丢弃其后的所有帧与消息**（用户明确要的破坏性回退）。 */
function cbRevertToFrame(id) {
    const idx = cbFrameIndexById(id);
    if (idx < 0) return;
    _cbStack = _cbStack.slice(0, idx + 1);
    _cbCursor = idx;
    // 丢弃该帧之后的对话/细化记录：只保留 frameId 仍在存活栈里的消息（含无 frameId 的在途项一并清掉）。
    const alive = {};
    _cbStack.forEach(function (f) { if (f && f.id != null) alive[f.id] = true; });
    _cbLog = _cbLog.filter(function (e) { return e.frameId != null && alive[e.frameId]; });
    cbReplay(_cbStack[idx]);   // 截断后再回放：cbReplay 末尾重画已按截断后的 _cbLog
}
function cbHistoryClick(event) {
    // msgfb：逐条反馈操作条（赞/倒赞/评论/分支 + 评论编辑器的发送/取消）。
    const actBtn = event.target.closest("[data-cbh-up],[data-cbh-down],[data-cbh-comment],[data-cbh-fork],[data-cbh-cmt-send],[data-cbh-cmt-cancel]");
    if (actBtn) {
        const _mid = actBtn.getAttribute("data-cbh-up") || actBtn.getAttribute("data-cbh-down")
            || actBtn.getAttribute("data-cbh-comment") || actBtn.getAttribute("data-cbh-fork")
            || actBtn.getAttribute("data-cbh-cmt-send") || actBtn.getAttribute("data-cbh-cmt-cancel") || "";
        const _entry = _cbEntryById(_mid);
        if (actBtn.hasAttribute("data-cbh-up")) _cbMsgFbToggle(_entry, "up");
        else if (actBtn.hasAttribute("data-cbh-down")) _cbMsgFbToggle(_entry, "down");
        else if (actBtn.hasAttribute("data-cbh-comment")) _cbMsgCommentToggle(_entry);
        else if (actBtn.hasAttribute("data-cbh-fork")) _cbMsgFork(_entry);
        else if (actBtn.hasAttribute("data-cbh-cmt-send")) { _cbMsgCommentSend(_entry); }
        else if (actBtn.hasAttribute("data-cbh-cmt-cancel")) _cbMsgCommentCancel();
        return;
    }
    /* 2026-08-30 下载批 pill（气泡内 data-dlq-pill）——点击 = 打开下载面板并滚进视野。
       与 act.js data-act-fix="panel" chip 同一套开门动作（unhide + previewTaskPack 重渲——
       重渲会把 #tpDlZone 重建后 dlqRender 复活，队列记录仍在 downloads.js 内存里，不丢）。 */
    const dlqPill = event.target.closest("[data-dlq-pill]");
    if (dlqPill) {
        const panel = $("taskPackPanel");
        if (panel) {
            panel.hidden = false;
            /* 滚到队列本身而不是面板顶——pill 的语义是「看这批下载」，面板顶是勾选清单。
               previewTaskPack 异步重渲（渲完 dlqRender 复活 #tpDlZone 队列区），落地后再滚；
               队列区空（历史回看的陈旧 pill / 队列已清空）或预览失败 → 退回滚到面板顶。 */
            Promise.resolve(previewTaskPack()).then(function () {
                const zone = $("tpDlZone");
                const target = (zone && zone.firstChild) ? zone : panel;
                if (target.scrollIntoView) target.scrollIntoView({ block: "nearest" });
            }, function () {
                if (panel.scrollIntoView) panel.scrollIntoView({ block: "nearest" });
            });
        }
        return;
    }
    // 结果 pill（气泡下方）——点击切换到对应结果批（switchBatch 换结果区显示；
    // 会话不动）。legacy 单批 pill 无批 id → 回退到该 pill 所属帧（与「查看历史回复」同径）。
    const pill = event.target.closest("[data-ft-pill]");
    if (pill) {
        const bid = String(pill.getAttribute("data-ft-pill") || "");
        // 零命中 pill —— 不再切换结果批。仅当它是最新结果（最后一个回执 entry 的活跃批）
        // 时点击 = 显式重开放松选择卡（清该批 dismiss 记忆）；否则（已被更新的结果取代）点击无任何反应。
        if (pill.getAttribute("data-ft-zero") === "1") {
            if (pill.getAttribute("data-ft-zero-latest") === "1") {
                _relaxDismissed.delete(bid);   // 显式重开：清该批 dismiss 记忆
                _autoRelaxCardOpen(bid);
            }
            return;
        }
        if (bid) {
            switchBatch(bid);
            // pill 活跃态随结果区同步（switchBatch 只重渲结果区，对话流 pill 的
            // is-on 原样留着 → 结果区已换批、对话流还亮着旧批，状态打架）。
            _cbLog.forEach(function (e) {
                if (!e || !Array.isArray(e.pills)) return;
                e.pills.forEach(function (p) { p.active = String(p.batchId || "") === bid; });
            });
            cbRenderHistory();
            return;
        }
        const fid = pill.getAttribute("data-ft-frame");
        if (fid != null && fid !== "") cbViewFrame(Number(fid));
        return;
    }
    // 降级气泡的指路按钮（「AI 执行」关时规则检出操作指令的那颗泡）：打开设置，直达开关。
    // C1直达兑现：无 key（llmCapable 假）→ 展开「AI / API 配置」并滚到；
    // 有 key → 滚到「AI 执行」开关并短暂高亮——此前只打开抽屉顶部，开关在屏外，「直达」是谎。
    const settingsBtn = event.target.closest("[data-cbh-settings]");
    if (settingsBtn) {
        openSettings();
        if (llmCapable()) revealSetting("nodeAgentExec", false);
        else revealSetting("apiConfig", true);
        return;
    }
    // 「查看历史回复」气泡按钮：只在结果区展示那一帧的历史结果、不动会话；
    // 之后要不要分支/回退，由输入条变形成的双键决定（#cbForkBar）。
    const view = event.target.closest("[data-cbh-view]");
    if (!view) return;
    const keyboard = event.detail === 0;   // Enter/Space 触发的点击 detail=0：只有键盘用户需要救回焦点，鼠标不动它
    const fid = Number(view.getAttribute("data-cbh-view"));
    cbViewFrame(fid);
    // cbRenderHistory 重建了整个 #cbHistory.innerHTML，刚被点的那颗按钮已被销毁 → 焦点掉回 <body>。
    // 键盘用户的下一站就是输入条变形来的双键（分支/回退），焦点落过去；鼠标不动。
    if (!keyboard) return;
    const fork = $("cbForkBar");
    const branchBtn = $("cbBranchBtn");
    if (fork && !fork.hidden && branchBtn) { branchBtn.focus(); return; }
    const back = $("cbHistory") && $("cbHistory").querySelector('[data-cbh-view="' + fid + '"]');
    (back || $("queryInput")).focus();
}

/* ============ 输入条变形：查看历史回复 → 回到最新 / 分支 / 回退============
   游标不在栈顶时（查看历史回复 / 历史浮窗找回），#chatComposer 里的 .cb-bar 整根隐藏、
   #cbForkBar 三键上场——在历史位置上**不许直接继续说**（那会把栈顶之后的对话静默剪掉，
   与 cbPushFrame 的剪枝语义撞车又不说清）：「回到最新」回栈顶；「从这里建立分支」
   **新开浏览器标签页**、以该帧为起点另起会话（本标签页原样保留）；「回退至此」剪掉
   落后的对话（不可撤销、二段确认）。回栈顶输入条自动复原。
   唯一同步口：cbRenderHistory 每次重画都调 cbComposerSync。 */
function cbComposerSync() {
    const composer = $("chatComposer");
    if (!composer) return;
    const bar = composer.querySelector(".cb-bar");
    const fork = $("cbForkBar");
    const viewing = _cbCursor >= 0 && _cbCursor < _cbStack.length - 1;
    if (bar) bar.hidden = viewing;
    if (fork) fork.hidden = !viewing;
}

function cbBranchFromHere() {
    const cur = _cbStack[_cbCursor];
    if (!cur) return;
    // 分支＝**新开浏览器标签页**——本标签页的原会话一帧不动；
    // 新标签页从 ?fork=<convId>:<N> 读出前 N 轮历史（每轮都带完整快照），重建到这一帧为止，
    // 并换上一个新 convId（cbAdoptAsBranch）——此后两边的历史记录分成两条对话。
    const conv = cbConvId();
    const url = location.pathname + "?fork=" + encodeURIComponent(conv + ":" + (_cbCursor + 1));
    if (openPopup(url)) toast("已在新标签页打开分支；本标签页保持不变");
}

/* 新标签页分支落点（?fork=，browse.js initHistWin 调用）：会话已按历史前缀重建好，
   这里换一个新 convId —— 之后这条分支的检索按新 id 落历史，与原对话分成两条（互不串行）。 */
export function cbAdoptAsBranch() {
    _cbConvId = "";
    cbLogPush("sys", "这是从原对话分出来的一支；原对话还在原标签页和历史记录里。");
}

function cbRevertDisarm() {
    const b = $("cbRevertBtn");
    resetTwoStepConfirm(b, "回退至此");
}
function cbRevertHere() {
    const cur = _cbStack[_cbCursor];
    if (!cur) return;
    const b = $("cbRevertBtn");
    // 不可撤销的破坏性操作：二段确认（与 histClear/hist-del 同一 armed 模式，3 秒不回就复位）。
    if (!armTwoStepConfirm(b, { idleText: "回退至此", confirmText: "再点一次确认回退（不可撤销）" })) return;
    usageLog(USAGE_KINDS.undo, { frame: String(cur.id || ""), q: String(cur.query || "").slice(0, 80) });   // 回退是「上一步白干了」的直接信号（v2 起记）；补最小上下文（回到哪一帧/那句查询）
    cbRevertToFrame(cur.id);   // 剪掉该帧之后的所有帧与消息（不可撤销）+ 回放该帧
    cbLogPush("sys", "已回退到这一步：之后的对话与改动已舍弃，不可撤销。");
    cbRenderSteps();
    toast("已回退到这一步");
}
/* ---------- 信息流 · 在途工具行 HTML（只活在流式期间）----------
   流式态：逐条无框工具行（`.ft-stage`，一工具一行：标记 + 工具名，detail 一律不渲染——
   「除此以外最好什么信息都没有」）。
   本轮结束后：压缩摘要**不再全局渲染**（已挂到回执 entry.flow，渲染在气泡上方），本函数断串。
   `.ft-*` 全部是样式钩子；本函数只出结构与纯文本，动态值一律 escapeHtml。 */
function _ftStageRow(r) {
    const stall = (r.phase === "pending" || r.phase === "running");
    const cls = r.phase === "failed" ? " ft-failed" : (stall ? " ft-run" : " ft-done");
    const mark = r.phase === "failed" ? "✗" : (stall ? "…" : "✓");
    return '<div class="ft-stage' + cls + '"><span class="ft-mark" aria-hidden="true">' + mark + '</span>'
        + '<span class="ft-text">' + escapeHtml(r.text) + "</span></div>";
}
/* 压缩摘要块（entry.flow 用，渲染在回执气泡上方；<details> 点击原地展开回看全部工具行）。 */
function _ftSummaryBlock(flow) {
    if (!flow || !flow.summaryText) return "";
    /* ft-compressed 两帧余量（计数在 flowFinish 置 2），让摘要原地淡入活到
       回执落地的最后一帧（同步连发只绘制最后一帧）；第三帧起摘除不重播。 */
    const animCls = _flowAnimArmed > 0 ? " ft-compressed" : "";
    if (_flowAnimArmed > 0) _flowAnimArmed -= 1;
    const rows = (flow.expanded || []).map(_ftStageRow).join("");
    return '<div class="ft-trace' + animCls + '">'
        + '<details class="ft-summary"><summary>' + escapeHtml(flow.summaryText) + "</summary>"
        + '<div class="ft-expand">' + rows + "</div></details></div>";
}
function _flowTraceHtml(grp) {
    const snap = flowTraceSnapshot();
    if (snap.done || !snap.stages || !snap.stages.length) return "";
    const grpCls = grp ? " cbh-grp" : "";
    const rows = snap.stages.map(_ftStageRow).join("");
    return '<div class="cbh-turn cbh-sys ft-trace' + grpCls + '"><div class="ft-stages">' + rows + "</div></div>";
}

/* ============ 逐条系统回复的反馈操作条（2026-08-28 msgfb）============
   每条系统回复气泡**外下侧**常驻一排低调小键：赞 / 倒赞 / 评论 / 分支——让用户在每一次
   收到系统回复后都有反馈出口，不必等 benchfb 的高价值节点弹卡。
   - 赞/倒赞：互斥三态（cbMsgFbNext 纯逻辑在 board_core），每次变化打 msgfb usage 事件
     （{conv, mid, v}，无文本）；态度随历史落盘（cbLogForHistory 的 f 字段），回看原样带回。
   - 评论：气泡下原地展开小编辑器；发送复用加密意见通道（feedback_core 入队 +
     usage_upload.sendFeedback，与设置抽屉「使用反馈」同一明示单次授权语义），正文尾部由
     cbMsgCommentText 拼一行「针对哪句回复」的引用（mid + 摘段）。feedback 三件套**只经相对
     路径动态 import**（与 feedback.js 同哲学：不进静态图，import 图门只盯 # 静态边）。
   - 分支：复用 ?fork=<convId>:<N>（与输入条变形三键同一落点格式）——浏览器里新开标签页、
     本窗口不动。没有存活帧的回复（首次检索前的纯对话回音）不能作分支点：按钮置灰、
     点击如实告知（cbMsgForkable 判据——给个点了没反应的按钮才是撒谎）。 */
const _CB_MSG_ICONS = {
    up: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3H14z"/><path d="M7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg>',
    down: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H6.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3H10z"/><path d="M17 2h3a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-3"/></svg>',
    comment: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
    fork: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>',
};
let _cbCommentFor = "";      // 评论编辑器正挂在哪条消息上（mid；"" = 没开）
let _cbCommentDraft = "";    // 编辑器草稿（innerHTML 重画后回填，不丢字）
let _cbCommentBusy = false;  // 发送中（防重复提交；完成/失败都复位）

/* 消息操作条 / 历史回复链接的文案（同一句在按钮 title 与点击提示两处复用，字面量只留一份）。 */
const _CB_FORK_DISABLED_COPY = "这句回复之前还没有检索结果，无法从这里分支";
const _CB_VIEW_LINK_HINT = "在结果区看这一步当时的结果；之后可以从这里分支或回退";

function _cbEntryById(mid) {
    const id = String(mid || "");
    for (let i = 0; i < _cbLog.length; i++) { if (_cbLog[i].id === id) return _cbLog[i]; }
    return null;
}

function _cbMsgActBarHtml(e) {
    const mid = String(e.id || "");
    const fb = e.msgFb || "";
    const forkable = cbMsgForkable(e.frameId, e.frameId != null && cbFrameIndexById(e.frameId) >= 0);
    let out = '<div class="cbh-actbar">'
        + '<button type="button" class="cbh-act' + (fb === "up" ? " is-on" : "") + '" data-cbh-up="' + escapeHtml(mid) + '"'
        + ' aria-pressed="' + (fb === "up") + '" title="' + (fb === "up" ? "取消赞" : "这句回复有帮助") + '">'
        + _CB_MSG_ICONS.up + "</button>"
        + '<button type="button" class="cbh-act' + (fb === "down" ? " is-on" : "") + '" data-cbh-down="' + escapeHtml(mid) + '"'
        + ' aria-pressed="' + (fb === "down") + '" title="' + (fb === "down" ? "取消倒赞" : "这句回复没帮上忙") + '">'
        + _CB_MSG_ICONS.down + "</button>"
        + '<button type="button" class="cbh-act' + (_cbCommentFor === mid ? " is-on" : "") + '" data-cbh-comment="' + escapeHtml(mid) + '"'
        + ' title="就这句回复说两句（经加密意见通道发给开发者）">'
        + _CB_MSG_ICONS.comment + "</button>"
        + '<button type="button" class="cbh-act' + (forkable ? "" : " is-off") + '" data-cbh-fork="' + escapeHtml(mid) + '"'
        + ' aria-disabled="' + (!forkable) + '" title="' + (forkable ? "从这里分出新对话：新开标签页，本窗口不动" : _CB_FORK_DISABLED_COPY) + '">'
        + _CB_MSG_ICONS.fork + "</button>"
        + "</div>";
    if (_cbCommentFor && _cbCommentFor === mid) {
        out += '<div class="cbh-cmt"><textarea class="cbh-cmt-ta" data-cbh-cmt-ta="' + escapeHtml(mid) + '"'
            + ' rows="2" maxlength="2000" placeholder="就这句回复说两句……">' + escapeHtml(_cbCommentDraft) + "</textarea>"
            + '<div class="cbh-cmt-row">'
            + '<button type="button" class="cbh-cmt-send" data-cbh-cmt-send="' + escapeHtml(mid) + '"'
            + (_cbCommentBusy ? " disabled" : "") + ">" + (_cbCommentBusy ? "发送中……" : "发送") + "</button>"
            + '<button type="button" class="cbh-cmt-cancel" data-cbh-cmt-cancel="' + escapeHtml(mid) + '">取消</button>'
            + "</div></div>";
    }
    return out;
}

function _cbMsgFbToggle(entry, action) {
    if (!entry) return;
    const next = cbMsgFbNext(entry.msgFb || "", action);
    entry.msgFb = next;
    usageLog(USAGE_KINDS.msgfb, { conv: cbConvId(), mid: String(entry.id || ""), v: next });
    cbRenderHistory();
}

function _cbMsgFork(entry) {
    if (!entry) return;
    const idx = (entry.frameId != null) ? cbFrameIndexById(entry.frameId) : -1;
    if (idx < 0) { toast(_CB_FORK_DISABLED_COPY); return; }
    // 与 cbBranchFromHere 同一落点格式（?fork=<convId>:<N>，N = 前 N 轮检索快照）——
    // 浏览器里新开标签页，本窗口原样保留；新标签页由 browse.js 重建前缀并换新 convId。
    const url = location.pathname + "?fork=" + encodeURIComponent(cbConvId() + ":" + (idx + 1));
    if (openPopup(url)) toast("已在新标签页打开分支；本标签页保持不变");
}

function _cbMsgCommentToggle(entry) {
    if (!entry) return;
    const mid = String(entry.id || "");
    _cbCommentFor = (_cbCommentFor === mid) ? "" : mid;
    _cbCommentDraft = "";
    cbRenderHistory();
    if (_cbCommentFor) {
        const box = $("cbHistory");
        const ta = box && box.querySelector('[data-cbh-cmt-ta="' + _cbCommentFor + '"]');
        if (ta) { ta.focus(); autoGrow(ta, { minRows: 2, maxRows: 6 }); }
    }
}

/* 发送 = 对该条不可变记录的明示单次授权（与 feedback.js _onSend 同一语义）：
   先入 per-profile pending 队列，再尝试 sendFeedback；失败留队，稍后的自动重试仍会发出。 */
async function _cbMsgCommentSend(entry) {
    if (!entry || _cbCommentBusy) return;
    const mid = String(entry.id || "");
    const box = $("cbHistory");
    const ta = box && box.querySelector('[data-cbh-cmt-ta="' + mid + '"]');
    const text = ta ? ta.value : _cbCommentDraft;
    _cbCommentBusy = true;
    cbRenderHistory();
    let sent = false;
    try {
        const dlg = await import("../core/feedback_dialog_core.js");
        const fcore = await import("../core/feedback_core.js");
        const st = dlg.feedbackTextState(text);
        if (!st.ok) {
            _cbCommentBusy = false;
            cbRenderHistory();
            toast(st.count > st.max ? "评论太长，最多 " + st.max + " 字" : "请先写下评论");
            const box2 = $("cbHistory");
            const ta2 = box2 && box2.querySelector('[data-cbh-cmt-ta="' + mid + '"]');
            if (ta2) ta2.focus();
            return;
        }
        const row = dlg.feedbackEntryBuild(cbMsgCommentText(text, mid, entry.text), false, null, {});
        const scope = currentAccountScope();
        fcore.feedbackEnqueue(scope, row, {});
        const upload = await import("../core/usage_upload.js");
        sent = await upload.sendFeedback(scope);
    } catch (_e) { sent = false; }
    if (sent) {
        _cbCommentFor = "";
        _cbCommentDraft = "";
        toast("已发送，谢谢反馈");
    } else {
        toast("未立即送达：已存入待发送队列，稍后会自动重试");
    }
    _cbCommentBusy = false;
    cbRenderHistory();
}

function _cbMsgCommentCancel() {
    _cbCommentFor = "";
    _cbCommentDraft = "";
    cbRenderHistory();
}

export function cbRenderHistory() {
    const box = $("cbHistory");
    if (!box) return;
    if (!_cbLog.length && !_cbProg && !arxVisible()) { box.hidden = true; box.innerHTML = ""; cbComposerSync(); return; }
    // 现在游标停在哪一帧：决定每条消息的系统回复呈现（当前那条给状态气泡，其余给「查看历史回复」气泡按钮）。
    const curId = (_cbCursor >= 0 && _cbStack[_cbCursor]) ? _cbStack[_cbCursor].id : null;
    // 「当前帧的最后一条消息」判定（isCurrent 的唯一用途：不给它渲染指着自己的「查看历史回复」）：
    // 一帧上可能挂着「搜索/细化」+若干「已执行」消息，取该 frameId 最后出现的那条。
    // **sys 不计入**：它不渲染回复行，计入会让分支/回退的确认回音把这条判定「偷走」。
    const lastIdxForFrame = {};
    _cbLog.forEach(function (e, i) { if (e.frameId != null && e.kind !== "sys") lastIdxForFrame[e.frameId] = i; });
    // 2026-08-03：每帧的「查看历史回复」入口**并入该帧的系统回复泡**（footer 内联链接）——
    // 同一帧的系统回应只有一颗泡（此前系统泡 + 独立按钮泡两颗并立，用户判定「两条太多」）。
    // 预扫描哪些帧有系统泡；没有的（分面点击细化等不产系统泡的帧）在用户消息下给独立行链接，入口不丢。
    // 两种形态同一颗 `.cbh-view-link` 低调文本链接（无箭头、muted 小字）。
    const sysFrames = {};
    _cbLog.forEach(function (e) { if (e.kind === "sys" && e.frameId != null) sysFrames[e.frameId] = true; });
    // 检索在途且进度泡已蜕变成文字：实时三点挂在最后一颗系统泡的**右端**继续滚（不再滚百分比）。
    // 渲染期注入、不进 _cbLog——不落盘、不参与帧剪枝。
    let lastSysIdx = -1;
    _cbLog.forEach(function (e, i) { if (e.kind === "sys") lastSysIdx = i; });
    const liveWorkAt = (!_cbProg && progressActive()) ? lastSysIdx : -1;
    // telegram 分组——**同一方的连续消息**轮距收紧（.cbh-grp，18→6px），跨方才给完整轮距：
    // 间距本身表达「这是一组」。方＝用户侧（say/refine/action）与系统侧（sys）。
    let prevParty = "";
    const grpOf = function (party) { const g = party !== "" && party === prevParty; prevParty = party; return g; };
    // 双行 telegram 版式（用户手绘图 2）：上＝用户消息气泡（右对齐），下＝系统回复（左对齐）。
    // 执行回执（「已…」注记）是普通灰气泡；非当前帧的「查看历史回复」入口在系统泡 footer（内联链接）。
    // 当前帧**没有任何特殊标识**（状态气泡/高亮描环已退役）——「没有按钮/链接的那一轮」就是当前。
    // 系统回音（sys）是**左对齐灰气泡**；帧链接只在它指向非当前帧时出现。
    box.innerHTML = _cbLog.map(function (e, i) {
        if (e.kind === "sys") {
            const sLinked = e.frameId != null && cbFrameIndexById(e.frameId) >= 0;
            const viewLink = (sLinked && e.frameId !== curId)
                ? '<button type="button" class="cbh-view-link" data-cbh-view="' + e.frameId + '"'
                    + ' title="' + _CB_VIEW_LINK_HINT + '">查看历史回复</button>'
                : "";
            const aiTag = e.llmTag
                ? '<span class="cbh-ai" title="这句由 AI 依据真实执行/检索结果写成；数字与事实以实际结果为准">AI 总结</span>'
                : "";
            const liveWork = (i === liveWorkAt)
                ? '<span class="cbh-prog-dots cbh-live-dots" id="cbLivePct" title="检索进行中"><i></i><i></i><i></i></span>'
                : "";
            // 气泡上方 = 本轮工具轨迹压缩摘要（entry.flow，<details> 原地展开回看，
            // 与流式工具行同一视觉族）——旧 execSummary 通道（.cbh-exec-summary）已退役，
            // 其「执行了 x 次 xxx」职能由压缩句取代（同一口径、且可展开回看）。
            const flowBlock = _ftSummaryBlock(e.flow);
            // 回执气泡**内部**（文字下方）= 结果 pill（一个 query 一个；多 query 多个；
            // 无检索没有）——挪进气泡增强整体性；点击切换结果批（data-ft-pill → is-on → 结果区同步）
            // 行为不变。用 <span> 承载（气泡本身是 <span>，容器走 CSS display:flex，合法不炸布局）。
            // 零命中批 = 视觉区分的 pill（.ft-pill--zero 琥珀/虚线），且仅当该批是
            // 「最新结果」（最后一个回执 entry 的活跃批）时显示「点击处理」；否则点击无任何反应
            // （连常规换批切换也不响应——救回链已退役，避免误以为还能救）。
            const _latestId = latestActiveBatchId(_cbLog);
            const pillsBlock = (Array.isArray(e.pills) && e.pills.length)
                ? '<span class="ft-pills">' + e.pills.map(function (p) {
                    /* 2026-08-30 下载批 pill（p.dlq）——与检索结果 pill 同位同族（气泡内
                       文字下方），一批一颗；点击 = 打开下载面板（下方 data-dlq-pill 分支），
                       不换批、无 is-on 状态语义（它是动作开关，不是结果批指示）。 */
                    if (p.dlq) {
                        return '<button type="button" class="ft-pill ft-pill--dlq" data-dlq-pill="1"'
                            + ' title="打开下载面板：查看队列、取消还没开始的排队项、追加下载">'
                            + '<span class="ft-pill-q">' + escapeHtml(p.label || "下载队列") + '</span>'
                            + (p.count != null ? '<span class="ft-pill-n">' + escapeHtml(String(p.count)) + " 项</span>" : "")
                            + "</button>";
                    }
                    const _zero = !!p.zero;
                    const _isLatest = _zero && String(p.batchId || "") === _latestId;
                    const _on = p.active;
                    return '<button type="button" class="ft-pill' + (_on ? " is-on" : "") + (_zero ? " ft-pill--zero" : "") + '"'
                        + ' data-ft-pill="' + escapeHtml(p.batchId || "") + '"'
                        + (_zero ? ' data-ft-zero="1"' : "")
                        + (_isLatest ? ' data-ft-zero-latest="1"' : "")
                        + (p.frameId != null ? ' data-ft-frame="' + escapeHtml(String(p.frameId)) + '"' : "")
                        + ' title="' + (_isLatest ? "这批没有匹配，点击处理可重新给出放宽/换词选项" : (_zero ? "这批没有匹配" : "在结果区看这批结果")) + '">'
                        + '<span class="ft-pill-q">' + escapeHtml(p.label || "检索结果") + '</span>'
                        + (p.count != null ? '<span class="ft-pill-n">' + escapeHtml(String(p.count)) + " 条</span>" : "")
                        + (_isLatest ? '<span class="ft-pill-act">点击处理</span>' : "")
                        + "</button>";
                }).join("") + "</span>"
                : "";
            // fb1：本轮系统回复的评分卡挂载点（rec.id 由 benchfb 收尾时经 benchfbOnChatEntry
            // 贴到 entry 上，重画后由 benchfbAfterRender 把卡填回；不进 _cbLog、不落历史）。
            const bfMount = e.bfRecId
                ? '<div class="bf-mount" data-bf-mount data-bf-rec="' + escapeHtml(e.bfRecId) + '"></div>'
                : "";
            // msgfb：气泡外下侧的 赞/倒赞/评论/分支 操作条（每条系统回复都有；低调小键）。
            const actBar = _cbMsgActBarHtml(e);
            return '<div class="cbh-turn cbh-sys' + (grpOf("sys") ? " cbh-grp" : "") + '">'
                + flowBlock
                + '<div class="cbh-sys-row">'
                + '<span class="cbh-sys-bubble' + (e.needsAgent ? " cbh-agent-bubble" : "") + (e.isError ? " cbh-err-bubble" : "") + '"><span class="sr-only">系统：</span>' + escapeHtml(e.text) + viewLink + aiTag + liveWork + pillsBlock + "</span></div>"
                + actBar
                + bfMount
                + (e.needsAgent
                    ? '<div class="cbh-reply-row"><button type="button" class="cbh-agent-cta" data-cbh-settings>'
                        // C1：无 key 时「去开启 AI 执行」会被门控弹回（死路），如实改指「去配置 API」。
                        + (llmCapable() ? '去开启 AI 执行' : '去配置 API') + '</button></div>'
                    : "")
                + (e.html ? '<div class="cbh-sys-extra">' + e.html + "</div>" : "")
                + "</div>";
        }
        const linked = e.frameId != null && cbFrameIndexById(e.frameId) >= 0;
        const isCurrent = linked && e.frameId === curId && lastIdxForFrame[e.frameId] === i;
        // 本帧有系统泡时，「查看历史回复」入口已并入那颗泡的 footer（内联链接），
        // 用户消息下不再单放；没有系统泡的帧（分面点击细化等）才在这里给独立行链接。
        // 两种形态同一颗 `.cbh-view-link` 低调文本链接（气泡按钮形态已退役）。
        // 执行类（action）的回执（「已…」注记）是普通系统气泡，不加任何标记。
        let reply = "";
        if (linked && !isCurrent && !sysFrames[e.frameId]) {
            reply += '<div class="cbh-reply-row"><button type="button" class="cbh-view-link" data-cbh-view="' + e.frameId + '"'
                + ' title="' + _CB_VIEW_LINK_HINT + '">'
                + '<span class="sr-only">系统：</span>查看历史回复</button></div>';
        }
        if (e.kind === "action" && e.note) {
            reply += '<div class="cbh-reply-row"><span class="cbh-sys-bubble cbh-note">'
                + '<span class="sr-only">系统：</span>' + escapeHtml(e.note) + "</span></div>";
        }
        const kindClass = e.kind === "action" ? "cbh-action" : (e.kind === "say" ? "cbh-say" : "cbh-refine");
        const srPrefix = e.kind === "action" ? "你要求：" : (e.kind === "say" ? "你说：" : "已细化：");
        return '<div class="cbh-turn ' + kindClass + (grpOf("user") ? " cbh-grp" : "") + '"' + (isCurrent ? ' aria-current="true"' : "") + '>'
            + '<div class="cbh-msg-row">'
            +   '<span class="cbh-bubble"><span class="sr-only">' + srPrefix + "</span>" + escapeHtml(e.text) + "</span>"
            + "</div>"
            + reply
            + "</div>";
    }).join("") + (arxVisible() ? arxTailHtml(grpOf("sys")) : "")   // p10 行动流：恒在对话流尾部（系统侧；渲染侧含 collapsing 余韵，见 arxVisible）
        + _flowTraceHtml(grpOf("sys"))   // 信息流：过程轨迹（展开态无框小字 / 终态压缩灰字摘要）
        // 思考指示：**恒在流式输出最底部**（turns → 行动流 → 轨迹 → 本块）——
        // 它是「正在写的回复」的呼吸信号，压在所有过程产物之下才符合视线动线；去气泡化见 app.css。
        // 三点跳动恒在，流式档（_cbProgLabel）额外带不确定态文案（「规划中…」），非流式档只有三点。
        + (_cbProg
            ? '<div class="cbh-turn cbh-sys cbh-pending' + (grpOf("sys") ? " cbh-grp" : "") + '"><div class="cbh-sys-row">'
                + '<span class="cbh-sys-bubble cbh-prog' + (_cbProgEnter ? " cbh-prog-in" : "") + '"><span class="sr-only">系统：</span>'
                + '<span class="cbh-prog-dots" aria-hidden="true"><i></i><i></i><i></i></span>'
                + (_cbProgLabel ? '<span class="cbh-prog-num" id="cbProgPct">' + escapeHtml(_cbProgLabel) + "</span>" : "")
                + '</span></div></div>'
            : "");
    box.hidden = false;
    // 输入条变形：游标不在栈顶＝正在查看历史回复 → 输入条换成「分支 / 回退」双键；回栈顶复原。
    cbComposerSync();
    // 新消息入场动效（Phase D）：消费 cbLogPush 置位的旗标，给最后一条**真消息** turn 播一次 cbhIn。
    // 进度泡蜕变成文字那一帧播 cbhMorph（原位渐变）而非 cbhIn（滑入）——同一颗泡的变化，不是新消息。
    // 尾部常驻块（思考行/轨迹/行动流）也占 .cbh-turn，入场/蜕变目标必须显式排除，
    // 否则思考行挪到最底后，「蜕变」会错播到轨迹摘要块上。
    const morphLast = _cbMorphPending; _cbMorphPending = false;
    if (_cbEnterPending) {
        _cbEnterPending = false;
        const msgTurns = box.querySelectorAll(".cbh-turn:not(.cbh-pending):not(.ft-trace):not(.arx-turn)");
        const lastTurn = msgTurns.length ? msgTurns[msgTurns.length - 1] : null;
        if (MOTION && lastTurn) lastTurn.classList.add(morphLast ? "cbh-morph" : "cbh-enter");
    }
    _cbProgEnter = false;   // 思考行入场类只挂第一次渲染（渲染即消费；流式重画不重播）
    // 「最新在底部」：telegram 态真正的滚动容器是外层 #sideBoardScroll（#cbHistory 自身无 overflow）；
    // 主区静态家（hero）态是 #cbHistory 自身（.cbh-main 给了 max-height + overflow-y）。
    // 滚最近的滚动祖先，否则 scrollTop 写在不可滚元素上是空操作。
    const scroller = box.closest(".sw-board-scroll") || box;
    scroller.scrollTop = scroller.scrollHeight;
    // benchmark 采集（b1）：评分卡是采集层的投影、不进 _cbLog——innerHTML 重画会把它抹掉，
    // 每次重画完由它自己挂回（无待评分记录时是一次布尔短路）。
    benchfbAfterRender();
    // 零命中自动放松卡随「最新结果」同步——最新 pill 是零命中批 → 兜底弹 ask.relax
    // 选择卡（环内已弹卡 / 用户已 dismiss 该批时不抢）。纯 UI 展示层，不进对话流、不落历史。
    maybeAutoRelaxCard();
}

/* ============ fb1：chat 轮收尾 → 评分卡绑到本轮系统回复 entry ============
   benchfb 收尾（_closeTurn）时经 benchfbOnChatEntry 通知本模块，把 rec.id 贴到**本轮**的
   sys entry 上（entry.bfRecId，cbRenderHistory 据此渲染挂载点）。延迟一拍（setTimeout 0）：
   失败收尾路径里收尾发生在 cbProgressDone 的 sys push **之前**（search.js catch 先
   benchfbTurnError 后 cbProgressDone），贴早了会把卡绑到上一轮的泡上；等同步段走完，
   _cbLog 里最新一条 sys 必是本轮的。entry 被剪枝/清空（cbLogClear / 回退）时挂载点自然消失，
   记录仍在 localStorage。 */
let _bfPendingChatId = null;
function _bfOnChatClosed(recId) {
    _bfPendingChatId = recId;
    setTimeout(function () {
        const id = _bfPendingChatId;
        _bfPendingChatId = null;
        if (id == null) return;
        for (let i = _cbLog.length - 1; i >= 0; i--) {
            if (_cbLog[i].kind === "sys") {
                if (!_cbLog[i].bfRecId) _cbLog[i].bfRecId = id;
                break;
            }
        }
        cbRenderHistory();   // 重画出挂载点，渲染钩（benchfbAfterRender）把卡填上
    }, 0);
}

/* ============ 检索范围控件（来源 / 时间）============
   把主控制台的 #sourceFilter + #timeFilter **整块搬进**侧栏输入条的合并弹层 #scopePop（零复制零漂移：
   源/时间的状态读取器全硬编码到这两个节点内部的 id，复制必然两处不一致）。
   2026-08-03：「范围」触发器从带摘要的 chip 改成**与发送键同形的圆钮**——
   摘要挪进弹层自身（toggle 在弹层内降级为静态摘要行，chips/年份下拉恒可见可改），
   圆钮上只留状态特效：两项都在智能识别 → is-auto（亮点+描边）；任一自定义 → 朴素态。
   摘要以 title/aria-label 形式现读两个既有摘要 span（不另存状态、不会漂移）。 */
function scopeChipSync() {
    // 2026-08-30 首页 pill 化后范围圆钮有两枚——侧栏 #scopeChip 与首页 #heroScopeBtn
    // 同读两个摘要 span + 两块面板的 mode class，一处真源同步两处呈现（谁不在场谁跳过）。
    const src = ($("srcSummary") && $("srcSummary").textContent.trim()) || "自动识别";
    const time = ($("timeSummary") && $("timeSummary").textContent.trim()) || "自动识别 · 不限";
    const srcAuto = !!($("srcPanel") && $("srcPanel").classList.contains("mode-auto"));
    const timeAuto = !!($("timePanel") && $("timePanel").classList.contains("mode-auto"));
    const label = "检索范围：" + src + "；" + time + "（点开查看与修改）";
    ["scopeChip", "heroScopeBtn"].forEach(function (id) {
        const btn = $(id);
        if (!btn) return;
        btn.classList.toggle("is-auto", srcAuto && timeAuto);
        btn.classList.toggle("is-custom", !(srcAuto && timeAuto));
        btn.title = label;
        btn.setAttribute("aria-label", label);
    });
    scopeDockSyncState();   // 右坞圆钮的自定义小点与 chip 的 is-auto/is-custom 同源
}
function scopePopOpen(open) {
    const pop = $("scopePop"), chip = $("scopeChip");
    if (!pop || !chip) return;
    pop.hidden = !open;
    chip.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
        // 防御：面板若曾在弹层外被「点面板外收起」/Esc 合上（hidden=true），重开弹层时必须重新
        // 展开——弹层语义＝控件恒可见可改。interactions 的 open*Panel 已对弹层内节点拒关，这两行兜的是
        // 「搬进弹层之前就被合上」的历史态（此前弹层因此只剩两行标签，正是「无法改变筛选」的观感来源）。
        if ($("srcPanel")) $("srcPanel").hidden = false;
        if ($("timePanel")) $("timePanel").hidden = false;
        // 右坞（2026-08-05 用户）：当前选中维是自动、另一维被自定义过 → 跳到自定义那维。
        scopeDockWire();
        const srcAuto = !!($("srcPanel") && $("srcPanel").classList.contains("mode-auto"));
        const timeAuto = !!($("timePanel") && $("timePanel").classList.contains("mode-auto"));
        if (_scopeDockPane === "source" && srcAuto && !timeAuto) scopeDockSelect("time");
        else if (_scopeDockPane === "time" && timeAuto && !srcAuto) scopeDockSelect("source");
        else scopeDockSelect(_scopeDockPane);
        scopeChipSync();
    }
}
/* 首页 pill 的范围弹层：#heroScopePop 锚在 hero .console 正下方，
   与侧栏 scopePopOpen 同一规矩——弹层语义＝两枚筛选恒可见可改，开时把面板可能残留的
   hidden 剥掉；无右坞（两块面板纵排同显），故不碰 scopeDock。 */
function heroScopePopOpen(open) {
    const pop = $("heroScopePop"), btn = $("heroScopeBtn");
    if (!pop || !btn) return;
    pop.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
        if ($("srcPanel")) $("srcPanel").hidden = false;
        if ($("timePanel")) $("timePanel").hidden = false;
        scopeChipSync();
    }
}
/* 统一输入条 #chatComposer 是否住在主区 hero（2026-08-18 输入栏统一）：chat-in-main
   且桌面+侧栏未收起时，主区输入从 hero 大框（#queryInput console）换成统一圆角长条，
   范围控件随之进它的弹层 #scopePop（与 side 态同源判据）。侧栏收起/移动端不搬——
   hero 大框仍是唯一输入入口（沿 p10 设计注释）。 */
function composerInHero() {
    return cbChatInMain()
        && !document.body.classList.contains("side-closed")
        && window.innerWidth > 780;
}
function placeScopeControls() {
    const src = $("sourceFilter"), time = $("timeFilter");
    if (!src || !time) return;
    const host = $("scopePanels"), pop = $("scopePop"), bar = $("consoleBar"), div = $("cbarDiv");
    const has = swAvailable();
    const inSide = (has.facets || has.board || composerInHero())
        && !document.body.classList.contains("side-closed")
        && window.innerWidth > 780
        && document.body.classList.contains("on-query");
    if (inSide && host && pop) {
        heroScopePopOpen(false);   // 筛选搬去侧栏了，首页弹层（若开着）随关——它锚在 hero 输入条上，内容已不在其中
        if (src.parentElement !== host) host.appendChild(src);
        if (time.parentElement !== host) host.appendChild(time);
        // 弹层内两块面板恒展开（toggle 由 CSS 隐藏）；hidden 一剥，面板内容直接可读可点。
        if ($("srcPanel")) $("srcPanel").hidden = false;
        if ($("timePanel")) $("timePanel").hidden = false;
        // 2026-08-05 用户点图：右坞双圆钮 + 单面板——按钮只绑一次；开面板时若当前维是自动、
        // 另一维被自定义过，自动跳到自定义那一维（把用户的手动意图摆到眼前）。
        scopeDockWire();
        scopeDockSelect(_scopeDockPane);
        scopeChipSync();
        document.body.classList.add("scope-in-side");
    } else {
        scopePopOpen(false);
        if (bar && div) {   // 搬回控制台原位：source 在分隔线前、time 在分隔线后（还原 grid 三格顺序）
            if (src.parentElement !== bar) bar.insertBefore(src, div);
            if (time.parentElement !== bar) bar.appendChild(time);
        }
        // 复位为收起态（弹层里被剥掉的 hidden 补回，toggle 的 aria 也归位）。
        openSrcPanel(false);
        openTimePanel(false);
        document.body.classList.remove("scope-in-side");
    }
}

/* 弹层「右坞双圆钮 + 单面板」（2026-08-05 用户）：数据来源/发表时间两枚圆钮竖排在右，
   点谁显示谁的选项（同一时刻只显示一块面板）；自动识别态内容从简（CSS 按 mode-auto 收）。
   圆钮上的 is-custom 小点 = 该维当前是自定义（状态由控件自身呈现，与 scopeChip 的 is-auto 同源）。 */
let _scopeDockPane = "source";
function scopeDockWire() {
    const dock = $("scopePop") && $("scopePop").querySelector(".scope-dock");
    if (!dock || dock.dataset.wired) return;
    dock.dataset.wired = "1";
    dock.querySelectorAll(".scope-dock-btn").forEach((b) => {
        b.addEventListener("click", (e) => { e.stopPropagation(); scopeDockSelect(b.dataset.pane); });
    });
}
function scopeDockSelect(pane) {
    if (pane !== "time") pane = "source";
    _scopeDockPane = pane;
    const src = $("sourceFilter"), time = $("timeFilter");
    if (src) src.classList.toggle("is-on", pane === "source");
    if (time) time.classList.toggle("is-on", pane === "time");
    document.querySelectorAll(".scope-dock-btn").forEach((b) => {
        const on = b.dataset.pane === pane;
        b.classList.toggle("is-on", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
    });
}
function scopeDockSyncState() {
    const srcB = $("scopeDockSrc"), timeB = $("scopeDockTime");
    if (srcB) srcB.classList.toggle("is-custom", !!($("srcPanel") && !$("srcPanel").classList.contains("mode-auto")));
    if (timeB) timeB.classList.toggle("is-custom", !!($("timePanel") && !$("timePanel").classList.contains("mode-auto")));
}

/* ============ 对话记录（#cbHistory）落位 ============
   重做主区/侧栏分工，取代「主区永不再有对话界面」的设计：

   - **有检索结果**（has-results，含零结果/弃权——结果区有东西要显示）：
     对话记录住侧栏 #sideBoardScroll（的形态，微信式窄列）。
   - **无结果但有对话**（cbChatInMain：首句是工具调用 / clarify 回音 / 检索还在路上）：
     对话记录住主区 hero 内的 #chatMain——Codex 式中央列，输入框就是它下方的 hero console
     （此时侧栏工作卡整卡收起，swSync 同一真源判断）。结果一落地（enterResultsLayout 加了
     has-results）下一次 swSync 链就把它搬回侧栏，主区让位给结果卡。
   - 移动端 / 侧栏收起：同样住 #chatMain（原「.chips 前回退位」退役，主区只有一个家）。 */
export function cbChatInMain() {
    const qview = document.querySelector('.view[data-view="query"]');
    const inResults = !!(qview && qview.classList.contains("has-results"));
    const hasConv = _cbLog.length > 0 || _cbProg || arxVisible();
    return document.body.classList.contains("on-query") && !inResults && hasConv;
}

/* ============ 视图交换（2026-08-16 vs1）：主区结果网格 ↔ 侧栏对话窗 ============
   用户痛点：出结果后对话窝在左下小窗。点 #swSwapBtn（.sw-switch 行内双箭头钮）后：
   对话记录 + 统一输入条搬进结果区的 #chatStage（主区宽列），#resultsGrid 整节点搬进
   侧栏 #sideBoardScroll 以 grid-mini 紧凑卡呈现；vs2 起结果区头部件（#resultsHead：
   标题行/摘要卡/诚实回显条）随网格一起进侧栏，主区纯对话（无白卡框）。纯 UI 偏好、**不落盘**（刷新即无结果，
   交换本无意义）。生效判据 viewSwapEffective()：旗标 ∧ on-query ∧ has-results ∧ 侧栏展开
   ∧ 非移动端——任一不满足即整体退化为现状布局（移动端/收起侧栏/无结果自动落回原位，
   内容不消失）。搬家唯一收口是 placeChatSuite()（swApplyMode 链尾，与 placeChatLog 同链）：
   一律整节点 move（已绑定监听不失效），绝不按数据重渲。 */
let _viewSwap = false;
let _suiteTl = null;   // 交换动画在途句柄（任务6）：反向翻转时杀掉旧时间线并把旧方向瞬时落定，再播新动画
function viewSwapEffective() {
    const qview = document.querySelector('.view[data-view="query"]');
    return !!(_viewSwap
        && document.body.classList.contains("on-query")
        && qview && qview.classList.contains("has-results")
        && !document.body.classList.contains("side-closed")
        && window.innerWidth > 780);
}

/* 交换搬家的唯一收口（swApplyMode 链尾、placeChatLog 之后调用；对话记录本体的落位
   仍归 placeChatLog——它在交换态把 #cbHistory 放进 #chatStage 上槽）。
   进入交换：#chatComposer 整棵（含 #scopePop 弹层锚点）→ #chatStageBar，#resultsGrid →
   #sideBoardScroll（加 grid-mini 紧凑化），页签文案「继续对话」→「检索结果」。
   vs2 起 #resultsHead（标题行/摘要卡/各诚实回显条）随 grid 一起搬进侧栏顶部（卡片列表之前），
   主区只剩对话舞台；#feasibilityPanel/#taskPackPanel 不进 #resultsHead、留在主区原位。
   退出交换：全部搬回静态原位（head 回 #resultsWrap 首子、grid 回 #resultsWrap 末尾、
   composer 回 #sideWork 末尾）。
   只在交换态**翻转**那一刻搬家 + 播淡入微移（MOTION 门控，reduced-motion 瞬时切换）；
   稳态调用零 DOM 操作直接短路（每次 swSync 都会过这里，必须便宜）。 */
function placeChatSuite() {
    const stage = $("chatStage"), slotBar = $("chatStageBar");
    const grid = $("resultsGrid"), composer = $("chatComposer");
    const head = $("resultsHead");
    if (!stage || !slotBar || !grid || !composer || !head) return;
    const swap = viewSwapEffective();
    const btn = $("swSwapBtn");
    if (btn) {   // 按钮态每次对齐（aria-pressed + title 双文案），与是否翻转无关
        btn.setAttribute("aria-pressed", swap ? "true" : "false");
        btn.title = swap ? "把结果放回主区，对话收回侧栏" : "把对话放大到主区，结果收进侧栏";
    }
    const scroll = $("sideBoardScroll"), wrap = $("resultsWrap"), work = $("sideWork");
    const tabLabel = $("swTabBoard") && $("swTabBoard").querySelector("span");
    const log = $("cbHistory");
    /* 搬家落定（两方向共用，dir 分流）：全部 DOM 操作幂等——动画在途被杀时新一轮 settle 照样
       把节点放到位，不留半搬态。对话记录本体由重跑的 placeChatLog 落位（翻转帧它那边已 defer）。 */
    const settleTo = function (dir) {
        scopePopOpen(false);   // 先关范围弹层：它锚在 composer 上（bottom:100%+6px），搬家途中悬着会错位
        if (dir) {
            document.body.classList.add("view-swapped");
            slotBar.appendChild(composer);   // 整棵搬：输入条/范围圆钮/弹层/分支三键随搬家全部生效（监听按 id 绑定）
            if (scroll) { scroll.appendChild(head); scroll.appendChild(grid); }   // 头部件在前、紧凑卡列表在后
            grid.classList.add("grid-mini");
            stage.hidden = false;
            if (tabLabel) tabLabel.textContent = "检索结果";
            if (scroll) scroll.setAttribute("aria-label", "检索结果（紧凑视图）");
            placeChatLog();   // 对话记录 → #chatStage 上槽（defer 后在这里落定）
            // 焦点落点（与 placeChatLog 主区→侧栏迁移同一规矩）：输入条在场落 #chatInput；
            // 游标不在栈顶（输入条已变形成三键）落「从这里建立分支」（同 cbHistoryClick 的 detail===0 处理）。
            const fork = $("cbForkBar");
            if (fork && !fork.hidden && $("cbBranchBtn")) $("cbBranchBtn").focus();
            else { const ci = $("chatInput"); if (ci) ci.focus(); }
            if (log) log.scrollTop = log.scrollHeight;   // 贴底：最新一条在视野里（主区态 log 自滚动，见 .cbh-main）
        } else {
            document.body.classList.remove("view-swapped");
            if (wrap) wrap.insertBefore(head, wrap.firstChild);   // 静态原位：#resultsWrap 首子
            if (wrap) wrap.appendChild(grid);     // 静态原位：#resultsWrap 末尾（#taskPackPanel 之后）
            grid.classList.remove("grid-mini");
            if (work) work.appendChild(composer); // 静态原位：#sideWork 末尾（#sideBoardPane 之后）
            stage.hidden = true;
            if (tabLabel) tabLabel.textContent = "继续对话";
            if (scroll) scroll.setAttribute("aria-label", "对话与细化记录（可上下滚动）");
            placeChatLog();   // 对话记录回侧栏 #sideBoardScroll（或侧栏收起/移动端时的 #chatMain）
        }
    };
    // 快速连续翻转（连点交换钮）：反向才来打断——杀掉在途时间线，把旧方向**瞬时落定**
    // （否则 swap===swapped 的早退判据会把第二次翻转吞掉，卡在半搬态），再从那个稳态播新动画。
    // 同向的链上重跑（swSync 每次都会过这里）直接放行，让在途动画跑完，不能半途瞬跳。
    if (_suiteTl) {
        if (swap === _suiteTl._dir) return;
        const pendingDir = _suiteTl._dir;
        _suiteTl.kill();
        _suiteTl = null;
        gsap.set([stage, head, grid, composer, log].filter(Boolean), { clearProps: "opacity,visibility,transform" });
        settleTo(pendingDir);
    }
    // 稳态早退必须**重读实时 DOM**：上面 settleTo(pendingDir) 刚搬完家，入口快照 swapped 已过期——
    // 用旧值会把「反向打断后的新动画」误吞（实测快速双击卡半搬态：view-swapped 留着、网格不进主区）。
    if (swap === document.body.classList.contains("view-swapped")) return;
    if (!MOTION) { settleTo(swap); return; }
    /* 交换动画统一为「淡出 → 搬家 → 淡入」一段式，手动点 #swSwapBtn 与
       自动回退（收侧栏/跨断点/结果清空）同走此路。对向两区的旧内容同步淡出（0.16s power2.in），
       落定后各自在新家淡入微升（0.26s power2.out）——先隐后搬不闪现，缓出回温不生硬。 */
    const cast = [stage, head, grid, composer, log]
        .filter(function (el) { return el && !el.hidden && el.getClientRects().length; });
    const inEls = (swap ? [stage, head, grid] : [head, grid, composer, log])
        .filter(function (el) { return el; });
    const tl = gsap.timeline({ onComplete: function () { if (_suiteTl === tl) _suiteTl = null; } });
    tl._dir = swap;
    _suiteTl = tl;
    tl.to(cast, { autoAlpha: 0, duration: 0.16, ease: "power2.in" })
        .add(function () {
            settleTo(swap);
            gsap.set(cast, { clearProps: "opacity,visibility,transform" });
        })
        .fromTo(inEls,
            { autoAlpha: 0, y: 8 },
            { autoAlpha: 1, y: 0, duration: 0.26, ease: "power2.out", clearProps: "opacity,visibility,transform" });
}

function placeChatLog() {
    const log = $("cbHistory");
    if (!log) return;
    const scroll = $("sideBoardScroll");
    const main = $("chatMain");
    const qview = document.querySelector('.view[data-view="query"]');
    const inMain = cbChatInMain();
    const wasChatMain = !!(qview && qview.classList.contains("chat-main-on"));
    const inSidebar = !inMain
        && !document.body.classList.contains("side-closed")
        && window.innerWidth > 780
        && document.body.classList.contains("on-query");
    /* 视图交换（vs1）生效时，对话记录的家是结果区 #chatStage 的上槽（自滚动宽列）——
       不是 #chatMain（那是「无结果有对话」的家；交换态有结果，结果网格此时在侧栏）。
       换位只改落点，cbh-main 自滚动语义与 #chatMain 态相同。 */
    const swapHome = viewSwapEffective() ? $("chatStageLog") : null;
    const fromParent = log.parentElement;
    /* 过渡（2026-08-03 缺陷修复，与 results.js 同一「淡入微升」语言；2026-08-04 复活 hero 位移）：
       (a) 首页 → 对话视图（!wasChatMain && inMain）：hero 大标题等瞬时退场（display:none 不动画），
           **整条对话列（chatMain + console）从首屏居中原位滑向顶部**（hero 位移 FLIP，owner 钦点
           「首页→结果区」动画）+ 对话容器淡入微升——切换不再硬跳。
           锚点必须是 hero 而非 console：chat-main-on 一落，console 新旧位置在该视口几乎重合
           （实测 dy≈1.4px，「滑落」名存实亡），真正位移的是 hero（居中块顶 → 页面顶，~220px）；
           且同一帧播、此后 enterResultsLayout 量得 dy≈0 自动跳过（不双播）。
       (b) 对话视图 → 左下侧边栏（fromParent===main && inSidebar，结果落地）：工作卡入场**已有**
           CSS swIn（app.css .side-work:not([hidden)]，08-02 图4 定的整卡滑入+淡入，hidden→显示自动重播）
           ——它是这段迁移的唯一卡级动画机器，这里**不得再叠加 gsap 卡级 tween**（实测双引擎抢
           transform/opacity：gsap 读到 swIn 的 -12px 缓存进自己的渲染，两路互相覆盖）。
           这里只给对话记录本体补一个淡入微移，标记它在卡里的新家。 */
    const heroFlip = (MOTION && !wasChatMain && inMain && qview) ? qview.querySelector(".hero") : null;
    const heroFromTop = heroFlip ? heroFlip.getBoundingClientRect().top : 0;
    // （a) 路径的瞬时 display:none 软化——hero 问候/示例 chips/记忆建议与
    // （侧栏展开桌面档将被统一输入条取代的）hero 大框，各留一粒屏幕原位幽灵淡出再隐：
    // 余部向上飘走、大框向下沉去（输入权交给即将落位的 #chatComposer），切换不再硬跳。
    if (heroFlip) {
        heroFlip.querySelectorAll(".hero-rot, .chips, .memory-suggestions").forEach(function (el) { ghostExit(el, { y: -12, duration: 0.32 }); });
        if (composerInHero()) ghostExit(heroFlip.querySelector(".console"), { y: 22, duration: 0.34, ease: "power3.inOut", keepIds: true });
    }
    // 交换翻转帧（viewSwapEffective 与 view-swapped 类尚未一致）对话记录的搬家延到
    // placeChatSuite 的「淡出 → 搬家 → 淡入」里做——否则侧栏旧内容在淡出开始前就已瞬移（先闪后淡）。
    const flipPending = (!!swapHome) !== document.body.classList.contains("view-swapped");
    if (swapHome) {
        // 视图交换（vs1）：对话记录住进 #chatStage 上槽；搬家动效/焦点/贴底由链尾 placeChatSuite 统一播。
        if (fromParent !== swapHome && !flipPending) swapHome.appendChild(log);
        if (main) main.hidden = true;
    } else if (inSidebar && scroll) {
        if (fromParent !== scroll && !flipPending) {
            scroll.insertBefore(log, scroll.firstChild);
            // (b) 主区 → 侧栏迁移（结果落地）：卡级入场由 CSS swIn 承担（见上方注释，不叠加），
            // 这里只给对话记录本体淡入微移标新家；焦点跟到侧栏输入行（继续说）。
            if (MOTION && fromParent === main) gsap.from(log, { opacity: 0, x: -10, duration: 0.3, ease: "power3.out", clearProps: "all" });
            if (fromParent === main) { const ci = $("chatInput"); if (ci) ci.focus(); }
        }
        if (main) main.hidden = true;
    } else if (main) {
        if (fromParent !== main && !flipPending) {
            main.appendChild(log);
            // 侧栏 → 主区回退（侧栏收起/移动端/结果清空且对话还在，inMain=false）：淡入微升提示新家。
            // 首页 → 对话视图（inMain=true）不播这条——容器级入场由下方 (a) 统一承担，避免双重动效。
            if (MOTION && !wasChatMain && !inMain) gsap.from(log, { opacity: 0, y: 8, duration: 0.3, ease: "power3.out", clearProps: "all" });
        }
        main.hidden = false;   // 记录已搬进主区（chat-in-main 或移动端/收起侧栏回退）——容器必须可见
    }
    if (qview) qview.classList.toggle("chat-main-on", inMain);
    if (heroFlip) {
        // (a) 的位移 FLIP：chat-main-on 已落、布局已定，hero 从旧屏幕位置（首屏居中）平滑送到位
        // （顶部对话列）；时长 0.48s ≤ 钦点 500ms 上限；对话容器同步淡入微升。
        const hdy = heroFromTop - heroFlip.getBoundingClientRect().top;
        if (Math.abs(hdy) > 1) gsap.fromTo(heroFlip, { y: hdy }, { y: 0, duration: 0.48, ease: "power3.inOut", clearProps: "transform" });
        if (main) gsap.from(main, { autoAlpha: 0, y: 10, duration: 0.45, ease: "power2.out", clearProps: "all" });
    }
    // （2026-08-18 输入栏统一）：chat-in-main 态主区不再挂 hero 大框——统一输入条
    // #chatComposer（圆角长条 + 范围钮 + 纸飞机）从侧栏 #sideWork 搬进 hero（#chatMain 之后），
    // 对话态只有 ② 一种输入栏；离开 chat-in-main 搬回静态原位。交换态由链尾 placeChatSuite
    // 整棵搬到 #chatStageBar，这里不碰（防抢节点）。
    if (!swapHome) {
        const composer = $("chatComposer"), work = $("sideWork");
        const heroHome = qview ? qview.querySelector(".hero") : null;
        if (composer && heroHome) {
            const target = composerInHero() ? heroHome : work;
            if (target && composer.parentElement !== target) {
                scopePopOpen(false);   // 弹层锚在 composer 上（bottom:100%+6px），搬家途中悬着会错位（同 placeChatSuite）
                target.appendChild(composer);
                // 统一输入条落进 hero（chat-in-main）时淡入微升到位——它接替刚淡出的 hero 大框
                // 输入权交接不断片。搬回侧栏不播（卡级 swIn / placeChatSuite 的淡入已承担）。
                if (MOTION && target === heroHome) gsap.from(composer, { autoAlpha: 0, y: 10, duration: 0.32, ease: "power2.out", clearProps: "all" });
            }
        }
    }
    // 主区态靠自己滚动（.cbh-main：flex:1 + overflow-y）；侧栏态由外层 #sideBoardScroll 统一滚动。
    log.classList.toggle("cbh-main", !inSidebar || !!swapHome);
    placeCbPreview();   // 追问/选择卡跟随对话记录落位（chat-in-main 时它的静态家必不可见，见函数注释）
}

/* #cbPreview（ask.relax 选择卡 / cbShowMessage 追问卡共用节点）的可见宿主跟随。
   它的静态家在条件板 #cbBody 里，而条件板只在「有结果落地」时才被摘下 hidden
   （renderCondBoard），chat-in-main（无结果有对话）时整条宿主链必然不可见
   （#condBoard hidden + 侧栏工作卡整卡收起）——环内 ask.relax 暂停回合恰恰没有结果落地，
   卡渲进去 0×0 不可见：用户只看到「选完我会按你的选择自动重跑检索并继续」却无东西可点。
   规则：卡有内容且处于 chat-in-main → 跟对话记录走（#cbHistory 之后、主区输入框正上方，
   即 index.html 对本卡「聊天记录下方、对话框上方」定位在该态下的正解）；其余一律回静态家。
   整节点搬家（点击委托绑在 #cbPreview 自身——见 initCondBoard——搬家不失效）；
   与 placeChatLog 同链收口，两个渲染口（askRelaxShow/cbShowMessage）落定后各调一次。 */
function placeCbPreview() {
    const pv = $("cbPreview"), log = $("cbHistory");
    if (!pv || !log) return;
    const live = !pv.hidden && !!pv.firstChild;   // 有内容的卡才算「在屏」；清空/隐藏态只负责回家
    if (live && cbChatInMain()) {
        const host = log.parentElement;
        if (host && pv.previousElementSibling !== log) host.insertBefore(pv, log.nextSibling);
    } else {
        const home = $("cbBody");
        if (home && pv.parentElement !== home) {
            home.insertBefore(pv, home.querySelector(".cb-hint"));   // 静态原位：#cbStepHint 之后、「不想打字…」提示之前
        }
    }
}

function initSideWork() {
    // 视图交换钮（2026-08-16 vs1）：只切旗标、走 swSync 链整体重排（搬家统一在 placeChatSuite 收口）。
    // 交换生效后侧栏承载的是结果网格——若显示模式停在「细化筛选」，用户点交换会看不到结果，
    // 故顺手把显示切回「检索结果」页签（只是显示切换：不算显式选择、不落盘，同 swShowBoard 口径）。
    const swapBtn = $("swSwapBtn");
    if (swapBtn) swapBtn.addEventListener("click", function () {
        _viewSwap = !_viewSwap;
        swSync();
        if (viewSwapEffective() && _swMode !== "board") swApplyMode("board", null);
    });
    const _swTabIds = ["swTabFacets", "swTabBoard"];
    _swTabIds.forEach(function (id, i) {
        const el = $(id);
        if (!el) return;
        el.addEventListener("click", function () { swApplyMode(el.dataset.swMode, null, { picked: true }); });
        // WAI-ARIA tab 模式的方向键：左右/上下在两个页签间移动焦点并激活。
        el.addEventListener("keydown", function (event) {
            let next = -1;
            if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (i + 1) % _swTabIds.length;
            else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (i + _swTabIds.length - 1) % _swTabIds.length;
            else return;
            event.preventDefault();
            const target = $(_swTabIds[next]);
            if (target && !target.disabled) { target.focus(); swApplyMode(target.dataset.swMode, null, { picked: true }); }
        });
    });
    // 侧栏收展 / 视口变化 / 切视图都要重新落位——否则收起侧栏后条件板留在已隐藏的容器里，
    // 用户既看不到也点不到（这正是 hidden 属性那一族缺陷的形态：功能还在、但到不了）。
    // resize 走 placeFacetBar（内部收口 swSync）而非只 swSync：跨 780 断点时 #facetBar 也要重新落位，
    // 否则 #facetActive(常驻栏,swSync 管) 与 #facetBar(分面条,placeFacetBar 管) 会落到不同容器/被搁在隐藏区。
    window.addEventListener("resize", function () { placeFacetBar(false); });
    // 「范围」chip（图7）：点开合并弹层；点外面 / Esc 收起。摘要跟随两个既有摘要 span
    // （MutationObserver 现读现写，不另存状态——updateSrcSummary/updateTimeSummary 无需知道这里）。
    const chip = $("scopeChip");
    if (chip) {
        chip.addEventListener("click", function (e) {
            e.stopPropagation();
            scopePopOpen($("scopePop") ? $("scopePop").hidden : false);
        });
        document.addEventListener("click", function (e) {
            const pop = $("scopePop");
            if (pop && !pop.hidden && !pop.contains(e.target) && e.target !== chip && !chip.contains(e.target)) scopePopOpen(false);
        });
        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape" && $("scopePop") && !$("scopePop").hidden) scopePopOpen(false);
        });
        const sync = function () { scopeChipSync(); };   // 两枚圆钮（侧栏 scopeChip / 首页 heroScopeBtn）一处同步，不再按 scope-in-side 分闸门
        // 摘要文字（srcSummary/timeSummary）与模式 class（srcPanel/timePanel 的 mode-auto/custom）
        // 都是圆钮特效的真源：模式切换时摘要文字未必变（「自动识别」前后同文），两路都得盯。
        ["srcSummary", "timeSummary"].forEach(function (id) {
            const el = $(id);
            if (el) new MutationObserver(sync).observe(el, { childList: true, characterData: true, subtree: true });
        });
        ["srcPanel", "timePanel"].forEach(function (id) {
            const el = $(id);
            if (el) new MutationObserver(sync).observe(el, { attributes: true, attributeFilter: ["class"] });
        });
    }
    // 首页 pill 的范围圆钮：点开向下弹层 #heroScopePop；点外面 / Esc 收起。
    // 与上面 scopeChip 块同构，两边互斥在场（结果态 hero console 隐藏、首页态侧栏 composer 未出），各自独立接线。
    const heroBtn = $("heroScopeBtn");
    if (heroBtn) {
        heroBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            heroScopePopOpen($("heroScopePop") ? $("heroScopePop").hidden : false);
        });
        document.addEventListener("click", function (e) {
            const pop = $("heroScopePop");
            if (pop && !pop.hidden && !pop.contains(e.target) && e.target !== heroBtn && !heroBtn.contains(e.target)) heroScopePopOpen(false);
        });
        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape" && $("heroScopePop") && !$("heroScopePop").hidden) heroScopePopOpen(false);
        });
    }
    scopeChipSync();   // 首页态 placeScopeControls 走 else 分支不触发同步——heroScopeBtn 的初态（is-auto 亮点）在这里补齐
    swSync();
}

function cbRenderSteps() {
    // 撤销/重做按钮已退役（游标移动只有「查看历史回复」气泡按钮一个入口；
    // 回栈顶有「回到最新」）。本函数只剩「正在查看历史」的提示行。
    const hint = $("cbStepHint");
    if (hint) {
        const ahead = _cbStack.length - 1 - _cbCursor;
        if (_cbCursor >= 0 && ahead > 0) {
            // 历史位置上不再允许「继续说」静默剪枝——输入条已变成 回到最新/分支/回退 三键，提示照实说。
            hint.textContent = "正在查看历史回复，结果区显示的也是那一步的结果。点「回到最新」返回当前；"
                + "也可以从这里开分支（新标签页），或回退到这一步（舍弃后面 " + ahead + " 步）。";
            hint.hidden = false;
        } else {
            hint.hidden = true;
        }
    }
}

/* 面板收着的时候有话要说（预览、听不懂、三选一）→ 先展开，否则那段话根本看不见。 */
function cbEnsureExpanded() {
    if (!_cbCollapsed) return;
    _cbCollapsed = false;
    cbApplyCollapsed();
}

function cbApplyCollapsed() {
    const body = $("cbBody"), bar = $("cbSummaryBar");
    if (!body || !bar) return;
    body.hidden = _cbCollapsed;
    bar.setAttribute("aria-expanded", _cbCollapsed ? "false" : "true");
    $("condBoard").classList.toggle("collapsed", _cbCollapsed);
}

function cbToggleCollapsed() {
    _cbCollapsed = !_cbCollapsed;
    cbApplyCollapsed();
    try { localStorage.setItem(nsKey(BOARD_COLLAPSED_KEY), _cbCollapsed ? "1" : "0"); } catch (_e) {}
}

/* 推一帧。**只被 search.js 的检索落地共享入口 landRecommendResult 调用**（runRecommend 两个
   落地点 + 零命中救回换屏同走它，2026-08-16 sr1），绝不放进 applyRecommendResult：
   那里同时是「回到上一步」「从左侧历史回看」「切账户重渲」三条路径的落点，
   放进去会让点两次上一步原地不动、也会把三天前的一条历史当成对话的下一步推进栈。
   popts.keepProgress：落地但按住进度泡不蜕变——preliminary 先行帧（环还在跑，
   泡要换句「正在更深一步思考…」继续等）与 final a 档换屏（完成话术由调用方另行留痕）。 */
export function cbPushCurrent(data, query, popts) {
    if (_cbReplaying) return;
    const frame = {
        id: ++_cbFrameSeq,   // 单调帧号：聊天/细化记录的每条消息回填这个 id，「查看历史回复」按它定位
        query: String(query || ""),
        resp: data,
        facets: JSON.parse(JSON.stringify(_facetFilters || [])),
        suppressed: (_suppressed || []).slice(),
        lenient: (_lenientDims || []).slice(),
        // 原始命中快照也得进帧。它是分面条那一整行「查询条件」的数据源，
        // 而 facets.js 只在「没有被忽略的条件」时才会重建它——回放到一个有忽略项的帧上，
        // 快照没了就再也建不回来，那一整行凭空消失。
        queryHits: JSON.parse(JSON.stringify(_queryHits || []))
    };
    const next = cbPushFrame(_cbStack, _cbCursor, frame);
    _cbStack = next.stack;
    _cbCursor = next.cursor;
    // 剪枝：在非栈顶继续改条件时，cbPushFrame 会丢弃游标之后那段「被放弃的分支」帧。
    // 挂在那些帧上的对话/细化消息随之成了指不到帧的孤儿——既没按钮，又会让人误读成「结果是经它们一路改过来的」。
    // 只保留仍指向存活帧的消息，以及尚未归属的在途消息（frameId==null，下面紧接着回填）。与 cbRevertToFrame 同口径。
    const alive = {};
    _cbStack.forEach(function (f) { if (f && f.id != null) alive[f.id] = true; });
    _cbLog = _cbLog.filter(function (e) { return e.frameId == null || alive[e.frameId]; });
    // 回填：这次检索之前新增、还没归属帧的对话/细化消息（一句话可能先记「说」再记「细化」，两条都归到这一帧），
    // 都挂到刚推出的这一帧上——从此它们才有「查看历史回复」按钮。走到第一条已有 frameId 的就停。
    for (let i = _cbLog.length - 1; i >= 0; i--) {
        if (_cbLog[i].frameId != null) break;
        _cbLog[i].frameId = frame.id;
    }
    /* 结果 pill（一个 query 一个）——普通落地（非 _applyBatchDecision 多批路径）也给 pill。
       有多批（result_batches）pill 指向各存活批（点击 switchBatch 换批）；legacy 单批无批 id →
       pill 只带 frameId，点击＝在结果区看这帧的这批结果（与「查看历史回复」同径）。
       零命中且有放松选项 → 包成单批视图给零命中 pill（放松卡触发/重开的锚点，见下方分支）；
       没做检索（abstained、标识符直达空态）→ 无 pill（用户：检索无结果可以没有）。 */
    const _landed = ((data && data.results) || []).length;
    if (_landed > 0) {
        const _lbs = Array.isArray(data.result_batches) ? data.result_batches : null;
        if (_lbs && _lbs.length) {
            flowSetPills(_flowPillsFrom(_lbs, data.active_batch, false));
        } else {
            // legacy 单批——优先用本次命中的关键词作 pill 文案（拿不到 → 回退原 query）。
            flowSetPills([{ batchId: "", frameId: frame.id,
                label: flowHitKeywords(data.query_constraints) || String(query || "检索结果"),
                count: _landed, active: true }]);
        }
    } else if (!Array.isArray(data.result_batches)
               && Array.isArray(data.relaxation_options) && data.relaxation_options.length) {
        /* legacy 单批**零命中**（纯 /api/recommend 路径，2026-09-14 收编补网）：包成单批视图——
           零命中 pill 由此存在，自动弹放松卡（maybeAutoRelaxCard）与 pill「点击处理」重开都锚在
           它上（_findBatchById 读 LAST_RECOMMEND_DATA.result_batches）。abstained/标识符直达
           空态没有 relaxation_options → 照旧无 pill（「没做检索可以没有」不变）。
           batch_id 带帧号：dismiss 记忆按批 id 记，跨查询不得互相吞没。
           payload 用浅拷贝（且在本函数给 data 挂 result_batches **之前**取）——直接引 data 会让
           LAST_RECOMMEND_DATA 自引用成环，任何 JSON 序列化都会炸。 */
        const _zid = "fz" + frame.id;
        const _zpayload = Object.assign({}, data);
        data.result_batches = [{ batch_id: _zid, kind: "final",
            query_raw: String(query || ""), query_effective: String(query || ""),
            payload: _zpayload }];
        data.active_batch = _zid;
        flowSetPills(_flowPillsFrom(data.result_batches, _zid, false));
    }
    // 进度泡：检索落地＝这句的回复到了——进度泡原位蜕变成完成摘要。
    // runRecommend 的所有路径（含加入/去掉/忽略等改条件重跑）都已开泡，这里几乎总是兑现；
    // 无泡 no-op 仅作防御残留（如未来新增绕过开泡的落地点）。
    // popts.keepProgress 按住不蜕变（先行帧要等环、final a 档另行留痕）。
    const _total = Number(data && data.result_total) || ((data && data.results) || []).length;
    const _shown = ((data && data.results) || []).length;
    // R2-2 P2-2：零结果文案按结果区**真实给了什么**分口径——有放宽 chips 才许说「给了放宽方式」。
    // 标识符直达 / 弃权（abstained）路径结果区是「这次没有做检索」说明条，没有任何放宽入口，
    // 照旧模板句就是泡说谎（结果区本体文案是诚实的）。
    let _doneText;
    if (_total > 0) {
        _doneText = searchFactsReceiptText({ total: _total, shown: _shown }, "检索完成：");
    } else {
        const _rs = String((data && data.resolution_status) || "");
        const _hasRelax = (((data && data.relaxation_options) || []).length > 0) || !!(data && data.degraded_search);
        if (_rs === "abstained" || (data && data.identifier_lookup && data.identifier_lookup.is_identifier)) {
            _doneText = _hasRelax ? "这次没有做检索；结果区说明了原因，并给了可以直接点的放宽方式。"
                : "这次没有做检索；结果区说明了原因。";
        } else if (_rs === "clarification_required") {
            _doneText = "这句话有两种理解；结果区给了可以直接点的选项。";
        } else {
            _doneText = _hasRelax ? "没有完全匹配的记录；结果区给了几种可以直接点的放宽方式。"
                : "没有完全匹配的记录。";
        }
    }
    if (!(popts && popts.keepProgress)) {
        // 非流式回退路径（无 SSE 工具行）但 final 帧带了环内 plan.steps——合成工具行
        // 并就地压缩，让这条路径也有「执行了 N 次检索」摘要（流式路径 _flow 非空、flowFinish
        // 已在 ubDispatch 跑过，此处短路不重复压缩）。压缩快照由紧随的 cbProgressDone 回执
        // 领取，渲染在气泡上方；旧 execSummary 通道（.cbh-exec-summary）已退役。
        const _pv = (popts && Array.isArray(popts.toolVerbs)) ? popts.toolVerbs : [];
        if (_pv.length && !renderableStages(_flow).length) {
            _pv.forEach(function (v) {
                const verb = String(v || "").trim();
                if (!verb) return;
                flowPushStage({ id: "tool:" + verb, kind: KIND_TOOL, verb: verb,
                    text: flowVerbLabel(verb), phase: "done" });
            });
            flowFinish();
        }
        /* 混合轮单泡化：本轮检索落地后紧跟一次已规划好的执行（popts.actPending
           「先检索后派发」档）——进度泡**不**蜕变成检索模板句：那会与随后的执行汇报泡构成
           双泡（用户 2026-08-30 截图投诉的正是这对双泡）。泡留给 actDispatchPlan 接管
           （cbProgressDrop），检索事实由 actFinish 的执行汇报合并携带（act.js
           _actTurnSearchFacts）；执行没接住的边界（取消/busy/未派发）由 actAfterSearch
           用 cbProgressDone 补一句诚实收尾，泡绝不悬空。
           flow/pill 快照不丢：它们由 actFinish 的总结 sys 领取（cbLogPush 的领取机制与
           回执泡无关）。 */
        if (popts && popts.actPending) {
            // 见上方块注释——本分支刻意不动进度泡。
        } else {
            // 先上确定性事实句，再异步请 LLM 改写成自然中文回执（成功原位替换+「AI 总结」标，
            // fail-open 留事实句）。hint 须在 cbProgressDone 之前捕获——它会消费 _cbProgHint。
            const _hint = _cbProgHint;
            const _receiptEntry = cbProgressDone(_doneText);
            if (_receiptEntry) cbFetchSearchReply(_receiptEntry, _searchReplyFacts(data, query, popts, _total, _shown, _hint));
        }
    }
    cbRenderSteps();
    cbRenderHistory();   // 消息拿到 frameId → 按钮出现、当前帧高亮更新
}

function cbReplay(frame) {
    if (!frame) return;
    // 这两步缺一不可（照抄历史回看那条路径的处理）：
    // 不作废在途请求，晚到的旧响应稍后落地会顶掉刚恢复的这一帧；
    // 不复位按钮，检索按钮会永久转圈。
    bumpRecSeq();   // 属主是 search.js（C3 起 ESM）：原 `_recSeq += 1` 裸写在 getter 桥上会 TypeError，必经其写口
    resetSubmitButton();
    _cbReplaying = true;
    try {
        setFacetState({   // 四个分面状态的属主是 results.js（C2 起 ESM）：重赋值必经属主 setter（live binding 只读）
            facetFilters: JSON.parse(JSON.stringify(frame.facets || [])),
            suppressed: (frame.suppressed || []).slice(),
            lenientDims: (frame.lenient || []).slice(),
            queryHits: JSON.parse(JSON.stringify(frame.queryHits || []))
        });
        const input = $("queryInput");
        if (input) input.value = frame.query || "";
        applyRecommendResult(frame.resp, frame.query, { fromHistory: true, noScroll: true });
        renderCondBoard(frame.resp);
    } finally {
        _cbReplaying = false;
    }
    // 回放的入口（查看历史回复 / 回到最新 / 分支 / 回退至此 / 历史浮窗找回）都经这里 → 统一在这重画聊天记录，
    // 让系统回复气泡的呈现始终跟着游标走（此前 undo/redo 漏了这步重画）。
    cbRenderHistory();
}

/* 回到最新（三键之一）：游标直接回栈顶——查看历史后不想分支也不想回退时的出口。 */
function cbToLatest() {
    if (!_cbStack.length) return;
    if (_cbCursor >= _cbStack.length - 1) { toast("已经在最新结果上"); return; }
    _cbCursor = _cbStack.length - 1;
    cbReplay(_cbStack[_cbCursor]);
}

/* 从左侧「历史记录」回看时调用：把整条对话搬回来（用户 2026-07-29）。
   `rows` 是同一条对话的历史行，**旧→新**排好序，每行形状即 core.js `pushHist` 存下的那个。

   此前这里叫 `cbResetTo`，做的是「清空对话记录 + 只推最后一帧」——于是从历史回看一次，
   之前那整段对话就没了，用户只能看到孤零零一屏结果。现在：

   - **逐轮重建帧栈**。历史本来就一行存了一份完整快照，所以每条消息的「查看结果」都真能点，
     撤销/重做也能在这条对话里正常走 —— 不是把旧对话画成一张不能交互的截图。
   - **对话记录按累计长度差归属到各轮**：第 k 行存的是「到第 k 轮为止的全部记录」，
     那么第 k 轮新增的就是 `chat[k].slice(chat[k-1].length)`。
   - 配额不足被剥成「仅元信息」的那几轮没有 `snap` → **不建帧**，挂在它上面的消息就没有按钮。
     这是如实的：那一次的结果确实没留下来，给个点不出东西的按钮才是撒谎。
   - conv id 沿用被回看的那条 → 接着改/接着说会续在同一条对话上，而不是又分出一行。 */
export function cbRestoreConversation(rows) {
    rows = Array.isArray(rows) ? rows : [];
    _cbStack = [];
    _cbCursor = -1;
    _cbLog = [];
    _cbConvId = String((rows[rows.length - 1] || {}).convId || "");
    let prevChatLen = 0;
    rows.forEach(function (h) {
        let frameId = null;
        if (h && h.snap && h.snap.results) {
            _cbStack.push({
                id: ++_cbFrameSeq,
                query: String(h.query || ""),
                resp: h.snap,
                facets: (h.facetFilters || []).map(function (f) { return Object.assign({}, f); }),
                suppressed: (h.suppressed || []).slice(),
                lenient: (h.lenientDims || []).slice(),
                queryHits: (h.queryHits || []).map(function (g) {
                    return { filter_id: g.filter_id || g.dim, polarity: g.polarity || "include", dim: g.dim, label: g.label, values: (g.values || []).slice() };
                }),
            });
            _cbCursor = _cbStack.length - 1;
            frameId = _cbStack[_cbCursor].id;
        }
        // 老历史行没有 chat 字段（本轮之前存的）→ 退化成「一条＝当时那句查询」，与从前屏上什么都没有相比只多不少。
        const chat = Array.isArray(h && h.chat) ? h.chat : null;
        const fresh = chat ? chat.slice(prevChatLen) : [{ k: "say", t: String((h && h.query) || "") }];
        fresh.forEach(function (e) {
            const t = String((e && e.t) || "").trim();
            if (!t) return;
            const k = (e && e.k === "action") ? "action" : ((e && e.k === "sys") ? "sys" : ((e && e.k === "say") ? "say" : "refine"));
            // needsAgent 不随历史回看恢复：降级气泡的指路只属当下，回看时只留回音本身（不落盘，见 cbLogForHistory）。
            // msgfb：消息 id 与赞/倒赞态原样带回（旧历史行没有这两个字段 → 重新发 id、态度默认为空）。
            const mid = String((e && e.i) || "");
            const fb = (e && (e.f === "up" || e.f === "down")) ? e.f : "";
            if (mid) _cbMsgIdSeen(mid);
            _cbLog.push({ kind: k, text: t, frameId: frameId, note: String((e && e.n) || ""), needsAgent: false,
                id: mid || _cbMsgIdNext(), msgFb: fb });
        });
        if (chat) prevChatLen = chat.length;
    });
    // 恢复路径与推帧同一截断闸：历史行数 > CB_MAX_FRAMES 时
    // 只留最近 N 帧（与 cbPushFrame 同向丢最旧）；被丢帧的「查看结果」按钮如实消失——
    // 那几轮的结果帧确实不在了，留个点了没反应的按钮才是撒谎。
    if (_cbStack.length > CB_MAX_FRAMES) {
        const dropped = _cbStack.slice(0, _cbStack.length - CB_MAX_FRAMES).map(function (f) { return f.id; });
        _cbStack = _cbStack.slice(_cbStack.length - CB_MAX_FRAMES);
        _cbCursor = _cbStack.length - 1;
        _cbLog.forEach(function (e) { if (e.frameId && dropped.indexOf(e.frameId) >= 0) e.frameId = null; });
    }
    while (_cbLog.length > CB_LOG_MAX) _cbLog.shift();
    cbRenderSteps();
    cbRenderHistory();
    const hint = $("cbStepHint");
    if (hint) {
        // A2 仅对话回看：一行帧都建不起来（当时没有检索）时照实说，不说成「一次结果」。
        hint.textContent = !_cbStack.length
            ? "这是从历史里回看的一段对话（当时只有对话、没有检索），接着说会从这里继续。"
            : (rows.length > 1
                ? "这是从历史里回看的一条对话（共 " + rows.length + " 轮），接着说会从这里继续。"
                : "这是从历史里回看的一次结果，接着改会从这里继续。");
        hint.hidden = false;
    }
}

export function cbClear(opts) {
    // A2：丢弃前归档「仅对话」历史行。账户切换除外（accounts.js 传 archive:false）——
    // 那一刻命名空间已是新账户，归档会把上一个人的对话写进新账户的历史（隔离漏洞）。
    if (!(opts && opts.archive === false)) cbArchiveChatOnly("");
    _cbStack = [];
    _cbCursor = -1;
    _cbCoverageCounts = {};
    cbLogClear();   // 结果清空 → 对话/细化记录随之作废（不把上一轮的记录挂在新一屏上）
    const board = $("condBoard");
    if (board) board.hidden = true;
    swSync();   // 条件板没内容了 → 侧栏工作卡的「对话记录」页签要跟着失效/整卡收起
}

/* ---------- 说一句话改条件 ---------- */

function cbPlanBody(extra) {
    const data = cbFrameData() || {};
    const body = {
        query: cbFrameQuery(),
        utterance: "",
        current_filters: data.query_constraints || [],
        resolution: (data.interpretation && data.interpretation.resolution) || null,
        suppressed_constraints: (_suppressed || []).slice(),
        lenient_dims: (_lenientDims || []).slice(),
        facet_filters: (_facetFilters || []).map(function (f) { return { dim: f.dim, value: f.value }; }),
        coverage_dims: cbCoverageDims(data)
    };
    Object.assign(body, getDateRange(String(body.query || "")));
    Object.assign(body, extra || {});
    return body;
}

async function cbPlan(extra) {
    const myGen = ++_cbSeq;
    let plan = null;
    try {
        const res = await fetch(API.boardPlan, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(cbPlanBody(extra))
        });
        plan = await res.json();
        if (!res.ok || !plan.ok) throw new Error(plan.detail || "这一步没改成，条件保持原样。");
    } catch (err) {
        if (myGen !== _cbSeq) return null;
        cbShowMessage("这一步没改成，条件保持原样。", String((err && err.message) || err), []);
        return null;
    }
    if (myGen !== _cbSeq) return null;   // 已被更晚的一次规划取代
    return plan;
}

function cbShowMessage(message, detail, actions) {
    const box = $("cbPreview");
    if (!box) return;
    cbProgressDrop();   // 进度泡：选项/说明预览卡就是这句的回复——进度泡静默撤下，不重复回
    let html = '<p class="cb-msg">' + escapeHtml(message) + "</p>";
    // 有些拒绝档的补充说明就是主句本身，两行原样重复只会让人以为出了两次错。
    if (detail && detail !== message) html += '<p class="cb-msg-detail">' + escapeHtml(detail) + "</p>";
    if (actions && actions.length) {
        html += '<div class="cb-choices">' + actions.map(function (a) {
            return '<button type="button" class="btn cb-choice" data-cb-choice="' + escapeHtml(a.id) + '">' + escapeHtml(a.label) + "</button>";
        }).join("") + "</div>";
    }
    box.innerHTML = html;
    box.hidden = false;
    cbEnsureExpanded();
    placeCbPreview();   // 与 askRelaxShow 同一可见宿主管线：chat-in-main 时跟对话记录走
}

/* 环内 ask.relax 的「放松哪个条件」选择卡（2026-09-14 YOLO 批）：坏结果的病根在
   用户条件互相打架/过严时由 LLM 环内发起，选项与各选项能救回多少条由服务端确定性
   预演。**不复用 cbShowMessage**——它首行 cbProgressDrop 会把「正在等作答」的进度泡
   撤掉；本卡渲进 #cbPreview（输入框上方），作答走 cbChoose 的 askrelax: 分支。
   卡面两行事实：msg（环内 reason 或默认句）+「卡住的查询：「…」」（payload.query，
   一句话多句查询时指出是哪句）。选项只写「怎么放松 + 约几条」，不内嵌示例数据集名
   （2026-09-14 用户评审：示例名塞进按钮信息效率太低，卡层 payload 不再携带 preview）。 */
function askRelaxShow(payload) {
    const box = $("cbPreview");
    if (!box || !payload) return;
    _askRelaxPending = payload;
    const options = Array.isArray(payload.options) ? payload.options : [];
    if (!options.length) { _askRelaxPending = null; return; }
    const msg = String(payload.reason || "").trim()
        || "这组条件在库里查不到同时满足的数据——选一个放松试试：";
    let html = '<p class="cb-msg">' + escapeHtml(msg) + "</p>";
    // 指出卡住的到底是哪句查询（一句话可含多句查询、各自成批）——选项只答「怎么放松」，
    // 不答「在放松谁」。长查询单行截断，悬停 title 看全。
    const q = String(payload.query || "").trim();
    if (q) {
        html += '<p class="cb-msg cb-relax-q" title="' + escapeHtml(q) + '">卡住的查询：「'
            + escapeHtml(q) + "」</p>";
    }
    html += '<div class="cb-choices">' + options.map(function (o, i) {
        const approx = o.count ? "（约 " + o.count + " 条）" : "";
        const verb = o.kind === "only" ? "只按「" + (o.label || "") + "」搜" : "不按「" + (o.label || "") + "」筛";
        return '<button type="button" class="btn cb-choice" data-cb-choice="askrelax:' + i + '">'
            + escapeHtml(verb + approx) + "</button>";
    }).join("");
    html += '<button type="button" class="btn cb-choice" data-cb-choice="askrelax:dismiss">都不合适，保持原样</button>';
    html += "</div>";
    box.innerHTML = html;
    box.hidden = false;
    cbEnsureExpanded();
    placeCbPreview();   // chat-in-main（无结果落地——环内暂停回合的常态）时静态家不可见，卡跟对话记录走
}

/* 听不懂 / 没通过校验时固定挂出来的一排出路。
   只报错不给下一步，用户的实际行为是放弃条件板、回主搜索框重打整句——那功能等于没有。 */
function cbEscapeHatches() {
    const data = cbFrameData() || {};
    const rows = cbRowsFrom(data.query_constraints || [], _facetFilters, _lenientDims, _suppressed, cbCoverageDims(data));
    const out = rows.filter(function (r) { return r.zone === "query" && r.editable; })
        .map(function (r) { return { id: "edit:" + r.dim, label: "改" + r.label }; });
    out.push({ id: "reset", label: "恢复全部条件" });
    return out;
}

/* ============ 统一对话窗口：提交与路由分发============
   #queryInput 是唯一输入框（#cbInput 已退役）。提交先问后端统一路由 /api/utterance，
   再按 route 分发。后端只在 action/歧义分支碰 LLM——search/refine/identifier/clarify 结构性零 LLM，
   所以明确检索句不为路由多付一次大模型延迟。 */

let _ubSeq = 0;        // 路由请求代号：晚到的旧路由响应一律丢弃（与 _recSeq/_cbSeq/_curateSeq 同型）
let _ubBusy = false;   // 统一框在途闸：一次路由未落地不接下一句。**不绑死 submitBtn**——路由阶段检索还没开始

/* 幂等请求号（2026-08-08 修复）：**每次 ubSubmit 提交生成一个新号**，流式请求与
   断流后的非流式重发共用同一号——服务端按号占用去重，重发拿缓存结果而不是把同一句话
   再执行一遍（写工具重复入库的问题）。两次独立提交（用户真发两遍）各自新号，不受影响。
   crypto.randomUUID 在老内核/非安全上下文可能缺席 → 回退时间戳+随机数拼接。 */
function ubNewReqId() {
    try {
        // 2026-08-08：静态门禁止 typeof 守卫（会把打错的函数名静默短路）——crypto 是浏览器内建，
        // 用真值性探测代替：randomUUID 缺席即 undefined → 走回退拼接。
        if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    } catch (_e) { /* 回退走下方拼接 */ }
    return "r" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 14);
}

/* 发送时的文字淡出幽灵：把输入框当前文字钉成一粒 fixed 定位的纯文本层
   在原位 240ms 淡出微升后自毁。真值的清空与状态流转照旧瞬时——幽灵只是视觉层的「话被收走」。
   版式（字体/行高/内边距/首行 chip 缩进）从输入框 computed style 现抄，保证与框内原排布重合。 */
function ubInputGhost(box) {
    if (!MOTION || !box) return;
    const val = String(box.value || "");
    if (!val) return;
    const r = box.getBoundingClientRect();
    if (!r.width) return;
    const cs = window.getComputedStyle(box);
    const g = document.createElement("div");
    g.textContent = val;
    g.setAttribute("aria-hidden", "true");
    const st = g.style;
    st.position = "fixed"; st.left = r.left + "px"; st.top = r.top + "px";
    st.width = r.width + "px"; st.height = r.height + "px";
    st.font = cs.font; st.lineHeight = cs.lineHeight; st.padding = cs.padding;
    st.textIndent = cs.textIndent;   // 上下文 chip 在场时首行从 chip 右侧起排（projects.js 同步的 inline indent）
    st.color = cs.color; st.whiteSpace = "pre-wrap"; st.wordBreak = "break-word";
    st.overflow = "hidden"; st.pointerEvents = "none"; st.zIndex = "30";
    document.body.appendChild(g);
    gsap.to(g, { autoAlpha: 0, y: -6, duration: 0.24, ease: "power2.out", onComplete: () => g.remove() });
    setTimeout(() => { if (g.parentElement) g.remove(); }, 700);   // 后台标签页 rAF 停摆兜底
}

function ubRouteBody(text, opts) {
    // 自包含：LLM 分流所需的现场（有无结果 / 当前查询 / 当前条件 / 来源池）逐次原样带上。
    opts = opts || {};
    const data = cbFrameData() || {};
    const cfg = getConfig();
    //后端 pre-loop 检索要按**当前检索参数**跑——top_k/rerank/recall/
    // strategy/分面/抑制/宽容/时间窗与 runRecommend 发 /api/recommend 同源构造（search.js
    // searchParamSnapshot），两处口径不许漂移。pl1b 收尾加 polish：后端 preliminary_final
    // 判定要看「AI 润色会不会跑」（b 档只在不润色时成立）。
    const body = Object.assign({
        utterance: text,
        has_results: !!((data.results || []).length),
        result_total: Number(data.result_total) || 0,
        query: cbFrameQuery(),
        current_filters: data.query_constraints || [],
        sources: cfg.sources,
        // LLM 配置覆盖（2026-08-03 设置三维度化）：use_llm 由 API 可用性门控
        // （llmCapable 单一判据——已配 key 必可用；mock 演示恒 true 供演示输出）。
        // 总开关退役前的两道闸是「AI 重排点不动」那类困惑的根。
        provider: cfg.provider,
        use_llm: cfg.use_llm,
        mock_llm: cfg.provider === "mock" && cfg.use_llm,
        api_key: cfg.api_key,
        base_url: cfg.base_url,
        model: cfg.model,
        // AI 执行开关（cfgAgentExec，2026-08-03 合并旧「说了就直接做」+「Agent 规划执行」）：
        // 后端在「装扩展 ∧ 本开关 ∧ LLM 可用」时走 langgraph 规划，否则自动回退 action_plan
        // 基础规划（契约 §2.2）。
        agent: cfg.agent
    }, searchParamSnapshot(text));
    // 建议动作 hint——任务卡模板**未经编辑**时由 ladder.js 显式传入
    // （编辑后不传 = 普通路由）；服务端 allowlist 校验在 action_plan 单一真源。
    if (opts.suggestedRecipe) body.suggested_recipe = opts.suggestedRecipe;
    // 环内放松卡作答（2026-09-14 ask 暂停/重启批）：作答作为新回合重新入环——
    // additive ask_answer 字段（kind 恒 "ask.relax"，服务端另有形状/净化闸），且
    // suppressed_constraints 覆写为所选选项的 suppress（服务端已合并既有忽略，
    // 整组替换语义与条件板「忽略此条件」同径）；pre-loop 与图 initial state 都吃覆写后值。
    if (opts.askAnswer) {
        body.ask_answer = opts.askAnswer;
        body.suppressed_constraints = (opts.askAnswer.suppress || []).slice();
    }
    return _attachArtifactCtx(body);
}

export async function ubSubmit(source, opts) {
    // 信息流：每提交一次（新检索 / 新对话）开一条新的过程轨迹——在此复位（不再挂 cbLogClear，
    //   那会被 display 落地路径的 `if(!keepConv) cbLogClear()` 中途清掉，轨迹就没了）。
    flowReset();
    // 新一轮提交清掉上一轮挂起的 ask.relax 选择卡（作答语义不属于这一轮；卡随预览区一并撤下）。
    if (_askRelaxPending) { _askRelaxPending = null; cbCancel(); }
    // 双来源：hero 主框（首屏）与侧栏微信式输入行（结果态唯一输入入口）共用这一条路由。
    // （additive）：opts 可选 {text（任务卡等程序化提交的文本，跳过读输入框）,
    //   suggestedRecipe, templateOriginated}——普通手打调用不传，行为与旧版逐位一致。
    opts = opts || {};
    // 微信语义：输入行**发送即清空、默认为空**；说过的话原样写进 #queryInput——
    // 它是「当前检索句」的唯一状态真源（runRecommend / 任务包 / 词表回显全读它）。
    const fromChat = source === "chat";
    const box = $(fromChat ? "chatInput" : "queryInput");
    const text = (typeof opts.text === "string" && opts.text.trim())
        ? opts.text.trim()
        : (box ? String(box.value || "").trim() : "");
    if (!text) { toast(fromChat ? "先说点什么再发" : "请先输入检索需求"); return; }
    // 在途闸：上一次检索（submitBtn.disabled）或上一次路由（_ubBusy）未落地不接新句——
    // 否则两次 runRecommend 并发，先落地的会被后落地的 cbPushCurrent 一并吞并。
    const _sb = $("submitBtn");
    if ((_sb && _sb.disabled) || _ubBusy) { toast("上一步还在处理，稍等一下再说"); return; }
    // consent 首次告知（S5）：开关开着且没同意过 → 弹窗拦截，不点确认不发送。
    // 开关关着（usageEnabled() 为假）不弹；'disable' → 关掉采集开关，本次照常发送。
    if (usageEnabled() && !usageConsentGiven()) {
        const r = await requestUsageConsent();
        if (r === "disable") usageSetEnabled(false);
    }
    if (fromChat) {
        const qi = $("queryInput");
        if (qi) qi.value = text;
    }
    // A1：「发送前主区是否已经在进行一段对话」（chat-in-main）必须在 say 上屏**之前**
    // 捕获——cbLogPush 之后 _cbLog 恒非空，cbChatInMain() 恒真，hero 首句与主区续聊再也分不清。
    // 这个值串到 ubDispatch 的 search 档：wasChat 时与 fromChat 同待遇（keepConv 保对话）。
    const wasChat = cbChatInMain();
    // 发送即清空（微信语义，2026-08-04）：说过的话由对话记录与「当前检索句」状态真源保管，
    // 输入框只是起草器——此前主框发完仍留原话，用户以为没发出去。需要回显的路径各自回填：
    // search 档 / requires_results 档在**结果落地时**由 runRecommend 回填（queryOverride 递句，
    // 在途窗口保持空框，owner 新指①）；fail-open 回退在 catch 即回填（框里的原话就是退路）。
    // 工具调用档（chat-in-main）不回填——起草器保持空白等你下一句。
    // 发送即空的视觉软化——真值仍瞬时清空（在途窗口语义不变）
    // 文字本身钉一粒 fixed 幽灵在原位淡出微升：「话被收走了」而不是「字没了」。仅首页大框；
    // 侧栏输入行维持微信式瞬时清空（聊天气泡已即时上屏，反馈足够）。
    if (!fromChat) ubInputGhost(box);
    box.value = "";
    autoGrow(box);
    // 零反应治理（p10 起双来源统一）：气泡**先上屏**再走路由——此前要等 /api/utterance 回来
    // 才见到自己这句。后续 ubDispatch 各分支与 runRecommend 的 say push 被 cbLogPush 的
    // 「连续同条去重」天然吞掉，不会双泡；进度泡幂等（已开不再开）。
    // 环内放松卡作答回合（opts.askAnswer，2026-09-14 ask 暂停/重启批）：用户不是「说了
    // 一句新话」，而是在选择卡上做了选择——对话记录按 refine 口径记所选（与条件板
    // cbCommit 的呈现同径），不把原查询当新话再 push 一遍。
    if (opts.askAnswer) cbLogPush("refine", String(opts.askAnswer.message || "") || text);
    else cbLogPush("say", text);
    const _cfg = getConfig();
    // 遥测轮次起点：所有用户提交都过 ubSubmit——search 与 conv
    // 共用这一个 tid，本轮内的 open/dl/fav/view 全归到它。usageLogSearch 里另有兜底。
    usageBeginTurn();
    // benchmark 采集（b1，默认关）：一轮的开头——用户原话 + 现场。后续 benchfbTurnRoute/
    // benchfbTurnSearch/benchfbTurnAction 各挂一段，闭环成一条记录。关着时这是一次布尔比较。
    benchfbTurnBegin(text, {
        source: source, convId: cbConvId(), scope: usageScope(),
        model: _cfg.model, provider: _cfg.provider, baseUrl: _cfg.base_url,
        agent: !!_cfg.agent, useLlm: !!_cfg.use_llm,
        rerank: _cfg.rerank, recall: _cfg.recall, strategy: _cfg.strategy,
        // 任务卡/chip 生成文本提交时携带 true/false（普通手打无此键）。
        templateOriginated: opts.templateOriginated,
    });
    const _bfT0 = Date.now();
    // 流式规划：AI 执行开且扩展可用时走 SSE——规划步骤随流实时上屏
    // （claudecode 式），行动流是主反馈；AI 执行关 / 扩展缺失时维持不确定态加载（无数字）。
    const streamAgent = !!(_cfg.agent && !agentExtMissing());
    // 进度泡：发送**即刻**收到一颗系统回复气泡。流式档给克制的不确定态「规划中…」，
    // 非流式档进度泡只有三点动画，真实回复落地时原位渐变成文字。
    cbProgressBegin(streamAgent ? "规划中…" : "");
    // 零反应治理：发送**即**开 loading（按钮 loading 态 + 进度泡三点，progress.js 镜像），
    // 覆盖「路由问句（可能含 LLM 判断）+ 检索」全程——此前进度要等路由回来、runRecommend 起跑才出现。
    // startProgress 对已在 loading 的按钮直接跳过（幂等），runRecommend 接手时不重启。
    // （产品方钦点）：流式档**同样起跑**；数字里程表退役
    // loading 只表达「还在干活」，行动流步骤实时上屏表达「干到哪步」，互不冒充。
    _sb.disabled = true;
    _sb.setAttribute("aria-busy", "true");
    startProgress(estimateDuration(_cfg) + 3000);   // +3s 给统一路由那次后端判断（可能含 LLM）
    _ubBusy = true;
    const myGen = ++_ubSeq;
    // preliminary 帧落地前的代际闸——环跑期间用户另起了检索（分面重跑等，_recSeq
    // 已自增）时，晚到的 pre-loop 先行帧不得盖新屏（search.js recSeqNow 只读取口）。
    const recGenAtSend = recSeqNow();
    let reply = null;
    // 流式播过的规划步数：>0 说明步骤已实时上屏（且会被 arxFinish 收进总结泡 details），
    // final 里的 plan 要打 _traceStreamed 去重标，actDispatchPlan 不再二次渲染 plan.trace。
    let streamStepCount = 0;
    //本流是否已把 pre-loop 先行结果放上屏（preliminary 帧到达过）——
    // final 三档分发（ubDispatch）要知道屏上是不是先行结果（徽标在不在）。
    let prelimShown = false;
    // 回复是否真的来自流式：只有这时才许打去重标——流式中途失败回退非流式后，
    // 重发回来的 plan.trace 一步都还没播过，误打标会让规划步骤从记录里整体消失。
    let replyFromStream = false;
    // 幂等请求号：本次提交一个号，流式与断流重发共用——服务端占用去重
    // 断流重发拿缓存结果而不再执行一遍。独立于 myGen/_ubSeq：那是上屏代际闸，与服务端无关。
    const reqId = ubNewReqId();
    try {
        if (streamAgent) {
            try {
                reply = await ubFetchStream(text, reqId, function (step, evKind) {
                    if (myGen !== _ubSeq) return;   // 已被更晚的一次提交取代，迟到的帧不再上屏
                    // pre-loop 第一阶段结果先行上屏——共享落地入口 +
                    // 「初步结果」徽标 + 进度泡换句「正在更深一步思考…」（均收口在 landRecommendResult
                    // 的 fromPrelim 档）。代际闸：发送时的检索代被取代（用户环跑期间另起检索）→ 丢帧。
                    if (evKind === "preliminary") {
                        if (recGenAtSend !== recSeqNow()) return;
                        if (!step || step.ok !== true) return;   // 畸形帧忽略（契约：recommend_payload 同形）
                        flowPushEvent("preliminary", step);      // 信息流：初步检索落地 → 轨迹「已完成初步检索」
                        prelimShown = true;
                        landRecommendResult(step, text, { fromPrelim: true, sentText: text });
                        return;
                    }
                    // 用户重申：上部只展示工具调用、一工具一行、除此以外什么都没有：
                    // 环内节点的过程展示**全部**归信息流工具行（flowPushEvent）——行动流（arx）
                    // 不在流式期间开播（它把分流共识/理解意图等非工具节点连同 detail 一起搬上屏，
                    // 正是用户点名的冗余）。真实执行（runner 派发）的行动流由 actDispatchPlan 自开。
                    if (evKind === "tool_start") {
                        flowPushEvent("tool_start", step);       // 轨迹 pending 行（状态机去重，同 id 只留一条）
                        return;
                    }
                    const label = String((step && (step.label_zh || step.node)) || "").trim();
                    if (!label) return;
                    flowPushEvent("step", step);   // 信息流：step 落定 → 轨迹同 id 更新（pending→done/failed，不 append）
                    streamStepCount += 1;   // 流式播过计数：final 的 plan 打 _traceStreamed 去重标，actDispatchPlan 不二次渲染 plan.trace
                }, opts);
                replyFromStream = true;
            } catch (streamErr) {
                if (myGen !== _ubSeq) return;
                // 流式失败不静默：撤掉已播的半成品规划流，按非流式原路径（下方 if (!reply)）再发一次——
                // 用户看到进度继续，请求一定落地，不吞掉这句话。
                // loading 从发送起就在跑（流式档也起跑，见上方），这里是**同一次路由的继续**——
                // 绝不摘了 loading 再 startProgress：那是同一在途请求的重启。
                if (streamStepCount) arxFinish();
                _cbProgLabel = "";   // 进度泡从「规划中…」回到纯三点（非流式回退后没有步骤文案可报）
            }
        }
        if (!reply) {
            // 断流重发（或非流式首发）：带同一个 reqId——服务端已在跑的那次收尾后，这里拿缓存体。
            const res = await fetch(API.utterance, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(Object.assign(ubRouteBody(text, opts), { req_id: reqId }))
            });
            reply = await res.json();
            if (!res.ok) {
                // 503（服务忙/在途请求过多）与 409（req_id 撞号）
                // 绝不 fail-open 成检索——操作句被悄悄当检索词是最伤的静默错。如实亮忙态、
                // 保留输入、用户手动重试；其余错误维持原 fail-open 检索哲学。
                if (res.status === 503 || res.status === 409) {
                    const busyErr = new Error((reply && reply.detail)
                        || (res.status === 409 ? "这次请求与另一次撞号了" : "服务忙，请稍后重试"));
                    busyErr.busy = true;
                    throw busyErr;
                }
                throw new Error((reply && reply.detail) || "没能读懂这句话");
            }
        }
        if (!reply || !reply.ok) throw new Error((reply && reply.detail) || "没能读懂这句话");
    } catch (err) {
        if (myGen !== _ubSeq) return;
        _ubBusy = false;
        if (err && err.busy) {
            // 忙态上屏：原话留在框里（发送即清空的兜底回填），用户看着重发即可。
            ubFillQuery($("queryInput"), text, text);
            cbLogPush("sys", "系统正忙：" + err.message + "。你的原话已留在输入框，稍后再发一次即可。", { isError: true });
            benchfbTurnError("busy: " + err.message);   // 采集：这轮以「忙」告终（不会再有检索/动作落地）
            _fireCtxAfterSend(false);   // 上下文卡：发送未成功 → 卡保留 + 如实标注（projects.js 处理）
            return;
        }
        // 路由不可用（旧后端 / 网络故障 / 后端 500）：退回这个框的默认语义——按原话检索一次。fail-open，
        // 与后端「LLM 缺席走规则兜底」同哲学；改条件/执行类的话退化成字面检索，不比统一前的主框更坏。
        // 但绝不静默（2026-08-15 触发点操作句被当检索词是最伤的静默错——亮一条 sys 错误泡
        // 如实告知「没走通、按原话检索了」，console 留痕，用户知道操作没执行、可稍后重试。
        // 发送即清空后 runRecommend 从主框取查询，先把原话填回去（B：在途草稿守卫，见 ubFillQuery）。
        console.warn("utterance route failed; falling back to literal search", err);
        cbLogPush("sys", "没走通 AI 分流（" + err.message + "），已按原话直接检索。想执行操作请稍后重试。", { isError: true });
        // 采集留痕：在途轮次挂错误注记但不收尾——随后的检索段仍并入同一条，
        // 反馈包里看得出这轮路由层失败过（此前 fail-open 后无任何记录痕迹）。
        benchfbTurnNote("route fail-open: " + err.message);
        // ov1-tel1：路由层失败也进 usage 事件流（stage:"route" 与检索失败 stage:"search" 分开算）。
        usageLog(USAGE_KINDS.err, { msg: String((err && err.message) || err || "").slice(0, 80), stage: "route" });
        ubFillQuery($("queryInput"), text, text);
        runRecommend();
        _fireCtxAfterSend(false);   // 上下文卡：fail-open 成普通检索，卡内容没随消息走 → 保留 + 如实标注
        return;
    }
    if (myGen !== _ubSeq) return;   // 已被更晚的一次提交取代
    _ubBusy = false;
    // 流式去重标：步骤已随 SSE 播过，actDispatchPlan 跳过 plan.trace 二次渲染。
    if (replyFromStream && streamStepCount && reply.plan) reply.plan._traceStreamed = true;
    //本流上过先行结果 → 告诉 ubDispatch（final 三档：换屏摘徽标 / 免二次检索摘徽标 /
    // 现状 runRecommend 落地摘徽标）。
    if (prelimShown && reply) reply._prelimShown = true;
    // 采集：路由应答全量入档（agent 路径的 plan.trace 逐步执行记录随 reply.plan 一起来）。
    benchfbTurnRoute(reply, { ms: Date.now() - _bfT0, streamed: replyFromStream });
    ubDispatch(text, reply, fromChat, wasChat);
    _fireCtxAfterSend(true);   // 上下文卡：发送成功 → 自动移除卡（projects.js 统一清空双挂点）
}

/* 流式路由请求：POST /api/utterance 带 additive stream:true → text/event-stream。
   reqId随体上传：服务端按它占用——本流若中途断，ubSubmit 的非流式重发
   带同一号，拿的是这次路由收尾后的缓存体，不会再执行一遍。
   手动解析 SSE：帧界是空行（\n\n），帧可能跨 chunk——只在缓冲里拼出完整帧后再按
   「data: 」行解析 JSON。事件五档：step（agent 各节点真实记录 {node,label_zh,detail,ok,ms}，
   逐条喂 onStep 实时上屏）→ preliminary / tool_start（新帧，同走 onStep 第二参
   分种类）→ final（与非流式**同形**的
   完整响应，含 route/plan/echo_zh/agent，additive 带 result_payload/
   preliminary_final 两键）→ error（{detail}，视为协议失败抛给调用方走回退）。
   只在**传输/协议**层面失败时抛错（非 2xx / 断流 / 收不到 final）；final 里 ok:false 是
   业务回答，原样交回，由 ubSubmit 统一的 !reply.ok 出口处理——不为它多发一次请求。
   签名尾部加可选 opts（透传 ubSubmit 的 suggestedRecipe 等
   与非流式路径同一 ubRouteBody(text, opts) 口径）；静态钉锚同步更新。 */
async function ubFetchStream(text, reqId, onStep, opts) {
    const res = await fetch(API.utterance, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign(ubRouteBody(text, opts), { stream: true, req_id: reqId }))
    });
    if (!res.ok || !res.body) throw new Error("流式路由不可用（HTTP " + res.status + "）");
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "", finalReply = null, errDetail = "";
    for (;;) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buf += decoder.decode(chunk.value, { stream: true });
        let cut;
        while ((cut = buf.indexOf("\n\n")) >= 0) {
            const frame = buf.slice(0, cut);
            buf = buf.slice(cut + 2);
            frame.split("\n").forEach(function (line) {
                if (line.indexOf("data: ") !== 0) return;   // 注释行/事件名行不消费（后端只发 data 行）
                let msg = null;
                try { msg = JSON.parse(line.slice(6)); } catch (_e) { return; }   // 坏帧跳过，不毒化整流
                if (!msg) return;
                if (msg.event === "step" && msg.data) onStep(msg.data, "step");
                else if (msg.event === "preliminary" && msg.data) onStep(msg.data, "preliminary");
                else if (msg.event === "tool_start" && msg.data) onStep(msg.data, "tool_start");
                else if (msg.event === "final") finalReply = msg.data;
                else if (msg.event === "error") errDetail = String((msg.data && msg.data.detail) || "路由失败");
                // 其余事件种类一律忽略（§8：additive 演进——旧前端对新流完全兼容，不留硬耦合）
            });
        }
    }
    if (errDetail) throw new Error(errDetail);
    if (!finalReply) throw new Error("流式响应没有 final 帧");
    return finalReply;
}

/* 三档路由分发（turn pipeline：search / tool / none，后端 /api/utterance 是唯一路由脑）。
   「框里的草稿」与「产生当前结果的那句话」两个真源保持分离：只有 search 档才动框
   （写入 effective_query——LLM 据当前查询+条件改写的整句，改写必须回显），且动框必经
   ubFillQuery 的在途草稿守卫（B）。 */
/* B回填守卫：发送即清空后，路由在途期间用户可能已在框里打了下一句草稿。
   回写只在「框是空的」或「框里仍是被发送的那句」时发生——否则原样保留草稿，绝不把用户
   正在打的字冲掉。search 档 / requires_results 档的回填已挪到结果落地时（search.js
   _ubLandingFill，同口径守卫），本函数现只服务 catch 回退档（路由挂了、按原话 fail-open
   检索前把原话填回框）。 */
function ubFillQuery(input, value, sentText) {
    if (!input) return;
    const cur = String(input.value || "").trim();
    if (cur && cur !== String(sentText || "").trim()) return;   // 框里是别人的草稿 → 保留
    input.value = value;
}
/* 覆盖策略：统一应用 selectDisplayBatch 的决定。search a 档与 route=tool 档
   共用：两档都说同一套「严格更高级才自动换屏 / 同 scope 去重 / 换词批备选 / 未知 trace 不覆盖」。
   - display：落地 decision.view（prefetched 零网络、淡入、keepProgress 按住进度泡）；留痕等
     runRecommend 推出最终帧后再 push（同步 push 会挂到 preliminary 先行帧、帧 id 错位）。
   - dedupe / alternate：不重渲结果区（保持当前屏）——摘徽标、撤进度、如实回执；弱批直接丢弃
     （不作任何形式展示），不再刷新 batchBar（结果头部件整体退役）。 */
/* 结果 pill 文案 = 实际命中/生效的关键词——把 query_constraints（同 _queryHits 的
   {dim,label,values} 形状）里「生效」的取值用顿号连起来（如「FASTQ、乳腺癌」），太长截断；
   取不到 → 空串，调用方回退原 query（现状）。只用 include（正向硬条件）：排除/优先/被忽略的
   不是「命中的关键词」（用户点名的「实际命中/生效」）。 */
function flowHitKeywords(qc) {
    if (!Array.isArray(qc) || !qc.length) return "";
    const seen = Object.create(null);
    const parts = [];
    qc.forEach(function (c) {
        if (!c || typeof c !== "object") return;
        if (String(c.polarity || "include") !== "include") return;
        (c.values || []).forEach(function (v) {
            const s = String(v || "").trim();
            if (!s || seen[s]) return;
            seen[s] = true;
            parts.push(s);
        });
    });
    if (!parts.length) return "";
    const joined = parts.join("、");
    // 太长截断：超长整体截断、留尾部省略号（视觉省略仍由 .ft-pill-q 的 ellipsis 兜底）。
    return joined.length > 40 ? joined.slice(0, 39) + "…" : joined;
}

/* 本轮存活批 → 结果 pill（渲染在回执气泡内部；一个 query 一个）。
   onlyActive=true 时只留当前活跃批（alternate/dedupe：弱批已丢弃，不作任何展示）。 */
function _flowPillsFrom(batches, activeBatchId, onlyActive) {
    if (!Array.isArray(batches)) return null;
    const out = [];
    batches.forEach(function (b) {
        if (!b) return;
        const id = String(b.batch_id || "");
        const isOn = Boolean(id) && id === String(activeBatchId || "");
        if (onlyActive && !isOn) return;
        const p = (b.payload && typeof b.payload === "object") ? b.payload : {};
        // 优先用实际命中/生效的关键词作 pill 文案（拿不到 → 回退原 query，现状）。
        const kw = flowHitKeywords(p.query_constraints);
        out.push({ batchId: id,
            label: kw || String(b.query_effective || b.label || b.query_raw || "检索结果"),
            count: Array.isArray(p.results) ? p.results.length : null,
            // 零命中标记（payload.results 空数组）——渲染成视觉区分的 pill，
            // 且仅在该批是最新结果时显示「点击处理」；否则点击无反应（救回链退役）。
            zero: isZeroHitBatch(b),
            active: isOn });
    });
    return out.length ? out : null;
}

function _applyBatchDecision(text, reply, decision, opts) {
    opts = opts || {};
    const q = String(opts.q || "").trim();
    const rewritten = !!opts.rewritten;
    // 不用 typeof x === "function"（board 静态门禁禁它：会把打错的函数名静默短路）；用 instanceof Function 同义。
    const dispatchAction = (opts.dispatchAction instanceof Function) ? opts.dispatchAction : null;
    /* 唯一气泡规则——纯检索计划：检索回执是唯一气泡（actFinish 由 _execReceiptCovered 抑制）；
       混合计划：actFinish 是唯一气泡（本函数不推检索回执泡）。无计划（plan 空）照旧推检索回执。
       判定看规范派发清单（actCanonicalDispatchPlans）——plan.steps 是「后端已在图内执行」的
       记录，拿它判会误当纯检索；清单内部已滤除「图内已执行的检索」头部项（真实混合轮顶层
       plan 就是已跑完的 rank，真身动作在 pending_frontend），这里拿到的即是真实待派发动作。 */
    const _execPlans = actCanonicalDispatchPlans(reply && reply.plan);
    const _actWillReceipt = !!dispatchAction && plansNeedActReceipt(_execPlans);
    if (_execPlans.length && !_actWillReceipt && dispatchAction) _execReceiptCovered = true;
    if (decision && decision.mode === "display" && decision.view) {
        // supersede 即丢弃：落地前先把被胜者 supersede 的批从视图里剔掉——只展示一份最终结果，
        // 被覆盖批从数据（result_batches）到渲染（pill/切换器）整个不存在。
        const _view = Object.assign({}, decision.view, {
            result_batches: _discardSuperseded(decision.view.result_batches, decision.activeBatchId),
        });
        // 结果 pill（存活批，可多 query 多 pill）持件——随本轮回执（或 actFinish）领取。
        flowSetPills(_flowPillsFrom(_view.result_batches, decision.activeBatchId, false));
        const _a = { queryOverride: String(decision.query || "").trim() || q || text, sentText: text,
            handSubmit: true, prefetched: _view, fadeIn: true, keepProgress: true,
            sayText: text,   // say 恒记用户原话（缺省时 search.js 回落 query=活跃批检索词，把用户说的话换成检索词）
            planSteps: opts.planSteps };
        // 换屏留痕优先用采纳批随行的确定性披露句（r2p：哪些词没被理解/被丢弃）；批上无
        // disclosure_zh 时回退既有通用句（无披露句的采纳批仍要有留痕）。只许真正升级才说
        // 「已更新」——dedupe/alternate 走下方否则分支，不再进这里。
        // 补网：改写披露不再单独成泡（一个动作两颗泡正是用户投诉的形态），并入回执正文与
        // 事实包 note，由 LLM 一句话合并说清；fail-open 时确定性句同样完整携带全部信息。
        const _aNote = function () {
            if (_actWillReceipt) return;   // 混合计划——唯一气泡归 actFinish，本处不推
            let _disc = "";
            if (decision.activeBatchId && Array.isArray(reply.result_batches)) {
                const _ba = reply.result_batches.find(function (b) {
                    return String((b && b.batch_id) || "") === String(decision.activeBatchId);
                });
                _disc = String((_ba && _ba.disclosure_zh) || "").trim();
            }
            const _note = (rewritten ? "我把这句按「" + (String(decision.query || "").trim() || q) + "」检索。" : "")
                + (_disc || "深入思考后找到了更匹配的结果，已更新。");
            const _e = cbLogPush("sys", _note);
            // 回执同样走 LLM 原位改写（事实取自屏上真实落地的活跃批 payload；
            // legacy 单批没有 result_batches——_view 本身就是该批的 recommend 响应，同形直用）。
            const _ab = _batchById(_view.result_batches, decision.activeBatchId);
            if (_e) {
                cbFetchSearchReply(_e, cbSearchReplyFacts(_ab ? _ab.payload : _view, text,
                    String(decision.query || "").trim() || q || text, _note));
            }
        };
        const _run = function () {
            if (opts.fromChat || opts.wasChat) {
                return runRecommend(Object.assign({ keepConv: true, sayPushed: true }, _a)).then(_aNote);
            }
            return runRecommend(Object.assign(_a, rewritten ? { sayText: text } : {})).then(_aNote);
        };
        if (dispatchAction) { _run().then(function () { dispatchAction(_actWillReceipt ? LAST_RECOMMEND_DATA : null); }); } else { _run(); }
        return;
    }
    // dedupe / alternate：结果区不重渲（保持当前屏），只摘徽标、撤进度、如实回执。
    if (decision && decision.stripPrelimBadge) setPrelimBadge(false);
    cbProgressDrop();
    finishProgress();
    const _bb = $("submitBtn");
    if (_bb) { _bb.disabled = false; _bb.removeAttribute("aria-busy"); }
    // 结果 pill——当前存活批（弱批已丢弃，onlyActive 只留活跃批；view 为 null 时用 mergedBatches）。
    if (decision && Array.isArray(decision.mergedBatches) && decision.mergedBatches.length) {
        flowSetPills(_flowPillsFrom(decision.mergedBatches, decision.activeBatchId, true));
    }
    // 混合计划时唯一气泡归 actFinish，检索回执不推（否则两颗气泡）。
    // 补网：去重/备选的如实回执（「没有更优结果、保持不变」类）同样接 LLM 原位改写——
    // 事实取自仍在屏上的活跃批；note 把「没有变化」的语义原样带给 LLM，绝不许它说成「已更新」。
    if (!_actWillReceipt && decision && decision.sysText) {
        const _e = cbLogPush("sys", decision.sysText);
        const _ab = _batchById(decision.mergedBatches, decision.activeBatchId);
        if (_e && _ab) {
            cbFetchSearchReply(_e, cbSearchReplyFacts(_ab.payload, text,
                String(decision.query || "").trim() || text, decision.sysText));
        }
    }
    // alternate（换词/未知 trace 的弱批）不再作「备选 pill」——弱批直接丢弃（不展示也不存储），
    //   保持当前屏 + 如实回执（ALTERNATE_SYS_TEXT 已改不含「上方切换」）。不再刷新 batchBar。
    if (dispatchAction) { dispatchAction(_actWillReceipt ? LAST_RECOMMEND_DATA : null); } else { ubStreamRunSettle(); }
}

/* supersede 即丢弃：剔除被胜者（activeBatchId 那批）覆盖的批。胜者保留；其余若被
   shouldDiscardOutcome 判为 supersede → 丢弃；跨 query / 无判定 → 保留（防御）。 */
function _discardSuperseded(batches, activeBatchId) {
    if (!Array.isArray(batches)) return batches;
    const winner = batches.find(function (b) {
        return String((b && b.batch_id) || "") === String(activeBatchId);
    });
    if (!winner) return batches;
    return batches.filter(function (b) {
        if (String((b && b.batch_id) || "") === String(activeBatchId)) return true;
        return !shouldDiscardOutcome(b, winner);
    });
}

/* ============ 零命中自动弹 ask.relax 卡（收编后唯一救回触点，2026-09-14） ============
   「放松哪个条件」收编为单一通道后，非 agent 路径的零命中也走同一张卡：
   最新活跃 pill 是零命中批 → 从批载荷派生 ask.relax 选择卡（deriveRelaxCard 纯函数），
   渲进 #cbPreview（askRelaxShow 唯一渲染口），作答走 cbChoose askrelax 分支。
   作答按卡的来源分流（2026-09-14 ask 暂停/重启批）：自动卡（带 _autoBatchId，非环内、
   可能 AI 关）走 cbCommit 零 LLM 重搜旧路径；环内卡（LLM 触发）把作答作为新回合
   经 ubSubmit 重新入环（ask_answer 字段），由环裁决上屏/finish/再追问。
   两条不变量守住与环内通道的竞态：
     ① 环内优先——_askRelaxPending 非空（环内已弹卡/卡在屏上）时不补发、不覆盖；
     ② dismiss 记忆——自动卡点过「都不合适，保持原样」的批记入 _relaxDismissed，
        同批不再自动弹（zero pill「点击处理」显式重开时清掉该批记忆）。
   状态只是展示层：不进 _cbLog、不落盘、不占对话流。 */
let _relaxDismissed = new Set();   // 自动卡被 dismiss 过的 batchId（本页面会话内有效）

/* 取「最后一个带 pill 的回执 entry」的活跃 pill——即最新结果（与 _flowPillsFrom 同源判定）。 */
function _latestActivePill() {
    for (let i = _cbLog.length - 1; i >= 0; i--) {
        const e = _cbLog[i];
        if (!e || !Array.isArray(e.pills) || !e.pills.length) continue;
        const act = e.pills.find(function (p) { return !!p.active; });
        if (act) return act;
    }
    return null;
}
function _findBatchById(batchId) {
    const d = LAST_RECOMMEND_DATA;
    if (!d || !Array.isArray(d.result_batches)) return null;
    return d.result_batches.find(function (b) {
        return String((b && b.batch_id) || "") === String(batchId || "");
    }) || null;
}
/* 渲染侧同步：最新 pill 是零命中批 → 兜底弹 ask.relax 选择卡；否则不动（卡的去留
   由用户作答/新提交各路径管理，不在这里强行收）。 */
function maybeAutoRelaxCard() {
    const act = _latestActivePill();
    if (!act || !act.zero) return;
    const bid = String(act.batchId || "");
    if (!bid || _relaxDismissed.has(bid)) return;
    if (_askRelaxPending) return;   // 环内通道已弹卡（或自动卡已在屏上）——环内优先，不抢不覆盖
    _autoRelaxCardOpen(bid);
}

/* 组卡并弹出（自动兜底与 zero pill 显式重开共用；卡上带 _autoBatchId 供 dismiss 记忆）。 */
function _autoRelaxCardOpen(batchId) {
    const card = deriveRelaxCard(_findBatchById(batchId));
    if (!card) return;   // 无放松选项（死维不给选 / 分面收窄到 0）→ 不弹
    card._autoBatchId = String(batchId || "");
    askRelaxShow(card);
}

function ubDispatch(text, reply, fromChat, wasChat) {
    const route = String((reply && reply.route) || "");
    /* 信息流：流已收尾（ubDispatch 在流结束后才被调），所有工具行都已记录 →
       立即压缩（只减不增），压缩快照由本轮回执（下一颗 sys）领取、渲染在气泡上方。
       分流本身不是工具调用，不上行（「一工具一行，除此以外什么信息都没有」）。 */
    flowFinish();
    // 环内 ask.relax 弹卡（2026-09-14 YOLO 批）：服务端只在真发起询问时带 ask_relax 键；
    // 选择卡渲进 #cbPreview，作答走 cbChoose 的 askrelax: 分支。键缺席时与现状逐位一致。
    if (reply && reply.ask_relax) askRelaxShow(reply.ask_relax);
    const _planSteps = (reply && reply.plan && reply.plan.steps) || null;
    // 2026-08-18 rescue 去重（评估迁移步骤 1）：final 帧 plan.steps 沿 runRecommend →
    // landRecommendResult 透传到 maybeSearchRescue——环内已提议过 search.rerun/rerank 就跳过
    // 端点补发；非 utterance 环路径（分面/历史/缓存直发 /api/recommend）没有 plan，恒 null。
    if (route === "search") {
        ubStreamRunSettle();          // 防御性收尾（流式不开 arx，恒 no-op）
        const q = String((reply && reply.query) || "").trim() || text;
        const rewritten = q !== text;
        const _prelim = !!(reply && reply._prelimShown);   // 屏上是 pre-loop 先行结果（「初步结果」徽标在）
        const _rpay = (reply && reply.result_payload) || null;
        usageLog(USAGE_KINDS.conv, { mode: "chat" });
        //final 三档。
        // preliminary_final 分支必须**先于**
        // 通用 result_payload 分支判定——后端结果批常驻且仅 preliminary 批时，legacy
        // result_payload 也非 None（镜像活跃批，turn.py 组卷收尾），若 a 档先判就会把屏上已在的
        // 同一批初步结果重复上屏、误报「已更新」。后端 preliminary_final 由独立的 loop_payload
        // 哨兵推导（环内真有采纳批时为 false），前置不会误压真正的环内采纳结果。
        // b：环跑完没有更优结果（preliminary_final=true）——屏上的先行结果就是最终结果：
        // 摘徽标、loading 收尾；不调 /api/recommend。
        // 2026-08-30 修复（用户报 bug「回复突然消失了」）：此前这里 cbProgressDrop() 把进度泡
        // **静默撤下**——先行帧落地时 keepProgress 按住没说话（cbProgressDone 跳过），final 又不说话，
        // 整段对话只剩用户自己的气泡、一颗系统回复都没有（pill 也因无回执领取而丢失）。
        // 改为进度泡原位蜕变成如实完成句（与 a 档「找到了更匹配的」对仗），pill 随回执正常领取。
        if (reply.preliminary_final === true && _prelim) {
            setPrelimBadge(false);
            const _bNote = "深入思考后没有更匹配的结果——屏上这批就是最终结果。";
            const _bEntry = cbProgressDone(_bNote);
            // 补网：b 档收尾回执同样接 LLM 原位改写（事实=屏上先行批，LAST_RECOMMEND_DATA）。
            if (_bEntry) {
                cbFetchSearchReply(_bEntry, cbSearchReplyFacts(LAST_RECOMMEND_DATA, text,
                    String((reply && reply.query) || "").trim() || text, _bNote));
            }
            finishProgress();
            const _bb = $("submitBtn");
            if (_bb) { _bb.disabled = false; _bb.removeAttribute("aria-busy"); }
            return;
        }
        // polish-on 面：preliminary_final 的 b 档判定含「润色不会跑」
        // （turn.py 全与保守），polish 开时仅 preliminary 批也 preliminary_final=false——
        // 镜像载荷会落进 a 档：同一批初步结果重复上屏 + 误报「已更新」+ 跳过本该跑的润色。
        // 判别真源是结果批本身：活跃批 kind=preliminary 且屏上正是先行帧 → result_payload 只是
        // 该批镜像、不是环内采纳，a 档不得截胡——落 c 档照旧 runRecommend（既有行为：
        // 润色照跑、落地摘徽标、无「已更新」）。环内真有采纳时活跃批是 rank/rerank 等环内批，
        // 本闸不压（无结果批的旧帧/非 agent 帧 batches 恒缺，判别短路为 false，行为逐位不变）。
        // a：环内采纳了结果（含 preliminary 镜像/去重/备选），统一交 selectDisplayBatch 决定——
        // 严格更高级才自动换屏；同 scope 去重不追加、换词批作备选、未知 trace 不自动覆盖。
        if (_rpay && _rpay.ok === true) {
            _applyBatchDecision(text, reply, selectDisplayBatch(reply, LAST_RECOMMEND_DATA), {
                planSteps: _planSteps, fromChat: fromChat, wasChat: wasChat, rewritten: rewritten, q: q,
            });
            return;
        }
        // c：其余（现状）——照旧 runRecommend（LLM 重排/润色或改写后的查询），落地即摘徽标。
        cbProgressRelabel("检索中…");  // 不确定态换句：规划落地、进入检索（非流式恒空，不动 % 画像）
        // owner 新指①：发送即空必须贯穿**整个在途窗口**——此前路由一落地就把检索句
        // 写回框，在 chat-in-main（框=对话起草器）下看起来像「话没发出去、原话又回来了」。
        // 检索句改由 opts.queryOverride 显式交给 runRecommend（框不再是它的取数口）；
        // 输入框等**结果落地**才由 runRecommend 回填成当前检索句（任务包/可行性读框的真源职责不变），
        // 且回填过同一在途草稿守卫（B：框里已是新草稿就不回写）。
        if (fromChat || wasChat) {
            // 对话窗来的、或 chat-in-main 主区已有对话时主框来的检索指令：对话记录保留（keepConv），
            // 原话 say 已在 ubSubmit 上屏（sayPushed）；LLM 改写了检索句 → 如实回显。
            // （修复）：改写回显不再提前 push 一颗独立 sys（那会挂到 preliminary 先行帧
            // 渲染时 ≠ 最终帧误显示「查看历史回复」），改作 _cbProgHint 前缀——runRecommend 落地时
            // 由 cbPushCurrent 的 cbProgressDone 拼进完成句（「我把这句按…检索；检索完成：…」），
            // 挂最终帧、语序照旧。
            // A1：wasChat 漏掉时，chat-in-main 主框发检索句会把前段工具对话清掉（keepConv 只看 fromChat）。
            if (rewritten) _cbProgHint = "我把这句按「" + q + "」检索。";
            runRecommend({ keepConv: true, sayPushed: true, queryOverride: q, sentText: text, handSubmit: true, planSteps: _planSteps });
        } else {
            // hero 首句：runRecommend 会清对话重开时间线——sayText 让开头 say 记**用户原话**，
            // 改写句由落地完成句的前缀如实交代（同 hint 通道，挂最终帧、不误显示历史链接）。
            // handSubmit:true：统一框是用户亲手提交的主检索路径，
            // 不带它 usageLogSearch 的闸全拒，「检索 N 次」恒 0（v2 的速度/弃权维度跟着饿死）。
            if (rewritten) _cbProgHint = "我把这句按「" + q + "」检索。";
            runRecommend(Object.assign({ queryOverride: q, sentText: text, handSubmit: true, planSteps: _planSteps }, rewritten ? { sayText: text } : {}));
        }
        return;
    }
    if (route === "tool") {
        /* 三 flag（scoped 路由/RAG 工具/多批）ON 时检索改由
           环内 rank/rerank 工具完成，整轮以 route=tool（EXEC plan）收尾——但后端契约里
           result_payload 恒为「环内上屏批」、result_batches/active_batch 随行（turn.py 组卷收尾；
           RAG 工具 ON 即使 MULTI_BATCH OFF 也会有 loop_payload）。tool 档若不消费它们，屏上永远停在
           pre-loop 先行批（「初步结果」徽标也摘不掉），环内采纳批与结果批切换器全丢（真机核实：换条件
           一轮后屏上仍是上一轮结果）。消费口径（与 search 档三档同一纪律）：
           · 屏上先行帧已是最终活跃批（仅 preliminary 批）→ 只摘徽标，不重复上屏、不报「已更新」；
           · 否则把活跃批落地（prefetched 零网络、淡入同 search a 档），result_batches/active_batch
             并入视图对象——切换器（results.js renderBatchSwitcher）随之对真实多批响应渲染；
             落地查询取活跃批 query_effective（实际管线输入），不动 query_raw（本轮原话只作结果批溯源）。
           落地只管结果区；执行流 trace 卡照旧由 ubDispatchAction 派发渲染，两不相扰。 */
        const _tpay = (reply && reply.result_payload) || null;
        // （修复）：tool 档「活跃批落地」是**异步**推帧（runRecommend 内部 await
        // SOURCES_READY 后才 landRecommendResult → cbPushCurrent 推最终帧）。若在此同步调
        // ubDispatchAction，actFinish 的总结 sys 会挂到 preliminary 先行帧，而 runRecommend
        // 落地又推了新帧 → sys.frameId ≠ 当前帧 → 误显示「查看历史回复」（实测复现：纯检索
        // 走环内 rank 的 route=tool 路径，sys「检索数据集完成。」下方出链接）。故把派发延后到
        // runRecommend 落地（promise resolve）后执行；摘徽标/去重/备选分支不推新帧，直接同步派发。
        const _dispatchAction = function (searchData) { ubDispatchAction(text, reply.plan || null, fromChat, wasChat, searchData); };
        if (_tpay && _tpay.ok === true) {
            // 覆盖策略：与 search a 档共用同一选择函数——严格更高级才
            // 自动换屏；同 scope 去重不追加；换词批作备选（轻量刷新 batchBar 加非活动 pill）；
            // 未知 trace 不自动覆盖。exec 计划仍照常派发（互不干扰）。
            _applyBatchDecision(text, reply, selectDisplayBatch(reply, LAST_RECOMMEND_DATA), {
                planSteps: _planSteps, fromChat: fromChat, wasChat: wasChat, rewritten: false, q: "",
                dispatchAction: _dispatchAction,
            });
            return;
        }
        _dispatchAction();
        return;
    }
    // none / 未知路由：如实回音。needs_agent（「AI 执行」关 + 规则检出操作指令）→ 降级气泡，
    // 带指路按钮（有 key「去开启 AI 执行」/ 无 key「去配置 API」）；其余是普通灰泡。
    ubStreamRunSettle();   // 流式规划档：回音即终点，规划流就地折叠
    // R2-9 P1-4：echo_zh 缺失 ≠ 没听懂——后端契约里 none 路由恒带 echo_zh，拿不到就是
    // 服务/传输层异常（半截响应、旧后端字段漂移）。把故障说成「我没有听懂」是错误归因：
    // 用户会去改说法，而问题根本不在说法上。分口径 + 错误样式气泡，与正常灰泡一眼可分。
    let echo = String((reply && reply.echo_zh) || "");
    let echoError = false;
    if (!echo) {
        echoError = true;
        echo = "这次没有拿到服务的正常回应（网络中断，或服务出了故障），什么都没有做。稍后再发一次试试。";
    }
    // C1：LLM 缺席的规则兜底回音（via=rule）末尾那句「可以再发一次试试」
    // 在 mock/无 key 下是死路——重试永不成功。API 不可用时换成如实指路：去设置里配 API。
    // llmCapable 为假时 route=none 且非 needs_agent 的回音只可能来自这条兜底
    // （LLM 真判的「没听懂」以大模型在场为前提），换掉不是误伤。
    if (!echoError && !llmCapable() && !(reply && reply.needs_agent)) {
        echo = "大模型没有接上：还没有配置可用的 API。检查更新、删除、联网搜这类操作必须大模型在场才能判断——"
            + "到「设置 → AI / API 配置」选好服务商并填好密钥后，再把这句说一次就行。";
    }
    // 婉拒候选 chips（2026-08-09 五机制批）：LLM 真判 none 的死胡同回音下方给 2~3 颗可点候选
    // （后端机械生成、契约在 turn.route_turn），点击即把那句重新入环——分流代替硬拒。
    // needs_agent（指路按钮已在）与 echoError（传输故障）两档不带。html 不进落盘（cbLogForHistory
    // 只存 k/t/n），回看降级为纯文字——与既有纠错 chips 同口径。
    let noneOpts = (reply && reply.needs_agent) ? { needsAgent: true } : (echoError ? { isError: true } : undefined);
    const _sug = (reply && reply.suggestions) || [];
    if (!echoError && !(reply && reply.needs_agent) && _sug.length) {
        const chips = _sug.slice(0, 3).map(function (s) {
            return '<button type="button" class="btn act-chip" data-act-say="'
                + escapeHtml(String((s && s.utterance) || "")) + '">'
                + escapeHtml(String((s && s.label) || "")) + "</button>";
        }).join("");
        noneOpts = noneOpts || {};
        noneOpts.html = '<div class="act-fix act-suggest"><span class="act-suggest-lead">你可以试试：</span>' + chips + "</div>";
    }
    cbLogPush("sys", echo, noneOpts);
    benchfbTurnEcho();   // 采集：none 路由（没听懂/婉拒/指路）收尾——这类正是 benchmark 的硬数据
    resetSubmitButton();
}

/* 行动流收尾保险（流式路径下基本是 no-op）：流式期间 arx 不再开播（过程展示归信息流工具行），
   行动流只由 actDispatchPlan 的 runner 派发自开自收；本函数防御性折叠任何残留的未收口流。 */
function ubStreamRunSettle() {
    if (arxActive()) arxFinish();
}

function ubDispatchAction(said, plan, fromChat, wasChat, searchData) {
    // 后端 tool 路由只发 EXEC plan；词表与派发表漂移时如实回音，不瞎做。
    if (!plan || plan.kind !== "exec") {
        ubStreamRunSettle();   // 流式规划档：这里已是终点，规划流就地折叠
        cbLogPush("sys", "没能读懂这一步要做什么，什么都没有做。");
        benchfbTurnEcho();   // 采集：tool 路由却没产出可执行计划——如实按回音收尾
        resetSubmitButton();
        return;
    }
    // 取消态（后端恒带 cancelled 字段）：动词照留但执行层不得执行——只在对话流里回音，
    // 不打开任何面板、不出执行回执。
    if (plan.cancelled) {
        ubStreamRunSettle();   // 流式规划档：取消回音即终点，规划流就地折叠
        usageLog(USAGE_KINDS.conv, { mode: "chat" });
        cbLogPush("sys", String(plan.reason_zh || PLAN_CANCELLED_FALLBACK_ZH));
        benchfbTurnEcho();   // 采集：取消回音（婉拒/否定句）同属「系统没动手」轮
        resetSubmitButton();
        return;
    }
    /* 兼容旧 plan.intents 清单：取消项**只回音**（与单 plan 取消档同一文案真源），
       执行项只用于先检索 requires_results 闸和全取消收尾；最终派发清单由 act.js 统一规范化。
       Wave 3 后端不再生产 intents，此段在现行 plan 上自然为空。 */
    const _intents = Array.isArray(plan.intents)
        ? plan.intents.filter(function (p) { return p && p.kind === "exec"; })
        : null;
    const _activeIntents = [];
    if (_intents && _intents.length) {
        _intents.forEach(function (p) {
            if (p.cancelled) cbLogPush("sys", String(p.reason_zh || PLAN_CANCELLED_FALLBACK_ZH));
            else _activeIntents.push(p);
        });
        if (!_activeIntents.length) {   // 全取消：回音即终点（与单 plan 取消档同收尾）
            ubStreamRunSettle();
            usageLog(USAGE_KINDS.conv, { mode: "chat" });
            benchfbTurnEcho();
            resetSubmitButton();
            return;
        }
    }
    const data = cbFrameData() || {};
    const hasResults = !!((data.results || []).length);
    const isCurate = String(plan.verb || "").indexOf("curate.") === 0;
    if (!hasResults && (plan.requires_results ||
            _activeIntents.some(function (p) { return !!p.requires_results; }))) {
        // 执行对象是**当前这批结果**（打包/引文/可行性…），屏上还没有：先按原话检索——
        // 动作词由检索侧剥离（queryForRetrieval）。开了「AI 执行」：检索落地后直接派发
        // 这份已拿到的 plan（actAfterSearch 的 actPlan 档；共享规范链会接续
        // pending_frontend 并去掉与顶层 plan 重复的动作）；没开就只检索不代劳。
        // keepConv 与 search 档同判据（A1：fromChat || wasChat）——chat-in-main 主框来的
        // 工具句也不许清掉前段对话。say 的存续分两态（owner 疑点 B）：keepConv 时 ubSubmit
        // 那句 say 还在（sayPushed 防双泡）；新时间线（hero 首句）会被 runRecommend 清场，
        // 必须给 sayText 让它清完重推——否则原话从对话流里消失。
        // 与 search 档同一口径（owner 新指①）：原话不在在途期间写回输入框——经 queryOverride
        // 交给 runRecommend，结果落地时才由它回填成当前检索句（B 草稿守卫不变）。
        // 流式规划档：开了「AI 执行」时规划流留着，检索落地后 actAfterSearch 接力续跑；
        // 没开则只检索不代劳——规划流没有下文，就地折叠。检索期间不确定态换句「检索中…」。
        if (!actEnabled()) ubStreamRunSettle();
        cbProgressRelabel("检索中…");
        runRecommend(Object.assign(
            { keepConv: !!(fromChat || wasChat), queryOverride: said, sentText: said, handSubmit: true },
            (fromChat || wasChat) ? { sayPushed: true } : { sayText: said },
            actEnabled() ? { actPlan: plan, actSaid: said } : {}
        ));
        return;
    }
    // cur3：**不需要结果的执行动词直接派发**（curate.* 五动词，作用对象是本地语料库而非屏上结果）。
    // 管护句关键词往往零命中——turn pipeline 的 LLM 分流带原始查询，这类句子不再被关键词阶段毙掉。
    // owner 疑点 A：动作句不是检索句，不该占住「当前检索句」真源——发送即清空后，
    // 对话窗来源的原话被写进主框（ubSubmit fromChat）、hero 主框则被清空；任务包/引文/可行性
    // 都读框取 query：读到动作句会被词表剥光（0 命中假失败：屏上明明有结果），读到空串直接 400。
    // 直派/指路前把框恢复成**当前帧的检索句**；框里已是用户新打的草稿则不动（B 守卫同口径）。
    const _frameQ = cbFrameQuery() || "";
    const _qbox = $("queryInput");
    if (_qbox && _frameQ) {
        const _bv = String(_qbox.value || "").trim();
        if (!_bv || _bv === String(said || "").trim()) _qbox.value = _frameQ;
    }
    setLastRecommendData(data);   // 属主是 search.js（C3 起 ESM）：写必经 setter
    usageLog(USAGE_KINDS.conv, { mode: "chat" });
    // curate.* 不过「AI 执行」开关、永远直派（2026-08-03 全自动化：plan→apply 链式直推，
    // 无人工确认停点，记账 + 回收站可回退）——开关关上时管护在界面上没有任何别的入口
    // 关掉「AI 执行」不该让管护能力整个消失。
    if (!actEnabled() && !isCurate) {
        // 没开「AI 执行」：走既有指路（在**当前这批结果**上打开任务包，不拿这句话去重搜——
        // 「打包前20条」被词表剥光后退化成空查询命中全库，正是 cbRouteAsFirstBox 注释里那个坑）。
        ubStreamRunSettle();   // 流式规划档：指路面板即终点，规划流就地折叠
        const done = cbRouteAsFirstBox(said, "action", []);
        if (done) cbMarkLastAsAction(typeof done === "string" ? done : "已打开下载面板");
        resetSubmitButton();   // 只开面板不检索——收表
        return;
    }
    // 有结果 + 开了自动执行（或 curate 直派）：直接派发（busy 闸在 actDispatchPlan 里）。
    // p10：执行过程与总结都长在对话流里（行动流 + 总结 sys），结果区不再需要为面板提前露头
    // （的 enterResultsLayout 块随 #curatePanel/#actReceipt 一起退役）。
    resetSubmitButton();   // 派发不检索（下载/写盘由行动流自报进度）——收表
    const pending = cbPendingMessage();
    /* 顶层 plan/兼容 intents 与 pending_frontend 只交给 act.js 的规范链拼接、去重、串行派发；
       board 不再先派顶层 plan、再自行跑尾巴，避免同一动作执行两次。 */
    /* 混合轮（先检索后派发）：检索事实只在这个真派发点灌注（actFinish 一次性消费并进
       唯一气泡）——上方 act 关闭的指路分支提前 return 灌不到，杜绝陈旧事实漏进下一轮。 */
    if (searchData) actPrimeTurnSearchFacts(searchData);
    const _dispatching = actDispatchPlanChain(plan, said);
    _dispatching.then(function (mark) {
        // 返回值契约：true=行动流全程呈现（标 action 不挂注记）；字符串=以注记挂上（取消/忙碌）；
        // false=不属执行（理论不可达：这里派发的必是 exec plan）。
        if (mark === true) cbMarkMessageAsAction(pending, "");
        else if (mark) cbMarkMessageAsAction(pending, mark);
    }).catch(function (err) {
        cbMarkMessageAsAction(pending, "这一步没有执行：" + String((err && err.message) || err));
    });
}

/* 「继续对话」与「初次对话」对齐：这句话不是改条件时，别只回一句听不懂。
   同一个人在同一个页面上，换个输入框说同样的话就不 work —— 那不是功能少一点，
   是两个入口不是同一个产品。三条路由由后端 `board.classify_utterance` 判定（复用
   ACTION_MARKERS 与 N8 标识符判据两个既有单一真源，前端不另抄一份词表）：

     action     → 在**当前这批结果**上打开任务包（不重搜；原因见函数体注释）
     identifier → 整句送回主搜索框重搜，天然带上 N8 直链反查
     new_query  → 不擅自改写用户的检索意图，给一个「当作新的检索」的按钮，点一下即走同一条管线

   前两档自动执行、第三档给按钮，分界是「这句话的意图是否唯一」：说了「打包」「贴了个编号」
   意图无歧义；一整句新需求则可能只是没说清的改条件，替用户决定会把他上一次的条件冲掉。 */
/* 把最近一条「还没归属帧」的用户消息（ubDispatch 刚 push 的 say）标成执行类（action）：
   显式指向**当前已在屏上的**那一帧、挂上「已…」注记。action 不触发 runRecommend、不产新帧，
   所以 frameId 只能在这里显式补（不能等 cbPushCurrent 回填——那永远不会为 action 跑）。
   刻意**不**为了造帧去误调 cbPushCurrent：那会往撤销栈里插一条与当前帧完全相同的幽灵帧、污染 undo/redo。 */
/* 取当前这条「还没归属帧」的用户消息的**对象引用**。
   执行层是异步的（要先问后端该做什么、再真去做），期间完全可能又进来别的消息；
   等它回来再按「最后一条 frameId==null」找一次，就会把「已打包 20 条」这条注记
   贴到**别人那句话**上——那比丢失更严重。所以先取引用，回来直接改那个对象。 */
function cbPendingMessage() {
    for (let i = _cbLog.length - 1; i >= 0; i--) { if (_cbLog[i].frameId == null) return _cbLog[i]; }
    return null;
}

function cbMarkMessageAsAction(msg, note) {
    if (!msg) return;
    msg.kind = "action";
    msg.note = String(note || "");
    msg.frameId = (_cbCursor >= 0 && _cbStack[_cbCursor]) ? _cbStack[_cbCursor].id : null;
    cbProgressDrop();   // 进度泡：执行注记泡就是这句的回复——进度泡静默撤下，不重复回
    cbRenderHistory();
}

function cbMarkLastAsAction(note) {
    cbMarkMessageAsAction(cbPendingMessage(), note);
}

/* 统一框「先检索后派发」的回注（act.js actAfterSearch 的 actPlan 档用）：把执行注记挂到那句**原话**
   上。目标必须按 kind 找（owner 疑点 B）：actDispatchPlan 的总结 sys（actFinish）先落地，
   此时 _cbLog 末尾已是回执泡而非原话——标错对象会把回执泡改成 action（明细折叠区随之不上屏、
   sr 前缀错成「你要求：」），原话 say 反倒永远没标记。 */
export function cbMarkLastSayAsAction(note) {
    for (let i = _cbLog.length - 1; i >= 0; i--) {
        if (_cbLog[i].kind === "say") { cbMarkMessageAsAction(_cbLog[i], note); return; }
    }
}

function cbRouteAsFirstBox(text, route, actionMarkers) {
    if (route === "action") {
        // 「打包 / 下载脚本 / 导出引文」针对的是**当前这批结果**，不是一句新检索。
        // 绝不拿这句话去覆盖查询框再重搜：「打包前20条」被词表当填充词剥光后会退化成**空查询**，
        // 空查询命中全库（实测 767 条），于是「打包当前结果」悄悄变成「打包全库前 10 条」——
        // 包里的清单/下载脚本/FAIR/引文与用户看到的结果毫不相干，正是「跑起来了但全错」。
        // 直接在**当前**查询/筛选/范围上打开任务包即可：previewTaskPack 读的就是 queryInput 与当前条件。
        const btn = $("taskPackBtn");
        // 屏上没有可打包的结果：**如实说**，不要交回上层那句「这句话我没有听懂」——
        // 系统明明听懂了（route 就判成了 action），真相是「现在没东西可打包」。
        // 把「没东西可做」谎报成「没听懂」，用户会去改说法，而问题根本不在说法上。
        if (!btn || btn.hidden) {
            cbShowMessage("现在还没有检索结果可以打包。", "先查一批数据，出结果之后再说一次就行。",
                [{ id: "focus_search", label: "先查点数据" }]);
            return "这一步没有执行：屏上还没有结果";
        }
        const panel = $("taskPackPanel");
        if (panel) panel.hidden = false;
        const count = tpCountFromUtterance(text);   // 「前20条」→20、「前5条」→5；认不出返回 0，走默认口径
        previewTaskPack(count ? { count: count } : {});
        // 对话框在侧栏、任务包面板在结果区（主区）：滚进视野，用户才看得见点了有反应。
        if (panel && panel.scrollIntoView) panel.scrollIntoView({ block: "nearest" });
        return true;
    }
    const input = $("queryInput");
    if (!input) return false;
    // identifier / new_query 这两档就是要发起一次新检索：把整句放进主搜索框、走与首个搜索框
    // **完全同一条**函数（runRecommend），而不是复制一份请求体——来源/时间/重排/账户/缓存/历史
    // 全部原样继承，不会有「对话记录少带了个参数」这类漂移。
    input.value = text;
    runRecommend();
    // 「把 E-MTAB-1234 打包」这类话：编号优先（先把用户点名的那一条查出来），但他确实也说了「打包」。
    // 不吭声就是静默吞掉半句诉求——如实说一句，别让他以为系统没看见。
    // （一句话里同时办两件事属于执行层的多动作编排，本期不做。）
    const marks = (actionMarkers || []).filter(Boolean);
    if (route === "identifier" && marks.length) {
        toast("先按编号查这一条；你说的「" + marks[0] + "」等结果出来后再说一次就能办。");
    }
    return true;
}

/* hadSay：本 turn 是否已经 push 过一条「说」（统一框打字发送路径 ubDispatch 才有）。显式传参，
   不再靠「_cbLog 末尾 frameId 是否为 null」这种隐式尾状态判断——那在并发提交下会被别的在途消息干扰。
   返回值：true=本计划已提交、一次新检索正在跑（调用方留着进度表）；
   false=没有检索（三选一/建议词/听不懂/交执行层/新检索指路——调用方收表）。 */
function cbApplyPlan(plan, hadSay) {
    if (plan.status === "auto_apply") {
        cbCommit(plan, hadSay);
        return true;
    }
    if (plan.status === "needs_confirm") {
        // 用户要「发送后不再有确认界面，保持干净」：这一档系统已经读懂了怎么改，
        // 原本只是弹一个「确认并检索 / 取消 + 可编辑句子」的预览。直接应用即可——
        // 应用后的这一步会作为一条「细化」消息进记录；走错了也能随时 分支/回退，撤销成本极低。
        // 注意只自动化**读懂了**的这一档；needs_choice / suggest / 听不懂 / new_query 是**真歧义**，
        // 系统不知道该怎么改，那些必须继续问，不能替设计决定。
        cbCommit(plan, hadSay);
        return true;
    }
    if (plan.status === "needs_choice") {
        // 记下这几个选项：点下去时要送**后端已经拆好的那个值**（例如「人类」），
        // 而不是用户原话（「再加一条：人类」）——原话里还带着说话方式词，
        // 指定了改动类型之后就不会再剥一次，词表会当场认不出来。
        _cbChoices = plan.choices || [];
        cbShowMessage(plan.message, plan.detail, _cbChoices.map(function (c) {
            return { id: "choice:" + c.id, label: c.label };
        }));
        return false;
    }
    if (plan.status === "suggest") {
        cbShowMessage(plan.message, "", (plan.suggestions || []).map(function (s) {
            return { id: "term:" + plan.dim + ":" + s.alias, label: s.display + "（" + s.alias + "）" };
        }).concat(cbEscapeHatches()));
        return false;
    }
    // not_understood / rejected：一个字节都没改。先看这句话是不是「本来就不是改条件」——
    // 是的话按 route 交给对应的入口，而不是丢一句听不懂。
    // （统一路由后一切自由文本都过 /api/utterance，这里只剩 chip 流程的 forced_op 边角：
    //  指路即可，不再二次规划执行。）
    const said = String((plan.echoed && plan.echoed.utterance) || "");
    if (plan.route === "action" || plan.route === "identifier") {
        // 返回值：false=没接住（交回下面的兜底）；true=按常规接住了；字符串=接住了，但实际发生的
        // 不是「打开下载面板」，注记以这个字符串为准。**注记必须与真实发生的事一致**——
        // 屏上没有结果时照旧写「已打开下载面板」就是谎报。
        const done = cbRouteAsFirstBox(said, plan.route, plan.action_markers);
        if (done) {
            // action 只开下载面板、不检索 → 不产新帧，得手动把这条用户消息挂到**当前**帧上（要「查看历史回复」按钮），
            // 否则它 frameId 恒 null：永远没按钮、且被帧剪枝的 frameId!=null 过滤静默吃掉。
            // identifier 走了 runRecommend、会自然产帧并回填，无需在此处理。不再弹 #cbPreview 解释文字。
            if (plan.route === "action") {
                cbMarkLastAsAction(typeof done === "string" ? done : "已打开下载面板");
            }
            // identifier 被 cbRouteAsFirstBox 接住时内部走 runRecommend——有检索，进度表留着。
            return plan.route === "identifier";
        }
    }
    if (plan.route === "new_query") {
        cbShowMessage(plan.message, plan.detail,
            [{ id: "as_new_query", label: "当作新的检索跑一次" }].concat(cbEscapeHatches()));
        return false;
    }
    cbShowMessage(plan.message, plan.detail, (plan.suggestions || []).map(function (s) {
        return { id: "term:" + plan.dim + ":" + s.alias, label: s.display + "（" + s.alias + "）" };
    }).concat(cbEscapeHatches()));
    return false;
}

function cbCommit(plan, hadSay) {
    const request = plan.next_request;
    if (!request) return;
    const box = $("cbPreview");
    if (box) { box.hidden = true; box.innerHTML = ""; }

    const queryInput = $("queryInput");
    if (queryInput && String(queryInput.value || "") !== request.query) {
        queryInput.value = request.query;
        setFacetState({ queryHits: [] });   // 换了句子，原始命中的旧快照必须失效，否则会与新条件同屏矛盾（C2：重赋值经属主 setter）
    }
    setFacetState({   // C2：四个分面状态的重赋值必经 results.js（属主）setter
        suppressed: (request.suppressed_constraints || []).slice(),
        lenientDims: (request.lenient_dims || []).slice()
    });
    // 分面选择只在「去掉 / 放宽」时保留（存活集必为超集）；其余改动服务端已经清空，
    // 这里按服务端给的长度决定——但**不整表覆盖**：服务端只回 {dim,value}，
    // 照抄回来会把面包屑退化成内部术语 + 英文原值（display/label 的真源在前端）。
    if (!(request.facet_filters || []).length) setFacetState({ facetFilters: [] });   // C2：重赋值经属主 setter
    // 统一框打字发送路径（ubDispatch，hadSay=true）已 push 过一条「说」代表本 turn，不再叠一条系统描述文字。
    // 但 cbChoose 的 改/建议词/三选一（hadSay 缺省 falsy）事先**没有任何** push——那些路径若也跳过就会零留痕
    //（条件改了、也重搜了，聊天记录里却什么都没有），故补一条。用显式 hadSay 而非「_cbLog 末尾 frameId
    // 是否为 null」的隐式尾状态：后者在两次提交并发在途时会被对方的未回填消息干扰、把该补的这条误跳过。
    if (!hadSay) cbLogPush("refine", plan.message);
    // 先开泡再冠 hint：runRecommend（keep 路径）内部也会幂等开泡，若由它先开会把这句 hint 重置掉。
    cbProgressBegin();
    _cbProgHint = String(plan.message || "");   // 进度泡蜕变时冠上这句（「已经不按『物种』筛了」），比干计数更像回复
    toast(plan.message);
    // 条件板改条件的唯一提交点：conv 事件的 refine 模态在此接线
    // （chat 模态在各发送路径另记）；同一轮的 tid 由 ubSubmit 开局。
    usageLog(USAGE_KINDS.conv, { mode: "refine" });
    runRecommend({ fromBoard: true });
}

function cbCancel() {
    const box = $("cbPreview");
    if (box) { box.hidden = true; box.innerHTML = ""; }
}

async function cbChoose(choiceId) {
    const id = String(choiceId || "");
    // 与 ubSubmit / facetStageSubmit 同一道在途互斥闸门：上一次检索还在途时不接新的选择——
    // 否则两次 cbCommit 并发在途，且 cbPushCurrent 会把两批改动吞并进同一帧。
    const _sb = $("submitBtn");
    if (_sb && _sb.disabled) { toast("上一步还在检索，稍等一下再操作"); return; }
    // ask.relax 选择卡的作答（2026-09-14 收编单一通道 + ask 暂停/重启批）：
    // 自动卡（带 _autoBatchId）→ cbCommit 零 LLM 重搜（suppress 服务端已合并去重）；
    // 环内卡 → ubSubmit 把作答作为新回合重新入环（ask_answer 字段），环裁决后续。
    if (id === "askrelax:dismiss") {
        const pending = _askRelaxPending;
        _askRelaxPending = null;
        // 自动卡的 dismiss 按批记忆（同批不再自动弹；zero pill 显式重开时清除）；
        // 环内卡无 _autoBatchId，不记（环内机械闸自己管触发频率）。
        if (pending && pending._autoBatchId) _relaxDismissed.add(String(pending._autoBatchId));
        cbCancel();
        return;
    }
    if (id.indexOf("askrelax:") === 0) {
        const pending = _askRelaxPending;
        _askRelaxPending = null;
        const opt = pending ? (pending.options || [])[parseInt(id.slice(9), 10)] : null;
        if (!opt) { cbCancel(); return; }
        // 「用户不得不放宽条件」本身就是一次检索没做好的信号，值得进反馈包
        //（埋点自退役的空态卡 chips 迁来，payload 同形：d=放松目标可读名，dim/key 供分析侧归类）。
        usageLog(USAGE_KINDS.relax, {
            d: String(opt.label || "放宽"),
            dim: String(opt.key || "").replace(/^(dim:|only:|exclude:)/, ""),
            key: String(opt.key || ""),
        });
        const msg = opt.kind === "only"
            ? "这次只按「" + (opt.label || "") + "」搜，其余条件都没参与筛选"
            : "已经不按「" + (opt.label || "") + "」筛了（其余条件不变）";
        if (pending._autoBatchId) {
            // 自动弹卡（零命中兜底 / zero pill 重开，非环内、可能 AI 关）：没有环可续——
            // 走 cbCommit 零 LLM 重搜旧路径，逐位不变。
            cbCommit({
                message: msg,
                next_request: {
                    query: String(pending.query || ""),
                    suppressed_constraints: (opt.suppress || []).slice(),
                    lenient_dims: (_lenientDims || []).slice(),
                    facet_filters: (_facetFilters || []).map(function (f) { return { dim: f.dim, value: f.value }; })
                }
            }, false);
            return;
        }
        // 环内卡（2026-09-14 ask 暂停/重启批）：作答作为新回合重新入环——服务端按所选
        // suppress 重跑检索（rank 步），decide 注入作答事实后由 LLM 裁决上屏/finish/再追问；
        // 环暂停时弹的卡在此刻把结果交还给环。日志口径 = refine（ubSubmit 内接线）。
        cbCancel();
        ubSubmit("board", {
            text: String(pending.query || ""),
            askAnswer: {
                kind: "ask.relax",
                choice_key: String(opt.key || ""),
                label: String(opt.label || ""),
                message: msg,
                suppress: (opt.suppress || []).slice()
            }
        });
        return;
    }
    if (id === "reset") { cbResetAllConditions(); return; }
    // 「先查点数据」：屏上没有结果时那条如实回答给的出路。只把光标放回统一输入框，不替他编一句查询。
    if (id === "focus_search") {
        cbCancel();
        const box = $("queryInput");
        if (box) { box.focus(); if (box.scrollIntoView) box.scrollIntoView({ block: "nearest" }); }
        return;
    }
    if (id === "as_new_query") {
        // 用户确认「这就是一句新需求」→ 走统一框同一条管线。原话还在框里（提交后不清框），直接跑。
        const input = $("queryInput");
        const said = input ? String(input.value || "").trim() : "";
        if (!said) { toast("这句话已经清空了，重新说一次吧"); return; }
        cbCancel();
        runRecommend();
        return;
    }
    if (id.indexOf("edit:") === 0) {
        const plan = await cbPlan({ forced_op: "suggest", dim: id.slice(5) });
        if (plan) cbApplyPlan(plan);
        return;
    }
    if (id.indexOf("term:") === 0) {
        const rest = id.slice(5);
        const sep = rest.indexOf(":");
        const dim = rest.slice(0, sep);
        const alias = rest.slice(sep + 1);
        const plan = await cbPlan({ forced_op: "replace", dim: dim, utterance: alias });
        if (plan) cbApplyPlan(plan);
        return;
    }
    if (id.indexOf("choice:") === 0) {
        const op = id.slice(7);
        const chosen = _cbChoices.filter(function (c) { return c.id === op; })[0];
        if (!chosen) return;
        const plan = await cbPlan({ forced_op: chosen.op, dim: chosen.dim || "", utterance: chosen.payload || "" });
        if (plan) cbApplyPlan(plan);
    }
}

function cbResetAllConditions() {
    resetFacetState();   // C2：四个分面状态一起归零，经 results.js（属主）setter
    cbCancel();
    cbLogPush("refine", "恢复全部条件");
    toast("已经恢复全部条件");
    runRecommend({ fromBoard: true });
}

/* 行内按钮：一律复用既有通道，不新造一套并行的过滤逻辑。 */
function cbBindRowActions(event) {
    const target = event.target.closest("[data-cb-remove],[data-cb-restore],[data-cb-lenient],[data-cb-strict],[data-cb-unfacet],[data-cb-edit],[data-cb-choice]");
    if (!target) return;
    if (target.dataset.cbRemove !== undefined) { toggleQueryHit(target.dataset.cbRemove); return; }
    if (target.dataset.cbRestore !== undefined) { toggleQueryHit(target.dataset.cbRestore); return; }
    if (target.dataset.cbLenient !== undefined) { toggleLenient(target.dataset.cbLenient); return; }
    if (target.dataset.cbStrict !== undefined) { toggleLenient(target.dataset.cbStrict); return; }
    if (target.dataset.cbUnfacet !== undefined) { cbRemoveFacet(target.dataset.cbUnfacet); return; }
    if (target.dataset.cbEdit !== undefined) { cbChoose("edit:" + target.dataset.cbEdit); return; }
    if (target.dataset.cbChoice !== undefined) { cbChoose(target.dataset.cbChoice); return; }
}

function cbRemoveFacet(dim) {
    setFacetState({ facetFilters: (_facetFilters || []).filter(function (f) { return f.dim !== dim; }) });   // C2：重赋值经属主 setter（C4 起读取走 import 活绑定）
    runRecommend({ keepFacets: true });
}

export function initCondBoard() {
    // fb1：benchfb 收尾通知的绑定回调注册（chat 轮收尾 → 评分卡绑到本轮系统回复 entry）。
    benchfbOnChatEntry(_bfOnChatClosed);
    // T1 把输入框自动伸展工具注入 benchfb（断环——benchfb 不 import interactions
    // 那会把它拉进 SCC 环；评语输入框的动态 textarea 经它随行数伸展）。
    benchfbSetAutoGrow(autoGrow);
    // 历史打标钩子注册进 core（2026-08-10：core→board 反向边切断，改注册反转；
    // core 的 pushHist/pushHistChatOnly 只在用户检索时调钩子，此时必已注册）。
    setHistHooks({ convId: cbConvId, logForHistory: cbLogForHistory });
    // 没存过偏好就沿用默认（收起）；存过就尊重用户那次选择。
    try {
        const saved = localStorage.getItem(nsKey(BOARD_COLLAPSED_KEY));
        if (saved !== null) _cbCollapsed = saved === "1";
    } catch (_e) { /* 存储不可用时沿用默认 */ }
    const board = $("condBoard");
    if (!board) return;
    board.addEventListener("click", cbBindRowActions);
    // #cbPreview 会被 placeCbPreview 搬出 #condBoard（chat-in-main 时跟对话记录走）——
    // 卡上按钮的点击委托必须绑在卡自身才搬家不失效；命中可点项后止泡，避免在静态家时
    // 继续冒泡到 #condBoard 再触发一次 cbBindRowActions。
    $("cbPreview").addEventListener("click", function (event) {
        cbBindRowActions(event);
        if (event.target.closest("[data-cb-remove],[data-cb-restore],[data-cb-lenient],[data-cb-strict],[data-cb-unfacet],[data-cb-edit],[data-cb-choice]")) {
            event.stopPropagation();
        }
    });
    // 聊天/细化记录里每条消息的「查看历史回复」：委托挂在 #cbHistory 上（它的 innerHTML 每次重画，
    // 但监听绑在元素本身、不随子节点重建而失效）。#cbHistory 在条件板内，与上面的行内按钮通道分开。
    // 不加 `if (hist)` 守卫——与下面几个兄弟绑定一致：#cbHistory 缺失就当场炸（loud fail），
    // 别让整块「查看历史回复」静默失效；该 id 由 test_board_frontend_static 的 markup 门钉死。
    $("cbHistory").addEventListener("click", cbHistoryClick);
    // msgfb：评论编辑器的草稿随输入记到模块级（innerHTML 重画后回填不丢字），并自适应高度。
    $("cbHistory").addEventListener("input", function (event) {
        const ta = event.target && event.target.closest ? event.target.closest("[data-cbh-cmt-ta]") : null;
        if (!ta) return;
        _cbCommentDraft = ta.value;
        autoGrow(ta, { minRows: 2, maxRows: 6 });
    });
    $("cbSummaryBar").addEventListener("click", cbToggleCollapsed);
    // 输入条变形三键：回到最新 / 分支（新开浏览器标签页）/ 回退（剪掉之后，二段确认）
    $("cbTopBtn").addEventListener("click", cbToLatest);
    $("cbBranchBtn").addEventListener("click", cbBranchFromHere);
    $("cbRevertBtn").addEventListener("click", cbRevertHere);
    // 数据细化「提交/取消」条：facets.js 的暂存草稿由这两颗静态按钮落地。绑在这里（board init 已在 boot 期运行、
    // facets.js 全局此刻已定义）。不加 typeof 守卫——名字对不对交给 markup 门钉死，别让打错名字静默短路。
    $("facetStageSubmit").addEventListener("click", facetStageSubmit);
    $("facetStageCancel").addEventListener("click", facetStageCancel);
    cbApplyCollapsed();
    initSideWork();
}
