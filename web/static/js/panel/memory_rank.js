"use strict";

/* ============================================================================
 * memory_rank.js —— 用户记忆「纯排序核」（无副作用、可单测）
 * ----------------------------------------------------------------------------
 * 加载：ES Module（importmap 键 #memory_rank）；memory.js 经 import 调用（此前是经典脚本共享全局）。
 *
 * 为什么单独成文（MODULES.md 前端表）：
 *   记忆召回的排序/淘汰是**纯函数逻辑**（给定记忆集 + 查询 + now → 名次），却是这个
 *   功能「能召回」核心价值最吃重的部分。旧实现把它和 DOM、本地存储缠在 memory.js
 *   里 → `web_smoke` 只静态查字符串、`node --check` 只验语法，两门都测不出打分行为对不对
 *   （测试文件自己都写「行为正确性另由 node 手验」）。抽成零依赖纯核后：
 *     · 浏览器：ES Module，memory.js 经 import { memRankOrder, memRankEvict, memRankNormalize } 调用；
 *     · node：tests/js/memory_rank_spec.mjs 直接 import，跑**真行为断言**。
 *   —— 让排序数学有回归网，上面的 IDF/衰减/分层优化才敢动。
 *
 * 设计不变量：
 *   · 纯确定性：不读 DOM、不读本地存储、不取墙钟时间（now 一律由调用方传入）、无随机。
 *   · 无外部依赖：不引入嵌入模型 / 远端接口 / 网络往返——契合「本地、可检查、离线」的产品定位。
 *   · 分层（对齐 OpenClaw「可解释分层」）：note=研究偏好=**持久层**（不衰减、召回有下限、
 *     永不被工作记忆挤出淘汰）；search=已存需求=**工作层**（按时间衰减、可淘汰）。
 * ========================================================================== */

/* 归一：折叠空白 + 小写。记忆的相关性匹配与 memory.js 的去重键单一真源都走它。 */
export function memRankNormalize(value) {
    return String(value || "").trim().replace(/\s+/g, " ").toLowerCase();
}

/* 分词：先按标点/空白分段；每段内「CJK 连续段」做 bi-gram（单字段保 unigram），
 * 「非 CJK 段」整体保留。中文查询常无分隔符、整句当一个 token 会让相关性退化成纯最近
 * 使用——bi-gram 让「人类肺癌」在「肺癌」上与记忆「肺癌单细胞」计分。纯确定性、无依赖。 */
export function memRankTokens(text) {
    const out = new Set();
    for (const seg of String(text || "").split(/[\s,，。；;、/]+/)) {
        if (!seg) continue;
        for (const run of (seg.match(/[一-鿿]+|[^一-鿿]+/g) || [])) {
            if (/[一-鿿]/.test(run)) {
                if (run.length === 1) out.add(run);
                else for (let i = 0; i < run.length - 1; i++) out.add(run.slice(i, i + 2));
            } else {
                const t = run.trim();
                if (t) out.add(t);
            }
        }
    }
    return Array.from(out);
}

/* 语料统计：df(token) = 记忆集里**含该 token** 的记忆条数（用户自己的记忆集，全本地可算）。
 * 供 IDF 区分度加权用——让「细胞/数据」这类几乎条条都有的泛 token 权重被自然压低。 */
export function memRankCorpusStats(items) {
    const df = new Map();
    let n = 0;
    for (const it of (items || [])) {
        if (!it) continue;   // 容错：跳过 null/undefined 元素（与 memRankScore 的 null 守卫一致）
        n += 1;
        const seen = new Set(memRankTokens(memRankNormalize(`${it.text || ""} ${it.summary || ""}`)));
        for (const t of seen) df.set(t, (df.get(t) || 0) + 1);
    }
    return { n, df };
}

/* IDF：df 越小（越稀有、越有区分度）权重越大；df 越大（越泛）越接近 0。
 * log(1 + n/(1+df))——平滑、恒正、随 df 单调不增。 */
export function memRankIdf(token, stats) {
    const n = (stats && stats.n) || 0;
    const df = (stats && stats.df && stats.df.get(token)) || 0;
    return Math.log(1 + n / (1 + df));
}

