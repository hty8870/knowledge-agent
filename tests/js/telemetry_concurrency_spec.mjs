/* 遥测真实模块并发规格：假 DOM/localStorage/fetch，零外网。 */
class MemoryStorage {
    constructor() { this.map = new Map(); }
    get length() { return this.map.size; }
    key(i) { return Array.from(this.map.keys())[i] ?? null; }
    getItem(k) { return this.map.has(String(k)) ? this.map.get(String(k)) : null; }
    setItem(k, v) { this.map.set(String(k), String(v)); }
    removeItem(k) { this.map.delete(String(k)); }
    clear() { this.map.clear(); }
}

const storageListeners = [];
globalThis.localStorage = new MemoryStorage();
globalThis.window = {
    gsap: undefined, ScrollTrigger: undefined,
    crypto: globalThis.crypto,
    matchMedia: () => ({ matches: false }),
    addEventListener: (kind, fn) => { if (kind === "storage") storageListeners.push(fn); },
};
globalThis.document = {
    getElementById: () => null,
    querySelector: (selector) => {
        if (selector.includes("biodata-telemetry-endpoint")) return { getAttribute: () => "https://telemetry.example/v1/ingest" };
        if (selector.includes("biodata-telemetry-token")) return { getAttribute: () => "test-client-token" };
        return { src: "http://127.0.0.1/static/js/core/boot.js?v=test" };
    },
    querySelectorAll: () => [],
    addEventListener: () => {},
    body: { classList: { add() {}, remove() {} }, contains: () => false },
};
Object.defineProperty(globalThis, "navigator", {
    value: { userAgent: "node-spec", language: "zh-CN" }, configurable: true,
});

const core = await import(new URL("../../web/static/js/core/core.js", import.meta.url));
const usage = await import(new URL("../../web/static/js/core/usage_log.js", import.meta.url));
const bench = await import(new URL("../../web/static/js/core/benchfb.js", import.meta.url));
const upload = await import(new URL("../../web/static/js/core/usage_upload.js", import.meta.url));

let failed = 0;
function check(label, ok, detail) {
    if (ok) console.log("PASS", label);
    else { failed += 1; console.error("FAIL", label, detail || ""); }
}
function ns(base, scope) { return core.nsKeyFor(base, scope); }
function events(n, prefix) { return Array.from({ length: n }, (_, i) => ({ t: i + 1, k: "search", q: prefix + i })); }
function setup(scope, n) {
    core.setCurrentUser({ id: scope });
    localStorage.setItem(ns(core.LS.usageEnabled, scope), "1");
    //  consent v2：值是同意时刻 ISO 串（"0"=拒绝；旧写法 "1" 仍兼容读，但测试钉新写法）
    localStorage.setItem(ns(core.LS.usageConsent, scope), new Date().toISOString());
    localStorage.setItem(ns(core.LS.usage, scope), JSON.stringify(events(n, scope)));
    usage.usageOnAccountChanged(); bench.benchfbOnAccountChanged();
}

let pending = [];
globalThis.fetch = (url, opts) => {
    const u = String(url);
    //  MCP 中继默认按「接收端未配套」立即 404——否则中继 GET 会进 pending 数组，
    // 把 waitCount 与请求计数断言全搞乱（中继是搭车，不是被测主通道）。中继用例在下方自带 mock。
    if (u.indexOf("/api/telemetry/mcp-calls") >= 0) {
        return Promise.resolve({ ok: false, status: 404, headers: { get: () => null }, json: async () => ({}) });
    }
    return new Promise((resolve, reject) => {
        const row = { resolve, reject, body: String(opts && opts.body || ""), url: u };
        if (opts && opts.signal) opts.signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
        pending.push(row);
    });
};
async function waitCount(n) {
    for (let i = 0; i < 100 && pending.length < n; i++) await new Promise((r) => setTimeout(r, 0));
    if (pending.length < n) throw new Error(`expected ${n} requests, got ${pending.length}`);
}
function ok(row) {
    const packet = JSON.parse(row.body);
    row.resolve({ ok: true, headers: { get: () => null }, json: async () => ({ ok: true, packet_id: packet.packet_id }) });
}

// 同标签页 single-flight。
localStorage.clear(); setup("one", 50); pending = [];
const oneA = upload.maybeUploadUsage(false), oneB = upload.maybeUploadUsage(false);
await waitCount(1);
check("overlap uses one request", pending.length === 1, pending.length);
ok(pending[0]); await Promise.all([oneA, oneB]);
check("acked events removed", usage.usageEventsForScope("one").length === 0);

// 账户切换：ACK 仍按开工 scope 精确删除。
localStorage.clear(); setup("alice", 50); pending = [];
const switched = upload.maybeUploadUsage(false); await waitCount(1);
setup("bob", 3);
ok(pending[0]); await switched;
check("alice acked in alice scope", usage.usageEventsForScope("alice").length === 0);
check("bob queue preserved", usage.usageEventsForScope("bob").length === 3, usage.usageEventsForScope("bob").length);

// consent/profile 独立。
check("consent is per profile", usage.usageConsentGivenForScope("alice") === true && usage.usageConsentGivenForScope("charlie") === false);

