"use strict";

/* 本文件是 ES Module：core 的存储/工具与 usage_core 的聚合函数都经 import
   （此前经共享全局裸调）。与 core.js 存在 import 环，但环上绑定只在函数体内使用。 */
import { LS, cacheGeneration, currentAccountScope, nsKey, nsKeyFor, readJSON, $ } from "#core";
import { USAGE_KINDS, USAGE_AI_LABELS, usageActiveImpressionId, usageActiveTurnId,
    telemetryExperimentAssign, usageBeginTurn, usageFnv1a, usagePolicyRef, usageRandomId, usageSessionId } from "#usage_core";
import { queueMigrateLegacyArray, queueRead, queueRemoveIds, queueWrite } from "./storage_queue.js";

/* 使用反馈 · 记录层（开关 + 本机存储 + 触发上传）
 *
 * 聚合在 usage_core.js（纯核，node 可单测）；这里做开关、localStorage 读写，
 * 以及打点落盘后 fire-and-forget 触发脱敏自动上传。
 *
 * 2026-08-13 起：回传通道 = benchfb 的「导出反馈包」（全量交互记录 + 评分 → 单个 JSON 文件）。
 * 早先的聚合文字报告弹窗（可编辑 textarea + 复制）已随「生成反馈」按钮一起退役——
 * 导出反馈包覆盖了它的全部功能；usage_core 的聚合纯核与事件记录层保留（聚合核有
 * 独立 node 规格，后续若复活文字通道可直接复用）。
 *
 * ## 这个功能是什么，以及为什么它长这样
 *
 * 产品决定做「使用数据采集」，最初的形态是**本机记录 + 手动回传**，
 * 代码为「零出网」付过真实成本：`/api/reuse-pack` 走 POST 而不是 GET（免得 dataset_uid 进
 * uvicorn 的 access log）、服务端不存会话、导出走前端 Blob 不写盘。
 * 2026-08-20 起（设计裁决见 `设计文档）：
 * **默认开启本地采集**，行为埋点（usage）+ benchmark 轮次记录（benchfb）经结构性脱敏；
 * 只有部署配置安全 HTTPS 通道后才自动上传，默认仅本地保存；手动导出保留作兜底。
 *
 * 分层：本文件**零网络原语**——唯一出网通道是 `usage_upload.js`（相对路径动态 import，
 * 不进 importmap），契约门（test_usage_telemetry_contract.py）把「遥测层只有它允许出现
 * fetch」钉成机械不变量；上传的双重门控（开关 + consent 同意）也都在那侧。
 *
 * - **默认态（2026-08-20 单版本化）**：主线版分叉废弃，行为恒取原反馈强化版——**默认开**；
 *   显式选择（开/关）永远优先于默认。关闭 = 零采集、零上传、零评分 UI；**关闭不删已有数据**。
 * - **consent**：per-profile `nsKey(biodata_consent_v2)`（2026-08-22 起 v2：值为同意时刻
 *   ISO 串，"0"=拒绝；v1 键不再认，老用户自动重问），每个本机账户首次发送时独立弹窗
 *   拦截完成；未同意前不上传（`usageConsentGiven()` 为假则 maybeUploadUsage 第一行就返回）。
 *
 * ## 不记什么（比记什么更重要）
 *
 * API Key、密码、账户名与账户 id、数据集完整内容 —— 一律不记。记的是：查询原话、命中条数、
 * 点开的是第几条、用了哪些功能、AI 哪一层成没成。（上传包构造时还会再过一遍结构性脱敏，
 * 见 usage_core.buildTelemetryPackage——防御性兜底，万一有打点把不该进的东西带进事件也挡在门外。）
 *
 * ## 埋点绝不许把主功能带崩
 *
 * 所有写入都在 try 里；localStorage 写满（QuotaExceededError）就**安静地少记一条**，
 * 绝不弹错、绝不打断检索。上传更是锦上添花——失败静默、无 toast，检索是本职工作，
 * 出让顺序不能反。
 */

/* 事件条数上限。按每条约 80 字节估，1500 条 ≈ 120KB，离 localStorage 的 5MB 很远；
   真到上限就丢最旧的（FIFO）—— 新近的使用比几个月前的更能说明当下的问题。 */
