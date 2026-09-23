"use strict";
/* 意见反馈 · 核心模块（2026-08-22，后端/核心层；对话框侧见 feedback_dialog_core）
 *
 * 纯逻辑、零 DOM、零网络、零墙钟（id/时间由调用方注入）：只做
 * - 开发者公钥配置点（FEEDBACK_PUBKEY_B64）：2026-08-22 已填生产公钥；
 * - per-profile `feedback_pending` 队列（不可变 feedback_id/授权时间/正文/诊断快照）；
 * - WebCrypto ECDH(P-256)+HKDF-SHA256+AES-256-GCM 加密（明文 → 密文载荷）；
 * - `buildDiagSnapshot()` 组装诊断信息（版本/平台/错误计数/功能计数 allowlist 聚合）；
 * - `hasSendChannel()`：公钥为空或 WebCrypto 不可用 → false（UI 走「复制到剪贴板」兜底）。
 *
 * 分层：存储（localStorage）在本模块（与 usage_core 的「零存储纯核」不同——队列是
 * 意见记录的不可变账本，读写在核心层集中，node 规格用 MemoryStorage mock）；
 * 唯一出网通道在 usage_upload.js（sendFeedback，见该文件）；正文遮蔽复用
 * usage_core.telemetryMaskString（含 API Key 形态——加密前第一层，
 * 接收端解密后还有第二层，双层同规则）。
 *
 * 加密协议（与接收端 app.py 逐字段同源，改动必须两端同步）：
 * 客户端生成临时 ECDH(P-256) 密钥对 → 与开发者公钥 deriveBits 得共享密钥 →
 * HKDF-SHA256(salt="biodata-feedback-v1", info="biodata-feedback/1") 派生 32 字节
 * AES-256-GCM 密钥 → 12 字节随机 nonce 加密明文 JSON。载荷字段：
 * {feedback_id, identity, ephemeral_pubkey(base64 65B), nonce(base64 12B),
 *  ciphertext(base64), with_diag}。
 */
import { telemetryMaskString } from "./usage_core.js";

/* ============================================================================
 * FEEDBACK_PUBKEY_B64 —— 开发者公钥配置点（2026-08-22 已填生产公钥）
 * ----------------------------------------------------------------------------
 * P-256 公钥，未压缩点（0x04||X||Y，65 字节）的 base64；与接收端环境变量
 * FEEDBACK_DECRYPT_KEY 里的私钥配对（私钥只在服务器，绝不入库）。若清空 =
 * 未配置：hasSendChannel() 返回 false，UI 只给「复制到剪贴板」兜底，
 * **绝不产生任何出网请求**。
 * ========================================================================== */
export const FEEDBACK_PUBKEY_B64 = "BFuuKa3V2c6pPHuB3ZdgVbdAFaomyOUHr6XXAX7d+01F0/yzjv43Z+c7g8sAHcWe/bWQiQXwqOJOG0SsWkDD/F0=";

/* 协议参数（与接收端 app.py FEEDBACK_HKDF_SALT / FEEDBACK_HKDF_INFO 同源） */
const _HKDF_SALT = new TextEncoder().encode("biodata-feedback-v1");
const _HKDF_INFO = new TextEncoder().encode("biodata-feedback/1");

/* per-profile 队列上限（与接收端 MAX_FEEDBACK_RECORDS=20 同源：一包装得下整队列）；
   正文上限 2000 字（与 artifact_context 同量级，防 localStorage 与密文体积失控）。 */
export const FEEDBACK_MAX_PENDING = 20;
export const FEEDBACK_MAX_TEXT = 2000;

/* localStorage 键（与 core.js 的 LS.feedbackPending 同值同源；本模块刻意零 # import，
   常量在此单点定义，core.js 只读同一字符串，避免把核心模块拖进 import 环）。 */
export const FEEDBACK_PENDING_KEY = "biodata_feedback_pending_v1";

/* 诊断快照里允许聚合并展示的功能 kind（与 usage_core.js USAGE_KINDS 同值同源；
   零 # import 的单点副本，键对不上只是不计数，不报错）。 */
export const FEEDBACK_DIAG_KINDS = ["search", "open", "dl", "facet", "relax", "conv", "undo", "fav",
    "ai", "err", "view", "imp", "label"];

function _b64Encode(bytes) {
    let bin = "";
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(bin);
}

function _b64Decode(text) {
    const bin = atob(String(text));
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
}

/* feedback_id 生成的唯一实现：crypto.randomUUID 优先，老环境退时间戳+随机串。
   对话框侧（feedback_dialog_core re-export）与入队兜底（feedbackEnqueue）同用本函数。 */