// 合同 v2：队列丢弃账本按 profile 隔离，旧快照 ACK 只扣已发计数，新丢弃保留。
usage.usageNoteDropsForScope("alice", "usage", 2);
const dropSnap = usage.usageDropSnapshotForScope("alice");
usage.usageNoteDropsForScope("alice", "benchfb", 3);
usage.usageAckDropSnapshotForScope("alice", dropSnap);
const dropLeft = usage.usageDropSnapshotForScope("alice");
check("drop ACK preserves concurrent increments", dropSnap.dropped_count === 2
    && dropLeft.dropped_count === 3 && dropLeft.by_queue.benchfb === 3, JSON.stringify(dropLeft));
check("drop ledger is per profile", usage.usageDropSnapshotForScope("charlie") === null);
core.setCurrentUser({ id: "alice" }); usage.usageOnAccountChanged(); bench.benchfbOnAccountChanged();
localStorage.removeItem(ns(core.LS.usageUploadMeta, "alice")); pending = [];
const dropOnlyDone = upload.maybeUploadUsage(false, { force: true }); await waitCount(1);
const dropOnlyPacket = JSON.parse(pending[0].body);
check("drop-only snapshot is uploaded", dropOnlyPacket.drop_report.dropped_count === 3
    && dropOnlyPacket.usage_events.length === 0 && dropOnlyPacket.benchfb_records.length === 0,
JSON.stringify(dropOnlyPacket.drop_report));
ok(pending[0]); await dropOnlyDone;
check("drop-only snapshot is acked after 200", usage.usageDropSnapshotForScope("alice") === null);
check("client id is long-lived strong id", usage.usageClientId().length >= 20, usage.usageClientId());

// ping single-flight + 稳定 packet id。
localStorage.clear(); setup("ping", 0); pending = [];
const pingA = upload.sendActivationPing("ping"), pingB = upload.sendActivationPing("ping");
await waitCount(1);
check("ping overlap uses one request", pending.length === 1, pending.length);
const pingPacket = JSON.parse(pending[0].body);
check("ping carries ids", !!pingPacket.packet_id && !!pingPacket.client_id && !!pingPacket.profile_id);
ok(pending[0]); await Promise.all([pingA, pingB]);

// poison record 被标 manual-only，后续小记录照常上传。
localStorage.clear(); setup("poison", 0);
localStorage.setItem(ns(core.LS.benchfb, "poison"), JSON.stringify([
    { id: "too-large", t: 1, kind: "search", blob: "x".repeat(1_950_000) },
    { id: "small-2", t: 2, kind: "search" },
    { id: "small-3", t: 3, kind: "search" },
]));
bench.benchfbOnAccountChanged(); pending = [];
const poison = upload.maybeUploadUsage(false); await waitCount(1);
const poisonPacket = JSON.parse(pending[0].body);
check("poison does not starve later records", poisonPacket.benchfb_records.length === 2, poisonPacket.benchfb_records.length);
ok(pending[0]); await poison;
const poisonLeft = bench.benchfbRecordsForScope("poison");
check("poison retained manual-only", poisonLeft.length === 1 && poisonLeft[0].telemetry_oversize === true, JSON.stringify(poisonLeft));

// 清空同时覆盖 usage + benchfb + meta，且账户隔离。
localStorage.clear(); setup("clear", 2);
localStorage.setItem(ns(core.LS.benchfb, "clear"), JSON.stringify([{ id: "b1", t: 1, kind: "search" }]));
bench.benchfbOnAccountChanged();
usage.usageClearScope("clear"); bench.benchfbClearScope("clear");
check("clear removes usage and bench", usage.usageEventsForScope("clear").length === 0 && bench.benchfbRecordsForScope("clear").length === 0);

// Origin 级总预算：多账户合计也必须有界。
localStorage.clear();
for (let i = 0; i < 3100; i++) {
    const scope = "s" + (i % 3);
    localStorage.setItem(ns(core.LS.usage, scope) + "::event::e" + i,
        JSON.stringify({ event_id: "e" + i, t: i, k: "open" }));
}
core.setCurrentUser({ id: "s0" });
localStorage.setItem(ns(core.LS.usageEnabled, "s0"), "1");
for (let i = 0; i < 16; i++) usage.usageLog("open", {}, "s0");   // s0 1034→1050，触发每 50 条收敛
const usageKeyCount = Array.from(localStorage.map.keys()).filter((k) => k.includes("::event::")).length;
check("usage origin budget is bounded", usageKeyCount <= 3000, usageKeyCount);

localStorage.clear(); setup("b0", 0);
for (let i = 0; i < 130; i++) {
    const scope = "b" + (i % 3);
    localStorage.setItem(ns(core.LS.benchfb, scope) + "::record::r" + i,
        JSON.stringify({ id: "r" + i, t: i, kind: "none" }));
}
bench.benchfbTurnBegin("budget", { scope: "b0" }); bench.benchfbTurnEcho();
const benchKeyCount = Array.from(localStorage.map.keys()).filter((k) => k.includes("::record::")).length;
check("bench origin budget is bounded", benchKeyCount <= 120, benchKeyCount);

