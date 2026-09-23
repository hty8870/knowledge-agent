"use strict";

/* 本文件是 ES Module：core 的工具、board_core 的 CB_DIM_LABEL、#cards 的 buildCard、
   #memory 的 setRememberSearchAvailable 经 import 取。runRecommend（search）、cbClear/cbLogPush/
   renderCondBoard（board）、resetTaskPack/syncTaskPackBar（task_pack）、placeFacetBar（facets）、
   getSelectedSources/getSourceMode（interactions）经 import 取（互调成环——绑定都只在函数体内使用）。
   search/board/browse/interactions/act/accounts 经 import 取本文件导出（绞杀桥已全退役）。 */
import { API, MOTION, $, escapeHtml, escapeHtmlStrong, ghostExit, isHttp, normalizeItem, prettySource, revealCards, toast } from "#core";
import { CB_DIM_LABEL } from "#board_core";
import { buildCard } from "#cards";
import { setRememberSearchAvailable } from "#memory";
import { USAGE_KINDS, usageActiveImpressionId, usageActiveTurnId, usageBeginImpression, usageSeenCreate, usageSeenPause, usageSeenTick } from "#usage_core";
import { usageBindImpression, usageImpressionItems, usageLog, usageLogImpression, usageMakeImpression } from "#usage_log";
import { placeFacetBar } from "#facets";
import { cbClear, cbLogPush, renderCondBoard } from "#board";
import { resetTaskPack, syncTaskPackBar } from "#task_pack";
import { getSelectedSources, getSourceMode } from "#interactions";
import { applyRecommendResult, LAST_RECOMMEND_DATA, runRecommend } from "#search";
import { benchfbAfterSearchRender } from "#benchfb";   // 结果区重建后重新挂 hero 评分卡槽位

/* ---------- 结果曝光追踪（2026-08-22；重写为 500ms 状态机，schema v3）----------
   一张结果卡入视口**可见累计 ≥500ms** 才算「看过」（USAGE_SEEN_MIN_MS；状态机在 usage_core
   纯核 usageSeenCreate/usageSeenTick/usageSeenPause，node 可测）。语义：
   - 滚动离开：在途计时清零重来（瞥一眼不算看）；
   - 页面隐藏（hidden/pagehide）：在途区间**冻结不清零**（usageSeenPause），补发一条 view，
     断开观察器但**保留 iid 与已见集合**；可见恢复（visible）后同一 iid 继续，
     重新观察未 seen 的卡（IO 初始回调自动重开可见卡的在途区间）；
   - 只有 renderResults 的 _seenBegin 才开新 iid（上一屏在 _seenFinish 里收尾）。
   view 事件去重：每次展示**首发恒发**（哪怕 seen 为空——证明这屏存在过），之后只在 seen
   集合变大时补发；dwell=可见累计毫秒（hidden 时段不计）。
   tid/iid 用 begin 时捕获的**快照**——发的时候新一轮 turn 可能已经开始，读现值会串号。
   时刻只用 IO entry.time / performance.now()（二者同源），绝不与 Date.now() 混用。
   IO 不可用（老浏览器）时只换 iid 不追踪——没有 view 事件，分析侧按缺失处理。 */
const _seen = { iid: "", tid: "", machine: null, observer: null, grid: null, cards: null,
    dwellAccum: 0, dwellSince: null, emittedCount: -1 };

function _seenNow() {
    try {
        if (typeof performance !== "undefined" && performance && typeof performance.now === "function") return performance.now();
    } catch (_e) {}
    return Date.now();
}
function _seenHidden() {
    return typeof document !== "undefined" && document.visibilityState === "hidden";
}

/* 发事件前把「一直停在视口里、没有后续 IO 回调」的卡按当前时刻复评一遍——
   IO 只在交集变化时回调，停着不动到点了也不会自己升 seen。只复评在途区间开着的卡
   （since!==null）；不在途的卡（未见过/已清零）不碰，否则会把离屏卡凭空开区间。 */
function _seenSweep(nowMs) {
    if (!_seen.machine) return;
    const hidden = _seenHidden();
    Array.from(_seen.machine.cards.keys()).forEach(function (pos) {
        const c = _seen.machine.cards.get(pos);
        if (c && c.since !== null) usageSeenTick(_seen.machine, pos, true, nowMs, { pause: hidden });
    });
}

function _seenEmit() {
    if (!_seen.iid) return;
    const now = _seenNow();
    _seenSweep(now);
    const seenArr = _seen.machine
        ? Array.from(_seen.machine.seen).sort(function (a, b) { return a - b; }) : [];
    // 首发恒发（emittedCount 初值 -1，空 seen 也发）；之后只在 seen 集合变大时补发。
    if (seenArr.length <= _seen.emittedCount) return;
    _seen.emittedCount = seenArr.length;
    let dwell = _seen.dwellAccum;
    if (_seen.dwellSince !== null) dwell += Math.max(0, now - _seen.dwellSince);
    usageLog(USAGE_KINDS.view, {
        tid: _seen.tid, iid: _seen.iid,
        seen: seenArr,
        dwell_ms: Math.round(Math.max(0, dwell)),
    });
}

function _seenFinish() {
    if (_seen.observer) { try { _seen.observer.disconnect(); } catch (_e) {} _seen.observer = null; }
    if (!_seen.iid) { _seen.machine = null; return; }
    _seenEmit();   // 上一屏的最终 view（首发/补发去重都在 _seenEmit 内）
    _seen.iid = ""; _seen.machine = null; _seen.grid = null; _seen.cards = null;
}

function _seenBegin() {
    _seenFinish();   // 上一屏的曝光先收尾，再开新展示
    _seen.iid = usageBeginImpression();
    _seen.tid = usageActiveTurnId();
    _seen.machine = usageSeenCreate();
    _seen.grid = null; _seen.cards = null;
    _seen.dwellAccum = 0;
    _seen.dwellSince = _seenHidden() ? null : _seenNow();
    _seen.emittedCount = -1;
}

/* hidden/pagehide：冻结（不清零）全部在途区间与 dwell，补发一条 view，断开观察器，
   **保留 iid 与已见集合**——可见恢复后同一展示继续（_seenResume）。 */
function _seenPause() {
    if (!_seen.iid) return;
    const now = _seenNow();
    if (_seen.machine) usageSeenPause(_seen.machine, now);
    if (_seen.dwellSince !== null) { _seen.dwellAccum += Math.max(0, now - _seen.dwellSince); _seen.dwellSince = null; }
    _seenEmit();
    if (_seen.observer) { try { _seen.observer.disconnect(); } catch (_e) {} _seen.observer = null; }
}

function _seenResume() {
    if (!_seen.iid || !_seen.machine || _seen.observer) return;
    _seen.dwellSince = _seenNow();
    _seenObserve(_seen.grid);   // 同一 iid 继续：重新观察未 seen 的卡，IO 初始回调自动重开区间
}

function _seenObserve(grid) {
    if (!grid || typeof IntersectionObserver !== "function" || !_seen.machine) return;
    const cards = Array.prototype.slice.call(grid.querySelectorAll(".card"));
    if (!cards.length) return;
    _seen.grid = grid; _seen.cards = cards;
    const machine = _seen.machine;
    const observer = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
            const i = cards.indexOf(en.target);
            if (i < 0) return;
            // entry.time 与 performance.now 同源；缺失时取 _seenNow()（同源兜底）。
            const t = (en && typeof en.time === "number" && en.time > 0) ? en.time : _seenNow();
            usageSeenTick(machine, i + 1, !!en.isIntersecting, t, { pause: _seenHidden() });   // 名次与 imp items / usageCardRank 同为 1-based
        });
    }, { threshold: 0.5 });
    cards.forEach(function (c, i) { if (!machine.seen.has(i + 1)) observer.observe(c); });
    _seen.observer = observer;
}

