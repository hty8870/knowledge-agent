"use strict";

/* 本文件是 ES Module：core/shell/results/facets/progress/usage 层经 import 取
   （results 的四个分面态是 live binding，本文件只读）。interactions 的 SOURCES_READY/
   queryForRetrieval/getDateRange/updateSrcSummary/updateTimeSummary/setLastInterpretation 与
   board/task_pack/act 的 renderCondBoard/cbLogPush/cbLogClear/cbPushCurrent/resetTaskPack/
   actAfterSearch 经 import 取（互调成环——绑定都只在函数体内使用，ESM 允许）。
   board/task_pack/act/accounts/memory/results/facets 与 browse/interactions 经 import
   取本文件导出（绞杀桥已全退役）。 */
import { API, $, MOTION, cacheGeneration, pushHist, toast } from "#core";
import { placeFacetBar, renderFacets } from "#facets";
import { QCACHE_MAX, _queryCache, estimateDuration, finishProgress, resetSubmitButton, startProgress } from "#progress";
import { _facetFilters, _lenientDims, _queryHits, _suppressed, renderResults, resetActionHint, resetFacetState } from "#results";
import { getConfig, renderStatus } from "#shell";
import { USAGE_KINDS, usageActiveTurnId, usagePolicyRef } from "#usage_core";
import { usageExperimentContext, usageLog, usageLogSearch, usageEnabled, usageSetEnabled, usageConsentGiven, requestUsageConsent, usageScope } from "#usage_log";
import { benchfbTurnError, benchfbTurnSearch } from "#benchfb";
import { SOURCES_READY, getDateRange, queryForRetrieval, setLastInterpretation, updateSrcSummary, updateTimeSummary } from "#interactions";
import { cbArchiveChatOnly, cbLogClear, cbLogPush, cbProgressBegin, cbProgressDone, cbProgressRelabel, cbPushCurrent, renderCondBoard } from "#board";
import { resetTaskPack } from "#task_pack";
import { actAfterSearch } from "#act";

export let LAST_RECOMMEND_DATA = null;
/* 属主写口（约定 #4：可变跨模块状态他人写必经 setter）：board.js 对齐回当前帧、
   interactions.js 输入即失效，都经它写；消费方经 import 拿这个函数。 */
export function setLastRecommendData(v) { LAST_RECOMMEND_DATA = v; }
/* owner 新指①的落地侧（2026-08-04）：统一路由来的检索句在**结果落地时**才回填进输入框
   （框=「当前检索句」真源：任务包/可行性/词表回显读它），在途窗口保持「发送即空」。
   守卫与 board.js ubFillQuery 同口径：框空、或框里仍是被发送的那句（sentText）才回写；
   用户趁在途打了新草稿 → 保留草稿，绝不冲掉（B）。 */
function _ubLandingFill(query, sentText) {
    const box = $("queryInput");
    if (!box) return;
    const cur = String(box.value || "").trim();
    if (cur && cur !== String(sentText || "").trim()) return;
    box.value = query;
}
function queryCacheKey(body) {
    const { api_key, ...rest } = body;
    const norm = {};
    // 数组值规范化：元素若是对象（如 facet_filters 的 {dim,value}）先 stable-stringify 再排序，
    // 使「同一组过滤、不同点选顺序」映射到同一键——否则默认 .sort() 对对象数组是 no-op、会漏缓存。
    Object.keys(rest).sort().forEach((k) => {
        const v = rest[k];
        norm[k] = Array.isArray(v)
            ? v.map((x) => (x && typeof x === "object") ? JSON.stringify(x) : x).sort()
            : v;
    });
    return JSON.stringify(norm);
}
function queryCacheGet(key) {
    if (!_queryCache.has(key)) return null;
    const v = _queryCache.get(key);
    _queryCache.delete(key); _queryCache.set(key, v);   // LRU：命中即置最新
    return v;
}
function queryCacheSet(key, data) {
    _queryCache.set(key, data);
    while (_queryCache.size > QCACHE_MAX) _queryCache.delete(_queryCache.keys().next().value);   // 逐出最旧
}
/* （设计 §2.5.1）：「初步结果」徽标的唯一写口——任何结果重渲（落地/历史回看/回退）
   都先经 applyRecommendResult 摘徽标；preliminary 帧落地（opts.fromPrelim）再由共享入口重新亮上。 */
