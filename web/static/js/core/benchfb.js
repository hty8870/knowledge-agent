"use strict";

/* benchmark 采集反馈 · 记录层 + 评分/导出界面（存储与 DOM；纯变换在 benchfb_core.js）
 *
 * ## 这是什么（与 usage_log 的关系）
 *
 * usage_log 是「聚合文本反馈」：一段时间的使用聚成一段能粘进微信的中文。
 * 本模块是「benchmark 采集反馈」：**每一轮真实交互**（用户原话 → 路由 → 检索/工具执行
 * → 结果）整条结构化落本机，用户可对轮次选完成度（完成/部分完成/未完成）+ 可选原因、
 * 标有用条目、写评语，最后一键导出 **单个 JSON 文件**（微信当文件发回即可）——为
 * benchmark 制作供数。两条通道共用**同一个开关**（usage_log 的使用反馈开关）、同一批红线：
 * 默认本地采集、部署方配置上传通道后脱敏上传（本文件零出网，唯一出口 usage_upload.js；
 * 生产通道为明文 HTTP，属已知风险裁决，consent 弹窗如实告知）、
 * api_key 绝不落盘（请求脱敏在 benchfb_core.benchfbStripRequest，契约门另有断言）。
 *
 * ## 采集点（三个调用方，各留一行）
 *
 * - board.js ubSubmit：benchfbTurnBegin（用户原话 + 现场）→ benchfbTurnRoute（路由应答，
 *   agent 路径的 plan.trace 逐步执行记录就在其中）；none 路由 → benchfbTurnEcho；
 *   路由 fail-open → benchfbTurnNote（注记不收尾，反馈包里看得出路由层失败过）。
 * - search.js runRecommend 两个落点：benchfbTurnSearch（脱敏请求体 + 完整响应）。
 * - act.js actFinish 尾部：benchfbTurnAction（工具执行回执）。
 * 一轮 = 一条记录。「search 后又接打包」（一句话任务包）时动作段**并回同一条**（2 分钟窗）。
 *
 * ## 评分卡为什么不进 _cbLog（各绑各的 rec.id；会话降频）
 *
 * _cbLog 会随 pushHist 持久化进历史、参与帧剪枝/分支/回退——评分卡是**采集层的临时投影**，
 * 不是对话内容，历史格式一个字节不变。**每次收尾（hero 检索 / 对话检索 / 工具执行
 * 都算一轮）生成一张绑定该轮 rec.id 的独立卡**，不再有全局单卡 _promptId；
 * **2026-08-22 起加会话降频**（派发口径「就你最关注的地方出现两次就行」）：
 * 每 tab 会话主动完整卡 ≤2 张、收起/不评分连续 2 次本会话不再主动出卡
 * （sessionStorage 计数，纯函数闸在 benchfb_core.js；刷新页面会话重置是可接受的
 * 会话语义）；被降频的 search/tool 轮与 none/error 轮一样只给折叠「评价」按钮，
 * 用户点开的完整卡不占配额、不计忽略。
 *
 * - 对话轮（src="chat"）：收尾时经 benchfbOnChatEntry 通知 board，board 把 rec.id 贴到
 *   本轮最近一条系统回复 entry 上（bfRecId），cbRenderHistory 在 entry 下方渲染挂载点
 *   [data-bf-mount]，重画后由 benchfbAfterRender（渲染钩）把卡填回——entry 被剪枝/清空
 *   时卡自然消失，记录仍在 localStorage。
 *   （2026-08-20）：none/error 轮（纯埋点、不出完整卡）在挂载点渲染一颗
 *   低调「评价」按钮，点击原位展开完整评分卡、再点收起（展开态存 _expandedRates 内存集合，
 *   重画后照读不错乱）；hero 侧 none/error 轮（回音/检索失败同样有系统回复泡）一并绑定。
 * - hero 轮（src="hero"，无对话）：卡挂结果区顶部专用槽位（#resultsGrid 首子，由
 *   benchfbAfterSearchRender 随结果区渲染重建）；新一轮 hero 检索替换结果区时旧卡消失。
 * - 「标出有用条目」只在**该卡就是最新一次检索**（rec.id === _lastSearchRecId）且那轮
 *   确有结果时出现——旧轮的卡保留星星/评语/收起，不误导用户拿老名次标新一屏。
 *
 * ## 埋点绝不许把主功能带崩
 *
 * 同 usage_log 纪律：所有存储写都在 try 里，配额满就安静地少记一条，绝不弹错打断检索。 */
import { LS, nsKeyFor, readJSON, writeJSON, $, cacheGeneration, escapeHtml, toast } from "#core";
import { dlqFireBlob } from "#downloads";
import { USAGE_KINDS, usageActiveTurnId } from "#usage_core";
import { usageClearScope, usageClientId, usageEnabled, usageEnabledForScope, usageInstallId,
    usageLog, usageNoteDropsForScope, usageProfileIdForScope, usageScope } from "#usage_log";
import {
    BENCHFB_MAX_COMMENT, BENCHFB_COMPLETIONS, BENCHFB_REASONS, BENCHFB_RATE_SESSION_KEY,
    benchfbStripRequest, benchfbMakeId, benchfbRate as benchfbRateCore, benchfbTrim,
    benchfbBuildPackage, benchfbFileName, benchfbPackageSummary, benchfbEndpointHost,
    benchfbRateSession, benchfbProactiveAllowed, benchfbNoteShown, benchfbNoteRated, benchfbNoteDismissed,
    benchfbResolveUseful,
} from "#benchfb_core";
/* 意见反馈队列（相对 import，不进 importmap/静态图——同 usage_upload 的哲学）；
   仅导出反馈包时读明文本地账本，其余路径零接触。 */
import { feedbackPendingForScope } from "./feedback_core.js";
import { armTwoStepConfirm, closeModal, openModal } from "./copy.js";
import { queueMigrateLegacyArray, queueRead, queueRemoveIds, queueRemovePrefix, queueWrite } from "./storage_queue.js";

const BENCH_RECORD_MARK = "::record::";
let _bfCache = new Map();   // scope -> 记录数组；每条记录独立 key，跨标签页 append 不再整表覆盖
let _turn = null;           // 在途轮次累加器（ubSubmit 在途闸保证同一时刻至多一轮）
let _lastClosed = null;     // {id, t}：一句话任务包「检索→接打包」的动作段并回同一条用
let _dismissed = new Set(); // 已收起的卡 rec.id（内存态：UI 偏好不落盘，刷新即回到未收起）
let _lastSearchRecId = null; // 最近一次检索落屏的 rec.id（「标出有用条目」按钮显隐判据）
let _lastHeroRecId = null;  // 最近一次 hero 侧收尾的 rec.id（结果区顶部 hero 卡的数据源）
let _chatEntryBinder = null; // board 注入的回调：chat 轮（及 hero 侧 none/error 轮）收尾时通知它把 rec.id 绑到本轮系统回复
let _expandedRates = new Set(); // none/error 轮「评价」按钮的展开态 rec.id（内存态；重画后 _renderMount 照读，状态不错乱）
let _autoGrow = null;        // board 注入的输入框自动伸展工具（断环，同 _chatEntryBinder 模式）；评语框用
let _markMode = false;      // 标注模式开关
let _markObs = null;        // 标注期的 MutationObserver（结果区重画后补回标记/越界即收摊）
let _markFingerprint = "";  // 标注开启时结果区的内容指纹（同数量不同内容的重画也要识别）

