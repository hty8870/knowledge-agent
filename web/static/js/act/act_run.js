"use strict";

/* ============================================================================
 * act_run.js —— 执行行动流（Agent Run Experience）：对话式、分条的执行播报
 * ----------------------------------------------------------------------------
 * 用户点名的形态（2026-08-03 图2 指令）：执行类操作不再是结果区里一堵
 * 规则拼接的静态面板，而是**对话流里的行动条目流**——关键节点逐条上屏供监督，
 * 完成后收成一条总结（总结正文由 act.js 决定：LLM 生成、缺席时回退紧凑事实句）。
 *
 * 设计不变量：
 *   · **状态非日志**（与 board.js _cbProg 同哲学）：行动流不进 _cbLog、不落盘、
 *     不参与帧剪枝；它是"正在发生的执行"的投影，完成后折叠消失，只留下
 *     _cbLog 里那条总结 sys（执行过程折叠进总结泡的 <details>，见 arxTraceHtml）。
 *   · **条目粒度 = 真实步骤边界**（前端 runner 的顺序 await：plan / 联网查询 /
 *     apply 各是一条），绝不伪造流式进度——没有 SSE，后端端点全是单次阻塞，
 *     这里每一行都对应一件真发生了的事。agent 路径的规划步骤（plan.trace）同样
 *     不是前端编的：那是后端 langgraph 各节点实记、随单次响应带回。
 *   · 渲染收口在 board.js cbRenderHistory 的尾部（arxTailHtml），本文件只持有
 *     状态与 HTML 构造；重画经 _onChange 回调（act.js 注入），不反向 import board。
 *   · 动态值一律 escapeHtml；steps/detail 只允许纯文本。
 * ========================================================================== */
import { escapeHtml } from "#core";

let _run = null;      // {verbZh, steps:[{text, state, detail, pending}], collapsing, seq, t0, elapsedSec, finalLine}
let _seq = 0;
let _collapseTimer = null;
let _elapsedTimer = null;   // 头部实时秒表（500ms 一拍，只改那一颗 span，不整史重画）
let _onChange = null; // 重画回调（act.js 注入 board.js 的 cbRenderHistory；断环）

export function arxOnChange(fn) { _onChange = fn; }
function _emit() { if (_onChange) _onChange(); }

/* 设计 §2.5.4（claude code ≥2s 规则）：行动流头部实时秒表——<2s 不亮（防闪烁），
   到秒才进位。不走 _emit（整史重画太贵）：一拍只改头部 [data-arx-elapsed] 的文本；
   重画时该 span 由 arxTailHtml 按 _run.elapsedSec 重建，两通道同源。 */
function _stopElapsed() { if (_elapsedTimer) { clearInterval(_elapsedTimer); _elapsedTimer = null; } }
function _tickElapsed() {
    if (!_run || _run.collapsing || !_run.t0) { _stopElapsed(); return; }
    const sec = Math.floor((Date.now() - _run.t0) / 1000);
    if (sec < 2 || sec === _run.elapsedSec) return;
    _run.elapsedSec = sec;
    // node 规格环境（tests/js/act_run_spec.mjs）没有 document——经 globalThis 探测（不 typeof，
    // 拼错名会静默短路），缺席时只更状态，下次浏览器重画照样带出。
    const doc = globalThis.document;
    const el = doc && doc.querySelector("[data-arx-elapsed]");
    if (el) el.textContent = "· " + sec + "s";
}

/* 两个谓语分开（僵尸行动流）：arxFinish 后 _run 还留 420ms 做折叠动效，
   这期间它**能看见**（arxVisible，渲染侧用）但**已收口**（arxActive=false，派发侧用）——
   新派发见到 collapsing 的旧流必须 arxBegin 另开一条（bump seq 使旧折叠计时器失效），
   绝不往死流里追步：追进去的步骤要么张冠李戴进上一句的总结，要么被旧计时器整个判 null 静默吞掉。 */
export function arxActive() { return !!_run && !_run.collapsing; }
export function arxVisible() { return !!_run; }