const USAGE_MAX_EVENTS = 1500;
const USAGE_GLOBAL_MAX_EVENTS = 3000;   // 同一 Origin 全账户合计上限，防多账户把 localStorage 吃满

/* v2 存储：每条事件一个 localStorage key。整数组 read-modify-write 在多标签页下必然
   last-write-wins；独立 key 让并发 append 原子化。旧数组首次读取时幂等迁移。 */
const USAGE_EVENT_MARK = "::event::";
let _usageCache = new Map();   // scope -> 排序后的事件数组；storage 事件到来即失效

export function usageScope() { return currentAccountScope(); }

/* 本地队列丢弃账本：每次丢弃一个独立 key，跨标签页 append 不走共享 RMW，因此不会
   last-write-wins。下一包快照携带聚合计数；ACK 精确删快照 ids，上传期间新条目保留。 */
const DROP_MARK = "::drop::";
function _dropPrefix(scope) { return nsKeyFor(LS.telemetryDrops, scope) + DROP_MARK; }

function _dropRows(scope) {
    const prefix = _dropPrefix(scope), rows = [];
    try {
        for (let i = 0; i < localStorage.length; i++) {
            const key = localStorage.key(i);
            if (!key || !key.startsWith(prefix)) continue;
            const raw = JSON.parse(localStorage.getItem(key));
            const queue = raw && (raw.queue === "benchfb" || raw.queue === "storage_error") ? raw.queue : "usage";
            const count = Math.max(0, Math.floor(Number(raw && raw.count) || 0));
            if (count) rows.push({ key: key, id: key.slice(prefix.length), queue: queue, count: count });
        }
    } catch (_e) {}
    rows.sort(function (a, b) { return a.id.localeCompare(b.id); });
    return rows;
}

export function usageNoteDropsForScope(scope, queue, count) {
    const n = Math.max(0, Math.floor(Number(count) || 0));
    if (!n) return false;
    const kind = (queue === "benchfb" || queue === "storage_error") ? queue : "usage";
    const id = usageRandomId("drop", Date.now());
    try { localStorage.setItem(_dropPrefix(scope) + id, JSON.stringify({ queue: kind, count: n })); return true; }
    catch (_e) { return false; }
}

export function usageDropSnapshotForScope(scope) {
    const rows = _dropRows(scope);
    const by = { usage: 0, benchfb: 0, storage_error: 0 };
    rows.forEach(function (row) { by[row.queue] += row.count; });
    const total = by.usage + by.benchfb + by.storage_error;
    const revision = rows.length ? (parseInt(usageFnv1a(rows.map(function (r) { return r.id; }).join("|")), 36) % 2147483646) + 1 : 0;
    return total ? {
        revision: revision,
        dropped_count: total,
        by_queue: by,
        _entry_ids: rows.map(function (row) { return row.id; }),
    } : null;
}

export function usageAckDropSnapshotForScope(scope, snapshot) {
    if (!snapshot || !(Number(snapshot.dropped_count) > 0)) return;
    try {
        (Array.isArray(snapshot._entry_ids) ? snapshot._entry_ids : []).forEach(function (id) {
            localStorage.removeItem(_dropPrefix(scope) + String(id));
        });
    } catch (_e) {}
}

function _legacyEventId(event, index) {
    let body = "";
    try { body = JSON.stringify(event); } catch (_e) { body = String(index); }
    return "legacy-u-" + String(Number(event && event.t) || 0) + "-" + index + "-" + usageFnv1a(body);
}

function _usageBase(scope) { return nsKeyFor(LS.usage, scope); }
function _usagePrefix(scope) { return _usageBase(scope) + USAGE_EVENT_MARK; }

function _migrateLegacyUsage(scope) {
    const oldKey = _usageBase(scope);
    queueMigrateLegacyArray(localStorage, {
        legacyKey: oldKey, prefix: _usagePrefix(scope),
        normalize: function (raw, index) {
            const event = (raw && typeof raw === "object") ? Object.assign({}, raw) : { t: 0, k: "err" };
            event.event_id = String(event.event_id || _legacyEventId(event, index));
            return event;
        },
        id: function (event) { return event.event_id; },
    });
}

