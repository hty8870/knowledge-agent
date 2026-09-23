"use strict";

/* 本文件是 ES Module：core/cards/progress/results/shell 经 import 取。
   allDatasets/bs 自 search.js 末尾归位本文件（它们从来只服务浏览/收藏/历史，search 从不读写——
   早年 search/browse 拆分遗留在那边的）。search 的 runRecommend/applyRecommendResult/bumpRecSeq、
   interactions 的 autoGrow/resetHistClear/initSourceChips、fav_folders 的 renderFavFolderBar/
   renderFavFolderGroups/setFavRerender、reuse_pack 的 syncReuseBar、board 的 renderCondBoard/cbRestoreConversation
   经 import 取（browse↔interactions 成环，绑定都只在函数体内使用，ESM 允许。
   2026-08-10：browse↔fav_folders 环已切——renderFavorites 由本文件在模块求值期
   经 setFavRerender 注册给 fav_folders，fav_folders→browse 反向边消失，见 fav_folders.js 头部）。
   shell/accounts/boot 与 interactions/fav_folders/reuse_pack 同样经 import 取本文件导出
   （绞杀桥已全退役）。 */
import { API, LS, REDUCE_MOTION, $, countUp, escapeHtml, fmtTime, getFavs, getHist, isFav, itemKey, killRevealST, nsKey, openPopup, prettyPlatform, revealCards, toast, writeJSON } from "#core";
import { buildCard } from "#cards";
import { resetSubmitButton } from "#progress";
import { exitResultsLayout, setFacetState } from "#results";
import { getConfig, setKV, showView, closeHistWin, setHistRenderer, setLibRenderer } from "#shell";
import { applyRecommendResult, bumpRecSeq, runRecommend } from "#search";
import { autoGrow, initSourceChips, resetHistClear } from "#interactions";
import { cbAdoptAsBranch, cbRestoreConversation, renderCondBoard, swSync } from "#board";
import { ACCOUNTS_READY } from "#accounts";
import { renderFavFolderBar, renderFavFolderGroups, setFavRerender, setCatalogLookup, setCatalogEnsure } from "#fav_folders";
import { syncReuseBar } from "#reuse_pack";
import { COPY, armTwoStepConfirm } from "../core/copy.js";

/* 注册反转：把 renderFavorites 注册给 fav_folders（它不再 import 本文件，沿 core.js
   setHistHooks 同一范式）。函数声明提升使模块求值期即可注册；fav_folders 侧全部调用点都在
   用户交互路径上，注册必定先于任何触发；不加载本文件的页面（dataset.html）由 fav_folders
   侧的空值守卫兜底——那些页面本就没有收藏视图，调用点本来也不可达。 */
setFavRerender(renderFavorites);
/* 把 catalogLookup 注册给 fav_folders（收藏操作条「更新」需要目录三态访问器；浏览→收藏
   反向边保持切断，fav_folders 不 import 本文件）。 */
if (typeof setCatalogLookup === "function") setCatalogLookup(catalogLookup);
/* 连同目录加载器一起注册——收藏页签「更新」在目录未加载时先拉目录再重查一次
   （否则没进过浏览视图的用户点「更新」永远只得「目录未加载」，重试也落空）。
   ensureDatasetsLoaded 是函数声明，提升后模块求值期可引用。 */
if (typeof setCatalogEnsure === "function") setCatalogEnsure(ensureDatasetsLoaded);

/* ---------- browse 状态（所有来源并列展示，可按来源筛选） ---------- */
let allDatasets = null;
export const bs = { source: "", species: "", platform: "", fastq: false, q: "", yearFrom: "", yearTo: "", page: 1, pageSize: 24 };

let browseYearFacets = [];       // 全库年份集（后端 facet）：时间线 x 轴与下拉的稳定年份列表
let browseUnknownYears = 0;      // 全库未标日期总数（后备/无筛选时的口径）
let browseUnknownYearsLive = 0;  // 当前筛选上下文（不含年份筛选）下的未标日期数（实时口径）
let browseLoadFailed = false;    // 上次全量加载是否失败（区分「加载中」与「加载失败」两种 null 态）
let datasetsGen = 0;             // 数据代际：每次成功拉取全量后 +1（内容签名的一部分——重拉后同名同址记录字段可能已变，必须判旧重建）

function validPublishedYear(value) {
    const match = String(value || "").trim().match(/^(19|20)\d{2}/);
    return match ? Number(match[0]) : null;
}

// 单一真源：后端每条已解析好 published_year(int|null)；老响应无此字段时回退解析 published_date。
function yearOf(it) {
    if (it && it.published_year !== undefined && it.published_year !== null) return Number(it.published_year);
    return validPublishedYear(it && it.published_date);
}

