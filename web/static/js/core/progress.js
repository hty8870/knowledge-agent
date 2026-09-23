"use strict";

/* 本文件是 ES Module：core 的 $/REDUCE_MOTION 经 import 取；search/board/browse/interactions
   经 import 取进度函数与查询缓存（绞杀桥已全退役）。
   （animateConsoleWidth 在 2026-08-03 曾随「.console-bar width:0 排除法」退役；
   同日缺陷修复按「防跳三件套」复活：打字期间 pill 冻结不变（interactions.js onQueryInput 不再
   同步刷摘要），只有识别落地/用户改模式才更新摘要并走这里补宽度过渡——抖动源头在调用侧堵死，
   动画机器只负责让「一次性更新」平滑。） */
import { $, REDUCE_MOTION } from "#core";

/* 搜索框(.console)宽度随两枚筛选 pill 摘要变化（CSS fit-content）；此处给宽度变化补一段过渡动画。
   fit-content 是内在关键字、无法直接 transition，故用 FLIP：**先量此刻真实渲染宽作起点(from)**（含正在跑动画的中间值），
   再清 inline 宽让 CSS 量出新 fit-content 目标(to)，用显式 px 从 from 过渡到 to，结束后交回 fit-content。
   rAF 合并同帧内多次调用（updateSrcSummary/updateTimeSummary 常成对触发）。
   仅桌面生效——移动端 .console 是 width:auto 全宽，绝不给它写 inline 宽；reduced-motion 直接跳过（保持即时）。 */
let _cwRAF = 0, _lastConsoleW = null;
function _cwClearDone(con) { if (con._cwDone) { con.removeEventListener("transitionend", con._cwDone); con._cwDone = null; } }
export function animateConsoleWidth() {
    if (REDUCE_MOTION || window.innerWidth <= 780) return;
    if (_cwRAF) return;
    _cwRAF = requestAnimationFrame(() => {
        _cwRAF = 0;
        const con = document.querySelector(".console");
        if (!con) return;
        // 结果态+侧栏开时主检索框 display:none，getBoundingClientRect().width 恒 0。不特判就会把
        // _lastConsoleW 缓存成 0，等切回可见时从 0px 长到全宽、闪一下。
        // 这个守卫留作防御：隐藏时直接退出、**不**改 _lastConsoleW（留住上次真实宽），下次可见时起点=真实宽、无跳变。
        if (con.offsetParent === null) { _cwClearDone(con); con.style.width = ""; con.style.transition = ""; return; }
        // chat-in-main 态 console 是 width:100% 全宽（上限 760px），宽度不随 pill 内容变化——若照
        // 旧记忆宽(_lastConsoleW，首页量到的 fit-content 宽)播动画，会把全宽框短暂缩窄又弹回（实测
        // 提交后识别落地正好落在这个态，inline 宽 684→760 闪一下）。与隐藏态同处理：清 inline、
        // 记住此刻真实宽（供退回首页时作起点）、不播动画。
        if (con.closest(".chat-main-on")) { _cwClearDone(con); con.style.width = ""; con.style.transition = ""; _lastConsoleW = con.getBoundingClientRect().width; return; }
        if (window.innerWidth <= 780) { _cwClearDone(con); con.style.width = ""; con.style.transition = ""; _lastConsoleW = null; return; }
        // 起点 from：上一段动画还在跑（inline 宽存在）→ 取此刻真实插值宽（被打断时避免跳变）；
        // 否则摘要文字已改、fit-content 已重排，DOM 里已无旧宽 → 用记忆的上次显示宽 _lastConsoleW。
        const from = con.style.width ? con.getBoundingClientRect().width : _lastConsoleW;
        con.style.transition = "none"; con.style.width = "";                 // 回到 CSS fit-content 量新目标宽
        const to = con.getBoundingClientRect().width;
        _lastConsoleW = to;                                                  // 记住新显示宽，作下次起点
        if (from == null || Math.abs(to - from) < 1) { con.style.transition = ""; _cwClearDone(con); return; }   // 首帧 / 没变：不动
        // 摘掉上一段悬挂的收尾监听再挂新的：每次至多一个，杜绝堆积。**只听 transitionend**——
        // 被本次打断的旧过渡会派 transitioncancel，若也监听它，会误触发本次新过渡的 done 把新动画掐掉；
        // 被打断的旧监听已由这里的 _cwClearDone 显式摘除，无需再靠 transitioncancel 兜底。
        _cwClearDone(con);
        con.style.width = from + "px";
        void con.offsetWidth;                                                // 强制回流锚定起点
        con.style.transition = "width .28s cubic-bezier(.22,1,.36,1)";
        con.style.width = to + "px";
        const done = (e) => {
            if (e && e.propertyName !== "width") return;
            _cwClearDone(con);
            con.style.transition = ""; con.style.width = "";                 // 交回 fit-content，后续内容变化继续自适应
        };
        con._cwDone = done;
        con.addEventListener("transitionend", done);
    });
}
/* 检索进度（不确定态）：后端是单次阻塞调用、拿不到真实进度，此前用「恒速爬升到 96% 再补满」
   的百分比画像表达处理中；用户要求撤掉所有数字进度——现在只表达「处理中 / 已完成」：按钮 loading 静态态
   + 系统回复气泡里的三点跳动（#cbProgPct 那颗泡保留三点、数字列撤下；蜕变成回音后 #cbLivePct
   以同款三点继续滚，board.js 渲染侧按 progressActive() 决定挂不挂）。数字里程表机器
   （_setPct/_pctVal/_pctRate/rAF tick/收尾 timer）全部退役。 */