if (typeof window !== "undefined" && window.addEventListener) {
    window.addEventListener("pagehide", _seenPause);
}
if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "hidden") _seenPause();
        else _seenResume();
    });
}

/* ---------- imp 展示事件 + 卡级归因（2026-08-22，schema v3）----------
   每次非空结果渲染：造一个不可变 ImpressionContext（tid/iid/policy/当屏 items 快照），
   绑到每张卡 DOM 上（之后这张卡的 open/fav 都归因到**这次展示**，新一轮检索/重渲不串号），
   并发一条 imp 事件。imp 参数缺 tid 时回落当前轮（渲染调用方 search.js
   applyRecommendResult 会显式给）。 */
function _emitImpression(items, imp) {
    if (!items || !items.length) return;
    const grid = $("resultsGrid");
    if (!grid) return;
    const ctx = usageMakeImpression({
        tid: (imp && imp.tid !== undefined) ? imp.tid : usageActiveTurnId(),
        iid: usageActiveImpressionId(),   // _seenBegin 已在本渲染开头换好新 iid
        policy: imp ? imp.policy : "",
        items: usageImpressionItems(items),
    });
    Array.prototype.forEach.call(grid.querySelectorAll(".card"), function (c) { usageBindImpression(c, ctx); });
    usageLogImpression(ctx);
}

// 有序层名连成中文短语：["规则排序"] → "规则排序"；[a,b] → "a 与 b"；[a,b,c] → "a、b 与 c"。
function joinLayers(layers) {
    if (layers.length <= 1) return layers[0] || "";
    return layers.slice(0, -1).join("、") + " 与 " + layers[layers.length - 1];
}
/* 回退附注：**每一句都由后端出**（`step.fallback_note`，单一真源 workflow._fallback_note）。

   为什么不在这里写死措辞——2026-07-26 抓到的真事故：本函数原来对所有 `status="fallback"` 一律写
   「本次未启用，已改用基础方式」。可后端从来就分得清两件事：`skipped` = 没启用，`fallback` = **试过但没成**。
   于是 provider 真返 400 的那几天，用户（和我）看到的都是「AI 重排本次未启用」——一次故障被写成一个选择，
   谁都看不出它坏了。`shell.js` 的开发者面板早就守着「尝试过但失败才算故障」，
   唯独这句用户天天看见的话没守。

   所以措辞的产地只有一个：后端。前端只负责把层名和后端给的那半句话拼起来。
   老后端没有这个字段时退到「没能完成」——**宁可把「没启用」说重，也绝不把故障说轻**。 */
/* 后端 fallback_note 里的内部工程术语 → 人话。后端措辞是诚实真源（未启用/没能完成
   的分档不能丢），这里只做同义改写，不改含义分档：「反捏造校验」用户读不懂，它就是
   「AI 写的说明与检索事实逐条对不上就打回」的那道内容核对。 */
const _FALLBACK_NOTE_HUMAN = [
    [/反捏造校验/g, "内容核对"],
];
function fallbackLayerNotes(steps, wanted) {
    const notes = [];
    (steps || []).forEach(function (s) {
        if (!s || s.status !== "fallback" || !wanted[s.id]) return;
        let note = (typeof s.fallback_note === "string" && s.fallback_note) || "";
        _FALLBACK_NOTE_HUMAN.forEach(function (pair) { note = note.replace(pair[0], pair[1]); });
        // 2026-08-10：note 是后端任意字符串（可含 provider 原始报错），返回值最终经
        // innerHTML 上屏（renderResultSummary）——拼接前必须转义，XSS 面封死在产地。
        notes.push(wanted[s.id] + (note ? "（" + escapeHtml(note) + "）" : "没能完成"));
    });
    return notes;
}
/* 结果摘要：把旧「N 条 · 方法 · 库中匹配 N 条」计数行、「本次检索用了什么」trace 折叠、覆盖缺口
   三处合并成**一段自然语言**。逐步 trace 明细「查看每一步」折叠已删（用户判冗余——方法句已含回退附注，
   逐步明细又与分面/命中 chip 重复；原始输出仍在「设置 → 开发者信息 / 诊断」）。
   诚实红线：方法句只据真实 search_trace（哪层 status=used 才写哪层、回退如实附注）；后端没返回 trace
   （新前端 + 未重启的旧后端）绝不猜方法，如实说「执行明细不可用（请重启后端）」（web_smoke 钉该串）。
   覆盖缺口仍由 renderCoverageCaveats 渲染进本卡内的 #coverageCaveats（其逻辑一字不动）。
   本函数只管：方法句 + 计数 + 摘要卡显隐；覆盖缺口显隐归 renderCoverageCaveats。 */
function renderResultSummary(data) {
    const box = $("searchTrace"), txt = $("resultSummaryText");
    if (!box || !txt) return;
    const ok = !!(data && data.ok);
    // error:true 或后端 not-ok → 摘要整卡隐藏（交给下方「检索失败 / 没有匹配」空态卡如实说话）
    if (!ok) { box.hidden = true; return; }
    const trace = data.search_trace;
    const shown = ((data.results) || []).length;
    const total = (typeof data.result_total === "number") ? data.result_total : shown;
    const hasCoverage = ((data.coverage_caveats || []).length > 0) || ((data.applied_lenient || []).length > 0);
    // 0 结果且无覆盖缺口 → 没有可诚实汇报的增量，摘要卡隐藏（交给空态卡），不重复一句「没有匹配」
    if (shown === 0 && !hasCoverage) { box.hidden = true; return; }

    let sentence;
    if (!trace || !Array.isArray(trace.steps)) {
        // 新静态前端可能由尚未重启的旧 Python 进程提供：此时无 trace，绝不能把方法猜成「规则排序」。
        sentence = `这次用的排序方式无法确认：页面是新版，服务还是旧版。请重启服务（关掉启动窗口再启动一次）后重新检索。`
            + (total > 0 ? `库中共 <b>${total}</b> 条记录匹配，展示前 <b>${shown}</b> 条。` : `库中没有完全匹配的记录。`);
    } else {
        const used = new Set(trace.steps.filter((s) => s && s.status === "used").map((s) => s.id));
        const layers = ["规则排序"];
        if (used.has("local_semantic")) layers.push("本地精准重排");
        if (used.has("llm_rerank")) layers.push("AI 重排");
        // 三层都要报回退：本地精准重排 / AI 重排 / **AI 说明润色**。润色也在列——它同样是用户主动开的开关，
        // 它失败时只是不写那句「已由 AI 润色」，等于让用户以为自己没开过；沉默在这里就是不诚实。
        const fellNotes = fallbackLayerNotes(trace.steps, {
            local_semantic: "本地精准重排", llm_rerank: "AI 重排", llm_polish: "AI 说明润色",
        });
        const fellSentence = fellNotes.length ? `${fellNotes.join("；")}，本次结果按基础方式给出。` : "";
        const polished = data.llm_response_used === true;
        if (total > 0) {
            const disp = (shown >= total) ? `已全部展示（共 <b>${shown}</b> 条）` : `展示前 <b>${shown}</b> 条`;
            // 2026-08-16 用户要求：方法层关键词上行内高光，阅读时一眼定位用了哪几层——
            // 三层统一上（「规则排序/本地精准重排/AI 重排」是同类方法词，只标两个会留下一个裸的不一致）。
            // 层名全是上方硬编码安全串，无 XSS 面；回退附注 fellSentence 里的层名不上（那是状态说明，不是方法）。
            const layersHtml = joinLayers(layers.map((l) => `<mark class="sum-layer">${l}</mark>`));
            sentence = `通过${layersHtml}检索，库中共 <b>${total}</b> 条记录匹配；`
                + `${disp}。${polished ? "推荐说明由 AI 润色。" : ""}${fellSentence}`;
        } else {
            const layersHtml = joinLayers(layers.map((l) => `<mark class="sum-layer">${l}</mark>`));
            sentence = `通过${layersHtml}检索，库中没有完全匹配的记录。${fellSentence}`;
        }
    }
    txt.innerHTML = sentence;
    // 交换态侧栏摘要压 2 行截断，title 兜全文（正常态不截断，title 无害）
    txt.title = txt.textContent || "";
    box.hidden = false;
}