// 当前**非年份**筛选（source/species/platform/q/fastq）下的逐年计数 + 未标日期数。
// 时间线是「年份」维度自身的分面 → 计数刻意**不含年份筛选**（ignoreYear），这样选中某年后其它柱仍
// 按该筛选上下文显示真实条数、可继续切换年份，与 fSource/fSpecies 等其它分面语义一致。
function liveYearCounts() {
    const counts = new Map();
    let unknown = 0;
    for (const it of filteredDatasets({ ignoreYear: true })) {
        const y = yearOf(it);
        if (y === null) unknown++;
        else counts.set(String(y), (counts.get(String(y)) || 0) + 1);
    }
    return { counts, unknown };
}

function renderBrowseTimeline() {
    const years = browseYearFacets.slice().sort((a, b) => Number(a.value) - Number(b.value));
    const { counts, unknown } = liveYearCounts();
    browseUnknownYearsLive = unknown;
    const countOf = (v) => counts.get(String(v)) || 0;
    const from = $("fYearFrom"), to = $("fYearTo"), bars = $("timelineBars");
    // 下拉年份集用全库（任何筛选下都能选任一年），但每项计数展示当前筛选上下文的实时数。
    // 签名未变时跳过 innerHTML 重建：每次 renderBrowse 都走到这里，重建会关掉用户正展开的下拉并丢焦点。
    const yearSig = years.map((f) => f.value + ":" + countOf(f.value)).join("|");
    if (from.dataset.sig !== yearSig) {
        const options = `<option value="">不限</option>` + years.map((f) => `<option value="${escapeHtml(f.value)}">${escapeHtml(f.value)} (${countOf(f.value)})</option>`).join("");
        from.innerHTML = options; to.innerHTML = options;
        from.dataset.sig = yearSig; to.dataset.sig = yearSig;
    }
    from.value = bs.yearFrom; to.value = bs.yearTo;
    // 选中年份若已不在重建后的选项里，<select>.value 会静默回落为 ''；回灌回 bs 保持控件与实际筛选
    // 一致（镜像 fSource/fSpecies/fPlatform 的做法），避免下拉显示「不限」而 filteredDatasets 仍按旧年份过滤。
    bs.yearFrom = from.value; bs.yearTo = to.value;
    const maxCount = Math.max(1, ...years.map((f) => countOf(f.value)));
    bars.innerHTML = years.map((f) => {
        const count = countOf(f.value);
        const height = Math.max(10, Math.round(62 * count / maxCount));
        // 柱本身就是 <button>：不套 role="listitem"（会覆盖 button 语义、aria-pressed 也变非法）。
        return `<button class="timeline-bar" type="button" data-year="${escapeHtml(f.value)}" aria-label="${escapeHtml(f.value)} 年，${count} 条" title="${escapeHtml(f.value)}：${count} 条"><span class="timeline-count">${count}</span><i style="--bar-h:${height}px" aria-hidden="true"></i><em>${escapeHtml(f.value)}</em></button>`;
    }).join("") || `<div class="timeline-empty">暂无可用的发表年份</div>`;
    bars.querySelectorAll(".timeline-bar").forEach((bar) => bar.addEventListener("click", () => {
        const year = bar.dataset.year || "";
        if (bs.yearFrom === year && bs.yearTo === year) { bs.yearFrom = ""; bs.yearTo = ""; }
        else { bs.yearFrom = year; bs.yearTo = year; }
        from.value = bs.yearFrom; to.value = bs.yearTo; bs.page = 1; renderBrowse();
    }));
    syncBrowseTimelineState();
}

function syncBrowseTimelineState() {
    const hasRange = !!(bs.yearFrom || bs.yearTo);
    const label = !hasRange ? "全部年份" : (bs.yearFrom && bs.yearTo && bs.yearFrom === bs.yearTo ? `${bs.yearFrom} 年` : `${bs.yearFrom || "最早"}–${bs.yearTo || "最新"}`);
    $("browseTimelineSummary").textContent = label;
    // 未标日期计数跟随筛选上下文：有其它筛选时用实时口径，避免 aria/文案断言与当前视图不符的数字。
    const hasOtherFilter = !!(bs.source || bs.species || bs.platform || bs.fastq || bs.q);
    const unknownShown = hasOtherFilter ? browseUnknownYearsLive : browseUnknownYears;
    $("timelineUnknown").textContent = `未标注发表日期 ${unknownShown} 条${hasRange ? "（启用年份筛选时不计入结果）" : ""}`;
    $("timelineClear").disabled = !hasRange;
    $("timelineBars").querySelectorAll(".timeline-bar").forEach((bar) => {
        const y = Number(bar.dataset.year);
        const active = (!bs.yearFrom || y >= Number(bs.yearFrom)) && (!bs.yearTo || y <= Number(bs.yearTo));
        bar.classList.toggle("active", hasRange && active);
        bar.setAttribute("aria-pressed", hasRange && active ? "true" : "false");
    });
}

