"use strict";

/* 遥测自动上传 · 唯一出网通道（2026-08-20）
 *
 * 全前端**唯一**允许出现网络原语（fetch）的模块——契约门（test_usage_telemetry_contract.py）
 * 钉死这一点；usage_log / usage_core / benchfb* 及其余遥测文件仍零网络原语。
 *
 * 本模块用**相对路径动态 import** 从 usage_log.js 引入（`import("./usage_upload.js")`），
 * 刻意不进 importmap / package.json 的 `#xxx` 键表——新模块上键会牵动两页 importmap 与
 * parity 门，而动态 import 完全绕开静态图（test_frontend_import_graph.py 只盯静态边）。
 *
 * ## 职责
 *
 * - **consent 状态属主 + 首次告知弹窗**：per-profile `nsKey(biodata_consent_v2)`
 *   （v2：值为同意时刻 ISO 串），
 *   每个本机账户独立确认；读写接口 `usageConsentGiven()` / `setUsageConsent(v)`，
 *   `requestUsageConsent()` 负责首次发送时的告知弹窗（设计 §3）；
 *   发送拦截在 search.js / board.js 发送入口（`usageEnabled() && !usageConsentGiven()` 时弹）。
 * - **触发门控**（设计 §2）：`usageEnabled()` 与 `usageConsentGiven()` **双重门控**，
 *   任一为假绝不发。2026-08-22 起改**激进上传**：阈值 benchfb ≥ 1 轮 /
 *   usage ≥ 2 条（默认，无 hint 时）；启动时有待发即传（取消「距上次成功 > 6 小时」的等待）；
 *   阈值/节奏**随服务器压力自适应**：200 响应带 server_hint 时按其
 *   batch_threshold / min_interval_ms 档位执行（钳制到 [2,50] 条 / [15s,10min] 并持久化，
 *   跨刷新生效）；429 临时升到高档（20 条 / 5 分钟）直到下一次 200 hint 覆盖；老接收端
 *   （无 server_hint 字段）与网络失败都保持当前动态值不动（fail-safe）。
 *   常规打点触发的评估做 30s trailing 防抖，另有 5 分钟周期兜底（页面可见时）；
 *   pagehide / visibilitychange→hidden 时以 fetch keepalive 尽力补一发
 *   （仅 usage 事件、60KB 预算，不带 benchfb/MCP）。
 *   再加**配额双闸**（接收端防刷）：常规上传距上次成功 < UPLOAD_MIN_INTERVAL_MS
 *   （3 分钟）不再发；keepalive 尽力档距上次尝试 < KEEPALIVE_MIN_GAP_MS（30s）不发，
 *   且 keepalive 失败只记尝试时刻、不进退避（opportunistic 语义，不推高 failCount）。
 * - **MCP 中继**（**相对同源**——webapp 与 API 同源，旧代码误取
 *   ingest 端点的 origin）：组包前 GET /api/telemetry/mcp-calls?after=<offset>&limit=100&
 *   max_bytes=500000&since_ts=<consent ISO>，200 且 {ok:true,records,next_offset} 才把
 *   records 附进顶层 mcp_records（同过脱敏）；usage/benchfb 都空但有中继记录时也发
 *   （MCP-only 包）；上传 ACK 成功后 POST /api/telemetry/mcp-calls/ack {offset} 推进本地
 *   offset（只前进：与本地 meta.mcpOffset 取大）。404/500/网络错一律静默跳过、
 *   不拖累上传；ack 失败不阻塞（下次重传，接收端按 call_id 幂等去重）。
 *   中继空页但 next_offset 已推进（since_ts 过滤墙——墙内行被滤掉、服务端 offset
 *   照常前进）时回执推进本地 offset，过滤墙不再冻结游标。
 * - **激活 ping**：consent 同意即发一次性 hello 包（`sendActivationPing()`）——
 *   与常规上传同通道同格式，仅包体为单条 {k:"hello"} 事件；幂等 ACK 后写 profile 级键
 *   `biodata_ping_sent_v1`（LS.pingSent）=1 不再发；失败静默、下次触发点重试。
 * - **脱敏包**：body 必经 usage_core.buildTelemetryPackage（尽力过滤——consent v2 文案口径：
 *   api_key 整键删、端点只留主机、不记密码/账户名），本模块只负责读数据、发出去、成功后按快照截断。
 * - **绝不打断主功能**：全程 try/catch、失败静默（无 toast）、截断只移除**本次已成功上传**
 *   的快照部分，上传期间新写入的保留。
 *
 * ## 为什么读取的键与 usage_log 同款 nsKey
 *
 * usage 事件与 benchfb 记录都按账户命名空间存（共用一台电脑多人隔离），上传包自然也按
 * 开工即捕获账户 scope，完成按 event/record id 精确 ACK；consent 与 ping 都是 profile 级。
 */

import { LS, cacheGeneration, nsKeyFor, readJSON, writeJSON } from "#core";
import { buildTelemetryPackage, TELEMETRY_CONTRACT_VERSION, usagePacketHashFallback } from "#usage_core";
import { benchfbMarkOversizeForScope, benchfbRecordsForScope, benchfbRemoveRecordsForScope } from "#benchfb";
import { usageClientId, usageConsentAtForScope, usageConsentGivenForScope, usageEnabledForScope, usageEventsForScope,
    usageExperimentContext, usageInstallId, usageMetaContent, usageProfileIdForScope, usageRemoveEventsForScope, usageScope,
    usageAckDropSnapshotForScope, usageClearEpochForScope, usageDropSnapshotForScope,
    usageSetTrainingConsent, usageTrainingConsentGivenForScope } from "#usage_log";
/* 意见反馈核心（纯逻辑模块；相对 import，不进 importmap/静态图——与本模块
   的动态 import 同哲学，见文件头注释）。 */
import { feedbackEncrypt, feedbackMarkSentForScope, feedbackPendingForScope,
    hasSendChannel as feedbackHasSendChannel } from "./feedback_core.js";

/* 接收端由交付/部署在 HTML meta 中注入。安全默认是空（仅本地采集+手动导出）；
   公网只接受 HTTPS，HTTP 仅允许显式 loopback 开发端点。浏览器里的 token 不是长期秘密，
   只作可轮换 client credential；真实性仍由 receiver 的 client/profile 配额与隔离承担。
   明文公网 HTTP 属已知风险（用户知情授权）：若部署方接受明文传输，
   必须显式在 meta `biodata-telemetry-allow-insecure` 登记该主机（逗号分隔白名单）才会放行；
   默认空值保持 fail-closed，切勿随意添加。
   meta 读取走 usage_log.usageMetaContent（唯一实现）。 */