// 明文公网 HTTP 显式白名单（合并裁决）：未登记主机 fail-closed，登记后才放行。
localStorage.clear(); setup("insec", 50); pending = [];
const savedQS = globalThis.document.querySelector;
let metaMap = {};
globalThis.document.querySelector = (selector) => {
    const m = selector.match(/meta\[name="([^"]+)"\]/);
    if (m) return { getAttribute: () => metaMap[m[1]] ?? "" };
    return { src: "http://127.0.0.1/static/js/core/boot.js?v=test" };
};
metaMap = {
    "biodata-telemetry-endpoint": "http://telemetry.example/v1/ingest",
    "biodata-telemetry-token": "test-client-token",
};
check("plain http without allowlist is rejected", upload.telemetryUploadConfigured() === false);
metaMap["biodata-telemetry-allow-insecure"] = "telemetry.example";
check("plain http with allowlisted host is accepted", upload.telemetryUploadConfigured() === true);
metaMap["biodata-telemetry-allow-insecure"] = "other.example";
check("plain http with other host still rejected", upload.telemetryUploadConfigured() === false);
metaMap["biodata-telemetry-allow-insecure"] = "1";
check("allowlist is host-based, not a boolean flag", upload.telemetryUploadConfigured() === false);
globalThis.document.querySelector = savedQS;

// 单条 benchfb 即触发上传（阈值 3→1）+ MCP 中继 attach / ack / offset 推进。
localStorage.clear(); setup("relay", 0);
localStorage.setItem(ns(core.LS.benchfb, "relay"), JSON.stringify([{ id: "r-only", t: 1, kind: "search" }]));
bench.benchfbOnAccountChanged(); pending = [];
const relayCalls = [];
const savedFetch = globalThis.fetch;
globalThis.fetch = (url, opts) => {
    const u = String(url);
    if (u.includes("/api/telemetry/mcp-calls/ack")) {
        relayCalls.push({ kind: "ack", body: String(opts && opts.body || "") });
        return Promise.resolve({ ok: true, status: 200, headers: { get: () => null }, json: async () => ({ ok: true }) });
    }
    if (u.includes("/api/telemetry/mcp-calls")) {
        relayCalls.push({ kind: "get", url: u, headers: (opts && opts.headers) || null });
        return Promise.resolve({
            ok: true, status: 200, headers: { get: () => null },
            json: async () => ({ ok: true, records: [{ call_id: "c1", tool: "ds_search", args: { q: "肺癌 13812345678" } }], next_offset: 42 }),
        });
    }
    const body = String(opts && opts.body || "");
    relayCalls.push({ kind: "ingest", body: body });
    const packet = JSON.parse(body);
    return Promise.resolve({ ok: true, headers: { get: () => null }, json: async () => ({ ok: true, packet_id: packet.packet_id }) });
};
const relayDone = upload.maybeUploadUsage(false);
for (let i = 0; i < 100 && !relayCalls.some((c) => c.kind === "ingest"); i++) await new Promise((r) => setTimeout(r, 0));
{
    const ingest = relayCalls.find((c) => c.kind === "ingest");
    check("single benchfb record triggers upload (threshold 1)", !!ingest);
    const packet = JSON.parse(ingest.body);
    check("mcp_records attached to packet", Array.isArray(packet.mcp_records) && packet.mcp_records.length === 1, ingest.body.slice(0, 300));
    check("mcp record masked in packet", packet.mcp_records[0].args.q === "肺癌 [手机号]", JSON.stringify(packet.mcp_records[0]));
    await relayDone;
    const ack = relayCalls.find((c) => c.kind === "ack");
    check("mcp ack posted with next_offset", !!ack && JSON.parse(ack.body).offset === 42, ack && ack.body);
    // 中继改相对同源——不带绝对 origin、不带 X-Ingest-Token，分页参数随请求带上
    const get = relayCalls.find((c) => c.kind === "get");
    check("mcp get is same-origin relative", !!get && get.url.indexOf("/api/telemetry/mcp-calls?") === 0
        && get.url.indexOf("http") < 0, get && get.url);
    check("mcp get carries paging params", !!get && get.url.includes("limit=100")
        && get.url.includes("max_bytes=500000") && get.url.includes("since_ts="), get && get.url);
    check("mcp get sends no ingest token header", !!get && !get.headers, get && JSON.stringify(get.headers));
    const meta = JSON.parse(localStorage.getItem(ns(core.LS.usageUploadMeta, "relay")) || "{}");
    check("mcp offset persisted in upload meta", Number(meta.mcpOffset) === 42, JSON.stringify(meta));
    check("relay upload acked benchfb record", bench.benchfbRecordsForScope("relay").length === 0);
}
globalThis.fetch = savedFetch;

// 超大 benchfb 记录先截 top-20（truncated:true）照常上传，不再整条 manual-only。
// 截断点之外的用户标注名次（useful_idx>20）浅拷贝加 _preserved_idx 追加尾部。
localStorage.clear(); setup("shrink", 0);
const bigResults = Array.from({ length: 25 }, (_, i) => ({ dataset_uid: "u" + i, text: "y".repeat(85000) }));
localStorage.setItem(ns(core.LS.benchfb, "shrink"), JSON.stringify([
    { id: "huge", t: 1, kind: "search", search: { req: { query: "q" }, res: { results: bigResults, result_total: 25 } },
        rating: { useful_idx: [23] } },
]));
bench.benchfbOnAccountChanged(); pending = [];
const shrinkDone = upload.maybeUploadUsage(false); await waitCount(1);
const shrinkPacket = JSON.parse(pending[0].body);
check("oversize record truncated to top20 plus preserved labels and flagged", shrinkPacket.benchfb_records.length === 1
    && shrinkPacket.benchfb_records[0].search.res.results.length === 21
    && shrinkPacket.benchfb_records[0].truncated === true,
    shrinkPacket.benchfb_records.length + "/" + (shrinkPacket.benchfb_records[0] && shrinkPacket.benchfb_records[0].search.res.results.length));