/* 结果区内容指纹（2026-08-15）：逐卡取「数据集详情」链接 href（含 uid/url/名称/来源），
   非卡片子节点（如放宽横幅 / hero 评分卡槽位）计空串。只比 children.length 挡不住
   「同数量、不同内容」的重画。hero 卡槽位（.bf-mount）也计入空串档——名次计算统一走
   _gridCards（跳过槽位），指纹与名次同一套口径。 */
function _gridCards(grid) {
    return Array.prototype.filter.call(grid.children, function (c) {
        return !(c.classList && c.classList.contains("bf-mount"));
    });
}
function _gridFingerprint(grid) {
    if (!grid) return "";
    return _gridCards(grid).map(function (c) {
        const a = c.querySelector ? c.querySelector(".btn-detail") : null;
        return a ? (a.getAttribute("href") || "") : "";
    }).join("|");
}
let _returnFocus = null;    // 导出弹窗关闭后焦点还回

/* ---------- 开关与存储 ---------- */

export function benchfbOn() { return usageEnabled(); }   // 与使用反馈同一开关（用户已授权记录）

function _benchBase(scope) { return nsKeyFor(LS.benchfb, scope); }
function _benchPrefix(scope) { return _benchBase(scope) + BENCH_RECORD_MARK; }

function _migrateLegacyBench(scope) {
    const oldKey = _benchBase(scope);
    queueMigrateLegacyArray(localStorage, {
        legacyKey: oldKey, prefix: _benchPrefix(scope),
        normalize: function (raw, index) {
            const rec = (raw && typeof raw === "object") ? Object.assign({}, raw) : {};
            rec.id = String(rec.id || ("legacy-b-" + String(Number(rec.t) || 0) + "-" + index));
            return rec;
        },
        id: function (rec) { return rec.id; },
    });
}

function _readRecords(scope) {
    _migrateLegacyBench(scope);
    return queueRead(localStorage, _benchPrefix(scope), function (a, b) {
        return (Number(a.t) || 0) - (Number(b.t) || 0) || String(a.id || "").localeCompare(String(b.id || ""));
    });
}

function _records(scope) {
    scope = scope === undefined ? usageScope() : scope;
    if (_bfCache.has(scope)) return _bfCache.get(scope).slice();
    const records = _readRecords(scope);
    _bfCache.set(scope, records);
    return records.slice();
}

function _persistRecord(rec, scope) {
    try {
        if (!queueWrite(localStorage, _benchPrefix(scope), rec.id, rec)) throw new Error("storage_full");
        _bfCache.delete(scope);
        return true;
    } catch (_e) { usageNoteDropsForScope(scope, "storage_error", 1); return false; }
}

function _trimScope(scope) {
    const all = _readRecords(scope);
    const keep = benchfbTrim(all);
    const keepIds = new Set(keep.map(function (r) { return String(r.id || ""); }));
    all.forEach(function (rec) {
        if (!keepIds.has(String(rec.id || ""))) {
            try { localStorage.removeItem(_benchPrefix(scope) + rec.id); } catch (_e) {}
        }
    });
    if (all.length > keep.length) usageNoteDropsForScope(scope, "benchfb", all.length - keep.length);
    _bfCache.set(scope, keep);
    _trimGlobalBench();
    return keep;
}

function _trimGlobalBench() {
    const rows = [];
    let chars = 0;
    try {
        for (let i = 0; i < localStorage.length; i++) {
            const key = localStorage.key(i);
            if (!key || !key.includes(BENCH_RECORD_MARK)) continue;
            const text = localStorage.getItem(key) || "";
            const rec = JSON.parse(text);
            rows.push({ key: key, t: Number(rec && rec.t) || 0, n: text.length });
            chars += text.length;
        }
        rows.sort(function (a, b) { return a.t - b.t || a.key.localeCompare(b.key); });
        let removed = false;
        const removedByScope = new Map();
        while (rows.length > 120 || chars > 3200000) {
            const row = rows.shift(); if (!row) break;
            localStorage.removeItem(row.key); chars -= row.n; removed = true;
            const marker = LS.benchfb + "::u:";
            const scope = row.key.startsWith(marker) ? row.key.slice(marker.length).split(BENCH_RECORD_MARK, 1)[0] : "";
            removedByScope.set(scope, (removedByScope.get(scope) || 0) + 1);
        }
        removedByScope.forEach(function (count, scope) { usageNoteDropsForScope(scope, "benchfb", count); });
        if (removed) _bfCache.clear();
    } catch (_e) {}
}

function _findRecord(id, scope) {
    const list = _records(scope);
    for (let i = list.length - 1; i >= 0; i--) { if (list[i] && list[i].id === id) return list[i]; }
    return null;
}

export function benchfbRecordsForScope(scope) { return _records(scope); }

export function benchfbRemoveRecordsForScope(scope, ids) {
    queueRemoveIds(localStorage, _benchPrefix(scope), ids);
    _bfCache.delete(scope);
}

export function benchfbMarkOversizeForScope(scope, id) {
    const rec = _findRecord(id, scope);
    if (!rec) return;
    rec.telemetry_oversize = true;
    _persistRecord(rec, scope);
}

export function benchfbClearScope(scope) {
    queueRemovePrefix(localStorage, _benchPrefix(scope));
    try { localStorage.removeItem(_benchBase(scope)); } catch (_e) {}
    _bfCache.delete(scope);
}

if (window.addEventListener) {
    window.addEventListener("storage", function (event) {
        const key = String((event && event.key) || "");
        if (key.includes(BENCH_RECORD_MARK) || key.includes(LS.benchfb)) { _bfCache.clear(); _syncPanel(); }
    });
}

/* ---------- 轮次采集（board / search / act 三方的全部触点）---------- */

/* 用户发出一句话（ubSubmit 第一落点）。env 由调用方从 getConfig() 摘好传入——
   本模块不 import shell（那会成 import 环），只收纯数据。
   opts.templateOriginated（2026-08-22，设计 §5.5）：任务卡/chip 生成文本
   提交时由调用方显式带 true（未经编辑）/ false（编辑过）——轮次记录 additive 落
   `template_originated` 键；普通手打不传 → 无此键。接收端/导出按此排除 benchmark 候选。 */
