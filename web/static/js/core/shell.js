"use strict";

/* 本文件是 ES Module：core 的工具经 import 取；LS_SIDE 自 #cards、placeFacetBar 自 #facets、
   getSelectedSources/getSourceMode 自 #interactions、ensureDatasetsLoaded/renderFavorites 自
   #browse、resetFavFolderState 自 #fav_folders import（shell↔browse / shell↔interactions 成环，
   绑定只在函数体内使用）；benchfbSyncSettings 自 #benchfb import（设置打开时刷新
   使用反馈卡片的本机编号行——benchfb 不反向 import 本文件，无环）。
   boot/interactions/onboarding/browse/search/board/task_pack/act/dream 经 import 取本文件导出
   （绞杀桥已全退役）。 */
import { $, API, LS, MOTION, REDUCE_MOTION, clampInt, readJSON, toast, writeJSON } from "#core";
import { LS_SIDE } from "#cards";
import { placeFacetBar } from "#facets";
import { ensureDatasetsLoaded } from "#browse";
import { getSelectedSources, getSourceMode } from "#interactions";
import { benchfbSyncSettings } from "#benchfb";

export function setSidebar(open) {
    document.body.classList.toggle("side-closed", !open);
    // 移动端抽屉与浮窗互斥（2026-08-04：我的库 + 历史两窗都关）：浮窗曾靠抬 z 压回抽屉，
    // 结果抽屉导航/遮罩被浮窗盖死点不到——层级对调治不好，开抽屉即关浮窗。桌面不动：
    // 那边侧栏非遮罩，浮窗与侧栏本就同层共存。
    if (open && window.innerWidth <= 780) { closeLibWin(); closeHistWin(); }
    if (window.innerWidth > 780) { try { localStorage.setItem(LS_SIDE, open ? "0" : "1"); } catch (_e) {} }
    if (typeof placeFacetBar === "function") placeFacetBar(false);   // 收起/展开只安置分面条落位，不播导航收缩 FLIP（侧栏本身有滑入/滑出过渡，导航动画会与之抢戏、重复）
}
function closeSidebarOnMobile() { if (window.innerWidth <= 780) { document.body.classList.add("side-closed"); if (typeof placeFacetBar === "function") placeFacetBar(); } }   // 抽屉关闭 → 分面条搬回结果区，别遗落在离屏抽屉里
/* ≤780px 固钉 logo（side-fab）恒显会压住从它下面滚过的正文（放宽横幅/控制台顶边被盖）。
   向下滚动阅读时把它淡出（fab-tucked），向上回滚/回到顶部即还——只切 class，动画走 side-fab 既有
   transition；桌面不动（那边 fab 与内容无冲突）。 */
let _fabLastY = 0, _fabTicking = false;
function _syncFabTuck() {
    _fabTicking = false;
    const y = window.scrollY || 0;
    if (window.innerWidth > 780 || y <= 48 || y < _fabLastY - 4) document.body.classList.remove("fab-tucked");
    else if (y > _fabLastY + 4) document.body.classList.add("fab-tucked");
    _fabLastY = y;
}
export function initFabTuck() {
    window.addEventListener("scroll", () => {
        if (!_fabTicking) { _fabTicking = true; requestAnimationFrame(_syncFabTuck); }
    }, { passive: true });
}
export function initSidebar() {   // 移动端默认收起（抽屉）；桌面读持久化，默认展开
    if (window.innerWidth <= 780) { document.body.classList.add("side-closed"); return; }
    try { if (localStorage.getItem(LS_SIDE) === "1") document.body.classList.add("side-closed"); } catch (_e) {}
}
const SIDEBAR_WIDTH = { min: 240, max: 420, dflt: 300 };
function setSidebarWidth(value, persist) {
    const n = Number(value);
    const width = Math.round(Math.max(SIDEBAR_WIDTH.min, Math.min(SIDEBAR_WIDTH.max, Number.isFinite(n) ? n : SIDEBAR_WIDTH.dflt)));
    document.documentElement.style.setProperty("--side-w", `${width}px`);
    const resizer = $("sideResizer");
    if (resizer) resizer.setAttribute("aria-valuenow", String(width));
    if (persist) { try { localStorage.setItem(LS.sidebarWidth, String(width)); } catch (_e) {} }
    return width;
}
export function initSidebarResize() {
    const resizer = $("sideResizer"), sidebar = $("sidebar");
    if (!resizer || !sidebar || resizer.dataset.bound) return;
    resizer.dataset.bound = "1";
    let saved = SIDEBAR_WIDTH.dflt;
    try { saved = Number(localStorage.getItem(LS.sidebarWidth)) || SIDEBAR_WIDTH.dflt; } catch (_e) {}
    setSidebarWidth(saved, false);

    let drag = null;
    resizer.addEventListener("pointerdown", (event) => {
        if (event.button !== 0 || window.innerWidth <= 780) return;
        const rect = sidebar.getBoundingClientRect();
        drag = { pointerId: event.pointerId, left: rect.left };
        resizer.setPointerCapture(event.pointerId);
        document.body.classList.add("side-resizing");
        event.preventDefault();
    });
    resizer.addEventListener("pointermove", (event) => {
        if (!drag || event.pointerId !== drag.pointerId) return;
        setSidebarWidth(event.clientX - drag.left, false);
    });
    const finish = (event) => {
        if (!drag || (event && typeof event.pointerId === "number" && event.pointerId !== drag.pointerId)) return;
        const width = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--side-w"));
        drag = null;
        document.body.classList.remove("side-resizing");
        setSidebarWidth(width, true);
    };
    resizer.addEventListener("pointerup", finish);
    resizer.addEventListener("pointercancel", finish);
    resizer.addEventListener("lostpointercapture", finish);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
    window.addEventListener("blur", () => finish(null));
    window.addEventListener("resize", () => { if (window.innerWidth <= 780) finish(null); });
    resizer.addEventListener("keydown", (event) => {
        const current = Number(resizer.getAttribute("aria-valuenow")) || SIDEBAR_WIDTH.dflt;
        let next = current;
        if (event.key === "ArrowLeft") next -= 12;
        else if (event.key === "ArrowRight") next += 12;
        else if (event.key === "Home") next = SIDEBAR_WIDTH.min;   // Home=最窄（惯例），End=最宽；重置默认宽用双击分隔条
        else if (event.key === "End") next = SIDEBAR_WIDTH.max;
        else return;
        event.preventDefault();
        setSidebarWidth(next, true);
    });
    // 双击分隔条 → 复位到默认宽（补 Home 改成 min 后失去的"回默认"入口）。
    resizer.addEventListener("dblclick", (event) => { event.preventDefault(); setSidebarWidth(SIDEBAR_WIDTH.dflt, true); });
}

