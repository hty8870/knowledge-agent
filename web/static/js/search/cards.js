"use strict";

/* 本文件是 ES Module：core 的工具/常量、usage 层、reuse_pack 的 downloadTextBlob 与
   fav_folders 的 openFavPopover 经 import 取（cards↔fav_folders 成环，绑定只在函数体内使用）。
   dataset_page/browse/fav_folders/interactions/act 经 import 取卡片与介绍渲染函数（绞杀桥已全退役）。 */
import { API, HEART, MOTION, $, copyTextAny, escapeHtml, escapeHtmlStrong, fastqInfo, fmtThousands, isFav, isHttp, normalizeItem, prettyPlatform, prettySource, toast } from "#core";
import { USAGE_KINDS } from "#usage_core";
import { usageCardRank, usageLog, usageLogCardAction } from "#usage_log";
import { downloadTextBlob } from "#reuse_pack";
import { openFavPopover } from "#fav_folders";
import { COPY, closeModal, openModal } from "../core/copy.js";

/* 国内可达性徽章的唯一构造点（结果卡与数据集详情页头共用同一粒徽章）：
   按下载 host 推断（非实测速度）；未识别的 host 返回空串，不臆造。 */
export function reachBadgeHtml(it) {
    const reach = it && it.reachability;
    return (reach && reach.tier && reach.tier !== "unknown")
        ? `<span class="reach reach-${escapeHtml(reach.tier)}" title="${escapeHtml((reach.hosting || "") + "：" + (reach.advice || "") + "（按托管位置推断、非实测速度）")}">🌐 ${escapeHtml(reach.tier_label)}</span>`
        : "";
}

/* opts 一览：rank=结果序号角标；compact=精简卡（介绍弹窗下半窗的兼容网格/对比用——
   隐藏「数据集详情」「查看全部文件」按钮、rank 角标与承诺行，保留心形/徽章/CTA 直链）。 */
