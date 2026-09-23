"use strict";
/* 行动流（act_run.js）真行为规格。钉两条不变量：
    两通道一致：plan.trace 回放/流式上屏的每一步必须带 state+detail——
        失败节点（ok:false）渲染成 ✗ 且原因可进总结泡 details，与快照通道逐位同源；
    僵尸流：arxFinish 后的 420ms 折叠窗口内 arxActive() 必须为假——新派发 arxBegin
        另开新流（bump seq），旧折叠计时器不得把新流判 null（步骤静默丢失）。
   三门都不执行 JS，没有这份规格这两个不变量就没有回归网。 */

// act_run.js 经 #core 取 escapeHtml；core 顶层读 window.matchMedia/localStorage——
// 桩 window=globalThis 本物（浏览器里 window 即全局），桩完再动态 import。
globalThis.window = globalThis;
const _store = new Map();
globalThis.localStorage = {
    getItem: (k) => (_store.has(k) ? _store.get(k) : null),
    setItem: (k, v) => { _store.set(k, String(v)); },
    removeItem: (k) => { _store.delete(k); },
};

const ns = await import("../../web/static/js/act/act_run.js");
const { arxActive, arxVisible, arxBegin, arxStep, arxFinish, arxTraceHtml } = ns;

let checks = 0;
function ok(cond, what) {
    checks += 1;
    if (!cond) { console.error("FAIL: " + what); process.exit(1); }
}
function eq(actual, expected, what) {
    ok(JSON.stringify(actual) === JSON.stringify(expected),
        what + "  期望 " + JSON.stringify(expected) + " 实得 " + JSON.stringify(actual));
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------------- state/detail 随步进流、随快照出 ---------------- */
arxBegin("联网检索入库");
arxStep("Agent 规划 · 理解指令", { state: "done", detail: "识别为管护句" });
arxStep("修复计划", { state: "failed", detail: "第一次规划不合法" });
arxStep("执行工具", { state: "done", detail: "" });
const snap = arxFinish();
eq(snap, [
    { text: "Agent 规划 · 理解指令", state: "done", detail: "识别为管护句" },
    { text: "修复计划", state: "failed", detail: "第一次规划不合法" },
    { text: "执行工具", state: "done", detail: "" },
], " 快照必须带 state+detail（失败节点 ✗ + 原因）");
const html = arxTraceHtml(snap);
ok(html.indexOf("✗") >= 0 && html.indexOf("第一次规划不合法") >= 0, " 总结 details 里失败节点亮 ✗ 且带原因");
ok(html.indexOf("arx-trace-failed") >= 0, " 失败行带 failed 样式类");
await sleep(460);   // 等折叠撤下，下一段从零开始

/* runner 自报进度（无 opts）照旧 running 级联 + 同文案去重不被破坏 */
arxBegin("清点外部库");
arxStep("读取数据库状态");
arxStep("读取数据库状态");   // 同文案连续重复 = 同一格更新，不新增行
arxStep("读取数据库状态 2");
const snap2 = arxFinish();
eq(snap2.map((s) => s.text), ["读取数据库状态", "读取数据库状态 2"], "同文案去重只作用于 running 步");
ok(snap2.every((s) => s.state === "done"), "running 步收尾一律落 done");
await sleep(460);

/* ---------------- 420ms 折叠窗口内新派发恒开新流 ---------------- */
arxBegin("第一句");
arxStep("步骤甲", { state: "done", detail: "" });
arxFinish();
ok(arxVisible() && !arxActive(), "折叠窗口内：可见（余韵在）但已收口（不可追步）");
arxBegin("第二句");   // 窗口内新派发：另开新流（bump seq，旧折叠计时器当场失效）
arxStep("步骤乙", { state: "done", detail: "" });
await sleep(460);     // 越过旧流的折叠死线——旧计时器若误伤，新流会被判 null
ok(arxActive(), " 新流越过旧折叠死线后必须还活着（旧计时器不得误杀）");
const snap3 = arxFinish();
eq(snap3.map((s) => s.text), ["步骤乙"], " 第二句的步骤不夹带第一句（不张冠李戴）");
eq(arxFinish(), [], "arxFinish 幂等：collapsing 中的流不再交第二份快照");
await sleep(460);
ok(!arxActive() && !arxVisible(), "折叠结束整块撤下");

console.log("OK act_run_spec.mjs — " + checks + " checks");