const shrunkResults = shrinkPacket.benchfb_records[0].search.res.results;
check("preserved entry carries original rank", shrunkResults[20]._preserved_idx === 23
    && shrunkResults[20].dataset_uid === "u22", JSON.stringify(shrunkResults[20]).slice(0, 120));
check("top20 entries not marked preserved", shrunkResults.slice(0, 20).every((r) => !("_preserved_idx" in r)));
ok(pending[0]); await shrinkDone;
check("truncated record acked instead of manual-only", bench.benchfbRecordsForScope("shrink").length === 0);

// usage/benchfb 都空但有 MCP 中继记录时也发包（MCP-only），packet_id 材料含 call_id。
localStorage.clear(); setup("mcponly", 0); pending = [];
const mcpOnlyCalls = [];
const savedFetchM = globalThis.fetch;
globalThis.fetch = (url, opts) => {
    const u = String(url);
    if (u.includes("/api/telemetry/mcp-calls/ack")) {
        mcpOnlyCalls.push({ kind: "ack", body: String(opts && opts.body || "") });
        return Promise.resolve({ ok: true, status: 200, headers: { get: () => null }, json: async () => ({ ok: true }) });
    }
    if (u.includes("/api/telemetry/mcp-calls")) {
        mcpOnlyCalls.push({ kind: "get", url: u });
        return Promise.resolve({
            ok: true, status: 200, headers: { get: () => null },
            json: async () => ({ ok: true, records: [{ call_id: "c1", tool: "ds_search", args: { q: "肺癌" } }], next_offset: 7 }),
        });
    }
    const body = String(opts && opts.body || "");
    mcpOnlyCalls.push({ kind: "ingest", body: body });
    const packet = JSON.parse(body);
    return Promise.resolve({ ok: true, headers: { get: () => null }, json: async () => ({ ok: true, packet_id: packet.packet_id }) });
};
const mcpOnlyDone = upload.maybeUploadUsage(false);
for (let i = 0; i < 100 && !mcpOnlyCalls.some((c) => c.kind === "ingest"); i++) await new Promise((r) => setTimeout(r, 0));
{
    const ingest = mcpOnlyCalls.find((c) => c.kind === "ingest");
    check("mcp-only packet sent without usage/benchfb", !!ingest);
    const packet = JSON.parse(ingest.body);
    check("mcp-only packet carries one mcp record and nothing else",
        Array.isArray(packet.mcp_records) && packet.mcp_records.length === 1
        && packet.usage_events.length === 0 && packet.benchfb_records.length === 0, ingest.body.slice(0, 200));
    const profileId = usage.usageProfileIdForScope("mcponly");
    const material = ["batch", profileId, "", "", "c1", ""].join("|");
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(material));
    const expectedPkt = "pkt-" + Array.from(new Uint8Array(digest)).map((v) => v.toString(16).padStart(2, "0")).join("");
    check("packet id material includes mcp call ids", packet.packet_id === expectedPkt, packet.packet_id + " vs " + expectedPkt);
    await mcpOnlyDone;
    const ack = mcpOnlyCalls.find((c) => c.kind === "ack");
    check("mcp-only ack advances offset", !!ack && JSON.parse(ack.body).offset === 7, ack && ack.body);
}
globalThis.fetch = savedFetchM;

// ack 只前进——本地 offset 已 50，对端页 next_offset 42 不得把本地回退。
localStorage.clear(); setup("ackonly", 0);
localStorage.setItem(ns(core.LS.usageUploadMeta, "ackonly"), JSON.stringify({ mcpOffset: 50 }));
pending = [];
const ackOnlyCalls = [];
const savedFetchA = globalThis.fetch;
globalThis.fetch = (url, opts) => {
    const u = String(url);
    if (u.includes("/api/telemetry/mcp-calls/ack")) {
        ackOnlyCalls.push({ kind: "ack", body: String(opts && opts.body || "") });
        return Promise.resolve({ ok: true, status: 200, headers: { get: () => null }, json: async () => ({ ok: true }) });
    }
    if (u.includes("/api/telemetry/mcp-calls")) {
        ackOnlyCalls.push({ kind: "get", url: u });
        return Promise.resolve({
            ok: true, status: 200, headers: { get: () => null },
            json: async () => ({ ok: true, records: [{ call_id: "c2", tool: "ds_search" }], next_offset: 42 }),
        });
    }
    const body = String(opts && opts.body || "");
    ackOnlyCalls.push({ kind: "ingest", body: body });
    const packet = JSON.parse(body);
    return Promise.resolve({ ok: true, headers: { get: () => null }, json: async () => ({ ok: true, packet_id: packet.packet_id }) });
};
const ackOnlyDone = upload.maybeUploadUsage(false);
for (let i = 0; i < 100 && !ackOnlyCalls.some((c) => c.kind === "ack"); i++) await new Promise((r) => setTimeout(r, 0));
await ackOnlyDone;
{
    const get = ackOnlyCalls.find((c) => c.kind === "get");
    check("mcp get starts from local offset", !!get && get.url.includes("after=50"), get && get.url);
    const ack = ackOnlyCalls.find((c) => c.kind === "ack");
    check("ack never moves offset backwards", !!ack && JSON.parse(ack.body).offset === 50, ack && ack.body);
    const meta = JSON.parse(localStorage.getItem(ns(core.LS.usageUploadMeta, "ackonly")) || "{}");
    check("local offset stays at max", Number(meta.mcpOffset) === 50, JSON.stringify(meta));
}
globalThis.fetch = savedFetchA;

