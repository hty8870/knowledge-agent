"use strict";
/* ==================== 数据集介绍详情页 ====================
   「查看介绍」从模态弹窗改为**独立浏览器标签页** `/dataset?uid=…&url=…&name=…&source=…`。
   数据来源两路：
     1) 打开介绍前，主页 buildCard 的链接把**完整记录**写进 localStorage(DS_VIEW_KEY)；本页按 uid/url 取回
        （含 download_url / n_files / reachability 等 URL 参数带不了的字段）。
     2) 直接打开 / 分享 URL（无 handoff）→ 用 URL 参数搭最小记录，介绍正文仍可由 /api/introduction 补全，
        页头缺字段如实留空（不臆造下载链接）。
   内容渲染全部复用 cards.js：renderIntroduction/loadIntroductionInto（介绍）、loadFilesInto（全部文件）、
   loadCompatInto→renderCompatCards（元数据兼容，全检索卡）、loadFairInto（FAIR/DAS）、loadCiteInto（引文）。
   分屏 → 子标签页：介绍 / 全部文件 / 元数据兼容 / 投稿可用性与 FAIR / 导出引文 / 数据集对比。

   本文件是 ES Module：core/cards/fav_folders 经 import 取（同页三模块全 type=module，
   不再依赖共享全局）。本页无经典消费方、无跨页调用 → 不挂绞杀桥。 */
import { HEART, $, escapeHtml, fastqInfo, isFav, isHttp, normalizeItem, prettyPlatform, prettySource, revealCards, loadDomainManifest, brandName } from "#core";
import { buildCard, closeFilesModal, introKeyOf, loadCiteInto, loadCompatInto, loadFairInto, loadFilesInto, loadIntroductionInto, loadLlmIntro, reachBadgeHtml } from "#cards";
import { openFavPopover } from "#fav_folders";
import { initDownloads } from "#downloads";

const DS_VIEW_KEY = "biodata_dataset_view_v1";          // 主页写入的完整记录（打开介绍前）
const DS_COMPARE_POOL_KEY = "biodata_compare_pool_v1";  // 主页每次检索写入的对比池（存完整归一化记录）
const DS_SPLIT_KEY = "biodata_split_handoff_v1";        // 全屏并排：开分屏前按 uid 写入两侧完整记录，供 embed iframe 各自取回

// 嵌入态：分屏浮层里的两侧各是一个 `?embed=1` 的完整 /dataset iframe。嵌入态隐藏自己的返回主页 topbar，
// 并从子标签里丢掉「数据集对比」（不在对比里再开对比 → 防嵌套分屏）。模块加载时定一次。
const DS_EMBED = new URLSearchParams(location.search).get("embed") === "1";

let DS_ITEM = null;      // 当前数据集（normalizeItem 形状）
let _dsHandoffDegraded = false;   // 完整记录 handoff 未取到、按 URL 参数搭了最小记录（页头如实标注）
const _dsLoaded = {};    // 各子标签页是否已懒加载

function dsParams() {
    const p = new URLSearchParams(location.search);
    return { uid: p.get("uid") || "", url: p.get("url") || "", name: p.get("name") || "", source: p.get("source") || "" };
}

/* split handoff：按 uid 取回开分屏时写入的完整记录（两个 iframe 读同一 localStorage，故需按 uid 各取各的）。 */
function dsReadSplitHandoff(uid) {
    if (!uid) return null;
    try {
        const map = JSON.parse(localStorage.getItem(DS_SPLIT_KEY) || "null");
        if (map && typeof map === "object" && map[uid] && typeof map[uid] === "object") return map[uid];
    } catch (_e) {}
    return null;
}

/* 取完整记录：优先 split handoff（分屏 embed 页按 uid）→ 按 uid 分键 handoff（2026-08-15 起，
   多标签连开不同数据集互不覆盖）→ 旧单槽 handoff（uid 或 url 对得上）→ 否则 URL 参数搭最小记录。 */