export function buildCard(rawItem, opts) {
    opts = opts || {};
    const it = normalizeItem(rawItem);
    const card = document.createElement("article");
    const library = opts.variant === "library";   // 我的库卡片化（候选/收藏/更新 delta）——保留「打开数据集页」「数据集详情」，省下载与文件展开
    card.className = "card" + (library ? " card-library" : "");
    const fq = fastqInfo(it.raw_data_status);
    const rank = opts.rank && !opts.compact ? `<span class="rank">#${opts.rank}</span>` : "";
    // 平台 / 来源降为静音文字（去顶部色噪；FASTQ 仍保留语义色 pill）
    const metaBits = [prettyPlatform(it.platform), prettySource(it.source)].filter(Boolean);
    const metaText = metaBits.length
        ? `<span class="card-meta">${metaBits.map((m, i) => (i ? `<span class="dot">·</span>` : "") + escapeHtml(m)).join("")}</span>`
        : "";
    const tags = [["物种", it.species], ["组织", it.tissue], ["疾病", it.disease]]
        .filter(([, v]) => v)
        // tags 容器限高截断（CSS max-height 2 行），每个 tag 的 title 兜住完整值——悬浮可见全文，不丢信息、不靠 JS 测量
        .map(([k, v]) => `<span class="tag" title="${escapeHtml(`${k}：${v}`)}"><span class="tag-k">${k}</span>${escapeHtml(v)}</span>`).join("");   // 裸灰块加前缀小标签，不再像装饰
    const faved = isFav(it);
    const dl = it.download_url || it.url;
    // 主 CTA 行：数据集页面(有独立页则填充=主按钮，视觉权重 ≥ 下载) 与 下载数据 等宽同排；
    // 无独立数据集页面时下载升为填充主按钮；下载无直链则灰禁占位（不再让「数据集页面」缩成隐形文字链）。
    const hasPage = isHttp(it.url) && it.url !== dl;
    const pageBtn = hasPage
        ? `<a class="cta cta-primary" href="${escapeHtml(it.url)}" target="_blank" rel="noopener">打开数据集页 ↗</a>`
        : "";
    const dlBtn = library ? ""
        : isHttp(dl) ? `<a class="cta ${hasPage ? "cta-soft" : "cta-primary"}" href="${escapeHtml(dl)}" target="_blank" rel="noopener" download data-dlq="data" data-dlq-uid="${escapeHtml(it.dataset_uid || "")}" data-dlq-name="${escapeHtml(it.dataset_name || "")}">下载数据 ↓</a>`
        : `<span class="cta cta-disabled" aria-disabled="true">暂无下载</span>`;
    // 「查看介绍」改为打开**独立浏览器标签页** /dataset（不再模态弹窗）。链接携 uid/url/name/source；
    // 点击时把**完整记录**写进 localStorage 供新标签页取回 download_url/n_files 等 URL 带不了的字段（见下方 detailLink 接线）。
    // href 用**同源相对路径** /dataset?… 直插（参数 encodeURIComponent，目标恒为本站 /dataset，绝不涉外部方案，
    // 非整值插值 → 不触发 isHttp sink 门；见 tests/test_frontend_url_safety.py）。
    // 改名「数据集详情 ↗」并从 card-foot 文字链升格为 .card-cta 之下的独立全宽按钮行（.btn-detail）。
    const detail = opts.compact ? "" : `<a class="btn-detail" href="/dataset?uid=${encodeURIComponent(it.dataset_uid || "")}&url=${encodeURIComponent(it.url || "")}&name=${encodeURIComponent(it.dataset_name || "")}&source=${encodeURIComponent(it.source || "")}" target="_blank" rel="noopener" aria-label="查看 ${escapeHtml(it.dataset_name || "该数据集")} 的数据集详情页（含介绍、文件、FAIR、引文、对比；在新标签页打开）">数据集详情 ↗</a>`;
    // 国内可达性徽章：构造函数见本文件 reachBadgeHtml（唯一锚点）
    const reachBadge = reachBadgeHtml(it);

    const nFiles = it.n_files || 0;
    const filesBtnHtml = (!opts.compact && !library && nFiles > 1)
        ? `<button class="files-open" type="button"><span class="fo-ico">▤</span>查看全部 ${nFiles} 个文件<span class="fo-arrow">→</span></button>`
        : "";
    // 样本量/基因数过千分位（与首页覆盖声明、可行性面板同一记数法；年份不过——
    // 4 位年份会被加成 2,025）。每段包 .sample-bit nowrap，折行只许发生在分隔符上——
    // 此前「发表」与年份被拆到两行（年份孤挂）。
    const footBits = [`<b>样本量</b> ${escapeHtml(fmtThousands(it.sample_size))}`];
    if (it.gene_count) footBits.push(`<b>基因数</b> ${escapeHtml(fmtThousands(it.gene_count))}`);
    if (it.published_date) footBits.push(`<b>发表</b> ${escapeHtml(String(it.published_date).slice(0, 4))}`);
    // 底部钉齐：无文件按钮的完整卡补同盒尺寸占位槽（visibility:hidden、aria-hidden，结构与真实按钮一致 → 高度恒等），
    // 同行各卡「查看全部 N 个文件」有/无不再把 CTA 行错开；compact/库卡片不出按钮也不出槽。
    const filesRegion = (library || opts.compact) ? ""
        : (!filesBtnHtml) ? `<span class="files-open files-open-slot" aria-hidden="true"><span class="fo-ico">▤</span>查看全部<span class="fo-arrow">→</span></span>`
        : filesBtnHtml;
    card.innerHTML = `
        <button class="fav ${faved ? "active" : ""}" title="收藏" aria-label="收藏">${HEART}</button>
        <div class="badges">${rank}${metaText}<span class="fastq ${fq.cls}">${fq.label}</span>${reachBadge}</div>
        <h3 class="card-title">${escapeHtml(it.dataset_name || "未命名数据集")}</h3>
        ${it.reason ? `<p class="card-why"><b>推荐理由</b> ${escapeHtml(it.reason)}</p>` : ""}
        <div class="card-tags">${tags}</div>
        <div class="card-foot"><span class="sample">${footBits.map((b) => `<span class="sample-bit">${b}</span>`).join('<span class="sample-sep" aria-hidden="true"> · </span>')}</span></div>
        <div class="card-cta">${pageBtn}${dlBtn}</div>
        ${detail}
        ${filesRegion}`;

    const favBtn = card.querySelector(".fav");
    // 心形不再直接 toggle：交给收藏夹 popover（fav_folders.js）——未收藏先选夹，已收藏给「取消 / 移到」菜单。
    // 收藏成功/取消后的心形态、动效与 toast 都由 popover 侧收尾（它持有 favBtn 引用）。
    favBtn.addEventListener("click", (event) => {
        event.stopPropagation();   // 防这次点击冒泡到 document 立刻触发 popover 的「点外关闭」
        openFavPopover(favBtn, it);
    });

    const filesBtn = card.querySelector("button.files-open");   // 占位槽是 span.files-open-slot，只给真按钮绑事件
    if (filesBtn) filesBtn.addEventListener("click", (event) => openFilesModal(it, event.currentTarget));
    // 详情按钮自身 target=_blank 导航到 /dataset；点击时同步把完整记录写进 localStorage（导航前执行，新标签页读得到）。
    const detailLink = card.querySelector(".btn-detail");
    if (detailLink) detailLink.addEventListener("click", () => {
        // 2026-08-15：handoff 双写——按 uid 分键是主通道（多标签连开不同数据集互不覆盖）；
        // 旧单槽保留作兼容兜底（旧版详情页只认它）。两条各自 try，一条写满不影响另一条。
        try { localStorage.setItem("biodata_dataset_view_v1", JSON.stringify(it)); } catch (_e) {}
        try {
            if (it.dataset_uid) localStorage.setItem("biodata_dataset_view_v1:" + it.dataset_uid, JSON.stringify(it));
        } catch (_e) {}
        // v2：uid/pos 归因到具体条目与名次（r 保留——聚合器 usageRankBuckets 读它）。
        // v3：卡级动作经 usageLogCardAction——结果页的卡带展示快照 tid/iid，
        // 其它页面的卡（无绑定快照）显式 null，不冒领当前轮。
        const rank = usageCardRank(card);
        usageLogCardAction(card, USAGE_KINDS.open, { what: "intro", r: rank, uid: String(it.dataset_uid || ""), pos: rank });
    });
    // 去原站（v2 起记）：用户愿不愿点回原始来源页，是推荐可信度最直接的出站信号。
    // 选择器排除带 download 属性的直链下载钮（那是另一类动作），只留「打开数据集页」。
    const siteLink = card.querySelector("a.cta:not([download])");
    if (siteLink) siteLink.addEventListener("click", () => {
        const rank = usageCardRank(card);
        usageLogCardAction(card, USAGE_KINDS.open, { what: "site", r: rank, uid: String(it.dataset_uid || ""), pos: rank });
    });
    return card;
}

