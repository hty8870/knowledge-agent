"use strict";

/* 本文件是 ES Module。toggleFav 用到的 usageLogCardAction / USAGE_KINDS 来自
   usage_log / usage_core（ESM live binding）。
   2026-08-10 起：**切断 core→board 反向边**——对话 id/对话日志
   的真源在 board.js，但 core 不再 import board（那条边是前端 18 模块 SCC 的关键反向边，
   「模块求值期互不触碰」此前全靠人肉纪律）。改为注册反转：board 在 initCondBoard 时经
   `setHistHooks` 把两个取值函数注册进来；core 只在函数体内调钩子，环结构性消失。 */
import { USAGE_KINDS } from "#usage_core";
import { usageCardRank, usageLogCardAction } from "#usage_log";
import { COPY } from "./copy.js";

/* 历史打标钩子（board.js 在 initCondBoard 注册）。未注册时（理论上只剩未加载 board 的
   页面）回退空值——pushHist 各字段形状不变，仅 convId/chat 为空。 */
let _histConvId = null;
let _histLogForHistory = null;
export function setHistHooks(h) {
    _histConvId = (h && h.convId) || null;
    _histLogForHistory = (h && h.logForHistory) || null;
}

export const API = { health: "/api/health", interpret: "/api/interpret", recommend: "/api/recommend", utterance: "/api/utterance", upload: "/api/upload", diagnose: "/api/diagnose", datasets: "/api/datasets", sources: "/api/sources", introduction: "/api/introduction", files: "/api/files", fair: "/api/fair", compatible: "/api/compatible", feasibility: "/api/feasibility", reusePack: "/api/reuse-pack", boardPlan: "/api/board/plan", taskPackPreview: "/api/task-pack/preview", taskPackBuild: "/api/task-pack/build", curatePlan: "/api/curate/plan", curateApply: "/api/curate/apply", curateCheckUpdates: "/api/curate/check-updates", curateSyncUpdates: "/api/curate/sync-updates", curateSyncJobStatus: "/api/curate/sync-updates/status", curateSyncStatus: "/api/curate/sync-status", curateRecall: "/api/curate/recall", curateStatus: "/api/curate/status", actSummary: "/api/act/summary", searchReply: "/api/search/reply", searchRescue: "/api/agent/search-rescue", accountRegister: "/api/account/register", accountLogin: "/api/account/login", accountLogout: "/api/account/logout", accountWhoami: "/api/account/whoami", accountSwitch: "/api/account/switch", accountTrialQuota: "/api/account/trial-quota", mcpTokenMint: "/api/account/mcp-token", mcpTokenList: "/api/account/mcp-tokens", mcpTokenRevoke: "/api/account/mcp-token/revoke", dream: "/api/dream", curateExamplesPending: "/api/curate-examples/pending", curateExamplesApprove: "/api/curate-examples/approve", curateExamplesDismiss: "/api/curate-examples/dismiss", citationsDownload: "/api/citations/download", localModelStatus: "/api/local-model/status", localModelInstall: "/api/local-model/install", localModelCancel: "/api/local-model/cancel", watchCheck: "/api/watch/check", domainManifest: "/api/domain/manifest" };
export const LS = { fav: "biodata_favorites_v1", favFolders: "biodata_fav_folders_v1", hist: "biodata_history_v1", cfg: "biodata_settings_v1", sourcesOff: "biodata_sources_off_v1", sourceMode: "biodata_source_mode_v1", timeMode: "biodata_time_mode_v1", onboarding: "biodata_onboarding_v1", sidebarWidth: "biodata_sidebar_width_v1", memory: "biodata_user_memory_v1", memoryEnabled: "biodata_user_memory_enabled_v1", dreamConsent: "biodata_dream_consent_v1", usage: "biodata_usage_log_v1", usageEnabled: "biodata_usage_enabled_v1", usageInstall: "biodata_usage_install_v1", usageClient: "biodata_usage_client_v2", usageProfile: "biodata_usage_profile_v2", benchfb: "biodata_benchfb_v1", benchfbLabels: "biodata_benchfb_labels_v1", usageConsent: "biodata_consent_v2", trainingConsent: "biodata_training_consent_v1", telemetryDrops: "biodata_telemetry_drops_v1", usageUploadMeta: "biodata_usage_upload_meta_v1", usageClearEpoch: "biodata_usage_clear_epoch_v2", pingSent: "biodata_ping_sent_v1", feedbackPending: "biodata_feedback_pending_v1", projectsCoachmark: "biodata_projects_coachmark_v1" };
export const $ = (id) => document.getElementById(id);