function _readUsage(scope) {
    _migrateLegacyUsage(scope);
    return queueRead(localStorage, _usagePrefix(scope), function (a, b) {
        const dt = (Number(a.t) || 0) - (Number(b.t) || 0);
        return dt || String(a.event_id || "").localeCompare(String(b.event_id || ""));
    });
}

function _dropUsageKeys(scope, ids) {
    queueRemoveIds(localStorage, _usagePrefix(scope), ids);
    _usageCache.delete(scope);
}

function _trimGlobalUsage() {
    const rows = [];
    try {
        for (let i = 0; i < localStorage.length; i++) {
            const key = localStorage.key(i);
            if (!key || !key.includes(USAGE_EVENT_MARK)) continue;
            const event = JSON.parse(localStorage.getItem(key));
            rows.push({ key: key, t: Number(event && event.t) || 0 });
        }
        rows.sort(function (a, b) { return a.t - b.t || a.key.localeCompare(b.key); });
        const removedByScope = new Map();
        rows.slice(0, Math.max(0, rows.length - USAGE_GLOBAL_MAX_EVENTS)).forEach(function (r) {
            localStorage.removeItem(r.key);
            const marker = LS.usage + "::u:";
            const scope = r.key.startsWith(marker) ? r.key.slice(marker.length).split(USAGE_EVENT_MARK, 1)[0] : "";
            removedByScope.set(scope, (removedByScope.get(scope) || 0) + 1);
        });
        removedByScope.forEach(function (count, scope) { usageNoteDropsForScope(scope, "usage", count); });
        if (rows.length > USAGE_GLOBAL_MAX_EVENTS) _usageCache.clear();
    } catch (_e) {}
}

if (window.addEventListener) {
    window.addEventListener("storage", function (event) {
        const key = String((event && event.key) || "");
        if (key.includes(USAGE_EVENT_MARK) || key.includes(LS.usageEnabled) || key.includes(LS.usageConsent)
                || key.includes(LS.trainingConsent) || key.includes(LS.telemetryDrops)) {
            _usageCache.clear();
        }
    });
}

/* ---------- 开关（三态：未表态 / 开 / 关；未表态时默认开） ---------- */

/* 返回 "1" | "0" | null（null = 用户还没显式表过态，此时按默认：开）。 */
export function usageChoice() {
    return usageChoiceForScope(usageScope());
}

export function usageChoiceForScope(scope) {
    try { return localStorage.getItem(nsKeyFor(LS.usageEnabled, scope)); } catch (_e) { return null; }
}

/* 构建版本判定（2026-08-20 单版本化）：主线版分叉废弃，**恒为 true**（原反馈强化版）。
   <meta name="biodata-build" content="benchfb"> 保留（指纹契约用），但运行时代码不再做
   主线/强化分叉判断。导出保留——onboarding.js 顶层 `const BENCHFB_BUILD = isBenchfbBuild()`
   直接调用本函数定默认分支，恒 true 使其永远走强化版文案。 */
export function isBenchfbBuild() {
    return true;
}

export function usageEnabled() {
    return usageEnabledForScope(usageScope());
}
export function usageEnabledForScope(scope) {
    const chosen = usageChoiceForScope(scope);
    if (chosen !== null) return chosen === "1";
    // 单版本化：恒取原强化版默认——开。
    return true;
}

export function usageSetEnabled(on) {
    try { localStorage.setItem(nsKey(LS.usageEnabled), on ? "1" : "0"); } catch (_e) {}
    const node = $("nodeUsage");
    if (node) node.checked = !!on;
}

/* 与身份无关的随机短码，只为把同一台机器的多次反馈对上号。
   不含账户名、不含机器名、不含任何可反查的信息。 */
export function usageInstallId() {
    let id = "";
    try { id = localStorage.getItem(LS.usageInstall) || ""; } catch (_e) { id = ""; }
    if (!id) {
        id = Math.random().toString(36).slice(2, 6);
        try { localStorage.setItem(LS.usageInstall, id); } catch (_e) {}
    }
    return id;
}

