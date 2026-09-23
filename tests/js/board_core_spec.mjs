"use strict";
/* 条件板纯核的真行为规格。三门都不执行 JS，所以四分区归并与撤销栈的行为
   如果没有这份规格，就完全没有回归网。 */

import * as core from "../../web/static/js/panel/board_core.js";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
const { cbRowsFrom, cbSummary, cbPushFrame, cbGo, cbIsNoop, cbLabelForFilterId, CB_MAX_FRAMES } = core;

let checks = 0;
function ok(cond, what) {
    checks += 1;
    if (!cond) { console.error("FAIL: " + what); process.exit(1); }
}
function eq(actual, expected, what) {
    ok(JSON.stringify(actual) === JSON.stringify(expected),
       what + "  期望 " + JSON.stringify(expected) + " 实得 " + JSON.stringify(actual));
}

/* ---------------- 条件名还原 ---------------- */
eq(cbLabelForFilterId("include:species"), "物种", "正向物种");
eq(cbLabelForFilterId("exclude:disease"), "排除·疾病", "负向疾病要带排除前缀");
eq(cbLabelForFilterId("raw:required"), "原始数据", "原始数据");
eq(cbLabelForFilterId("raw:forbidden"), "原始数据", "原始数据（不要那一档）");
eq(cbLabelForFilterId("date:range"), "发表时间", "发表时间");
ok(cbLabelForFilterId("") === "", "空编号不崩且不编造名字");

/* ---------------- 四分区 ---------------- */
const ACTIVE = [
    { filter_id: "include:species", polarity: "include", dim: "species", label: "物种", values: ["Human"] },
    { filter_id: "include:tissue", polarity: "include", dim: "tissue", label: "组织", values: ["Lung"] },
    { filter_id: "raw:required", polarity: "include", dim: "has_raw_data", label: "原始数据", values: ["需要 FASTQ"] }
];
const rows = cbRowsFrom(ACTIVE, [{ dim: "species", value: "homo sapiens", display: "Homo sapiens" }],
                        ["disease"], ["include:platform"], ["tissue"]);
eq(rows.map(function (r) { return r.zone; }),
   ["query", "query", "query", "facet", "lenient", "suppressed"], "四分区顺序稳定");

const tissueRow = rows.filter(function (r) { return r.zone === "query" && r.dim === "tissue"; })[0];
ok(tissueRow.lenientable === true, "有覆盖缺口的那一项才可以放宽");
const speciesRow = rows.filter(function (r) { return r.zone === "query" && r.dim === "species"; })[0];
ok(speciesRow.lenientable === false, "没有缺口的项不显示放宽按钮");
ok(speciesRow.editable === true, "结构化正向项可以改");

const rawRow = rows.filter(function (r) { return r.dim === "has_raw_data"; })[0];
ok(rawRow.editable === false, "原始数据不是可以「换成别的值」的项");
ok(rawRow.lenientable === false, "原始数据不支持放宽");

const suppressedRow = rows.filter(function (r) { return r.zone === "suppressed"; })[0];
eq(suppressedRow.values, [], "已忽略的行不显示取值（那是可能过期的快照）");
eq(suppressedRow.label, "平台", "已忽略的行仍然要有中文条件名");

const negRows = cbRowsFrom([{ filter_id: "exclude:disease", polarity: "exclude", dim: "disease", label: "排除·疾病", values: ["Tumor"] }], [], [], [], []);
ok(negRows[0].editable === false, "负向条件不提供「换成」按钮");

/* ---------------- 软偏好：只排序、不筛选 ---------------- */
eq(cbLabelForFilterId("prefer:platform"), "优先·平台", "软偏好的条件名必须带「优先」");
eq(cbLabelForFilterId("prefer:raw"), "优先·原始数据", "prefer:raw 不能被还原成硬条件「原始数据」");
eq(cbLabelForFilterId("prefer:date"), "优先·发表时间", "prefer:date 不能被还原成硬条件「发表时间」");
eq(cbLabelForFilterId("prefer:source"), "优先·数据来源", "来源偏好也要带「优先」");

const PREF = [
    { filter_id: "include:tissue", polarity: "include", dim: "tissue", label: "组织", values: ["Lung"] },
    { filter_id: "prefer:platform", polarity: "prefer", dim: "platform", label: "优先·平台", values: ["Visium"] }
];
const prefRows = cbRowsFrom(PREF, [], [], [], []);
eq(prefRows.map(function (r) { return r.zone; }), ["query", "prefer"], "软偏好单独一个分区，不混进筛选条件");
ok(prefRows[1].editable === false, "软偏好不提供「换成」按钮");
ok(prefRows[1].removable === true, "软偏好可以单独停用");
/* 这是这个特性最核心的一句话：加一条「优先」，屏幕上的筛选条件数**一个都不能多**。
   多了就等于告诉用户「结果都满足这一条」，而其实一条数据都没被筛掉。 */