/* ---------- 数据集介绍内容渲染（详情页 dataset.html 与兼容/对比复用）+ 文件弹窗 ---------- */
let _filesReturnFocus = null;
const _introCache = new Map();

export function fallbackIntroduction(it) {
    const values = [it.source, it.species, it.tissue, it.disease,
        [it.platform, it.assay, it.chemistry].filter(Boolean).join(" / "),
        it.sample_size, it.published_date, it.raw_data_status];
    const facts = COPY.introductionFacts.map((label, index) => ({
        label: label, value: values[index] || "未说明",
    }));
    // 检测基因数（10x 平台信息补充）：与后端 introduction 同口径——有才追加，无补充不加「未说明」行。
    if (it.gene_count) facts.push({ label: "检测基因数", value: it.gene_count });
    const pieces = [];
    if (it.species) pieces.push(`物种为 ${it.species}`);
    if (it.tissue) pieces.push(`组织为 ${it.tissue}`);
    if (it.disease) pieces.push(`疾病信息为 ${it.disease}`);
    if (it.platform || it.assay || it.chemistry) pieces.push(`采用 ${[it.platform, it.assay, it.chemistry].filter(Boolean).join(" / ")}`);
    // 本地兜底：只在服务端没返回 introduction 时用。
    // **刻意不在这里判体裁**：体裁由「哪个 ingest 写的」决定，真源在后端 summary_genre.py
    // （按 source 判定）。在 JS 里再抄一份 source→体裁映射，就是刚被 item_view 修掉的
    // 「两份手抄迟早漂移」。所以这里如实说明「这是本地兜底、未经体裁判定」，
    // 不冒充「来源元数据」——旧代码 `it.description ? "来源元数据" : …` 会把 CELLxGENE
    // 的论文标题标成来源摘要，正是后端刚修掉的那个错。
    return {
        summary: it.description || (pieces.length ? `该数据集的公开字段显示：${pieces.join("；")}。` : "当前只有有限索引信息，请到来源页面核对完整实验背景。"),
        source_label: "本页临时整理",
        facts,
        caveat: "没有拿到完整介绍，以上是本页根据已有字段临时整理的内容，未经来源官方介绍核验，请到来源页面核对。",
    };
}