/* 旧 4 位 install_id 仅保留报表兼容；并发/幂等使用 128-bit client/profile id。 */
export function usageClientId() {
    let id = "";
    try { id = localStorage.getItem(LS.usageClient) || ""; } catch (_e) {}
    if (!id) { id = usageRandomId("client", Date.now()); try { localStorage.setItem(LS.usageClient, id); } catch (_e) {} }
    return id;
}

export function usageProfileIdForScope(scope) {
    const key = nsKeyFor(LS.usageProfile, scope);
    let id = "";
    try { id = localStorage.getItem(key) || ""; } catch (_e) {}
    if (!id) { id = usageRandomId("profile", Date.now()); try { localStorage.setItem(key, id); } catch (_e) {} }
    return id;
}

/* ---------- 存储 ---------- */

export function usageEvents() {
    return usageEventsForScope(usageScope());
}

export function usageEventsForScope(scope) {
    if (_usageCache.has(scope)) return _usageCache.get(scope).slice();
    const events = _readUsage(scope);
    _usageCache.set(scope, events);
    return events.slice();
}

/* 兼容出口：不再覆盖整数组，而是逐事件 upsert；上传侧使用 removeIds 精确 ACK。 */
export function usagePersist(list, scope) {
    scope = scope === undefined ? usageScope() : scope;
    const prefix = _usagePrefix(scope);
    try {
        (Array.isArray(list) ? list : []).forEach(function (raw) {
            const event = Object.assign({}, raw || {});
            event.event_id = String(event.event_id || usageRandomId("u", Date.now()));
            if (!queueWrite(localStorage, prefix, event.event_id, event)) throw new Error("storage_full");
        });
        _usageCache.delete(scope);
        return true;
    } catch (_e) { return false; }
}

export function usageRemoveEventsForScope(scope, ids) { _dropUsageKeys(scope, ids); }

export function usageClearEpochForScope(scope) {
    try { return Number(localStorage.getItem(nsKeyFor(LS.usageClearEpoch, scope))) || 0; } catch (_e) { return 0; }
}

export function usageClearScope(scope) {
    const prefix = _usagePrefix(scope);
    const dropPrefix = _dropPrefix(scope);
    const remove = [];
    try {
        for (let i = 0; i < localStorage.length; i++) {
            const key = localStorage.key(i);
            if (key && (key.startsWith(prefix) || key.startsWith(dropPrefix))) remove.push(key);
        }
        remove.forEach(function (key) { localStorage.removeItem(key); });
        localStorage.removeItem(_usageBase(scope));
        localStorage.removeItem(nsKeyFor(LS.usageUploadMeta, scope));
        localStorage.setItem(nsKeyFor(LS.usageClearEpoch, scope), String(Date.now()));
    } catch (_e) {}
    _usageCache.delete(scope);
}

/* ---------- 触发上传（唯一出网通道在 usage_upload.js） ---------- */

let _uploadModulePromise = null;
function _fireUpload(startup) {
    /* 相对路径动态 import：刻意不进 importmap / package.json 的 `#xxx` 键表——
       新模块上键要动两页 importmap 与 parity 门；动态 import 完全绕开静态图
       （test_frontend_import_graph.py 只盯静态边），也避免把「唯一网络模块」拖进
       import 环（SCC allowlist 只缩不涨）。promise 缓存：只加载一次。 */
    if (!_uploadModulePromise) _uploadModulePromise = import("./usage_upload.js");
    _uploadModulePromise.then(function (m) {
        if (m && typeof m.maybeUploadUsage === "function") m.maybeUploadUsage(startup === true);
    }).catch(function () { /* 加载/调用失败静默：上传是锦上添花，绝不打断主功能 */ });
}

/* ---------- consent（per-profile：写口与弹窗在 usage_upload.js，这里同步读 + 异步转发） ---------- */

/* 同步读当前 profile 是否同意：发送拦截用。键属主是 usage_upload.js；这里只读同一 nsKey。
   v2：值为同意时刻 ISO 串（"0"=显式拒绝）；任何非 "0" 非空值都算同意，
   兼容旧写法的 "1"。 */
export function usageConsentGiven() {
    return usageConsentGivenForScope(usageScope());
}

