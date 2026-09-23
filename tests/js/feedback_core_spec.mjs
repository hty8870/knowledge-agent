"use strict";

/* ============================================================================
 * feedback_core_spec.mjs —— 意见反馈核心模块「真行为」规格（node 跑）
 * ----------------------------------------------------------------------------
 * 由 tests/test_usage_telemetry_contract.py 经 `node <this>` 驱动；断言失败 → 非零退出。
 * 覆盖：per-profile 队列（幂等/遮蔽/上限/状态流转/隔离）、buildDiagSnapshot（allowlist
 * 聚合 + 遥测关闭语义）、hasSendChannel（公钥空 → false）、WebCrypto 加解密往返
 * （本机生成开发者密钥对，加密 → 解密 → 逐字段断言；node 24 自带 globalThis.crypto）。
 * localStorage 用 MemoryStorage mock（同 telemetry_concurrency_spec 的做法）。
 * ========================================================================== */

class MemoryStorage {
    constructor() { this.map = new Map(); }
    get length() { return this.map.size; }
    key(i) { return Array.from(this.map.keys())[i] ?? null; }
    getItem(k) { return this.map.has(String(k)) ? this.map.get(String(k)) : null; }
    setItem(k, v) { this.map.set(String(k), String(v)); }
    removeItem(k) { this.map.delete(String(k)); }
    clear() { this.map.clear(); }
}
globalThis.localStorage = new MemoryStorage();

const core = await import(new URL("../../web/static/js/core/feedback_core.js", import.meta.url));
const fakeApiKey = "sk-" + "abcdefghijklmnopqrstuvwxyz0123";

let failed = 0;
function check(label, ok, detail) {
    if (ok) console.log("PASS", label);
    else { failed += 1; console.error("FAIL", label, detail || ""); }
}

/* ---------- 0. 常量与发送通道 ---------- */

/*内置生产公钥（P-256 raw 未压缩点 65 字节的
   base64，与 importKey("raw") 口径一致）。契约钉「合法且可导入」，防误改/误清空。 */
const prodPubRaw = (() => {
    const bin = atob(String(core.FEEDBACK_PUBKEY_B64));
    const u8 = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
    return u8;
})();
check("已填生产公钥（65B raw 未压缩点、0x04 前缀）",
    prodPubRaw.length === 65 && prodPubRaw[0] === 4);
const prodPubImportable = await crypto.subtle.importKey("raw", prodPubRaw,
    { name: "ECDH", namedCurve: "P-256" }, false, []).then(() => true, () => false);
check("生产公钥可被 WebCrypto 以 raw/ECDH/P-256 导入", prodPubImportable);
check("生产公钥就位 → hasSendChannel()=true", core.hasSendChannel() === true);
check("公钥空覆盖 → hasSendChannel(\"\")=false", core.hasSendChannel("") === false);

/* 生成测试开发者密钥对（P-256），导出未压缩点 base64——供加密与解密用例复用。 */
const devKeys = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
const devPubRaw = new Uint8Array(await crypto.subtle.exportKey("raw", devKeys.publicKey));
let devPubB64 = "";
{
    let bin = "";
    for (const b of devPubRaw) bin += String.fromCharCode(b);
    devPubB64 = btoa(bin);
}
check("配置公钥后 hasSendChannel(pub)=true", core.hasSendChannel(devPubB64) === true);

/* ---------- 1. 队列：入队/幂等/遮蔽/上限/状态流转 ---------- */

localStorage.clear();
core.feedbackEnqueue("alice", {
    feedback_id: "fb-1", authorized_at: "2026-08-22T06:00:00Z",
    text: ` 搜索「肺癌 10x」时结果很慢，我的 key 是 ${fakeApiKey} 请勿外泄 `,
    diag: null, with_diag: false,
});
const q1 = core.feedbackPendingForScope("alice");
check("入队返回队列含 1 条", q1.length === 1, q1.length);
check("正文 trim 后落队", q1[0].text.indexOf("搜索「肺癌 10x」时结果很慢") === 0, q1[0].text);
check("正文入队时过 API Key 遮蔽", q1[0].text.indexOf(fakeApiKey) < 0
    && q1[0].text.indexOf("[API Key]") >= 0, q1[0].text);
check("authorized_at 定格", q1[0].authorized_at === "2026-08-22T06:00:00Z");
check("初始 status=pending", q1[0].status === "pending");

core.feedbackEnqueue("alice", { feedback_id: "fb-1", text: "重试不应入重" });
check("同 feedback_id 幂等（不入重）", core.feedbackPendingForScope("alice").length === 1);

core.feedbackMarkSentForScope("alice", ["fb-1"]);
const q2 = core.feedbackPendingForScope("alice");
check("发送成功 → status=sent", q2[0].status === "sent" && typeof q2[0].sent_at === "number");

core.feedbackEnqueue("alice", { feedback_id: "fb-2", text: "第二条" });
core.feedbackRemoveForScope("alice", ["fb-2"]);
check("按 id 移除", core.feedbackPendingForScope("alice").length === 1
    && core.feedbackPendingForScope("alice")[0].feedback_id === "fb-1");

/* per-profile 隔离：bob 队列为空，清空 alice 不影响 bob */
core.feedbackClearScope("alice");
check("清空 alice 后其队列为空", core.feedbackPendingForScope("alice").length === 0);
core.feedbackEnqueue("bob", { feedback_id: "fb-b1", text: "bob 的意见" });
check("per-profile 隔离", core.feedbackPendingForScope("bob").length === 1);

