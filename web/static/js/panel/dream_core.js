"use strict";

/* ============================================================================
 * dream_core.js —— dream 记忆「纯核」（无副作用、可单测）
 * ----------------------------------------------------------------------------
 * dream 界面层（dream.js）只做 DOM 与网络；素材的组织/裁剪/去重全在这里，
 * 与 memory_rank.js 同一个「纯核 + node 行为门」哲学。
 *
 *   · 浏览器：ES Module（importmap 键 #dream_core），dream.js 经 import 调用；
 *   · node：tests/js/dream_core_spec.mjs 直接 import 跑真行为断言。
 * ========================================================================== */

export const DREAM_MAX_CONV = 12;         // 一次最多整理的对话段数（与后端 dream.py 对齐）
export const DREAM_CHAT_PER_CONV = 40;    // 每段最多带入的消息条数（= board.js CB_LOG_MAX）
export const DREAM_CONV_CHAR_MAX = 1500;  // 每段对话的字符预算（省 token；后端同值再收一遍口）

/* 归一：折叠空白 + 小写。与 memRankNormalize 同规则——dream 去重键与记忆排序核同一口径。
   本地另写一份（3 行）而不是 require memory_rank：纯核零依赖，node 下单文件可跑。 */
export function dreamNormalize(value) {
    return String(value || "").trim().replace(/\s+/g, " ").toLowerCase();
}

/* 历史快照 → 对话素材。
   hist 每行存「到该轮为止的**累计** chat」（core.js pushHist）——同一 convId 的**最后一行**
   携带该段最完整对话。按 convId 取最后一行、新→旧、限 maxConv 段；
   每段 {query, chat:[{k,t,n}]}，只带白名单字段（发给 LLM 的素材最小化）。 */
export function dreamCollectConversations(hist, maxConv) {
    const rows = Array.isArray(hist) ? hist : [];
    const lastByConv = new Map();
    rows.forEach(function (row, i) {
        if (!row || typeof row !== "object") return;
        const cid = String(row.convId || ("row-" + i));
        lastByConv.set(cid, row);   // 同 convId 后出现的覆盖：留下的即最后一轮
    });
    const convs = [];
    lastByConv.forEach(function (row) {
        const chat = (Array.isArray(row.chat) ? row.chat : [])
            .map(function (m) {
                return m && typeof m === "object"
                    ? { k: String(m.k || ""), t: String(m.t || "").trim(), n: String(m.n || "").trim() }
                    : null;
            })
            .filter(function (m) { return m && m.t; })
            .slice(-DREAM_CHAT_PER_CONV);
        const query = String(row.query || "").trim();
        if (query || chat.length) convs.push({ query: query, chat: chat, at: Number(row.at) || 0 });
    });
    convs.sort(function (a, b) { return b.at - a.at; });   // 新→旧
    return convs.slice(0, Math.max(1, Number(maxConv) || DREAM_MAX_CONV))
        .map(function (c) { return { query: c.query, chat: c.chat }; });
}

/* 字符预算裁剪：每段对话从**最新**的消息往回装，超预算截断（保留最近的上下文）。
   发给 LLM 前先裁一遍省 token；服务端 dream.py 同值再收一遍口（纵深防御）。 */
export function dreamClipConversations(convs) {
    return (Array.isArray(convs) ? convs : []).map(function (c) {
        const chat = Array.isArray(c.chat) ? c.chat.slice() : [];
        let budget = DREAM_CONV_CHAR_MAX - String(c.query || "").length;
        const kept = [];
        for (let i = chat.length - 1; i >= 0 && budget > 0; i--) {
            const m = chat[i];
            const cost = (m.t || "").length + (m.n || "").length;
            if (cost > budget && kept.length) break;
            kept.unshift(m);
            budget -= cost;
        }
        return { query: String(c.query || "").slice(0, 300), chat: kept };
    });
}

/* 与既有记忆去重：候选条与任一既有记忆同文 → 不再呈现给用户勾选。
   判重键 = 归一后剥掉空白、常见标点与助词（的/了）：偏好短句里「的/了」是随手加的——
   「只要人类的肺数据」与「只要人类肺数据」是同一条；这与 memRank 的检索归一刻意不同。
    B6 调研六候选批（Mem0 对账思想的前端落地，ADD/NOOP 版）：
   判重键全等之外再加 **Jaccard 近重复通道**——长句重排/高度重叠的候选判 NOOP。
   阈值定在 0.75 的校准记录：7-8 字短句上「肺部 vs 肺」与「脑 vs 肺」的 bigram 分数
   会撞车（单字符级差异在短句上不可分）——本通道只抓长句重排，短句级微调交给判重键。 */
export const DREAM_DUP_JACCARD = 0.75;   // CJK bi-gram + ASCII 整词混合 token 集阈值（边界由 node spec 钉死）

/* 判重键：归一后剥空白、标点、助词。 */
function _dreamDupKey(v) {
    return dreamNormalize(v).replace(/[\s，。、：；的了]/g, "");
}

/* CJK bi-gram + ASCII 整词混合 token 集：ASCII 词（fastq / visium）作为整体 token 计入，
   避免被拆成无意义双字母。 */
function _dreamTokenSet(value) {
    const flat = dreamNormalize(value).replace(/\s+/g, "");
    const set = new Set();
    for (let i = 0; i < flat.length - 1; i++) set.add(flat.slice(i, i + 2));
    dreamNormalize(value).split(/\s+/).forEach(function (w) {
        if (/^[a-z0-9]+$/.test(w)) set.add(w);
    });
    return set;
}

function _dreamJaccard(a, b) {
    if (!a.size || !b.size) return 0;
    let inter = 0;
    a.forEach(function (t) { if (b.has(t)) inter += 1; });
    return inter / (a.size + b.size - inter);
}

export function dreamFilterNew(candidates, existingItems) {
    const seen = new Set();
    const tokenSets = [];
    (Array.isArray(existingItems) ? existingItems : []).forEach(function (it) {
        const key = _dreamDupKey(it && it.text);
        if (!key) return;
        seen.add(key);
        tokenSets.push(_dreamTokenSet(it.text));
    });
    const out = [];
    (Array.isArray(candidates) ? candidates : []).forEach(function (c) {
        const key = _dreamDupKey(c && c.text);
        if (!key || seen.has(key)) return;
        const tokens = _dreamTokenSet(c.text);
        const nearDup = tokenSets.some(function (other) {
            return _dreamJaccard(tokens, other) >= DREAM_DUP_JACCARD;
        });
        if (nearDup) return;   // 近重复 = NOOP（对账判重），不再呈现
        seen.add(key);
        tokenSets.push(tokens);
        out.push(c);
    });
    return out;
}

/* 无绞杀桥：唯一消费方 dream.js 本就是 ESM、经 import 调用；node 规格（tests/js/dream_core_spec.mjs）直接 import。 */