// 对比池：把本次结果集写进 localStorage，供「数据集详情」独立标签页的「数据集对比」子页取用（跨同源标签共享）。
// 改存**完整归一化记录**（原只存轻量五字段）：全屏并排对比要把选中数据集当作 handoff 交给一个完整
// 的 /dataset iframe 渲染，页头需要 download_url / 物种 / 组织 / 疾病 / 平台 / 可达性 / n_files 等——轻量字段
// 会让对比侧退回「暂无下载」降级态。items = data.results（仅展示集、条数有界），故体积可控；换查询即整体覆盖。
// setItem 失败（配额）时**删除旧 key**（2026-08-09：旧实现空 catch——写失败会留着上一查询的
// 旧池，对比子页把旧数据当本次结果展示）；对比池空 → 对比子页如实提示先检索，不影响主结果渲染。
function writeComparePool(items) {
    try {
        const pool = (items || []).map((it) => normalizeItem(it));
        localStorage.setItem("biodata_compare_pool_v1", JSON.stringify(pool));
    } catch (_e) {
        try { localStorage.removeItem("biodata_compare_pool_v1"); } catch (_e2) {}
    }
}

/* ---------- 多批检索结果：批次数据通道 ----------
   后端批次常驻（响应带 result_batches：批 {batch_id,kind,label,query_raw,
   query_effective,payload}）+ active_batch。
   2026-08-24 用户手写定稿：结果头部的 pill 切换器（#batchBar）退役——
   「检索结果页不再展示该 pill」，切换入口唯一化到对话流里的结果 pill（.ft-pill）。
   切换纯前端不发网络：批次 payload 与 /api/recommend 响应同形状，经共享落地口
   applyRecommendResult 重渲——推荐结果标题行两钮、摘要卡、各诚实回显条、条件板全部随批次
   payload 走；视图对象显式回写 result_batches/active_batch，切完再切仍从同一批组里选。 */
function _batchView(data, batch) {
    return Object.assign({}, data, batch.payload, {
        result_batches: data.result_batches, active_batch: batch.batch_id,
    });
}
/* 2026-08-17：pill 文案撞车时的批次来源前缀——初步批 label=本轮原话、rank 批
   label=rank query、rerank 批 label=改写后 query，同句两轮就出现两枚同名 pill 无法区分。
   只在撞名时加前缀（不撞不加，保持既有文案）；kind 口径与后端一致（turn.py 组卷 /
   agent_exec.py 的 _loop_rank/_loop_rerank），措辞沿用 UI 既有说法。2026-08-18 补齐：
   rerank=「改写后重检」、search_rerun=「换词重检」；加前缀后仍撞名再按顺序补 ·2/·3。 */
function _batchPillTexts(batches) {
    const prefixByKind = {
        preliminary: "初步", rank: "新检索", rerank: "改写后重检",
        search_rerun: "换词重检", rescue: "救回",
    };
    const labelOf = (b, idx) => String(b.label || b.query_effective || b.query_raw || ("批次 " + (idx + 1)));
    const labelCount = new Map();
    (batches || []).forEach((b, idx) => {
        if (!b || !b.payload) return;
        const label = labelOf(b, idx);
        labelCount.set(label, (labelCount.get(label) || 0) + 1);
    });
    // 2026-08-18：kind 前缀不是唯一性证明——同 kind 可因 20 字 label 截断撞名，
    // 甚至某个原始 label 可能恰等于另一枚「前缀·label」。最终文案再过 Set，按渲染顺序
    // 追加稳定 ·2/·3，保证任何合法批次数组里两枚 pill 不会完全同名。
    const used = new Set();
    return (batches || []).map((b, idx) => {
        if (!b || !b.payload) return "";
        const label = labelOf(b, idx);
        const prefix = labelCount.get(label) > 1 ? prefixByKind[String(b.kind || "")] : null;
        const base = prefix ? (prefix + "·" + label) : label;
        let shown = base;
        let seq = 2;
        while (used.has(shown)) shown = base + "·" + seq++;
        used.add(shown);
        return shown;
    });
}
/* 覆盖策略（设计 §10.3）：非活动备选批的排序层标注——「新检索 · 规则排序」这类
   后缀，让用户看出备选批比当前批弱在哪一层。只给**非活动** pill 加（当前批已是最好，无谓标注）；
   未知/缺失 trace → 空串（不标注，诚实：不可比较）。级别口径与 batch_select.rankingLevel 一致
   （规则=1 / +本地精准重排=2 / +AI 重排=3；polish 不计）。 */
function _batchRankSuffix(batch) {
    const p = (batch && batch.payload) || batch;
    const trace = (p && p.search_trace) || null;
    if (!trace || !Array.isArray(trace.steps) || trace.steps.length === 0) return "";
    const used = new Set();
    trace.steps.forEach(function (s) { if (s && s.status === "used" && s.id) used.add(s.id); });
    let level = 1;
    if (used.has("local_semantic")) level = 2;
    if (used.has("llm_rerank")) level = 3;
    if (level === 1) return "规则排序";
    if (level === 2) return "规则+本地精准重排";
    return "规则+本地+AI 重排";
}
export function renderBatchSwitcher(data) {
    const bar = $("batchBar");
    if (!bar) return;
    /* 用户 2026-08-24 手写定稿「检索结果页不再展示该 pill」：结果头部的批次 pill
       切换器退役——批次切换入口唯一化到对话流里的结果 pill（.ft-pill，点击走 switchBatch，
       数据通道一字不动）。本函数只剩「确保 bar 不现身」；_batchPillTexts/_batchRankSuffix
       纯函数保留（tests 钉着，且批次文案口径以后若复用仍取这里）。 */
    bar.hidden = true;
    bar.innerHTML = "";
}
/* 换批 = 重渲该批 payload，**不是**一次新落地：applyRecommendResult 负责结果区全件重渲 +
   LAST_RECOMMEND_DATA 换指（fromHistory 不推历史帧、noScroll 不跳顶），renderCondBoard 让
   侧栏「查询条件」chips 同批走；不推对话帧（cbPushCurrent）、不动输入框（_ubLandingFill）、
   不过零命中救回门禁（maybeSearchRescue）——那些都属于「新一轮检索落地」，换批不是。 */