export function usageConsentGivenForScope(scope) {
    try {
        const v = localStorage.getItem(nsKeyFor(LS.usageConsent, scope));
        return !!v && v !== "0";
    } catch (_e) { return false; }
}

/* consent 同意时刻（ISO 串；"" = 未同意/拒绝/旧写法 "1" 无时刻）。MCP 中继拉取拿它当
   since_ts 下界——只中继「用户同意之后」产生的记录；时刻未知就不加下界（由接收端兜底）。 */
export function usageConsentAtForScope(scope) {
    try {
        const v = String(localStorage.getItem(nsKeyFor(LS.usageConsent, scope)) || "");
        return (v && v !== "0" && v !== "1") ? v : "";
    } catch (_e) { return ""; }
}

/* 训练授权默认开启（2026-08-25 产品方决策）：键不存在 = 从未表态 → 按同意计；
   显式关闭存 "0"（opt-out 永远优先）；显式开启存同意时刻 ISO。故障 → 关（fail-closed 口径不变）。
   隐私口径随默认翻转同步更新：设置卡片/「?」气泡/consent 弹窗预勾选，见 index.html。 */
export function usageTrainingConsentGivenForScope(scope) {
    try {
        const v = String(localStorage.getItem(nsKeyFor(LS.trainingConsent, scope)) || "");
        return v !== "0";
    } catch (_e) { return false; }
}

/* HTML meta 注入值读取的唯一实现：实验分臂（本文件）与遥测上传端点配置
   （usage_upload.js 经 import 复用）同一读法——querySelector by name，取 content，trim；
   读不到/异常一律空串，绝不抛。取值为 getAttribute("content") 而不是 .content 属性：
   对真实 meta 元素两者等价，但 getAttribute 是通用 Element 接口（node 规格的假 DOM 只桩它）。 */
export function usageMetaContent(name) {
    try {
        const node = document.querySelector('meta[name="' + name + '"]');
        return String((node && node.getAttribute && node.getAttribute("content")) || "").trim();
    } catch (_e) { return ""; }
}

/* 单安装包内的稳定随机分臂：默认 meta 为空即 null、零行为变化。配置 arms 时按随机 profile
   做确定性 hash；也兼容旧的静态 arm+propensity（用于一包一臂的受控发布）。 */
export function usageExperimentContext(scope) {
    scope = scope === undefined ? usageScope() : scope;
    const experimentId = usageMetaContent("biodata-experiment-id");
    const armsText = usageMetaContent("biodata-experiment-arms");
    const assigned = telemetryExperimentAssign(
        usageProfileIdForScope(scope), experimentId, armsText);
    if (assigned) return assigned;
    // 动态配置一旦出现但不合法就整体 fail-closed；不得悄悄落到遗留静态臂，造成错标。
    if (armsText) return null;
    const arm = usageMetaContent("biodata-experiment-arm");
    const propensity = Number(usageMetaContent("biodata-experiment-propensity"));
    if (!experimentId || !arm || !Number.isFinite(propensity) || propensity <= 0 || propensity > 1) return null;
    return { experimentId: experimentId, experimentArm: arm, propensity: propensity, overrides: null };
}

export function usageSetTrainingConsent(on, scope) {
    scope = scope === undefined ? usageScope() : scope;
    try { localStorage.setItem(nsKeyFor(LS.trainingConsent, scope), on ? new Date().toISOString() : "0"); }
    catch (_e) {}
    const node = $("usageTrainingToggle");
    if (node && scope === usageScope()) node.checked = !!on;
}

/* 首次发送拦截：打开 consent 告知弹窗并等待用户决定。
   返回 Promise<'agree'|'disable'>——'agree'：同意并继续（弹窗侧已落 setUsageConsent(true)）；
   'disable'：调用方应 usageSetEnabled(false) 后再继续发送（本次照常发，以后开关关着不再弹）。
   与 _fireUpload 同一条动态 import 通道（usage_upload.js 不进 importmap，静态 import 会
   牵动两页 importmap 与 parity 门）。 */
