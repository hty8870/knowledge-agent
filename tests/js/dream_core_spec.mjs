"use strict";

/* ============================================================================
 * dream_core_spec.js —— dream 记忆纯核「真行为」规格（node 跑）
 * ----------------------------------------------------------------------------
 * 由 tests/test_dream_core_behavior.py 经 `node <this>` 驱动；断言失败 → 非零退出。
 * 素材组织（convId 取最后一轮累计 chat）、字符预算裁剪、与既有记忆去重——
 * 这三件事错了，dream 就是「发了不该发的 / 重复打扰 / 超 token」，静态门测不出来。
 * ========================================================================== */

import * as D from "../../web/static/js/panel/dream_core.js";

let failures = 0;
function check(name, cond, detail) {
    if (cond) { console.log(`  ok   ${name}`); }
    else { failures++; console.log(`  FAIL ${name}${detail ? "  —— " + detail : ""}`); }
}

/* ---- dreamCollectConversations：同 convId 只留最后一轮（累计 chat 最完整） ---- */
const hist = [
    { convId: "c1", query: "人类乳腺癌", at: 100, chat: [{ k: "say", t: "人类乳腺癌", n: "" }] },
    { convId: "c1", query: "人类乳腺癌", at: 200, chat: [{ k: "say", t: "人类乳腺癌", n: "" }, { k: "refine", t: "换成小鼠", n: "" }] },
    { convId: "c2", query: "肺组织", at: 300, chat: [{ k: "say", t: "肺组织", n: "" }] },
    { convId: "", query: "", at: 0, chat: [] },                    // 空行：不进素材
    { convId: "c3", query: "  ", at: 50, chat: [{ k: "say", t: " ", n: "" }] },  // 全空白：不进
];
const convs = D.dreamCollectConversations(hist, 12);
check("同 convId 只留最后一轮", convs.length === 2, JSON.stringify(convs.map(c => [c.query, c.chat.length])));
check("累计 chat 带完整对话", convs.find(c => c.query === "人类乳腺癌").chat.length === 2);
check("新→旧排序", convs[0].query === "肺组织");
check("白名单字段（不带 at/snap）", !("at" in convs[0]) && !("snap" in convs[0]));

/* ---- 仅对话历史行：chatOnly 标记不破坏消费口径 ----
   仅对话行（snap:null、query=首句用户消息、chat 完整累计）与普通行同走
   「同 convId 最后一行」归并，对话内容完整进入素材。 */
const histChatOnly = [
    { convId: "c9", query: "检查10x数据库是否有更新", at: 400, snap: null, chatOnly: true,
      chat: [{ k: "say", t: "检查10x数据库是否有更新", n: "" }, { k: "sys", t: "已检查：没有更新", n: "" }] },
    { convId: "c8", query: "人类乳腺癌", at: 300, chat: [{ k: "say", t: "人类乳腺癌", n: "" }] },
];
const convsChatOnly = D.dreamCollectConversations(histChatOnly, 12);
check("仅对话行按原口径进素材", convsChatOnly.length === 2, JSON.stringify(convsChatOnly));
check("仅对话行 chat 完整（含 sys）", convsChatOnly[0].chat.length === 2 && convsChatOnly[0].chat[1].k === "sys");
check("仅对话行 query=首句用户消息", convsChatOnly[0].query === "检查10x数据库是否有更新");

/* ---- 段数上限 ---- */
const many = Array.from({ length: 30 }, (_, i) => ({ convId: "c" + i, query: "q" + i, at: i, chat: [{ k: "say", t: "t", n: "" }] }));
check("最多 DREAM_MAX_CONV 段", D.dreamCollectConversations(many, D.DREAM_MAX_CONV).length === D.DREAM_MAX_CONV);
check("最新优先保留", D.dreamCollectConversations(many, 3).map(c => c.query).join(",") === "q29,q28,q27");

/* ---- dreamClipConversations：字符预算内保留最新消息 ---- */
const longChat = Array.from({ length: 40 }, (_, i) => ({ k: "say", t: "消息" + i + "x".repeat(100), n: "" }));
const clipped = D.dreamClipConversations([{ query: "q", chat: longChat }]);
const totalChars = clipped[0].chat.reduce((s, m) => s + m.t.length + m.n.length, 0);
check("预算内截断", totalChars <= D.DREAM_CONV_CHAR_MAX, "total=" + totalChars);
check("保留的是最新的消息", clipped[0].chat[clipped[0].chat.length - 1].t.indexOf("消息39") === 0);
check("query 限长", D.dreamClipConversations([{ query: "q".repeat(1000), chat: [] }])[0].query.length === 300);

/* ---- dreamFilterNew：与既有记忆 + 候选内部去重 ---- */
const existing = [{ text: "只要人类数据" }, { text: "需要 FASTQ" }];
const cands = [
    { text: "只要人类数据", summary: "s" },      // 与既有重复 → 丢
    { text: "  只要 人类数据 ", summary: "s" },   // 归一后同文 → 丢
    { text: "偏好空间转录组", summary: "s" },
    { text: "偏好空间转录组", summary: "s2" },    // 候选内部重复 → 丢
    { text: "", summary: "空" },                  // 空文 → 丢
];
const fresh = D.dreamFilterNew(cands, existing);
check("去重后只剩 1 条", fresh.length === 1 && fresh[0].text === "偏好空间转录组", JSON.stringify(fresh));

/* ---- 近重复判重：助词剥离判重键 + CJK bi-gram Jaccard ≥ 0.75 ----
   边界钉：助词级微调与长句重排该判重；换物种/换平台/短句单字符扩展绝不该判重
   （「肺部 vs 肺」是短句单字符差异，bigram 分数与「脑 vs 肺」撞车不可分——
   这种 miss 宁可放过（呈现为新候选），不许误伤）。 */
const nearExisting = [{ text: "只要人类肺数据" }, { text: "总要 FASTQ 的人类数据" }, { text: "偏好 Visium 平台的数据" }];
const nearCands = [
    { text: "只要人类的肺数据" },      // 助词「的」剥离后同键 → 丢
    { text: "人类数据总要 FASTQ" },    // 长句重排（Jaccard 0.91）→ 丢
    { text: "只要人类肺部数据" },      // 短句单字符扩展：miss 放行（宁可呈现，不许误伤）→ 留
    { text: "只要小鼠肺数据" },        // 换物种 → 绝不判重，留
    { text: "偏好 ATAC 平台的数据" },  // 换平台 → 绝不判重，留
];
const nearFresh = D.dreamFilterNew(nearCands, nearExisting);
check("近重复判重后剩 3 条", nearFresh.length === 3, JSON.stringify(nearFresh.map(c => c.text)));
check("助词级微调被拦", !nearFresh.some(c => c.text === "只要人类的肺数据"));
check("长句重排被拦", !nearFresh.some(c => c.text === "人类数据总要 FASTQ"));
check("短句扩展放行（不误伤）", nearFresh.some(c => c.text === "只要人类肺部数据"));
check("换物种不判重", nearFresh.some(c => c.text === "只要小鼠肺数据"));
check("换平台不判重", nearFresh.some(c => c.text === "偏好 ATAC 平台的数据"));

/* ---- dreamNormalize 与 memRank 口径一致（折叠空白 + 小写） ---- */
check("归一规则", D.dreamNormalize("  A  B\tC ") === "a b c");

console.log(failures === 0 ? "\nDREAM_CORE_SPEC_OK" : `\nDREAM_CORE_SPEC_FAILED (${failures})`);
process.exit(failures === 0 ? 0 : 1);