export function benchfbTurnBegin(text, opts) {
    opts = opts || {};
    const scope = opts.scope === undefined ? usageScope() : opts.scope;
    if (!usageEnabledForScope(scope)) return;
    if (_turn) _closeTurn("unknown");   // 上一轮没收尾（中途断流/异常）——如实标 unknown，不丢
    _turn = {
        scope: scope,
        t: Date.now(),
        tid: String(usageActiveTurnId() || ""),   // 与 usage 事件同一轮次 id（ubSubmit 先 usageBeginTurn 再调这里）
        q: String(text || "").slice(0, 500),
        src: opts.source === "chat" ? "chat" : "hero",
        conv: String(opts.convId || ""),
        env: {
            model: String(opts.model || ""),
            provider: String(opts.provider || ""),
            endpoint_host: benchfbEndpointHost(opts.baseUrl),
            agent: !!opts.agent, use_llm: !!opts.useLlm,
            rerank: String(opts.rerank || ""), recall: String(opts.recall || ""), strategy: String(opts.strategy || ""),
        },
        route: null, route_ms: 0, search: null, action: null, err: "",
    };
    if (opts.templateOriginated === true || opts.templateOriginated === false) {
        _turn.template_originated = opts.templateOriginated;   // 无此键 = 普通手打（设计 §5.5）
    }
}

/* 路由应答（/api/utterance 的完整回复，agent 路径含 plan.trace 逐步记录）。 */
export function benchfbTurnRoute(reply, opts) {
    if (!_turn || !reply) return;
    opts = opts || {};
    _turn.route = {
        route: String(reply.route || ""), via: String(reply.via || ""),
        query: String(reply.query || ""), echo_zh: String(reply.echo_zh || ""),
        needs_agent: !!reply.needs_agent,
        suggestions: Array.isArray(reply.suggestions) ? reply.suggestions : [],
        plan: reply.plan || null,
        agent: reply.agent || null,
        streamed: !!opts.streamed,
    };
    _turn.route_ms = Math.round(Number(opts.ms) || 0);
}

/* 检索落地（runRecommend 两个落点）。req 在这里脱敏（api_key 整键删、base_url 留主机）。
   opts.handSubmit=false 且没有在途轮次 → 不记（分面芯片/历史重跑不是「用户问了一句」，
   与 usageLogSearch 同一准入口径）。 */
export function benchfbTurnSearch(reqBody, data, opts) {
    opts = opts || {};
    const scope = opts.scope === undefined ? usageScope() : opts.scope;
    if (!usageEnabledForScope(scope)) return;
    if (_turn && _turn.scope !== scope) return;   // 旧账户响应不得并入新账户在途轮次
    if (!_turn) {
        if (!opts.handSubmit) return;
        benchfbTurnBegin(opts.query || (reqBody && reqBody.query) || "", { source: "hero", scope: scope });
        if (!_turn) return;
    }
    _turn.search = {
        req: benchfbStripRequest(reqBody),
        res: data || null,
        cached: !!opts.cached,
        ms: Math.round(Number(opts.ms) || 0),
    };
    _closeTurn("search");
}

/* 并段白名单（2026-08-15）：只有「检索→接打包」系动作段允许并回上一轮记录——
   设计意图就是一句话任务包「检索完接打包」。窗口内其余动作（删文件/恢复等）并回去会污染
   反馈包里「这轮检索后用户做了什么」的因果，一律自立一条。 */
const MERGE_BACK_VERB_RE = /^(pack\.|打包|打开打包清单)/;

/* 工具执行收尾（actFinish 尾部）。在途轮次 → 并入并收尾；2 分钟内刚收尾的轮次 →
   动作段并回那一条（限打包系动词，见上方白名单）；再否则自立一条。 */
export function benchfbTurnAction(action) {
    if (!benchfbOn()) return;
    const seg = {
        verb: String((action && action.verb) || ""),
        cancelled: !!(action && action.cancelled),
        receipt: String((action && action.receipt) || "").slice(0, 800),
        trace: (action && Array.isArray(action.trace)) ? action.trace : [],
    };
    if (_turn) { _turn.action = seg; _closeTurn("tool"); return; }
    if (_lastClosed && _lastClosed.scope === usageScope()
            && Date.now() - _lastClosed.t < 120000 && MERGE_BACK_VERB_RE.test(seg.verb)) {
        const rec = _findRecord(_lastClosed.id, _lastClosed.scope);
        if (rec) { rec.action = seg; _persistRecord(rec, _lastClosed.scope); _syncPanel(); return; }
    }
    benchfbTurnBegin(seg.verb ? "（操作）" + seg.verb : "（操作）", { source: "hero" });
    if (_turn) { _turn.action = seg; _closeTurn("tool"); }
}

/* none 路由：系统如实回音（没听懂/婉拒/指路）。这类「系统答非所问」正是 benchmark 的硬数据。 */
export function benchfbTurnEcho() { if (_turn) _closeTurn("none"); }

/* 轮次以失败告终（路由异常且 fail-open 也没走成等）。 */
export function benchfbTurnError(msg) {
    if (!_turn) return;
    _turn.err = String(msg || "").slice(0, 200);
    _closeTurn("error");
}

/* 在途轮次挂错误注记但**不收尾**（2026-08-15 补漏）：路由 fail-open 时
   检索段随后仍并入同一条记录，若什么都不写，反馈包里这轮看起来就是一次正常检索——
   看不出路由层失败过。err 字段本就是记录形状的一部分，这里只填不收尾。 */
export function benchfbTurnNote(msg) {
    if (_turn) _turn.err = String(msg || "").slice(0, 200);
}

function _closeTurn(kind) {
    if (!_turn) return;
    const now = Date.now();
    const rec = Object.assign({}, _turn, {
        id: benchfbMakeId(now, Math.random().toString(36).slice(2, 8)),
        kind: kind,
        end: now,
        ms: Math.max(0, now - _turn.t),   // 轮次耗时（turn begin→close，设计 §5）
        rating: null,
    });
    _turn = null;
    const scope = rec.scope === undefined ? usageScope() : rec.scope;
    delete rec.scope;   // scope 只决定本地命名空间，不上传账户 id
    if (!_persistRecord(rec, scope)) return;
    _trimScope(scope);
    // 旧账户请求晚到时只完成旧 scope 落盘，不把评分卡/名次状态挂到新账户当前 UI。
    if (scope !== usageScope()) return;
    _lastClosed = { id: rec.id, t: now, scope: scope };
    // 检索结果落屏的那轮记成「最新检索」——「标出有用条目」只对最新检索的卡可见。
    if (kind === "search") _lastSearchRecId = rec.id;
    // hero 侧**检索**收尾记成「最新 hero 轮」——结果区顶部槽位的数据源。只认 search 轮：
    // agent 环里 final 落地后工具回执会迟到（白名单外自立一条 kind="tool"），若不限
    // 轮型，工具轮会把 heroMount 顶成一张没有检索段的空卡。
    if (rec.src === "hero" && kind === "search") _lastHeroRecId = rec.id;
    // chat 轮：通知 board 把这张卡绑到本轮系统回复（board 侧延迟一拍贴 bfRecId，见 benchfbOnChatEntry）。
    // hero 侧的 none/error 轮同样有一颗系统回复泡（回音/检索失败），一并绑——
    // 「评价」按钮要落在每一颗系统回复气泡下方；hero 检索/tool 轮仍走结果区顶部槽位，不绑。
    if ((rec.src === "chat" || rec.kind === "none" || rec.kind === "error") && _chatEntryBinder) {
        _chatEntryBinder(rec.id);
    }
    _syncPanel();
    _renderAllMounts();
}