/* 时间衰减：0.5^(age/半衰期)，落在 (0,1]。仅**工作层**衰减；偏好层由调用方置 1（不衰减）。 */
export const MEM_RANK_HALFLIFE_MS = 14 * 24 * 3600 * 1000;   // 14 天
export function memRankRecency(item, now) {
    const t = (item && (item.lastUsedAt || item.updatedAt || item.createdAt)) || 0;
    if (!t) return 0;
    const age = Math.max(0, Number(now) - t);
    return Math.pow(0.5, age / MEM_RANK_HALFLIFE_MS);
}

/* 分层判定：note = 研究偏好 = 持久层；其余（search）= 工作层。 */
export function memRankIsPreference(item) { return item && item.kind === "note"; }

/* 打分权重：relevance 数量级远大于其余——**有查询且相关**时，命中的工作记忆恒压过不相关的
 * 偏好（守住「打了相关词就该浮上来」）；tier/recency/use 只在相近相关度间做二次排序。 */
export const MEM_RANK_W = { relevance: 100, use: 2.0, recency: 6.0, tier: 4.0 };

export function memRankScore(item, query, stats, now) {
    const q = memRankNormalize(query);
    const hay = memRankNormalize(`${(item && item.text) || ""} ${(item && item.summary) || ""}`);
    let relevance = 0;
    if (q) {
        let sum = 0;
        for (const token of memRankTokens(q)) if (hay.includes(token)) sum += memRankIdf(token, stats);
        // 温和长度归一：抑制「越啰嗦越容易命中任意 token」的虚高，但不抹掉真实多命中。
        const norm = 1 + Math.log10(1 + hay.length / 40);
        relevance = sum / norm;
    }
    const pref = memRankIsPreference(item);
    const recency = pref ? 1 : memRankRecency(item, now);   // 偏好不衰减
    const useCount = Math.max(0, Number(item && item.useCount) || 0);
    return MEM_RANK_W.relevance * relevance
        + MEM_RANK_W.use * Math.log(1 + useCount)
        + MEM_RANK_W.recency * recency
        + MEM_RANK_W.tier * (pref ? 1 : 0);
}

/* 召回排序：返回**新数组**（不改入参），高分在前；分数并列按原始下标稳定排序。 */
export function memRankOrder(items, query, now) {
    const list = (items || []).slice();
    const stats = memRankCorpusStats(list);
    return list
        .map((it, i) => ({ it, i, s: memRankScore(it, query, stats, now) }))
        .sort((a, b) => (b.s - a.s) || (a.i - b.i))
        .map((x) => x.it);
}

/* 淘汰（持久层 literal 保证 + 工作层封顶）：
 *   · 研究偏好（持久层）**绝不自动淘汰**——数量由用户经删除/清空自行管理（手工添加、天然有限、
 *     字节量微小）。故 saveUserMemories 对任何偏好写入都不会「报成功却静默丢一条更早偏好」(RANK-1)。
 *   · `limit` 只封顶**工作层**：工作记忆超上限时按最近使用新→旧保留 limit 条、淘汰最旧的一次性检索。
 *     刚 unshift 的新工作项 lastUsedAt=now 最新，恒在保留集内 → 不会「记住成功却查不到」。
 *   · 幸存者按**原始相对顺序**返回；null/坏元素随工作层判定被剔除。工作层未超上限时原样返回（保序）。 */
export function memRankEvict(items, limit) {
    const list = (items || []).slice();
    const work = list.filter((it) => it && !memRankIsPreference(it));
    if (work.length <= limit) return list;   // 工作层未超上限 → 无需淘汰，原样保序
    const byRecency = (a, b) =>
        (((b && (b.lastUsedAt || b.updatedAt || b.createdAt)) || 0) - ((a && (a.lastUsedAt || a.updatedAt || a.createdAt)) || 0));
    const keepWork = new Set(work.slice().sort(byRecency).slice(0, limit));
    return list.filter((it) => memRankIsPreference(it) || keepWork.has(it));   // 偏好全留 + 最新 limit 条工作层，保序
}

/* 无绞杀桥：唯一消费方 memory.js 同批转 ESM、经 import 调用；node 规格（tests/js/memory_rank_spec.mjs）直接 import。 */
