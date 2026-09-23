"use strict";
/* 使用反馈 · **纯聚合核**（无 DOM / 无 localStorage / 无墙钟 / 无网络，node 可单测）
 *
 * 职责：把一串使用事件聚合成**一段能直接 Ctrl+C 粘进微信输入框**的中文文本。
 * 记录、存储、开关、界面全在 usage_log.js；这里只做「事件数组 → 字符串」这一个纯变换。
 * 分层理由同 memory_rank.js / board_core.js：数学与文案可以在 node 里按真行为断言，
 * 不用起浏览器、也不会因为改了界面就把聚合逻辑测糊。
 *
 * 两处**例外区**（各自带独立小节头注释，仍零 DOM/零网络/零墙钟，node 可测）：
 * ① 遥测 ID 层：sid/tid/iid/policy 四级标识——有模块内状态，
 *    可选读 sessionStorage（不可用降级内存），不读墙钟、不碰 DOM；
 * ② 上传包构造（buildTelemetryPackage）：「事件/记录数组 → 已脱敏的包」纯变换，
 *    时间与环境由调用方注入。
 *
 * ## 为什么产物是「一段文本」而不是 JSON 文件
 *
 * 产品方的原话是「简单地 ctrl c+v 微信发送即可的那种」。这句话把格式定死了：
 * ① 必须是纯文本 —— 微信输入框粘 JSON 会连引号带缩进一起过去，没人读得下去；
 * ② 必须**短** —— 微信对超长粘贴会弹「是否转为文件发送」，一转成文件就不是「简单粘一下」了。
 *    所以这里有硬字数预算 USAGE_MAX_CHARS，超了**如实截断并写明省略了多少**（见下条）。
 * ③ 用户看到的就是他发出去的 —— 界面把这段文本放进一个可编辑 textarea，
 *    所见即所发。不做「后台另有一份完整数据」这种事。
 *
 * ## 诚实性约束（本项目的老规矩，聚合层同样适用）
 *
 * 截断**绝不许静默**。查询条目按次数降序，超出上限的部分不是消失，而是显式写成
 * 「另有 N 条各出现 1 次（本次省略）」。一份看起来干净、实则悄悄少了一半的反馈，
 * 比一份明说「省了 30 条」的反馈有害得多 —— 因为读它的人会拿它当全集做判断。
 *
 * ## 时间戳一律按 UTC 格式化
 *
 * 纯核不许碰墙钟，但把调用方传进来的 ts 转成「07-29」是确定性变换。
 * 这里刻意用 getUTC*：本地时区会让同一份事件在不同机器上聚合出不同日期，
 * node 规格就没法逐字节断言。日期在这里只用于「大概哪段时间」，差几小时不影响任何判断。
 */

export const USAGE_SCHEMA = 3;   // v3：imp/label 独立事件、卡级不可变 ImpressionContext 归因、policy_id 优先、consent v2（同意时刻 ISO）、曝光「看过」判据 500ms 状态机（USAGE_SEEN_MIN_MS）。v2已被占用故跳号；v2 内容：活跃天数、「检索速度」小节（秒出占比+实测耗时）、弃权查询原话；打点侧 search.ms/cached、open site、undo

/* 字数预算。微信实测在几千字上下开始提示转文件；留足余量，
   并且给用户自己在 textarea 里补充几句话的空间。 */
export const USAGE_MAX_CHARS = 1800;

/* 查询列表最多列几条（超出走「另有 N 条」的诚实省略）。 */
export const USAGE_MAX_QUERY_LINES = 24;

/* 事件类型表 —— **记录层和聚合层共用这一份**，不许各抄一份。
   本项目在「两份手抄必漂移」上栽过不止一次（gitignore/deliveryignore 对不上账、
   两份条数解析器各算各的），这里从一开始就只留一个真源。 */
export const USAGE_KINDS = {
    search: "search",   // 一次检索：查询原话 + 命中数 + 用了哪几层排序 + 没用上的词（v3 起当屏 items 移到 imp 事件）
    open: "open",       // 打开某条结果（介绍 / 文件清单 / 原站），带它在结果里的名次
    dl: "dl",           // 产出物：任务包 / 下载脚本 / 引文
    facet: "facet",     // 数据细化
    relax: "relax",     // 放宽条件
    conv: "conv",       // 对话记录页签内的交互（细化 / 聊天）
    undo: "undo",       // 撤销 / 回退
    fav: "fav",         // 收藏
    ai: "ai",           // AI 某一层的结果（用上了 / 没能完成）
    err: "err",         // 检索直接失败（服务未响应 / 网络异常）
    view: "view",       // 一次结果展示的曝光汇总（v3：seen=入视口≥500ms 的名次表 + 可见累计毫秒，results.js 的 IO 追踪）
    imp: "imp",         // 一次结果展示的内容快照（v3：tid/iid/policy + 当屏 items[{uid,pos,score,reason?}]，卡级归因的锚）
    label: "label",     // 一次轮次评分（v3：benchfb 评分也进 usage 流——记录被 ACK 删除后评价仍可达；接收端按 rev 合并）
    // ---- 追踪（2026-08-22 计数型无文本——追踪名/query/uid 不进遥测）----
    project_created: "project_created",     // 存为追踪成功（带候选数 n）
    project_resumed: "project_resumed",     // 打开追踪详情
    context_card_used: "context_card_used", // 追踪上下文卡激活（{once}：会话内同追踪只计一次）
    // ---- 数据集页一键同步----
    sync_button_used: "sync_button_used",   // 点「同步数据集」并拿到回执（带 added/skipped/failed 计数；无文本）
    // ---- 下一步行动（2026-08-22 全部计数型无文本）----
    ladder_shown: "ladder_shown",           // 结果页阶梯 chips 展示（带 n=颗数；无文本）
    ladder_clicked: "ladder_clicked",       // 点某颗阶梯 chip / 收窄建议（带 action=动作 id；无文本）
    template_originated: "template_originated", // 任务卡/chip 生成文本提交（带 edited=true/false；无文本）
    // ---- 追踪更新检查闭环----
    watch_checked: "watch_checked",         // 单追踪检查完成（{changed}：1=有 material change，0=无；无文本）
    delta_review_completed: "delta_review_completed",   // 某追踪「待查看更新」被用户逐条处理完（{}；无文本）
    // ---- 追踪导出中心----
    export_downloaded: "export_downloaded", // 追踪导出 ZIP 真拿到之后（带 kind：download_list/citations/screening_record/full；无文本）
    // ---- 逐条系统回复反馈（2026-08-28 msgfb）：气泡下操作条的赞/倒赞 ----
    msgfb: "msgfb",             // 单条系统回复的赞/倒赞（{conv, mid, v:"up"|"down"|""}；无文本——评论正文走加密 feedback 通道，不进 usage 流）
};