function _prelimBadge(show) {
    const b = $("prelimBadge");
    if (b) b.hidden = !show;
}
/* board.js 的 final b 档（preliminary_final：先行结果即最终结果）不经过落地也要摘徽标 → 导出写口。 */
export function setPrelimBadge(show) { _prelimBadge(show); }
/* 当前检索参数快照（设计 §2.4）：runRecommend 发 /api/recommend 与 board.js
   ubRouteBody 发 /api/utterance（后端 pre-loop 检索用）**同源构造**——加字段时两处一起变，
   不许两处口径漂移。polish（2026-08-16）：后端 preliminary_final 判定要看
   「AI 润色会不会跑」——b 档（先行=最终）只在无采纳 ∧ 无改写 ∧ rerank=off ∧ 不润色时成立。 */
export function searchParamSnapshot(query) {
    const cfg = getConfig();
    const experiment = usageExperimentContext();
    const params = Object.assign({
        top_k: cfg.top_k, rerank: cfg.rerank, recall: cfg.recall, strategy: cfg.strategy,
        polish: cfg.polish,
        facet_filters: _facetFilters.map((f) => ({ dim: f.dim, value: f.value })),
        suppressed_constraints: _suppressed.slice(),
        lenient_dims: _lenientDims.slice()
    }, getDateRange(query));
    if (experiment) {
        params.experiment_id = experiment.experimentId;
        params.experiment_arm = experiment.experimentArm;
        params.propensity = experiment.propensity;
        if (experiment.overrides) Object.assign(params, experiment.overrides);
    }
    return params;
}
/* 渲染一份推荐结果（新拉取 or 缓存命中共用），把结果落到状态卡/列表/历史/折叠 pill 并顶部对齐。
   opts.noScroll：分面细化重跑时不滚回顶部（用户正看着结果，避免每次点芯片都跳动）。 */
export function applyRecommendResult(data, query, opts) {
    opts = opts || {};
    setLastInterpretation((data && data.interpretation) || null);   // 属主是 interactions.js（ESM）：写必经 setter
    LAST_RECOMMEND_DATA = data || null;
    _prelimBadge(false);   // 任何结果重渲先摘「初步结果」徽标（fromPrelim 落地由共享入口随后亮回）
    renderStatus(data);
    // （schema v3 imp）：展示归因上下文随渲染下发——policy 优先用后端响应里的
    // policy_id（缓存命中时就是缓存响应里那份，与当屏结果严格同源），没给再按请求参数现组合；
    // 历史回看（fromHistory 且非批次内切换 keepTurn）不属于任何一轮检索，tid 如实为 null。
    const _pp = opts.policyParts || {};
    const _imp = {
        tid: (opts.fromHistory && !opts.keepTurn) ? null : usageActiveTurnId(),
        policy: usagePolicyRef(data,
            { strategy: _pp.strategy, rerank: _pp.rerank, recall: _pp.recall, gen: cacheGeneration() }),
    };
    renderResults(data.results || [], data, _imp);
    renderFacets(data);   // 结果上方的分面面板（面包屑 + 可细化维度）
    if (!opts.fromHistory) pushHist(query, data, _facetFilters, _suppressed, _queryHits, _lenientDims, opts.replaceHist);   // 存下这次的结果快照（回看历史时不再回灌）；replaceHist=分面细化重跑→原地更新，新查询→新行
    // 折叠 pill 的「最终落到的筛选条件」——两者都只据输入文字判定（源/时间对称，未点名即「不限」），不看结果集
    updateSrcSummary();
    updateTimeSummary();
    if (!opts.noScroll) window.scrollTo({ top: 0, behavior: "smooth" });   // 顶部对齐：hero 上移后搜索框 + 结果同框可见
}
/* 检索落地共享入口（2026-08-16 检索工具化，设计 §4）：runRecommend 的两个落地点与
   零命中救回的换屏**同走这一个函数**——applyRecommendResult 重渲 + renderCondBoard 条件板 +
   cbPushCurrent 推帧 + _ubLandingFill 回填，不许第二调用方各抄一份。
   推帧/条件板只在本函数做，绝不放进 applyRecommendResult：那里同时是「回到上一步」
   「从左侧历史回看」「切账户重渲」三条路径的落点，放进去会让点两次上一步原地不动、
   也会把三天前的一条历史当成对话的下一步推进栈。
   帧语义：每次调用 = 一次新落地压一个新帧。零命中帧也留一份在栈里——「回到上一步」
   如实回到零命中态，不装成那次检索从没发生过。
   opts.resetFacets：调用方语义是「一次新查询」时先清四个分面态（救回=换新查询词，true；
   runRecommend 自己已在请求开头按 keep 口径清过，false）。
   2026-08-16 初步结果先行（设计 §2.5.1）新增三档 opts：
   opts.fromPrelim：本帧是 utterance 流 preliminary 事件的 pre-loop 先行结果——亮「初步结果」
   徽标 + 进度泡换句「正在更深一步思考…」（环还在跑，cbPushCurrent 用 keepProgress 按住
   进度泡不蜕变）；
   opts.keepProgress：落地但不许进度泡蜕变成完成摘要（final a 档的环内采纳换屏——
   完成话术由调用方另行留痕）；opts.fadeIn：换屏补一次淡入（第二次 land 本无动画，
   参数与 results.js 首揭 firstReveal 同款）。
   零命中落地不再自动跑救回门禁——救回选项由 board.js 选择条呈现（零命中 pill
   「点击处理」），诚实回执（disclosure_zh）仍在落地路径上。 */