export function renderIntroduction(it, opts) {
    // opts.bare = 对比下半窗的精简正文：只保留描述/事实/提醒 + 下载与来源页直链行，
    // 不含主弹窗 tab 栏已经承载的入口（全部文件/兼容/FAIR/引文/AI 导读）。
    opts = opts || {};
    const intro = (it.introduction && typeof it.introduction === "object") ? it.introduction : fallbackIntroduction(it);
    const facts = Array.isArray(intro.facts) ? intro.facts : [];
    const factHtml = facts.map((fact) => `<div class="intro-fact"><dt>${escapeHtml(fact.label || "信息")}</dt><dd>${escapeHtml(fact.value || "未说明")}</dd></div>`).join("");
    // 样本量语义提醒：只在服务端返回（确定性单一真源 units.sample_size_note）时展示，
    // 前端不再自算——避免「两份手抄迟早漂移」。空数组/兜底介绍时整块隐藏。
    const ssCav = Array.isArray(intro.sample_size_caveats) ? intro.sample_size_caveats : [];
    const ssHtml = ssCav.length ? `<section class="intro-ss-caveats"><h4>样本量怎么读</h4><ul>${ssCav.map((c) => `<li>${escapeHtml(c)}</li>`).join("")}</ul></section>` : "";
    const reason = it.reason ? `<section class="intro-reason"><h4>为什么推荐给你</h4><p>${escapeHtml(it.reason)}</p></section>` : "";
    // 下载/来源页直链移到标题下常驻 tab 栏（href/target/rel 逐位保持）；
    // bare 正文（对比窗）仍给一行直链，方便对照时直接点走。
    const sourceLink = isHttp(it.url) ? `<a class="btn" href="${escapeHtml(it.url)}" target="_blank" rel="noopener">打开来源页 ↗</a>` : "";
    const downloadLink = isHttp(it.download_url) ? `<a class="btn btn-primary" href="${escapeHtml(it.download_url)}" target="_blank" rel="noopener" download data-dlq="data" data-dlq-uid="${escapeHtml(it.dataset_uid || "")}" data-dlq-name="${escapeHtml(it.dataset_name || "")}">下载数据 ↓</a>` : "";
    const directRow = (opts.bare && (downloadLink || sourceLink))
        ? `<div class="intro-bare-actions">${downloadLink}${sourceLink}</div>` : "";
    // LLM 中文导读（实验性、opt-in）。`llm_status` 键**只在用户点过「AI 导读」**（llm=1）后才出现，
    // 故凭它区分「没请求过」与「请求了但不可用」。始终 additive：确定性 summary/facts/caveat 恒在上方。
    let llmHtml = "";
    if (intro.llm_summary) {
        llmHtml = `<section class="intro-llm"><div class="intro-source">🤖 AI 中文导读（实验性）</div>`
            + `<p>${escapeHtml(intro.llm_summary)}</p>`
            + `<p class="intro-llm-note">由大模型据上方元数据生成，可能有误；请以原始介绍、关键事实和来源页面为准。</p></section>`;
    } else if (typeof intro.llm_status === "string") {
        const st = intro.llm_status;
        const msg = st === "disabled" ? "本站未开启 AI 导读功能。"
            : st === "not_translatable" ? "该来源没有可翻译的叙述（如只有字段清单/模板），无法生成 AI 导读。"
            : st === "no_key" ? "AI 导读已开启，但还没有配置可用的 AI 接口，暂时无法生成。"
            : "AI 导读暂不可用，已保留上方原始介绍。";
        llmHtml = `<section class="intro-llm intro-llm-off"><p>🤖 ${escapeHtml(msg)}</p></section>`;
    }
    // 「AI 导读」入口按钮在 tab 栏（intro_tabs.js）：只有「有真散文可译」的体裁（prose/title）
    // + **尚未请求过**（无 llm_status）才出现；请求过之后由上方导读块/降级提示取代，不与提示并存。
    return `<section class="intro-summary"><div class="intro-source">${escapeHtml(intro.source_label || "按公开字段整理")}</div><p>${escapeHtml(intro.summary || "暂无介绍")}</p></section>`
        + llmHtml
        + `${reason}<section class="intro-facts"><h4>关键事实</h4><dl>${factHtml}</dl></section>`
        + ssHtml
        + `<p class="intro-caveat">${escapeHtml(intro.caveat || "未提供的字段保持未知。")}</p>`
        + directRow;
}

export function introKeyOf(it) { return it.dataset_uid || it.url || `${it.source}|${it.dataset_name}`; }

export function bindIntroductionBody(body, it, opts) {
    // 每次绑定都把当前卡片 key 写进 body：一旦切到新卡，上一张卡片在途的 /api/introduction 响应护栏
    // (introKey===key) 即失效，防止慢网下旧响应覆盖当前卡正文，造成"标题 B 配正文 A"的串卡。
    // 介绍改独立浏览器标签页后不再有模态 tab 栏，本函数只渲染正文；子标签（全部文件/兼容/FAIR/引文/对比）
    // 归 dataset_page.js 的子标签页各自懒加载，不再从这里 syncIntroTabs。
    opts = opts || {};
    body.dataset.introKey = introKeyOf(it);
    body.innerHTML = renderIntroduction(it, opts);
}

// 用户显式点「AI 中文导读」→ 带 llm=1 重取介绍（双层门的**客户端**那半：管理员开 ENABLE_LLM +
// 用户此刻点按，两者同时满足才真调 LLM）。回来无论成功/降级都重渲染；确定性介绍始终在上方。
export async function loadLlmIntro(body, it, btn) {
    const key = introKeyOf(it);
    if (btn) { btn.disabled = true; btn.textContent = "AI 导读生成中…"; }
    const params = new URLSearchParams({ uid: it.dataset_uid || "", url: it.url || "", name: it.dataset_name || "", source: it.source || "", llm: "1" });
    try {
        const response = await fetch(`${API.introduction}?${params.toString()}`);
        const data = await response.json();
        if (!response.ok || !data.ok || !data.introduction) throw new Error(data.detail || "unavailable");
        it.introduction = data.introduction;
        // 只缓存**成功**的 llm 版（本会话再开该卡即带导读）；失败/降级不污染缓存，
        // 保留确定性版在缓存里，换卡/重搜时按钮仍可再出现。
        if (data.introduction.llm_summary) _introCache.set(key, data.introduction);
        // AI 导读不进 search_trace（它是按需拉的），所以在这里自己记一笔，共用同一张标签表。
        // 判据是**真有导读正文**，不是「请求没报错」——后端 fail-open 时会返回一份没有
        // llm_summary 的确定性介绍，那种情况对用户来说就是「AI 这层没成」。
        usageLog(USAGE_KINDS.ai, { step: "llm_intro", ok: !!data.introduction.llm_summary, why: data.introduction.llm_summary ? "" : String(data.introduction.llm_status || "").slice(0, 40) });
    } catch (_e) {
        if (it.introduction && typeof it.introduction === "object") it.introduction.llm_status = "error:fetch";
        usageLog(USAGE_KINDS.ai, { step: "llm_intro", ok: false, why: "请求失败" });
    }
    // 护栏 null-safe：介绍详情页（dataset.html）没有 #introModal，im=null 时视作「容器仍在」照常绑定；
    // 首页模态时保留「仅在弹窗仍开时绑定」的旧语义。
    { const im = $("introModal"); if (body.dataset.introKey === key && (!im || !im.hidden)) bindIntroductionBody(body, it); }
}