export function feedbackNewId() {
    try {
        if (typeof crypto !== "undefined" && crypto.randomUUID) return "fb-" + crypto.randomUUID();
    } catch (_e) {}
    return "fb-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12);
}

/* ---------- 队列（per-profile，localStorage 数组；键按 nsKeyFor 命名空间隔离） ---------- */

function _readPending(scope) {
    const key = scope ? FEEDBACK_PENDING_KEY + "::u:" + String(scope) : FEEDBACK_PENDING_KEY;
    try {
        const v = JSON.parse(localStorage.getItem(key));
        return Array.isArray(v) ? v : [];
    } catch (_e) { return []; }
}

function _writePending(scope, rows) {
    const key = scope ? FEEDBACK_PENDING_KEY + "::u:" + String(scope) : FEEDBACK_PENDING_KEY;
    try { localStorage.setItem(key, JSON.stringify(rows)); } catch (_e) { /* 写满静默：宁可少记，不打断主功能 */ }
}

/* 读队列快照（不可变：返回副本，调用方修改不落库）。 */
export function feedbackPendingForScope(scope) {
    return _readPending(scope).map(function (r) { return Object.assign({}, r); });
}

/* 入队一条**不可变**意见记录：feedback_id/授权时间/正文（遮蔽后）/诊断快照在入队时
   定格；正文超长截断到 FEEDBACK_MAX_TEXT（不静默——调用方传 opts.overflow 回调可见）。
   返回入队后的队列长度；同一 feedback_id 已存在 → 不动（幂等，重试不入重）。
   status："pending"（待发送）→ 发送成功由 usage_upload 置 "sent"（记录保留供导出反馈包）。 */
export function feedbackEnqueue(scope, entry, opts) {
    opts = opts || {};
    const rows = _readPending(scope);
    const fid = String((entry && entry.feedback_id) || feedbackNewId());
    if (rows.some(function (r) { return String(r.feedback_id) === fid; })) return rows.length;
    let text = String((entry && entry.text) || "").trim();
    if (text.length > FEEDBACK_MAX_TEXT) {
        text = text.slice(0, FEEDBACK_MAX_TEXT);
        if (typeof opts.onTruncated === "function") opts.onTruncated(fid);
    }
    rows.push({
        feedback_id: fid,
        authorized_at: (entry && entry.authorized_at) || new Date().toISOString(),
        text: telemetryMaskString(text),           // 加密前第一层遮蔽（API Key/手机号/证件号/邮箱）
        diag: (entry && entry.diag !== undefined) ? entry.diag : null,
        with_diag: !!(entry && entry.with_diag),
        status: "pending",
        sent_at: null,
        created_at: Date.now(),
    });
    // 上限：优先丢最旧已发送记录（导出账本保留在文件里，队列只留近期）；全 pending 时
    // 丢最旧 pending（记录在案，与 usage FIFO 同哲学——新近的意见价值最高）。
    while (rows.length > FEEDBACK_MAX_PENDING) {
        let idx = -1;
        for (let i = 0; i < rows.length; i++) {
            if (rows[i].status === "sent") { idx = i; break; }
        }
        if (idx < 0) idx = 0;
        rows.splice(idx, 1);
    }
    _writePending(scope, rows);
    return rows.length;
}

/* 发送成功 → 置 sent（保留记录供「导出反馈包」；同一 feedback_id 幂等）。 */
export function feedbackMarkSentForScope(scope, ids) {
    const idSet = {};
    (ids || []).forEach(function (id) { idSet[String(id)] = true; });
    const rows = _readPending(scope);
    let changed = false;
    rows.forEach(function (r) {
        if (idSet[String(r.feedback_id)] && r.status !== "sent") {
            r.status = "sent";
            r.sent_at = Date.now();
            changed = true;
        }
    });
    if (changed) _writePending(scope, rows);
}

/* 按 feedback_id 移除（清空/手动清理用）。 */
export function feedbackRemoveForScope(scope, ids) {
    const idSet = {};
    (ids || []).forEach(function (id) { idSet[String(id)] = true; });
    const next = _readPending(scope).filter(function (r) { return !idSet[String(r.feedback_id)]; });
    if (next.length !== _readPending(scope).length) _writePending(scope, next);
    return next.length;
}

/* 清空某 profile 整队列（账户切换/清空生命周期；sent 账本一并清——导出反馈包是快照，不影响）。 */
export function feedbackClearScope(scope) {
    _writePending(scope, []);
}

/* ---------- 发送通道能力 ---------- */