/* ---------- 评分 ---------- */

/* 会话级降频状态（sessionStorage，与 biodata_sid_v1 同生命周期——刷新页面会话重置，
   这是可接受的会话语义，见 benchfb_core.js 降频常量注释）。sessionStorage 不可用
   （隐私模式等）时退化为内存态，效果等同「刷新即重置」。 */
let _rateSessionCache = null;
function _rateSession() {
    if (_rateSessionCache) return _rateSessionCache;
    let raw = null;
    try { raw = JSON.parse(sessionStorage.getItem(BENCHFB_RATE_SESSION_KEY) || "null"); } catch (_e) { raw = null; }
    _rateSessionCache = benchfbRateSession(raw);
    return _rateSessionCache;
}
function _saveRateSession(next) {
    if (!next) return;
    const before = JSON.stringify(_rateSession());
    const after = JSON.stringify(next);
    _rateSessionCache = next;
    if (after === before) return;   // 重画挂回等幂等路径不产生存储写
    try { sessionStorage.setItem(BENCHFB_RATE_SESSION_KEY, after); } catch (_e) {}
}

/* 「这轮评过了吗」的单一判据：完成度/原因/标注/评语任一非空（星级已退役，旧记录
   的 stars 不再纳入——它只会随旧记录自然消亡，新形状里没有这个键）。 */
function _isRated(rec) {
    const r = (rec && rec.rating) || {};
    return !!(r.completion || (r.reasons || []).length || (r.useful_idx || []).length || r.comment);
}

/* 这张卡是否只给折叠「评价」按钮：none/error 轮（纯埋点）一律折叠；search/tool 轮
   被会话降频闸拦下后也折叠。折叠态点「评价」原位展开完整卡——用户主动行为，
   不占配额、不计忽略。 */
function _collapsedRate(rec) {
    if (!rec) return false;
    if (rec.kind === "none" || rec.kind === "error") return true;
    return !benchfbProactiveAllowed(_rateSession(), rec.id);
}

/* 主动完整卡上屏计一次（幂等：已计过的卡重画不改变状态）。pending 卡评没评，
   决定连续忽略计数 +1 还是清零。 */
function _noteShown(rec) {
    const cur = _rateSession();
    const prev = cur.pending ? _findRecord(cur.pending) : null;
    _saveRateSession(benchfbNoteShown(cur, rec.id, !!(prev && _isRated(prev))));
}

/* ---------- 评分标签事件（2026-08-22，schema v3 label）----------
   每次评分写入**无论 benchfb 记录还在不在本地**都发一条 usage label 事件：记录可能被
   上传 ACK 精确删除，但「这轮评了什么」必须仍可达接收端（事件带 recId，导出侧按
   (tid, recId) 双源去重、同键大 rev 赢——recId 是 additive 可选字段，schema 版本不再 bump）。
   台账（per-scope 单 JSON map：recId → {tid,rev,completion,reasons,useful_idx,useful_uids,comment,at}，
   LS.benchfbLabels）只为「记录已删后的续评」兜住 tid 与旧值；cap 200 条按写入时刻 FIFO，
   全 try 静默——label 是遥测附加，绝不打断评分主流程。 */
const BENCHFB_LABELS_CAP = 200;

function _labelLedger(scope) {
    let m = null;
    try { m = readJSON(nsKeyFor(LS.benchfbLabels, scope), {}); } catch (_e) { m = null; }
    return (m && typeof m === "object" && !Array.isArray(m)) ? m : {};
}

function _labelLedgerSave(scope, ledger) {
    try {
        const keys = Object.keys(ledger);
        if (keys.length > BENCHFB_LABELS_CAP) {
            keys.sort(function (a, b) { return (Number(ledger[a].at) || 0) - (Number(ledger[b].at) || 0); });
            keys.slice(0, keys.length - BENCHFB_LABELS_CAP).forEach(function (k) { delete ledger[k]; });
        }
        writeJSON(nsKeyFor(LS.benchfbLabels, scope), ledger);
    } catch (_e) {}
}

function _sanitizeUsefulIdx(list) {
    const seen = new Set();
    const out = [];
    (Array.isArray(list) ? list : []).forEach(function (v) {
        const n = Number(v);
        if (!Number.isInteger(n) || n < 1 || seen.has(n)) return;
        seen.add(n); out.push(n);
    });
    out.sort(function (a, b) { return a - b; });
    return out;
}

/* label 快照组装：记录在 → 用合并后的完整评分（useful_uids 由记录 results 现解析，
   benchfbResolveUseful 同一口径）；记录不在（已上传 ACK 删除后的续评窗口）→ 在台账旧值上
   叠本次 patch——useful_idx 本次没变才沿台账 useful_uids，变了置 []（没有 results 可解析，
   绝不编造 uid）。tid 取记录.tid 或台账旧值；都没有 → null（诚实缺失）。 */
function _emitLabel(recId, updated, patch, scope) {
    try {
        const p = patch || {};
        const ledger = _labelLedger(scope);
        const prev = (ledger[recId] && typeof ledger[recId] === "object") ? ledger[recId] : null;
        let snap;
        if (updated && updated.rating) {
            const r = updated.rating;
            snap = {
                tid: String(updated.tid || (prev && prev.tid) || "") || null,
                completion: r.completion || null,
                reasons: Array.isArray(r.reasons) ? r.reasons.slice() : [],
                useful_idx: _sanitizeUsefulIdx(r.useful_idx),
                useful_uids: benchfbResolveUseful(updated).map(function (x) { return x.uid; }),
                comment: String(r.comment || ""),
            };
        } else {
            const idxChanged = Array.isArray(p.usefulIdx);
            snap = {
                tid: String((prev && prev.tid) || "") || null,
                completion: p.completion !== undefined ? (p.completion || null) : (prev ? (prev.completion || null) : null),
                reasons: Array.isArray(p.reasons) ? p.reasons.slice() : (prev && Array.isArray(prev.reasons) ? prev.reasons.slice() : []),
                useful_idx: idxChanged ? _sanitizeUsefulIdx(p.usefulIdx) : (prev && Array.isArray(prev.useful_idx) ? prev.useful_idx.slice() : []),
                useful_uids: idxChanged ? [] : (prev && Array.isArray(prev.useful_uids) ? prev.useful_uids.slice() : []),
                comment: p.comment !== undefined ? String(p.comment).slice(0, BENCHFB_MAX_COMMENT) : (prev ? String(prev.comment || "") : ""),
            };
        }
        const rev = (Number(prev && prev.rev) || 0) + 1;
        ledger[recId] = Object.assign({}, snap, { rev: rev, at: Date.now() });
        _labelLedgerSave(scope, ledger);
        usageLog(USAGE_KINDS.label, {
            tid: snap.tid, iid: null,   // label 不归某一次展示：显式 null，不冒领当前 iid
            // 带 recId（记录 id）——导出侧按 (tid, recId) 与 benchfb 记录双源去重，
            // 同一轮次多条记录的评分互不吞并；additive 可选字段，老数据缺失时导出侧空串兜底。
            recId: String(recId || ""),
            completion: snap.completion, reasons: snap.reasons,
            useful_uids: snap.useful_uids, useful_idx: snap.useful_idx,
            comment: snap.comment, rev: rev,
        }, scope);
    } catch (_e) { /* 静默：label 失败绝不影响评分落盘 */ }
}