export async function loadIntroductionInto(body, it, opts) {
    const key = introKeyOf(it);
    body.dataset.introKey = key;
    if (_introCache.has(key)) { it.introduction = _introCache.get(key); bindIntroductionBody(body, it, opts); return; }
    body.innerHTML = `<div class="files-hint">正在整理来源元数据…</div>`;
    const params = new URLSearchParams({ uid: it.dataset_uid || "", url: it.url || "", name: it.dataset_name || "", source: it.source || "" });
    try {
        const response = await fetch(`${API.introduction}?${params.toString()}`);
        const data = await response.json();
        if (!response.ok || !data.ok || !data.introduction) throw new Error(data.detail || "introduction unavailable");
        _introCache.set(key, data.introduction);
        it.introduction = data.introduction;
    } catch (_e) {
        it.introduction = fallbackIntroduction(it);
    }
    { const im = $("introModal"); if (body.dataset.introKey === key && (!im || !im.hidden)) bindIntroductionBody(body, it, opts); }
}

/* openIntroModal / closeIntroModal（介绍模态弹窗）已退役——「查看介绍」改为打开独立浏览器标签页
   /dataset（dataset.html + dataset_page.js）。介绍**内容**渲染函数（renderIntroduction/loadIntroductionInto/
   loadLlmIntro/loadFairInto/loadCompatInto/loadCiteInto/loadFilesInto）保留在本文件，被详情页复用。 */

/* ---------- 投稿数据可用性说明（DAS）+ FAIR 元数据自检（懒加载，介绍弹窗下半窗）----------
   只读、确定性、离线：点按钮才拉 /api/fair；按 introKey 缓存；复制走 navigator.clipboard（带兜底）。 */
const _fairCache = new Map();
const _compatCache = new Map();
const FAIR_PRINCIPLES = [["F", "可发现 Findable"], ["A", "可获取 Accessible"], ["I", "可互操作 Interoperable"], ["R", "可复用 Reusable"]];

function fairStatusMeta(status) {
    if (status === "pass") return { cls: "ok", mark: "✓" };
    if (status === "partial") return { cls: "partial", mark: "~" };
    return { cls: "unknown", mark: "?" };
}

/* toggle 面板已迁出正文——FAIR 走下半窗、引文/文件走推挤下拉（intro_tabs.js），
   这里只保留数据加载与渲染（loadXxxInto/renderXxxInto），供 tab 栏与下半窗复用。 */
export async function loadFairInto(wrap, it) {
    const key = introKeyOf(it);
    if (_fairCache.has(key)) { renderFairInto(wrap, _fairCache.get(key)); return; }
    wrap.innerHTML = `<div class="files-hint">正在生成数据可用性说明与 FAIR 自检…</div>`;
    const params = new URLSearchParams({ uid: it.dataset_uid || "", url: it.url || "", name: it.dataset_name || "", source: it.source || "" });
    try {
        const res = await fetch(`${API.fair}?${params.toString()}`);
        const data = await res.json();
        if (!res.ok || !data.ok || !data.fair_report) throw new Error(data.detail || "fair unavailable");
        _fairCache.set(key, data.fair_report);
        renderFairInto(wrap, data.fair_report);
    } catch (_e) {
        wrap.innerHTML = `<div class="files-hint">暂时无法生成（该数据集可能不在当前库中）。</div>`;
    }
}

// 元数据兼容分组：同物种 + 兼容 chemistry/platform 的其它数据集。
// 诚实边界：只回「元数据兼容」（必要非充分），始终显示 caveat，绝不说「可整合」。
export async function loadCompatInto(wrap, it) {
    const key = introKeyOf(it);
    if (_compatCache.has(key)) { renderCompatCards(wrap, _compatCache.get(key)); return; }
    wrap.innerHTML = `<div class="files-hint">正在查找元数据兼容的数据集…</div>`;
    const uid = it.dataset_uid || "";
    if (!uid) { wrap.innerHTML = `<div class="files-hint">该数据集没有可用编号，无法查找兼容数据集。</div>`; return; }
    try {
        const res = await fetch(`${API.compatible}?uid=${encodeURIComponent(uid)}&limit=20`);
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.detail || "compatible unavailable");
        _compatCache.set(key, data);
        renderCompatCards(wrap, data);
    } catch (_e) {
        wrap.innerHTML = `<div class="files-hint">暂时无法查找（该数据集可能不在当前库中）。</div>`;
    }
}