export function switchBatch(batchId) {
    const data = LAST_RECOMMEND_DATA;
    const batches = (data && Array.isArray(data.result_batches)) ? data.result_batches : [];
    let idx = -1;
    for (let i = 0; i < batches.length; i += 1) {
        const b = batches[i];
        if (String((b && b.batch_id) || ("b" + (i + 1))) === String(batchId)) { idx = i; break; }
    }
    if (idx < 0) return;
    const batch = batches[idx];
    if (!batch || !batch.payload || batch.payload.ok === false) return;   // 形状不全 → 保持现状
    // 用归一 id 比「已在这批」——批缺 batch_id 时 data.active_batch 可为非空合成键，
    //   旧实现 `String(active||"")===String(batch.batch_id||"")` 会把缺 id 的批（两边都是 ""）误判成
    //   「已在这批」而空转，让 pill 点不动。改比归一 id 后只在真正是当前批时 no-op。
    const normId = String(batch.batch_id || ("b" + (idx + 1)));
    if (String(data.active_batch || "") === normId) return;   // 已在这批
    const view = _batchView(data, batch);
    const q = String(batch.query_effective || batch.query_raw || "");
    // keepTurn：批次切换是**同一轮检索**内的换屏，imp 归因沿用当前轮 tid，
    // 不能按 fromHistory 落入 null（那是「历史回看」的语义）。
    applyRecommendResult(view, q, { noScroll: true, fromHistory: true, keepTurn: true });
    renderCondBoard(view);
    // 切换动画 ≈200ms（设计 §M3）：与既有「淡入微移」同一语言（placeChatSuite/firstReveal 同款参数族）。
    if (MOTION) {
        gsap.from("#resultsGrid", { opacity: 0, y: 8, duration: 0.2, ease: "power2.out", clearProps: "all" });
        const sum = $("searchTrace");
        if (sum && !sum.hidden) gsap.from(sum, { opacity: 0, duration: 0.2, ease: "power2.out", clearProps: "all" });
    }
}

export function enterResultsLayout() {
    const v = document.querySelector('.view[data-view="query"]');
    if (!v || v.classList.contains("has-results")) return;
    const hero = v.querySelector(".hero");
    if (!(MOTION && hero)) { v.classList.add("has-results"); return; }
    /* 2026-08-30：无对话直达路径（历史回看/切账户重渲——hero 各部还完整可见）的
       display:none 瞬隐软化，与 board.js placeChatLog (a) 的幽灵同一语言。正常首发检索不走到这——
       hero 各部在发送那一刻（chat-main-on）已隐，幽灵已播过（getClientRects 空 → ghostExit 自动跳过）。
       console 仅在它将被隐藏时播（侧栏展开桌面档）；侧栏收起/移动端留任不播。 */
    const sideOpen = !document.body.classList.contains("side-closed") && window.innerWidth > 780;
    hero.querySelectorAll(".hero-rot, .chips, .memory-suggestions").forEach((el) => ghostExit(el, { y: -12, duration: 0.32 }));
    if (sideOpen) ghostExit(hero.querySelector(".console"), { y: 22, duration: 0.34, ease: "power3.inOut", keepIds: true });
    const from = hero.getBoundingClientRect().top;
    v.classList.add("has-results");
    const dy = from - hero.getBoundingClientRect().top;
    // 时长 0.48s：owner 钦点过渡上限 500ms（旧 0.62s 超帽，且主流程的位移已由 placeChatLog 的
    // hero FLIP 承担——那里 dy≈0 会跳过，这里是无对话直达路径（历史回看/切账户重渲）的同一机制）。
    if (Math.abs(dy) > 1) gsap.fromTo(hero, { y: dy }, { y: 0, duration: 0.48, ease: "power3.inOut", clearProps: "transform" });
}
/* 清空查询输入 → 退出结果态：hero 从 slim 恢复完整首屏（enterResultsLayout 的逆向，复用同一 has-results
   机制与 FLIP，不新造平行体系）。同时收起结果区与分面条：分面/命中/宽容态都「属于那次查询」，
   随查询清空失效（与 runRecommend 新查询时的清空同口径）。 */
export function exitResultsLayout() {
    const v = document.querySelector('.view[data-view="query"]');
    const wrap = $("resultsWrap");
    if (!v) return;
    const hero = v.querySelector(".hero");
    const wasResults = v.classList.contains("has-results");
    const from = (MOTION && hero && wasResults) ? hero.getBoundingClientRect().top : 0;
    v.classList.remove("has-results");
    if (wrap) wrap.style.display = "none";
    _facetFilters = []; _suppressed = []; _queryHits = []; _lenientDims = [];
    cbClear();          // 条件板与撤销栈都「属于那次查询」，随结果区一起收起
    resetTaskPack();    // 任务包清单同理：它是「那一次检索的前 N 条」，换查询即失效
    const bar = $("facetBar");
    if (bar) { bar.hidden = true; placeFacetBar(); }   // 隐藏后 placeFacetBar 会把它搬回结果区原位并收起侧栏二分
    // 本来就不在结果态（wasResults=false，如首页打字后删光）：布局没有任何变化，**不得播 FLIP**——
    // 否则 from 哨兵 0 会让 dy=−hero.top，hero 整屏从上方滑回（2026-08-03 缺陷：删空文字输入框上跳）。
    if (!MOTION || !hero || !wasResults) return;
    const dy = from - hero.getBoundingClientRect().top;
    if (Math.abs(dy) > 1) gsap.fromTo(hero, { y: dy }, { y: 0, duration: 0.48, ease: "power3.inOut", clearProps: "transform" });
}
/* 渲染钩子（注册式反转，2026-08-22）：结果区每次重建（新检索/分面重跑/放宽/
   历史回看/失败屏）都通知注册方——projects.js 经 setAfterRenderHook 注册「存为课题」按钮显隐
   与上下文卡重挂。**不留 import 边**（results → projects 会成环：projects 反向 import 本模块
   的分面态），回调是闭包不是依赖。 */
let _afterRenderHook = null;
export function setAfterRenderHook(fn) { _afterRenderHook = (typeof fn === "function") ? fn : null; }

/* 下一步行动阶梯（2026-08-22，设计 §5）：同一注册式反转模式——ladder.js 经
   setLadderRenderHook 注册结果区阶梯 chips 渲染（本模块不 import ladder，单向边不进 SCC）。 */
let _ladderRenderHook = null;
export function setLadderRenderHook(fn) { _ladderRenderHook = (typeof fn === "function") ? fn : null; }