export function applyBrowseYearRange(changed) {
    let from = $("fYearFrom").value, to = $("fYearTo").value;
    if (from && to && Number(from) > Number(to)) {
        if (changed === "from") to = from;
        else from = to;
    }
    bs.yearFrom = from; bs.yearTo = to;
    $("fYearFrom").value = from; $("fYearTo").value = to;
    bs.page = 1; renderBrowse();
}

export async function ensureDatasetsLoaded(force) {
    if (allDatasets && !force) return;
    browseLoadFailed = false;
    $("browseGrid").innerHTML = `<div class="muted-block">加载全部数据集…</div>`;
    // 首载在途窗口就把计数区复位成「—」——否则它整窗显示 HTML 静态默认的
    // 「符合条件 0 条 / 第 1 / 1 页」（还没加载就报 0 条的伪精确；二次进入
    // 走 allDatasets 缓存不经过这里）。与 renderBrowse 的加载中分支同一口径；落地后由它覆写真实值。
    $("browseCount").textContent = "—";
    $("browsePageInfo").textContent = "—";
    $("browsePrev").disabled = true; $("browseNext").disabled = true;
    try {
        const d = await (await fetch(API.datasets)).json();
        allDatasets = d.records || [];
        datasetsGen++;   // 内容可能变了——让 renderBrowse 的内容签名失效、判旧重建
        browseYearFacets = d.facets && d.facets.published_year || [];
        browseUnknownYears = Number(d.unknown_year_count) || 0;
        countUp($("browseTotal"), d.count ?? allDatasets.length);
        const sp = d.facets && d.facets.species || []; const pf = d.facets && d.facets.platform || []; const sc = d.facets && d.facets.source || [];
        $("fSource").innerHTML = `<option value="">全部来源</option>` + sc.map((f) => `<option value="${escapeHtml(f.value)}">${escapeHtml(f.value)} (${f.count})</option>`).join("");
        $("fSpecies").innerHTML = `<option value="">全部物种</option>` + sp.map((f) => `<option value="${escapeHtml(f.value)}">${escapeHtml(f.value)} (${f.count})</option>`).join("");
        $("fPlatform").innerHTML = `<option value="">全部平台</option>` + pf.map((f) => `<option value="${escapeHtml(f.value)}">${escapeHtml(prettyPlatform(f.value))} (${f.count})</option>`).join("");
        // 重建下拉后把已应用的筛选回填到控件；选项若已不存在，.value 自动回落为 ""，再同步回 bs.* 保持一致
        $("fSource").value = bs.source; bs.source = $("fSource").value;
        $("fSpecies").value = bs.species; bs.species = $("fSpecies").value;
        $("fPlatform").value = bs.platform; bs.platform = $("fPlatform").value;
        bs.page = 1; renderBrowse();   // renderBrowse → renderBrowseTimeline，时间线随之建好
    } catch (err) {
        browseLoadFailed = true;
        // 拉取失败别把时间线永久停在「正在统计…」占位——一并复位到明确失败态，不给"还在加载"的错觉。
        allDatasets = null; browseYearFacets = []; browseUnknownYears = 0; browseUnknownYearsLive = 0;
        const bars = $("timelineBars"); if (bars) bars.innerHTML = `<div class="timeline-empty">时间线加载失败</div>`;
        const unk = $("timelineUnknown"); if (unk) unk.textContent = "未标注发表日期 —";
        const sum = $("browseTimelineSummary"); if (sum) sum.textContent = "全部年份";
        // 统一走 renderBrowse 的失败态（带「重试」按钮）；不再在这里直接写无按钮文案——否则**初次进浏览页**
        // 就加载失败时，重试入口只在用户之后碰某个筛选才出现（renderBrowse 的失败分支才有按钮），初次路径上不可达。
        // renderBrowse 的 !allDatasets 分支会 early-return，不重建时间线（见其内注释），故上面的时间线复位保留。
        renderBrowse();
    }
}
/* 目录三态访问器（设计 §4）：区分「真不在目录」与「/api/datasets 加载失败/未完成」。**无 DOM 副作用**，
   只读 allDatasets（allDatasets 私有问题由该访问器封装给 projects/fav_folders 用）。
   load_error 一律不标「已下架」而是「目录未加载，稍后重试」。 */
export function catalogLookup(uid) {
    if (!allDatasets) return { status: "load_error" };
    const u = String(uid);
    const item = allDatasets.find((it) => String(it && it.dataset_uid) === u || itemKey(it) === u);
    return item ? { status: "found", item } : { status: "not_found" };
}
/* 翻页 / 改每页条数后平滑滚回列表顶部：锚点是「数据集」结果头（grid 正上方），
   reduced-motion 下直接跳转（behavior:"auto"），不做平滑。 */