export function initStrategyTooltips() {
    const triggers = Array.from(document.querySelectorAll(".strategy-tip[data-tooltip]"));
    if (!triggers.length || $("strategyTooltip")) return;
    const tip = document.createElement("div");
    tip.id = "strategyTooltip";
    tip.className = "strategy-tooltip";
    tip.hidden = true;
    tip.setAttribute("role", "tooltip");
    document.body.appendChild(tip);
    let current = null;
    const hide = () => { current = null; tip.hidden = true; tip.textContent = ""; };
    const show = (trigger) => {
        current = trigger;
        tip.textContent = trigger.dataset.tooltip || "";
        tip.hidden = false;
        const rect = trigger.getBoundingClientRect();
        const tr = tip.getBoundingClientRect();
        const gap = 8;
        let left = rect.left + (rect.width - tr.width) / 2;
        left = Math.max(12, Math.min(window.innerWidth - tr.width - 12, left));
        let top = rect.top - tr.height - gap;
        if (top < 12) top = Math.min(window.innerHeight - tr.height - 12, rect.bottom + gap);
        tip.style.left = `${Math.round(left)}px`;
        tip.style.top = `${Math.round(top)}px`;
    };
    triggers.forEach((trigger) => {
        trigger.addEventListener("pointerenter", () => show(trigger));
        trigger.addEventListener("pointerleave", () => { if (current === trigger && !trigger.matches(":focus-within") && document.activeElement !== trigger) hide(); });
        trigger.addEventListener("focusin", () => show(trigger));
        trigger.addEventListener("focusout", () => { if (current === trigger) hide(); });
    });
    window.addEventListener("resize", hide);
    document.addEventListener("scroll", hide, true);
}

/* ---------- router ---------- */
export function showView(name) {
    document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.dataset.view === name));
    document.querySelectorAll(".side-nav .nav-item[data-view]").forEach((n) => n.classList.toggle("active", n.dataset.view === name));
    document.body.classList.toggle("on-query", name === "query");   // 仅智能查询视图允许侧栏二分（承载分面细化）
    closeSidebarOnMobile();   // 移动端选完页面顺手收起抽屉
    window.scrollTo({ top: 0, behavior: "smooth" });
    if (name === "browse") ensureDatasetsLoaded();
    // 我的库（追踪+收藏）与历史记录都收进浮窗（#libWin / #histWin），不占视图位——
    // 本函数永远不该收到 "lib"/"history"/"favorites"/"archive"；收到也不渲染页面。
    placeFacetBar(false, true);   // 切视图：不播导航 FLIP（导航卡保持原样、跨视图两列不动），但让数据细化卡按新视图淡入(回查询)/淡出(离开)
}

/* ---------- 通用浮窗工厂（骨架 开合/拖动/缩放/落位/重钳 每窗一份） ----------
   把档案浮窗的骨架（开合/拖动/缩放/落位/重钳/关窗计时器）抽成每窗一份的控制器；
   #histWin 与 #libWin 共用，几何参数与既有实现逐位一致。渲染器注册反转保留——
   projects.js（追踪）/ browse.js（收藏·历史）经 setLibRenderer/setHistRenderer 注册，
   本文件不反向 import 它们的渲染器（防新环；browse→shell / projects→shell 边早已存在）。 */
export function initFloatingWin(winEl, opts) {
    const w = winEl;
    const head = (opts && opts.head) || (w && w.querySelector(".hw-head"));
    const rz = (opts && opts.resize) || (w && w.querySelector(".hw-resize"));
    const closeBtn = (opts && opts.close) || (w && w.querySelector(".hw-close"));
    if (!w) return null;
    let closeTimer = null;

    function isOpen() { return !!(w && !w.hidden); }

    function open() {
        if (closeTimer) { clearTimeout(closeTimer); closeTimer = null; w.classList.remove("hist-win-out"); }
        if (w.hidden) {
            w.hidden = false;
            if (!w.dataset.placed) {   // 首次落位：主区中央偏上（侧栏占左，稍右偏）；之后记住拖动位置
                w.dataset.placed = "1";
                const ww = w.offsetWidth || 560;
                // 2026-08-04：+110 的桌面右偏在 ≤780px 视口豁免（实测移动宽度右缘溢出 98px、
                // 按钮不可达），且任何宽度下都钳进视口（右缘留 12px）。
                const wide = window.innerWidth > 780;
                const left = (window.innerWidth - ww) / 2 + (wide ? 110 : 0);
                w.style.left = Math.min(Math.max(12, left), Math.max(12, window.innerWidth - ww - 12)) + "px";
                w.style.top = Math.max(12, Math.round(window.innerHeight * 0.12)) + "px";
            }
        }
        if (typeof opts.onOpen === "function") opts.onOpen();
    }

    /* 关窗补退出动画（hist-win-out，与入场 histWinIn 对称）——动画播完才真正 hidden；
       REDUCE_MOTION 直接 hidden。 */
    function close() {
        if (!w || w.hidden) return;
        if (REDUCE_MOTION) { w.hidden = true; return; }
        w.classList.add("hist-win-out");
        if (closeTimer) clearTimeout(closeTimer);
        closeTimer = setTimeout(() => {
            closeTimer = null;
            w.classList.remove("hist-win-out");
            w.hidden = true;
        }, 180);   // 与 app.css histWinOut 时长一致
    }

    function toggle() { if (isOpen()) close(); else open(); }

    /* 落位钳制此前只在首开（dataset.placed）算一次——浮窗开着时缩窗，窗口留在
       旧坐标、右缘与关闭按钮出屏不可达。resize 时对已开浮窗按当前尺寸重钳回视口，
       口径与拖动钳制同（保左缘 80px 可抓、右下角缩放把手留屏内 8px）。 */
    function reclamp() {
        if (!w || w.hidden || !w.dataset.placed) return;
        const r = w.getBoundingClientRect();
        const maxLeft = Math.max(0, Math.min(window.innerWidth - 80, window.innerWidth - r.width - 8));
        const maxTop = Math.max(0, Math.min(window.innerHeight - 44, window.innerHeight - r.height - 8));
        const left = Math.min(Math.max(0, r.left), maxLeft);
        const top = Math.min(Math.max(0, r.top), maxTop);
        if (Math.abs(left - r.left) > 0.5) w.style.left = left + "px";
        if (Math.abs(top - r.top) > 0.5) w.style.top = top + "px";
    }

    if (closeBtn) closeBtn.addEventListener("click", close);
    // 拖动（标题栏，按钮除外）：指针捕获（无捕获时指针一甩出 44px 标题栏就断拖）；
    // 钳制不只保左缘可抓，还要保右下角缩放把手恒在视口内。
    if (head) head.addEventListener("pointerdown", (e) => {
        if (e.target.closest("button")) return;
        const r = w.getBoundingClientRect();
        const dx = e.clientX - r.left, dy = e.clientY - r.top;
        const pid = e.pointerId;
        try { head.setPointerCapture(pid); } catch (_e) {}   // 捕获后指针滑出标题栏/窗口也照收 move/up
        const move = (ev) => {
            if (ev.pointerId !== pid) return;
            // 右钳制取「留 80px 可抓」与「右缘（缩放把手）留在视口内 8px」的较小者；下缘同理。
            // 窗比视口还宽/高时两头不能兼得，退到 0（贴左/贴上，把手优先可见）。
            const maxLeft = Math.max(0, Math.min(window.innerWidth - 80, window.innerWidth - r.width - 8));
            const maxTop = Math.max(0, Math.min(window.innerHeight - 44, window.innerHeight - r.height - 8));
            w.style.left = Math.min(Math.max(0, ev.clientX - dx), maxLeft) + "px";
            w.style.top = Math.min(Math.max(0, ev.clientY - dy), maxTop) + "px";
        };
        const up = (ev) => {
            if (ev && typeof ev.pointerId === "number" && ev.pointerId !== pid) return;
            head.removeEventListener("pointermove", move);
            head.removeEventListener("pointerup", up);
            head.removeEventListener("pointercancel", up);
        };
        head.addEventListener("pointermove", move);
        head.addEventListener("pointerup", up);
        head.addEventListener("pointercancel", up);
        e.preventDefault();
    });
    // 缩放（右下角把手）：指针捕获 + 按下时记偏移的真实拖拽几何（2026-08-04）——
    // 按下点距窗口右/下缘的偏移换算回「窗右/下缘位置」，宽高变化量严格等于指针位移量。
    if (rz) rz.addEventListener("pointerdown", (e) => {
        if (e.button !== 0) return;
        const r = w.getBoundingClientRect();
        const offX = e.clientX - r.right, offY = e.clientY - r.bottom;
        const pid = e.pointerId;
        try { rz.setPointerCapture(pid); } catch (_e) {}   // 捕获后指针滑出把手/窗口也照收 move/up
        const move = (ev) => {
            if (ev.pointerId !== pid) return;
            // 下限保住可读（360×260），上限不超出视口（右/下各留 8px）。
            w.style.width = Math.min(Math.max(360, ev.clientX - offX - r.left), window.innerWidth - r.left - 8) + "px";
            w.style.height = Math.min(Math.max(260, ev.clientY - offY - r.top), window.innerHeight - r.top - 8) + "px";
        };
        const up = (ev) => {
            if (ev && typeof ev.pointerId === "number" && ev.pointerId !== pid) return;
            rz.removeEventListener("pointermove", move);
            rz.removeEventListener("pointerup", up);
            rz.removeEventListener("pointercancel", up);
        };
        rz.addEventListener("pointermove", move);
        rz.addEventListener("pointerup", up);
        rz.addEventListener("pointercancel", up);
        e.preventDefault();
    });
    // 缩窗重钳：浮窗开着时视口变小，把它钳回屏内（关闭按钮恒可达）。
    window.addEventListener("resize", reclamp);

    return { open, close, toggle, isOpen, reclamp };
}