// keepalive 尽力档 30s 最小间隔——上一发（无论成败）后 30s 内不再发。
localStorage.clear(); setup("ka", 12); pending = [];
const ka1 = upload.maybeUploadUsage(false, { keepalive: true });
await waitCount(1);
ok(pending[0]);
check("first keepalive sends", (await ka1) === true);
// 再补 12 条新事件，确保第二发被拦是因为 30s 闸而不是「队列空了」
localStorage.setItem(ns(core.LS.usage, "ka"), JSON.stringify(events(12, "ka2")));
usage.usageOnAccountChanged();
pending = [];
const ka2 = await upload.maybeUploadUsage(false, { keepalive: true });
check("keepalive within 30s of last attempt is gated", ka2 === false && pending.length === 0, String(ka2) + "/" + pending.length);

// 常规上传 3 分钟最小间隔——距上次成功 < 3min 即使有新 benchfb 记录也不再发
// （force 只绕 30s 评估防抖，不绕配额闸；5 分钟周期兜底天然满足该间隔）。
localStorage.clear(); setup("quota", 0);
localStorage.setItem(ns(core.LS.benchfb, "quota"), JSON.stringify([{ id: "q1", t: 1, kind: "search" }]));
bench.benchfbOnAccountChanged(); pending = [];
const quota1 = upload.maybeUploadUsage(false, { force: true });
await waitCount(1);
ok(pending[0]);
check("first regular upload sends", (await quota1) === true);
localStorage.setItem(ns(core.LS.benchfb, "quota"), JSON.stringify([{ id: "q2", t: 2, kind: "search" }]));
bench.benchfbOnAccountChanged(); pending = [];
const quota2 = await upload.maybeUploadUsage(false, { force: true });
check("regular upload within 3min of last success is gated", quota2 === false && pending.length === 0,
    String(quota2) + "/" + pending.length);

// since_ts 过滤墙——中继空页但 next_offset 已推进（本页行全被滤掉、offset 照常前进）
// → 客户端回执推进本地 offset，不再 return null 把 offset 冻在墙前；墙后记录下轮可出。
function wallMock(calls) {
    return (url, opts) => {
        const u = String(url);
        if (u.includes("/api/telemetry/mcp-calls/ack")) {
            calls.push({ kind: "ack", body: String(opts && opts.body || "") });
            return Promise.resolve({ ok: true, status: 200, headers: { get: () => null }, json: async () => ({ ok: true }) });
        }
        if (u.includes("/api/telemetry/mcp-calls")) {
            calls.push({ kind: "get", url: u });
            return Promise.resolve({
                ok: true, status: 200, headers: { get: () => null },
                json: async () => ({ ok: true, records: [], next_offset: 100, truncated: false }),
            });
        }
        const body = String(opts && opts.body || "");
        calls.push({ kind: "ingest", body: body });
        const packet = JSON.parse(body);
        return Promise.resolve({ ok: true, headers: { get: () => null }, json: async () => ({ ok: true, packet_id: packet.packet_id }) });
    };
}

// 纯墙页（usage/benchfb 都空）：ack 照常推进，不发空包。
localStorage.clear(); setup("wall", 0);
const wallCalls = [];
const savedFetchW = globalThis.fetch;
globalThis.fetch = wallMock(wallCalls);
const wallDone = upload.maybeUploadUsage(false);
for (let i = 0; i < 100 && !wallCalls.some((c) => c.kind === "ack"); i++) await new Promise((r) => setTimeout(r, 0));
await wallDone;
{
    const get = wallCalls.find((c) => c.kind === "get");
    check("wall page fetched from offset 0", !!get && get.url.includes("after=0"), get && get.url);
    const ack = wallCalls.find((c) => c.kind === "ack");
    check("wall empty page acked to advance offset", !!ack && JSON.parse(ack.body).offset === 100, ack && ack.body);
    const meta = JSON.parse(localStorage.getItem(ns(core.LS.usageUploadMeta, "wall")) || "{}");
    check("wall offset persisted (no freeze)", Number(meta.mcpOffset) === 100, JSON.stringify(meta));
    check("pure wall page sends no empty packet", !wallCalls.some((c) => c.kind === "ingest"));
}
globalThis.fetch = savedFetchW;