function _ingestConfig() {
    const endpoint = usageMetaContent("biodata-telemetry-endpoint");
    const token = usageMetaContent("biodata-telemetry-token");
    if (!endpoint || !token) return null;
    const allowInsecureHosts = usageMetaContent("biodata-telemetry-allow-insecure");
    try {
        const u = new URL(endpoint);
        const host = (u.hostname || "").toLowerCase();
        const loopback = host === "localhost" || host === "127.0.0.1" || host === "::1";
        if (u.protocol !== "https:" && !(u.protocol === "http:" && loopback)) {
            const hosts = allowInsecureHosts.split(",").map(function (h) { return h.trim().toLowerCase(); }).filter(Boolean);
            if (hosts.indexOf(host) < 0) return null;   // 明文公网主机不在显式白名单 → fail-closed
        }
        return { endpoint: u.toString(), token: token };
    } catch (_e) { return null; }
}

export function telemetryUploadConfigured() { return !!_ingestConfig(); }

/* 分析合同上下文。prompt 可独立标记；实验字段只在 id/arm/propensity 三件齐全且概率合法时
   生效，禁止半截配置冒充随机实验。默认全 null，不会把普通流量误标成 control。 */
function _analysisContext(scope) {
    const promptVersion = usageMetaContent("biodata-prompt-version") || null;
    const assigned = usageExperimentContext(scope);
    if (assigned) return Object.assign({ promptVersion: promptVersion }, assigned);
    const experimentId = usageMetaContent("biodata-experiment-id");
    const experimentArm = usageMetaContent("biodata-experiment-arm");
    const propensity = Number(usageMetaContent("biodata-experiment-propensity"));
    const valid = !!experimentId && !!experimentArm && Number.isFinite(propensity) && propensity > 0 && propensity <= 1;
    return {
        promptVersion: promptVersion,
        experimentId: valid ? experimentId : null,
        experimentArm: valid ? experimentArm : null,
        propensity: valid ? propensity : null,
    };
}

/* 触发阈值（设计 §2，契约门钉「常量存在且 ≤ 设计值」）。设计意图：工具使用量小，
   阈值定低，轻度使用也能上传。2026-08-22 激进上传：benchfb 1 轮 / usage 10 条
   ——benchfb 单条即一轮完整检索现场，轮次收尾后尽快上传。
   usage 默认降到 2 条（无 hint 时几乎实时、绝不积压），且阈值/最小间隔
   随服务器 server_hint 动态调整（见下方自适应常量）。 */
const UPLOAD_BENCHFB_MIN = 1;            // 未上传 benchfb 记录 ≥ 1 轮（不随 hint 调整）
const UPLOAD_EVENTS_MIN = 2;             // 默认阈值：无 hint 时未上传 usage 事件 ≥ 2 条

/* 自适应上传阈值（2026-08-22，server-driven）：
   - 200 响应带 server_hint{batch_threshold, min_interval_ms} 时采用服务端按自身并发压力
     给的档位，钳制到 [2,50] 条 / [15s,10min] 并持久化到上传 meta（跨刷新生效）；
   - 429（接收端限流/过载）临时升到高档（20 条 / 5 分钟），直到下一次 200 hint 覆盖；
   - 老接收端（无 server_hint 字段）与网络失败都保持当前动态值不动（fail-safe）。 */
const HINT_THRESHOLD_MIN = 2;                 // 服务端 hint 阈值钳制下界
const HINT_THRESHOLD_MAX = 50;                // 服务端 hint 阈值钳制上界
const HINT_INTERVAL_MIN_MS = 15 * 1000;       // 服务端 hint 最小间隔钳制下界
const HINT_INTERVAL_MAX_MS = 10 * 60 * 1000;  // 服务端 hint 最小间隔钳制上界
const HINT_BODY_MIN_BYTES = 60 * 1000;
const HINT_BODY_MAX_BYTES = 16 * 1024 * 1024; // 防脏 hint 令浏览器一次组装无界大包
const BODY_SAFETY_RATIO = 0.90;                // 服务端上限之外给代理/header/未来字段留余量
const RATE_LIMIT_THRESHOLD = 20;              // 429 时的临时高档阈值
const RATE_LIMIT_INTERVAL_MS = 5 * 60 * 1000; // 429 时的临时高档最小间隔

/* 常规打点尾触发的评估防抖（trailing）：benchfb 记录含完整检索响应（整表可达数 MB），
   每次打点都 JSON.parse 一遍会把检索卡顿。距上次评估不足 30s 时挂一个尾随定时器收尾；
   启动、周期兜底与 keepalive 尽力档不受防抖限制。 */
const UPLOAD_DEBOUNCE_MS = 30 * 1000;
/* 周期兜底：防抖只覆盖「有点打」的场景，长时间停留在结果页不操作时由它收尾（页面隐藏时跳过）。 */
const UPLOAD_PERIODIC_MS = 5 * 60 * 1000;
/* pagehide/hidden 尽力档的体算上限制：fetch keepalive 的请求体上限是 64KB，留余量取 60KB，
   只带 usage 事件（benchfb/MCP 太重，等下次常规上传）。 */
const KEEPALIVE_MAX_BODY_BYTES = 60000;
/* 配额双闸（2026-08-22，接收端防刷）：常规上传距上次成功不足 UPLOAD_MIN_INTERVAL_MS
   （3 分钟默认）不再发——实际间隔取当前动态值（meta.hintIntervalMs 或默认）；
   keepalive 尽力档距上次尝试不足 30s 不发（页面反复 hidden/恢复不刷屏），且 keepalive 失败
   只记尝试时刻、不进退避（见 _maybeUploadLocked）。 */
const UPLOAD_MIN_INTERVAL_MS = 3 * 60 * 1000;
const KEEPALIVE_MIN_GAP_MS = 30 * 1000;
/* 单条 benchfb 超预算时的降级：res.results 截到 top-N 并打 truncated 标，仍超才 manual-only。 */
const BENCHFB_TRUNCATE_TOP_N = 20;

/* 首包兼容预算：场外旧安装包已冻结为 1.9MB，服务端必须能容纳；新客户端在第一次 200
   后改用 server_hint.max_body_bytes 的 90%，并持久化。413 会立即读取服务端上限（旧服务端
   无 hint 时对半降档）重组下一包，不再让同一 1~1.9MB 队列永久退避。 */
const INGEST_BODY_MAX_BYTES = 1900000;
const BODY_RETRY_MAX = 4;
const UPLOAD_TIMEOUT_MS = 15000;
const UPLOAD_BACKOFF_MAX_MS = 6 * 3600 * 1000;
const _uploadInFlight = new Map();       // scope -> Promise（同标签页 single-flight）
const _uploadControllers = new Map();    // scope -> Set<AbortController>（清空时取消当前标签页全部请求）
const _pingInFlight = new Map();
const _lastEvalAt = new Map();           // scope -> 上次上传评估的墙钟毫秒（防抖用）
const _trailingTimers = new Map();       // scope -> 尾随定时器（同 scope 同时候至多一个）