/* 兼容数据集卡**完全对齐真正的检索结果卡**——直接用缺省 buildCard（心形 / 徽章 / 标题 / tags /
   样本量 /「数据集详情 ↗」/「查看全部 N 个文件」/ CTA 直链一应俱全，仅无结果序号角标），每条卡底附
   `_compat_basis` 兼容依据；顶部「必要非充分」caveat 逐字保留（诚实红线，不说「可整合」）。
   （旧版 compact 精简卡是下半窗窄栏的迁就；详情页子标签页空间充足，回归全卡。） */
export function renderCompatCards(wrap, data) {
    const list = Array.isArray(data.compatible) ? data.compatible : [];
    const crit = data.criteria || {};
    const critTxt = [crit.chemistry ? `chemistry ${crit.chemistry}` : "", crit.platform_family ? `平台 ${crit.platform_family}` : ""].filter(Boolean).join(" · ");
    const caveat = `<div class="compat-caveat">${escapeHtmlStrong(data.caveat || "")}</div>`;
    if (!list.length) {
        wrap.innerHTML = caveat + `<div class="files-hint">本目录内没有找到元数据兼容的其它数据集。</div>`;
        return;
    }
    wrap.innerHTML = `<h5 class="compat-h">元数据兼容（${data.total} 条${critTxt ? "，依据 " + escapeHtml(critTxt) : ""}）</h5>`
        + caveat + `<div class="compat-grid grid"></div>`;
    const grid = wrap.querySelector(".compat-grid");
    list.forEach((c) => {
        const card = buildCard(c);
        if (c._compat_basis) {
            const basis = document.createElement("div");
            basis.className = "compat-basis-line";
            basis.textContent = c._compat_basis;   // textContent 填，天然转义
            card.appendChild(basis);
        }
        grid.appendChild(card);
    });
}

/* ---------- 单条引文导出（可发现性）：tab 栏「导出引文 RIS/BibTeX」推挤下拉 ----------
   复用 /api/reuse-pack 的单 uid 既有用法；ris/bibtex 串**原样**来自后端 to_ris/to_bibtex
   （RIS TY-DATA / BibTeX @misc）——绝不在 JS 里拼引用串、绝不写成 article 类型。 */
const _citeCache = new Map();

export async function loadCiteInto(wrap, it) {
    const key = introKeyOf(it);
    if (_citeCache.has(key)) { renderCiteInto(wrap, it, _citeCache.get(key)); return; }
    const uid = String(it.dataset_uid || "").trim();
    if (!uid) { wrap.innerHTML = `<div class="files-hint">该数据集没有可用编号，无法生成引文。</div>`; return; }
    wrap.innerHTML = `<div class="files-hint">正在生成引文…</div>`;
    try {
        const res = await fetch(API.reusePack, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ uids: [uid] }),
        });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.detail || "citation unavailable");
        _citeCache.set(key, { ris: data.ris || "", bibtex: data.bibtex || "" });
        renderCiteInto(wrap, it, _citeCache.get(key));
    } catch (_e) {
        wrap.innerHTML = `<div class="files-hint">暂时无法生成引文（该数据集可能不在当前库中）。</div>`;
    }
}

export function renderCiteInto(wrap, it, cite) {
    const safe = String(it.dataset_uid || "dataset").replace(/[^A-Za-z0-9_.-]+/g, "_");
    wrap.innerHTML = `<div class="compat-caveat">引文条目类型为数据集（RIS <code>TY - DATA</code> / BibTeX <code>@misc</code>）；写「数据可用性声明」请点本页上方的「投稿可用性与 FAIR 自检」标签。</div>`
        + `<div class="cite-acts"><button class="btn cite-ris" type="button">下载 .ris</button>`
        + `<button class="btn cite-bib" type="button">下载 .bib</button>`
        + `<button class="btn cite-copy" type="button">复制 BibTeX</button></div>`;
    const btnRis = wrap.querySelector(".cite-ris");
    if (btnRis) btnRis.addEventListener("click", () => {
        if (!cite.ris) { toast("暂无可下载的引文"); return; }
        downloadTextBlob(cite.ris, `citation-${safe}.ris`, "application/x-research-info-systems;charset=utf-8", "已下载 .ris 引文", "下载失败", { uid: String(it.dataset_uid || "") });
    });
    const btnBib = wrap.querySelector(".cite-bib");
    if (btnBib) btnBib.addEventListener("click", () => {
        if (!cite.bibtex) { toast("暂无可下载的引文"); return; }
        downloadTextBlob(cite.bibtex, `citation-${safe}.bib`, "application/x-bibtex;charset=utf-8", "已下载 .bib 引文", "下载失败", { uid: String(it.dataset_uid || "") });
    });
    const btnCopy = wrap.querySelector(".cite-copy");
    if (btnCopy) btnCopy.addEventListener("click", () => {
        if (!cite.bibtex) { toast("暂无可复制的引文"); return; }
        copyTextAny(cite.bibtex, { okMsg: "已复制 BibTeX" });
    });
}