/* 评分写入唯一入口（点击 handler 全走这里）。记录在本地说照常合并落盘；
   **无论记录在不在都发 label 事件**（_emitLabel）。返回合并后的记录（记录不在 → null，
   调用方 _toggleMark 等只在记录存在时操作，行为不变）。 */
export function benchfbRateRecord(recId, patch) {
    if (!recId) return null;
    const scope = usageScope();
    const list = _records(scope);
    let updated = null;
    for (let i = list.length - 1; i >= 0; i--) {
        if (list[i] && list[i].id === recId) {
            updated = benchfbRateCore(list[i], Object.assign({ ratedAt: Date.now() }, patch));
            _persistRecord(updated, scope);
            _saveRateSession(benchfbNoteRated(_rateSession(), recId));   // 任一评分写入都打断连续忽略
            break;
        }
    }
    _emitLabel(recId, updated, patch, scope);
    return updated;
}

/* ---------- 评分卡（各绑各的 rec.id；完成度三选 + 原因 chips 取代星级）----------

   卡 DOM 由 benchfb 构造（采集层投影），挂载点由宿主决定：
   - 对话轮：board 在本轮系统回复 entry 下渲染 <div data-bf-mount data-bf-rec="...">，
     cbRenderHistory 重画后 benchfbAfterRender 把卡填回；
   - hero 轮：结果区顶部专用槽位（.bf-hero-mount，随结果区渲染重建）。
   动态值只有数字与 escape 过的评语；三选项/原因 chips/按钮全静态（文案常量在
   BENCHFB_COMPLETIONS / BENCHFB_REASONS，标签映射在本文件，均非用户输入）。 */
const BF_COMPLETION_LABELS = { done: "完成", partial: "部分完成", failed: "未完成" };

function _compRow(rec) {
    const cur = (rec && rec.rating && rec.rating.completion) || "";
    return BENCHFB_COMPLETIONS.map(function (c) {
        return '<button type="button" class="bf-comp' + (c === cur ? " on" : "") + '" data-bf-comp="' + c + '"'
            + ' role="radio" aria-checked="' + (c === cur) + '">' + BF_COMPLETION_LABELS[c] + "</button>";
    }).join("");
}

function _reasonRow(rec) {
    const cur = new Set((rec && rec.rating && Array.isArray(rec.rating.reasons)) ? rec.rating.reasons : []);
    return BENCHFB_REASONS.map(function (r) {
        return '<button type="button" class="bf-chip' + (cur.has(r) ? " on" : "") + '" data-bf-reason="' + r + '"'
            + ' aria-pressed="' + cur.has(r) + '">' + r + "</button>";
    }).join("");
}

function _fillCard(mountEl, rec) {
    if (!mountEl || !rec) return;
    const rating = rec.rating || {};
    const nResults = (rec.search && rec.search.res && Array.isArray(rec.search.res.results)) ? rec.search.res.results.length : 0;
    const nMarked = (rating.useful_idx || []).length;
    const rated = _isRated(rec);
    // 「标出有用条目」只在该卡对应检索结果正显示在 #resultsGrid 时出现：
    // 该卡就是最新一次检索（rec.id === _lastSearchRecId）且那轮确有检索结果。
    const canMark = nResults > 0 && rec.id === _lastSearchRecId;
    // none/error 轮（无结果）展开后的头部提示说「这轮处理得怎么样」——
    // 「这次结果怎么样」对没有结果的轮不准确；search/tool 轮保持原句。
    const head = rated
        ? "已记录，谢谢！还可继续补标："
        : (rec.kind === "none" || rec.kind === "error" ? "这轮处理得怎么样？" : "这次结果怎么样？");
    const div = document.createElement("div");
    div.className = "bf-rate";
    div.dataset.bfRec = rec.id;
    div.innerHTML =
        '<div class="bf-rate-head">' + head
        + '<span class="bf-rate-sub">脱敏自动上传 · 设置可随时关</span></div>'
        + '<div class="bf-rate-line"><span class="bf-rate-comp" role="radiogroup" aria-label="这轮完成了吗">' + _compRow(rec) + "</span>"
        + '<span class="bf-rate-tools">'
        + (canMark ? '<button type="button" class="bf-tool" data-bf-mark>标出有用条目（' + nMarked + "）</button>" : "")
        + '<button type="button" class="bf-tool" data-bf-comment>' + (rating.comment ? "评语✓" : "写评语") + "</button>"
        + '<button type="button" class="bf-tool bf-skip" data-bf-skip>收起</button>'
        + "</span></div>"
        + '<div class="bf-rate-reasons" aria-label="可选：哪里不对">' + _reasonRow(rec) + "</div>"
        + '<div class="bf-rate-comment" hidden><textarea rows="2" maxlength="' + BENCHFB_MAX_COMMENT + '"'
        + ' placeholder="哪条不对、缺了什么、哪句理解错了…">' + escapeHtml(rating.comment || "") + "</textarea>"
        + '<button type="button" class="bf-tool" data-bf-save-comment>保存评语</button></div>';
    mountEl.appendChild(div);
    // 评语输入框随行数自动伸展（初始 2 行、上限 5 行，超出内部滚动）。
    const ta = div.querySelector("textarea");
    if (ta && _autoGrow) _autoGrow(ta, { minRows: 2, maxRows: 5 });
}

/* 折叠「评价」按钮（低视觉权重小字）：none/error 纯埋点轮与被会话
   降频闸拦下的 search/tool 轮共用。点击原位展开完整评分卡（展开态存 _expandedRates
   内存集合，重画后照读不错乱），再点「收起」回到按钮。 */
function _fillToggle(mountEl, rec) {
    const wrap = document.createElement("div");
    wrap.className = "bf-rate bf-rate-toggle";
    wrap.dataset.bfRec = rec.id;
    wrap.innerHTML = '<button type="button" class="bf-rate-toggle-btn" data-bf-rate-toggle'
        + ' aria-expanded="false" aria-label="评价这轮">评价</button>';
    mountEl.appendChild(wrap);
}

/* 对话轮挂载点（[data-bf-mount]）：按挂载点上的 rec.id 填卡；已收起/记录被清 → 留空
   （.bf-mount:empty 不占位）。折叠档（_collapsedRate：none/error 轮，或降频闸拦下的
   search/tool 轮）默认只渲染「评价」按钮；主动出完整卡前经 _noteShown 计一次会话配额。 */
