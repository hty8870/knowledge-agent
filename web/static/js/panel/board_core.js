"use strict";
/* 条件板的纯逻辑核：四分区归并、撤销栈、摘要句。
   零依赖——不碰 DOM、不碰本地存储、不发网络请求、不读系统时钟（时间相关的一律由调用方传进来），
   因此可以被 node 直接跑真行为规格（三门都不执行 JS，前端排序/归并这类数学没有回归网就等于没测）。
   这几条禁令由 tests/js/board_core_spec.mjs 末尾的扫描钉死，所以本文件正文里也不出现那几个名字。

   与后端 `board.board_view` 是**同一份规则的两处实现**：浏览器要在每次检索后立刻画出条件板，
   不可能为了拿几行字再往返一次服务端。两边的漂移由 `tests/test_board_core_behavior.py` 里的
   跨语言对拍钉死——同样的入参，两边必须产出同样的行。 */

export var CB_DIM_LABEL = {
    species: "物种", tissue: "组织", disease: "疾病",
    platform: "平台", assay: "技术", modality: "模态",
    has_raw_data: "原始数据", date: "发表时间"
};
/* 撤销栈上限（2026-08-08 约束放松批 20→50：帧只是筛选态快照、纯内存不落盘，
   实测 50 帧×20 条响应下限约 3.5MB，现代浏览器无感；回放只渲染当前帧）。 */
export var CB_MAX_FRAMES = 50;

/* 筛选项编号 → 中文条件名。与后端 _label_for_filter_id 同构：
   已忽略的行拿不到取值（取值在被忽略后已不在返回里），只能从编号还原条件名。 */
export function cbLabelForFilterId(filterId) {
    var fid = String(filterId || "");
    // prefer 先判：它的 dim 段是 raw / date / source，会被下面两条前缀判定和 CB_DIM_LABEL
    // 一起还原成「原始数据」「发表时间」这种**硬条件**的名字，那就把「只排前面」说成了「筛掉了别的」。
    if (fid.indexOf("prefer:") === 0) {
        var pdim = fid.slice("prefer:".length);
        if (pdim === "raw") return "优先·原始数据";
        if (pdim === "source") return "优先·数据来源";
        return "优先·" + (CB_DIM_LABEL[pdim] || pdim);
    }
    if (fid.indexOf("raw:") === 0) return CB_DIM_LABEL.has_raw_data;
    if (fid.indexOf("date:") === 0) return CB_DIM_LABEL.date;
    var parts = fid.split(":");
    var polarity = parts.length > 1 ? parts[0] : "include";
    var dim = parts.length > 1 ? parts[1] : fid;
    var base = CB_DIM_LABEL[dim] || dim;
    return polarity === "exclude" ? ("排除·" + base) : base;
}

/* 四个分区的行。**只是再投影**：每一行的取值都直接来自上一次检索的返回或三个开关，
   一个数字都不在这里算。 */