export function landRecommendResult(data, query, opts) {
    opts = opts || {};
    if (opts.resetFacets) resetFacetState();
    applyRecommendResult(data, query, opts);
    renderCondBoard(data);
    // 2026-08-18：planSteps（utterance final 帧的环内工具记录）的 verb 随帧落地——
    // cbPushCurrent 的完成句据此带工具调用摘要（纯检索环内 rank/rerank 也如实报
    // 「执行了 N 次检索」；无环工具时 null → 摘要空串不渲染）。
    const _tv = (opts.planSteps || []).map(function (s) { return s && s.verb; }).filter(Boolean);
    // actPending（2026-08-30 混合轮单泡化）：「先检索后派发」档（runRecommend 据
    // opts.actPlan 透传）——cbPushCurrent 据此**抑制**检索模板回执（进度泡留给 actDispatchPlan
    // 接管，检索事实并入执行汇报一颗泡）。sentText 透传供检索回执的 LLM 事实包取用户原话。
    cbPushCurrent(data, query, { keepProgress: !!(opts.fromPrelim || opts.keepProgress),
        toolVerbs: _tv.length ? _tv : null, actPending: !!opts.actPending, sentText: opts.sentText });
    _ubLandingFill(query, opts.sentText);   // 落地才回填「当前检索句」（B 守卫），在途窗口保持发送即空
    // 零命中救回不再自动跑——零命中时的放宽/换词选项改由 board.js 的救回选择条
    // 呈现（零命中 pill「点击处理」），用户点了才作为下一句走既有管线，不再自动发
    // /api/agent/search-rescue、也不自动产 sys 气泡。诚实回执（disclosure_zh）仍在地段上屏。
    if (opts.fromPrelim) {
        _prelimBadge(true);
        cbProgressRelabel("正在更深一步思考…");
    }
    if (opts.fadeIn && MOTION) gsap.from("#resultsWrap", { autoAlpha: 0, y: 10, duration: 0.45, ease: "power2.out", clearProps: "all" });
}