function _renderMount(mountEl) {
    if (!mountEl) return;
    mountEl.innerHTML = "";
    const recId = mountEl.dataset.bfRec;
    if (!recId || _dismissed.has(recId)) return;
    const rec = _findRecord(recId);
    if (!rec) return;
    if (_collapsedRate(rec)) {
        if (!_expandedRates.has(recId)) { _fillToggle(mountEl, rec); return; }
        // 展开态：落下去渲染完整评分卡（与主动卡同一副卡；用户手动展开不占配额）。
    } else if (!_expandedRates.has(recId)) {
        _noteShown(rec);
    }
    _fillCard(mountEl, rec);
}

/* hero 轮槽位：#resultsGrid 首子（随结果区渲染/搬移走，results.js renderResults 尾部经
   benchfbAfterSearchRender 重建）。规则：显示「最近一次 hero 侧收尾」的卡；但该卡若是
   检索轮且已不是最新一次检索（结果区已被后续 chat 检索替换）→ 卡随结果区消失；无卡可显 → 摘掉槽位不留占位。 */
function _heroCardTarget() {
    const grid = $("resultsGrid");
    if (!grid) return null;
    let m = grid.querySelector(".bf-hero-mount");
    if (!m) {
        m = document.createElement("div");
        m.className = "bf-mount bf-hero-mount";
        m.dataset.bfHero = "1";
        grid.insertBefore(m, grid.firstChild);
    }
    return m;
}

function _renderHeroMount() {
    const m = _heroCardTarget();   // 结果区在就建/复用槽位；结果区还没渲染过 → null（等收尾/渲染钩再建）
    if (!m) return;
    m.innerHTML = "";
    const heroRec = _lastHeroRecId ? _findRecord(_lastHeroRecId) : null;
    if (!heroRec || _dismissed.has(heroRec.id)) { m.remove(); return; }
    if (heroRec.kind === "search" && heroRec.id !== _lastSearchRecId) { m.remove(); return; }
    // （设计 §4）：hero 槽位也只接受 search/tool 轮；none/error 轮（_lastHeroRecId 不会更新到它们）
    // 与未来可能的其他轮型一律不出卡，防槽位被挂成空卡。
    if (heroRec.kind !== "search" && heroRec.kind !== "tool") { m.remove(); return; }
    // 会话降频闸同样约束 hero 槽位——被拦下的轮次只给折叠「评价」按钮。
    if (_collapsedRate(heroRec)) {
        if (!_expandedRates.has(heroRec.id)) { _fillToggle(m, heroRec); return; }
    } else if (!_expandedRates.has(heroRec.id)) {
        _noteShown(heroRec);
    }
    _fillCard(m, heroRec);
}

function _renderAllMounts() {
    document.querySelectorAll("[data-bf-mount]").forEach(function (m) { _renderMount(m); });
    _renderHeroMount();
}

/* 单卡交互后的刷新（星级/评语落盘后只重渲这张卡，不牵动其他卡）。 */
function _refreshCard(card, recId) {
    if (!card) return;
    if (card.closest(".bf-hero-mount")) { _renderHeroMount(); return; }
    const mount = card.closest("[data-bf-mount]");
    if (mount) _renderMount(mount);
}

/* 收起单张卡：进内存集合，重画不再挂回（记录仍在 localStorage，导出反馈包不受影响）。 */
function _dismissCard(recId) {
    if (recId) _dismissed.add(recId);
    _renderAllMounts();
}

/* 全量收起（清空采集记录 / 切换账户）：收起态与记录生命周期一致。 */
function _dismissAll() {
    _exitMarkMode(false);
    _dismissed.clear();
    _expandedRates.clear();
    _renderAllMounts();
}

/* board.js cbRenderHistory 尾部唯一钩：重画后把所有挂载点的卡填回（卡不进 _cbLog，innerHTML 重画会掉）。 */
export function benchfbAfterRender() {
    _renderAllMounts();
}

/* results.js renderResults 尾部钩：结果区重建后把 hero 轮卡挂回顶部槽位（分面重跑等
   重建 #resultsGrid 但无新收尾的路径靠它恢复）。 */
export function benchfbAfterSearchRender() {
    _renderHeroMount();
}

/* board 注入：chat 轮收尾时通知它把 rec.id 绑到本轮系统回复 entry（断环——board 不读本模块私有状态）。 */
export function benchfbOnChatEntry(fn) { _chatEntryBinder = fn; }

/* board 注入输入框自动伸展工具（断环，同 benchfbOnChatEntry 模式——本模块不 import
   interactions，那会把 benchfb 拉进 SCC 环）。评语输入框随行数伸展，上限 5 行。 */
export function benchfbSetAutoGrow(fn) { _autoGrow = (typeof fn === "function") ? fn : null; }

/* ---------- 标注模式（逐条「有用」标注：benchmark 的 per-document 相关性标签）---------- */

function _grid() { return $("resultsGrid"); }

/* 「标出有用条目」只对最新一次检索的卡开放（按钮也只在那张卡上）——标注名次对照的就是
   屏上这份结果。 */
function _markableResults() {
    const rec = _lastSearchRecId ? _findRecord(_lastSearchRecId) : null;
    return (rec && rec.search && rec.search.res && Array.isArray(rec.search.res.results)) ? rec.search.res.results.length : 0;
}

function _applyMarks() {
    const grid = _grid();
    const rec = _lastSearchRecId ? _findRecord(_lastSearchRecId) : null;
    if (!grid || !rec || !rec.rating) return;
    const marked = new Set(rec.rating.useful_idx || []);
    _gridCards(grid).forEach(function (card, i) {
        card.classList.toggle("bf-marked", marked.has(i + 1));
    });
    const bar = $("bfMarkCount");
    if (bar) bar.textContent = String(marked.size);
}

function _enterMarkMode() {
    if (_markMode) return;
    const n = _markableResults();
    const grid = _grid();
    if (!n || !grid) { toast("结果区没有可标注的条目"); return; }
    if (_gridCards(grid).length !== n) { toast("结果区已变化，和这次记录对不上了"); return; }
    _markMode = true;
    _markFingerprint = _gridFingerprint(grid);
    grid.classList.add("bf-marking");
    const bar = $("bfMarkBar");
    if (bar) { bar.hidden = false; }
    _applyMarks();
    // 结果区被重画（分面/新检索）时：内容没变就补回标记；变了就如实收摊——
    // 老记录的名次对新一屏结果不成立，硬标上去就是脏数据。
    // 2026-08-15：判据从「只比卡片数量」升级为「数量 + 内容指纹」——
    // 同数量不同内容的重画（新一轮检索恰好也是 N 条）此前会把旧名次贴到新条目上。
    if (_markObs) _markObs.disconnect();
    _markObs = new MutationObserver(function () {
        if (!_markMode) return;
        const g = _grid();
        if (!g || _gridCards(g).length !== _markableResults() || _gridFingerprint(g) !== _markFingerprint) { _exitMarkMode(true); return; }
        _applyMarks();
    });
    _markObs.observe(grid, { childList: true });
}