export const USAGE_OPEN_LABELS = { intro: "数据集详情", files: "文件清单", site: "去原站" };
export const USAGE_DL_LABELS = { pack: "打包下载", script: "下载脚本", cite: "引文导出", reuse: "投稿材料", data: "数据下载" };
export const USAGE_CONV_LABELS = { refine: "数据细化", chat: "对话记录" };
/* **键必须逐字等于后端 search_trace 里的 step id**（local_semantic / llm_rerank / llm_polish），
   不是我另起的短名 —— 记录层用 USAGE_AI_LABELS[s.id] 当准入判据，键对不上的后果不是报错，
   而是**一条 AI 事件都记不进去、还没有任何提示**（同 FRONTEND.md §4.3 那类静默短路）。
   llm_intro 不在 trace 里（它是 cards.js 的按需导读），由该处单独打点，共用这张表。 */
export const USAGE_AI_LABELS = { local_semantic: "本地精准重排", llm_rerank: "AI 重排", llm_polish: "AI 说明润色", llm_intro: "AI 中文导读" };

/* ============================================================================
 * 遥测 ID 层（2026-08-22 schema）
 * ----------------------------------------------------------------------------
 * 与上方的聚合纯核不同，本节是**有状态的会话标识**——它仍是本文件唯一的有状态区：
 * 零 DOM / 零网络 / 零墙钟（id 由随机数 + 模块内单调计数器合成，不读 Date.now），
 * sessionStorage 不可用时静默降级为内存值，node 规格可直接断言。
 *
 * 四级标识的分工：
 * - sid（tab 级）：sessionStorage 键 biodata_sid_v1。一个浏览器标签页一生一个，
 *   用来把同一标签页内的事件串成一条线；关标签页即灭，不是长期指纹。
 * - tid（轮级）：用户每提交一次检索或对话（board.js ubSubmit）换一个新的。
 *   search 与 conv 共用「当前轮」——同一轮里的 open/dl/fav/view 都归到这一tid。
 * - iid（展示级）：结果区每重渲一次列表（results.js renderResults / 放宽预览）换一个新的，
 *   search 事件的 items 与 view 事件的 seen 都按它对齐到同一屏。
 * - policy（策略串）：排序策略 + rerank/recall 开关 + 缓存代的紧凑串（usagePolicyId 纯组合），
 *   让同一句查询在不同管线配置下的表现可以分开算。
 * ========================================================================== */

/* ---------- uid/hash 基元（usage 域所有 id/哈希的唯一实现；形状各保其旧，消费点一律 import） ---------- */

let _idSeq = 0;
/* 带会话内递增计数后缀的 uid：CSPRNG（randomUUID）优先，不可用时降级 random+counter。
   随机段更长：sid/tid/iid 跨标签页/跨会话撞名概率基本归零；计数后缀让同毫秒生成也可区分。 */
export function usageTelemetryUid(prefix) {
    _idSeq += 1;
    try {
        if (typeof crypto !== "undefined" && crypto && typeof crypto.randomUUID === "function") {
            return prefix + "-" + crypto.randomUUID() + "-" + _idSeq.toString(36);
        }
    } catch (_e) {}
    return prefix + "-" + Math.random().toString(36).slice(2, 10) + "-" + _idSeq.toString(36);
}
/* 纯随机 uid（无计数后缀；降级路径带时间戳）：client/profile/event/drop 等持久 id 用。
   纯核零环境依赖：随机源读全局 crypto（与 usageTelemetryUid 同形），墙钟毫秒数由调用方注入。 */
export function usageRandomId(prefix, nowMs) {
    try {
        if (typeof crypto !== "undefined" && crypto && typeof crypto.randomUUID === "function") {
            return prefix + "-" + crypto.randomUUID();
        }
    } catch (_e) {}
    return prefix + "-" + Number(nowMs).toString(36) + "-" + Math.random().toString(36).slice(2, 14);
}
/* FNV-1a 32bit → base36：legacy 事件迁移 id 与 drop 快照 revision 的确定性哈希。 */
export function usageFnv1a(text) {
    let h = 2166136261;
    for (let i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = Math.imul(h, 16777619); }
    return (h >>> 0).toString(36);
}
/* 双种子 FNV → 16 hex：crypto.subtle 不可用时 packet_id 的兜底哈希（输出形状与主路径区分开）。 */
export function usagePacketHashFallback(text) {
    let a = 2166136261, b = 2246822519;
    for (let i = 0; i < text.length; i++) {
        const c = text.charCodeAt(i); a = Math.imul(a ^ c, 16777619); b = Math.imul(b ^ c, 3266489917);
    }
    return (a >>> 0).toString(16).padStart(8, "0") + (b >>> 0).toString(16).padStart(8, "0");
}