/* ---- 零命中救回链已退役（2026-08-24）----
   自动救回（发 /api/agent/search-rescue + sys 气泡）退役：零命中时的救回选项改由
   board.js 的救回选择条呈现（零命中 pill「点击处理」），用户点了才作为下一句走既有管线。
   此处不再有 maybeSearchRescue / handleSearchRescue / _rescueSeq 等；诚实回执仍在落地路径上。
   /api/agent/search-rescue 端点保留（后端与 action_plan 豁免清单仍列），前端不再自动调用。 */
/* 请求代号：分面芯片在请求进行中仍可点（有意保持响应），故可能并发多个 runRecommend。
   每次进入自增、捕获本次 myGen；只有「仍是最新一次」的响应/错误/收尾才落地——
   丢弃晚到的旧响应，杜绝「结果/计数属于旧筛选、面包屑却是新筛选」的错配。 */
let _recSeq = 0;
/* 请求代号自增的属主写口：browse.js 历史回看、board.js cbReplay 都要作废在途请求（同 `++_recSeq`），
   消费方经 import 拿它（可变状态只许属主写——外部 `_recSeq += 1` 裸写在对 import 绑定的赋值上本就会 TypeError）。 */
export function bumpRecSeq() { _recSeq += 1; }
/* ubSubmit 发送时快照检索代（preliminary 帧落地前的代际闸——用户在环跑期间
   点了分面重跑，晚到的先行帧不得盖新屏）。只读，不写。 */