export function renderResults(items, data, imp) {
    _seenBegin();   // 每次结果区重渲 = 一次新展示（上一屏曝光在 begin 内先收尾）
    // 可行性面板的数据是按「点开那一刻的查询」聚合的——任何一次新结果渲染
    // （新检索 / 分面重跑 / 放宽 / 历史回看）都意味着屏上条件已变，面板残留上一轮的数字
    // 就是拿别轮的统计给这轮背书。每次渲染先重置（隐藏+清空）；想看得就新条件重新点。
    // renderFeasibilityBar 只钉按钮显隐，管不到「面板正开着」的情形，所以重置收口在这里。
    const _feasP = $("feasibilityPanel");
    if (_feasP && !_feasP.hidden) { _feasP.hidden = true; _feasP.innerHTML = ""; }
    enterResultsLayout();
    if (_afterRenderHook) _afterRenderHook(data);   // 空态/失败/弃权各分支早退也不漏（data 恒可判 hasResults）
    if (_ladderRenderHook) _ladderRenderHook(data);   // 结果区阶梯 chips / 过宽收窄建议
    const wrap = $("resultsWrap");
    const firstReveal = wrap.style.display !== "block";
    wrap.style.display = "block";
    // 2026-08-03：首页 → 结果区的容器级过渡——整块淡入 + 微升（0.45s power2.out，
    // 克制、不晃眼）。只在 none→block 的真实切换播（细化/继续对话不重播）；卡片自身的
    // stagger 由 revealCards 照旧，MOTION 关/reduced-motion 时直接静落。
    if (firstReveal && MOTION) gsap.from(wrap, { autoAlpha: 0, y: 10, duration: 0.45, ease: "power2.out", clearProps: "all" });
    renderResultSummary(data);     // 方法句 + 计数 + 逐步 trace 合并成一段摘要（替换旧 resultsCount/resultsSource/renderSearchTrace）
    renderBatchSwitcher(data);     // 多批切换器（result_batches>1 才现身；缺失/≤1 批恒 hidden=回退）
    writeComparePool(items);       // 把本次结果写进对比池 localStorage，供「数据集详情」新标签页的「数据集对比」子页取用
    setRememberSearchAvailable(items.length > 0);   // 「记住这次需求」按钮已删；函数 null 守卫后为 no-op（记忆能力保留、待需要时再挂新入口）
    renderCoverageCaveats(data);   // 诚实降级：覆盖缺口渲染进摘要卡内的 #coverageCaveats（有/无结果都渲染）
    renderUnusedQueryTerms(data);  // 静默丢词：无对应筛选维度、被静默丢弃的实义描述词显式回显
    renderOrHandling(data);        // 「A 或 B」实际怎么执行的（同维度多值＝或；跨维度只能同时满足）
    renderActionHint(data);        // 执行类说法（打包/下载脚本/导出引文）：只指路、不代劳
    renderIdentifierLookup(data);  // 标识符精确反查：贴 DOI/编号直达记录 / GEO/SRA 诚实 fail-closed
    renderFeasibilityBar(data);    // 可行性概览入口：有结果时露出「这个方向有多少可复用数据」按钮
    syncTaskPackBar(data);         // 一句话下载入口：有结果时露出「下载这批数据」按钮
    const grid = $("resultsGrid");
    grid.innerHTML = "";
    if (!items.length) {
        // 澄清态（如"不需要fastq"歧义）：不是"没有匹配"，而是需要用户在两种理解间选择。优先于其它空态。
        if (data && data.resolution_status === "clarification_required" && data.clarification) {
            const c = data.clarification;
            const btns = (c.options || []).map((o) =>
                `<button type="button" class="clarify-opt" data-opt="${escapeHtml(o.id)}">${escapeHtml(o.label)}</button>`
            ).join("");
            grid.innerHTML = `<div class="info-bar noresult clarify"><strong>需要确认 FASTQ 条件</strong>
                <p>${escapeHtml(c.detail || "")}</p><div class="clarify-opts">${btns}</div></div>`;
            grid.querySelectorAll(".clarify-opt").forEach((b) =>
                b.addEventListener("click", () => applyClarification(b.dataset.opt)));
            // 空态卡与有结果卡同一入场（MOTION 门控）——否则首次检索的 hero FLIP 位移途中，
            // slim 控制台会扫过已瞬时可见的空态卡（实测「压住 32px」；有结果态靠同一 reveal 天然遮蔽）。
            revealCards(grid.children, false);
            return;
        }
        // 分面细化把结果收窄到 0：不提「放宽 query」（空是筛选所致），提示移除一个筛选。
        // 文案不写死"上方/左侧"——面包屑落位随状态在结果区上方或左侧栏「数据细化」面板间搬家。
        // error:true（网络/后端出错）时不走此支——空不是筛选所致，应如实报错。
        if (_facetFilters.length && !(data && data.error)) {
            grid.innerHTML = `<div class="info-bar noresult"><strong>该筛选组合下没有数据</strong>
                <p>当前细化条件叠加后，没有同时满足的数据。移除其中一个细化条件，通常就能重新看到结果。</p></div>`;
            revealCards(grid.children, false);   // 同上空态入场对齐
            return;
        }
        // error:true（网络/后端出错）：空**不是**筛选/语义所致，如实报「检索失败」，绝不套用「安全弃权」话术甩锅用户查询。
        // 正文与详情分工（2026-08-04）：data.markdown 已是人话（网络层错误在 search.js 翻好），
        // 进正文段落；结构化代号 + 浏览器原始串退进详情 <pre>——上屏不再直接甩「Failed to fetch」。
        if (data && data.error) {
            const emsg = (data && data.markdown) ? escapeHtml(String(data.markdown)) : "";
            const ecode = (data && data.error_code) ? escapeHtml(String(data.error_code)) : "";
            const eraw = (data && data.error_raw && String(data.error_raw) !== String(data.markdown))
                ? escapeHtml(String(data.error_raw)) : "";
            const detail = ecode ? ecode + (eraw ? " · " + eraw : "") : eraw;
            grid.innerHTML = `<div class="info-bar noresult"><strong>检索失败</strong>
                <p>${emsg || "服务未响应或网络异常，请稍后重试；持续失败可在「设置 → 开发者信息 / 诊断」里点「运行诊断」。"}</p>${detail ? `<pre>${detail}</pre>` : ""}</div>`;
            revealCards(grid.children, false);   // 同上空态入场对齐
            return;
        }
        // 弃权态：未收录词自 2026-09-10 起由后端自动降级（下方 degraded 分支），走不到这里；
        // 其余弃权（否定歧义等）如实说明「这次没有做检索」——这**不是**「没有匹配」，
        // 混在「没有匹配的结果」里说，用户会以为库里真没有，转头去别处找。
        if (data && data.resolution_status === "abstained") {
            // 后端的 markdown 首行就是弃权理由（render_no_result → "抱歉，" + abstain_detail），
            // 直接拿来当正文，比前端再编一套说辞诚实，也不会与后端口径漂移。
            // 2026-08-03：后端文案已收敛成一句话（哪个词没收录、去掉可能有结果），
            // 前端不再补第二句（补了就是同一句话说两遍）。
            const raw = String((data && data.markdown) || "").split("\n")[0].replace(/^抱歉，?/, "");
            const why = raw ? escapeHtml(raw)
                : "这句话里有系统无法可靠理解的说法。为了不返回违背你意图的结果，这次没有做检索。";
            grid.innerHTML = `<div class="info-bar noresult"><strong>这次没有做检索</strong>
                <p>${why}</p>
                </div>`;
            revealCards(grid.children, false);
            return;
        }
        const opts = (data && data.relaxation_options) || [];
        // 收编（2026-09-14）：「放松哪个条件」唯一通道 = 输入框上方的 ask.relax 选择卡
        //（零命中批落地后由 board.js maybeAutoRelaxCard 兜底弹出；点选即零 LLM 重搜）。
        // 空态卡只做如实说明 + 指路，不再内嵌 chips（旧就地预览是冻结快照、条件板不同步，已退役）。
        grid.innerHTML = `<div class="info-bar noresult"><strong>没有匹配的结果</strong>
            <p>库里没有同时满足所有条件的数据。一般是条件叠得太严，或句子里有系统没看懂的说法——系统不会拿不满足条件的数据来凑数。${opts.length ? "输入框上方给出了几种放宽方式，选一个就能直接看结果。" : ""}</p></div>`;
        revealCards(grid.children, false);   // 同上空态入场对齐
        return;
    }
    // degraded：LLM 把关档批准了「忽略未收录词」，结果是**降级**来的。绝不能和正常命中长一个样——
    // 用户必须一眼看到「有几个词没被用来筛」，否则就是把降级结果冒充成精确匹配。
    // vector_fallback：解析弃权且降级救不回时，后端改用全库向量语义召回——结果**未经条件核验**，
    // 必须与正常命中/降级结果长得不一样，否则就是把语义相似冒充成条件筛选。
    if (data && data.resolution_status === "vector_fallback") {
        const banner = document.createElement("div");
        banner.className = "relax-banner";
        banner.innerHTML = `<span>这句话的检索条件<b>未能执行</b>——以下按<b>语义相似度</b>给出，<b>未经条件核验</b>，请自行判断是否合用</span>`;
        grid.appendChild(banner);
    }
    if (data && data.resolution_status === "degraded" && data.degraded_search) {
        const d = data.degraded_search;
        const terms = (d.ignored_terms || []).map((t) => `<b>「${escapeHtml(t)}」</b>`).join("、");
        const why = d.llm_reason ? `（AI 判断：${escapeHtml(d.llm_reason)}）` : "";
        const banner = document.createElement("div");
        banner.className = "relax-banner";
        banner.innerHTML = `<span>这批结果<b>忽略了</b>${terms}——系统词表里没有它们，结果<b>没有</b>按它们筛${why}</span>`;
        grid.appendChild(banner);
    }
    items.forEach((it, idx) => grid.appendChild(buildCard(it, { rank: idx + 1 })));
    revealCards(grid.querySelectorAll(".card"), false);
    // 结果区重建（新检索 / 分面重跑 / 放宽 / 历史回看）后把 hero 轮评分卡挂回顶部槽位——
    // 空态/失败/弃权/澄清各分支已在上面 return，那张卡只对「有结果」的屏出现。
    benchfbAfterSearchRender();
    _seenObserve(grid);   // hero/横幅挂完再观察——非卡片节点不影响 .card 名次口径
    _emitImpression(items, imp);   // imp 展示事件 + 每张卡绑不可变归因快照
}
// 澄清态选一个 → 把当前查询改写后重跑：exclude=锚定在 fastq 短语上的『不需要/无需…』→『不要』（硬排除）；ignore=删掉该 fastq 短语（不筛 raw）。
function applyClarification(optionId) {
    const inp = $("queryInput");
    let q = (inp.value || "");
    if (optionId === "exclude_raw") {
        // 2026-08-15：只改写锚定在 fastq/原始数据 短语上的那一处否定词——
        // 此前的 /g 全局替换会把原句其它位置的「不需要」也一并改写（用户原话被静默改动）。
        const anchored = q.replace(/(不需要|无需|无须|不用|不必|没必要)(\s*(?:fastq|原始数据|raw\s*data))/gi, "不要$2");
        // 原句里没有可锚定的否定短语时显式补一条排除条件——否则这次选择是无操作，会再跑回同一个澄清。
        q = (anchored !== q) ? anchored : (q.trim() ? q.trim() + " 不要 fastq" : "不要 fastq");
    } else {
        q = q.replace(/(不需要|无需|无须|不用|不必|没必要)\s*(fastq|原始数据|raw\s*data)/gi, " ")
             .replace(/(fastq|raw\s*data)\s*(not required|not necessary|optional)/gi, " ").trim();
    }
    inp.value = q;
    runRecommend();
}