export function cbRowsFrom(activeFilters, facetFilters, lenientDims, suppressed, coverageDims) {
    var rows = [];
    var coverage = {};
    (coverageDims || []).forEach(function (d) { coverage[String(d)] = true; });

    (activeFilters || []).forEach(function (item) {
        if (!item || typeof item !== "object") return;
        var dim = String(item.dim || "");
        var polarity = String(item.polarity || "include");
        var structural = CB_DIM_LABEL.hasOwnProperty(dim) && dim !== "has_raw_data" && dim !== "date";
        rows.push({
            // 软偏好单独一个分区：它**不筛掉任何数据**，只把命中的排前面。
            // 混进 query 分区会被摘要行数成「正在按 N 个条件筛选」的一员，
            // 用户读完会以为结果都满足这一条——那正是「优先」最容易被误解的地方。
            zone: polarity === "prefer" ? "prefer" : "query",
            dim: dim,
            label: String(item.label || cbLabelForFilterId(item.filter_id || (polarity + ":" + dim))),
            values: (item.values || []).map(String),
            filter_id: String(item.filter_id || ""),
            polarity: polarity,
            removable: !!item.filter_id,
            editable: structural && polarity === "include",
            lenientable: structural && !!coverage[dim],
            note: ""
        });
    });

    (facetFilters || []).forEach(function (item) {
        if (!item || typeof item !== "object") return;
        var dim = String(item.dim || "");
        rows.push({
            zone: "facet",
            dim: dim,
            label: String(item.label || CB_DIM_LABEL[dim] || dim),
            values: [String(item.display || item.value || "")],
            filter_id: "",
            polarity: "include",
            removable: true,
            editable: false,
            lenientable: false,
            note: ""
        });
    });

    (lenientDims || []).forEach(function (dim) {
        rows.push({
            zone: "lenient",
            dim: String(dim),
            label: CB_DIM_LABEL[String(dim)] || String(dim),
            values: [],
            filter_id: "",
            polarity: "include",
            removable: true,
            editable: false,
            lenientable: false,
            note: ""
        });
    });

    (suppressed || []).forEach(function (fid) {
        var s = String(fid);
        var parts = s.split(":");
        rows.push({
            // 停用一条**排序偏好**不能说成「没有用它筛」——它从来就没筛过。
            // 两种停用放同一分区，读起来像是刚刚放宽了一个筛选条件、结果集会变大，而其实一条都没变。
            zone: s.indexOf("prefer:") === 0 ? "prefer_off" : "suppressed",
            dim: parts.length > 1 ? parts[1] : s,
            label: cbLabelForFilterId(s),
            values: [],   // 取值在被忽略之后已不在返回里，显示旧值就是拿过期快照当事实
            filter_id: s,
            polarity: s.indexOf("exclude:") === 0 ? "exclude"
                : (s.indexOf("prefer:") === 0 ? "prefer" : "include"),
            removable: true,
            editable: false,
            lenientable: false,
            note: ""
        });
    });
    return rows;
}

/* 摘要行的那句话。两个数字都从入参来——前端一个都不自己算。
   刻意写「当前结果 N 条」而不是「库中匹配 N 条」：这个数是套过「在结果里再缩小」之后的存活数，
   勾了分面还说「库中」就是谎报口径。 */
export function cbSummary(rows, resultTotal) {
    var active = 0, suppressedCount = 0, lenientCount = 0, preferCount = 0, preferOffCount = 0;
    (rows || []).forEach(function (r) {
        if (!r) return;
        // 「已放宽」不是一个**额外的**条件，它是对某个已经在算的条件的松绑：
        // 放进 active 里，用户点一下「放宽」，屏幕上的「正在按 N 个条件筛选」反而变大，
        // 读起来像是刚刚多加了一条限制。它单独报，且报成「其中」。
        if (r.zone === "suppressed") suppressedCount += 1;
        else if (r.zone === "prefer_off") preferOffCount += 1;
        else if (r.zone === "lenient") lenientCount += 1;
        else if (r.zone === "prefer") preferCount += 1;   // 只排序、不筛选 → 绝不计入筛选条件数
        else active += 1;
    });
    var parts = [];
    parts.push(active > 0 ? ("正在按 " + active + " 个条件筛选") : "当前没有任何筛选条件");
    if (lenientCount > 0) parts.push("其中 " + lenientCount + " 项已放宽");
    if (preferCount > 0) parts.push("另有 " + preferCount + " 项只用来排先后，没有筛掉数据");
    if (suppressedCount > 0) parts.push("另有 " + suppressedCount + " 个这次没有用它筛");
    // 停用一条排序偏好，结果集一条都不会变。说成「没有用它筛」会让人以为放宽了筛选、该多出结果，
    // 然后发现数字纹丝不动 —— 那种「说了话却什么都没发生」正是最伤信任的一种。
    if (preferOffCount > 0) parts.push("另有 " + preferOffCount + " 项排序偏好这次没有生效");
    var head = parts.join("，");
    if (typeof resultTotal === "number" && resultTotal >= 0) head += " · 当前结果 " + resultTotal + " 条";
    return head;
}

/* 推一帧进撤销栈。返回 {stack, cursor} 二元组——只返回栈会让「丢最旧那一帧时游标怎么动」无人定义，
   连推满之后游标会越界一格，而只断言栈长度的测试照样是绿的。
   在游标不在栈顶时推帧，会先丢掉游标之后的帧（那是被放弃的分支）。 */