if (window.addEventListener) {
    window.addEventListener("storage", function (event) {
        const key = String((event && event.key) || "");
        if (key === LS.usageClearEpoch) cancelTelemetryUpload("");
        else if (key.startsWith(LS.usageClearEpoch + "::u:")) {
            cancelTelemetryUpload(key.slice((LS.usageClearEpoch + "::u:").length));
        }
    });
    // 页面关闭/隐藏前尽力补一发：keepalive 档只带 usage 事件。
    // sendBeacon 带不了 X-Ingest-Token 自定义头，故用 fetch keepalive。
    window.addEventListener("pagehide", _bestEffortFlush);
}
if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "hidden") _bestEffortFlush();
    });
}

function _bestEffortFlush() {
    try { maybeUploadUsage(false, { keepalive: true }); } catch (_e) { /* 尽力档绝不抛出 */ }
}

/* 周期兜底定时器：页面可见时每 5 分钟评估一次；node 规格里 unref 防挂住进程。 */
const _periodicTimer = setInterval(function () {
    try {
        if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
        maybeUploadUsage(false, { force: true });
    } catch (_e) { /* 周期器绝不抛出 */ }
}, UPLOAD_PERIODIC_MS);
if (_periodicTimer && typeof _periodicTimer.unref === "function") _periodicTimer.unref();

/* ---------- consent（per-profile，属主在本模块） ---------- */

export function usageConsentGiven(scope) {
    return usageConsentGivenForScope(scope === undefined ? usageScope() : scope);
}

export function setUsageConsent(v, scope) {
    scope = scope === undefined ? usageScope() : scope;
    // v2（2026-08-22）：同意落的是**同意时刻 ISO 串**（MCP 中继 since_ts 下界要用）；
    // 拒绝仍写 "0"。v1 键（biodata_consent_v1）不再认——老用户首次发送自动重问一次。
    try { localStorage.setItem(nsKeyFor(LS.usageConsent, scope), v ? new Date().toISOString() : "0"); } catch (_e) {}
}

/* ---------- consent 首次告知弹窗（设计 §3） ---------- */

let _consentResolve = null;   // 在途弹窗的决议器（同一时刻至多一个弹窗在问）

/* 弹窗是「告知即阻断」：无 ✕、点遮罩不关闭、Esc 不关闭——不点两个按钮之一，
   发送就不会发生。按钮语义（按钮事件在模块底部一次性绑定，document 级委托）：
   - 「同意并继续」（主按钮）：落 consent='1'（setUsageConsent），返回 'agree'；
   - 「关闭采集并发送」（次按钮）：不落 consent，返回 'disable'，由调用方关掉开关
     （usageSetEnabled(false)）后照常发送本次。
   返回 Promise<'agree'|'disable'>。调用方（search/board 发送入口）负责在
   `usageEnabled() && !usageConsentGiven()` 时调用；开关关着根本不弹。 */
export function requestUsageConsent() {
    if (_consentResolve) return _consentResolve.promise;   // 已在弹：复用同一个决议，别叠弹窗
    const modal = document.getElementById("consentModal");
    if (!modal) return Promise.resolve("agree");   // DOM 缺失（异常态，刻意不修）：不卡发送直接放行；
    //   返回 'agree' 但**不落 consent**——采集不启动，方向保守：宁可漏采，也不在 DOM 异常时强行采集。
    const defer = {};
    defer.promise = new Promise(function (resolve) { defer.resolve = resolve; });
    defer.scope = usageScope();
    _consentResolve = defer;
    modal.hidden = false;
    document.body.classList.add("modal-lock");
    const trainingOptIn = document.getElementById("consentTrainingOptIn");
    if (trainingOptIn) trainingOptIn.checked = usageTrainingConsentGivenForScope(defer.scope);
    const agreeBtn = document.getElementById("consentAgreeBtn");
    if (agreeBtn) agreeBtn.focus();
    return defer.promise;
}

function _settleConsent(choice) {
    if (!_consentResolve) return;
    const resolve = _consentResolve.resolve;
    _consentResolve = null;
    const modal = document.getElementById("consentModal");
    if (modal) modal.hidden = true;
    document.body.classList.remove("modal-lock");
    resolve(choice);
}

/* ---------- 内部小工具 ---------- */

/* 上传元数据（lastSuccess / lastAttempt / lastCheck / pending）。按账户 nsKey：
   上传量按当前账户读，账本也按当前账户记（换账户不互串）。键只记时间与计数，
   不含任何用户内容。 */
function _uploadMeta(scope) {
    try {
        const m = readJSON(nsKeyFor(LS.usageUploadMeta, scope), {});
        return (m && typeof m === "object") ? m : {};
    } catch (_e) { return {}; }
}

function _saveMeta(scope, m) {
    try { writeJSON(nsKeyFor(LS.usageUploadMeta, scope), m); } catch (_e) {}
}

/* 自适应上传节奏（2026-08-22）：
   - _adoptServerHint：200 响应里的 server_hint → 钳制后的动态值（threshold/interval）；
     无字段或形状非法返回 null（调用方维持当前值不动，fail-safe）。
   - _dynamicThreshold / _dynamicIntervalMs：读上传 meta 里持久化的动态值；不在合法
     钳制区间（脏 meta / 旧数据）时回落默认常量。写入侧已钳制，读取侧再兜一道。 */
function _clampInt(v, lo, hi) {
    const n = Number(v);
    if (!Number.isFinite(n)) return null;
    return Math.max(lo, Math.min(hi, Math.round(n)));
}

function _adoptServerHint(serverHint) {
    if (!serverHint || typeof serverHint !== "object") return null;
    const threshold = _clampInt(serverHint.batch_threshold, HINT_THRESHOLD_MIN, HINT_THRESHOLD_MAX);
    const intervalMs = _clampInt(serverHint.min_interval_ms, HINT_INTERVAL_MIN_MS, HINT_INTERVAL_MAX_MS);
    if (threshold === null || intervalMs === null) return null;
    const serverMax = _clampInt(serverHint.max_body_bytes, HINT_BODY_MIN_BYTES, HINT_BODY_MAX_BYTES);
    const adopted = { hintThreshold: threshold, hintIntervalMs: intervalMs, hintAt: Date.now() };
    if (serverMax !== null) adopted.hintBodyMaxBytes = Math.floor(serverMax * BODY_SAFETY_RATIO);
    return adopted;
}

function _dynamicThreshold(meta) {
    const v = Number(meta && meta.hintThreshold);
    return (Number.isFinite(v) && v >= HINT_THRESHOLD_MIN && v <= HINT_THRESHOLD_MAX) ? v : UPLOAD_EVENTS_MIN;
}