let _sidMem = "";
export function usageSessionId() {
    if (_sidMem) return _sidMem;
    let s = "";
    try { if (typeof sessionStorage !== "undefined") s = String(sessionStorage.getItem("biodata_sid_v1") || ""); } catch (_e) { s = ""; }
    if (!s) {
        s = usageTelemetryUid("sid");
        try { if (typeof sessionStorage !== "undefined") sessionStorage.setItem("biodata_sid_v1", s); } catch (_e) {}
    }
    _sidMem = s;
    return s;
}

let _activeTurnId = "";
/* 一轮的开始（用户亲手提交检索/对话）。返回新 tid；调用方只有 board.js ubSubmit。 */
export function usageBeginTurn() { _activeTurnId = usageTelemetryUid("t"); return _activeTurnId; }
export function usageActiveTurnId() { return _activeTurnId; }

let _activeImpressionId = "";
/* 一次新结果列表展示的开始。返回新 iid；调用方只有 results.js 的结果区重渲。 */
export function usageBeginImpression() { _activeImpressionId = usageTelemetryUid("i"); return _activeImpressionId; }
export function usageActiveImpressionId() { return _activeImpressionId; }

/* 策略串纯组合：parts = {strategy, rerank, recall, gen}（调用方从既有请求参数/缓存代取，
   本函数不读任何全局、不发任何请求）。形如 "auto/llm/cross_encoder@20260820"。 */
export function usagePolicyId(parts) {
    parts = parts || {};
    const seg = function (v) { return String(v || "").trim() || "unknown"; };
    const gen = String(parts.gen || "").trim();
    return seg(parts.strategy) + "/" + seg(parts.rerank) + "/" + seg(parts.recall) + (gen ? "@" + gen : "");
}

function _stablePolicyValue(value, seen) {
    if (value === null || typeof value === "string" || typeof value === "boolean") return value;
    if (typeof value === "number") return Number.isFinite(value) ? value : null;
    if (Array.isArray(value)) return value.map(function (item) { return _stablePolicyValue(item, seen); });
    if (!value || typeof value !== "object") return null;
    if (seen.has(value)) throw new TypeError("cyclic policy_id");
    seen.add(value);
    const out = {};
    Object.keys(value).sort().forEach(function (key) {
        const item = value[key];
        if (item !== undefined && typeof item !== "function" && typeof item !== "symbol") {
            out[key] = _stablePolicyValue(item, seen);
        }
    });
    seen.delete(value);
    return out;
}

/* 后端策略引用的单一边界：优先 response.policy_id_str；兼容旧字符串；旧后端结构体走
   sorted-key JSON，绝不再落成 `[object Object]`。fallbackParts 仅在后端确实缺失时使用。 */
export function usagePolicyRef(source, fallbackParts) {
    let raw = source;
    if (source && typeof source === "object" && !Array.isArray(source)) {
        const isResponse = Object.prototype.hasOwnProperty.call(source, "policy_id")
            || Object.prototype.hasOwnProperty.call(source, "policy_id_str");
        if (isResponse) {
            const authoritative = typeof source.policy_id_str === "string" ? source.policy_id_str.trim() : "";
            if (authoritative && authoritative !== "[object Object]") return authoritative;
            raw = source.policy_id;
        } else if (!(String(source.schema || "").indexOf("biodata-policy-id/") === 0
                || source.ranking || source.corpus)) {
            raw = null; // 普通 API 响应没有 policy 字段，不得把整份 results 响应当策略对象
        }
    }
    if (typeof raw === "string") {
        const legacy = raw.trim();
        if (legacy && legacy !== "[object Object]") return legacy;
    } else if (raw && typeof raw === "object") {
        try { return "bpol-json:" + JSON.stringify(_stablePolicyValue(raw, new Set())); } catch (_e) {}
    }
    return fallbackParts ? usagePolicyId(fallbackParts) : "";
}

/* ============================================================================
 * 曝光「看过」状态机（2026-08-22 schema）
 * ----------------------------------------------------------------------------
 * 一张结果卡入视口**连续/累计可见 ≥ USAGE_SEEN_MIN_MS** 才算「看过」。仍零 DOM/零墙钟：
 * 时刻一律由调用方注入（results.js 用 IO entry.time 或 performance.now()，二者同源）；
 * 绝不与另一套时钟（如墙钟毫秒）混用——两套时钟原点不同，混用会把在途区间算成天文数字。
 *
 * 状态：cards: Map(pos → {accum, since})——accum=已冻结的累计毫秒，since=在途区间起点
 * （null=不在途）；seen: Set(pos)——已达冠名次（1-based），只进不出。
 *
 * 退出视口的两种语义（调用方经 opts.pause 区分）：
 * - pause=false（滚动离开）：清零重来——瞥一眼不算看；
 * - pause=true（页面隐藏）：冻结不清零——accum 收进在途区间，可见恢复后同一展示继续累计。
 * ========================================================================== */
export const USAGE_SEEN_MIN_MS = 500;

export function usageSeenCreate(thresholdMs) {
    return {
        threshold: Number(thresholdMs) > 0 ? Number(thresholdMs) : USAGE_SEEN_MIN_MS,
        cards: new Map(),
        seen: new Set(),
    };
}