/* 领域清单（骨肉分离）：品牌/维度由后端活动领域包下发，前端品牌文案不再硬编码。
   缓存一次；离线/旧后端一律回退 biodata 内置默认（与旧版逐位一致）。 */
let _domainManifestCache = null;
export async function loadDomainManifest() {
    if (_domainManifestCache) return _domainManifestCache;
    try {
        const r = await fetch(API.domainManifest);
        const j = await r.json();
        if (j && j.ok && j.domain) { _domainManifestCache = j.domain; return _domainManifestCache; }
    } catch (e) { /* 回退默认 */ }
    _domainManifestCache = {
        domain_id: "biodata",
        branding: {
            product_name: "BioData Agent",
            page_title: "BioData Agent · 生物数据集检索",
            dataset_page_title: "数据集介绍 · BioData Agent",
        },
    };
    return _domainManifestCache;
}
export function brandName(manifest) {
    const b = manifest && manifest.branding;
    return (b && b.product_name) || "BioData Agent";
}
/* 缓存代（2026-08-22 起提到 core 共用）：任意 /static/js/ 脚本 src 的 ?v= 令牌，
   与 benchfb.js / usage_upload.js 既有读法一致；没有即空串（不上传空环境信息，也不动 index.html）。 */
export function cacheGeneration() {
    const s = document.querySelector('script[src*="/static/js/"]');
    const m = s && /[?&]v=([^&"]+)/.exec(s.src);
    return m ? m[1] : "";
}
export const HEART = '<svg viewBox="0 0 24 24"><path d="M12 20.3s-6.7-4.3-9.2-8.2C1 9.1 2.4 5.8 5.7 5.8c1.9 0 3.2 1.1 4 2.2.8-1.1 2.1-2.2 4-2.2 3.3 0 4.7 3.3 2.9 6.3-2.5 3.9-8.2 8.2-8.2 8.2z"/></svg>';

/* 登录账户（accounts.js 经 setCurrentUser 设置；null=匿名）。用户数据 localStorage key 按账户 namespace，
   让共用一台机器/浏览器的多人不互相看到对方的记忆/收藏/历史。匿名沿用原 key（旧行为逐位不变）。 */
export let CURRENT_USER = null;
/* 可变共享状态只允许属主模块写：accounts.js 改 CURRENT_USER 必经本 setter（ESM live binding 对外只读）。 */
export function setCurrentUser(u) { CURRENT_USER = u; }
/* 遥测等异步任务必须在开工时捕获 scope，回调不得再读会变化的 CURRENT_USER。
   空串 = 匿名旧命名空间（保持既有 key 兼容）；登录账户 = 服务端随机 id。 */
export function currentAccountScope() { return (CURRENT_USER && CURRENT_USER.id) ? String(CURRENT_USER.id) : ""; }
export function nsKeyFor(base, scope) { return scope ? base + "::u:" + String(scope) : base; }
export function nsKey(base) { return nsKeyFor(base, currentAccountScope()); }

/* ---------- motion（GSAP，可选 + 尊重 prefers-reduced-motion） ----------
   一切动效经 MOTION 门控：GSAP 未加载（离线/失败）或用户偏好减弱动效时，
   MOTION=false，页面回退到纯静态，功能完全不受影响。 */
export const HAS_GSAP = typeof window.gsap !== "undefined";
export const HAS_ST = HAS_GSAP && typeof window.ScrollTrigger !== "undefined";
export const REDUCE_MOTION = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
export const MOTION = HAS_GSAP && !REDUCE_MOTION;
if (HAS_GSAP) {
    gsap.defaults({ ease: "power3.out", duration: 0.6 });
    if (HAS_ST) gsap.registerPlugin(ScrollTrigger);
}
/* 数字滚动：0.55s 缓动到目标值，非动效环境直接落值。 */
export function countUp(el, to) {
    to = Number(to) || 0;
    if (!el) return;
    // 快速连续调用（连搜 / 放宽预览）时杀掉在途旧动画与旧兜底：否则两个 tween 同写 textContent，
    // 旧 tween 后结束会把新值盖回去。
    if (el._countUpTween) { el._countUpTween.kill(); el._countUpTween = null; }
    if (el._countUpTimer) { clearTimeout(el._countUpTimer); el._countUpTimer = null; }
    if (!MOTION) { el.textContent = String(to); return; }
    const o = { v: Number(String(el.textContent).replace(/[^\d.]/g, "")) || 0 };
    el._countUpTween = gsap.to(o, { v: to, duration: 0.55, ease: "power2.out", onUpdate: () => { el.textContent = String(Math.round(o.v)); }, onComplete: () => { el._countUpTween = null; } });
    // 落值兜底：后台标签页 rAF 停摆时 tween 永不推进（实测 updates=0），计数会卡旧值直到重新聚焦。
    // 正确性不许依赖可见性——setTimeout 在后台仍会（被节流地）触发，到点按目标值直接落值。
    el._countUpTimer = setTimeout(() => {
        el._countUpTimer = null;
        if (el._countUpTween) { el._countUpTween.kill(); el._countUpTween = null; }
        el.textContent = String(to);
    }, 700);
}
/* 退场幽灵（2026-08-30）：元素将被 display:none 类瞬时隐藏（或整树搬离原位）前，
   钉一粒 fixed 定位的克隆在它的旧屏幕位置淡出/漂移——布局切换照旧瞬时完成（FLIP/搬家不排队），
   视觉上却是「淡走」而不是「消失/闪现」。克隆剥掉全部 id（防重复 id 污染 $() 查询）、
   aria-hidden + pointer-events:none，动画结束即移除（setTimeout 兜底后台标签页 rAF 停摆）。
   MOTION 关 / reduced-motion / 元素本就不可见时整体跳过，零 DOM 残留。 */
export function ghostExit(el, opts) {
    if (!MOTION || !el || !el.getClientRects().length) return;
    opts = opts || {};
    const r = el.getBoundingClientRect();
    const g = el.cloneNode(true);
    /* keepIds（2026-09-03）：默认剥光克隆树 id（防重复 id 污染 $() 查询）。但 .console 子树的
       圆形发送键整套 pill 造型锚在 #submitBtn id 上——剥 id 后幽灵失配回落 .btn 基态（方角）
       与 .btn:disabled（灰底），肉眼即「点击发送后按钮短暂变方」。该真身在幽灵存活期（~0.34s）
       始终留在 DOM 前序位置（display:none 而非移除），getElementById/querySelector 恒定先命中
       真身；幽灵又 aria-hidden + pointer-events:none + 挂在 body 末尾——短暂共存无查询污染，
       故对它保留 id，幽灵与真身分毫不差。 */
    if (!opts.keepIds) {
        g.querySelectorAll("[id]").forEach((n) => n.removeAttribute("id"));
        g.removeAttribute("id");
    }
    g.setAttribute("aria-hidden", "true");
    const st = g.style;
    st.position = "fixed"; st.left = r.left + "px"; st.top = r.top + "px";
    st.width = r.width + "px"; st.height = r.height + "px";
    st.margin = "0"; st.pointerEvents = "none"; st.zIndex = "30";
    document.body.appendChild(g);
    const dur = opts.duration || 0.3;
    gsap.to(g, {
        autoAlpha: 0, y: opts.y != null ? opts.y : -10,
        duration: dur, ease: opts.ease || "power2.in",
        onComplete: () => { g.remove(); },
    });
    setTimeout(() => { if (g.parentElement) g.remove(); }, (dur + 0.4) * 1000);
}
/* 卡片入场：results 用即时错峰；browse 用 ScrollTrigger 滚动逐屏浮现（带兜底，绝不留隐藏卡）。 */
let _revealST = [];
export function killRevealST() { if (HAS_ST) _revealST.forEach((t) => t && t.kill()); _revealST = []; }
export function revealCards(cards, scroll) {
    cards = Array.from(cards || []);
    if (!MOTION || !cards.length) return;
    // clearProps:"transform" —— 收尾清掉 GSAP 写入的 inline transform，
    // 否则会盖住 CSS 的 .card:hover 上浮（保住原有卡片悬停手感）。
    if (scroll && HAS_ST) {
        gsap.set(cards, { autoAlpha: 0, y: 24 });
        _revealST = ScrollTrigger.batch(cards, {
            start: "top 90%", once: true,
            onEnter: (els) => gsap.to(els, { autoAlpha: 1, y: 0, duration: 0.55, stagger: 0.06, ease: "power3.out", overwrite: true, clearProps: "transform" }),
        }) || [];
        ScrollTrigger.refresh();
        gsap.delayedCall(1.2, () => gsap.to(cards, { autoAlpha: 1, y: 0, duration: 0.3, overwrite: "auto", clearProps: "transform" }));  // 兜底：ST 未触发也绝不留隐藏卡
    } else {
        gsap.from(cards, { autoAlpha: 0, y: 22, duration: 0.5, stagger: 0.05, ease: "power3.out", clearProps: "transform" });
    }
}

/* ---------- utils ---------- */
export function escapeHtml(v) {
    return String(v ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}
/* 后端少量诚实层文案（feasibility/compatibility 的 caveat、gaps）用成对 `**…**` 标注强调。
   只在这类文本的渲染点用本函数：先 escapeHtml（XSS 不变），再把 `**…**` 转 <strong>…</strong>。
   不是通用 markdown 解析器——只认成对 `**`，不支持嵌套与其它语法；后端字符串一律不动。 */
export function escapeHtmlStrong(v) {
    return escapeHtml(v).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}
export function isHttp(u) { return /^https?:\/\//i.test(String(u || "").trim()); }
export function prettyPlatform(p) { p = String(p || "").trim(); return p ? p.charAt(0).toUpperCase() + p.slice(1) : ""; }
export function prettySource(s) { s = String(s || "").trim(); if (/cellxgene/i.test(s)) return "CELLxGENE"; if (/10x/i.test(s)) return "10x"; if (/human cell atlas|hca/i.test(s)) return "HCA"; if (/ebi|expression atlas/i.test(s)) return "EBI SCEA"; if (/hubmap/i.test(s)) return "HuBMAP"; if (/single.?cell.?portal|scp/i.test(s)) return "SCP"; if (/refine\.?\s?bio/i.test(s)) return "refine.bio"; if (/geo/i.test(s)) return "GEO"; return s; }
export function rawStatusFromBool(v) { if (v === true) return "含 FASTQ"; if (v === false) return "无 FASTQ"; return "原始数据未标注"; }
/* Blob 下载共用样板：task_pack/reuse_pack/benchfb/project_exports 四处同款手抄的
   单一真源。revoke 统一 4000ms——任务包/导出 zip 可达数 MB，慢盘浏览器 1s 内未必读完 blob
   就 revoke 会断下载（benchfb 原手抄取 4s 的教训），统一采用保守值。 */
export function downloadBlobAs(blob, name) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
}
/* 剪贴板写入的单一实现（与 downloadBlobAs 同级的浏览器能力基元）：
   navigator.clipboard.writeText 优先；不可用（非安全上下文）或失败时退 textarea + execCommand 兜底。
   返回 Promise<boolean>——是否真的写进了剪贴板，调用方据它做成功态/回执。
   okMsg：成功时 toast（省略则静默，调用方自理成功态，如按钮文案变换）；
   failMsg：失败时 toast，缺省用通用句（单一锚点，全站同一句）。 */
function _copyTextLegacy(text) {
    try {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        const ok = !!(document.execCommand && document.execCommand("copy"));
        ta.remove();
        return ok;
    } catch (_e) { return false; }
}
export function copyTextAny(text, msgs) {
    msgs = msgs || {};
    const failMsg = msgs.failMsg || "复制失败，请手动选择复制";
    const report = (ok) => {
        if (ok) { if (msgs.okMsg) toast(msgs.okMsg); }
        else toast(failMsg);
        return ok;
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
        return navigator.clipboard.writeText(text).then(() => report(true)).catch(() => report(_copyTextLegacy(text)));
    }
    return Promise.resolve(report(_copyTextLegacy(text)));
}
/* 与后端 units.format_sample_size（全项目规范单一真源）逐位对齐：
   count+unit → "{count} {unit}"；仅 unit → "未说明 {unit}"；仅 count → "{count}"；都无 → "未说明"。
   旧前端私改「仅 count → {count} 未说明单位」已消除——卡脚、介绍关键事实、冻结 markdown 表三处同格式。 */
export function sampleFrom(count, unit) {
    count = String(count ?? "").trim(); unit = String(unit ?? "").trim();
    if (count && unit) return count + " " + unit;
    if (unit) return "未说明 " + unit;
    if (count) return count;
    return "未说明";
}
/* 分面值展示归一（只动展示层；原值经 data-val / title 保留）：
   全大写长词视作排版噪音 → 词首大写（HUMAN → Human）；短大写视作缩写保留（PBMC 不动）；
   全小写词首字母大写（brain → Brain）；已含大写的混合词原样（Homo sapiens 不动）。 */
export function prettyFacetValue(v) {
    const s = String(v ?? "").trim().replace(/\s+/g, " ");
    return s.replace(/[A-Za-z]+/g, (w) => {
        if (w === w.toUpperCase()) return w.length >= 5 ? w.charAt(0) + w.slice(1).toLowerCase() : w;
        if (w === w.toLowerCase()) return w.charAt(0).toUpperCase() + w.slice(1);
        return w;
    });
}
export function fastqInfo(text) {
    text = String(text || "");
    // 纯文字匹配，同时兼容后端旧式 emoji 串（「✅ 包含 FASTQ」⊃「含 FASTQ」、「❌ 无 FASTQ」⊃「无 FASTQ」）。
    // 顺序不能反：必须先判「无 FASTQ」（「包含 FASTQ」里也含「含 FASTQ」子串）。
    if (text.includes("无 FASTQ")) return { cls: "no", label: "无 FASTQ" };
    if (text.includes("含 FASTQ")) return { cls: "ok", label: "含 FASTQ" };
    return { cls: "unknown", label: "原始数据未标注" };
}
export function fmtTime(ts) { try { return new Date(ts).toLocaleString("zh-CN", { hour12: false }); } catch (_e) { return ""; } }
export function activeView() { const el = document.querySelector(".view.active"); return el ? el.dataset.view : ""; }

let toastTimer = null;
let toastDismissBound = false;
/* sticky=true：常驻到用户点一下才关（不自动消失）。用于「点了没反应=多半被浏览器拦了」
   这类必须被读完的提醒——4 秒上限的瞬时 toast 一滑而过，用户照样不知所措（2026-09-11
   用户实测：拦截提示根本没看到）。 */
export function toast(msg, opts) {
    const t = $("toast");
    const sticky = !!(opts && opts.sticky);
    t.textContent = msg;
    t.classList.add("show");
    t.classList.toggle("sticky", sticky);
    if (sticky && !toastDismissBound) {
        toastDismissBound = true;
        t.addEventListener("click", () => t.classList.remove("show"));
    }
    if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
    if (sticky) return;
    const ms = Math.round(Math.max(2400, Math.min(4000, 1600 + String(msg).length * 70)));
    toastTimer = setTimeout(() => t.classList.remove("show"), ms);
}

/* 程序化新开标签页的唯一出口（2026-09-09 弹窗拦截提醒，与 downloads.js 单通道同纪律）。
   不用 noopener 特性：它使 window.open 恒返回 null（2026-09-12 实测，标准行为），
   「被拦」与「成功」无法区分；改为成功后手动 w.opener=null 防 tabnabbing。
   被拦（返回 null）时给可操作的提醒（文案唯一锚点 COPY.common.popupBlocked），
   绝不静默无事发生。返回窗口句柄（被拦=null），调用方据此决定要不要发成功回执。 */
export function openPopup(url) {
    const w = window.open(url, "_blank");
    if (!w) {
        toast(COPY.common.popupBlocked, { sticky: true });
        return null;
    }
    try { w.opener = null; } catch (_e) { /* 防 tabnabbing；置 opener 失败不挡打开 */ }
    return w;
}

/* ---------- storage ---------- */
export function readJSON(k, fb) { try { const v = JSON.parse(localStorage.getItem(k)); return v ?? fb; } catch (_e) { return fb; } }
export function writeJSON(k, v) { localStorage.setItem(k, JSON.stringify(v)); }
export function clampInt(v, dflt, lo, hi) { const n = parseInt(v, 10); if (!Number.isFinite(n)) return dflt; return Math.max(lo, Math.min(hi, n)); }
export const MISSING_DISPLAY_VALUES = new Set(["unknown", "not specified", "not provided", "n/a", "na", "none", "null", "-"]);
export function displayText(value) {
    const text = String(value ?? "").trim();
    return MISSING_DISPLAY_VALUES.has(text.toLowerCase()) ? "" : text;
}
/* 千分位分组：结果卡上的大数字与首页覆盖声明「5,712」/ 可行性面板「3,086,388」
   同一记数法——此前同屏混用两种。只把 ≥4 位的连续数字段分组；**不许**往里面塞年份
   （4 位年份会被加成 2,025）——年份保持原样由调用方自己渲染。 */
export function fmtThousands(text) {
    return String(text ?? "").replace(/\d{4,}/g, (m) => Number(m).toLocaleString("en-US"));
}
export function normalizeItem(it) {
    // 不再把「未说明 {unit}」私删成「未说明」——与后端 format_sample_size / 介绍关键事实同一格式
    const sampleSize = displayText(it.sample_size) || sampleFrom(it.count, it.unit);
    return {
        dataset_name: String(it.dataset_name || "").trim(), species: displayText(it.species), tissue: displayText(it.tissue), disease: displayText(it.disease),
        chemistry: displayText(it.chemistry), platform: displayText(it.platform || it.platform_family), assay: displayText(it.assay),
        sample_size: sampleSize,
        gene_count: displayText(it.gene_count),   // 10x 平台信息补充：检测基因数（无补充 → ""）
        raw_data_status: it.raw_data_status || rawStatusFromBool(it.has_raw_data),
        url: it.url || "", download_url: it.download_url || it.url || "", reason: it.reason || "",
        dataset_uid: it.dataset_uid || "", n_files: it.n_files || 0, published_date: displayText(it.published_date),
        source: displayText(it.source), description: displayText(it.description),
        preservation_method: displayText(it.preservation_method), analysis_software: displayText(it.analysis_software), software_version: displayText(it.software_version),
        introduction: (it.introduction && typeof it.introduction === "object") ? it.introduction : null,
        reachability: (it.reachability && typeof it.reachability === "object") ? it.reachability : null,
        folder: String(it.folder || "")   // 收藏夹归属（收藏条目专属字段；缺省/老数据 → "" = 默认收藏夹）
    };
}
export function itemKey(it) { return (it.dataset_name || "") + "|" + (it.url || ""); }
/* 收藏夹归属的防御性读取：老收藏条目没有 folder 字段 → 一律视为默认收藏夹（""）。
   读取处统一走这里，不在各消费点各自 (it.folder || "")。 */
export function favFolderOf(it) { return String((it && it.folder) || ""); }
export function getFavs() { return readJSON(nsKey(LS.fav), []); }
export function setFavs(a) { writeJSON(nsKey(LS.fav), a); }
export function isFav(it) { const k = itemKey(it); return getFavs().some((f) => itemKey(f) === k); }
export function toggleFav(it, folder, anchorEl) {
    const a = getFavs(); const k = itemKey(it); const i = a.findIndex((f) => itemKey(f) === k);
    let added; if (i >= 0) { a.splice(i, 1); added = false; }
    else { const norm = normalizeItem(it); norm.folder = String(folder || ""); a.unshift(norm); added = true; }
    setFavs(a);
    // schema v2（2026-08-22）：记 uid + 名次（anchorEl 是点中的卡片内按钮，
    // 经它找回卡片算名次），与 open/dl 同一套归因口径；仍不记数据集名等研究内容。
    // v3：经 usageLogCardAction——结果页的卡带展示快照 tid/iid，
    // 非结果页（收藏/浏览/详情）的卡无绑定快照，显式 null，不冒领当前轮。
    if (added) {
        const _favCard = anchorEl && anchorEl.closest ? anchorEl.closest(".card") : null;
        usageLogCardAction(_favCard, USAGE_KINDS.fav, {
            uid: String((it && it.dataset_uid) || ""),
            pos: usageCardRank(_favCard),
        });
    }
    return added;
}
/* ---------- 收藏夹（fav 条目的分组属性；列表本身存独立 per-account 键） ----------
   默认收藏夹是内置概念：id ""、名「默认收藏夹」，不进入用户夹列表、不可改名/删除。 */
export const DEFAULT_FAV_FOLDER_NAME = "默认收藏夹";
export function getFavFolders() {
    const raw = readJSON(nsKey(LS.favFolders), []);
    if (!Array.isArray(raw)) return [];
    return raw.filter((f) => f && typeof f === "object").map((f) => ({
        id: String(f.id || ""), name: String(f.name || "").trim(), createdAt: Number(f.createdAt) || 0,
    })).filter((f) => f.id && f.name);
}
export function setFavFolders(a) { writeJSON(nsKey(LS.favFolders), a); }
/* 夹 id → 显示名；"" 或已不存在的 id（老数据/异常态）都回落到默认收藏夹名。 */
export function favFolderNameById(id) {
    if (!id) return DEFAULT_FAV_FOLDER_NAME;
    const f = getFavFolders().find((x) => x.id === id);
    return f ? f.name : DEFAULT_FAV_FOLDER_NAME;
}
/* 归一化夹 id：已删除/未知的 id 视为默认夹（""），分组与过滤共用一个口径。 */
export function favFolderIdOrDefault(id) {
    if (!id) return "";
    return getFavFolders().some((f) => f.id === id) ? id : "";
}
/* 名字校验：trim 后非空、≤20 字、不与既有夹（含默认夹）重名。返回 "" = 通过，否则为错误文案。 */
export function validateFavFolderName(name, excludeId) {
    const n = String(name || "").trim();
    if (!n) return "收藏夹名字不能为空";
    if (n.length > 20) return "收藏夹名字最多 20 字";
    if (n === DEFAULT_FAV_FOLDER_NAME) return "不能和默认收藏夹重名";
    if (getFavFolders().some((f) => f.name === n && f.id !== excludeId)) return "已有同名收藏夹";
    return "";
}
export function addFavFolder(name) {
    const err = validateFavFolderName(name);
    if (err) return { ok: false, error: err };
    const folders = getFavFolders();
    const f = { id: "f-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8), name: String(name).trim(), createdAt: Date.now() };
    folders.push(f); setFavFolders(folders);
    return { ok: true, folder: f };
}
export function renameFavFolder(id, name) {
    const err = validateFavFolderName(name, id);
    if (err) return { ok: false, error: err };
    const folders = getFavFolders();
    const f = folders.find((x) => x.id === id);
    if (!f) return { ok: false, error: "收藏夹不存在" };
    f.name = String(name).trim(); setFavFolders(folders);
    return { ok: true };
}
/* 删除用户夹：该夹内条目归回默认夹（folder 改回 ""），条目本身保留。 */
export function deleteFavFolder(id) {
    const folders = getFavFolders();
    const next = folders.filter((f) => f.id !== id);
    if (next.length === folders.length) return false;
    setFavFolders(next);
    const favs = getFavs();
    let changed = false;
    favs.forEach((it) => { if (favFolderOf(it) === id) { it.folder = ""; changed = true; } });
    if (changed) setFavs(favs);
    return true;
}
/* 把某条已收藏条目移到指定夹（folderId "" = 默认夹）。只改 folder 属性，不动条目本身与顺序。 */
export function moveFavToFolder(it, folderId) {
    const a = getFavs(); const k = itemKey(it); const f = a.find((x) => itemKey(x) === k);
    if (!f) return false;
    f.folder = String(folderId || ""); setFavs(a); return true;
}
/* 仅对话历史行（2026-08-04）：纯工具对话（一句检索都没跑过）从不经过 pushHist——
   对话被丢弃（强制新开对话等）前由 board.js 的 cbArchiveChatOnly 调这里补一行。
   行形状与 pushHist 一致（convId + 累计 chat），两个差别：
   - `chatOnly: true` 是可识别标记：snap 恒 null、count 恒 0，browse.js 的展示/恢复按
     「只有对话没有结果」如实呈现；dream_core.js 按原口径消费（query=首句用户消息，chat 完整），不弄坏三方。
   - 去重：同一 convId 的仅对话行只留最新一条（回看后又丢弃等重复归档路径），不灌重复行。
     不能只看 a[0]：旧仅对话行可能已被后来的检索行压到深处，全表找同 convId 摘除再置顶。
   取数走 board.js 注册进来的运行期真源（setHistHooks），不加形参——与 pushHist 同一哲学。 */
export function pushHistChatOnly() {
    const chat = _histLogForHistory ? _histLogForHistory() : [];
    if (!chat.length) return;
    const firstUser = chat.find((m) => m.k === "say" || m.k === "refine" || m.k === "action");
    const entry = { query: firstUser ? String(firstUser.t || "") : "", count: 0, at: Date.now(), snap: null,
        chatOnly: true, convId: _histConvId ? _histConvId() : "", chat: chat,
        facetFilters: [], suppressed: [], lenientDims: [], queryHits: [] };
    const a = getHist();
    const dup = a.findIndex((h) => h && h.chatOnly && String(h.convId || "") === String(entry.convId));
    if (dup >= 0) a.splice(dup, 1);
    a.unshift(entry);
    let arr = a.slice(0, 50);
    for (;;) {
        try { writeJSON(nsKey(LS.hist), arr); break; }
        catch (_e) {
            // 与 pushHist 同口径的配额降级：从最旧的仍带快照的行起逐个剥成「仅元信息」。
            // 仅对话行没有 snap 可剥（本来就极小），永远不会是剥离对象。
            let idx = -1;
            for (let k = arr.length - 1; k >= 0; k--) { if (arr[k].snap) { idx = k; break; } }
            if (idx < 0) break;   // 已全是元信息仍写不下 → 放弃本次写入（保留原有历史）
            arr = arr.map((e, k) => (k === idx ? { query: e.query, count: e.count, at: e.at, convId: e.convId, chat: e.chat } : e));
        }
    }
}
export function getHist() { return readJSON(nsKey(LS.hist), []); }
/* 历史记录：不再只存「查询 + 条数」（点一下重跑），而是**存下当次的具体结果快照**——
   完整后端响应 data（含 results/facets/result_total…）+ 当时的分面筛选 _facetFilters；点历史项直接回看当时结果，不重发后端。
   快照较大：写入配额溢出时逐步丢最旧项；仍写不下则退化成「仅元信息」（那几项点开会回退重跑）。历史是易失的、可牺牲。

   2026-07-29 起每行还带 `convId` + `chat`（用户反馈）：一段对话会产出多行历史，
   靠 convId 在历史视图里合成一行、点开时整条对话一起回来。`chat` 是**到这一轮为止的累计**
   对话记录（board.js 的 `cbLogForHistory()` 压扁而成），回看时按相邻两行的长度差把消息归属到各轮。
   这两个值取自 board.js 注册进来的运行期真源（`setHistHooks`，2026-08-10 起 core 不再
   反向 import board）：**故意不加进本函数的形参**——形参已经七个，
   再塞两个必然有调用点漏传；注册钩子就没有「传漏了」这回事。钩子只在本函数被调用时
   （用户真检索时）取值，那时 initCondBoard 早已注册完毕。 */
export function pushHist(q, data, facetFilters, suppressed, queryHits, lenientDims, replaceLast) {
    const a = getHist();
    const count = ((data && data.results) || []).length;
    const entry = { query: q, count, at: Date.now(), snap: data || null,
        convId: _histConvId ? _histConvId() : "", chat: _histLogForHistory ? _histLogForHistory() : [],
        facetFilters: (facetFilters || []).map((f) => ({ dim: f.dim, value: f.value, display: f.display, label: f.label })),
        suppressed: (suppressed || []).slice(),
        lenientDims: (lenientDims || []).slice(),   // 诚实降级宽容态：跨回看正确复原（老快照无此字段→空）
        queryHits: (queryHits || []).map((g) => ({ filter_id: g.filter_id || g.dim, polarity: g.polarity || "include", dim: g.dim, label: g.label, values: (g.values || []).slice() })) };   // 原始命中完整快照：忽略态跨回看正确复原（含被忽略的 chip、极性 filter_id）
    // 只有「分面细化重跑」才原地更新最近一行（同一次查询的连续收窄不该灌一堆近乎相同的行）；
    // 用户主动发起的新查询——哪怕与上一条完全相同——一律新起一行，重复项照实并列显示，不去重。
    if (replaceLast && a.length && a[0].query === q) a[0] = entry;
    else a.unshift(entry);
    let arr = a.slice(0, 50);
    for (;;) {
        try { writeJSON(nsKey(LS.hist), arr); break; }
        catch (_e) {
            // 配额不足：从**最旧的、仍带完整快照**的条目起，逐个剥成「仅元信息」（保留该行、只丢 snap），直到写得下——
            // 保住全部行数（点开被剥的行会回退重跑），而不是整行丢弃。
            let idx = -1;
            for (let k = arr.length - 1; k >= 0; k--) { if (arr[k].snap) { idx = k; break; } }   // arr[0] 最新、末尾最旧
            if (idx < 0) break;   // 已全是元信息仍写不下 → 放弃本次写入（保留原有历史）
            // 剥的只有 `snap`（那才是大头）。**convId / chat 必须留着**：剥掉 convId 会让这一轮
            // 从它所属的对话里掉出来、在历史视图里另起一行；剥掉 chat 会让后面几轮的
            // 「累计长度差」错位，整条对话的消息全归错帧。两者加起来不过几十个短字符串。
            // snap_evicted 打标（2026-08-09）：点开被剥的行会回退重跑——
            // 那是**重新检索**不是「当时的结果」，回看路径必须如实告诉用户，不许静默冒充。
            arr = arr.map((e, k) => (k === idx ? { query: e.query, count: e.count, at: e.at,
                convId: e.convId, chat: e.chat, snap_evicted: true } : e));
        }
    }
}