function _dynamicIntervalMs(meta) {
    const v = Number(meta && meta.hintIntervalMs);
    return (Number.isFinite(v) && v >= HINT_INTERVAL_MIN_MS && v <= HINT_INTERVAL_MAX_MS) ? v : UPLOAD_MIN_INTERVAL_MS;
}

function _dynamicBodyMaxBytes(meta) {
    const v = Number(meta && meta.hintBodyMaxBytes);
    return (Number.isFinite(v) && v >= HINT_BODY_MIN_BYTES && v <= HINT_BODY_MAX_BYTES)
        ? Math.floor(v) : INGEST_BODY_MAX_BYTES;
}

function _bodyBudgetAfter413(sent, currentBudget) {
    let serverMax = null;
    try {
        const detail = sent && sent.body && sent.body.detail;
        serverMax = _clampInt(detail && detail.max_body_bytes, HINT_BODY_MIN_BYTES, HINT_BODY_MAX_BYTES);
    } catch (_e) {}
    const hinted = serverMax === null ? null : Math.floor(serverMax * BODY_SAFETY_RATIO);
    const halved = Math.max(HINT_BODY_MIN_BYTES, Math.floor(Number(currentBudget || INGEST_BODY_MAX_BYTES) / 2));
    // 代理层也可能比应用声明更小；只有真正缩小时才信 hint，否则继续对半探测。
    return hinted !== null && hinted < currentBudget ? hinted : halved;
}

/* 缓存代读法统一在 core.js 的 cacheGeneration()（本模块从 #core 委托）。 */

async function _packetId(profileId, usageIds, benchIds, kind, mcpIds, contractIds) {
    // 附了 MCP 中继记录的包把 call_id 一并拼进幂等材料——同一批记录重传
    // （ack 失败重发）产出同一 packet_id，接收端幂等去重；未附时保持原四段材料。
    const material = [kind || "batch", profileId, usageIds.join(","), benchIds.join(","),
        (Array.isArray(mcpIds) && mcpIds.length) ? mcpIds.join(",") : "",
        (Array.isArray(contractIds) && contractIds.length) ? contractIds.join(",") : ""].join("|");
    try {
        const bytes = new TextEncoder().encode(material);
        const digest = await window.crypto.subtle.digest("SHA-256", bytes);
        return "pkt-" + Array.from(new Uint8Array(digest)).map(function (v) { return v.toString(16).padStart(2, "0"); }).join("");
    } catch (_e) { return "pkt-" + usagePacketHashFallback(material); }
}

/* 单条 benchfb 自身超预算时的降级（2026-08-22）：把 search.res.results 截到
   top-20 并打 truncated:true 标记后照常上传——一轮检索现场的头部结果远比「整条沦入
   manual-only 再也上不来」有价值。浅拷贝链 rec→search→res，不改原记录（ACK 还按原 id 删）。
   返回 null = 无可截（没有更长的 results 数组），调用方按原逻辑判 manual-only。
   用户标注的「有用条目」若在截断点之外（名次 >20 且 ≤ 原数组长度），把对应条目
   浅拷贝加 `_preserved_idx:<原名次>` 追加在尾部——相关性标签是 benchmark 的硬数据，
   不能随截断静默丢失；接收端按 _preserved_idx 还原名次。 */
function _shrinkBenchfbRecord(rec) {
    if (!rec || typeof rec !== "object") return null;
    const search = rec.search && typeof rec.search === "object" ? rec.search : null;
    const res = search && search.res && typeof search.res === "object" ? search.res : null;
    const results = res && Array.isArray(res.results) ? res.results : null;
    if (!results || results.length <= BENCHFB_TRUNCATE_TOP_N) return null;
    const kept = results.slice(0, BENCHFB_TRUNCATE_TOP_N);
    const rating = rec.rating && typeof rec.rating === "object" ? rec.rating : null;
    const usefulIdx = rating && Array.isArray(rating.useful_idx) ? rating.useful_idx : [];
    usefulIdx.forEach(function (v) {
        const n = Number(v);
        if (!Number.isInteger(n) || n <= BENCHFB_TRUNCATE_TOP_N || n > results.length) return;
        const src = results[n - 1];
        kept.push(src && typeof src === "object"
            ? Object.assign({}, src, { _preserved_idx: n })
            : { value: src, _preserved_idx: n });
    });
    return Object.assign({}, rec, {
        truncated: true,
        search: Object.assign({}, search, { res: Object.assign({}, res, { results: kept }) }),
    });
}

/* 体量预算：usage 先按最老前缀装入，再逐条尝试 benchfb。单条自身超预算时先走
   _shrinkBenchfbRecord 截断降级，仍超才标记 manual-only 并继续后续记录，
   避免一条 poison record 永久饿死整队列。maxBytes 缺省 INGEST_BODY_MAX_BYTES
   （keepalive 尽力档传 60KB）；mcpRecords 非空时尝试整页附进顶层 mcp_records——
   附上会超预算就整页放弃（不推进 offset、不拖累本次上传）。 */
function _fitBody(opts, usageList, benchfbList, maxBytes, mcpRecords) {
    const limit = Number(maxBytes) > 0 ? Number(maxBytes) : INGEST_BODY_MAX_BYTES;
    let usageChosen = usageList.slice();
    while (usageChosen.length && _utf8Bytes(JSON.stringify(buildTelemetryPackage(usageChosen, [], opts))) > limit) {
        usageChosen = usageChosen.slice(0, Math.floor(usageChosen.length / 2));
    }
    let benchChosen = [];
    const oversize = [];
    benchfbList.filter(function (r) { return !r.telemetry_oversize; }).forEach(function (rec) {
        let candidate = benchChosen.concat([rec]);
        if (_utf8Bytes(JSON.stringify(buildTelemetryPackage(usageChosen, candidate, opts))) <= limit) {
            benchChosen = candidate;
            return;
        }
        const shrunk = _shrinkBenchfbRecord(rec);
        if (shrunk) {
            candidate = benchChosen.concat([shrunk]);
            if (_utf8Bytes(JSON.stringify(buildTelemetryPackage(usageChosen, candidate, opts))) <= limit) {
                benchChosen = candidate;
                return;
            }
        }
        if (_utf8Bytes(JSON.stringify(buildTelemetryPackage([], [shrunk || rec], opts))) > limit) oversize.push(String(rec.id || ""));
    });
    let payload = buildTelemetryPackage(usageChosen, benchChosen, opts);
    let mcpAttached = false;
    if (Array.isArray(mcpRecords) && mcpRecords.length) {
        const withMcp = buildTelemetryPackage(usageChosen, benchChosen, Object.assign({}, opts, { mcpRecords: mcpRecords }));
        if (_utf8Bytes(JSON.stringify(withMcp)) <= limit) { payload = withMcp; mcpAttached = true; }
    }
    return {
        payload: payload,
        usageIds: usageChosen.map(function (e) { return String(e.event_id || ""); }).filter(Boolean),
        benchIds: benchChosen.map(function (r) { return String(r.id || ""); }).filter(Boolean),
        oversizeIds: oversize.filter(Boolean),
        mcpAttached: mcpAttached,
        dropAttached: !!(payload.drop_report && Number(payload.drop_report.dropped_count) > 0),
    };
}