/* 一次 IO 回调落点。返回 true 仅当本次让 pos **新**进入 seen（去重由 seen 集合保证）。
   intersecting=true 且非 pause：开在途区间（已开则不动）；随后按 accum+在途 复评阈值。 */
export function usageSeenTick(state, pos, intersecting, nowMs, opts) {
    if (!state || state.seen.has(pos)) return false;
    const pause = !!(opts && opts.pause);
    let c = state.cards.get(pos);
    if (!c) { c = { accum: 0, since: null }; state.cards.set(pos, c); }
    if (!intersecting && !pause) {
        c.accum = 0; c.since = null;   // 滚动离开：清零重来
    } else {
        if (c.since !== null) { c.accum += Math.max(0, nowMs - c.since); c.since = null; }   // pause 冻结 / 到点结算
        if (intersecting && !pause) c.since = nowMs;   // 可见且非暂停：（重）开在途区间
    }
    const total = c.accum + (c.since === null ? 0 : Math.max(0, nowMs - c.since));
    if (total >= state.threshold) {
        state.seen.add(pos);
        state.cards.delete(pos);
        return true;
    }
    return false;
}

/* 页面隐藏：冻结**全部**在途区间（accum 保留），由可见恢复后的 IO 初始回调重开。 */
export function usageSeenPause(state, nowMs) {
    if (!state) return;
    state.cards.forEach(function (c) {
        if (c.since !== null) { c.accum += Math.max(0, nowMs - c.since); c.since = null; }
    });
}

export function usagePad2(n) { return String(n).padStart(2, "0"); }

/* ts(ms) → "07-29"。UTC，理由见文件头。 */
export function usageDayStamp(ts) {
    const d = new Date(Number(ts) || 0);
    return usagePad2(d.getUTCMonth() + 1) + "-" + usagePad2(d.getUTCDate());
}

/* 计数：返回 [[值, 次数], …]，次数降序；同次数时按**首次出现顺序**稳定排列
   （不按字典序 —— 那会让「同样常用的两条」在两次导出里换位置，看起来像有变化其实没有）。 */
export function usageCountBy(list, keyFn) {
    const counts = new Map();
    const order = new Map();
    (list || []).forEach(function (item, i) {
        const key = keyFn(item);
        if (key === null || key === undefined || key === "") return;
        counts.set(key, (counts.get(key) || 0) + 1);
        if (!order.has(key)) order.set(key, i);
    });
    return Array.from(counts.entries()).sort(function (a, b) {
        if (b[1] !== a[1]) return b[1] - a[1];
        return order.get(a[0]) - order.get(b[0]);
    });
}

/* 名次分桶：排序质量的直接读数。都点第 1 条 = 排序好；总点到很后面 = 排序需要改。 */
export function usageRankBuckets(ranks) {
    const b = { first: 0, second: 0, third: 0, rest: 0, unknown: 0 };
    (ranks || []).forEach(function (r) {
        const n = Number(r);
        if (!Number.isFinite(n) || n < 1) { b.unknown += 1; return; }
        if (n === 1) b.first += 1;
        else if (n === 2) b.second += 1;
        else if (n === 3) b.third += 1;
        else b.rest += 1;
    });
    return b;
}

/* 「搜完什么都没点」的次数 —— 无效检索率的直接读数。
   按时间顺序扫：一次 search 到下一次 search 之间，有没有出现过 open / dl / fav。
   这个数字比「零返回」更能说明问题：有结果但一条都不想点，等于排序或召回不对路。 */
export function usageBarrenSearches(events) {
    let barren = 0;
    let open = false;      // 当前是否有一次「尚未见到任何后续动作」的检索
    let hadAction = false;
    (events || []).forEach(function (e) {
        if (!e) return;
        if (e.k === USAGE_KINDS.search) {
            if (open && !hadAction) barren += 1;
            // 零返回的检索不算「搜完没点」——那是另一类问题，单独统计，不重复归罪。
            open = Number(e.n) > 0;
            hadAction = false;
            return;
        }
        if (e.k === USAGE_KINDS.open || e.k === USAGE_KINDS.dl || e.k === USAGE_KINDS.fav) hadAction = true;
    });
    if (open && !hadAction) barren += 1;
    return barren;
}

export function usageJoinCounts(pairs, sep) {
    return pairs.map(function (p) { return p[0] + " " + p[1]; }).join(sep || " · ");
}

/* 主入口：事件数组 → { text, chars, truncated, … }。
   opts.installId：一个与身份无关的随机短码，用来把同一台机器的多次反馈对上号。
   opts.maxChars / opts.maxQueryLines：预算，便于测试注入小值验证省略行为。 */