export function scrollBrowseTop() {
    const head = $("browseGrid") && $("browseGrid").previousElementSibling;
    const target = (head && head.classList && head.classList.contains("results-head")) ? head : $("browseGrid");
    if (target && target.scrollIntoView) target.scrollIntoView({ behavior: REDUCE_MOTION ? "auto" : "smooth", block: "start" });
}
function filteredDatasets(opts) {
    const ignoreYear = !!(opts && opts.ignoreYear);   // 算年份分面时忽略年份筛选自身
    const q = bs.q.toLowerCase();
    return (allDatasets || []).filter((it) => {
        if (bs.source && (it.source || "") !== bs.source) return false;
        if (bs.species && it.species !== bs.species) return false;
        if (bs.platform && (it.platform || "") !== bs.platform) return false;
        if (bs.fastq && it.has_raw_data !== true) return false;
        if (!ignoreYear && (bs.yearFrom || bs.yearTo)) {
            const year = yearOf(it);
            if (year === null) return false;
            if (bs.yearFrom && year < Number(bs.yearFrom)) return false;
            if (bs.yearTo && year > Number(bs.yearTo)) return false;
        }
        if (q) { const hay = ((it.dataset_name || "") + " " + (it.tissue || "") + " " + (it.disease || "") + " " + (it.species || "")).toLowerCase(); if (!hay.includes(q)) return false; }
        return true;
    });
}
export function renderBrowse() {
    // allDatasets===null 有两种含义：还在加载 / 加载失败。失败时给明确失败态+重试入口；
    // 否则筛选一变动就会把失败刷成「没有符合条件」，把加载失败伪装成筛选无结果。
    if (!allDatasets) {
        const grid = $("browseGrid"); killRevealST();
        delete grid.dataset.items; delete grid.dataset.favs;   // 占位/失败块覆盖了网格：内容签名作废，下次成功渲染必重建
        if (browseLoadFailed) {
            grid.innerHTML = `<div class="muted-block">数据集加载失败，请稍后重试。 <button class="btn browse-retry" type="button">重试</button></div>`;   // 用户口吻，去「请检查服务」管理员腔
            grid.querySelector(".browse-retry").addEventListener("click", () => ensureDatasetsLoaded(true));
        } else {
            grid.innerHTML = `<div class="muted-block">加载全部数据集…</div>`;
        }
        // 加载中/失败是「还没有数」，不是「0 条」——不显示伪精确的 0 与 0/0 页
        $("browseCount").textContent = "—";
        $("browsePageInfo").textContent = "—";
        $("browsePrev").disabled = true; $("browseNext").disabled = true;
        return;   // 时间线已由 ensureDatasetsLoaded 的 catch 复位，这里不重建、不覆盖失败文案
    }
    const list = filteredDatasets();
    const pages = Math.max(1, Math.ceil(list.length / bs.pageSize));
    if (bs.page > pages) bs.page = pages;
    const start = (bs.page - 1) * bs.pageSize;
    const slice = list.slice(start, start + bs.pageSize);
    countUp($("browseCount"), list.length);
    const grid = $("browseGrid");
    /* 网格重建幂等（2026-08-04）：卡片集没变就不动 DOM；只收藏态变（账户切换）就地翻心形。
       根因之一（「加载后 ~1s 内首次点心形偶发无反应」）：启动期 whoami 落定 →
       onAccountChanged 按「视图==browse」再调一次 renderBrowse（命名空间没变时内容逐字相同），
       旧实现无条件 innerHTML 重建 → revealCards 入场重播（gsap.set autoAlpha:0 + y 位移）——
       刚浮现的心形被打回隐藏/移动态，受信点击在命中测试时落空（JS el.click() 不做命中测试故
       必中、「静置 1.5s 后必中」，两证据互证动画窗口即死因）。修法的机制保证：账户记账引发的
       重渲要么全跳过、要么只翻心形 class——心形永不被打回 autoAlpha:0，加载后任意时刻首点
       必响应；内容真变（筛选/翻页/重拉）才重建并重演入场。签名手法同 renderBrowseTimeline 的
       yearSig；页码/计数/时间线在守卫外照常更新。 */
    const itemsTok = datasetsGen + "\n" + slice.map(itemKey).join("\n");
    const favsTok = slice.map((it) => (isFav(it) ? "1" : "0")).join("");
    /* 点心形不许触发浏览器默认聚焦滚动（2026-08-04）。心形半露出视口边缘时，
       mousedown 的默认聚焦会把页面滚到心形完全可见——这次由开场点击**自己**引发的滚动落在
       popover 的「滚动即关」监听登记**之后**，刚开的 popover 被自己的点击关掉（观感=点了没反应；
       真机插桩实锤：closeFavPopover ← _onFavPopoverPassiveClose ← #document scroll）。
       preventDefault 阻断聚焦即阻断这条滚动；键盘 Tab 聚焦不经 mousedown，可达性不受影响。
       绑在静态 grid 上（卡片重建不影响监听本身），一次即可。 */
    if (!grid.dataset.favGuard) {
        grid.dataset.favGuard = "1";
        grid.addEventListener("mousedown", (e) => { if (e.target.closest(".fav")) e.preventDefault(); });
    }
    if (grid.dataset.items !== itemsTok) {
        grid.dataset.items = itemsTok; grid.dataset.favs = favsTok;
        killRevealST(); grid.innerHTML = "";
        if (!slice.length) grid.innerHTML = `<div class="muted-block">没有符合当前筛选条件的数据集，试试放宽筛选。</div>`;
        else { slice.forEach((it) => grid.appendChild(buildCard(it))); revealCards(grid.querySelectorAll(".card"), true); }
    } else if (grid.dataset.favs !== favsTok) {
        // 同批卡片、只心形态变（whoami 落定/换账户）：就地同步 active，不重建、不重演入场——
        // 心形 click 闭包里的 it 仍有效（同 itemKey 同记录；重拉会 bump datasetsGen 走重建）。
        grid.dataset.favs = favsTok;
        grid.querySelectorAll(".card .fav").forEach((fav, i) => fav.classList.toggle("active", favsTok[i] === "1"));
    }
    $("browsePageInfo").textContent = `第 ${bs.page} / ${pages} 页`;
    $("browsePrev").disabled = bs.page <= 1;
    $("browseNext").disabled = bs.page >= pages;
    // 每次渲染都按当前筛选态重算时间线（柱高/计数/aria-label 跟随筛选，不再是全库静态数）。
    renderBrowseTimeline();
}