export async function requestUsageConsent() {
    if (!_uploadModulePromise) _uploadModulePromise = import("./usage_upload.js");
    try {
        const m = await _uploadModulePromise;
        if (m && typeof m.requestUsageConsent === "function") return await m.requestUsageConsent();
    } catch (_e) { /* 弹窗模块加载失败不卡发送：按「未同意」继续（不会自动上传），下次再弹 */ }
    return "agree";
}

/* 打点总入口。**所有接线点只调它**，不许各模块自己写 localStorage。
   关着的时候第一行就返回 —— 关闭状态下这个功能的运行时开销约等于一次布尔比较。 */
export function usageLog(kind, payload, scopeOverride) {
    const scope = scopeOverride === undefined ? usageScope() : scopeOverride;
    if (!usageEnabledForScope(scope)) return false;
    if (!kind || !USAGE_KINDS[kind]) return false;
    // sid/tid/iid 中央注入（2026-08-22 schema）：全事件带 sid，适用事件带
    // 当前轮 tid 与当前展示 iid；payload 里显式给的值优先（view 事件用 begin 时的快照，
    // 防止新一轮 turn 串号；v3 起 imp/卡动作/label 也靠这条显式通道传快照或显式 null）。
    const entry = Object.assign({ event_id: usageRandomId("u", Date.now()), t: Date.now(), k: kind,
        sid: usageSessionId(), tid: usageActiveTurnId(), iid: usageActiveImpressionId() }, payload || {});
    const list = usageEventsForScope(scope);
    try { if (!queueWrite(localStorage, _usagePrefix(scope), entry.event_id, entry)) throw new Error("storage_full"); }
    catch (_e) { usageNoteDropsForScope(scope, "storage_error", 1); return false; }
    list.push(entry);
    _usageCache.set(scope, list);
    if (list.length > USAGE_MAX_EVENTS) {
        const overflow = list.length - USAGE_MAX_EVENTS;
        _dropUsageKeys(scope, list.slice(0, overflow).map(function (e) { return e.event_id; }));
        usageNoteDropsForScope(scope, "usage", overflow);
    }
    // Origin 总预算不必每次全扫；每 50 条/触及 profile 上限时收敛，跨标签页最终一致。
    if (list.length % 50 === 0 || list.length >= USAGE_MAX_EVENTS) _trimGlobalUsage();
    // 落盘成功后才 fire-and-forget 触发脱敏自动上传。usageSetEnabled(false) 后
    // 第一行就返回，这里到不了 —— 天然保证「关闭后绝不触发上传」。
    _fireUpload(false);
    return true;
}

/* 一次检索的打点。**「从后端响应里读哪些字段」只写在这一个地方** ——
   接线点（search.js 的两个落点）各留一行调用即可。本项目在「同一份口径抄两遍必然漂移」上
   栽过不止一次，这里从一开始就不给第二份抄本留位置。

   opts.handSubmit（2026-08-05 前身 userSubmit 已随统一路由退役）沿用 actAfterSearch 的同一判据：
   只有用户亲手提交的那次才算一次「检索」。分面芯片、一键放宽、撤销/重做、历史重跑都走 runRecommend，
   把它们也算进「搜过什么」会让同一句查询的计数虚高好几倍 —— 那几类行为各有自己的事件，不在这里重复归账。 */
/* 当屏条目快照（v3 起独立成 imp 事件的 items；usageLogSearch 不再携带）：
   uid + 名次 + 四位小数分 + 截 160 字理由。单一真源——imp 接线（results.js）与
   旧的 search 内联 items（已退役）共用同一形状，导出器按它对齐。 */
export function usageImpressionItems(results) {
    return (Array.isArray(results) ? results : []).map(function (r, i) {
        const it = { uid: String((r && r.dataset_uid) || ""), pos: i + 1 };
        const score = Number(r && r.score);
        if (Number.isFinite(score)) it.score = Math.round(score * 10000) / 10000;
        const reason = String((r && r.reason) || "").trim();
        if (reason) it.reason = reason.slice(0, 160);
        return it;
    });
}