/* ---------- 分面细化：结果上方按未固定维度一键收窄 ----------
   _facetFilters 是前端唯一真源（含 display/label 供面包屑显示），每次变更都带着它重跑同一查询。
   后端只读 {dim,value}（value 是归一化键），据此在存活集上精确等值过滤 → 计数与结果同源、可复现。
   单选语义：同一维度点第二个值＝替换（该维度选定后即从分面面板收起，靠面包屑撤销）。 */
export let _facetFilters = [];   // [{dim,value,display,label}]
// 被**忽略**的**原始命中**维度（dim 字符串，取自 query_constraints[].dim）。每次重跑随请求带上 → 后端在检索前放宽这些维度。
// 与 _facetFilters 一样是前端唯一真源；换新查询时一并清空（原始命中随新句子重新解析）。
export let _suppressed = [];   // ["species","has_raw_data","date",...]
// 原始命中的**完整快照**（本次查询解析出的全部硬约束 {dim,label,values}，含被忽略的）。
// 忽略某维度后后端的 query_constraints 会少掉它，但 chip 仍要留在原位（灰显、可「恢复」），故前端自留一份完整底稿：
// 仅在 _suppressed 为空（拿到这句话的**纯净**结果）时刷新 → 之后跨忽略/细化保持稳定；换查询清空、回看历史随快照复原。
export let _queryHits = [];   // [{dim,label,values}]
// 诚实降级：被用户「也纳入未标注的」的维度（dim 字符串，取自 coverage_caveats[].dim）。每次重跑随请求带上 →
// 后端把这些维度上**字段为空**的记录视作通过（无法核验≠不匹配），已知不同值仍排除。换查询清空、随历史快照复原。
export let _lenientDims = [];   // ["tissue","disease",...]
/* 维度中文标签单一真源：CB_DIM_LABEL（board_core.js，已从 #board_core import）。
   本区块出现的 dim 域 = 后端 DIMENSIONS 六维（species/tissue/disease/platform/assay/modality），
   是 CB_DIM_LABEL 键集的子集，直用不多不少。 */

/* 可变共享状态只允许属主模块写（迁移约定 §4）：模块之外对这四个数组的**重赋值**一律经 setFacetState
   （ESM live binding 对外只读，消费方经 import 拿到本函数）。patch 只给要换的键；
   原地改（push/splice）不在此列——facets.js 等经 live binding 拿到的就是同一个数组。 */
export function setFacetState(patch) {
    const p = patch || {};
    if (p.facetFilters !== undefined) _facetFilters = p.facetFilters;
    if (p.suppressed !== undefined) _suppressed = p.suppressed;
    if (p.queryHits !== undefined) _queryHits = p.queryHits;
    if (p.lenientDims !== undefined) _lenientDims = p.lenientDims;
}
/* 四个数组一起归零（「换查询即失效」的统一清空）：search.js 新查询、board.js 恢复全部条件。 */
export function resetFacetState() {
    _facetFilters = []; _suppressed = []; _queryHits = []; _lenientDims = [];
}

// 诚实降级层回显：把「满足其它条件、但某维未标注（无法核验）」的覆盖缺口显式告诉用户 + 一键纳入/撤销。
// 不静默判负是这个功能的全部意义——空 caveat 且无宽容 → 整块隐藏。
function renderCoverageCaveats(data) {
    const box = $("coverageCaveats");
    if (!box) return;
    const caveats = (data && data.coverage_caveats) || [];
    const applied = (data && data.applied_lenient) || [];
    if (!caveats.length && !applied.length) { box.hidden = true; box.innerHTML = ""; return; }
    box.hidden = false;
    let html = "";
    if (applied.length) {
        const chips = applied.map((d) =>
            `<button type="button" class="cov-undo" data-cov-dim="${escapeHtml(d)}" title="撤销：重新按标注严格筛选">已纳入未标注「${escapeHtml(CB_DIM_LABEL[d] || d)}」的数据 ✕</button>`
        ).join("");
        html += `<div class="cov-applied">${chips}</div>`;
    }
    if (caveats.length) {
        const rows = caveats.map((c, i) => {
            const srcs = (c.by_source || []).map((s) => `${escapeHtml(prettySource(s.source))} ${s.count} 条`).join("、");
            // 2026-08-01：用户要的是**多档放宽策略选择**，不是「看看是哪些」记录预览。
            // 每行 caveat 的展开给出该维度的两档策略（方向相反、文案各自成立）：
            //   ① lenient＝纳入未标注「X」的 N 条（旧「也纳入」；无法核验≠不匹配，已知不同值仍排除）
            //   ② drop   ＝不按「X」筛选（该条件整个放开，连已知其它取值也进来 → 结果明显变宽）
            const dimLabel = CB_DIM_LABEL[c.dim] || c.dim;
            const expandBtn =
                `<button type="button" class="cov-expand" data-cov-idx="${i}" aria-expanded="false">放宽方式 ▸</button>`;
            const detail =
                `<div class="cov-detail" data-cov-detail="${i}" hidden>`
                + `<button type="button" class="cov-strat" data-cov-how="lenient" data-sdim="${escapeHtml(c.dim)}">`
                + `<b>纳入未标注「${escapeHtml(dimLabel)}」的 ${c.count} 条</b>`
                + `<span>没标注不等于不匹配；已明确标注为其它取值的，仍然排除。</span></button>`
                + `<button type="button" class="cov-strat" data-cov-how="drop" data-sdim="${escapeHtml(c.dim)}">`
                + `<b>不按「${escapeHtml(dimLabel)}」筛选</b>`
                + `<span>这个条件整个放开——连已标注为其它取值的也进来，结果会明显变宽。</span></button>`
                + `</div>`;
            return `<div class="cov-row"><span class="cov-txt">另有 <b>${c.count}</b> 条（${srcs}）满足其它条件，但<b>未标注「${escapeHtml(c.label)}」</b>、无法核验是否匹配。</span>${expandBtn}</div>${detail}`;
        }).join("");
        // 去掉冗余表头「部分数据缺少元数据、已被保守排除…」——每行的「另有 N 条…满足其它条件，但未标注「X」、
        // 无法核验是否匹配」已自含「保守排除、不代表不相关」的诚实含义，表头是重复。直接渲染各行。
        html += rows;
    }
    box.innerHTML = html;
    // 「已纳入未标注 X ✕」撤销 chips（applied_lenient 回显）：只绑撤销 chip 自身，不再罩住整片 [data-cov-dim]
    // （策略按钮改走 data-cov-how 通道，两种触发语义不同，不能混绑）。
    box.querySelectorAll(".cov-undo[data-cov-dim]").forEach((b) =>
        b.addEventListener("click", () => toggleLenient(b.dataset.covDim)));
    // 策略按钮：lenient → 纳入未标注的（toggleLenient）；drop → 不按该维筛（relaxDimFully）。
    box.querySelectorAll(".cov-strat").forEach((b) =>
        b.addEventListener("click", () => {
            if (b.dataset.covHow === "drop") relaxDimFully(b.dataset.sdim);
            else toggleLenient(b.dataset.sdim);
        }));
    // 展开开关：切换该行下方 .cov-detail 的显隐 + ▸/▾。展开态只活在本次渲染里——重新检索/应用宽容后
    // 整块 innerHTML 重画，展开态不保留（可接受：内容本身已随新一批 caveat 换了）。
    box.querySelectorAll("[data-cov-idx]").forEach((b) =>
        b.addEventListener("click", () => {
            const d = box.querySelector(`[data-cov-detail="${b.dataset.covIdx}"]`);
            if (!d) return;
            d.hidden = !d.hidden;
            b.setAttribute("aria-expanded", String(!d.hidden));
            b.textContent = d.hidden ? "放宽方式 ▸" : "放宽方式 ▾";
        }));
}