function dsResolveItem(params) {
    const split = dsReadSplitHandoff(params.uid);
    if (split) return normalizeItem(split);
    let raw = null;
    // 按 uid 分键是主通道：先开/后开的标签各取各的，不再互相覆盖。
    if (params.uid) {
        try { raw = JSON.parse(localStorage.getItem(DS_VIEW_KEY + ":" + params.uid) || "null"); } catch (_e) { raw = null; }
        if (raw && typeof raw === "object") return normalizeItem(raw);
    }
    try { raw = JSON.parse(localStorage.getItem(DS_VIEW_KEY) || "null"); } catch (_e) { raw = null; }
    const matches = raw && typeof raw === "object" && (
        (params.uid && raw.dataset_uid === params.uid) ||
        (params.url && raw.url === params.url) ||
        (!params.uid && !params.url)
    );
    if (matches) return normalizeItem(raw);
    // 完整记录没取到（双标签竞态被覆盖/直接打开链接等）：降级为 URL 参数最小记录，
    // 并在页头如实标注「部分字段缺失」（此前静默降级，页头信息残缺而无任何提示）。
    _dsHandoffDegraded = true;
    return normalizeItem({ dataset_uid: params.uid, url: params.url, dataset_name: params.name, source: params.source });
}

/* ---------- 子标签页定义（顺序固定）---------- */
function dsTabDefs(it) {
    const nFiles = Number(it.n_files || 0);
    const defs = [
        { key: "intro", label: "介绍" },
        { key: "files", label: nFiles > 0 ? `全部文件（${nFiles}）` : "全部文件" },
        { key: "compat", label: "元数据兼容的数据集" },
        { key: "fair", label: "投稿可用性与 FAIR 自检" },
        { key: "cite", label: "导出引文 RIS/BibTeX" },
        { key: "compare", label: "数据集对比" },
    ];
    // 嵌入态（分屏浮层里的 iframe）不给「数据集对比」——不在对比里再开对比（防嵌套分屏）。
    return DS_EMBED ? defs.filter((t) => t.key !== "compare") : defs;
}

function dsRenderTabs(it) {
    const nav = $("dsTabs");
    if (!nav) return;
    nav.innerHTML = dsTabDefs(it).map((t, i) =>
        `<button class="ds-tab${i === 0 ? " active" : ""}" type="button" role="tab" id="dstab-${t.key}"`
        + ` aria-selected="${i === 0 ? "true" : "false"}" aria-controls="dsPanel-${t.key}" data-tab="${t.key}">${escapeHtml(t.label)}</button>`
    ).join("");
    nav.querySelectorAll(".ds-tab").forEach((b) => b.addEventListener("click", () => dsActivate(b.dataset.tab)));
}