/* ---------- favorites / history ---------- */
export function renderFavorites() {
    const favs = getFavs();
    const grid = $("favGrid"); const empty = $("favEmpty");
    grid.innerHTML = "";
    // 复用出处清单的工具条随收藏数同步（空收藏→隐藏；条数变化→收起陈旧清单）。
    // 放在 early-return 之前，否则清空收藏后工具条会留在页面上（reuse_pack.js:syncReuseBar）。
    if (typeof syncReuseBar === "function") syncReuseBar();
    // 收藏夹 tab 条 + 管理面板（fav_folders.js）：同样在 early-return 前同步，空收藏时它自行隐藏。
    if (typeof renderFavFolderBar === "function") renderFavFolderBar(favs);
    if (!favs.length) { empty.style.display = "block"; grid.style.display = "none"; return; }
    empty.style.display = "none"; grid.style.display = "block";
    // 「全部」tab 按夹分组渲染，单夹 tab 平铺（fav_folders.js:renderFavFolderGroups）
    if (typeof renderFavFolderGroups === "function") renderFavFolderGroups(favs, grid);
    revealCards(grid.querySelectorAll(".card"), false);
}
/* 历史按**对话**分组（用户 2026-07-29 反馈）：一段对话会一轮一行地写进历史，
   分开列会把一段连贯的对话拆成一串近乎相同的行。同 `convId` 的行合成一组，组内**新→旧**，
   组的先后按其最新一轮（历史本身就是倒序，故按首次出现即可）。
   老历史行没有 convId → 各自成组，显示与从前逐字相同。 */
function histGroups(hist) {
    const byId = new Map();
    const order = [];
    hist.forEach((h, i) => {
        const key = (h && h.convId) ? String(h.convId) : "legacy:" + i;
        if (!byId.has(key)) { byId.set(key, []); order.push(key); }
        byId.get(key).push(h);
    });
    return order.map((k) => byId.get(k));
}

/* 点历史项：回看**整条对话**——最后一轮的结果 + 全部对话记录，且每一轮的快照都还在（可逐条「查看结果」）。
   `group` 是 histGroups 的一组，**新→旧**；这里先翻成旧→新再交给 cbRestoreConversation。
   老格式历史（无 snap）→ 回退到重跑。 */