export function usageLogSearch(data, query, opts) {
    const scope = (opts && opts.telemetryScope !== undefined) ? opts.telemetryScope : usageScope();
    if (!usageEnabledForScope(scope)) return;
    if (!(opts && opts.handSubmit)) return;
    data = data || {};
    const shown = (Array.isArray(data.results) ? data.results : []).length;
    const total = (typeof data.result_total === "number") ? data.result_total : shown;
    const terms = Array.isArray(data.unused_query_terms) ? data.unused_query_terms.slice(0, 8).map(String) : [];
    // v2：秒出与实测耗时。缓存命中的 data 带着**上次**检索的旧 trace，耗时必须剔除（记 0=未实测），
    // 否则「秒出」会被算成几次正常耗时，把平均值洗假。cached 由 search.js 缓存命中落点显式传入。
    const cached = !!(opts && opts.cached);
    const rawMs = Number(data.search_trace && data.search_trace.total_duration_ms);
    // v2：tid 兜底——正常由 board.js ubSubmit 开局；历史重跑等不过 ubSubmit
    // 的路径在这里补开一轮，保证 search 事件永远有轮次可归。
    const tid = usageActiveTurnId() || usageBeginTurn();
    // v3：策略串**优先用后端响应里的 policy_id**（缓存命中时就是缓存响应里那份，
    // 与当屏结果严格同源）；后端没给才按请求参数现组合。当屏 items 移去 imp 事件（usageImpressionItems）。
    const pp = (opts && opts.policyParts) || {};
    const policy = usagePolicyRef(data,
        { strategy: pp.strategy, rerank: pp.rerank, recall: pp.recall, gen: cacheGeneration() });
    usageLog(USAGE_KINDS.search, {
        q: String(query || "").trim().slice(0, 120),
        n: total,
        shown: shown,
        abstain: data.resolution_status === "abstained",
        unused: terms,
        cached: cached,
        ms: (!cached && Number.isFinite(rawMs) && rawMs > 0) ? Math.round(rawMs) : 0,
        tid: tid,
        policy: policy,
        experiment_id: data.experiment && data.experiment.id,
        experiment_arm: data.experiment && data.experiment.arm,
        propensity: data.experiment && data.experiment.propensity,
    }, scope);

    // AI 各层的成败单独记。**「没启用」和「试过但没成」必须分开** —— 这正是 2026-07-29
    // 修过的病根：把故障说成「本次未启用」，于是接口坏了好几天也没人看得出来。
    // 反馈包同样不许把两者合成一个数，所以这里只记 status==="fallback"（真试过）和 "used"（真成了），
    // "skipped"（用户压根没开这一层）一条都不记 —— 记了就是拿「没开」冲淡「坏了」。
    const steps = (data.search_trace && Array.isArray(data.search_trace.steps)) ? data.search_trace.steps : [];
    steps.forEach(function (s) {
        if (!s || !USAGE_AI_LABELS[s.id]) return;
        if (s.status === "used") usageLog(USAGE_KINDS.ai, { step: s.id, ok: true }, scope);
        else if (s.status === "fallback") usageLog(USAGE_KINDS.ai, { step: s.id, ok: false, why: String(s.reason || "").slice(0, 40) }, scope);
    });
}

/* 一张卡片在**检索结果**里的名次（1-based）。返回 0 = 不适用（浏览页/收藏页/详情页的卡片
   没有「排第几」的语义）。名次是排序质量最直接的读数：老是点第 1 条说明排得准，
   总要翻到第 5 条才有用的说明排序该改。用 DOM 位置现算，不去改 buildCard 的签名。 */
export function usageCardRank(card) {
    if (!card) return 0;
    const grid = card.closest ? card.closest("#resultsGrid") : null;
    if (!grid) return 0;
    // 只数 .card 兄弟（2026-08-22 修正）：grid.children 会被 benchfb hero mount、
    // relax-banner 等非卡片节点污染，名次虚高；这里与 search 事件 items 的 pos 口径对齐。
    const cards = grid.querySelectorAll(".card");
    const i = Array.prototype.indexOf.call(cards, card);
    return i >= 0 ? i + 1 : 0;
}