const filterCountIn = function (text) { return parseInt(/正在按 (\d+) 个/.exec(text)[1], 10); };
eq(filterCountIn(cbSummary(cbRowsFrom(PREF, [], [], [], []), 235)),
   filterCountIn(cbSummary(cbRowsFrom([PREF[0]], [], [], [], []), 235)), "加一条「优先」不会让筛选条件数变大");
ok(cbSummary(prefRows, 235).indexOf("只用来排先后，没有筛掉数据") > 0, "摘要必须点明它没有筛掉数据");

/* 停用一条排序偏好：结果集一条都不会变，所以不能说成「没有用它筛」 */
const prefOff = cbRowsFrom([PREF[0]], [], [], ["prefer:platform"], []);
eq(prefOff.map(function (r) { return r.zone; }), ["query", "prefer_off"], "被停用的偏好不混进「没有用它筛」");
eq(prefOff[1].label, "优先·平台", "停用后仍然看得出这是一条排序偏好");
ok(cbSummary(prefOff, 235).indexOf("排序偏好这次没有生效") > 0, "停用偏好要说成「没有生效」而不是「没有用它筛」");
ok(cbSummary(prefOff, 235).indexOf("没有用它筛") < 0, "停用偏好绝不能说成放宽了筛选");

eq(cbRowsFrom(null, null, null, null, null), [], "全空入参返回空表、不崩");
eq(cbRowsFrom([null, 3, "x"], [null], [], [], []), [], "非法项被安全丢弃");

/* 缺字段的老数据不能崩 */
const legacy = cbRowsFrom([{ dim: "species" }], [], [], [], []);
ok(legacy.length === 1 && legacy[0].label === "物种", "缺 filter_id 的老数据仍能还原条件名");
ok(legacy[0].removable === false, "没有编号就不该给「不按这条筛」按钮（点了也没用）");

/* ---------------- 摘要句 ---------------- */
eq(cbSummary(rows, 128), "正在按 4 个条件筛选，其中 1 项已放宽，另有 1 个这次没有用它筛 · 当前结果 128 条", "摘要三个数字都来自入参");
/* 放宽是给已有条件松绑，不是新加一条限制：松绑之后「正在按 N 个条件筛选」的 N 绝不许变大。 */
const beforeLenient = cbRowsFrom(ACTIVE, [], [], [], ["tissue"]);
const afterLenient = cbRowsFrom(ACTIVE, [], ["tissue"], [], ["tissue"]);
const nOf = function (text) { return parseInt(/正在按 (\d+) 个/.exec(text)[1], 10); };
ok(nOf(cbSummary(afterLenient, 9)) <= nOf(cbSummary(beforeLenient, 9)),
   "点了「放宽」之后，条件计数不会反而变多");
ok(cbSummary(afterLenient, 9).indexOf("其中 1 项已放宽") > 0, "放宽单独报，且报成「其中」");
ok(cbSummary(rows, 128).indexOf("库中") < 0, "绝不写「库中」——这个数是套过再缩小之后的存活数");
eq(cbSummary([], null), "当前没有任何筛选条件", "没有条件时不说「正在按 0 个条件筛选」");
eq(cbSummary([{ zone: "query" }], 0), "正在按 1 个条件筛选 · 当前结果 0 条", "0 条也要如实说");

/* ---------------- 撤销栈 ---------------- */
let stack = [], cursor = -1;
// 推 CB_MAX_FRAMES+5 帧：栈必须收在上限、丢最旧 5 帧（上限值由常量给出，规格不硬写数字）。
for (let i = 0; i < CB_MAX_FRAMES + 5; i += 1) {
    const next = cbPushFrame(stack, cursor, { query: "q" + i });
    stack = next.stack; cursor = next.cursor;
}
ok(stack.length === CB_MAX_FRAMES, "栈满之后不再增长，实得 " + stack.length);
ok(cursor === stack.length - 1, "丢最旧那一帧之后游标必须跟着回来，不能越界一格");
eq(stack[stack.length - 1].query, "q" + (CB_MAX_FRAMES + 4), "栈顶是最新一帧");
eq(stack[0].query, "q5", "丢掉的是最旧的那些");