let _pctActive = false;       // 有检索在途。board.js 渲染侧据此把三点动画挂到当前系统泡右端

/* 是否有检索在途。board.js 渲染侧据此决定要不要在当前系统泡右端挂 #cbLivePct（三点）。 */
export function progressActive() { return _pctActive; }
/* 期望时长：纯前端拿不到后端真实进度，据「是否走 LLM」估一个量级。数字里程表退役后，
   该值不再参与动画时长（startProgress 只收下参数不动调用点），仅保留导出与调用约定。 */
export function estimateDuration(cfg) {
    if (cfg && cfg.mock_llm) return 1200;             // mock：无真实 API 往返，很快
    let ms = 1600;                                     // 纯向量/关键词召回：快
    if (cfg && cfg.use_llm) ms += 3000;                // 查询理解 LLM
    if (cfg && cfg.rerank)  ms += 6000;                // LLM 重排：最慢
    return ms;
}
export function startProgress(expectedMs) {
    const btn = $("submitBtn");
    if (!btn) return;
    // 幂等：统一框在路由问句前就已把按钮置 loading 并起跑；runRecommend 接手时不重启。
    // 不再逐帧翻数（数字已撤），动画交给 CSS 三点跳动，这里只管 loading 态与在途旗标。
    if (btn.classList.contains("loading")) return;
    btn.classList.add("loading");
    _pctActive = true;
    const chatSend = $("chatSendBtn");
    if (chatSend) chatSend.classList.add("loading");
}
export function finishProgress() {
    const btn = $("submitBtn");
    if (!btn) return;
    const chatSend = $("chatSendBtn");
    // 同步收尾（数字机器退役后没有补满尾巴）：摘 loading、清在途旗标、摘掉回音泡右端的三点。
    // 完成语义＝结果落地即收；取消语义走 resetSubmitButton（同步复位，两者不再有动画差异）。
    _pctActive = false;
    btn.classList.remove("loading");
    if (chatSend) chatSend.classList.remove("loading");
    const live = $("cbLivePct");
    if (live) live.remove();
}

/* 把「检索」按钮从 loading/disabled 态**同步**复位。

   为什么需要它、而不是靠 runRecommend 的 finally：finally 的守卫是「我是最新一代」
   （`myGen === _recSeq`）。分面芯片在请求在途时仍可点（有意保持响应），所以一旦有更晚的
   runRecommend 接管代号，**在途那一代的 finally 两个分支都进不去 → 收尾被整个跳过**。
   若接管者自己也不收尾（例如它命中了查询缓存、根本不走网络），按钮就永久停在 disabled + .loading。

   同步复位、不依赖动画收尾：隐藏标签页的 rAF 会被浏览器暂停，靠动画收尾会漏。
   （本函数是从 viewHistorySnapshot 里抽出来的——那里早就正确处理了这条路径。）

   **不借道 finishProgress**：那是「**完成**」语义；这里是「**取消**」语义——
   数字机器已退役，两者都同步收干净，区别只在语义标签与调用点约定。 */
export function resetSubmitButton() {
    const b = $("submitBtn");
    if (!b) return;
    _pctActive = false;
    const live = $("cbLivePct");
    if (live) live.remove();
    const chatSend = $("chatSendBtn");
    if (chatSend) chatSend.classList.remove("loading");
    b.disabled = false;
    b.removeAttribute("aria-busy");
    b.classList.remove("loading");
}

/* ---------- 查询结果缓存（会话内存，避免相同查询+设置重跑后端） ----------
   键＝决定后端结果的**全部**入参（检索 query、sources、日期、LLM/重排/召回配置…），**排除 api_key**
   （不影响结果、且不让密钥进键）。数组值排序、对象键排序 → 规范化，命中即秒出、不再走网络。
   纯前端、仅本会话内存（刷新即清）；官方评测走 sources=None 的后端直连、**从不经过此路径**，故确定性不受影响。 */
export const _queryCache = new Map();
export const QCACHE_MAX = 60;