/* 「不按「X」筛选」（放宽方式第二档 drop）：把该维条件**整个放开**——已标注为其它取值的也进来。
   与「纳入未标注的」（toggleLenient，只放行空字段、已知不同值仍排除）是**相反方向的两档**。
   机制全复用既有原语、无新后端行为：分面筛选里有该维 → 撤掉该维全部分面取值；否则按原始命中忽略
   （_suppressed，与条件栏 chip 的「忽略」同一通道，还原也走既有路径：条件板「已忽略」分区 /
   对话「回退至此」/ 撤销步进条）。 */
function relaxDimFully(dim) {
    if (!dim) return;
    const hadFacet = _facetFilters.some((f) => f.dim === dim);
    if (hadFacet) {
        _facetFilters = _facetFilters.filter((f) => f.dim !== dim);
    } else if (!_suppressed.includes(dim)) {
        _suppressed.push(dim);
    } else {
        return;   // 该维已被忽略（此时本不该再有它的 caveat）→ 不重复推帧
    }
    cbLogPush("refine", "放宽：不按「" + (CB_DIM_LABEL[dim] || dim) + "」筛选");
    runRecommend({ keepFacets: true });
}

// 静默丢词诚实层回显：用户输入了**结构上无对应筛选维度**的实义描述词（性别/年龄/受试者/功能类，如
// 「免疫」「儿童」「男性」），系统既没落维、也没弃权 → 原本静默丢弃零信号。这里显式告诉用户「没按它们过滤」。
// 纯提示、无交互（这些维度系统本就没有，给不出「也纳入」按钮）；空 → 整块隐藏。
function renderUnusedQueryTerms(data) {
    const box = $("unusedQueryTerms");
    if (!box) return;
    const terms = (data && data.unused_query_terms) || [];
    if (!terms.length) { box.hidden = true; box.innerHTML = ""; return; }
    box.hidden = false;
    const chips = terms.map((t) => `<span class="uqt-term">${escapeHtml(String(t))}</span>`).join("");
    box.innerHTML = `<span class="uqt-txt" title="如果需要精确筛选，请改用物种、组织、疾病、平台、技术的规范名称">以下词没有对应的筛选维度，结果<b>未按它们过滤</b>：</span> ${chips}`;
}

// 「A 或 B」的实际处理方式。2026-07-25 之前这类说法**整句弃权**（0 条结果、0 个放宽选项、0 个降级），
// 现在照做——但引擎能表达的「或」只有「同一维度多个值」这一种。三档必须如实播报，否则就是静默偏离：
//   exact    唯一一个维度拿到多值 → 执行的就是用户说的那个「或」
//   superset 多个维度各拿到多值 → 实际是交叉组合，比用户说的搭配更宽
//   narrower 「或」跨了维度 → 只能按「同时满足」执行，比「或」更窄
// 后两档是真实的语义偏离，加一句抬头点明；exact 档只如实告知。空 → 整块隐藏。
// **刻意不改 className**：样式由 index.html 上那几个类（info-bar-caution / unused-query-terms）承载，
// 在 JS 里重写 className 会把它们全冲掉——本仓库栽过一次同型（改选择器比抢 className 稳）。
function renderOrHandling(data) {
    const box = $("orHandling");
    if (!box) return;
    const oh = (data && data.or_handling) || null;
    if (!oh || !oh.note_zh) { box.hidden = true; box.innerHTML = ""; return; }
    box.hidden = false;
    const lead = oh.fit === "exact" ? "" : "<b>这一次和你说的「或」不完全一样：</b>";
    box.innerHTML = `<span class="uqt-txt">${lead}${escapeHtml(String(oh.note_zh))}</span>`;
}

// 执行类说法（「帮我打包前20条」「生成下载脚本」「导出引文」）：这些词此前会让**整句检索炸掉**
// （unresolved_term 弃权，连人类肺数据都查不到）。现在它们不再阻断检索，但也不能就这么吞掉——
// 用户明确说了要打包，界面得告诉他功能在哪儿。只指路、不代劳：产包仍走原来的预览→确认流程。
// 复用 #unusedQueryTerms 那套样式类，不新增 DOM 结构。
/* 核销态跨重渲存续（2026-09-03）：只摘当前 DOM 不够——换批（switchBatch/_batchView 继承落地时的
   action_markers）、分面重跑、历史回看都会重新走本渲染口，把已摘掉的指路条复活（明明执行了，
   却又说「检索本身不包含这一步」）。核销因此记为「这条对话时间线」的状态：一旦动作执行成，
   本时间线内任何重渲都不再出指路条；新时间线由 resetActionHint 复位（search.js runRecommend
   新查询分支调用），那句若也提到执行类说法则照常指路。 */
let _actionHintSettled = false;
function renderActionHint(data) {
    const box = $("actionHint");
    if (!box) return;
    const ruleMarks = (data && data.action_markers) || [];
    const marks = ruleMarks;
    const hasResults = data && Array.isArray(data.results) && data.results.length;
    if (_actionHintSettled || !marks.length || !hasResults) { box.hidden = true; box.innerHTML = ""; return; }
    box.hidden = false;
    const chips = marks.map((t) => `<span class="uqt-term">${escapeHtml(String(t))}</span>`).join("");
    box.innerHTML = `<span class="uqt-txt">你提到了 ${chips}——检索本身<b>不包含</b>这一步。`
        + `结果上方的「📦 下载这批数据」可以直接下载真实文件，也可一次生成清单、下载脚本、FAIR 自检与引文。</span>`;
}

/* 指路条的核销口（2026-08-31）：「检索本身不包含这一步」只在该动作**没被执行**时成立——
   同轮把动作真执行成了（act.js 执行层成功收尾时调本函数），它就自相矛盾，必须摘掉；
   失败/取消不摘（手动入口恰好是那时的正确退路）。 */