export function recSeqNow() { return _recSeq; }
export async function runRecommend(opts) {
    opts = opts || {};
    // 统一路由（ubSubmit 分发）的检索句经 opts.queryOverride 显式到来——不读输入框：
    // 发送即空贯穿整个在途窗口（owner 新指①），框不再是这条路在途期的取数口。
    // 其余全部调用点（分面 chip / 放宽 / 历史重跑 / 回退……）照旧读框里的「当前检索句」。
    const query = String(opts.queryOverride || ($("queryInput").value || "")).trim();
    if (!query) { toast("请先输入检索需求"); return; }
    const telemetryScope = usageScope();   // 请求全生命周期固定；账户切换不改变晚到回调归属
    // consent 首次告知（设计 §3）：开关开着且没同意过 → 弹窗拦截，不点确认不发送。
    // 开关关着（usageEnabled() 为假）不弹；'disable' → 关掉采集开关，本次照常发送。
    if (usageEnabled() && !usageConsentGiven()) {
        const r = await requestUsageConsent();
        if (r === "disable") usageSetEnabled(false);
    }
    // 新查询（非分面细化重跑）清空既有分面筛选 + 原始命中抑制表 + 命中快照：三者都是「针对某次查询」，换查询即失效（快照随纯净结果重建）。
    // fromBoard 是条件板刚刚**算好并写进这四个全局**的一步改动，清空会把它自己的改动抹掉；
    // 不传 opts 时 keep 恒 false，与本行原本的行为逐位相同。
    const keep = !!(opts.keepFacets || opts.fromBoard);
    // 新查询（非细化重跑）＝一条新时间线：清空既有分面/抑制/命中/宽容，也清空侧栏的对话记录，
    // 并把这句原始查询作为对话记录的开头（后续细化/追问接在它下面）。fromBoard 是条件板刚算好的一步，keep=true 不清。
    // keepConv（2026-08-02 图5）：分面/抑制照旧清空，但**对话记录保留**——
    // 「接着这句往下走」的重跑不是「另起一段对话」，整段对话被清掉正是用户报的异常。
    // 接线方在 board 路径（原回执 chip「按原话重新检索」已于 2026-08-16 退役，机制保留）。
    if (!keep) {
        resetFacetState();   // 四个分面状态的属主是 results.js（ESM）：重赋值必经属主 setter
        resetActionHint();   // 新时间线：上一段对话的指路条核销态一并失效（属主同为 results.js）
        // 2026-08-04：清对话前先归档「仅对话」——纯工具对话从未走过 pushHist，
        // 这里不清仓补一行就永久丢失（hero 首句自己那句 say 会被 cbArchiveChatOnly 识别跳过）。
        // 排除串必须是**那句 say 本身**（opts.sayText=用户原话）：hero 首句被 LLM 改写时
        // query 已是改写句，比错对象会误产幽灵「仅对话」行（同一句在历史里出现两次）。
        if (!opts.keepConv) { cbArchiveChatOnly(String(opts.sayText || query)); cbLogClear(); }
        // sayPushed（turn pipeline）：统一路由分发前原话 say 已上屏，这里不再重复推；
        // sayText（同）：hero 首句被 LLM 改写时，对话开头的 say 仍记**用户原话**
        // （改写句由 ubDispatch 的 sys 回显如实交代），不许把改写的句子记成用户说的。
        if (!opts.sayPushed) cbLogPush("say", String(opts.sayText || query));
        // 进度泡：新检索（hero 首查 / chat 判成新检索）也有「发送即回一颗进度泡、落地渐变成完成摘要」。
        // chat 改条件路径在 ubSubmit 已开（幂等）。
        cbProgressBegin();
    } else {
        // 改条件重跑（加入/去掉/忽略/放宽/条件板提交…，keep=true）：用户的动作已作为一条对话消息上屏，
        // 系统必须回一句——开进度泡，落地时原位蜕变成完成摘要（此前这类路径从不开泡，
        // cbPushCurrent 的 cbProgressDone 空转，「加入：…」这类用户消息永远没有系统回复）。
        // 幂等：ubSubmit / cbCommit 已开泡的路径（label 与 _cbProgHint）原样保留。
        cbProgressBegin();
    }
    const myGen = ++_recSeq;
    // 等来源清单就绪再取配置：否则 /api/sources 返回前 getSelectedSources 会兜底成仅 10x，
    // 把检索悄悄缩成基础库、漏掉外部平台库（HCA/EBI/CELLxGENE）且用户无感。
    try { await SOURCES_READY; } catch (_e) {}
    const cfg = getConfig();
    const btn = $("submitBtn");
    const reqBody = Object.assign({ query: queryForRetrieval(query), provider: cfg.provider, use_llm: cfg.use_llm, mock_llm: cfg.mock_llm, api_key: cfg.api_key, base_url: cfg.base_url, model: cfg.model, auto_allow_llm: cfg.auto_allow_llm, rerank_top_n: cfg.rerank_top_n, sources: cfg.sources, auto_parse_sources: cfg.auto_parse_sources }, searchParamSnapshot(query));
    const cacheKey = queryCacheKey(reqBody);
    // final a 档（设计 §2.1）：环内采纳的更优结果随 utterance final 抵达
    // （opts.prefetched）——不再发 /api/recommend，把它当本次响应走完落地+收尾链；
    // 缓存查询整段跳过（它是环内择优管线的产物，按本请求 reqBody 建键会错位缓存/错位命中）。
    const prefetched = opts.prefetched || null;
    const cached = prefetched ? null : queryCacheGet(cacheKey);
    if (cached) {   // 命中：秒出、不走网络
        if (myGen === _recSeq) {
            try {
                landRecommendResult(cached, query, { noScroll: keep, replaceHist: opts.keepFacets, sentText: opts.sentText, planSteps: opts.planSteps,
                    actPending: !!opts.actPlan,
                    policyParts: { strategy: reqBody.strategy, rerank: reqBody.rerank, recall: reqBody.recall } });
                toast("相同查询与设置，直接沿用了上次结果");
                // 使用反馈打点（默认关；关着时这行等于一次布尔比较）。缓存命中对用户而言同样是
                // 「搜了一次并看到了结果」，所以照记 —— 少记会让「搜过什么」少掉重复查询那一半。
                // cached:true 告诉记录层剔除旧 trace 的耗时（否则秒出会被算成正常耗时，平均值洗假）。
                usageLogSearch(cached, query, Object.assign({}, opts, { cached: true, telemetryScope: telemetryScope,
                    policyParts: { strategy: reqBody.strategy, rerank: reqBody.rerank, recall: reqBody.recall } }));
                // benchmark 采集：缓存命中同样是「问了一句并看到结果」——请求体在这里脱敏
                // （api_key 整键删、base_url 留主机），响应是缓存的那份完整响应。
                benchfbTurnSearch(reqBody, cached, { cached: true, handSubmit: !!opts.handSubmit, query: query, ms: 0, scope: telemetryScope });
                actAfterSearch(query, opts);
            } finally {
                // 缓存命中不走网络、也就没有下方 finally 的收尾。但**在途的旧请求**（含统一框路由前的
                // startProgress）已把按钮置成 disabled + .loading，而它的 finally 守卫（myGen === _recSeq）
                // 此刻已因本次自增而失配 → 不在这里收尾，按钮就永久卡死。
                // 复现：搜一次（结果入缓存 K0）→ 点分面芯片（发请求）→ 请求返回前再点同一枚芯片
                // （取消该分面 → 请求体退回 K0 → 命中缓存）→ 检索按钮从此点不动、永远转圈。
                // 收尾用**完成语义**（finishProgress 同步摘 loading），不用 resetSubmitButton
                // 的取消语义——统一框发重复查询时是同一在途请求的继续，秒出＝这次检索瞬间完成。
                // 放在 myGen 守卫**内**：只有当前代有权收尾；若本代已被更晚的请求接管，
                // 那一代可能正在合法加载，收尾会抹掉它的 loading 态。
                // 2026-08-19 补 finally 兜底：上面渲染/打点任何一步抛错（典型：新旧 JS 混合缓存
                // 的 ReferenceError，FRONTEND.md §4.3）时也要复位按钮——否则 submitBtn/chatSendBtn 卡
                // loading、ubSubmit 在途闸（submitBtn.disabled）拦下所有后续输入。finishProgress /
                // resetSubmitButton 本身幂等，正常路径只执行一次。
                if (btn.classList.contains("loading")) {
                    finishProgress();   // 补满 100 → 淡出 → 自行摘除 loading（尾部 300ms timer 带句柄，可被新请求清）
                    btn.disabled = false; btn.removeAttribute("aria-busy");
                } else {
                    resetSubmitButton();   // 没有在途加载态：同步复位（幂等空操作 + 防残留）
                }
            }
        }
        return;
    }
    // 加载态由 .loading 类接管：按钮保留静态 loading（图标/文案不动，数字里程表已退役），
    // 进度表达交给系统回复气泡里的三点动画。仅置 aria-busy 供读屏。
    btn.disabled = true; btn.setAttribute("aria-busy", "true"); startProgress(estimateDuration(cfg));
    try {
        let data;
        if (prefetched) {
            data = prefetched;   // 环内采纳 payload 即本次响应，不发 /api/recommend
        } else {
            const res = await fetch(API.recommend, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(reqBody)
            });
            data = await res.json();
            if (!res.ok || !data.ok) throw new Error(data.detail || "服务未返回推荐结果");
            // 只缓存「对所请求管线完整成功」的结果：若请求了真 LLM（use_llm 且非 mock）却回退成确定性
            // （llm_response_used=false —— 限流/超时/空密钥触发后端 fail-open，仍返回 ok:true），则**不缓存**，
            // 否则这次退化结果会粘住整会话、稍后重试也被缓存拦截、永远打不到后端。
            const llmRequested = reqBody.use_llm && !reqBody.mock_llm;
            if (!llmRequested || data.llm_response_used === true) queryCacheSet(cacheKey, data);   // 缓存与代号无关，恒可写
        }
        if (myGen !== _recSeq) return;   // 已被更晚的请求取代 → 丢弃，避免旧结果覆盖新状态/错配面包屑
        landRecommendResult(data, query, { noScroll: keep, replaceHist: opts.keepFacets, sentText: opts.sentText,
            fadeIn: !!opts.fadeIn, keepProgress: !!opts.keepProgress, planSteps: opts.planSteps,
            actPending: !!opts.actPlan,
            policyParts: { strategy: reqBody.strategy, rerank: reqBody.rerank, recall: reqBody.recall } });
        usageLogSearch(data, query, Object.assign({}, opts, { telemetryScope: telemetryScope,
            policyParts: { strategy: reqBody.strategy, rerank: reqBody.rerank, recall: reqBody.recall } }));
        // benchmark 采集：完整请求（脱敏）+ 完整响应（results 全字段/search_trace 全步）入档。
        const _bfMs = (data && data.search_trace && Number(data.search_trace.total_duration_ms)) || 0;
        benchfbTurnSearch(reqBody, data, { cached: false, handSubmit: !!opts.handSubmit, query: query, ms: _bfMs, scope: telemetryScope });
        // 「人类肺癌数据，打包前5条」：检索落地了才谈得上打包。**只在用户亲手提交的那一次**触发——
        // 分面芯片、一键放宽、撤销/重做、从左侧历史重跑全都走 runRecommend，在那些路径上自动执行
        // 会让同一句话反复产包（用户点一下芯片就又下一个 zip）。
        actAfterSearch(query, opts);
    } catch (err) {
        if (myGen !== _recSeq) return;   // 陈旧错误也不覆盖新状态
        const raw = (err && err.message) ? String(err.message) : String(err);   // 去双重「Error: Error:」前缀
        // 浏览器网络层拒连（fetch 抛 TypeError）只有平台原文「Failed to fetch」一类，用户读不懂——
        // 上屏翻成人话；原始串不退正文，随结构化代号进失败卡详情行（可诊断、不甩机器码）。
        const netDown = /failed to fetch|network ?error|load failed/i.test(raw);
        const emsg = netDown ? "没有连上服务（网络中断，或服务没有启动）" : raw;
        const ecode = netDown ? "network_unreachable" : "recommend_failed";
        // 检索直接失败要记 —— 客户那边连着失败而开发者一无所知，正是这个反馈功能最该解决的事。
        usageLog(USAGE_KINDS.err, { msg: raw.slice(0, 80), stage: "search" });
        benchfbTurnError(emsg);   // 采集：这轮以检索失败告终（请求/响应已在档则无 search 段，如实记错）
        cbProgressDone("检索失败：" + emsg);   // 进度泡：失败也要如实收尾成文字（无进度泡时 no-op）
        _prelimBadge(false);   // 失败屏不是先行结果，徽标顺手摘掉
        renderStatus({ markdown: emsg });
        // error:true → 让 renderResults 走独立「检索失败」分支（如实报故障、不套用弃权话术）；并收起分面条避免陈旧残留。
        renderResults([], { markdown: emsg, error: true, error_code: ecode, error_raw: raw });
        // 条件板与任务包清单描述的是**上一次成功检索**。屏幕上已经是「检索失败」了，
        // 它们还挂在那里，就是拿旧一次的条件和结果条数给这一屏背书。分面条、条件板、常驻查询条件栏一起收。
        // **顺序要紧**：必须在 placeFacetBar（内部会 swSync）**之前**把 condBoard/#facetActive 判成空，
        // 否则 swSync 读到 condBoard 仍可见 → 保留对话记录卡、把上一次成功检索的 #facetActive 搬进 #swHits，
        // 「检索失败」屏上就挂着旧一次的查询条件（含可点的 忽略/× 按钮）——正是本分支要防的陈旧背书。
        const _cb = $("condBoard"); if (_cb) _cb.hidden = true;
        const _fa = $("facetActive"); if (_fa) { _fa.hidden = true; _fa.innerHTML = ""; }
        $("facetBar").hidden = true;
        placeFacetBar();   // 据上面已置的 hidden：拆二分、分面条搬回结果区、swSync 收起整卡 + 常驻栏（_facetFilters 保留供重试）
        resetTaskPack();
        toast("检索失败，可在「设置 → 开发者信息 / 诊断」里运行诊断");
    } finally { if (myGen === _recSeq) { finishProgress(); btn.disabled = false; btn.removeAttribute("aria-busy"); } }
}

/* allDatasets/bs 两个浏览态已归位 browse.js（它们从来只服务浏览/收藏/历史，本文件从不读写——
   是早年 search/browse 拆分遗留在本文件末尾的）。 */
