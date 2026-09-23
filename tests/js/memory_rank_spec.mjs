"use strict";

/* ============================================================================
 * memory_rank_spec.js —— 用户记忆纯排序核「真行为」规格（node 跑）
 * ----------------------------------------------------------------------------
 * 由 tests/test_memory_rank_behavior.py 经 `node <this>` 驱动；断言失败 → 非零退出。
 * 存在意义：web_smoke 只静态查字符串、node --check 只验语法，两门都测不出**排序结果对不对**。
 * 这里对纯核 memRank* 下确定性行为断言：CJK bi-gram 命中、IDF 区分度、useCount 强化、
 * 时间衰减、偏好分层（不衰减 + tier 加成）、偏好防淘汰。相对路径 import，避开中文路径入 argv。
 * ========================================================================== */

import * as M from "../../web/static/js/panel/memory_rank.js";

const DAY = 86400000;
const NOW = 1700000000000;   // 固定基准，确定性（脚本内禁用 Date.now）

let failures = 0;
function check(name, cond, detail) {
    if (cond) { console.log(`  ok   ${name}`); }
    else { failures++; console.log(`  FAIL ${name}${detail ? "  —— " + detail : ""}`); }
}

let _seq = 0;
function mk(kind, text, opts) {
    opts = opts || {};
    _seq += 1;
    return {
        id: opts.id || `id-${_seq}`,
        kind,
        text,
        summary: opts.summary || "",
        createdAt: opts.createdAt || NOW,
        updatedAt: opts.updatedAt || NOW,
        lastUsedAt: opts.lastUsedAt != null ? opts.lastUsedAt : NOW,
        useCount: opts.useCount || 0,
    };
}
const idOf = (arr) => arr.map((x) => x.id);

/* 1) CJK bi-gram：查询「人类肺癌」应在「肺癌」bigram 上命中记忆「肺癌单细胞图谱」，且排在无关记忆前。 */
(function () {
    const hit = mk("search", "肺癌单细胞图谱", { id: "hit" });
    const miss = mk("search", "小鼠脑组织切片", { id: "miss" });
    const stats = M.memRankCorpusStats([hit, miss]);
    // 相对量断言（非 tautology）：同一记忆上，命中 bigram 的查询得分 > 无关查询得分——证明 bigram 真贡献相关性。
    check("bigram-cjk 命中查询得分 > 无关查询得分", M.memRankScore(hit, "人类肺癌", stats, NOW) > M.memRankScore(hit, "斑马鱼胚胎", stats, NOW));
    check("bigram-cjk 无关记忆得分 < 命中记忆（仅 recency）", M.memRankScore(miss, "人类肺癌", stats, NOW) < M.memRankScore(hit, "人类肺癌", stats, NOW));
    check("bigram-cjk 相关排在无关前", idOf(M.memRankOrder([miss, hit], "人类肺癌", NOW))[0] === "hit");
})();

/* 2) IDF 区分度：命中「稀有」token 的记忆应压过命中「泛」token 的记忆（其余条件相等）。 */
(function () {
    const filler = [];
    for (let i = 0; i < 6; i++) filler.push(mk("note", "common", { id: `f${i}` }));
    const A = mk("note", "common", { id: "A-common" });   // 命中泛 token
    const B = mk("note", "rare", { id: "B-rare" });         // 命中稀有 token
    const items = [...filler, A, B];
    const stats = M.memRankCorpusStats(items);
    check("idf 稀有>泛（打分）", M.memRankScore(B, "common rare", stats, NOW) > M.memRankScore(A, "common rare", stats, NOW),
        `sB=${M.memRankScore(B, "common rare", stats, NOW).toFixed(3)} sA=${M.memRankScore(A, "common rare", stats, NOW).toFixed(3)}`);
    check("idf 稀有记忆排第一", idOf(M.memRankOrder(items, "common rare", NOW))[0] === "B-rare");
})();

/* 3) useCount 强化：相关度/时间/层级全等，useCount 高者靠前。 */
(function () {
    const lo = mk("note", "alpha", { id: "use-lo", useCount: 0 });
    const hi = mk("note", "alpha", { id: "use-hi", useCount: 10 });
    check("usecount 高者靠前", idOf(M.memRankOrder([lo, hi], "alpha", NOW))[0] === "use-hi");
})();

/* 4) 时间衰减（工作层）：相关度/useCount 相等，最近使用者靠前。 */
(function () {
    const recent = mk("search", "alpha", { id: "t-recent", lastUsedAt: NOW });
    const stale = mk("search", "alpha", { id: "t-stale", lastUsedAt: NOW - 60 * DAY });
    check("decay 最近者靠前", idOf(M.memRankOrder([stale, recent], "alpha", NOW))[0] === "t-recent");
    check("decay recency∈(0,1] 且随年龄下降", M.memRankRecency(stale, NOW) < M.memRankRecency(recent, NOW) && M.memRankRecency(recent, NOW) <= 1);
})();

/* 5) 偏好分层：偏好不衰减 + tier 加成——同样陈旧时，偏好压过工作记忆（空查询下按层级/衰减排）。 */
(function () {
    const prefOld = mk("note", "任意偏好", { id: "pref-old", lastUsedAt: NOW - 100 * DAY });
    const workOld = mk("search", "任意需求", { id: "work-old", lastUsedAt: NOW - 100 * DAY });
    check("layer 偏好 recency 恒为特权（不衰减，打分用 1）", M.memRankScore(prefOld, "", M.memRankCorpusStats([prefOld, workOld]), NOW) > M.memRankScore(workOld, "", M.memRankCorpusStats([prefOld, workOld]), NOW));
    check("layer 空查询下陈旧偏好排在陈旧工作记忆前", idOf(M.memRankOrder([workOld, prefOld], "", NOW))[0] === "pref-old");
})();