/* 公钥为空或 WebCrypto 不可用 → false。UI 据此走「复制到剪贴板」兜底；
   sendFeedback（usage_upload.js）也会先问它，false 时绝不组包出网。
   pubkeyB64 参数仅供测试/未来可配置覆盖（生产默认取 FEEDBACK_PUBKEY_B64 常量）。 */
export function hasSendChannel(pubkeyB64) {
    const pub = (pubkeyB64 === undefined) ? FEEDBACK_PUBKEY_B64 : pubkeyB64;
    if (!pub) return false;
    try {
        const c = typeof crypto !== "undefined" ? crypto : null;
        return !!(c && c.subtle && typeof c.subtle.deriveBits === "function"
            && typeof c.subtle.importKey === "function");
    } catch (_e) { return false; }
}

/* ---------- 加密 ---------- */

/* 加密一条意见 → 密文载荷（含 feedback_id/identity 元数据；identity 由调用方按
   profile/install 标识语义传入，与接收端幂等键口径一致）。任何一步失败抛错，
   调用方（sendFeedback）保留记录待重试——密文每次重试重新生成（新 ephemeral/nonce）。
   pubkeyB64 缺省取 FEEDBACK_PUBKEY_B64 常量（生产路径）；测试传入自备公钥。 */
export async function feedbackEncrypt(entry, identity, pubkeyB64) {
    const pub = (pubkeyB64 === undefined) ? FEEDBACK_PUBKEY_B64 : pubkeyB64;
    if (!hasSendChannel(pub)) throw new Error("feedback send channel unavailable");
    const pubRaw = _b64Decode(pub);
    const pubKey = await crypto.subtle.importKey("raw", pubRaw, { name: "ECDH", namedCurve: "P-256" }, false, []);
    const ephemeral = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
    const sharedBits = await crypto.subtle.deriveBits({ name: "ECDH", public: pubKey }, ephemeral.privateKey, 256);
    const hkdfKey = await crypto.subtle.importKey("raw", sharedBits, "HKDF", false, ["deriveKey"]);
    const aesKey = await crypto.subtle.deriveKey(
        { name: "HKDF", hash: "SHA-256", salt: _HKDF_SALT, info: _HKDF_INFO },
        hkdfKey, { name: "AES-GCM", length: 256 }, false, ["encrypt"]);
    const nonce = crypto.getRandomValues(new Uint8Array(12));
    const plaintext = new TextEncoder().encode(JSON.stringify({
        feedback_id: String(entry.feedback_id || ""),
        authorized_at: String(entry.authorized_at || ""),
        text: String(entry.text || ""),
        diag: entry.diag !== undefined ? entry.diag : null,
    }));
    const ciphertext = new Uint8Array(
        await crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce }, aesKey, plaintext));
    const epPubRaw = new Uint8Array(await crypto.subtle.exportKey("raw", ephemeral.publicKey));
    return {
        feedback_id: String(entry.feedback_id || ""),
        identity: String(identity || ""),
        ephemeral_pubkey: _b64Encode(epPubRaw),
        nonce: _b64Encode(nonce),
        ciphertext: _b64Encode(ciphertext),
        with_diag: !!(entry && entry.with_diag),
    };
}

/* ---------- 诊断快照 ---------- */

/* 组装诊断信息（设计 §8：版本/平台/最近错误计数/功能计数 allowlist 聚合值）。
   events：usage 事件数组；**null 或非数组 = 遥测关闭/无可用统计** → 返回
   {available:false}，UI 显示「无可用统计」，**不得为诊断重启采集**（隐私口径）。
   统计只做 allowlist 聚合：错误计数 = err 事件 + ai 失败（ok:false）；功能计数 =
   FEEDBACK_DIAG_KINDS 每个 kind 的出现次数（ai 拆成 ai_ok/ai_fail，其余原样）。
   纯函数：不读存储、不碰墙钟（now 可选，默认不打时间戳）。 */
export function buildDiagSnapshot(events, opts) {
    opts = opts || {};
    if (!Array.isArray(events)) {
        return { available: false };
    }
    const features = {};
    let errors = 0;
    events.forEach(function (ev) {
        if (!ev || typeof ev !== "object") return;
        const k = String(ev.k || "");
        if (FEEDBACK_DIAG_KINDS.indexOf(k) < 0) return;
        if (k === "err") { errors += 1; return; }
        if (k === "ai") {
            if (ev.ok === false) { errors += 1; }
            features.ai_ok = (features.ai_ok || 0) + (ev.ok === false ? 0 : 1);
            return;
        }
        features[k] = (features[k] || 0) + 1;
    });
    return {
        available: true,
        version: String(opts.version || ""),
        platform: String(opts.platform || ""),
        errors: errors,
        features: features,
    };
}