function dsActivate(key) {
    const nav = $("dsTabs");
    if (nav) nav.querySelectorAll(".ds-tab").forEach((b) => {
        const on = b.dataset.tab === key;
        b.classList.toggle("active", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
    });
    dsTabDefs(DS_ITEM).forEach((t) => { const p = $("dsPanel-" + t.key); if (p) p.hidden = t.key !== key; });
    if (!_dsLoaded[key]) { _dsLoaded[key] = true; dsLoadPanel(key); }
    try { history.replaceState(null, "", location.pathname + location.search + "#" + key); } catch (_e) {}
    const panel = $("dsPanel-" + key);
    if (panel) panel.scrollTop = 0;
}

function dsLoadPanel(key) {
    const it = DS_ITEM, panel = $("dsPanel-" + key);
    if (!panel) return;
    if (key === "intro") return;   // 介绍在 dsInit 已渲染
    if (key === "files") { loadFilesInto(panel, it); return; }
    if (key === "compat") { loadCompatInto(panel, it); return; }
    if (key === "fair") { loadFairInto(panel, it); return; }
    if (key === "cite") { loadCiteInto(panel, it); return; }
    if (key === "compare") { dsRenderCompare(panel); return; }
}

/* ---------- 页头（标题 / 徽章 / 来源·日期 / 主操作 / 收藏心形）---------- */
/* 品牌名随活动领域包（biodata 默认「BioData Agent」，与历史逐位一致）；首渲染未解析到就用默认，
   清单到达后按当前条目名回写一次标题，保证换域部署下标题也对。 */
let _brandName = "BioData Agent";
loadDomainManifest().then((m) => {
    _brandName = brandName(m);
    if (DS_ITEM && DS_ITEM.dataset_name) document.title = DS_ITEM.dataset_name + " · " + _brandName;
});
function dsRenderHeader() {
    const it = DS_ITEM;
    const name = it.dataset_name || "数据集介绍";
    document.title = name + " · " + _brandName;
    $("dsTitle").textContent = name;

    const fq = fastqInfo(it.raw_data_status);
    const reachBadge = reachBadgeHtml(it);   // 可达性徽章与结果卡同一锚点（cards.js）
    const metaBits = [prettyPlatform(it.platform), prettySource(it.source)].filter(Boolean)
        .map((m, i) => (i ? `<span class="dot">·</span>` : "") + escapeHtml(m)).join("");
    $("dsBadges").innerHTML = `<span class="fastq ${fq.cls}">${fq.label}</span>${reachBadge}`
        + (metaBits ? `<span class="ds-meta">${metaBits}</span>` : "");

    const subBits = [];
    // handoff 完整记录未取到：如实标注，不再静默展示残缺页头
    if (_dsHandoffDegraded) subBits.push("完整信息未取到，部分字段缺失");
    // 来源已在上方徽章行显示（prettySource），此处不再重复
    if (it.published_date) subBits.push("发表 " + escapeHtml(String(it.published_date).slice(0, 10)));
    const facts = [["物种", it.species], ["组织", it.tissue], ["疾病", it.disease]].filter(([, v]) => v)
        .map(([k, v]) => `${k} ${escapeHtml(v)}`);
    $("dsSub").innerHTML = subBits.concat(facts).join(' <span class="dot">·</span> ');

    // 主操作：与检索卡同口径——有独立数据集页则「打开数据集页」为主按钮、下载为软按钮；否则下载升主按钮。
    const dl = it.download_url || it.url;
    const hasPage = isHttp(it.url) && it.url !== dl;
    const pageBtn = hasPage ? `<a class="cta cta-primary" href="${escapeHtml(it.url)}" target="_blank" rel="noopener">打开数据集页 ↗</a>` : "";
    const dlBtn = isHttp(dl)
        ? `<a class="cta ${hasPage ? "cta-soft" : "cta-primary"}" href="${escapeHtml(dl)}" target="_blank" rel="noopener" download data-dlq="data" data-dlq-uid="${escapeHtml(it.dataset_uid || "")}" data-dlq-name="${escapeHtml(it.dataset_name || "")}">下载数据 ↓</a>`
        : `<span class="cta cta-disabled" aria-disabled="true">暂无下载</span>`;
    $("dsActions").innerHTML = pageBtn + dlBtn;

    const fav = $("dsFav");
    fav.innerHTML = HEART;
    fav.classList.toggle("active", isFav(it));
    fav.addEventListener("click", (e) => { e.stopPropagation(); openFavPopover(fav, it); });
}

/* ---------- 介绍面板（复用 loadIntroductionInto；末尾按体裁条件挂 AI 导读按钮）---------- */
async function dsRenderIntro() {
    const panel = $("dsPanel-intro"), it = DS_ITEM;
    await loadIntroductionInto(panel, it);   // 有 handoff.introduction 直接绑，否则拉 /api/introduction（bindIntroductionBody 已去 syncIntroTabs 耦合）
    dsAppendLlmButton(panel);
    // 介绍到达后 n_files/genre 可能变化：重渲 tab 栏（保当前激活态）以更新「全部文件 N」计数。
    dsRefreshFilesTabLabel();
}

function dsRefreshFilesTabLabel() {
    const btn = document.getElementById("dstab-files");
    if (!btn) return;
    const nFiles = Number(DS_ITEM.n_files || 0);
    btn.textContent = nFiles > 0 ? `全部文件 ${nFiles}` : "全部文件";
}

/* AI 中文导读：只有「有真散文可译」的体裁（prose/title）+ 尚未请求过才挂按钮；请求后由上方导读块/降级提示取代。 */
function dsAppendLlmButton(panel) {
    const it = DS_ITEM;
    const intro = (it.introduction && typeof it.introduction === "object") ? it.introduction : null;
    if (!intro) return;
    const genre = intro.genre || "";
    if (!((genre === "prose" || genre === "title") && !intro.llm_summary && !intro.llm_status)) return;
    const wrap = document.createElement("div");
    wrap.className = "ds-llm-cta";
    const btn = document.createElement("button");
    btn.className = "btn ds-llm-btn"; btn.type = "button"; btn.textContent = "🤖 AI 中文导读（实验性）";
    btn.addEventListener("click", async () => { await loadLlmIntro(panel, it, btn); dsAppendLlmButton(panel); });
    wrap.appendChild(btn);
    panel.appendChild(wrap);
}

/* ---------- 数据集对比：从主页写入的对比池（最近一次检索结果）里挑一条，并排对照关键信息 ---------- */
function dsReadComparePool() {
    let pool = [];
    try { pool = JSON.parse(localStorage.getItem(DS_COMPARE_POOL_KEY) || "[]"); } catch (_e) { pool = []; }
    if (!Array.isArray(pool)) pool = [];
    const curKey = introKeyOf(DS_ITEM);
    return pool.map(normalizeItem).filter((it) => it.dataset_name && introKeyOf(it) !== curKey);
}

/* 全屏并排对比：对比子标签是**启动器**——从最近检索池选一条 → dsOpenSplit 打开全屏分屏浮层，
   左右各是一个完整的 /dataset 页（iframe，embed 态），可各自完整操作、可关掉一侧保留另一侧。
   （替代旧版在子标签内塞两个 bare 介绍正文的窄栏；用户澄清「分屏」是全屏意义上的分屏。）

   2026-07-29：候选从 `<select>` 下拉改为**铺开的结果卡网格**。理由是选下拉要先点开、再逐行读
   一串「名字（来源）」——那串文字里既没有物种/组织/疾病，也没有 FASTQ 状态，正是决定「要不要拿它来对比」
   的全部依据。卡片直接复用检索结果的 `buildCard`（**同一个函数**，不是照着做一份像的：照做必漂移），
   于是这里看到的每一张与检索页逐像素同构，心形收藏、数据集详情、下载直链一并可用。 */
function dsRenderCompare(panel) {
    const rows = dsReadComparePool();
    if (!rows.length) {
        panel.innerHTML = `<div class="files-hint">没有可对比的数据集。先在检索页做一次检索，再从结果里打开某个数据集的介绍即可对比。</div>`;
        return;
    }
    panel.innerHTML = `<div class="compare-hint">从最近一次检索结果里挑一个，与当前数据集<b>全屏并排</b>对照。两侧都是完整介绍页，可独立操作，也可随时关掉一侧只看另一边。</div>`
        + `<div class="ds-compare-n">最近一次检索结果里的 ${rows.length} 个候选</div>`
        + `<div class="grid ds-compare-grid"></div>`;
    const grid = panel.querySelector(".ds-compare-grid");
    rows.forEach((it) => {
        const card = buildCard(it);
        // 「并排对比」是这张卡在**本页**的主动作，放在卡底与「查看全部 N 个文件」同一档全宽按钮位，
        // 不去挤 .card-cta 那两颗（300px 卡宽塞三颗会挤成三行竖排）。
        const go = document.createElement("button");
        go.type = "button";
        go.className = "ds-compare-go-card";
        go.innerHTML = `<span>并排对比</span><span class="dcg-arrow" aria-hidden="true">↔</span>`;
        go.setAttribute("aria-label", "与当前数据集并排对比 " + (it.dataset_name || "该数据集"));
        go.addEventListener("click", () => dsOpenSplit(DS_ITEM, it));
        card.appendChild(go);
        grid.appendChild(card);
    });
    revealCards(grid.querySelectorAll(".card"), false);
}

/* 拼一个 /dataset 独立页 URL（同源相对路径；extra 例如 "&embed=1"）。 */
function dsDatasetUrl(it, extra) {
    return "/dataset?uid=" + encodeURIComponent(it.dataset_uid || "")
        + "&url=" + encodeURIComponent(it.url || "")
        + "&name=" + encodeURIComponent(it.dataset_name || "")
        + "&source=" + encodeURIComponent(it.source || "") + (extra || "");
}

/* 打开全屏并排对比浮层：两侧各一个完整 /dataset iframe（embed 态）。
   完整性靠 split handoff——把两侧完整记录按 uid 写进 DS_SPLIT_KEY，iframe 的 dsResolveItem 各自取回 → 页头齐全。
   顶栏「← BioData Agent」只此一处（两侧 iframe 各自的 topbar 已隐藏，不重复）。
   关闭语义：每侧 ✕「关闭此侧，只看另一侧」→ 直接退出对比，落在保留的那个数据集完整页
   （survivor==当前页 A 则关浮层即回 A，否则导航到保留者的独立页）；顶栏「退出对比」/ Esc → 回当前页 A。 */
function dsOpenSplit(a, b) {
    if (!a || !b) return;
    try {
        const map = {};
        if (a.dataset_uid) map[a.dataset_uid] = a;
        if (b.dataset_uid) map[b.dataset_uid] = b;
        localStorage.setItem(DS_SPLIT_KEY, JSON.stringify(map));
    } catch (_e) {}

    const paneHtml = (role, it) => {
        const name = it.dataset_name || "数据集";
        return `<div class="ds-split-pane">`
            + `<div class="ds-split-bar"><span class="ds-split-role">${escapeHtml(role)}</span>`
            + `<span class="ds-split-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>`
            + `<button class="ds-split-x" type="button" title="关闭此侧，只看另一侧" aria-label="关闭此侧，只看另一侧">✕</button></div>`
            + `<iframe class="ds-split-frame" src="${dsDatasetUrl(it, "&embed=1")}" title="${escapeHtml(name)} 介绍"></iframe>`
            + `</div>`;
    };

    const wrap = document.createElement("div");
    wrap.className = "ds-split";
    wrap.setAttribute("role", "dialog");
    wrap.setAttribute("aria-label", "数据集并排对比");
    wrap.innerHTML = `<div class="ds-split-top">`
        + `<a class="ds-split-home" href="/" title="返回 ${escapeHtml(_brandName)} 检索主页">`
        + `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 6l-6 6 6 6"/></svg>${escapeHtml(_brandName)}</a>`
        + `<span class="ds-split-title">数据集并排对比</span>`
        + `<button class="ds-split-exit" id="dsSplitExit" type="button" title="退出对比" aria-label="退出对比，回到当前数据集">✕ 退出对比</button></div>`
        + `<div class="ds-split-panes">${paneHtml("当前数据集", a)}${paneHtml("对比数据集", b)}</div>`;

    const onKey = (e) => { if (e.key === "Escape") teardown(); };
    const teardown = () => {
        document.removeEventListener("keydown", onKey);
        wrap.remove();
        document.body.classList.remove("ds-split-open");
        try { localStorage.removeItem(DS_SPLIT_KEY); } catch (_e) {}
    };
    // 关掉一侧 = 只看另一侧：退出对比，落在保留的那个数据集完整页。
    const keepOnly = (survivor) => {
        const stayHere = !survivor || !survivor.dataset_uid || (DS_ITEM && survivor.dataset_uid === DS_ITEM.dataset_uid);
        if (stayHere) { teardown(); return; }                       // 保留的就是当前页 A → 关浮层即回 A
        try { localStorage.setItem(DS_VIEW_KEY, JSON.stringify(survivor)); } catch (_e) {}
        teardown();
        location.href = dsDatasetUrl(survivor, "");                 // 保留 B → 导航到 B 的独立页（handoff 保完整）
    };

    document.body.appendChild(wrap);
    document.body.classList.add("ds-split-open");
    wrap.querySelector("#dsSplitExit").addEventListener("click", teardown);
    const panes = wrap.querySelectorAll(".ds-split-pane");
    if (panes[0]) panes[0].querySelector(".ds-split-x").addEventListener("click", () => keepOnly(b));  // 关左(原) → 只看右(对比)
    if (panes[1]) panes[1].querySelector(".ds-split-x").addEventListener("click", () => keepOnly(a));  // 关右(对比) → 只看左(原)
    document.addEventListener("keydown", onKey);
}

/* ---------- 初始化 ---------- */
function dsInit() {
    if (DS_EMBED) document.body.classList.add("ds-embedded");   // 分屏 iframe：隐藏自己的返回主页 topbar
    initDownloads();   // dl-browser-queue：页头/介绍行/文件弹窗的 data-dlq 直链统一进下载队列
    DS_ITEM = dsResolveItem(dsParams());
    dsRenderHeader();
    dsRenderTabs(DS_ITEM);
    _dsLoaded.intro = true;
    dsRenderIntro();

    // 文件弹窗关闭接线（interactions.js 不在本页加载）：关闭按钮 / 点遮罩 / Esc。
    const fmClose = $("filesModalClose");
    if (fmClose) fmClose.addEventListener("click", closeFilesModal);
    const fm = $("filesModal");
    if (fm) fm.addEventListener("click", (e) => { if (e.target === fm) closeFilesModal(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && fm && !fm.hidden) closeFilesModal(); });

    // 深链：URL hash 指向某子页则直接激活（介绍是默认页，无需处理）。
    const hash = (location.hash || "").replace("#", "");
    if (hash && hash !== "intro" && dsTabDefs(DS_ITEM).some((t) => t.key === hash)) dsActivate(hash);
}

/* C1：core.js 转 ESM 后模块是 deferred——顶层裸调会抢在 core 求值前跑，改挂 DOMContentLoaded。 */
document.addEventListener("DOMContentLoaded", dsInit);