const back1 = cbGo(stack, cursor, -1);
eq(back1.frame.query, "q" + (CB_MAX_FRAMES + 3), "上一步是倒数第二帧");
const back2 = cbGo(stack, back1.cursor, -1);
eq(back2.frame.query, "q" + (CB_MAX_FRAMES + 2), "连续两次上一步落到两个不同的帧");
ok(back1.frame.query !== back2.frame.query, "两次上一步不能原地不动");

const clampLow = cbGo(stack, 0, -1);
ok(clampLow.cursor === 0, "已经是第一步时夹住不动");
const clampHigh = cbGo(stack, stack.length - 1, 1);
ok(clampHigh.cursor === stack.length - 1, "已经是最后一步时夹住不动");
const empty = cbGo([], -1, -1);
ok(empty.cursor === -1 && empty.frame === null, "空栈是 no-op");

/* 在游标不在栈顶时推帧，会丢掉后面被放弃的分支 */
let branched = cbPushFrame(["a", "b", "c"], 0, "d");
eq(branched.stack, ["a", "d"], "从中间继续改会丢掉后面那些步");
ok(branched.cursor === 1, "游标停在新推的那一帧上");

/* 入参不被就地改动 */
const original = ["a", "b"];
cbPushFrame(original, 1, "c");
eq(original, ["a", "b"], "推帧不改调用方的数组");

/* ---------------- 有没有真的改到 ---------------- */
const base = { query: "人类肺", suppressed_constraints: [], lenient_dims: [], facet_filters: [], date_from: "", date_to: "" };
ok(cbIsNoop(base, JSON.parse(JSON.stringify(base))) === true, "一模一样就是没改");
ok(cbIsNoop(base, Object.assign({}, base, { query: "小鼠肺" })) === false, "换了句子就是改了");
ok(cbIsNoop(base, Object.assign({}, base, { lenient_dims: ["tissue"] })) === false, "放宽了也是改了");
ok(cbIsNoop(null, base) === false, "缺一边时不敢判「没改」");

/* ---------------- 逐条反馈操作条（msgfb，2026-08-28） ---------------- */
const { cbMsgFbNext, cbMsgCommentText, cbMsgForkable } = core;

/* 赞/倒赞互斥三态机 */
eq(cbMsgFbNext("", "up"), "up", "空态点赞 = 赞");
eq(cbMsgFbNext("up", "up"), "", "再点已选中的赞 = 取消");
eq(cbMsgFbNext("up", "down"), "down", "赞上点倒赞 = 换边");
eq(cbMsgFbNext("", "down"), "down", "空态点倒赞 = 倒赞");
eq(cbMsgFbNext("down", "down"), "", "再点已选中的倒赞 = 取消");
eq(cbMsgFbNext("down", "up"), "up", "倒赞上点赞 = 换边");
eq(cbMsgFbNext("bogus", "up"), "up", "脏初态按空态处理");
eq(cbMsgFbNext("up", "bogus"), "up", "脏动作不改变现状");

/* 评论入队文本：正文 + 引用尾（mid + 摘段） */
const cmt = cbMsgCommentText("这句很有用", "m7", "检索完成：库中共 36 条匹配");
ok(cmt.indexOf("这句很有用") === 0, "评论正文在前");
ok(cmt.indexOf("m7") > 0 && cmt.indexOf("检索完成") > 0, "引用尾带 mid 与摘段");
const longSnip = "一二三四五六七八九十".repeat(10);
const cmt2 = cbMsgCommentText("x", "m1", longSnip);
ok(cmt2.indexOf("…") > 0, "超长摘段截断加省略号");
ok(cmt2.length < longSnip.length + 40, "截断确实变短");
ok(cbMsgCommentText("x", "m1", "").indexOf("（原文不在本地）") > 0, "空摘段如实标注");
ok(cbMsgCommentText("x", "m1", "a\n\nb").indexOf("a b") > 0, "摘段压平换行");

/* 分支点判据 */
ok(cbMsgForkable(3, true) === true, "挂在存活帧上的回复可分支");
ok(cbMsgForkable(null, false) === false, "无帧回复不可分支");
ok(cbMsgForkable(3, false) === false, "帧已不在栈里不可分支");

/* ---------------- 纯度 ---------------- */
const src = readFileSync(fileURLToPath(new URL("../../web/static/js/panel/board_core.js", import.meta.url)), "utf8");
["localStorage", "sessionStorage", "document", "fetch(", "Date.now(", "new Date", "performance.now"]
    .forEach(function (bad) {
        ok(src.indexOf(bad) < 0, "纯核里不该出现 " + bad);
    });

console.log("BOARD_CORE_SPEC_OK  断言 " + checks + " 条");