export function usageSummarize(events, opts) {
    events = (events || []).filter(function (e) { return e && typeof e === "object"; });
    opts = opts || {};
    const maxChars = Number(opts.maxChars) > 0 ? Number(opts.maxChars) : USAGE_MAX_CHARS;
    const maxQueryLines = Number(opts.maxQueryLines) > 0 ? Number(opts.maxQueryLines) : USAGE_MAX_QUERY_LINES;
    const installId = String(opts.installId || "").trim() || "未知";

    if (!events.length) {
        return {
            text: "BioData 使用反馈 · v" + USAGE_SCHEMA + "\n装机 " + installId + "\n\n（这段时间没有记录到任何使用，可能是刚打开就导出了，或者采集一直是关着的。）",
            chars: 0, empty: true, truncated: false, omittedQueries: 0, events: 0,
        };
    }

    const searches = events.filter(function (e) { return e.k === USAGE_KINDS.search; });
    const stamps = events.map(function (e) { return Number(e.t) || 0; }).filter(Boolean);
    const from = stamps.length ? usageDayStamp(Math.min.apply(null, stamps)) : "?";
    const to = stamps.length ? usageDayStamp(Math.max.apply(null, stamps)) : "?";

    const lines = [];
    lines.push("BioData 使用反馈 · v" + USAGE_SCHEMA);
    // 同一天不写「07-29 至 07-29」这种废话。
    const span = (from === to) ? from : (from + " 至 " + to);
    // 活跃天数：事件时间戳按 UTC 日期去重。只有 1 天时日期本身已说明，不啰嗦。
    const daySet = new Set();
    events.forEach(function (e) { const t = Number(e.t) || 0; if (t > 0) daySet.add(usageDayStamp(t)); });
    lines.push("装机 " + installId + " · " + span + (daySet.size > 1 ? " · 活跃 " + daySet.size + " 天" : "") + " · 检索 " + searches.length + " 次");

    // ── 搜过什么（去重计数）────────────────────────────────
    // 这是整份反馈里最值钱的一段：真人张嘴会怎么问，是任何自造测试查询都替代不了的。
    const zeroSet = new Set();
    searches.forEach(function (e) { if (!(Number(e.n) > 0)) zeroSet.add(String(e.q || "")); });
    const queryPairs = usageCountBy(searches, function (e) { return String(e.q || "").trim(); });
    let omittedQueries = 0;
    let omittedHits = 0;
    if (queryPairs.length) {
        lines.push("");
        lines.push("■ 搜过什么（去重 " + queryPairs.length + " 条）");
        queryPairs.slice(0, maxQueryLines).forEach(function (p) {
            lines.push((p[1] > 1 ? p[1] + "× " : "") + p[0] + (zeroSet.has(p[0]) ? "  ← 0 条" : ""));
        });
        const rest = queryPairs.slice(maxQueryLines);
        if (rest.length) {
            omittedQueries = rest.length;
            omittedHits = rest.reduce(function (s, p) { return s + p[1]; }, 0);
            lines.push("（另有 " + omittedQueries + " 条不同的查询、合计 " + omittedHits + " 次，为了长度这次省略）");
        }
    }

    // ── 没找到 ───────────────────────────────────────────
    const zeroCount = searches.filter(function (e) { return !(Number(e.n) > 0); }).length;
    if (zeroCount) {
        const pct = Math.round((zeroCount / searches.length) * 100);
        lines.push("");
        lines.push("■ 一条都没搜到：" + zeroCount + " 次（占 " + pct + "%）");
    }

    // ── 弃权（系统主动说「这句话我没把握」）──────────────────
    const abstainList = searches.filter(function (e) { return e.abstain === true; });
    if (abstainList.length) {
        lines.push("其中系统主动弃权 " + abstainList.length + " 次");
        // 弃权的查询原话是最直接的改进线索：它们全是真人说的话，却一句都没敢答。
        const aq = usageCountBy(abstainList, function (e) { return String(e.q || "").trim(); }).slice(0, 3);
        if (aq.length && aq[0][0]) lines.push("弃权的是：" + aq.map(function (p) { return "「" + p[0] + "」"; }).join("、"));
    }

    // ── 检索速度（秒出占比 + 实测耗时）──────────────────────────
    // 慢是用户能感到、却最难从代码看出来的事。cached=命中本地缓存的秒出（不计耗时）；
    // ms=后端 search_trace.total_duration_ms（真实管线耗时，缓存命中的旧 trace 不算数）。
    const cachedN = searches.filter(function (e) { return e.cached === true; }).length;
    const timings = searches.map(function (e) { return Number(e.ms) || 0; }).filter(function (v) { return v > 0; });
    if (cachedN || timings.length) {
        const fmtMs = function (ms) { return ms < 1000 ? Math.round(ms) + " 毫秒" : (ms / 1000).toFixed(1) + " 秒"; };
        lines.push("");
        lines.push("■ 检索速度");
        if (cachedN) lines.push("秒出（相同查询直接沿用上次结果）：" + cachedN + " 次");
        if (timings.length) {
            const avg = timings.reduce(function (s, v) { return s + v; }, 0) / timings.length;
            lines.push("实测耗时（不含秒出）：平均 " + fmtMs(avg) + " · 最慢 " + fmtMs(Math.max.apply(null, timings)) + "（" + timings.length + " 次）");
        }
    }

    // ── 没被当成筛选条件的词─────────────────
    // 这一段直接告诉我词表缺什么 —— 比任何「我猜用户会搜什么」都准。
    const unused = [];
    searches.forEach(function (e) {
        (Array.isArray(e.unused) ? e.unused : []).forEach(function (w) {
            const s = String(w || "").trim();
            if (s) unused.push(s);
        });
    });
    if (unused.length) {
        const pairs = usageCountBy(unused, function (w) { return w; }).slice(0, 12);
        lines.push("");
        lines.push("■ 系统没当成条件的词");
        lines.push(usageJoinCounts(pairs));
    }

    // ── 点开了哪些结果（含「一条都没点」）─────────────────────
    // 「有结果但一条都没点」必须和点击统计**同一小节**：它没有自己的标题时会串到上一节末尾，
    // 读的人会以为它跟上一节（未用词）有关 —— 真机 dump 出来一眼就看见了，代码里看不出来。
    const opens = events.filter(function (e) { return e.k === USAGE_KINDS.open; });
    const barren = usageBarrenSearches(events);
    if (opens.length || barren) {
        lines.push("");
        lines.push("■ 点开了哪些结果");
        if (opens.length) {
            const byWhat = usageCountBy(opens, function (e) { return USAGE_OPEN_LABELS[e.what] || e.what || "打开"; });
            lines.push(usageJoinCounts(byWhat));
            const b = usageRankBuckets(opens.map(function (e) { return e.r; }));
            const rankBits = [];
            if (b.first) rankBits.push("第1条 " + b.first);
            if (b.second) rankBits.push("第2条 " + b.second);
            if (b.third) rankBits.push("第3条 " + b.third);
            if (b.rest) rankBits.push("再往后 " + b.rest);
            if (rankBits.length) lines.push("点的是：" + rankBits.join(" · "));
        } else {
            lines.push("一条都没点开过");
        }
        if (barren) lines.push("有结果但一条都没点：" + barren + " 次");
    }

    // ── 产出物 ────────────────────────────────────────────
    const dls = events.filter(function (e) { return e.k === USAGE_KINDS.dl; });
    if (dls.length) {
        lines.push("");
        lines.push("■ 导出 / 下载");
        lines.push(usageJoinCounts(usageCountBy(dls, function (e) { return USAGE_DL_LABELS[e.what] || e.what || "导出"; })));
    }

    // ── 其它功能 ──────────────────────────────────────────
    const featureBits = [];
    const facets = usageCountBy(events.filter(function (e) { return e.k === USAGE_KINDS.facet; }), function (e) { return String(e.d || "细化"); });
    if (facets.length) featureBits.push("数据细化 " + facets.reduce(function (s, p) { return s + p[1]; }, 0) + "（" + usageJoinCounts(facets.slice(0, 5), " ") + "）");
    const relaxN = events.filter(function (e) { return e.k === USAGE_KINDS.relax; }).length;
    if (relaxN) featureBits.push("放宽条件 " + relaxN);
    const convPairs = usageCountBy(events.filter(function (e) { return e.k === USAGE_KINDS.conv; }), function (e) { return USAGE_CONV_LABELS[e.mode] || "对话记录"; });
    if (convPairs.length) featureBits.push(usageJoinCounts(convPairs));
    const undoN = events.filter(function (e) { return e.k === USAGE_KINDS.undo; }).length;
    if (undoN) featureBits.push("撤销 " + undoN);
    const favN = events.filter(function (e) { return e.k === USAGE_KINDS.fav; }).length;
    if (favN) featureBits.push("收藏 " + favN);
    if (featureBits.length) {
        lines.push("");
        lines.push("■ 用了哪些功能");
        lines.push(featureBits.join(" · "));
    }

    // ── AI 各层 ───────────────────────────────────────────
    // 「用上了」和「没能完成」必须分开报 —— 这正是 2026-07-29 那轮修的病根：
    // 把故障说成「未启用」，坏了也没人看得出来。反馈包同样不许把两者合成一个数。
    const ais = events.filter(function (e) { return e.k === USAGE_KINDS.ai; });
    if (ais.length) {
        const okPairs = usageCountBy(ais.filter(function (e) { return e.ok === true; }), function (e) { return USAGE_AI_LABELS[e.step] || e.step || "AI"; });
        const badPairs = usageCountBy(ais.filter(function (e) { return e.ok !== true; }), function (e) { return (USAGE_AI_LABELS[e.step] || e.step || "AI") + (e.why ? "（" + e.why + "）" : ""); });
        lines.push("");
        lines.push("■ AI 各层");
        if (okPairs.length) lines.push("真的用上了：" + usageJoinCounts(okPairs));
        if (badPairs.length) lines.push("没能完成：" + usageJoinCounts(badPairs));
    }

    // ── 检索失败 ──────────────────────────────────────────
    // 单列一段而不是并进上面的功能计数：用户那边连着失败而我这边一无所知，
    // 正是这个反馈功能最该解决的事，不该埋在一行小字里。
    const errs = events.filter(function (e) { return e.k === USAGE_KINDS.err; });
    if (errs.length) {
        lines.push("");
        lines.push("■ 检索直接失败：" + errs.length + " 次");
        const pairs = usageCountBy(errs, function (e) { return String(e.msg || "").trim(); }).slice(0, 3);
        pairs.forEach(function (p) { lines.push((p[1] > 1 ? p[1] + "× " : "") + p[0]); });
    }

    let text = lines.join("\n");
    let truncated = false;
    if (text.length > maxChars) {
        // 兜底截断（正常走上面的 maxQueryLines 省略就够了）。同样明说，不静默。
        const note = "\n（后面还有，为了能直接粘贴这里截断了）";
        text = text.slice(0, Math.max(0, maxChars - note.length)) + note;
        truncated = true;
    }

    return {
        text: text,
        chars: text.length,
        empty: false,
        truncated: truncated,
        omittedQueries: omittedQueries,
        omittedSearches: omittedHits,
        events: events.length,
        searches: searches.length,
    };
}