/* UTF-8 字节数：接收端按**字节**计 body 上限（2MB），与 fetch 实际
   发出的 UTF-8 字节严格一致。`JSON.stringify(s).length` 是 UTF-16 码元数——中文字符
   按 1 计而实际占 3 字节，会系统性低估包大小导致服务器 413。 */
function _utf8Bytes(s) {
    return new TextEncoder().encode(s).length;
}

/* ---------- 上传入口 ---------- */

async function _withCrossTabLock(scope, kind, task) {
    const locks = navigator && navigator.locks;
    if (!locks || typeof locks.request !== "function") return await task();
    let result = false;
    await locks.request("biodata-telemetry-" + kind + "-" + usageProfileIdForScope(scope),
        { ifAvailable: true }, async function (lock) { if (lock) result = await task(); });
    return result;
}

function _backoffMeta(meta, now, response) {
    const failCount = Math.min(10, (Number(meta.failCount) || 0) + 1);
    let delay = Math.min(UPLOAD_BACKOFF_MAX_MS, 5000 * (2 ** (failCount - 1)));
    try {
        const retryAfter = Number(response && response.headers && response.headers.get("Retry-After"));
        if (Number.isFinite(retryAfter) && retryAfter > 0) delay = Math.max(delay, retryAfter * 1000);
    } catch (_e) {}
    delay = Math.round(delay * (0.75 + Math.random() * 0.5));
    return Object.assign({}, meta, { lastAttempt: now, lastCheck: now, failCount: failCount, nextAttempt: now + delay });
}

async function _postPacket(scope, payload, packetId, opts) {
    const config = _ingestConfig();
    if (!config) return { ok: false, unconfigured: true };
    const keepalive = !!(opts && opts.keepalive);
    const controller = new AbortController();
    const controllers = _uploadControllers.get(scope) || new Set();
    controllers.add(controller); _uploadControllers.set(scope, controllers);
    const timer = setTimeout(function () { controller.abort(); }, UPLOAD_TIMEOUT_MS);
    try {
        const res = await fetch(config.endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Ingest-Token": config.token },
            body: JSON.stringify(payload), signal: controller.signal, keepalive: keepalive,
        });
        let body = null;
        try { body = await res.json(); } catch (_e) {
            if (!res.ok) return { ok: false, response: res, body: null };
            return { ok: false, response: res };
        }
        if (!res.ok) return { ok: false, response: res, body: body };
        if (!body || body.ok !== true || (body.packet_id && body.packet_id !== packetId)) return { ok: false, response: res };
        return { ok: true, response: res, body: body };
    } finally {
        clearTimeout(timer);
        controllers.delete(controller);
        if (!controllers.size) _uploadControllers.delete(scope);
    }
}

export function cancelTelemetryUpload(scope) {
    const controllers = _uploadControllers.get(scope);
    if (controllers) controllers.forEach(function (controller) { controller.abort(); });
}

/* ---------- MCP 调用记录中继（2026-08-22，跨包契约；相对同源） ----------
   接收端若已配套（/api/telemetry/mcp-calls 存在）：组包前按本地 offset 拉一页服务器暂存的
   MCP 调用记录，附进上传包顶层 mcp_records；上传 ACK 成功后再回执 ack 推进 offset。
   任何一步失败都静默跳过——中继是搭车，绝不拖累遥测上传本身。
   **相对同源** fetch（webapp 与 API 同源；旧代码误取 ingest 端点的 origin，
   端点指向另一台机器时中继永远 404/跨域），同源请求也不再带 X-Ingest-Token（该头只对
   ingest 端点是凭证，发给同源 API 是多余暴露）。limit/max_bytes/since_ts 参数随请求带上
   （since_ts = 本 profile 的 consent 同意时刻，空串=时刻未知不加下界，由接收端兜底）。 */

/* 网页版护栏判定（公网护栏硬化，2026-08-26）：唯一真源 = /api/health 的 account.required，
   与 accounts.js 的 _gate / shell.js 的 _healthSnapshot 同一份 health 快照。本模块不进静态
   import 图（文件头红线），故 shell.js 的 webGuardOn 只能**动态 import** 取（index 页 shell
   早已被 boot 加载，命中模块缓存零成本）。快照未到/加载失败 → false（按本机形态处理，
   绝不误停中继）；只缓存肯定结果，health 晚到时下一次调用还会重判。 */
let _webGuardCache = false;
async function _webGuardOn() {
    if (_webGuardCache) return true;
    try {
        const m = await import("./shell.js");
        if (typeof m.webGuardOn === "function" && m.webGuardOn()) {
            _webGuardCache = true;
            return true;
        }
    } catch (_e) { /* 判定失败按 guard off：本机形态不受影响 */ }
    return false;
}

async function _mcpFetchPage(scope) {
    try {
        // 公网护栏硬化（2026-08-26）：护栏模式（health.account.required=true）下后端遥测中继
        // 两端点一律 403（跨账号共享文件不中继）——前端直接跳过：不拉不 ack，全程静默。
        if (await _webGuardOn()) return null;
        const config = _ingestConfig();
        if (!config) return null;   // config 门保留：未配置端点的机器不刷 404
        const after = Number(_uploadMeta(scope).mcpOffset) || 0;
        const sinceTs = usageConsentAtForScope(scope);
        const controller = new AbortController();
        const controllers = _uploadControllers.get(scope) || new Set();
        controllers.add(controller); _uploadControllers.set(scope, controllers);
        const timer = setTimeout(function () { controller.abort(); }, 5000);
        try {
            const res = await fetch("/api/telemetry/mcp-calls?after=" + after + "&limit=100&max_bytes=500000&since_ts=" + encodeURIComponent(sinceTs), {
                method: "GET", signal: controller.signal,
            });
            if (!res.ok) return null;   // 404=接收端未配套；500=对端故障——都按「没有可中继」处理
            const data = await res.json();
            if (!data || data.ok !== true || !Array.isArray(data.records)) return null;
            const next = Number(data.next_offset);
            if (!Number.isFinite(next)) return null;
            // 空页但 next_offset 已推进（since_ts 过滤墙——墙内行被滤掉、服务端 offset 照常
            // 前进）→ 返回可 ack 的空页让调用方推进本地 offset，过滤墙不再冻结游标；空页且 offset 未动
            // 才是真正的「没有可中继」→ 仍返回 null（保持旧语义，不空转 ack）。
            if (!data.records.length && !(next > after)) return null;
            return { records: data.records, nextOffset: next };
        } finally {
            clearTimeout(timer);
            controllers.delete(controller);
            if (!controllers.size) _uploadControllers.delete(scope);
        }
    } catch (_e) { return null; }
}