function viewHistorySnapshot(group) {
    const chron = Array.isArray(group) ? group.slice().reverse() : [group];   // 旧→新
    const h = chron[chron.length - 1];                                        // 落在最后一轮上
    // 仅对话行（2026-08-04）：当时一句检索都没跑过，没有结果快照可落——如实回到
    // 「只有对话没有结果」的态：作废在途、退出结果态（屏上旧结果/分面/条件板随之收起；
    // 其 cbClear 会先归档屏上未归档的对话，同一口径），再把整条对话搬回主区。
    if (h && h.chatOnly) {
        bumpRecSeq();
        resetSubmitButton();
        showView("query");
        const qi = $("queryInput");
        if (qi) { qi.value = ""; autoGrow(qi); }   // 仅对话：没有「当前检索句」
        exitResultsLayout();
        cbRestoreConversation(chron);
        swSync();   // 无结果 → cbChatInMain 真，恢复出的对话落进主区 #chatMain
        toast("已回看这条对话（当时只有对话、没有检索）");
        return;
    }
    if (!h || !h.snap || !h.snap.results) {   // 迁移兜底：老格式无快照 → 重跑（runRecommend 自带 _recSeq 递增与收尾）
        if (h && h.snap_evicted) {
            // 快照是配额不足被剥掉的——接下来是**重新检索**，不是「当时的结果」，如实先说。
            toast("那一次的结果快照没存下来（空间不足被剥掉了），正在按原句重新检索");
        }
        showView("query"); $("queryInput").value = (h && h.query) || ""; autoGrow($("queryInput")); runRecommend(); return;
    }
    // 关键：让**在途 runRecommend** 失效——否则它稍后到达时 myGen===_recSeq 仍会落地，
    // 把刚渲染的快照顶掉、且用当前（快照的）_facetFilters 回灌一条错配历史。
    bumpRecSeq();   // 属主是 search.js（ESM）：作废在途 runRecommend 必经其写口（原 ++_recSeq 裸写在 getter 桥上会 TypeError）
    resetSubmitButton();   // 取消在途请求的进度 rAF + 同步复位（其 finally 因 myGen 失配会跳过收尾）
    showView("query");
    $("queryInput").value = h.query || "";
    autoGrow($("queryInput"));
    setFacetState({   // 四个分面状态的属主是 results.js（ESM），重赋值必经属主 setter（live binding 只读）
        facetFilters: Array.isArray(h.facetFilters) ? h.facetFilters.map((f) => ({ ...f })) : [],
        suppressed: Array.isArray(h.suppressed) ? h.suppressed.slice() : [],   // 一并恢复原始命中抑制态（老快照无此字段→空）
        queryHits: Array.isArray(h.queryHits) ? h.queryHits.map((g) => ({ filter_id: g.filter_id || g.dim, polarity: g.polarity || "include", dim: g.dim, label: g.label, values: (g.values || []).slice() })) : [],   // 恢复原始命中完整快照 → 被忽略的 chip 也随回看正确重现（老快照无 filter_id→回退用 dim）
        lenientDims: Array.isArray(h.lenientDims) ? h.lenientDims.slice() : []   // 恢复诚实降级宽容态（老快照无此字段→空），使回看后再点 caveat 从正确基准 toggle
    });
    applyRecommendResult(h.snap, h.query, { fromHistory: true });   // fromHistory → 不再回灌历史
    // 条件板显式重建成这条对话：否则「回到上一步」会跳到另一条查询的时间线上去。
    // 顺序不能反：renderCondBoard 里的 cbRenderSteps 会按「前面还有几步」隐掉提示行，
    // 而 cbRestoreConversation 最后写的正是那句「这是从历史里回看的…」——先板后提示，提示才留得住。
    renderCondBoard(h.snap);
    cbRestoreConversation(chron);
    toast(chron.length > 1 ? `已回看这条对话，共 ${chron.length} 轮` : "已回看这次检索的结果快照");
}
/* ---------- 历史记录（独立浮窗 #histWin；后拆回单页签）----------
   浮窗骨架（开合/拖动/缩放/落位）唯一属主在 shell.js（initHistWinSkeleton），
   本文件只管历史渲染：initHistWin 把 renderHistory 经 setHistRenderer 注册进历史浮窗。
   行交互（2026-08-03 用户反馈）：**点行本体**＝在本标签页找回这条对话（viewHistorySnapshot：
   对话记录+细化+最后一轮结果整体找回；老格式无快照自动回退重跑）；行尾三个动作——
   新标签页打开（?conv= 链接）、按当前库重新检索、删除（二段确认，仿 histClear armed 模式）。 */