/* 上限 FEEDBACK_MAX_PENDING=20：塞 22 条（先 3 条标 sent）→ 只留 20，优先丢最旧 sent */
localStorage.clear();
for (let i = 1; i <= 3; i++) core.feedbackEnqueue("cap", { feedback_id: "fb-s" + i, text: "sent-" + i });
core.feedbackMarkSentForScope("cap", ["fb-s1", "fb-s2", "fb-s3"]);
for (let i = 4; i <= 22; i++) core.feedbackEnqueue("cap", { feedback_id: "fb-p" + i, text: "pending-" + i });
const qc = core.feedbackPendingForScope("cap");
check("队列上限 20 条", qc.length === 20, qc.length);
check("超限优先丢最旧 sent（fb-s1/sb-s2 被丢，最新 sent 保留）",
    qc.every(function (r) { return r.feedback_id !== "fb-s1" && r.feedback_id !== "fb-s2"; })
    && qc.some(function (r) { return r.feedback_id === "fb-s3" && r.status === "sent"; }),
    qc.map(function (r) { return r.feedback_id; }).join(","));
check("全部 pending 时才丢最旧 pending", core.feedbackPendingForScope("cap").length === 20);

/* 超长正文截断 + onTruncated 回调 */
{
    let truncated = null;
    core.feedbackEnqueue("cap", { feedback_id: "fb-long", text: "x".repeat(core.FEEDBACK_MAX_TEXT + 100) },
        { onTruncated: function (id) { truncated = id; } });
    const row = core.feedbackPendingForScope("cap").find(function (r) { return r.feedback_id === "fb-long"; });
    check("超长正文截断到上限", row.text.length === core.FEEDBACK_MAX_TEXT, row.text.length);
    check("截断有回调留痕", truncated === "fb-long");
}

/* ---------- 2. buildDiagSnapshot：allowlist 聚合 + 遥测关闭语义 ---------- */

{
    const off = core.buildDiagSnapshot(null, { version: "v1", platform: "win" });
    check("events=null → available:false（遥测关闭「无可用统计」语义）", off.available === false);
    const notArray = core.buildDiagSnapshot("nope", {});
    check("非数组 → available:false", notArray.available === false);
}
{
    const snap = core.buildDiagSnapshot([
        { k: "search", q: "10x" }, { k: "search", q: "hca" }, { k: "open", r: 1 },
        { k: "fav" }, { k: "err" }, { k: "ai", step: "llm_rerank", ok: false },
        { k: "ai", step: "llm_polish", ok: true },
        { k: "unknown_kind" }, { k: 42 },
    ], { version: "20260822-ad1", platform: "windows" });
    check("available:true", snap.available === true);
    check("版本/平台透传", snap.version === "20260822-ad1" && snap.platform === "windows");
    check("错误计数 = err 事件 + ai 失败", snap.errors === 2, snap.errors);
    check("功能计数 allowlist 聚合", snap.features.search === 2 && snap.features.open === 1
        && snap.features.fav === 1, JSON.stringify(snap.features));
    check("ai 拆分 ok/fail", snap.features.ai_ok === 1 && !("ai" in snap.features), JSON.stringify(snap.features));
    check("非 allowlist kind 不计数", !("unknown_kind" in snap.features));
}

/* ---------- 3. WebCrypto 加解密往返（真实密钥，协议与接收端逐字段同源） ---------- */

{
    const plain = {
        feedback_id: "fb-crypto-1",
        authorized_at: "2026-08-22T07:00:00Z",
        text: `加密往返：结果页刷新后收藏丢失，${fakeApiKey} 别外传`,
        diag: { available: true, errors: 1, features: { search: 3 } },
    };
    const record = await core.feedbackEncrypt(plain, "profile-test-0001", devPubB64);
    check("密文载荷字段齐全", !!record.ephemeral_pubkey && !!record.nonce && !!record.ciphertext
        && record.feedback_id === "fb-crypto-1" && record.identity === "profile-test-0001");
    check("with_diag 透传", record.with_diag === false);
    const b64re = /^[A-Za-z0-9+/=]+$/;
    check("base64 字段形状", b64re.test(record.ephemeral_pubkey) && b64re.test(record.nonce)
        && b64re.test(record.ciphertext));

    // 解密侧：开发者私钥 ECDH + 临时公钥 → HKDF → AES-GCM（与接收端 app.py 同一派生流程）
    function b64decode(text) {
        const bin = atob(text);
        const out = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
        return out;
    }
    const epPubKey = await crypto.subtle.importKey("raw", b64decode(record.ephemeral_pubkey),
        { name: "ECDH", namedCurve: "P-256" }, false, []);
    const shared = await crypto.subtle.deriveBits({ name: "ECDH", public: epPubKey }, devKeys.privateKey, 256);
    const hkdfKey = await crypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveKey"]);
    const aesKey = await crypto.subtle.deriveKey(
        { name: "HKDF", hash: "SHA-256", salt: new TextEncoder().encode("biodata-feedback-v1"),
          info: new TextEncoder().encode("biodata-feedback/1") },
        hkdfKey, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
    const decrypted = new TextDecoder().decode(await crypto.subtle.decrypt(
        { name: "AES-GCM", iv: b64decode(record.nonce) }, aesKey, b64decode(record.ciphertext)));
    const got = JSON.parse(decrypted);
    check("解密后明文逐字段一致", got.feedback_id === plain.feedback_id
        && got.authorized_at === plain.authorized_at && got.text === plain.text
        && JSON.stringify(got.diag) === JSON.stringify(plain.diag), decrypted);
    check("密文不泄漏明文（正文与 API Key 都不出现）",
        record.ciphertext.indexOf("收藏") < 0 && record.ciphertext.indexOf(fakeApiKey) < 0);
}

console.log(failed ? "\nFEEDBACK_CORE_SPEC_FAIL" : "\nFEEDBACK_CORE_SPEC_OK");
process.exit(failed ? 1 : 0);