/* ---------- 我的库浮窗（#libWin：双页签 追踪 / 收藏） ----------
   渲染器注册反转：projects.js 注册 "tracks"，browse.js 注册 "favs"（fav_folders 保持其下级
   分组器、不直接注册，防后注册覆盖）。tab 状态存模块内存（默认 tracks、会话内记住上次），
   不落 localStorage；openLibWin("favs") 强制收藏页签。关窗计时器由工厂每窗一份。 */
let _libCtrl = null;
let _libTab = "tracks";
const _libRenderers = Object.create(null);
const _LIB_SUB = {
    tracks: "追踪的检索与收藏的数据集，只存在本地浏览器",
    favs: "收藏的数据集，只存在本地浏览器",
};

export function setLibRenderer(name, fn) { if (typeof fn === "function") _libRenderers[name] = fn; }
export function libWinOpen() { return _libCtrl ? _libCtrl.isOpen() : false; }

function _renderLibTab(tab) {
    const fn = _libRenderers[tab];
    if (typeof fn === "function") { try { fn(); } catch (_e) {} }
}

/* 切 tab：data-lib-active（CSS 按它显隐追踪详情的更新检查挂点）+ is-on/aria-selected
   + pane hidden + 头注，随后调该 tab 注册的渲染器。 */
export function setLibTab(name) {
    const tab = name === "favs" ? "favs" : "tracks";
    _libTab = tab;
    const w = $("libWin");
    if (!w) return;
    w.dataset.libActive = tab;
    const pairs = [["libTabTracks", "tracks"], ["libTabFavs", "favs"]];
    pairs.forEach(([id, key]) => {
        const b = $(id);
        if (!b) return;
        const on = key === tab;
        b.classList.toggle("is-on", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
    });
    const paneTracks = $("artifactsWinBody"), paneFavs = $("libPaneFavs");
    if (paneTracks) paneTracks.hidden = tab !== "tracks";
    if (paneFavs) paneFavs.hidden = tab !== "favs";
    const sub = $("libWinSub");
    if (sub) sub.textContent = _LIB_SUB[tab];
    _renderLibTab(tab);
}

export function openLibWin(tab) {
    if (!_libCtrl) return;
    if (_histCtrl && _histCtrl.isOpen()) _histCtrl.close();   // 两窗互斥：开一个自动关另一个（最简、可预测）
    if (tab) setLibTab(tab);   // 强制指定页签（追踪入口恒到 tracks；渲染随切页签完成）
    _libCtrl.open();
    if (!tab) _renderLibTab(_libTab);   // 未指定页签：开窗重渲当前页签
}
export function closeLibWin() { if (_libCtrl) _libCtrl.close(); }
export function toggleLibWin(tab) { if (libWinOpen()) closeLibWin(); else openLibWin(tab); }

/* 账户切换（accounts.js onAccountChanged）：开窗时重调当前页签渲染器按新命名空间重渲；没开不动。 */
export function libRefreshActive() { if (libWinOpen()) _renderLibTab(_libTab); }

export function initLibWin() {
    const w = $("libWin");
    if (!w) return;
    _libCtrl = initFloatingWin(w, { head: $("libWinHead"), resize: $("libWinResize"), close: $("libWinClose") });
    const tabTracks = $("libTabTracks"), tabFavs = $("libTabFavs");
    if (tabTracks) tabTracks.addEventListener("click", () => setLibTab("tracks"));
    if (tabFavs) tabFavs.addEventListener("click", () => setLibTab("favs"));
}

/* ---------- 历史记录浮窗（#histWin：单页签） ----------
   渲染器注册反转：browse.js 注册 renderHistory。骨架接线同工厂。 */
let _histCtrl = null;
let _histRenderer = null;
export function setHistRenderer(fn) { _histRenderer = typeof fn === "function" ? fn : null; }
export function histWinOpen() { return _histCtrl ? _histCtrl.isOpen() : false; }
function _renderHist() { if (typeof _histRenderer === "function") { try { _histRenderer(); } catch (_e) {} } }

export function openHistWin() {
    if (!_histCtrl) return;
    if (_libCtrl && _libCtrl.isOpen()) _libCtrl.close();   // 两窗互斥
    _histCtrl.open();
    _renderHist();
}
export function closeHistWin() { if (_histCtrl) _histCtrl.close(); }
export function toggleHistWin() { if (histWinOpen()) closeHistWin(); else openHistWin(); }
export function histRefreshActive() { if (histWinOpen()) _renderHist(); }

export function initHistWinSkeleton() {
    const w = $("histWin");
    if (!w) return;
    _histCtrl = initFloatingWin(w, { head: $("histWinHead"), resize: $("histWinResize"), close: $("histWinClose") });
}


/* ---------- settings ---------- */
// 通用 LLM 连接预设：底层统一走 OpenAI 兼容 /chat/completions。
// wire = 真正发给后端的 provider（后端认 mock / zhipuai / openai-compatible / trial）；
// 其余预设都映射成 openai-compatible，仅 base_url + model 不同 → 经安全校验的兼容端点即插即用。
// trial（限量试用，2026-08-25）：key/地址/模型全部由服务端托管并锁定（BIODATA_TRIAL_API_KEY；
// 2026-09-01 起默认模型改回 deepseek-v4-flash），
// base/model 留空不是「缺省」而是「不可填」——applyPreset 对 trial 禁用三个输入框。
const LLM_PRESETS = {
    mock: {
        wire: "mock", base: "", model: "",
        note: "无需密钥，也不会访问外部 AI 服务。",
    },
    trial: {
        wire: "trial", base: "", model: "",
        note: "限量试用通道：密钥与模型由本站托管并锁定，无需填写；每日对话轮数有限。",
    },
    deepseek: {
        // 预填非思考通道官方别名 deepseek-chat：AI 执行的工具调用走强制档（required），
        // 思考型模型（v4-flash/reasoner）拒收该档（400，2026-08-05 实测），速度也慢一倍。
        wire: "openai-compatible", base: "https://api.deepseek.com", model: "deepseek-chat",
        note: "已填入 DeepSeek 官方兼容地址与推荐模型，可按需修改。",
    },
    kimi: {
        wire: "openai-compatible", base: "https://api.moonshot.cn/v1", model: "kimi-k2.6",
        note: "已填入 Kimi 中国区官方兼容地址与通用模型，可按需修改。",
    },
    qwen: {
        wire: "openai-compatible", base: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus",
        note: "已填入阿里云百炼中国区兼容地址与 Qwen 通用模型，可按需修改。",
    },
    zhipuai: {
        wire: "zhipuai", base: "https://open.bigmodel.cn/api/paas/v4/", model: "glm-5.1",
        note: "已填入智谱通用 API 地址与 GLM 推荐模型，可按需修改。",
    },
    openrouter: {
        wire: "openai-compatible", base: "https://openrouter.ai/api/v1", model: "openrouter/auto",
        note: "已填入 OpenRouter 地址与自动路由模型；实际模型和费用由路由结果决定。",
    },
    openai: {
        wire: "openai-compatible", base: "https://api.openai.com/v1", model: "gpt-4o-mini",
        note: "已填入 OpenAI 官方地址与轻量模型，可按需修改。",
    },
    compatible: {
        wire: "openai-compatible", base: "", model: "",
        note: "填写任意 OpenAI Chat Completions 兼容地址和模型名称。",
    },
    local: {
        wire: "openai-compatible", base: "http://localhost:11434/v1", model: "llama3.1",
        note: "默认连接本机 Ollama 兼容接口；地址和模型均可修改。",
    },
};
const LLM_PRESET_LABELS = {
    mock: "本地演示",
    trial: "限量试用",
    deepseek: "DeepSeek",
    kimi: "Kimi",
    qwen: "Qwen",
    zhipuai: "GLM（智谱）",
    openrouter: "OpenRouter",
    openai: "OpenAI",
    compatible: "兼容接口",
    local: "本地模型",
};
const LEGACY_LLM_PRESETS = { glm: "zhipuai", gemini: "compatible", custom: "compatible", "openai-compatible": "compatible" };
let _settingsReturnFocus = null;
export function openSettings(opts) {
    const moveFocus = !(opts && opts.moveFocus === false);
    const wasOpen = $("settings").classList.contains("open");
    if (!wasOpen) _settingsReturnFocus = document.activeElement;
    $("settings").removeAttribute("inert");
    $("settings").setAttribute("aria-hidden", "false");
    $("settings").classList.add("open"); $("overlay").classList.add("on");
    // 刷新使用反馈卡片的「本机编号」行——编号可能在首次上传/导出后才生成，
    // 只在 init 时填一次会显示陈旧的「尚未产生数据」。
    benchfbSyncSettings();
    refreshTrialQuota();   // 试用额度卡同理：轮数随对话消耗，打开抽屉时取最新
    // 只在真正的「关闭→打开」过渡播放入场动画。新手教程 3/4/5 步会在抽屉已打开时反复调用 openSettings，
    // 若每次都重放 gsap.from(autoAlpha:0) 会让策略面板/节点闪一下白，并让焦点环测到动画中途的 y 偏移。
    if (MOTION && !wasOpen) gsap.from("#settings .pipe > .strategy-panel, #settings .pipe > .node", { autoAlpha: 0, y: 12, duration: 0.4, stagger: 0.035, ease: "power2.out", delay: 0.08 });
    if (moveFocus) requestAnimationFrame(() => $("settingsClose").focus());
}
export function closeSettings(opts) {
    $("settings").classList.remove("open"); $("overlay").classList.remove("on");
    $("settings").setAttribute("aria-hidden", "true");
    $("settings").setAttribute("inert", "");
    if (!(opts && opts.returnFocus === false) && _settingsReturnFocus && document.body.contains(_settingsReturnFocus)) _settingsReturnFocus.focus();
}
/* 2026-08-04 指路「直达」兑现：打开设置后把目标滚进视野并短暂高亮——
   此前降级气泡的「去开启 AI 执行」只打开抽屉，开关在屏外，照做被门控弹回（死路）。
   expand=true 用于 <details>（AI / API 配置默认收起，不展开滚进去也看不见内容）。 */
export function revealSetting(id, expand) {
    const el = $(id);
    if (!el) return;
    if (expand && el.tagName === "DETAILS") el.open = true;
    requestAnimationFrame(() => {
        if (el.scrollIntoView) el.scrollIntoView({ behavior: MOTION ? "smooth" : "auto", block: "center" });
        el.classList.add("setting-flash");
        setTimeout(() => el.classList.remove("setting-flash"), 1600);
    });
}
/* ---------- AI 能力门控与设置三维度（2026-08-03 设置体系重写） ----------
   三个独立维度：A 排序（规则恒开标识 ∥ 本地精准重排 ∥ AI 重排）、B 自动选择排序策略
   （开则隐藏 A 的手动项，按规则匹配结果自动决定重排层）、C AI 执行（对话指令理解与操作执行，
   合并旧「说了就直接做」+「Agent 规划执行」；大模型总开关同期退役——两道闸是「AI 重排点不动」
   那类困惑的根，一道 API 闸足够）。
   依赖 LLM 的开关（AI 重排 / 允许自动使用 AI / AI 润色 / AI 执行）统一由 API 可用性门控：
   未配 key（或本地演示）→ 禁点，点击弹「API 未配置」并展开 API 配置；已配 key → 必可开关；
   已开但 key 后来被撤 → 降级标注（值保留、不锁开关），功能如实暂停。 */
let _healthLlm = null;   // /api/health 的 llm_server 快照：{key_detected, provider, base_url}

function _llmWire() {
    const p = LLM_PRESETS[$("cfgProvider").value] || LLM_PRESETS.mock;
    return p.wire;
}

/* API 可用性的**唯一判据**：非 mock ∧（本次会话填了 key ∨（服务端有 key ∧ 接入方式与接口地址
   同服务端一致——改成自定义地址后服务端 key 不适用，与后端 _build_request_overrides 同约））。
   trial：无需 key——可用性 = 服务端报告试用通道已配置（health.llm_server.trial.available）。 */
export function llmCapable() {
    const wire = _llmWire();
    if (wire === "mock") return false;
    if (wire === "trial") return !!(_healthLlm && _healthLlm.trial && _healthLlm.trial.available);
    if (($("cfgApiKey").value || "").trim()) return true;
    const srv = _healthLlm;
    if (!srv || !srv.key_detected) return false;
    if (wire !== srv.provider) return false;
    // 护栏模式：请求永不携带 base_url（后端一律 400），地址视同「与服务端一致」。
    const base = webGuardOn() ? "" : ($("cfgBaseUrl").value || "").trim();
    return !base || base === srv.base_url;
}

// [开关 id, 行 id, 标注 id]——四个 LLM 依赖开关的注册表，syncAiGates 按它统一收口。
const _AI_GATED = [
    ["cfgRerank", "nodeRerank", "gateTagRerank"],
    ["cfgAutoLlm", "strategyDetail", "gateTagAutoLlm"],
    ["cfgPolish", "nodePolish", "gateTagPolish"],
    ["cfgAgentExec", "nodeAgentExec", "gateTagAgentExec"],
];

/* 禁点闸：未配 key 时点到 LLM 依赖开关 → 弹回并指路（值不留在开）；配了 key 必可开关。 */
export function aiGateChange(box) {
    if (box && box.checked && !llmCapable()) {
        box.checked = false;
        const wire = _llmWire();
        toast(wire === "mock"
            ? "本地演示模式不含大模型——到「AI / API 配置」选择服务商"
            : wire === "trial"
                ? "限量试用通道当前不可用（服务端未配置或已撤下）——可换用其他服务商并填入自己的密钥"
                : "API 未配置：到「AI / API 配置」填写密钥后即可开启");
        const api = $("apiConfig");
        if (api && !api.open) api.open = true;
    }
    syncAiGates();
}

export function syncAiGates() {
    const capable = llmCapable();
    _AI_GATED.forEach(function (ids) {
        const box = $(ids[0]), row = $(ids[1]), tag = $(ids[2]);
        if (!box || !row) return;
        const on = box.checked;
        row.classList.toggle("active", on);
        row.classList.toggle("ai-gated", !capable && !on);     // 未配 key 且没开：整行淡灰（禁点的视觉）
        row.classList.toggle("ai-degraded", !capable && on);   // 开过但 key 已撤：降级标注，值保留
        if (tag) {
            tag.hidden = capable;
            tag.textContent = on ? "API 未配置 · 已暂停" : "需要联网 AI（要密钥）";
        }
    });
    syncStrategyNode();   // B 维度显隐与 rerankDetail 可见性随之重算
}

/* langchain 扩展可用性 + 服务端 LLM 配置探测（启动一次）：
   health 快照写 _healthLlm（llmCapable 的输入之一）；扩展缺失只改 AI 执行的说明文案——
   后端自动回退基础规划，不锁开关（与「运行时不可用只降级标注」同一条语义）。 */
let _agentExtMissing = false;
export function agentExtMissing() { return _agentExtMissing; }
// 2026-08-26：整份 health 快照缓存（recall_api 在线状态等 additive 字段的消费口；
// 排序策略卡「已在线」判定用）。只在 /api/health 成功时更新。
let _healthSnapshot = null;
export function healthSnapshot() { return _healthSnapshot; }
/* health 快照到达钩子（2026-08-26；注册式反转——project_updates 的登录后
   自动刷新经此触发，shell 不反向 import 它）。快照每次成功写入后逐个调用；钩子异常不连累
   主流程。 */
const _healthArrivedHooks = [];
export function setHealthArrivedHook(fn) {
    if (typeof fn === "function" && !_healthArrivedHooks.includes(fn)) _healthArrivedHooks.push(fn);
}
function _fireHealthArrived() {
    _healthArrivedHooks.forEach(function (h) { try { h(); } catch (_e) {} });
}
/* 网页版公网护栏（2026-08-26）前端统一判定口：唯一真源 = /api/health 的
   account.required（与 accounts.js 的 _gate 同一份 health 快照语义）。快照未到/探测失败 →
   false（按本机形态处理，绝不误锁功能）。task_pack/act 等模块经此函数取判定，不自造第二真源。 */
export function webGuardOn() {
    return !!(_healthSnapshot && _healthSnapshot.account && _healthSnapshot.account.required);
}
/* 护栏模式设置面收口（health 快照到达后由 syncAgentAvailability 调用）：
   - 「记住api key」勾选禁用并强制不勾——网页版 key 只活内存，刷新即失；
   - 「接口地址」输入行隐藏并禁用（后端护栏模式对任何请求级 base_url 一律 400）；
   - provider 下拉的自定义地址类入口（兼容接口/本地模型）整组隐藏。 */
function _syncWebGuardUI() {
    const guard = webGuardOn();
    const rememberBox = $("cfgRememberApiKey");
    if (rememberBox) {
        rememberBox.disabled = guard;
        if (guard) rememberBox.checked = false;
        const rememberRow = rememberBox.closest(".set-row");
        if (rememberRow) rememberRow.style.display = guard ? "none" : "";
    }
    if (guard) {
        _setCfgRowHidden("cfgBaseUrl", true);
        const baseInput = $("cfgBaseUrl");
        if (baseInput) baseInput.disabled = true;
    }
    const sel = $("cfgProvider");
    if (sel) {
        Array.prototype.forEach.call(sel.querySelectorAll("option"), function (opt) {
            if (opt.value === "compatible" || opt.value === "local") opt.hidden = guard;
        });
        const customGroup = sel.querySelector('optgroup[label="高级接入"]');
        if (customGroup) customGroup.hidden = guard;
    }
}
export async function syncAgentAvailability() {
    try {
        const h = await (await fetch(API.health)).json();
        if (h && h.ok) { _healthSnapshot = h; _fireHealthArrived(); }
        if (h && h.llm_server) _healthLlm = h.llm_server;
        _agentExtMissing = !!(h && h.extensions && h.extensions.agent === false);
        // 零配置默认（2026-08-03）：用户从没存过设置、当前还是本地演示，而服务端已配好
        // 真实 key → 接入方式默认到与服务端一致的那个预设（服务端 key 直接适用，AI 开箱可用）。
        // 用户一旦自己选过/存过（LS.cfg 存在）就绝不替他改——存档优先。
        // 限量试用（2026-08-25）：服务端没配正式 key 但开了限量试用通道 → 默认到「限量试用」，
        // 新用户开箱即可对话（每日限轮）；正式 key 命中优先于试用。
        if (!readJSON(LS.cfg, null) && _healthLlm && _llmWire() === "mock") {
            let hit = null;
            if (_healthLlm.key_detected) {
                hit = Object.keys(LLM_PRESETS).find(function (k) {
                    const p = LLM_PRESETS[k];
                    return k !== "mock" && k !== "trial" && p.wire === _healthLlm.provider && p.base === _healthLlm.base_url;
                }) || null;
            }
            const trialHit = !hit && _healthLlm.trial && _healthLlm.trial.available;
            if (trialHit) hit = "trial";
            if (hit) {
                $("cfgProvider").value = hit;
                applyPreset(hit, { force: false });
                // 替用户改配置必须可见（2026-08-15）：「服务端有 key」这个条件用户看不到，
                // 静默改写预设就是隐式分支。如实 toast 一句；用户一旦自己存过设置（上面 LS.cfg 守卫）就到不了这里。
                toast(trialHit
                    ? "本站提供限量试用通道（每日限轮），已为你切到「限量试用」；正式使用请在设置里配置自己的密钥。"
                    : "检测到服务端已配好 AI 密钥，已为你把接入方式切到「" + (LLM_PRESET_LABELS[hit] || hit) + "」；在设置里可改。");
            }
        }
        const desc = $("agentExecDesc");
        if (desc) desc.textContent = _agentExtMissing
            ? "允许 AI 按您的话直接执行操作；每步都有记录，最近一次可一键撤回（未装 langchain 扩展，用基础规划）"
            : "允许 AI 按您的话直接执行操作；每步都有记录，最近一次可一键撤回";
        _syncWebGuardUI();   // 护栏模式设置面收口（health 快照刚更新，此刻判定最准）
    } catch (_e) {
        // fail-open：开关维持原状。但必须留痕（2026-08-15）——health 探测失败时
        // 「AI 执行」的实际可用性与界面显示可能不符，静默吞掉就无从排障。
        console.warn("syncAgentAvailability: /api/health 探测失败，AI 可用性维持界面原状", _e);
    }
    syncAiGates();
}

// 维度 B（自动选择排序策略）：开则隐藏维度 A 的手动项（.auto-owned 由 CSS 收口显隐）。
export function syncStrategyNode() {
    const on = $("cfgStrategy").checked;
    const control = $("rankingControl");
    if (control) control.classList.toggle("auto-owned", on);
    const hint = $("strategyModeHint");
    if (hint) hint.textContent = on
        ? "自动模式：规则排序始终启用，系统按每次查询的匹配情况自动决定是否叠加本地精准重排或 AI 重排。"
        : "手动模式：规则排序始终启用；可按需叠加本地精准重排或 AI 重排。";
    $("nodeRecall").classList.toggle("active", $("cfgRecall").checked && !on);
    $("nodeRerank").classList.toggle("active", $("cfgRerank").checked && !on);
    // AI 重排候选数：手动模式开重排时显示；自动模式常显，因为 auto 同样消费 rerank_top_n。
    const detail = $("rerankDetail");
    if (detail) detail.classList.toggle("off", !on && !$("cfgRerank").checked);
}
function syncApiConfigSummary() {
    const key = $("cfgProvider").value || "mock";
    const status = $("apiConfigStatus");
    if (status) status.textContent = LLM_PRESET_LABELS[key] || "兼容接口";
}

/* ---------- 可选本地模型在线安装 ---------- */
let _localModelPoll = null;
let _localModelState = null;

/* 字节人性化·本地模型安装专用口径（与 act_core.tpBytes 刻意不同，保留两份的理由）：
   ① 进度从 0 字节起报——0 必须显示「0 KB」，tpBytes 的 `!size → "未知"` 在进度态是错话；
   ② 数百 MB 的模型文件用整数 MB（toFixed(0)），一位小数全是噪音；KB 段同理取整。
   文件体积展示（任务包/导出）仍一律走 tpBytes，两处口径不混用。 */
function _modelBytes(value) {
    const n = Number(value) || 0;
    if (n < 1024 * 1024) return `${Math.max(0, Math.round(n / 1024))} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(0)} MB`;
    return `${(n / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function _renderLocalModelStatus(state) {
    _localModelState = state || { state: "idle" };
    const row = $("modelInstallRow"), title = $("modelInstallTitle"), note = $("modelInstallStatus");
    const install = $("modelInstallBtn"), cancel = $("modelCancelBtn"), progress = $("modelInstallProgress");
    if (!row || !title || !note || !install || !cancel || !progress) return;
    // 2026-08-26：服务端开了智谱 API 向量召回/重排（health.recall_api）时，
    // 网页版没有也不需要有「本地下载模型」这回事——直接如实显示「已在线」，藏起安装按钮。
    // recall_api 字段缺失（旧后端）或 embed=false 且本机形态 → 走下方原逻辑，逐字节不变。
    const recallApi = _healthSnapshot && _healthSnapshot.recall_api;
    if (recallApi && (recallApi.embed || recallApi.rerank)) {
        row.dataset.modelState = "online";
        title.textContent = "智能召回已在线";
        const parts = [];
        if (recallApi.embed) parts.push("向量召回");
        if (recallApi.rerank) parts.push("智能重排");
        note.textContent = parts.join("与") + "由服务器在线调用智谱模型提供，无需下载任何组件。";
        progress.hidden = true;
        cancel.hidden = true;
        install.hidden = true;
        if (_localModelPoll !== null) { clearTimeout(_localModelPoll); _localModelPoll = null; }
        return;
    }
    if (_healthSnapshot && _healthSnapshot.account && _healthSnapshot.account.required) {
        // 网页版（强制登录的多人部署）但未开在线向量服务：本机安装入口在服务器上没有意义，
        // 藏起按钮、如实标注，规则排序不受影响。
        row.dataset.modelState = "unavailable";
        title.textContent = "智能召回未启用";
        note.textContent = "服务器暂未开放在线向量服务；规则排序照常可用。";
        progress.hidden = true;
        cancel.hidden = true;
        install.hidden = true;
        if (_localModelPoll !== null) { clearTimeout(_localModelPoll); _localModelPoll = null; }
        return;
    }
    const kind = String(_localModelState.state || "idle");
    row.dataset.modelState = kind;
    const running = kind === "running";
    progress.hidden = !running;
    progress.setAttribute("aria-hidden", running ? "false" : "true");
    cancel.hidden = !running || !_localModelState.can_cancel;
    install.disabled = running;
    if (kind === "ready") {
        title.textContent = "本地模型已就绪";
        note.textContent = `运行组件 ${_modelBytes(_localModelState.runtime_bytes)} · 模型 ${_modelBytes(_localModelState.model_bytes)}；之后运行不联网。`;
        install.hidden = true;
    } else if (running) {
        title.textContent = "正在安装本地模型";
        note.textContent = _localModelState.message || "正在准备运行组件和模型权重…";
        install.hidden = false;
        install.textContent = "安装中";
    } else if (kind === "error" || kind === "cancelled") {
        title.textContent = kind === "cancelled" ? "本地模型安装已取消" : "本地模型暂未装好";
        note.textContent = (_localModelState.message || "可以稍后重试；基础检索不受影响。") + " 约下载 3.5 GB，安装后约占 4.5 GB。";
        install.hidden = false;
        install.textContent = "重试安装";
    } else {
        title.textContent = "本地模型未安装";
        note.textContent = "可在线安装；约下载 3.5 GB，安装后约占 4.5 GB。不装也能正常检索。";
        install.hidden = false;
        install.textContent = "在线安装";
    }
    if (running) {
        if (_localModelPoll !== null) clearTimeout(_localModelPoll);
        _localModelPoll = setTimeout(refreshLocalModelStatus, 1200);
    } else if (_localModelPoll !== null) {
        clearTimeout(_localModelPoll);
        _localModelPoll = null;
    }
}

export async function refreshLocalModelStatus() {
    // 「已在线」判定依赖 health 快照；启动顺序不保证 syncAgentAvailability 先跑完，
    // 快照缺失时这里补一次轻量探测（失败不阻塞，仍按本机口径渲染）。
    if (!_healthSnapshot) {
        try {
            const h = await (await fetch(API.health, { cache: "no-store" })).json();
            if (h && h.ok) { _healthSnapshot = h; _fireHealthArrived(); }
        } catch (_e) { /* 探测失败按本机口径渲染，与原行为一致 */ }
    }
    try {
        const response = await fetch(API.localModelStatus, { cache: "no-store" });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error("status_failed");
        _renderLocalModelStatus(data);
        return data;
    } catch (_error) {
        _renderLocalModelStatus({ state: "error", message: "暂时无法读取安装状态；基础检索不受影响。" });
        return null;
    }
}

async function _startLocalModelInstall() {
    if (!window.confirm("将联网下载约 3.5 GB（模型权重约 2.7 GB + 运行组件约 1 GB）。下载失败不影响基础检索，是否继续？")) return;
    _renderLocalModelStatus({ state: "running", message: "正在启动本地模型安装…", can_cancel: true });
    try {
        const response = await fetch(API.localModelInstall, { method: "POST" });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error("install_failed");
        _renderLocalModelStatus(data);
    } catch (_error) {
        _renderLocalModelStatus({ state: "error", message: "未能启动安装，请稍后重试；基础检索不受影响。" });
    }
}

async function _cancelLocalModelInstall() {
    try {
        const response = await fetch(API.localModelCancel, { method: "POST" });
        const data = await response.json();
        _renderLocalModelStatus(response.ok && data.ok ? data : { state: "error" });
    } catch (_error) {
        _renderLocalModelStatus({ state: "error", message: "取消请求没有完成，请稍后再看状态。" });
    }
}

export function initLocalModelControl() {
    const install = $("modelInstallBtn"), cancel = $("modelCancelBtn");
    if (install && !install.dataset.bound) {
        install.dataset.bound = "1";
        install.addEventListener("click", _startLocalModelInstall);
    }
    if (cancel && !cancel.dataset.bound) {
        cancel.dataset.bound = "1";
        cancel.addEventListener("click", _cancelLocalModelInstall);
    }
    refreshLocalModelStatus();
}

// 切 provider 时填入该预设的默认地址/模型。force=用户主动切换 → 覆盖；否则仅填空（不动用户已输入）。
// 兼容接口/本地演示的 base/model 可为空；本地演示不需要地址和模型输入。
// trial：地址/密钥两行**整行隐藏**（2026-08-25：锁定项不占地），模型框只读
// 上屏锁定模型名；额度卡（trialQuotaInfo）显示「今日剩余」；提示小字一并隐藏。
// 切走 trial 时三行恢复可填可见。隐藏用 inline display（.input 的 CSS display 会盖掉 hidden 属性）。
function _setCfgRowHidden(inputId, hide) {
    const input = $(inputId);
    const label = document.querySelector('label[for="' + inputId + '"]');
    if (input) input.style.display = hide ? "none" : "";
    if (label) label.style.display = hide ? "none" : "";
}
export function applyPreset(key, opts) {
    const p = LLM_PRESETS[key] || LLM_PRESETS.mock;
    const force = !!(opts && opts.force);
    if (force) {
        $("cfgBaseUrl").value = p.base;
        $("cfgModel").value = p.model;
    } else {
        if (p.base && !$("cfgBaseUrl").value.trim()) $("cfgBaseUrl").value = p.base;
        if (p.model && !$("cfgModel").value.trim()) $("cfgModel").value = p.model;
    }
    const isMock = key === "mock";
    const isTrial = key === "trial";
    const guard = webGuardOn();
    if (isTrial) {
        // 试用通道：残留的旧地址/密钥若留着会随请求发往后端（后端对 trial 也一律忽略，
        // 但界面上留着「看似生效」的输入是撒谎——清空并与锁定态一致）；
        // 模型框上屏锁定的模型名（health 未探测到时给已知缺省），如实展示「锁定不可改」。
        const t = _healthLlm && _healthLlm.trial;
        $("cfgBaseUrl").value = "";
        $("cfgApiKey").value = "";
        $("cfgModel").value = (t && t.model) || "deepseek-v4-flash";
    }
    _setCfgRowHidden("cfgBaseUrl", isTrial || guard);   // 护栏模式：自定义地址行整行隐藏（后端一律 400）
    _setCfgRowHidden("cfgApiKey", isTrial);
    $("cfgBaseUrl").disabled = isMock || isTrial || guard;
    $("cfgModel").disabled = isMock || isTrial;
    $("cfgApiKey").disabled = isTrial;
    const hint = $("providerPresetHint");
    if (hint) {
        if (isTrial) {
            // 试用态不显示提示小字（额度卡已承载全部有效信息）。
            hint.style.display = "none";
        } else {
            hint.style.display = "";
            const endpointKeyRule = isMock ? "" : " 自定义地址与服务器配置不同时，需填写密钥（仅本次会话使用）。";
            hint.textContent = p.note || "接口地址和模型均可修改；密钥默认不保存。";
            hint.textContent += endpointKeyRule;
        }
    }
    refreshTrialQuota();
    syncApiConfigSummary();
    syncAiGates();   // 接入方式变了 → API 可用性可能翻转，门控视觉随之重算
}

/* 试用额度卡（2026-08-25）：trial 预设 + 服务端通道可用 → 显示并拉取「今日剩余」。
   数据源 /api/account/trial-quota（护栏形态才有；本机形态 404 → 静默隐藏）。
   seq 防竞态：快速切预设时旧响应不得覆盖新状态。 */
let _trialQuotaSeq = 0;
export async function refreshTrialQuota() {
    const box = $("trialQuotaInfo");
    if (!box) return;
    const t = _healthLlm && _healthLlm.trial;
    if (_llmWire() !== "trial" || !(t && t.available)) { box.hidden = true; return; }
    const seq = ++_trialQuotaSeq;
    try {
        const r = await fetch(API.accountTrialQuota, { credentials: "same-origin" });
        if (seq !== _trialQuotaSeq) return;
        if (!r.ok) { box.hidden = true; return; }   // 401/404：未登录或本机形态——不显示
        const d = await r.json();
        if (seq !== _trialQuotaSeq) return;
        if (!d || d.available !== true) { box.hidden = true; return; }
        box.hidden = false;
        const num = $("trialQuotaNum"), fill = $("trialQuotaFill");
        if (d.unlimited === true || d.remaining === null || d.remaining === undefined) {
            if (num) num.textContent = "不限量";
            if (fill) { fill.style.width = "100%"; fill.classList.remove("low"); }
            return;
        }
        const total = Math.max(0, Number(d.daily_limit) || 0);
        const rem = Math.max(0, Number(d.remaining) || 0);
        if (num) num.textContent = rem + " / " + total + " 轮";
        if (fill) {
            fill.style.width = total > 0 ? Math.round(rem / total * 100) + "%" : "0%";
            fill.classList.toggle("low", total > 0 && rem <= Math.max(3, Math.ceil(total * 0.15)));
        }
    } catch (_e) { if (seq === _trialQuotaSeq) box.hidden = true; }
}
export function getConfig() {
    const key = $("cfgProvider").value || "mock";
    const preset = LLM_PRESETS[key] || LLM_PRESETS.mock;
    const provider = preset.wire;
    // 2026-08-03：大模型总开关退役。use_llm = mock 演示恒 true（演示输出靠它），
    // 真实 AI 由 API 可用性门控（llmCapable 单一判据）——已配 key 必可用，运行时不可用
    // 由后端如实降级（fallback_reason），不再由前端开关叠加第二道闸。
    const mockLlm = provider === "mock";
    const useLlm = mockLlm || llmCapable();
    const rerank = useLlm && $("cfgRerank").checked ? "llm" : "off";
    const recall = $("cfgRecall").checked ? "cross_encoder" : "off";
    const topK = clampInt($("cfgTopK").value, 10, 1, 50);
    const rerankTopN = clampInt($("cfgRerankTopN").value, 12, 1, 50);
    return {
        provider, preset: key, use_llm: useLlm, mock_llm: mockLlm,
        api_key: ($("cfgApiKey").value || "").trim(),
        // 护栏模式：请求绝不携带 base_url（后端对任何请求级自定义地址一律 400）；
        // 输入框里可能残留的预设/历史地址全部视为无效。
        base_url: webGuardOn() ? "" : ($("cfgBaseUrl").value || "").trim(),
        model: ($("cfgModel").value || "").trim(),
        rerank, recall, top_k: topK, rerank_top_n: rerankTopN,
        strategy: $("cfgStrategy").checked ? "auto" : "fixed",   // auto=分类器按候选压力和语义信息接管 recall/rerank
        // AI 润色推荐说明（只改说明文字，不动结果与排序；API 可用时生效）。
        polish: useLlm && $("cfgPolish").checked,
        // AI 执行（维度 C，2026-08-03 合并旧「说了就直接做」+「Agent 规划执行」）：
        // 开 → 所有消息过 LLM 分流（langgraph 优先、基础规划保底），执行类动词直接派发；
        // 关 → 一切输入按规则检索处理，操作句只回降级气泡。随 /api/utterance 请求带给后端。
        auto_act: $("cfgAgentExec") ? $("cfgAgentExec").checked : true,
        agent: $("cfgAgentExec") ? $("cfgAgentExec").checked : true,
        sources: getSelectedSources(),
        auto_parse_sources: getSourceMode() === "auto",
        auto_allow_llm: $("cfgAutoLlm") ? $("cfgAutoLlm").checked : false,
    };
}
export function loadConfig() {
    const c = readJSON(LS.cfg, null);
    if (!c) { applyPreset($("cfgProvider").value || "mock", { force: false }); syncAiGates(); return; }
    // 旧存档迁移（2026-08-03 三维度化）：
    // - 大模型总开关（useLlm）退役 → AI 能力由 API 可用性门控（llmCapable）；
    // - 「说了就直接做」(autoAct) +「Agent 规划执行」(agent) 合并为「AI 执行」(agentExec)——
    //   任一开过则开（用户显然要执行）；两个旧键都没有时沿用旧 agent 默认（开，未装扩展后端自动回退）。
    $("cfgPolish").checked = c.polish !== undefined ? !!c.polish : !!c.useLlm;
    $("cfgRerank").checked = !!c.rerank;
    $("cfgRecall").checked = !!c.recall;
    $("cfgStrategy").checked = !!c.strategy;
    if ($("cfgAutoLlm")) $("cfgAutoLlm").checked = !!c.autoAllowLlm;
    $("cfgAgentExec").checked = c.agentExec !== undefined ? !!c.agentExec
        : (c.agent !== undefined || c.autoAct !== undefined) ? !!(c.agent || c.autoAct) : true;
    // 兼容旧存档：曾经存在的品牌 preset 恢复为同名一键预设；协议名与未单列品牌落到兼容接口。
    let key = LEGACY_LLM_PRESETS[c.preset] || c.preset;
    if (!key && c.provider) key = (c.provider === "zhipuai" || c.provider === "mock") ? c.provider : "compatible";
    if (key && LLM_PRESETS[key]) $("cfgProvider").value = key;
    if (typeof c.baseUrl === "string") $("cfgBaseUrl").value = c.baseUrl;
    if (typeof c.model === "string") $("cfgModel").value = c.model;
    if (c.topK) $("cfgTopK").value = String(c.topK);
    if (c.rerankTopN) $("cfgRerankTopN").value = String(c.rerankTopN);
    $("cfgSaveSession").checked = !!c.save;
    // 护栏模式（网页版）：key 只活内存——绝不从 localStorage 恢复、也清掉历史遗留的已存 key。
    const rememberApiKey = c.save === true && c.rememberApiKey === true && !webGuardOn();
    $("cfgRememberApiKey").checked = rememberApiKey;
    if (rememberApiKey && typeof c.apiKey === "string") $("cfgApiKey").value = c.apiKey;
    // 旧存档迁移：旧版只有一个「记住设置」开关，不等于用户单独授权
    // 持久化密钥。缺 `rememberApiKey:true` 即从 localStorage 立即剔除旧 apiKey，
    // 其余非敏感配置原样保留。护栏模式同理剔除（网页版不持久化任何 key）。
    if (Object.prototype.hasOwnProperty.call(c, "apiKey") && (!rememberApiKey || webGuardOn())) {
        delete c.apiKey;
        c.rememberApiKey = false;
        writeJSON(LS.cfg, c);
    }
    applyPreset($("cfgProvider").value || "mock", { force: false });
    syncAiGates();
}
export function saveConfig() {
    const c = getConfig();
    if ($("cfgSaveSession").checked) {
        // 护栏模式（网页版）：强制不持久化 key——勾选框已被禁用/隐藏，这里再兜底一道。
        const rememberApiKey = !webGuardOn() && $("cfgRememberApiKey").checked === true;
        const stored = { polish: c.polish, preset: c.preset, baseUrl: c.base_url, model: c.model, rerank: c.rerank === "llm", recall: c.recall !== "off", strategy: c.strategy === "auto", autoAllowLlm: c.auto_allow_llm, agentExec: c.agent, topK: c.top_k, rerankTopN: c.rerank_top_n, save: true, rememberApiKey };
        if (rememberApiKey) stored.apiKey = c.api_key;
        writeJSON(LS.cfg, stored);
    } else {
        localStorage.removeItem(LS.cfg);
        $("cfgRememberApiKey").checked = false;
    }
    toast("设置已保存"); closeSettings();
}

/* ---------- recommend ---------- */
// false 默认中性色（「未启用/未使用」是状态、不是故障）；仅调用方显式传 tone:"no" 才染红（真失败）。
export function setKV(el, value, tone) {
    el.textContent = String(value ?? "-");
    el.classList.remove("ok", "no");
    const t = tone !== undefined ? tone : ((value === true || value === "success") ? "ok" : null);
    if (t === "ok" || t === "no") el.classList.add(t);
}
// 分类器决策回显：null=fixed（手动）；否则 auto · 档位 · 选中的 recall/rerank 后端
function fmtStrategy(s) {
    if (!s) return "手动";
    return `自动 · ${s.tier} · recall=${s.recall_backend} · rerank=${s.rerank_backend}`;
}
// 关键词审核决策回显（开发者信息）：null=未开；否则据 triggered/used/reason 摘要
function fmtAudit(a) {
    if (!a) return "-";
    if (!a.triggered) return `未触发（${a.reason || ""}）`;
    if (a.used) return `已改写重搜 · 「${a.rewritten_query}」（${a.n_before}→${a.n_after} 条）`;
    return (a.verdict === true ? "关键词完整，未改写" : "未采纳改写") + `（${a.reason || ""}）`;
}
// 结果区横幅：仅当**真的采纳了改写**时展示"我把问题理解成了 XX"，让 drift 对用户可见。
// 用 textContent 拼接（改写句来自 LLM，杜绝 XSS）。其余情形（未触发/关键词OK/改写更差退回）隐藏。
function renderAuditBanner(a) {
    const el = $("auditBanner");
    if (!el) return;
    if (a && a.used && a.rewritten_query) {
        el.textContent = "";
        const strong = document.createElement("b");
        strong.textContent = "「" + a.rewritten_query + "」";
        el.append("已按 ", strong, ` 重新检索（${a.n_before} → ${a.n_after} 条）。若不是你想找的，换个说法再试。`);
        el.hidden = false;
    } else {
        el.hidden = true;
        el.textContent = "";
    }
}
export function renderStatus(d) {
    setKV($("stPipeline"), d.pipeline || "-");
    setKV($("stProvider"), d.provider || "-");
    setKV($("stAttempted"), d.llm_attempted);
    // 「尝试过但失败」才是真故障（红）；未尝试时 false 只是「未启用」（中性，不染色）
    setKV($("stSucceeded"), d.llm_succeeded, (d.llm_attempted === true && d.llm_succeeded === false) ? "no" : undefined);
    setKV($("stUsed"), d.llm_response_used);
    setKV($("stFallback"), d.fallback_reason || "-");
    setKV($("stStrategy"), fmtStrategy(d.strategy));
    setKV($("stAudit"), fmtAudit(d.audit));
    renderAuditBanner(d.audit);
    $("rawMarkdown").textContent = d.markdown || "（无输出）";
}