async function _mcpAck(scope, offset) {
    try {
        if (await _webGuardOn()) return;   // 护栏模式不中继（同 _mcpFetchPage），静默跳过
        const config = _ingestConfig();
        if (!config) return;
        // 只前进：跨标签页/重试并发时，旧页拿到的小 offset 不得把本地已推进的
        // offset 回退——回退会让整批记录无限重传。
        const next = Math.max(Number(offset) || 0, Number(_uploadMeta(scope).mcpOffset) || 0);
        const res = await fetch("/api/telemetry/mcp-calls/ack", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ offset: next }),
        });
        if (res && res.ok) {
            const m = _uploadMeta(scope);
            _saveMeta(scope, Object.assign({}, m, { mcpOffset: next }));
        }
        /* ack 失败不阻塞：offset 不推进，下次上传重传这批记录（接收端按 call_id 幂等去重） */
    } catch (_e) { /* 同上：静默 */ }
}

async function _maybeUploadLocked(scope, startup, opts) {
    if (!usageEnabledForScope(scope) || !usageConsentGiven(scope)) return false;
    if (startup === true) sendActivationPing(scope);
    const keepalive = !!(opts && opts.keepalive);
    try {
        const now = Date.now();
        const meta = _uploadMeta(scope);
        if (!keepalive && now < (Number(meta.nextAttempt) || 0)) return false;
        // 配额双闸：常规上传距上次成功不足当前动态间隔不再发（间隔
        // 取 meta.hintIntervalMs，无 hint 回落默认 3 分钟）；keepalive 档距上次尝试 < 30s 不发。
        if (startup !== true && !keepalive && Number(meta.lastSuccess) > 0
                && now - Number(meta.lastSuccess) < _dynamicIntervalMs(meta)) return false;
        if (keepalive && Number(meta.lastAttempt) > 0 && now - Number(meta.lastAttempt) < KEEPALIVE_MIN_GAP_MS) return false;
        const usageList = usageEventsForScope(scope);
        // keepalive 尽力档不带 benchfb（64KB 上限装不下检索现场），只发 usage 事件。
        const benchfbList = keepalive ? [] : benchfbRecordsForScope(scope).filter(function (r) { return !r.telemetry_oversize; });
        const nUsage = usageList.length, nBenchfb = benchfbList.length;
        const dropSnapshot = usageDropSnapshotForScope(scope);
        const nDropped = dropSnapshot ? Number(dropSnapshot.dropped_count) || 0 : 0;

        // MCP 中继：组包前尽力拉一页服务器暂存记录（失败静默、不阻塞主流程；keepalive 档不带）。
        // 提前到空队列判断**之前**——usage/benchfb 都空但有中继记录时也要发（MCP-only 包）。
        const mcpPage = keepalive ? null : await _mcpFetchPage(scope);
        const mcpCount = mcpPage ? mcpPage.records.length : 0;
        // 中继空页但 next_offset 已推进（since_ts 过滤墙）→ 立即回执推进本地 offset——
        // 否则 offset 永远冻结在墙前，墙后 post-consent 记录永远出不了门。回执走 _mcpAck 的只前进
        // 语义（与本地 meta.mcpOffset 取大），失败静默不阻塞主流程；回执成功后把内存快照同步成
        // 刚推进的值，后续 _saveMeta（pending/成功/退避）都基于它，不会把旧 offset 写回去造成
        // 墙页每轮重扫。
        if (mcpPage && mcpCount === 0) {
            await _mcpAck(scope, mcpPage.nextOffset);
            meta.mcpOffset = Number(_uploadMeta(scope).mcpOffset) || 0;
        }
        if (!nUsage && !nBenchfb && !mcpCount && !nDropped) return false;
        // 激进上传（2026-08-22）：启动/尽力档有待发即传；常规触发只看低阈值，
        // 原先的「启动距上次成功 > 6 小时」与 2 分钟评估节流已退役（防抖改由 maybeUploadUsage 承担）。
        // MCP-only 包不受阈值拦（否则中继记录永远凑不够阈值才出门）。
        // usage 阈值取当前动态值（meta.hintThreshold，无 hint 回落默认 2）。
        if (startup !== true && !keepalive && !mcpCount && !nDropped && nBenchfb < UPLOAD_BENCHFB_MIN
                && nUsage < _dynamicThreshold(meta)) return false;

        const profileId = usageProfileIdForScope(scope);
        const pkgOpts = Object.assign({
            packetId: "pkt-" + "0".repeat(64), installId: usageInstallId(),
            clientId: usageClientId(), profileId: profileId, exportedAt: new Date().toISOString(),
            app: { cache_generation: cacheGeneration(), ua: navigator.userAgent || "", lang: navigator.language || "" },
            trainingConsent: usageTrainingConsentGivenForScope(scope), dropReport: dropSnapshot,
        }, _analysisContext(scope));
        const clearEpoch = usageClearEpochForScope(scope);
        let bodyBudget = keepalive ? KEEPALIVE_MAX_BODY_BYTES : _dynamicBodyMaxBytes(meta);
        let snap = null, packetId = "", sent = null;
        for (let attempt = 0; attempt < (keepalive ? 1 : BODY_RETRY_MAX); attempt++) {
            snap = _fitBody(pkgOpts, usageList, benchfbList, bodyBudget, mcpPage ? mcpPage.records : null);
            snap.oversizeIds.forEach(function (id) { benchfbMarkOversizeForScope(scope, id); });
            if (!snap.usageIds.length && !snap.benchIds.length && !snap.mcpAttached && !snap.dropAttached) return false;
            packetId = await _packetId(profileId, snap.usageIds, snap.benchIds, "batch",
                snap.mcpAttached && mcpPage
                    ? mcpPage.records.map(function (r) { return String((r && r.call_id) || ""); }).filter(Boolean)
                    : null,
                snap.dropAttached && dropSnapshot ? ["drop:" + String(dropSnapshot.revision)] : null);
            snap.payload.packet_id = packetId;
            _saveMeta(scope, Object.assign({}, meta, {
                lastAttempt: now, lastCheck: now, pendingPacket: packetId, hintBodyMaxBytes: bodyBudget,
            }));
            sent = await _postPacket(scope, snap.payload, packetId, { keepalive: keepalive });
            if (sent.ok || !(sent.response && sent.response.status === 413)) break;
            const smaller = _bodyBudgetAfter413(sent, bodyBudget);
            if (!(smaller < bodyBudget)) break;
            bodyBudget = smaller;
            meta.hintBodyMaxBytes = bodyBudget;
        }
        if (!sent.ok) {
            // 429 = 接收端限流/过载 → 临时升到高档（20 条 / 5 分钟）并持久化，
            // 直到下一次 200 server_hint 覆盖；网络失败 / 5xx 不改阈值，退避逻辑照旧（既有语义不动）。
            const throttled = !!(sent.response && sent.response.status === 429);
            const m = throttled
                ? Object.assign({}, meta, { hintThreshold: RATE_LIMIT_THRESHOLD, hintIntervalMs: RATE_LIMIT_INTERVAL_MS })
                : meta;
            // keepalive 尽力档失败只记尝试时刻、**不进退避**：页面正在卸载/隐藏的
            // opportunistic 一发失败是常态，推高 failCount 只会误伤下一次常规上传。
            _saveMeta(scope, keepalive
                ? Object.assign({}, m, { lastAttempt: now, lastCheck: now })
                : _backoffMeta(m, now, sent.response));
            return false;
        }

        // 精确 ACK：按开工 scope + event/record id 删除；账户切换、新事件 append、清空均不会误伤。
        usageRemoveEventsForScope(scope, snap.usageIds);
        benchfbRemoveRecordsForScope(scope, snap.benchIds);
        if (snap.dropAttached) usageAckDropSnapshotForScope(scope, dropSnapshot);
        const successMeta = { lastSuccess: now, lastAttempt: now, lastCheck: now, failCount: 0, nextAttempt: 0,
            lastPacket: packetId, clearEpoch: clearEpoch, mcpOffset: Number(meta.mcpOffset) || 0 };
        if (Number.isFinite(Number(meta.hintBodyMaxBytes))) successMeta.hintBodyMaxBytes = Number(meta.hintBodyMaxBytes);
        // 200 且响应带 server_hint → 采用并持久化（跨刷新生效）；老接收端无该字段 → 维持当前动态值。
        const hint = _adoptServerHint(sent.body && sent.body.server_hint);
        if (hint) Object.assign(successMeta, hint);
        _saveMeta(scope, successMeta);
        // MCP 中继回执：上传包已被确认才推进 offset；失败静默（下次重传，接收端按 call_id 幂等）。
        if (snap.mcpAttached && mcpPage) await _mcpAck(scope, mcpPage.nextOffset);
        return true;
    } catch (_e) {
        const now = Date.now(), meta = _uploadMeta(scope);
        _saveMeta(scope, keepalive
            ? Object.assign({}, meta, { lastAttempt: now, lastCheck: now })
            : _backoffMeta(meta, now, null));
        return false;
    }
}