/* 6) 淘汰：limit 只封顶工作层，偏好绝不自动淘汰；工作层超限只清最旧、保序。 */
(function () {
    const pref = mk("note", "研究偏好陈旧", { id: "P", lastUsedAt: NOW - 100 * DAY });
    const works = [];
    for (let k = 1; k <= 5; k++) works.push(mk("search", `需求${k}`, { id: `W${k}`, lastUsedAt: NOW - k * DAY }));
    const items = [pref, ...works];   // W1 最新 … W5 最旧
    const survivors = M.memRankEvict(items, 3);
    const sids = idOf(survivors);
    check("evict 偏好绝不被淘汰（即便最旧）", sids.includes("P"));
    check("evict 工作层封顶到 limit=3", sids.filter((s) => s[0] === "W").length === 3);
    check("evict 保留最新三条工作记忆", sids.includes("W1") && sids.includes("W2") && sids.includes("W3"));
    check("evict 淘汰最旧工作记忆", !sids.includes("W4") && !sids.includes("W5"));
    check("evict 幸存者保持原始相对顺序", sids.join(",") === "P,W1,W2,W3");
    check("evict 不改入参", items.length === 6);
})();

/* 6b) RANK-1 修复：偏好数超 limit 时偏好**全部**保留（持久层 literal 保证，0 静默丢失）。 */
(function () {
    const prefs = [];
    for (let k = 0; k < 45; k++) prefs.push(mk("note", "偏好" + k, { id: "P" + k, lastUsedAt: NOW - k * DAY }));
    check("evict 45 条偏好超 limit=40 仍全部保留", M.memRankEvict(prefs, 40).length === 45);
})();

/* 6c) 回归：偏好占满 limit 时，刚 unshift 的新工作记忆仍幸存（不会「保存成功却查不到」）。 */
(function () {
    const items = [];
    for (let k = 0; k < 40; k++) items.push(mk("note", "偏好" + k, { id: "P" + k, lastUsedAt: NOW - k * DAY }));
    const fresh = mk("search", "刚记的需求", { id: "FRESH", lastUsedAt: NOW });
    items.unshift(fresh);   // 新工作项在最前、最新
    const sids = idOf(M.memRankEvict(items, 40));
    check("evict 偏好占满时新工作记忆仍幸存", sids.includes("FRESH"));
    check("evict 偏好占满时 40 条偏好全留", sids.filter((s) => s[0] === "P").length === 40);
})();

/* 7) 淘汰未超上限=保序原样；排序不改入参。 */
(function () {
    const a = mk("search", "a", { id: "a" });
    const b = mk("note", "b", { id: "b" });
    const items = [a, b];
    check("evict 未超上限保序", idOf(M.memRankEvict(items, 3)).join(",") === "a,b");
    const before = idOf(items).join(",");
    M.memRankOrder(items, "a", NOW);
    check("order 不改入参顺序", idOf(items).join(",") === before);
})();

/* 8) 归一/分词纯度：normalize 折叠空白+小写；tokens 对 ASCII 整体保留、对 CJK 出 bigram。 */
(function () {
    check("normalize 折叠空白+小写", M.memRankNormalize("  Aa   Bb ") === "aa bb");
    const toks = M.memRankTokens("人类 Lung");
    check("tokens 含 CJK bigram 人类", toks.includes("人类"));
    check("tokens 含 ASCII 整体 lung（小写在 normalize 层）", toks.includes("Lung") || toks.includes("lung"));
})();

/* 9) 向后兼容（载重不变量#7）：v1 时代缺 useCount/lastUsedAt/summary/时间戳的**裸旧记忆**
 *    不崩、分数有限、可排序、可淘汰——防未来重构误删 `Number(...)||0` / `||0` 兜底后旧浏览器炸。 */
(function () {
    const legacy = { id: "legacy", kind: "search", text: "旧记忆无新字段" };   // 故意只含 id/kind/text
    const fresh = mk("search", "旧记忆无新字段", { id: "fresh", useCount: 3 });
    const stats = M.memRankCorpusStats([legacy, fresh]);
    check("legacy 缺字段分数有限（不 NaN）", Number.isFinite(M.memRankScore(legacy, "旧记忆", stats, NOW)));
    check("legacy 缺 lastUsedAt/时间戳 recency 兜底为 0（有限）", M.memRankRecency(legacy, NOW) === 0);
    let threw = false;
    try { M.memRankOrder([legacy, fresh], "旧记忆", NOW); M.memRankEvict([legacy, fresh], 40); }
    catch (_e) { threw = true; }
    check("legacy 裸对象喂 Order/Evict 不抛异常", !threw);
})();

/* 10) 鲁棒（与 memRankScore 的 null 守卫一致）：Order/Evict/CorpusStats 对 null 元素不抛异常。 */
(function () {
    let threw = false;
    try {
        M.memRankCorpusStats([null]);
        M.memRankOrder([null, mk("search", "x", { id: "x" })], "肺癌", NOW);
        M.memRankEvict([null], 40);
    } catch (_e) { threw = true; }
    check("null 元素不使纯核抛异常", !threw);
})();

console.log(failures === 0 ? "\nMEMORY_RANK_SPEC_OK" : `\nMEMORY_RANK_SPEC_FAILED (${failures})`);
process.exit(failures === 0 ? 0 : 1);