/* ============================================================================
 * 遥测上传包构造——仍保持纯核纪律：无 DOM / 无存储 /
 * 无墙钟 / 无网络。上传、门控、consent 全在 usage_upload.js（前端唯一出网模块）；
 * 这里只做「事件/记录数组 → 已脱敏的包」这一个纯变换，node 规格可逐字段断言。
 *
 * 脱敏是**结构性的**：与 benchfb 同一条红线——api_key 整键删除、端点只留主机名、
 * 不记密码/账户名。usage 事件本就不含这些字段（记录层有契约门盯着），这里的剔除
 * 是防御性兜底：万一未来某个打点把不该进的东西带进事件，构造上传包时也必须挡在门外。
 * 2026-08-22 起再加**值级遮蔽**：自由文本值里的手机号/证件号/邮箱正则打码，
 * 递归作用于整个包（usage 事件 + benchfb 记录 + mcp_records 中继记录）。
 * ========================================================================== */

export const TELEMETRY_SCHEMA = "biodata-telemetry/1";
export const TELEMETRY_CONTRACT_VERSION = 2;

/* 禁止入包的主键（防御性剔除）。与 benchfb 红线逐字对齐：api_key 系、密码、账户名/id；
   另扩 token|secret|authorization|cookie|email——账户凭证/会话类键
   一旦未来打点漂移进事件，构造上传包时同样必须挡在门外。
   大小写不敏感；只删**键**，不改值、不动其余字段——记录层采什么，这里就传什么。 */