function _exitMarkMode(notify) {
    if (!_markMode) return;
    _markMode = false;
    _markFingerprint = "";
    const grid = _grid();
    if (grid) {
        grid.classList.remove("bf-marking");
        Array.prototype.forEach.call(grid.children, function (c) { c.classList.remove("bf-marked"); });
    }
    if (_markObs) { _markObs.disconnect(); _markObs = null; }
    const bar = $("bfMarkBar");
    if (bar) bar.hidden = true;
    if (notify) toast("结果区变了，标注结束（已标的保留）");
}

function _toggleMark(card) {
    const grid = _grid();
    const rec = _lastSearchRecId ? _findRecord(_lastSearchRecId) : null;
    if (!grid || !rec) return;
    const idx = _gridCards(grid).indexOf(card) + 1;
    if (idx < 1) return;
    const cur = new Set((rec.rating && rec.rating.useful_idx) || []);
    if (cur.has(idx)) cur.delete(idx); else cur.add(idx);
    const updated = benchfbRateRecord(rec.id, { usefulIdx: Array.from(cur) });
    if (!updated) return;
    card.classList.toggle("bf-marked", cur.has(idx));
    const bar = $("bfMarkCount");
    if (bar) bar.textContent = String(cur.size);
    // mark 按钮全站唯一（只在最新检索的卡上渲染，见 _fillCard canMark）——标注名次变更同步计数。
    const markBtn = document.querySelector("[data-bf-mark]");
    if (markBtn) markBtn.textContent = "标出有用条目（" + cur.size + "）";
}

/* ---------- 导出 ---------- */

function _buildPackage() {
    const pkg = benchfbBuildPackage(_records(), {
        installId: usageInstallId(),
        clientId: usageClientId(),
        profileId: usageProfileIdForScope(usageScope()),
        exportedAt: new Date().toISOString(),
        app: { cache_generation: cacheGeneration(), ua: navigator.userAgent || "", lang: navigator.language || "" },
    });
    // 本地导出反馈包包含本 profile 已发送/待发送的意见记录（明文本地账本，
    // 入队时已遮蔽；不含密文——发送侧用加密载荷）。最小侵入：附加在顶层 feedback 字段，
    // 无意见记录时不加键（老消费方逐位不变）。
    try {
        const feedback = feedbackPendingForScope(usageScope()).map(function (r) {
            return { feedback_id: r.feedback_id, authorized_at: r.authorized_at, text: r.text,
                diag: r.diag, with_diag: r.with_diag, status: r.status, sent_at: r.sent_at };
        });
        if (feedback.length) pkg.feedback = feedback;
    } catch (_e) { /* 意见账本读取失败不影响反馈包主体 */ }
    return pkg;
}

function _summaryLines(sum) {
    const lines = [];
    const kinds = [];
    if (sum.search) kinds.push("检索 " + sum.search);
    if (sum.tool) kinds.push("操作 " + sum.tool);
    if (sum.none) kinds.push("未听懂回音 " + sum.none);
    if (sum.error) kinds.push("失败 " + sum.error);
    lines.push("共 " + sum.turns + " 轮交互" + (kinds.length ? "：" + kinds.join(" · ") : ""));
    if (sum.rated) {
        let s = "已评分 " + sum.rated + " 轮";
        const comps = [];
        if (sum.comp_done) comps.push("完成 " + sum.comp_done);
        if (sum.comp_partial) comps.push("部分完成 " + sum.comp_partial);
        if (sum.comp_failed) comps.push("未完成 " + sum.comp_failed);
        if (comps.length) s += "（" + comps.join(" · ") + "）";
        if (sum.marked) s += "，标出 " + sum.marked + " 条有用条目";
        lines.push(s);
    } else {
        lines.push("还没有评分——评分越多，反馈越有用。");
    }
    return lines;
}

export function benchfbOpenExport(trigger) {
    const modal = $("benchfbModal");
    if (!modal) return;
    _returnFocus = trigger || document.activeElement;
    const clearBtn = $("benchfbClearBtn");
    if (clearBtn) clearBtn.textContent = "清空本地待发记录";
    const recs = _records();
    const sum = benchfbPackageSummary(recs);
    const listEl = $("benchfbSummary");
    if (listEl) {
        listEl.innerHTML = recs.length
            ? _summaryLines(sum).map(function (l) { return "<li>" + escapeHtml(l) + "</li>"; }).join("")
            : "<li>还没有采集到任何记录——先去用几句，或确认使用反馈开关已打开。</li>";
    }
    const dlBtn = $("benchfbDownloadBtn");
    if (dlBtn) dlBtn.disabled = recs.length === 0;
    const prev = $("benchfbPreview");
    if (prev) { prev.value = ""; }
    const det = $("benchfbPreviewWrap");
    if (det) det.open = false;
    const closeBtn = $("benchfbCloseBtn");
    openModal(modal, { returnFocus: _returnFocus, initialFocus: closeBtn });
}

export function benchfbCloseExport() {
    const modal = $("benchfbModal");
    if (!closeModal(modal)) return;
}

export function benchfbDownload() {
    const recs = _records();
    if (!recs.length) { toast("还没有采集到任何记录"); return; }
    const pkg = _buildPackage();
    const text = JSON.stringify(pkg, null, 1);
    const name = benchfbFileName(new Date());
    // 反馈包也走统一下载队列（dlqFireBlob 内部 try/catch，失败返回 false）。
    if (dlqFireBlob(name, new Blob([text], { type: "application/json;charset=utf-8" }), { kind: "other" })) {
        toast("已下载 " + name + "，把它当文件发给开发者即可");
    } else {
        toast("下载没成功，可以改用「预览」全选复制");
    }
}

export function benchfbTogglePreview() {
    const det = $("benchfbPreviewWrap");
    const prev = $("benchfbPreview");
    if (!det || !prev) return;
    if (det.open && !prev.value) {
        try { prev.value = JSON.stringify(_buildPackage(), null, 1); } catch (_e) { prev.value = "（生成预览失败）"; }
    }
}

export function benchfbClearWithConfirm() {
    const btn = $("benchfbClearBtn");
    if (!armTwoStepConfirm(btn, { idleText: "清空本地待发记录",
                                     confirmText: "再点一次确认清空（不可恢复）" })) return;
    if (btn) btn.textContent = "清空本地待发记录";
    const scope = usageScope();
    benchfbClearScope(scope);
    usageClearScope(scope);
    import("./usage_upload.js").then(function (m) {
        if (m && typeof m.cancelTelemetryUpload === "function") m.cancelTelemetryUpload(scope);
    }).catch(function () {});
    _lastSearchRecId = null;
    _lastHeroRecId = null;
    _dismissAll();
    _syncPanel();
    benchfbCloseExport();
    toast("本账户尚未上传的使用与评分记录已清空；已上传记录不会被远程删除");
}

/* 设置区导出按钮态（2026-08-21：卡片状态小字随文案收敛移除，这里只剩按钮态）。
   顺带刷新「本机编号」行——只读既有键，**绝不为了展示而生成**（未产生数据时如实显示）。 */
export function _syncPanel() {
    const btn = $("benchfbExportBtn");
    if (btn) btn.disabled = _records().length === 0;
    const idNode = $("usageInstallId");
    if (idNode) {
        let id = "";
        try { id = String(localStorage.getItem(LS.usageInstall) || ""); } catch (_e) { id = ""; }
        idNode.textContent = id || "尚未产生数据";
    }
}