/* 开一条执行播报。同一时刻至多一条（act.js 的 _actBusy 闸保证）。 */
export function arxBegin(verbZh) {
    if (_collapseTimer) { clearTimeout(_collapseTimer); _collapseTimer = null; }
    _stopElapsed();
    _run = { verbZh: String(verbZh || "执行"), steps: [], collapsing: false, seq: ++_seq,
        t0: Date.now(), elapsedSec: 0, finalLine: "" };
    _elapsedTimer = setInterval(_tickElapsed, 500);
    _emit();
    return _run.seq;
}

function _settleRunning(state) {
    if (!_run) return;
    _run.steps.forEach(function (s) { if (s.state === "running") s.state = state; });
}

/* 推进一格：之前的 running 条目收成 done，新增一条 running。
   同一句文案的连续重复视为同一格的更新（不新增行）——联网重试这类自报进度不刷屏。
   opts.state / opts.detail：plan.trace 的回放步与 SSE step 事件都是**已落定的真实节点
   记录**——失败节点必须带 failed（✗）与原因 detail 进流，与快照通道逐位一致；缺省维持
   running 级联（runner 自报进度照旧）。终态步不参与同文案去重（逐条是不同节点的记录，不合并）。 */
export function arxStep(text, opts) {
    if (!_run) return;
    const t = String(text || "").trim();
    if (!t) return;
    const state = opts && (opts.state === "failed" || opts.state === "done") ? opts.state : "running";
    const detail = opts && opts.detail ? String(opts.detail) : "";
    const pending = !!(opts && opts.pending);   // tool_start 先亮的行——等完成帧按 label 改行
    const last = _run.steps[_run.steps.length - 1];
    if (state === "running" && !detail && last && last.state === "running" && last.text === t) { _emit(); return; }
    _settleRunning("done");
    _run.steps.push({ text: t, state: state, detail: detail, pending: pending });
    _emit();
}

/* tool_start 先亮的 pending running 行（设计 §2.5.3），等它的完成帧按 label **改行**
   （落 done/failed + detail），不落新行。返回 false = 没有匹配的 pending 行（旧后端无
   tool_start 事件 / label 漂移）——调用方回落 arxStep 追加，行为与现状逐位一致。 */
export function arxSettlePending(text, opts) {
    if (!_run) return false;
    const t = String(text || "").trim();
    if (!t) return false;
    for (let i = _run.steps.length - 1; i >= 0; i--) {
        const s = _run.steps[i];
        if (s.pending && s.text === t) {
            s.pending = false;
            s.state = (opts && opts.ok === false) ? "failed" : "done";
            if (opts && opts.detail) s.detail = String(opts.detail);
            _emit();
            return true;
        }
    }
    return false;
}

/* 决策条目：需要用户给东西/拍板时调用（现只剩浏览器安全边界一种：import 的系统
   文件对话框）；办完 arxDecisionDone(echo) 收格并附回显（如「已选文件 xx.json」）。 */
export function arxDecision(text) {
    if (!_run) return;
    _settleRunning("done");
    _run.steps.push({ text: String(text || "等你选择").trim(), state: "decision" });
    _emit();
}
export function arxDecisionDone(echo) {
    if (!_run) return;
    _run.steps.forEach(function (s) {
        if (s.state === "decision") { s.state = "done"; if (echo) s.detail = String(echo); }
    });
    _emit();
}

/* 收尾：running → failed（ok=false）或保持到 finish 统一收。 */
export function arxFail() {
    if (!_run) return;
    _stopElapsed();   // 活已经停了，秒表不再走字
    _settleRunning("failed");
    _run.steps.forEach(function (s) { if (s.state === "decision") s.state = "failed"; });
    _emit();
}

/* 完成：步骤块做折叠动效（CSS grid-rows 过渡，REDUCE_MOTION 时直接消失），
   随后整块撤下——对话流里只留下总结 sys。返回步骤快照（给总结泡的 <details> 用）。
   失败/取消也走这里：失败条目已在对话流里如实亮过相（arxFail 置 failed 图标），
   总结 sys（失败句/取消句）落地时整块折叠撤，避免「失败行 + 失败句」双份。
   幂等：已在 collapsing 的流快照早就交出去了，再 finish 一次会重启折叠计时器、
   还把同一份快照交第二回（总结泡 details 双份）——直接返回 []。 */