export function cbPushFrame(stack, cursor, frame) {
    var next = (stack || []).slice(0, (typeof cursor === "number" && cursor >= 0) ? cursor + 1 : 0);
    next.push(frame);
    while (next.length > CB_MAX_FRAMES) next.shift();
    return { stack: next, cursor: next.length - 1 };
}

/* 在撤销栈上走一步。两端夹紧，空栈是 no-op。 */
export function cbGo(stack, cursor, delta) {
    var list = stack || [];
    if (!list.length) return { cursor: -1, frame: null };
    var target = (typeof cursor === "number" ? cursor : list.length - 1) + (delta || 0);
    if (target < 0) target = 0;
    if (target > list.length - 1) target = list.length - 1;
    return { cursor: target, frame: list[target] || null };
}

/* 这一步到底改了什么没有——用来在「什么都没变」时不推新帧、也不谎报「已改」。 */
export function cbIsNoop(before, after) {
    if (!before || !after) return false;
    var same = function (a, b) { return JSON.stringify(a || []) === JSON.stringify(b || []); };
    return String(before.query || "") === String(after.query || "")
        && same(before.suppressed_constraints, after.suppressed_constraints)
        && same(before.lenient_dims, after.lenient_dims)
        && same(before.facet_filters, after.facet_filters)
        && String(before.date_from || "") === String(after.date_from || "")
        && String(before.date_to || "") === String(after.date_to || "");
}

/* ==================== 每条系统回复的反馈操作条（2026-08-28 msgfb） ====================
   赞 / 倒赞 / 评论 / 分支 四键的**纯逻辑**部分。UI 壳在 board.js（cbRenderHistory 渲染、
   cbHistoryClick 接线），这里只放确定性计算，好让 node 直接跑行为规格。 */

/* 赞/倒赞互斥切换：点已选中的那颗 = 取消（回 ""）；点另一颗 = 换边。
   返回值只有 ""|"up"|"down" 三态，调用方把它原样存回 entry.msgFb 并打点。 */
export function cbMsgFbNext(current, action) {
    var cur = (current === "up" || current === "down") ? current : "";
    if (action !== "up" && action !== "down") return cur;
    return cur === action ? "" : action;
}

/* 评论入队文本：用户正文 + 一行指向所评回复的引用尾（消息 id + 内容摘段），
   让开发者能把意见对回具体那句回复。摘段截断到 maxSnippet 字（默认 60）。
   只组装纯文本；遮蔽/截断/幂等仍由 feedback_core.feedbackEnqueue 兜底。 */
export function cbMsgCommentText(userText, mid, snippet, maxSnippet) {
    var text = String(userText || "").trim();
    var snip = String(snippet || "").replace(/\s+/g, " ").trim();
    var cap = (typeof maxSnippet === "number" && maxSnippet > 0) ? maxSnippet : 60;
    if (snip.length > cap) snip = snip.slice(0, cap) + "…";
    var ref = "—— 针对回复「" + (snip || "（原文不在本地）") + "」（" + String(mid || "") + "）";
    return text + "\n" + ref;
}

/* 这条系统回复能不能作为分支点：只有挂在存活检索帧上的回复才能分支
   （?fork= 机制按「前 N 轮检索快照」重建，纯对话回音之前没有任何快照可重建）。
   frameLinked 由调用方算（frameId 是否还在帧栈里），这里只合并两个判据。 */
export function cbMsgForkable(frameId, frameLinked) {
    return frameId != null && frameLinked === true;
}

/* ==================== 检索落地回执句（条件板/执行层共用锚点） ==================== */

/* 「库中共 N 条匹配，结果区展示前 M 条」——检索落地事实句的唯一真源。
   facts = {total, shown}；prefixZh 由调用方定（完成回执「检索完成：」、执行汇报「前置检索：」）。
   零结果兜底「这次检索没有找到匹配的记录。」只在未自带零结果多口径分支的调用路径浮现
   （条件板主回执的零结果按 resolution_status 分口径，在调用侧另行分支，不进本函数）。 */