export function renderHistory() {
    const hist = getHist();
    const list = $("histList"); const empty = $("histEmpty");
    // 空历史禁用「清空」；每次渲染复位二段确认态（resetHistClear 在 interactions.js 定义，运行时必已加载）
    const clearBtn = $("histClear");
    if (clearBtn) { clearBtn.disabled = !hist.length; if (typeof resetHistClear === "function") resetHistClear(); }
    if (!list) return;
    list.innerHTML = "";
    if (!hist.length) { if (empty) empty.hidden = false; list.style.display = "none"; return; }
    if (empty) empty.hidden = true; list.style.display = "block";
    histGroups(hist).forEach((g) => {
        const h = g[0];   // 组内新→旧：g[0] 就是这条对话的最后一轮
        // 多轮对话只列最后一句 + 轮数。中间几句在点开后的对话记录里，不在这里堆成一屏。
        const turns = g.length > 1 ? ` · 共 ${g.length} 轮` : "";
        // 仅对话行：没有「N 条结果」可报——照实报「仅对话 · N 条消息」；也没有可重跑的检索句
        // （首句很可能是工具句），不给「重新检索」按钮。
        const chatOnly = !!h.chatOnly;
        const meta = chatOnly
            ? `仅对话 · ${(Array.isArray(h.chat) ? h.chat.length : 0)} 条消息 · ${escapeHtml(fmtTime(h.at))}`
            : `${h.count} 条 · ${escapeHtml(fmtTime(h.at))}${turns}`;
        const mainTitle = chatOnly
            ? "在本标签页找回这条对话：完整对话记录（当时只有对话、没有检索）"
            : "在本标签页找回这条对话：完整对话记录、每一步细化、最后一轮的检索结果";
        const row = document.createElement("div"); row.className = "hist-row";
        row.innerHTML = `<button class="hist-main" type="button" title="${mainTitle}"><span class="hist-txt"><span class="hist-q">${escapeHtml(h.query || "（仅对话）")}</span>`
            + `<span class="hist-meta">${meta}</span></span></button>`
            + `<div class="hist-acts">`
            + (h.convId ? `<button class="btn hist-newtab" type="button" title="在新浏览器标签页中打开这条对话">新标签页</button>` : "")
            + (chatOnly ? "" : `<button class="btn hist-rerun" type="button" title="按当前库重新检索这句话">重新检索</button>`)
            + `<button class="btn hist-del" type="button" title="删除这条对话">删除</button></div>`;
        // 点行本体＝在本标签页找回（无快照的老行在 viewHistorySnapshot 内自动回退重跑）
        row.querySelector(".hist-main").addEventListener("click", () => { viewHistorySnapshot(g); closeHistWin(); });
        const nt = row.querySelector(".hist-newtab");
        if (nt) nt.addEventListener("click", () => {
            openPopup(location.pathname + "?conv=" + encodeURIComponent(h.convId));
        });
        const rr = row.querySelector(".hist-rerun");
        if (rr) rr.addEventListener("click", () => { $("queryInput").value = h.query; closeHistWin(); showView("query"); runRecommend(); });
        // 删除：二段确认（仿 histClear 的 armed 模式）——3 秒内再点才执行，超时自动复位
        const del = row.querySelector(".hist-del");
        del.addEventListener("click", () => {
            if (!armTwoStepConfirm(del, { idleText: COPY.common.delete })) return;
            deleteHistoryGroup(g);
            toast("已删除这条对话");
        });
        list.appendChild(row);
    });
}

/* 删除一条对话：convId 相同的所有行一起删（一段对话产多行历史）；
   无 convId 的老行按 query+at 定点删（毫秒戳实际上不会撞）。删完重渲浮窗。 */
function deleteHistoryGroup(g) {
    const convId = (g[0] && g[0].convId) ? String(g[0].convId) : "";
    const victim = String((g[0] && g[0].query) || "") + "@" + String((g[0] && g[0].at) || 0);
    const next = getHist().filter((h) => {
        if (h && h.convId) return String(h.convId) !== convId;
        return String((h && h.query) || "") + "@" + String((h && h.at) || 0) !== victim;
    });
    // 与 histClear 同口径：必须写**当前账户命名空间**键（nsKey），写裸 LS.hist 是账户隔离漏洞。
    writeJSON(nsKey(LS.hist), next);
    renderHistory();
}