export function renderFairInto(wrap, report) {
    const das = (report && report.data_availability) || {};
    const fair = (report && report.fair) || {};
    const checks = Array.isArray(fair.checks) ? fair.checks : [];
    const summary = fair.summary || {};
    const groups = FAIR_PRINCIPLES.map(([p, label]) => {
        const rows = checks.filter((c) => c.principle === p).map((c) => {
            const m = fairStatusMeta(c.status);
            // c.action（2026-07-17 由 c.improve 改名）：只写**复用者真能做的事**。
            // 旧字段名预设读者能改进这份数据 —— 而这里全是别人的公开数据，改不了。
            const action = c.action ? `<div class="fair-action">↳ ${escapeHtml(c.action)}</div>` : "";
            return `<li class="fair-check ${m.cls}"><span class="fair-pill" aria-hidden="true">${m.mark}</span>`
                + `<div class="fair-check-body"><div class="fair-check-label">${escapeHtml(c.label)}</div>`
                + `<div class="fair-check-ev">${escapeHtml(c.evidence)}</div>${action}</div></li>`;
        }).join("");
        return rows ? `<div class="fair-group"><h5>${escapeHtml(label)}</h5><ul>${rows}</ul></div>` : "";
    }).join("");
    const missing = (Array.isArray(das.missing) && das.missing.length)
        ? `<ul class="das-missing">${das.missing.map((m) => `<li>${escapeHtml(m)}</li>`).join("")}</ul>` : "";
    wrap.innerHTML =
        `<section class="das-box"><div class="das-head"><h4>「复用公开数据」声明（投稿用 · 英文）</h4>`
        + `<button class="btn das-copy" type="button">复制</button></div>`
        + `<blockquote class="das-text">${escapeHtml(das.statement || "")}</blockquote>`
        + `<p class="das-scope-note">只覆盖你<strong>复用的这份公开数据</strong>。`
        + `你自己产出的数据该怎么写，本工具帮不上，需你自行补写。</p>`
        + (das.notes ? `<p class="das-notes">${escapeHtml(das.notes)}</p>` : "") + missing + `</section>`
        + `<section class="fair-box"><div class="fair-summary"><span class="fair-score">${Number(summary.readiness_pct || 0)}%</span>`
        + `<span class="fair-summary-text">${escapeHtml(summary.statement || "")}</span></div>`
        + `<div class="fair-groups">${groups}</div>`
        + `<p class="fair-disclaim">衡量的是「<strong>这份公开元数据够不够你拿来引用/写方法学</strong>」，`
        + `非官方 FAIR 认证，也不是对该数据集质量的评价。「未知」= 来源未标注、或本工具未核验，都不等于不满足。</p></section>`;
    const copyBtn = wrap.querySelector(".das-copy");
    if (copyBtn) copyBtn.addEventListener("click", () => {
        copyTextAny(das.statement || "", { okMsg: "已复制英文数据可用性声明" });
    });
}

/* ---------- files modal ----------
   点卡片「查看全部 N 个文件」→ 居中弹窗展示该数据集全部真实直链。
   按需拉取一次并按 uid/url 缓存；开合动效经 MOTION 门控，无 GSAP 时纯显隐。 */
const _filesCache = new Map();

/* 由文件名后缀推一个简短类型标签（10x 常见：h5/bam/fastq/cloupe/web_summary…）。 */
function fileExtTag(name) {
    name = String(name || "").toLowerCase();
    if (name.endsWith(".tar.gz") || name.endsWith(".tgz")) return "TAR.GZ";
    const m = name.match(/\.([a-z0-9]+)$/);
    const e = m ? m[1] : "";
    const map = {
        h5: "H5", h5ad: "H5AD", bam: "BAM", bai: "BAI", cloupe: "LOUPE", loupe: "LOUPE",
        fastq: "FASTQ", fq: "FASTQ", gz: "GZ", zip: "ZIP", tar: "TAR", mtx: "MTX",
        csv: "CSV", tsv: "TSV", html: "HTML", txt: "TXT", pdf: "PDF", json: "JSON", png: "PNG",
    };
    return (map[e] || e || "FILE").toUpperCase();
}