export function arxFinish() {
    if (!_run || _run.collapsing) return [];
    _stopElapsed();
    // 收尾头部弱信息行（设计 §2.5.5，claude code "Done in Ns" 的等价物）——
    // 折叠动效那 420ms 里可见；步骤快照本身不带耗时（快照是过程记录，不是计时牌）。
    const _secs = _run.t0 ? Math.max(0, Math.round((Date.now() - _run.t0) / 1000)) : 0;
    _run.finalLine = "· 用时 " + _secs + "s · " + _run.steps.length + " 步";
    const trace = _run.steps.map(function (s) {
        return { text: s.text, state: s.state === "running" ? "done" : s.state, detail: s.detail || "" };
    });
    _settleRunning("done");
    _run.collapsing = true;
    _emit();
    const mySeq = _run.seq;
    _collapseTimer = setTimeout(function () {
        _collapseTimer = null;
        if (_run && _run.seq === mySeq) { _run = null; _emit(); }
    }, 420);   // 与 app.css --dur-slow 同长；reduced-motion 下过渡时长为 0，这只是撤块时机
    return trace;
}

/* 执行过程快照的 HTML（总结泡 <details> 用；arxFinish 返回的 trace）。 */
export function arxTraceHtml(trace) {
    if (!trace || !trace.length) return "";
    const rows = trace.map(function (s) {
        const mark = s.state === "failed" ? "✗" : "✓";
        const cls = s.state === "failed" ? " arx-trace-failed" : "";
        return '<div class="arx-trace-row' + cls + '"><span class="arx-trace-mark" aria-hidden="true">' + mark + "</span>"
            + "<span>" + escapeHtml(s.text) + "</span>"
            + (s.detail ? '<span class="arx-trace-detail">' + escapeHtml(s.detail) + "</span>" : "")
            + "</div>";
    }).join("");
    return '<details class="arx-trace"><summary>执行过程（' + trace.length + " 步）</summary>" + rows + "</details>";
}

/* 尾部渲染（board.js cbRenderHistory 拼接）。grp=true 时加 telegram 同方分组收紧类。 */
export function arxTailHtml(grp) {
    if (!_run) return "";
    const steps = _run.steps.map(function (s, i) {
        const isLast = i === _run.steps.length - 1;
        let icon = '<span class="arx-ico arx-ico-done" aria-hidden="true">✓</span>';
        if (s.state === "running") icon = '<span class="arx-ico arx-ico-run" aria-hidden="true"><i></i><i></i><i></i></span>';
        else if (s.state === "failed") icon = '<span class="arx-ico arx-ico-fail" aria-hidden="true">✗</span>';
        else if (s.state === "decision") icon = '<span class="arx-ico arx-ico-decide" aria-hidden="true">◆</span>';
        return '<div class="arx-step arx-' + s.state + (isLast ? " arx-last" : "") + '">'
            + icon + '<span class="arx-text">' + escapeHtml(s.text) + "</span>"
            + (s.detail ? '<span class="arx-detail">' + escapeHtml(s.detail) + "</span>" : "")
            + "</div>";
    }).join("");
    // 头部右侧——在跑时亮实时秒表（≥2s 才显示，_tickElapsed 直改文本；重画按
    // _run.elapsedSec 重建），折叠收尾时换成弱信息行「· 用时 Ns · N 步」。
    const _elapsed = _run.collapsing
        ? (_run.finalLine ? '<span class="arx-elapsed arx-elapsed-done">' + escapeHtml(_run.finalLine) + "</span>" : "")
        : '<span class="arx-elapsed" data-arx-elapsed>' + (_run.elapsedSec >= 2 ? "· " + _run.elapsedSec + "s" : "") + "</span>";
    return '<div class="cbh-turn cbh-sys arx-turn' + (grp ? " cbh-grp" : "") + '"><div class="arx-run' + (_run.collapsing ? " arx-collapsing" : "")
        + '" role="status" aria-label="正在执行：' + escapeHtml(_run.verbZh) + '">'
        + '<div class="arx-head"><span class="arx-head-ico" aria-hidden="true">▸</span>' + escapeHtml(_run.verbZh) + _elapsed + "</div>"
        + '<div class="arx-steps">' + steps + "</div>"
        + "</div></div>";
}