// 墙页 + 足够 usage：墙先回执推进 offset，usage 照常上传；成功保存不得把 offset 回滚
// （否则墙前 offset 每轮重扫，墙后记录永远出不来）。
localStorage.clear(); setup("wall2", 12); pending = [];
const wall2Calls = [];
const savedFetchW2 = globalThis.fetch;
globalThis.fetch = wallMock(wall2Calls);
const wall2Done = upload.maybeUploadUsage(false);
for (let i = 0; i < 100 && !wall2Calls.some((c) => c.kind === "ingest"); i++) await new Promise((r) => setTimeout(r, 0));
await wall2Done;
{
    const ingest = wall2Calls.find((c) => c.kind === "ingest");
    check("wall + enough usage still uploads", !!ingest);
    const ack = wall2Calls.find((c) => c.kind === "ack");
    check("wall ack advances offset alongside upload", !!ack && JSON.parse(ack.body).offset === 100, ack && ack.body);
    const meta = JSON.parse(localStorage.getItem(ns(core.LS.usageUploadMeta, "wall2")) || "{}");
    check("success save does not roll back wall offset", Number(meta.mcpOffset) === 100, JSON.stringify(meta));
    check("wall2 usage acked", usage.usageEventsForScope("wall2").length === 0);
}
globalThis.fetch = savedFetchW2;

// 200 server_hint 采用 + 持久化（跨刷新生效——模块每次上传都重新读 meta）。
localStorage.clear(); setup("hint", 50); pending = [];
const hintCalls = [];
const savedFetchH = globalThis.fetch;
globalThis.fetch = (url, opts) => {
    const u = String(url);
    if (u.indexOf("/api/telemetry/mcp-calls") >= 0) return Promise.resolve({ ok: false, status: 404, headers: { get: () => null }, json: async () => ({}) });
    const body = String(opts && opts.body || "");
    hintCalls.push({ kind: "ingest", body: body });
    const packet = JSON.parse(body);
    return Promise.resolve({
        ok: true, status: 200, headers: { get: () => null },
        json: async () => ({ ok: true, packet_id: packet.packet_id, server_hint: { pressure: 0.5, batch_threshold: 5, min_interval_ms: 120000 } }),
    });
};
const hintDone = upload.maybeUploadUsage(false);
for (let i = 0; i < 100 && !hintCalls.some((c) => c.kind === "ingest"); i++) await new Promise((r) => setTimeout(r, 0));
await hintDone;
{
    const meta = JSON.parse(localStorage.getItem(ns(core.LS.usageUploadMeta, "hint")) || "{}");
    check("server hint adopted and persisted", Number(meta.hintThreshold) === 5 && Number(meta.hintIntervalMs) === 120000, JSON.stringify(meta));
    // 跨刷新语义：新调用重新读 meta（等价刷新后的会话）——即便再攒够 5 条新事件，120s 间隔闸仍拦。
    localStorage.setItem(ns(core.LS.usage, "hint"), JSON.stringify(events(5, "hint-more")));
    usage.usageOnAccountChanged(); pending = [];
    const gated = await upload.maybeUploadUsage(false, { force: true });
    check("hint interval gates next upload from persisted meta", gated === false && pending.length === 0, String(gated) + "/" + pending.length);
}
globalThis.fetch = savedFetchH;

// hint 钳制上界——batch_threshold 999→50、min_interval_ms 1000→15s。
localStorage.clear(); setup("clamp", 50); pending = [];
const clampCalls = [];
const savedFetchK = globalThis.fetch;
globalThis.fetch = (url, opts) => {
    const u = String(url);
    if (u.indexOf("/api/telemetry/mcp-calls") >= 0) return Promise.resolve({ ok: false, status: 404, headers: { get: () => null }, json: async () => ({}) });
    const body = String(opts && opts.body || "");
    clampCalls.push({ kind: "ingest", body: body });
    const packet = JSON.parse(body);
    return Promise.resolve({
        ok: true, status: 200, headers: { get: () => null },
        json: async () => ({ ok: true, packet_id: packet.packet_id, server_hint: { pressure: 1.0, batch_threshold: 999, min_interval_ms: 1000 } }),
    });
};
const clampDone = upload.maybeUploadUsage(false);
for (let i = 0; i < 100 && !clampCalls.some((c) => c.kind === "ingest"); i++) await new Promise((r) => setTimeout(r, 0));
await clampDone;
{
    const meta = JSON.parse(localStorage.getItem(ns(core.LS.usageUploadMeta, "clamp")) || "{}");
    check("hint threshold clamped to upper bound 50", Number(meta.hintThreshold) === 50, JSON.stringify(meta));
    check("hint interval clamped to lower bound 15s", Number(meta.hintIntervalMs) === 15000, JSON.stringify(meta));
    // 钳制后的阈值 50 生效：25 条新事件（默认阈值 2 早该上传）被动态阈值拦下。
    // 先清 lastSuccess 模拟钳制后的 15s 间隔已过（否则先撞间隔闸，测不到阈值档）。
    const m = JSON.parse(localStorage.getItem(ns(core.LS.usageUploadMeta, "clamp")) || "{}");
    delete m.lastSuccess;
    localStorage.setItem(ns(core.LS.usageUploadMeta, "clamp"), JSON.stringify(m));
    localStorage.setItem(ns(core.LS.usage, "clamp"), JSON.stringify(events(25, "clamp-more")));
    usage.usageOnAccountChanged();
    const gatedByThreshold = await upload.maybeUploadUsage(false, { force: true });
    check("clamped threshold gates mid-size queue", gatedByThreshold === false, String(gatedByThreshold));
}
globalThis.fetch = savedFetchK;