export const TELEMETRY_STRIP_KEY_RE = /^(api[_-]?key|password|passwd|username|accountusername|account[_-]?(?:name|id)|token|secret|authorization|cookie|email)$/i;

/* 端点只留主机名（同 benchfb_core.benchfbEndpointHost 口径——那处是记录时的真源，
   这里只对漏网进包的原始 URL 兜底，非法/空一律空串，路径/查询串整段不采）。
   `new URL(s).host` 会**保留端口**（如 api.deepseek.com:8443）——这是有意为之
   （确认后维持现状）：自建代理/自托管服务的端口是复现检索现场所需，
   且端口本身不含敏感信息，刻意不剥。 */
function telemetryHost(url) {
    const s = String(url || "").trim();
    if (!s) return "";
    try { return new URL(s).host || ""; } catch (_e) { return ""; }
}

/* 字符串值级遮蔽：键级剔除挡不住「值里夹带」——用户查询原话、
   评语、MCP 调用参数这类自由文本里可能混进手机号/身份证号/邮箱。上传前对**整个包**
   （usage 事件 + benchfb 记录 + mcp_records）的每个字符串值递归过这三条正则。
   不用 lookbehind（老 Safari 不支持）：前导边界用捕获组保留下文再拼回。
   误伤代价可接受（查询里真含 11 位手机号段的串被换成占位符，分析不受影响），
   漏伤代价不可接受（明文证件号进了遥测库就是事故）。 */