export function renderFilesList(files) {
    if (!files.length) return `<div class="files-hint">该数据集暂无可用的文件直链</div>`;
    const rows = files.map((f) => {
        const primary = !!f.is_primary;
        const tag = fileExtTag(f.filename || f.title || "");
        const badge = primary ? `<span class="file-badge">主文件</span>` : "";
        // 活台账标记的「存疑」文件（链接失效 / 大小不一致）：加醒目标记 + 悬停看原因。
        const warn = f.problem
            ? `<span class="file-warn" title="${escapeHtml(f.problem_reason || "该文件最近核验存在完整性问题")}">⚠ 存疑</span>`
            : "";
        const size = f.size_human ? `<span class="file-size">${escapeHtml(f.size_human)}</span>` : "";
        const label = escapeHtml(f.title || f.filename || "文件");
        const fn = f.filename ? `<span class="file-fn">${escapeHtml(f.filename)}</span>` : "";
        const dl = isHttp(f.download_url)
            ? `<a class="file-dl" href="${escapeHtml(f.download_url)}" target="_blank" rel="noopener" download data-dlq="data" data-dlq-name="${escapeHtml(f.filename || f.title || "数据文件")}">下载 ↓</a>`
            : `<span class="file-dl disabled">暂无下载</span>`;
        return `<li class="file-row${primary ? " primary" : ""}${f.problem ? " warn" : ""}"><span class="file-type">${escapeHtml(tag)}</span>`
            + `<span class="file-info"><span class="file-name">${label}${badge}${warn}</span>${fn}</span>`
            + `<span class="file-meta">${size}${dl}</span></li>`;
    }).join("");
    return `<ul class="files-list">${rows}</ul>`;
}

/* 返回 `{ok, count, error?}`。**必须有返回值**：一句话执行层要据此渲染回执——
   没有返回值就只能照「成功」渲染，断网时也会写成「已经打开了 N 个文件的清单」。 */
export async function loadFilesInto(body, it) {
    const key = it.dataset_uid || it.url || "";
    if (_filesCache.has(key)) {
        const cached = _filesCache.get(key);
        body.innerHTML = renderFilesList(cached);
        return { ok: true, count: cached.length };
    }
    body.innerHTML = `<div class="files-hint">加载中…</div>`;
    try {
        const q = it.dataset_uid ? ("uid=" + encodeURIComponent(it.dataset_uid))
                                 : ("url=" + encodeURIComponent(it.url || ""));
        const d = await (await fetch(API.files + "?" + q)).json();
        const files = d.files || [];
        _filesCache.set(key, files);
        body.innerHTML = renderFilesList(files);
        return { ok: true, count: files.length };
    } catch (_e) {
        body.innerHTML = `<div class="files-hint">加载失败，请刷新页面后重试</div>`;
        return { ok: false, count: 0, error: "没能取到这一条的文件清单" };
    }
}

/* 返回 `loadFilesInto` 的 promise（`{ok, count}`），供一句话执行层 await 出真实条数。
   手点这个弹窗的路径不 await，行为逐位不变。 */
export function openFilesModal(it, trigger) {
    const back = $("filesModal");
    _filesReturnFocus = trigger || document.activeElement;
    const _openCard = trigger && trigger.closest ? trigger.closest(".card") : null;
    const _openRank = usageCardRank(_openCard);
    usageLogCardAction(_openCard, USAGE_KINDS.open, { what: "files", r: _openRank, uid: String(it.dataset_uid || ""), pos: _openRank });
    $("filesModalTitle").textContent = it.dataset_name || "数据集文件";
    $("filesModalSub").textContent = `共 ${it.n_files || 0} 个文件 · 点每行右侧「下载 ↓」保存到本地`;
    const body = $("filesModalBody"); body.scrollTop = 0;
    openModal(back, { returnFocus: _filesReturnFocus, initialFocus: $("filesModalClose") });
    if (MOTION) {
        gsap.set(back, { autoAlpha: 0 });
        gsap.to(back, { autoAlpha: 1, duration: 0.2, ease: "power2.out" });
        gsap.fromTo(back.querySelector(".modal"), { y: 16, scale: 0.97, autoAlpha: 0 },
            { y: 0, scale: 1, autoAlpha: 1, duration: 0.42, ease: "power3.out" });
    }
    const loading = loadFilesInto(body, it);
    return loading;
}

export function closeFilesModal() {
    const back = $("filesModal");
    if (back.hidden) return;
    let doneRan = false;
    const done = () => {
        if (doneRan) return; doneRan = true;   // 幂等：onComplete 与兜底 timer 谁先到谁执行
        closeModal(back);
    };
    if (MOTION) {
        gsap.to(back.querySelector(".modal"), { y: 10, scale: 0.98, autoAlpha: 0, duration: 0.2, ease: "power2.in" });
        gsap.to(back, { autoAlpha: 0, duration: 0.24, ease: "power2.in", onComplete: done });
        setTimeout(done, 420);   // 同类兜底：rAF 冻结时 tween 不完成 → 强制收尾（滚动锁不残留）
    } else { done(); }
}

/* ---------- 左侧悬浮 tab 栏 ---------- */
export const LS_SIDE = "biodata_sidebar_closed_v1";