// hint 钳制下界——batch_threshold 0→2、min_interval_ms 10_000_000→10min。
localStorage.clear(); setup("clamplow", 50); pending = [];
const clampLowCalls = [];
const savedFetchL = globalThis.fetch;
globalThis.fetch = (url, opts) => {
    const u = String(url);
    if (u.indexOf("/api/telemetry/mcp-calls") >= 0) return Promise.resolve({ ok: false, status: 404, headers: { get: () => null }, json: async () => ({}) });
    const body = String(opts && opts.body || "");
    clampLowCalls.push({ kind: "ingest", body: body });
    const packet = JSON.parse(body);
    return Promise.resolve({
        ok: true, status: 200, headers: { get: () => null },
        json: async () => ({ ok: true, packet_id: packet.packet_id, server_hint: { pressure: 0.0, batch_threshold: 0, min_interval_ms: 10000000 } }),
    });
};
const clampLowDone = upload.maybeUploadUsage(false);
for (let i = 0; i < 100 && !clampLowCalls.some((c) => c.kind === "ingest"); i++) await new Promise((r) => setTimeout(r, 0));
await clampLowDone;
{
    const meta = JSON.parse(localStorage.getItem(ns(core.LS.usageUploadMeta, "clamplow")) || "{}");
    check("hint threshold clamped to lower bound 2", Number(meta.hintThreshold) === 2, JSON.stringify(meta));
    check("hint interval clamped to upper bound 10min", Number(meta.hintIntervalMs) === 600000, JSON.stringify(meta));
    // 10min 间隔生效：再攒 5 条新事件也发不出去（间隔闸，钳制后 600s > 已过时间）。
    localStorage.setItem(ns(core.LS.usage, "clamplow"), JSON.stringify(events(5, "clamp-low-more")));
    usage.usageOnAccountChanged();
    const gatedLow = await upload.maybeUploadUsage(false, { force: true });
    check("clamped 10min interval gates re-upload", gatedLow === false, String(gatedLow));
}
globalThis.fetch = savedFetchL;

// 429 → 临时高档（20 条 / 5 分钟）持久化；下一次 200 hint 覆盖。
localStorage.clear(); setup("rl429", 50); pending = [];
const rlCalls = [];
const savedFetchR = globalThis.fetch;
globalThis.fetch = (url, opts) => {
    const u = String(url);
    if (u.indexOf("/api/telemetry/mcp-calls") >= 0) return Promise.resolve({ ok: false, status: 404, headers: { get: () => null }, json: async () => ({}) });
    const body = String(opts && opts.body || "");
    rlCalls.push({ kind: "ingest", body: body });
    const packet = JSON.parse(body);
    if (rlCalls.filter((c) => c.kind === "ingest").length === 1) {
        return Promise.resolve({ ok: false, status: 429, headers: { get: () => null }, json: async () => ({ detail: "rate limited" }) });
    }
    return Promise.resolve({
        ok: true, status: 200, headers: { get: () => null },
        json: async () => ({ ok: true, packet_id: packet.packet_id, server_hint: { pressure: 0.1, batch_threshold: 2, min_interval_ms: 30000 } }),
    });
};
const rlDone = upload.maybeUploadUsage(false);
for (let i = 0; i < 100 && !rlCalls.some((c) => c.kind === "ingest"); i++) await new Promise((r) => setTimeout(r, 0));
await rlDone;
{
    const meta = JSON.parse(localStorage.getItem(ns(core.LS.usageUploadMeta, "rl429")) || "{}");
    check("429 escalates to high band", Number(meta.hintThreshold) === 20 && Number(meta.hintIntervalMs) === 300000, JSON.stringify(meta));
    check("429 still backs off", Number(meta.failCount) >= 1 && Number(meta.nextAttempt) > 0, JSON.stringify(meta));
    check("429 keeps events queued", usage.usageEventsForScope("rl429").length === 50, String(usage.usageEventsForScope("rl429").length));
    // 退避过后重试（清 nextAttempt 模拟 Retry-After 到期）：200 + low hint 覆盖高档。
    const m = JSON.parse(localStorage.getItem(ns(core.LS.usageUploadMeta, "rl429")) || "{}");
    m.nextAttempt = 0; m.failCount = 0;
    localStorage.setItem(ns(core.LS.usageUploadMeta, "rl429"), JSON.stringify(m));
}
const rl2 = upload.maybeUploadUsage(false, { force: true });
for (let i = 0; i < 100 && rlCalls.filter((c) => c.kind === "ingest").length < 2; i++) await new Promise((r) => setTimeout(r, 0));
await rl2;
{
    const meta = JSON.parse(localStorage.getItem(ns(core.LS.usageUploadMeta, "rl429")) || "{}");
    check("200 hint overrides 429 band", Number(meta.hintThreshold) === 2 && Number(meta.hintIntervalMs) === 30000, JSON.stringify(meta));
}
globalThis.fetch = savedFetchR;