const _MASK_PATTERNS = [
    { re: /(^|[^\d])1[3-9]\d{9}(?=[^\d]|$)/g, tag: "[手机号]" },
    { re: /(^|[^\dXx])\d{17}[\dXx](?=[^\dXx]|$)/g, tag: "[证件号]" },
    // 邮箱同样带边界捕获组：三条模式共用一个替换回调（p1=边界原样拼回），无组会把 offset 当 p1 拼进结果；
    // 边界还顺带挡住「前缀字母数字被吞进匹配」（如 邮zhang@example.com → 邮[邮箱]，而不是 [邮箱]）。
    { re: /(^|[^A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g, tag: "[邮箱]" },
    // API Key 形态遮蔽**追加在既有遮蔽之后**（usage 既有遮蔽语义不动），覆盖
    // sk-…（OpenAI 系，含 sk-ant-… 长串）、AKIA…（AWS）、Bearer token、ghp_…（GitHub PAT）
    // 四种常见泄漏形态；规则与接收端 app.py 追加的 _API_KEY_MASK_RULES 逐字同源——
    // feedback 正文在客户端（加密前第一层）与接收端（解密后第二层）双层共用它。
    { re: /(^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}(?=[^A-Za-z0-9_-]|$)/g, tag: "[API Key]" },
    { re: /(^|[^A-Za-z0-9])AKIA[0-9A-Z]{16}(?=[^A-Za-z0-9]|$)/g, tag: "[API Key]" },
    { re: /(^|[^A-Za-z0-9])(?:[Bb]earer)\s+[A-Za-z0-9._~+/=-]{20,}/g, tag: "[API Key]" },
    { re: /(^|[^A-Za-z0-9])ghp_[A-Za-z0-9]{36}(?=[^A-Za-z0-9]|$)/g, tag: "[API Key]" },
];
export function telemetryMaskString(text) {
    let s = String(text);
    _MASK_PATTERNS.forEach(function (m) {
        s = s.replace(m.re, function (_match, p1) { return (p1 || "") + m.tag; });
    });
    return s;
}

/* 深度脱敏：返回**新结构**（不改原数组/对象——调用方还要继续用）。
   - 对象：跳过命中的键；`base_url` 值只留主机名；其余键原样复制（再对值递归）。
   - 数组：逐元素递归。
   - 字符串：过值级遮蔽（手机号/证件号/邮箱）；其余标量原样返回。 */
function telemetryStrip(value) {
    if (Array.isArray(value)) return value.map(telemetryStrip);
    if (value && typeof value === "object") {
        const out = {};
        Object.keys(value).forEach(function (k) {
            if (TELEMETRY_STRIP_KEY_RE.test(k)) return;
            if (k === "base_url") { out.base_url = telemetryHost(value[k]); return; }
            out[k] = telemetryStrip(value[k]);
        });
        return out;
    }
    if (typeof value === "string") return telemetryMaskString(value);
    return value;
}

function _nullableText(value) {
    const text = String(value === undefined || value === null ? "" : value).trim();
    return text ? text.slice(0, 128) : null;
}

function _nullablePropensity(value) {
    if (value === undefined || value === null || value === "") return null;
    const n = Number(value);
    return Number.isFinite(n) && n > 0 && n <= 1 ? n : null;
}

/* 可选排序实验的确定性分臂纯核。armsText 每段：
   arm|weight|strategy|rerank|recall，段间用 ;。权重必须严格合计 1；任何半截/未知配置
   都返回 null，不把普通流量冒充实验。subject 是本机随机 profile id，只用于本地 hash，
   不因分臂额外上传。 */
export function telemetryExperimentAssign(subject, experimentId, armsText) {
    const eid = String(experimentId || "").trim();
    if (!/^[A-Za-z0-9._-]{1,64}$/.test(eid)) return null;
    const arms = [];
    const seen = new Set();
    let invalid = false;
    String(armsText || "").split(";").forEach(function (segment) {
        if (!segment.trim()) return;
        const p = segment.split("|").map(function (v) { return v.trim(); });
        const id = p[0], weight = Number(p[1]);
        const strategy = p[2] || "fixed", rerank = p[3] || "off", recall = p[4] || "off";
        if (!/^[A-Za-z0-9._-]{1,64}$/.test(id) || seen.has(id)
                || !Number.isFinite(weight) || weight <= 0 || weight > 1
                || ["fixed", "auto"].indexOf(strategy) < 0
                || ["off", "llm"].indexOf(rerank) < 0
                || ["off", "dense", "cross_encoder", "vector"].indexOf(recall) < 0) { invalid = true; return; }
        seen.add(id); arms.push({ id: id, weight: weight, strategy: strategy, rerank: rerank, recall: recall });
    });
    const total = arms.reduce(function (sum, arm) { return sum + arm.weight; }, 0);
    if (invalid || arms.length < 2 || Math.abs(total - 1) > 1e-9 || seen.size !== arms.length) return null;
    let h = 2166136261;
    const material = eid + "|" + String(subject || "");
    for (let i = 0; i < material.length; i++) h = Math.imul(h ^ material.charCodeAt(i), 16777619);
    const draw = (h >>> 0) / 4294967296;
    let cumulative = 0, chosen = arms[arms.length - 1];
    for (const arm of arms) { cumulative += arm.weight; if (draw < cumulative) { chosen = arm; break; } }
    return {
        experimentId: eid, experimentArm: chosen.id, propensity: chosen.weight,
        overrides: { strategy: chosen.strategy, rerank: chosen.rerank, recall: chosen.recall },
    };
}

function _contractRecord(value, opts) {
    const clean = telemetryStrip(value && typeof value === "object" ? value : {});
    clean.contract_version = TELEMETRY_CONTRACT_VERSION;
    clean.prompt_version = _nullableText(clean.prompt_version !== undefined ? clean.prompt_version : opts.promptVersion);
    clean.experiment_id = _nullableText(clean.experiment_id !== undefined ? clean.experiment_id : opts.experimentId);
    clean.experiment_arm = _nullableText(clean.experiment_arm !== undefined ? clean.experiment_arm : opts.experimentArm);
    clean.propensity = _nullablePropensity(clean.propensity !== undefined ? clean.propensity : opts.propensity);
    return clean;
}

/* 上传包构造函数。纯函数：时间/环境信息一律由调用方（usage_upload.js）注入。
   opts.exportedAt（ISO 串）、opts.installId/clientId/profileId/packetId、
   opts.app = {cache_generation, ua, lang}、
   opts.mcpRecords（2026-08-22：同源 /api/telemetry/mcp-calls 取回的中继记录，
   非空数组才附进顶层 mcp_records 字段——接收端顶层 extra="forbid"，该字段的收编
   由接收端包配套；记录同样过深度脱敏 + 值级遮蔽）。
   usage_events / benchfb_records / mcp_records 都过深度脱敏；非数组输入按空数组处理（绝不抛）。 */
export function buildTelemetryPackage(usageEvents, benchfbRecords, opts) {
    opts = opts || {};
    const pkg = {
        schema: TELEMETRY_SCHEMA,
        contract_version: TELEMETRY_CONTRACT_VERSION,
        packet_id: String(opts.packetId || ""),
        install_id: String(opts.installId || ""),
        client_id: String(opts.clientId || ""),
        profile_id: String(opts.profileId || ""),
        exported_at: String(opts.exportedAt || ""),
        prompt_version: _nullableText(opts.promptVersion),
        experiment_id: _nullableText(opts.experimentId),
        experiment_arm: _nullableText(opts.experimentArm),
        propensity: _nullablePropensity(opts.propensity),
        training_consent: opts.trainingConsent === true,
        app: {
            cache_generation: String((opts.app && opts.app.cache_generation) || ""),
            ua: String((opts.app && opts.app.ua) || ""),
            lang: String((opts.app && opts.app.lang) || ""),
        },
        usage_events: (Array.isArray(usageEvents) ? usageEvents : []).map(function (r) { return _contractRecord(r, opts); }),
        benchfb_records: (Array.isArray(benchfbRecords) ? benchfbRecords : []).map(function (r) { return _contractRecord(r, opts); }),
    };
    if (Array.isArray(opts.mcpRecords) && opts.mcpRecords.length) {
        pkg.mcp_records = opts.mcpRecords.map(function (r) { return _contractRecord(r, opts); });
    }
    if (opts.dropReport && Number(opts.dropReport.dropped_count) > 0) {
        pkg.drop_report = telemetryStrip({
            revision: Math.max(0, Math.floor(Number(opts.dropReport.revision) || 0)),
            dropped_count: Math.max(0, Math.floor(Number(opts.dropReport.dropped_count) || 0)),
            by_queue: opts.dropReport.by_queue || {},
        });
    }
    return pkg;
}

/* ESM 出口：本文件全部经 export 提供；node 规格（tests/js/usage_core_spec.mjs）直接 import。
   纯核自己不依赖任何界面层——USAGE_KINDS 的消费方全部经 import 直取（保住零依赖纯度门）。 */