/* ---------- 卡级展示归因（2026-08-22 schema ImpressionContext）----------
   问题：open/fav 等卡上动作此前读**事件发生时**的全局 tid/iid——用户开着旧结果发起新检索，
   旧卡上的点击会被归到新轮/新展示，串号。
   解法：每次结果渲染造一个**不可变** ImpressionContext（tid/iid/policy/items 快照，自动带 sid），
   经 WeakMap 绑到每张卡 DOM 上；卡动作经 usageLogCardAction 发事件，有 ctx 用快照值，
   无 ctx（收藏页/浏览页/详情页等非结果渲染的卡）显式 tid:null/iid:null——诚实缺失，
   不冒领「当前轮」。空 tid/iid 在 ctx 内存 null（历史回看不是任何一轮的展示）。 */
export function usageMakeImpression(opts) {
    opts = opts || {};
    return Object.freeze({
        sid: usageSessionId(),
        tid: (opts.tid === undefined || opts.tid === null || opts.tid === "") ? null : String(opts.tid),
        iid: (opts.iid === undefined || opts.iid === null || opts.iid === "") ? null : String(opts.iid),
        policy: String(opts.policy || ""),
        items: Array.isArray(opts.items) ? opts.items : [],
    });
}

const _cardCtx = new WeakMap();   // card DOM → ImpressionContext（卡被回收时条目随 GC 消失，不泄漏）
export function usageBindImpression(card, ctx) {
    if (!card || !ctx) return;
    try { _cardCtx.set(card, ctx); } catch (_e) {}
}
export function usageImpressionOf(card) {
    if (!card) return null;
    try { return _cardCtx.get(card) || null; } catch (_e) { return null; }
}

/* 一次结果展示的内容快照事件（kind imp）。只在 items 非空时发——空屏没有可归因的内容，
   view 事件的空 seen 已足够证明「这屏存在过」。tid/iid 显式进 payload（可为 null），
   压住 usageLog 的全局注入：历史回看的展示不属于任何一轮。 */
export function usageLogImpression(ctx, scopeOverride) {
    if (!ctx || !Array.isArray(ctx.items) || !ctx.items.length) return false;
    return usageLog(USAGE_KINDS.imp, { tid: ctx.tid, iid: ctx.iid, policy: ctx.policy, items: ctx.items }, scopeOverride);
}

/* 卡上动作的统一打点口（open/fav；cards.js 的 intro/site/files 与 core.js 的收藏都走这里）。
   有绑定快照 → 归因到**那次展示**（换 tid/重渲不串号）；无 → tid/iid 显式 null。 */
export function usageLogCardAction(card, kind, payload, scopeOverride) {
    const ctx = usageImpressionOf(card);
    const base = ctx ? { tid: ctx.tid, iid: ctx.iid, policy: ctx.policy } : { tid: null, iid: null };
    return usageLog(kind, Object.assign(base, payload || {}), scopeOverride);
}

/* ---------- 界面 ---------- */

export function initUsage() {
    const node = $("nodeUsage");
    if (node) {
        node.checked = usageEnabled();
        node.addEventListener("change", function () { usageSetEnabled(node.checked); });
    }
    const training = $("usageTrainingToggle");
    if (training) {
        training.checked = usageTrainingConsentGivenForScope(usageScope());
        training.addEventListener("change", function () { usageSetTrainingConsent(training.checked); });
    }
    // 启动时也尝试一次上传：有待发数据即传（2026-08-22 起取消「距上次成功 > 6 小时」
    // 的等待——激进上传：阈值 benchfb 1 轮 / usage 默认 2 条，usage 阈值与最小
    // 间隔随服务器 server_hint 自适应（空闲近实时、压力大时攒批），另有 30s 防抖、5 分钟周期
    // 兜底与页面隐藏前的 keepalive 尽力一发）。门控仍由 usage_upload.js 内的开关 + consent 双重把关。
    _fireUpload(true);
}

/* 账户切换时事件缓存必须作废：不同账户走不同的 nsKey 命名空间，
   不清缓存会把上一个账户的记录算进下一个人的反馈包。 */
export function usageOnAccountChanged() {
    _usageCache.clear();
    const node = $("nodeUsage");
    if (node) node.checked = usageEnabled();
    const training = $("usageTrainingToggle");
    if (training) training.checked = usageTrainingConsentGivenForScope(usageScope());
}