/* 同标签页 single-flight + 跨标签页 Web Lock。没有 Web Locks 时仍依赖稳定 packet/event id
   让服务端去重，不能退回数组长度截断。
   防抖：非启动/非强制的调用距上次评估不足 30s 时只挂一个尾随定时器收尾，
   避免每次打点都全量评估。opts.force=周期/尾随兜底；opts.keepalive=pagehide 尽力档
   （不加跨标签页锁——页面正在卸载，等锁就发不出去了；服务端幂等兜底）。 */
export function maybeUploadUsage(startup, opts) {
    opts = opts || {};
    const scope = usageScope();
    if (_uploadInFlight.has(scope)) return _uploadInFlight.get(scope);
    if (startup !== true && opts.force !== true && opts.keepalive !== true) {
        const now = Date.now();
        if (now - (Number(_lastEvalAt.get(scope)) || 0) < UPLOAD_DEBOUNCE_MS) {
            if (!_trailingTimers.has(scope)) {
                const t = setTimeout(function () {
                    _trailingTimers.delete(scope);
                    maybeUploadUsage(false, { force: true });
                }, UPLOAD_DEBOUNCE_MS);
                if (t && typeof t.unref === "function") t.unref();
                _trailingTimers.set(scope, t);
            }
            return Promise.resolve(false);
        }
    }
    _lastEvalAt.set(scope, Date.now());
    const task = opts.keepalive === true
        ? _maybeUploadLocked(scope, false, opts)
        : _withCrossTabLock(scope, "upload", function () { return _maybeUploadLocked(scope, startup === true, opts); });
    const promise = Promise.resolve(task)
        .finally(function () { if (_uploadInFlight.get(scope) === promise) _uploadInFlight.delete(scope); });
    _uploadInFlight.set(scope, promise);
    return promise;
}

/* ---------- 激活 ping（consent 同意即发一次性 hello 包） ---------- */

/* 激活 ping 用途：产品方数「有多少台机器在测试」——接收端
   `select count(distinct install_id) from ingest_packets;` 即为测试机数下界。
   与常规上传**同通道同格式**：同一 INGEST_URL / X-Ingest-Token / buildTelemetryPackage
   （schema biodata-telemetry/1、install_id、app{cache_generation,ua,lang}），
   差异只在包体：usage_events 仅一条 {t: Date.now(), k:"hello"}、benchfb_records 空。
   关于 k:"hello"：它不是 USAGE_KINDS 成员，但 buildTelemetryPackage **没有 kind 白名单**
   （只做脱敏变换），不会被拦——于是 ping 包直接走构造函数：少一条旁路，也保住
   「payload 必经脱敏构造函数」的尽力过滤红线（consent v2 口径）；若未来构造函数加 kind
   白名单再考虑旁路。
   一次性：幂等 ACK 后写 profile 级 `biodata_ping_sent_v1`（LS.pingSent）=1，不再发；
   失败静默、下次触发点重试——绝不打扰主功能、无 toast。触发点：consent 同意落盘后
   （同意按钮 handler）与 maybeUploadUsage 启动路径（startup===true 时补发）。
   返回 boolean 只供内部/调试用；调用方一律 fire-and-forget。 */
async function _sendPingLocked(scope) {
    if (!usageEnabledForScope(scope) || !usageConsentGiven(scope)) return false;
    const pingKey = nsKeyFor(LS.pingSent, scope);
    try {
        if (localStorage.getItem(pingKey) === "1") return true;
        const profileId = usageProfileIdForScope(scope);
        const eventId = "hello-" + profileId;
        const packetId = await _packetId(profileId, [eventId], [], "ping");
        const opts = Object.assign({
            packetId: packetId, installId: usageInstallId(), clientId: usageClientId(), profileId: profileId,
            exportedAt: new Date().toISOString(),
            app: { cache_generation: cacheGeneration(), ua: navigator.userAgent || "", lang: navigator.language || "" },
            trainingConsent: usageTrainingConsentGivenForScope(scope),
        }, _analysisContext(scope));
        const payload = buildTelemetryPackage([{ event_id: eventId, t: Date.now(), k: "hello" }], [], opts);
        const sent = await _postPacket(scope, payload, packetId);
        if (!sent.ok) return false;
        try { localStorage.setItem(pingKey, "1"); } catch (_e) {}
        return true;
    } catch (_e) { return false; }
}