// 413 必须在同一轮立即缩小预算重组，不能让 1~1.9MB 的场外旧客户端包永久退避。
localStorage.clear(); setup("body413", 0);
localStorage.setItem(ns(core.LS.benchfb, "body413"), JSON.stringify([
    { id: "body-a", t: 1, kind: "search", blob: "甲".repeat(240_000) },
    { id: "body-b", t: 2, kind: "search", blob: "乙".repeat(240_000) },
]));
bench.benchfbOnAccountChanged();
const bodyCalls = [];
const savedFetch413 = globalThis.fetch;
globalThis.fetch = (url, opts) => {
    const u = String(url);
    if (u.indexOf("/api/telemetry/mcp-calls") >= 0) {
        return Promise.resolve({ ok: false, status: 404, headers: { get: () => null }, json: async () => ({}) });
    }
    const body = String(opts && opts.body || "");
    const packet = JSON.parse(body);
    bodyCalls.push({ bytes: new TextEncoder().encode(body).length, packet: packet });
    if (bodyCalls.length === 1) {
        return Promise.resolve({
            ok: false, status: 413, headers: { get: () => null },
            json: async () => ({ detail: { code: "payload_too_large", max_body_bytes: 1_048_576 } }),
        });
    }
    return Promise.resolve({
        ok: true, status: 200, headers: { get: () => null },
        json: async () => ({ ok: true, packet_id: packet.packet_id,
            server_hint: { pressure: 0.1, batch_threshold: 2, min_interval_ms: 30_000, max_body_bytes: 1_048_576 } }),
    });
};
const bodyRecovered = await upload.maybeUploadUsage(false);
check("413 retries immediately with a smaller packet", bodyRecovered === true && bodyCalls.length === 2,
    String(bodyRecovered) + "/" + bodyCalls.length);
check("413 retry obeys announced safety budget", bodyCalls[1].bytes < 1_048_576 * 0.91, String(bodyCalls[1].bytes));
check("413 retry acks only the transmitted prefix", bench.benchfbRecordsForScope("body413").length === 1,
    JSON.stringify(bench.benchfbRecordsForScope("body413").map((r) => r.id)));
{
    const meta = JSON.parse(localStorage.getItem(ns(core.LS.usageUploadMeta, "body413")) || "{}");
    check("413 negotiated body budget persisted", Number(meta.hintBodyMaxBytes) === Math.floor(1_048_576 * 0.90), JSON.stringify(meta));
    check("413 recovery does not enter backoff", Number(meta.failCount) === 0 && Number(meta.nextAttempt) === 0, JSON.stringify(meta));
}
globalThis.fetch = savedFetch413;

// 默认阈值 2 + 老接收端（200 无 server_hint）不动动态值（fail-safe）。
// 此段直接用顶层的 pending mock（200 响应不带 server_hint，等同老接收端）。
localStorage.clear(); setup("dflt", 1); pending = [];
const dflt1 = await upload.maybeUploadUsage(false, { force: true });
check("one event below default threshold 2 stays queued", dflt1 === false && pending.length === 0, String(dflt1) + "/" + pending.length);
localStorage.setItem(ns(core.LS.usage, "dflt"), JSON.stringify(events(2, "dflt-more")));
usage.usageOnAccountChanged(); pending = [];
const dflt2 = upload.maybeUploadUsage(false, { force: true });
await waitCount(1);
ok(pending[0]);
check("two events trigger default upload", (await dflt2) === true);
{
    const meta = JSON.parse(localStorage.getItem(ns(core.LS.usageUploadMeta, "dflt")) || "{}");
    check("no-hint 200 leaves dynamic values untouched", !("hintThreshold" in meta) && !("hintIntervalMs" in meta), JSON.stringify(meta));
    check("no-hint upload acked", usage.usageEventsForScope("dflt").length === 0);
}

// MCP-only 包不受动态阈值拦（即使 meta 已是 429 高档 20 条）。
localStorage.clear(); setup("mcphi", 0);
localStorage.setItem(ns(core.LS.usageUploadMeta, "mcphi"), JSON.stringify({ hintThreshold: 20, hintIntervalMs: 300000 }));
const mcpHiCalls = [];
const savedFetchMcp = globalThis.fetch;
globalThis.fetch = (url, opts) => {
    const u = String(url);
    if (u.includes("/api/telemetry/mcp-calls/ack")) {
        mcpHiCalls.push({ kind: "ack", body: String(opts && opts.body || "") });
        return Promise.resolve({ ok: true, status: 200, headers: { get: () => null }, json: async () => ({ ok: true }) });
    }
    if (u.includes("/api/telemetry/mcp-calls")) {
        mcpHiCalls.push({ kind: "get", url: u });
        return Promise.resolve({
            ok: true, status: 200, headers: { get: () => null },
            json: async () => ({ ok: true, records: [{ call_id: "c9", tool: "ds_search" }], next_offset: 3 }),
        });
    }
    const body = String(opts && opts.body || "");
    mcpHiCalls.push({ kind: "ingest", body: body });
    const packet = JSON.parse(body);
    return Promise.resolve({ ok: true, headers: { get: () => null }, json: async () => ({ ok: true, packet_id: packet.packet_id }) });
};
const mcpHiDone = upload.maybeUploadUsage(false);
for (let i = 0; i < 100 && !mcpHiCalls.some((c) => c.kind === "ingest"); i++) await new Promise((r) => setTimeout(r, 0));
await mcpHiDone;
check("mcp-only packet bypasses high dynamic threshold", mcpHiCalls.some((c) => c.kind === "ingest"));
globalThis.fetch = savedFetchMcp;

if (failed) process.exit(1);