export function clearActionHint() {
    _actionHintSettled = true;
    const box = $("actionHint");
    if (!box) return;
    box.hidden = true;
    box.innerHTML = "";
}

/* 新对话时间线的复位口：换查询词重起一段对话时，上一段的核销态随之失效——
   那句若也提到执行类说法，指路条照常指路（调用点：search.js runRecommend 的 !keep 新查询分支）。 */
export function resetActionHint() { _actionHintSettled = false; }

// 标识符精确反查回显：query 本身是标识符时。indexed 且命中 → 直达该数据集卡片；
// GEO/SRA 等本目录不索引的 → 诚实 fail-closed：说清不在本目录、给来源库直达链接，绝不静默返回 0。
// 非标识符 → identifier_lookup=null → 整块隐藏。
function renderIdentifierLookup(data) {
    const box = $("identifierLookup");
    if (!box) return;
    const look = data && data.identifier_lookup;
    if (!look || !look.is_identifier) { box.hidden = true; box.innerHTML = ""; return; }
    box.hidden = false;
    let html = `<div class="idlk-msg"><b>按编号查找</b>：${escapeHtmlStrong(String(look.message || ""))}</div>`;
    // 共享标识符多命中：message 只列编号，候选的名称/来源被丢弃用户无从挑选——
    // 补一份轻量候选清单（复用 arx-card 行样式，与 message 同口径最多 8 条）。
    const cands = Array.isArray(look.candidates) ? look.candidates : [];
    if (cands.length) {
        const rows = cands.slice(0, 8).map(function (c) {
            const meta = [String(c.dataset_name || ""), c.source ? "来源 " + String(c.source) : ""].filter(Boolean).join(" · ");
            return '<div class="arx-card-row"><span class="arx-card-name">' + escapeHtml(String(c.dataset_uid || ""))
                + '</span><span class="arx-card-meta">' + escapeHtml(meta) + "</span></div>";
        }).join("");
        const more = cands.length > 8 ? '<p class="arx-card-empty">其余 ' + (cands.length - 8) + " 条从略，用具体编号查。</p>" : "";
        html += '<div class="arx-card idlk-cands">' + rows + more + "</div>";
    }
    if (look.external_url && isHttp(look.external_url)) {
        html += `<div class="idlk-ext"><a class="btn" href="${escapeHtml(look.external_url)}" target="_blank" rel="noopener noreferrer">到来源网站查看 ${escapeHtml(String(look.value || ""))} ↗</a></div>`;
    }
    box.innerHTML = html;
    if (look.match && typeof look.match === "object") {
        const grid = document.createElement("div");
        grid.className = "dataset-grid idlk-card";
        grid.appendChild(buildCard(look.match, { rank: 1 }));
        box.appendChild(grid);
    }
}

// 可行性概览：有结果时露出入口；点开对当前查询做全命中集聚合（候选数/总细胞量下限/分布/缺口）。
// 按钮收进结果头行（.results-head-acts，独立横带已拆），显隐直接钉按钮本身；展开面板 #feasibilityPanel 仍在结果区原位。
function renderFeasibilityBar(data) {
    const btn = $("feasibilityBtn");
    if (!btn) return;
    const hasResults = data && data.resolution_status === "results" && Array.isArray(data.results) && data.results.length;
    if (!hasResults) { btn.hidden = true; const p = $("feasibilityPanel"); if (p) { p.hidden = true; p.innerHTML = ""; } return; }
    btn.hidden = false;
    btn.onclick = loadFeasibility;   // onclick 覆盖：不叠加监听
}

/* 返回 `{ok, candidate_count?, error?}`。**必须有返回值**：一句话执行层要据此渲染回执——
   没有返回值就只能照「成功」渲染，服务端报错时也会写成「已经统计好了」。
   `collapsed` 表示这次点击是把面板收起来了（切换语义），不是失败。 */
export async function loadFeasibility() {
    const panel = $("feasibilityPanel");
    if (!panel) return { ok: false, error: "页面上没有可行性面板" };
    const query = ($("queryInput") && $("queryInput").value || "").trim();
    if (!query) { toast("请先输入检索需求"); return { ok: false, error: "还没有检索语句" }; }
    if (!panel.hidden) { panel.hidden = true; panel.innerHTML = ""; return { ok: true, collapsed: true }; }   // 再点收起
    panel.hidden = false;
    panel.innerHTML = `<div class="files-hint">正在统计全部匹配结果…</div>`;
    try {
        // 复用主检索同一口径的来源范围（此前误写 selectedSources() —— 该函数不存在，恒 undefined →
        // 后端只统计 base(10x)，与结果头部「库中匹配 N 条」自相矛盾；真名是 getSelectedSources）。
        const body = { query, sources: getSelectedSources(), auto_parse_sources: getSourceMode() === "auto" };
        const res = await fetch(API.feasibility, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        const data = await res.json();
        if (!res.ok || !data.ok || !data.report) throw new Error(data.detail || "服务未返回可行性概览");
        renderFeasibility(panel, data.report);
        return { ok: true, candidate_count: Number(data.report.candidate_count) || 0 };
    } catch (e) {
        const msg = String(e && e.message || e);
        panel.innerHTML = `<div class="files-hint">暂时无法生成（${escapeHtml(msg)}）。</div>`;
        return { ok: false, error: msg };
    }
}

function renderFeasibility(panel, r) {
    const dist = (label, arr) => {
        const items = Array.isArray(arr) ? arr : [];
        if (!items.length) return "";
        return `<div class="feas-dist"><span class="feas-dist-h">${escapeHtml(label)}</span>`
            + items.map((x) => `<span class="feas-chip">${escapeHtml(String(x.value))} <b>${Number(x.count)}</b></span>`).join("") + `</div>`;
    };
    const gaps = Array.isArray(r.gaps) ? r.gaps : [];
    panel.innerHTML =
        `<div class="feas-head">命中 <b>${Number(r.candidate_count)}</b> 条`
        + (r.truncated ? `（本报告统计前 ${Number(r.aggregated_count)} 条）` : "") + `</div>`
        + `<div class="feas-metrics">`
        + `<span>总细胞量下限 <b>${Number(r.total_cells_lower_bound).toLocaleString()}</b>（${Number(r.datasets_with_cell_count)} 条有细胞数 / ${Number(r.datasets_without_cell_count)} 条未标注细胞数）</span>`
        + `<span>含 FASTQ <b>${Number(r.fastq_count)}</b> 条</span>`
        + `<span>有下载链接 <b>${Number(r.downloadable_count)}</b> 条</span>`
        + `</div>`
        + dist("来源", r.sources) + dist("物种", r.species) + dist("平台", r.platforms) + dist("年份", r.years)
        + (gaps.length ? `<ul class="feas-gaps">${gaps.map((g) => `<li>${escapeHtmlStrong(g)}</li>`).join("")}</ul>` : "")
        + `<div class="feas-caveat">${escapeHtmlStrong(r.caveat || "")}</div>`;
}

// 宽容/撤销某维度 → 带着更新后的 _lenientDims 重跑同一查询（keepFacets 保留分面/抑制态）。镜像 toggleSuppress。
export function toggleLenient(dim) {
    if (!dim) return;
    const i = _lenientDims.indexOf(dim);
    const strict = i >= 0;
    if (strict) _lenientDims.splice(i, 1);   // 再点＝撤销宽容、重新严格筛选
    else _lenientDims.push(dim);             // 宽容：后端把该维空字段视作通过
    cbLogPush("refine", (strict ? "改回严格：" : "放宽：") + (CB_DIM_LABEL[dim] || dim));
    runRecommend({ keepFacets: true });
}