export function searchFactsReceiptText(facts, prefixZh) {
    var total = Number(facts && facts.total) || 0;
    if (!total) return "这次检索没有找到匹配的记录。";
    return String(prefixZh || "") + "库中共 " + total + " 条匹配，结果区展示前 "
        + (Number(facts && facts.shown) || 0) + " 条。";
}

/* ==================== 纯检索计划判定（条件板/执行层共用锚点） ==================== */

/* 唯一气泡规则的判定件：检索族动词（环内 rank/rerank/search.rerun）。
   纯检索计划 → 批次回执就是最终总结（actFinish 闭嘴）；混合计划 → actFinish 是最终总结
   （批次回执闭嘴）。两分支都只有一颗气泡。 */
export var PLAN_RETRIEVAL_VERBS = { "rank": true, "rerank": true, "search.rerun": true };

/* 计划是否纯检索：steps 非空取逐步 verb，否则取 plan.verb；verb 全集落在检索族里才算。
   无 plan / 取不到任何 verb → false（按非纯检索处理，不抑制任何回执）。 */
export function planIsRetrievalOnly(plan) {
    if (!plan || typeof plan !== "object") return false;
    var verbs;
    if (Array.isArray(plan.steps) && plan.steps.length) {
        verbs = plan.steps.map(function (s) { return String((s && s.verb) || "").trim(); }).filter(Boolean);
    } else {
        var v = String(plan.verb || "").trim();
        verbs = v ? [v] : [];
    }
    return verbs.length > 0 && verbs.every(function (v) { return !!PLAN_RETRIEVAL_VERBS[v]; });
}

/* 规范派发清单是否会产生执行回执：任一动作的自身 verb 落在检索族之外即 true。
   唯一气泡规则的另一半判定件——判定输入必须是 act.js 规范链的真实派发清单
   （actCanonicalDispatchPlans：顶层 plan/兼容 intents + pending_frontend 去重、
   并滤除图内已执行的检索头部项后），不是 plan.steps：steps 只是「后端已在图内执行」
   的记录，拿它判会把混合轮误当纯检索。
   动词身份只看 item.verb（本动作要做什么），与该动作 steps 里记过的检索工具无关。
   空清单/非数组 → false（没有执行，谈不上执行回执）。 */
export function plansNeedActReceipt(plans) {
    if (!Array.isArray(plans)) return false;
    return plans.some(function (p) {
        return !!p && !PLAN_RETRIEVAL_VERBS[String(p.verb || "").trim()];
    });
}

/* 规范派发清单的头部过滤判据（2026-09-03 真机核实）：route=tool 混合轮的顶层 plan 是
   环内已跑完的 rank（steps 记 ok:true），真身动作排在 pending_frontend——「自身 verb
   属检索族且同一 verb 已在 plan.steps 以 ok:true 执行」的头部项不是待派发动作，
   派发它只会经「图内已执行渲染通道」把已落地的检索再渲染一颗 actFinish 泡（双泡真根因）。
   两类 false：非检索动词（compare 等图内执行动作——卡片与总结只靠渲染通道上屏）；
   检索步 ok 非 true（失败步留着如实渲染）。pending_frontend 尾巴不经本判据——
   那是图显式排队的图后接力，信任它。 */
export function planHeadIsGraphDoneRetrieval(item, plan) {
    if (!item || !plan) return false;
    var v = String(item.verb || "").trim();
    if (!PLAN_RETRIEVAL_VERBS[v]) return false;
    var steps = Array.isArray(plan.steps) ? plan.steps : [];
    return steps.some(function (s) {
        return s && s.ok === true && String(s.verb || "").trim() === v;
    });
}

/* ==================== 取消态回音（条件板/执行层共用锚点） ==================== */

/* 取消态 reason_zh 缺省时的前端兜底句——与后端 action_plan.py 极性门的
   「你说先不做这一步，所以这次没有执行。」逐字同源，改一边必须同步另一边。 */
export var PLAN_CANCELLED_FALLBACK_ZH = "你说先不做这一步，所以这次没有执行。";