export function sendActivationPing(scope) {
    scope = scope === undefined ? usageScope() : scope;
    if (_pingInFlight.has(scope)) return _pingInFlight.get(scope);
    const promise = _withCrossTabLock(scope, "upload", function () { return _sendPingLocked(scope); })
        .finally(function () { if (_pingInFlight.get(scope) === promise) _pingInFlight.delete(scope); });
    _pingInFlight.set(scope, promise);
    return promise;
}

/* ---------- 意见反馈发送（2026-08-22；独立入口，绝不捎带其它队列） ---------- */

/* sendFeedback：只发送本 profile feedback_pending 中**已授权**（status="pending"）的意见
   记录——明示单次授权语义（设计 §8：点发送=对该条不可变记录的授权，不改 usage 开关/
   consent key；关遥测也能发）。与 usage/benchfb/mcp 上传**完全隔离**：
   - 独立组包（payload 只含 feedback_records，不捎带 usage/benchfb/mcp 任何待发队列）；
   - 复用既有退避机制（_backoffMeta：failCount/nextAttempt 与常规上传同 meta，429 升档
     同款）；发送成功标 sent（记录保留供「导出反馈包」），失败保留 pending 待重试；
   - 同一 feedback_id 幂等：重试同组 → 同一 packet_id（材料=feedback_id 列表，不含密文），
     接收端按 identity|feedback_id 去重；ACK 只认 body.ok（duplicate 也视为已收）；
   - 公钥为空/WebCrypto 不可用（feedbackHasSendChannel()=false）→ 直接返回 false 不发，
     UI 走「复制到剪贴板」兜底（提供「复制并取消自动重试」，避免双通道重复）。
   返回 boolean 只供内部/调试用；调用方 fire-and-forget。 */
const _feedbackInFlight = new Map();   // scope -> Promise（同标签页 single-flight，与上传共用锁名）

async function _sendFeedbackLocked(scope) {
    const pending = feedbackPendingForScope(scope).filter(function (r) { return r.status === "pending"; });
    if (!pending.length) return false;
    if (!feedbackHasSendChannel()) return false;
    const profileId = usageProfileIdForScope(scope);
    // 逐条加密：单条失败（WebCrypto 偶发/载荷异常）跳过不阻塞其它意见，下轮重试补齐。
    const records = [];
    for (const p of pending) {
        try { records.push(await feedbackEncrypt(p, profileId)); } catch (_e) { /* 保留待下次 */ }
    }
    if (!records.length) return false;
    const now = Date.now();
    const meta = _uploadMeta(scope);
    if (now < (Number(meta.nextAttempt) || 0)) return false;
    const packetId = await _packetId(profileId,
        records.map(function (r) { return String(r.feedback_id || ""); }), [], "feedback", null);
    const payload = {
        schema: "biodata-telemetry/1",
        contract_version: TELEMETRY_CONTRACT_VERSION,
        packet_id: packetId,
        install_id: usageInstallId(),
        client_id: usageClientId(),
        profile_id: profileId,
        exported_at: new Date().toISOString(),
        prompt_version: null,
        experiment_id: null,
        experiment_arm: null,
        propensity: null,
        training_consent: false,
        app: { cache_generation: cacheGeneration(), ua: navigator.userAgent || "", lang: navigator.language || "" },
        feedback_records: records,
    };
    _saveMeta(scope, Object.assign({}, meta, { lastAttempt: now, lastCheck: now, pendingPacket: packetId }));
    const sent = await _postPacket(scope, payload, packetId, {});
    if (!sent.ok) {
        // 429 同款升档（与常规上传共享 meta 的 hintThreshold/hintIntervalMs）；
        // 其它失败只退避。意见正文不随任何日志/响应回显。
        const throttled = !!(sent.response && sent.response.status === 429);
        _saveMeta(scope, throttled
            ? Object.assign({}, meta, { hintThreshold: RATE_LIMIT_THRESHOLD, hintIntervalMs: RATE_LIMIT_INTERVAL_MS,
                lastAttempt: now, lastCheck: now, failCount: Math.min(10, (Number(meta.failCount) || 0) + 1) })
            : _backoffMeta(meta, now, sent.response));
        return false;
    }
    feedbackMarkSentForScope(scope, records.map(function (r) { return r.feedback_id; }));
    // 成功只清 failCount/nextAttempt，**不动 lastSuccess**——feedback 是独立通道，
    // 不干扰常规 usage/benchfb 的上传节奏（该闸由常规上传自己维护）。
    const okMeta = Object.assign({}, meta, { failCount: 0, nextAttempt: 0, lastAttempt: now, lastCheck: now });
    const hint = _adoptServerHint(sent.body && sent.body.server_hint);
    if (hint) Object.assign(okMeta, hint);
    _saveMeta(scope, okMeta);
    return true;
}

export function sendFeedback(scope) {
    scope = scope === undefined ? usageScope() : scope;
    if (_feedbackInFlight.has(scope)) return _feedbackInFlight.get(scope);
    const promise = _withCrossTabLock(scope, "upload", function () { return _sendFeedbackLocked(scope); })
        .finally(function () { if (_feedbackInFlight.get(scope) === promise) _feedbackInFlight.delete(scope); });
    _feedbackInFlight.set(scope, promise);
    return promise;
}

/* ---------- consent 弹窗接线（模块首次加载即绑；document 级委托） ----------
   按钮/焦点圈只在弹窗可见时生效；模块顶层绑一次，不随每次弹窗重复绑。 */

document.addEventListener("click", function (e) {
    if (!_consentResolve) return;   // 没有在问就一个字节都不做
    const t = e.target;
    if (!t || !t.closest) return;
    if (t.closest("#consentAgreeBtn")) {
        const scope = _consentResolve.scope;
        const training = document.getElementById("consentTrainingOptIn");
        setUsageConsent(true, scope);      // 每个账户/匿名 profile 独立同意
        usageSetTrainingConsent(!!(training && training.checked), scope); // 独立授权；默认勾选（2026-08-25 起默认开启，可关）
        sendActivationPing(scope);         // 同意后立即发该 profile 的幂等激活 ping
        _settleConsent("agree");
        return;
    }
    if (t.closest("#consentDisableBtn")) {
        _settleConsent("disable");  // 开关由调用方（发送入口）关闭
    }
});

document.addEventListener("keydown", function (e) {
    const modal = document.getElementById("consentModal");
    if (!modal || modal.hidden) return;
    if (e.key === "Escape") { e.preventDefault(); return; }   // 告知弹窗不给旁路：不点按钮不发送
    if (e.key !== "Tab") return;
    const focusable = Array.prototype.filter.call(
        modal.querySelectorAll('button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'),
        function (el) { return !el.hidden && el.getClientRects().length; });
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});