export function initHistWin() {
    /* 2026-08-23：历史拆回独立浮窗 #histWin（骨架在 shell.js initHistWinSkeleton），
       收藏迁入 #libWin 收藏页签；本函数（boot.js 调用不变）职责：① 把历史渲染注册进 #histWin
       （setHistRenderer 注册反转）与收藏渲染注册进 #libWin favs 页签（setLibRenderer）；② ?conv=/?fork= 落点处理。 */
    setHistRenderer(renderHistory);
    setLibRenderer("favs", renderFavorites);
    // ?conv=：新标签页打开某条对话（同一 localStorage）。读完即擦掉参数——刷新不重复触发。
    // ?fork=<convId>:<N>：分支落点——以第 N 轮为起点，把前 N 轮历史重建进本标签页，
    // 并换新 convId（cbAdoptAsBranch）：之后这条分支的检索与原对话分成两条历史，互不串行。
    // 两个落点都必须等启动期 whoami 落定（ACCOUNTS_READY）再跑：onAccountChanged 里有 cbClear()，
    // whoami 晚回来一步会把刚找回的对话再清掉（真实复现过的竞态——分支新标签页聊天记录为空）。
    try {
        const params = new URLSearchParams(location.search);
        const conv = params.get("conv");
        const fork = params.get("fork");
        if (conv || fork) {
            history.replaceState(null, "", location.pathname);
            const run = () => {
                if (conv) {
                    const g = histGroups(getHist()).find((grp) => String((grp[0] && grp[0].convId) || "") === conv);
                    if (g) { viewHistorySnapshot(g); toast("已打开这条对话"); }
                    else toast("这条对话不在本机的历史记录里");
                }
                if (fork) {
                    const sep = fork.lastIndexOf(":");
                    const convId = sep > 0 ? fork.slice(0, sep) : fork;
                    const n = Math.max(1, parseInt(sep > 0 ? fork.slice(sep + 1) : "1", 10) || 1);
                    const g = histGroups(getHist()).find((grp) => String((grp[0] && grp[0].convId) || "") === convId);
                    if (g) {
                        const chron = g.slice().reverse();   // 组内新→旧 翻成 旧→新
                        const rows = chron.slice(0, Math.min(n, chron.length));   // 只取前 N 轮（分支起点）
                        if (chron.length < n) toast("原对话在历史里只剩一部分，已按现存内容分支");
                        viewHistorySnapshot(rows.slice().reverse());   // viewHistorySnapshot 吃「新→旧」
                        cbAdoptAsBranch();
                        toast("已从这里分支，接着说就会走在这条分支上");
                    } else {
                        toast("这条对话不在本机的历史记录里，无法分支");
                    }
                }
            };
            if (ACCOUNTS_READY && typeof ACCOUNTS_READY.then === "function") ACCOUNTS_READY.then(run);
            else run();
        }
    } catch (_e) {}
}

/* ---------- upload ---------- */
function isJson(f) { return String(f && f.name || "").toLowerCase().endsWith(".json"); }
export async function uploadFile(file) {
    if (!file) { toast("请先选择文件"); return; }
    if (!isJson(file)) { $("uploadStatus").textContent = "仅支持 JSON"; toast("仅支持 JSON 文件"); return; }
    const fd = new FormData(); fd.append("file", file);
    const src = ($("uploadSource").value || "").trim();
    const url = API.upload + (src ? ("?source=" + encodeURIComponent(src)) : "");
    const warnBox = $("uploadWarn"); warnBox.hidden = true; warnBox.innerHTML = "";
    const btn = $("uploadBtn"); btn.disabled = true; btn.textContent = "上传中…"; $("uploadStatus").textContent = "上传中…";
    try {
        const d = await (await fetch(url, { method: "POST", body: fd })).json();
        if (!d.ok) throw new Error(d.detail || "上传失败");
        const srcSummary = d.sources && Object.keys(d.sources).length
            ? Object.entries(d.sources).map(([k, v]) => `${k} ${v} 条`).join("、")
            : "";
        $("uploadStatus").textContent = `成功：${d.filename} · 已加入 ${d.record_count} 条` + (srcSummary ? ` · 来源：${srcSummary}` : "");
        if (Array.isArray(d.warnings) && d.warnings.length) {
            warnBox.innerHTML = d.warnings.map((w) => `<div>⚠ 提示：${escapeHtml(w)}</div>`).join("");
            warnBox.hidden = false;
        }
        toast("上传成功");
        ensureDatasetsLoaded(true);
        initSourceChips();
    } catch (err) { $("uploadStatus").textContent = "上传失败，可检查文件是否为有效的 JSON 后重试。原因：" + (err && err.message ? err.message : String(err)); toast("上传失败"); }
    finally { btn.disabled = false; btn.textContent = "上传"; }
}

/* ---------- diagnose ---------- */
export async function runDiagnose() {
    const cfg = getConfig(); toast("正在诊断…");
    try {
        const health = await (await fetch(API.health)).json();
        const diagResponse = await fetch(API.diagnose, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                provider: cfg.provider,
                use_llm: cfg.use_llm,
                mock_llm: cfg.mock_llm,
                api_key: cfg.api_key,
                base_url: cfg.base_url,
                model: cfg.model,
            }),
        });
        const diag = await diagResponse.json();
        if (!diagResponse.ok) throw new Error(diag.detail || `HTTP ${diagResponse.status}`);
        const conn = diag.healthcheck && diag.healthcheck.Connection;
        setKV($("stPipeline"), "health → diagnose");
        setKV($("stProvider"), cfg.provider);
        // 诊断语境下连接失败就是真故障（显式 tone:"no" 染红；日常状态 false 保持中性）
        setKV($("stSucceeded"), conn === "success", conn === "success" ? "ok" : "no");
        setKV($("stUsed"), conn || "-");
        setKV($("stFallback"), `health=${health.ok}, diagnose=${conn || "unknown"}`);
        toast("诊断完成");
    } catch (err) { setKV($("stFallback"), "diagnose failed: " + err); toast("诊断失败"); }
}