/* 设置抽屉打开时的刷新钩（shell.js openSettings 调）：编号可能在首次上传/导出后才生成，
   只在 init 时填一次会显示陈旧的「尚未产生数据」。 */
export function benchfbSyncSettings() { _syncPanel(); }

/* ---------- 接线 ---------- */

export function initBenchfb() {
    // 评语输入框的输入事件委托（卡内 textarea 随重画重建，绑容器级不丢）——
    // 伸展逻辑本体在 interactions.autoGrow（board 注入），这里只转发 input 事件。
    document.addEventListener("input", function (e) {
        if (!_autoGrow) return;
        const ta = e.target;
        if (!ta || ta.tagName !== "TEXTAREA" || !ta.closest(".bf-rate-comment")) return;
        _autoGrow(ta, { minRows: 2, maxRows: 5 });
    });
    // 评分卡事件（document 级委托：卡既挂在对话流 #cbHistory 的 [data-bf-mount] 里，
    // 也挂在结果区顶部 hero 槽位里，两张卡都经这里；按卡上的 data-bf-rec 各评各的）。
    document.addEventListener("click", function (e) {
        const t = e.target;
        if (!t || !t.closest) return;
        const card = t.closest(".bf-rate");
        if (!card) return;
        const recId = card.dataset.bfRec;
        if (!recId) return;
        // 折叠档（none/error 轮、或被会话降频闸拦下的 search/tool 轮）点「评价」→
        // 原位展开完整评分卡（用户主动行为，不占配额）；焦点给第一个完成度选项。
        if (t.closest("[data-bf-rate-toggle]")) {
            _expandedRates.add(recId);
            const heroMount = card.closest(".bf-hero-mount");
            if (heroMount) {
                _renderHeroMount();
                const c0 = heroMount.querySelector("[data-bf-comp]");
                if (c0) c0.focus();
            } else {
                const mount = card.closest("[data-bf-mount]");
                if (mount) {
                    _renderMount(mount);
                    const c0 = mount.querySelector("[data-bf-comp]");
                    if (c0) c0.focus();
                }
            }
            return;
        }
        // 完成度三选（取代星级）：点选入库；再点同一项 = 取消选择。
        const comp = t.closest("[data-bf-comp]");
        if (comp) {
            const v = String(comp.dataset.bfComp || "");
            const rec = _findRecord(recId);
            const cur = (rec && rec.rating && rec.rating.completion) || null;
            benchfbRateRecord(recId, { completion: cur === v ? null : v });
            _refreshCard(card, recId);
            return;
        }
        // 可选原因 chips（可多选、可取消；白名单校验在 benchfb_core.benchfbRate）。
        const reason = t.closest("[data-bf-reason]");
        if (reason) {
            const r = String(reason.dataset.bfReason || "");
            const rec = _findRecord(recId);
            const cur = new Set((rec && rec.rating && Array.isArray(rec.rating.reasons)) ? rec.rating.reasons : []);
            if (cur.has(r)) cur.delete(r); else cur.add(r);
            benchfbRateRecord(recId, { reasons: Array.from(cur) });
            _refreshCard(card, recId);
            return;
        }
        if (t.closest("[data-bf-mark]")) { _markMode ? _exitMarkMode(false) : _enterMarkMode(); return; }
        if (t.closest("[data-bf-comment]")) {
            const wrap = card.querySelector(".bf-rate-comment");
            if (wrap) { wrap.hidden = !wrap.hidden; const ta = card.querySelector("textarea"); if (ta && !wrap.hidden) ta.focus(); }
            return;
        }
        if (t.closest("[data-bf-save-comment]")) {
            const ta = card.querySelector("textarea");
            benchfbRateRecord(recId, { comment: ta ? ta.value : "" });
            _refreshCard(card, recId);
            toast("评语已存");
            return;
        }
        if (t.closest("[data-bf-skip]")) {
            // 折叠档（none/error 轮 / 降频闸拦下的轮次）的「收起」＝收起回「评价」按钮
            // （展开态集合删除，不整张卡消失）；search/tool 主动卡保持既有语义——整卡收起、
            // 本会话不再挂回，且未评分即收起计入连续忽略（降频）。
            const rec = _findRecord(recId);
            if (rec && _collapsedRate(rec)) {
                _expandedRates.delete(recId);
                const heroMount = card.closest(".bf-hero-mount");
                if (heroMount) {
                    _renderHeroMount();
                    const b0 = heroMount.querySelector("[data-bf-rate-toggle]");
                    if (b0) b0.focus();
                } else {
                    const mount = card.closest("[data-bf-mount]");
                    if (mount) {
                        _renderMount(mount);
                        const btn = mount.querySelector("[data-bf-rate-toggle]");
                        if (btn) btn.focus();
                    }
                }
            } else {
                _saveRateSession(benchfbNoteDismissed(_rateSession(), recId, _isRated(rec)));
                _dismissCard(recId);
            }
            return;
        }
    });
    // 标注模式：结果区卡片点击委托（卡片重画不丢——监听器在容器上）
    const grid = _grid();
    if (grid) {
        grid.addEventListener("click", function (e) {
            if (!_markMode) return;
            const card = e.target && e.target.closest ? e.target.closest("article.card") : null;
            if (!card) return;
            e.preventDefault();
            e.stopPropagation();
            _toggleMark(card);
        }, true);
    }
    const markDone = $("bfMarkDone");
    if (markDone) markDone.addEventListener("click", function () { _exitMarkMode(false); _renderAllMounts(); });
    // 设置区 + 导出弹窗
    const exportBtn = $("benchfbExportBtn");
    if (exportBtn) exportBtn.addEventListener("click", function (e) { benchfbOpenExport(e.currentTarget); });
    const closeBtn = $("benchfbCloseBtn");
    if (closeBtn) closeBtn.addEventListener("click", benchfbCloseExport);
    const dlBtn = $("benchfbDownloadBtn");
    if (dlBtn) dlBtn.addEventListener("click", benchfbDownload);
    const clearBtn = $("benchfbClearBtn");
    if (clearBtn) clearBtn.addEventListener("click", benchfbClearWithConfirm);
    const det = $("benchfbPreviewWrap");
    if (det) det.addEventListener("toggle", benchfbTogglePreview);
    const modal = $("benchfbModal");
    if (modal) {
        modal.addEventListener("click", function (e) { if (e.target === modal) benchfbCloseExport(); });
        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape" && !modal.hidden) benchfbCloseExport();
        });
    }
    _syncPanel();
}

/* 账户切换：缓存作废（nsKey 命名空间随账户变），在途轮次、评分卡与 hero 槽位一并清。 */
export function benchfbOnAccountChanged() {
    _bfCache.clear();
    // 在途轮次保留其开工 scope；晚到响应仍落回旧账户，不能写进新账户。
    _lastClosed = null;
    _lastSearchRecId = null;
    _lastHeroRecId = null;
    _dismissAll();
    _syncPanel();
}
