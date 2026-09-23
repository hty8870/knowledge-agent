/* 遥测卡级归因规格（schema v3）：假 DOM/localStorage，零外网。
 * 覆盖：usageImpressionItems 形状、ImpressionContext 绑定与快照归因（换轮不串号）、
 * imp 事件形状与空 items 拒发、search 事件 policy_id 优先、benchfb label 事件 rev/台账兜底。
 * 刻意**不播种 consent**（usageEnabled="1" 即可）：usageLog 触发的 _fireUpload 会被
 * consent 门拦下，全程零网络（末尾断言 fetch 一次都没被调）。 */
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
globalThis.sessionStorage = new MemoryStorage();
globalThis.window = {
    gsap: undefined, ScrollTrigger: undefined,
    crypto: globalThis.crypto,
    matchMedia: () => ({ matches: false }),
    addEventListener: () => {},
};
globalThis.document = {
    getElementById: () => null,
    querySelector: () => ({ src: "http://127.0.0.1/static/js/core/boot.js?v=test" }),
    querySelectorAll: () => [],
    addEventListener: () => {},
    body: { classList: { add() {}, remove() {} }, contains: () => false },
};
Object.defineProperty(globalThis, "navigator", {
    value: { userAgent: "node-spec", language: "zh-CN" }, configurable: true,
});
let netCalls = 0;
globalThis.fetch = () => {
    netCalls++;
    return Promise.resolve({ ok: false, status: 404, headers: { get: () => null }, json: async () => ({}) });
};

const core = await import(new URL("../../web/static/js/core/core.js", import.meta.url));
const U = await import(new URL("../../web/static/js/core/usage_core.js", import.meta.url));
const usage = await import(new URL("../../web/static/js/core/usage_log.js", import.meta.url));
const bench = await import(new URL("../../web/static/js/core/benchfb.js", import.meta.url));

let failed = 0;
function check(label, ok, detail) {
    if (ok) console.log("PASS", label);
    else { failed += 1; console.error("FAIL", label, detail || ""); }
}
function ns(base, scope) { return core.nsKeyFor(base, scope); }
function setup(scope) {
    core.setCurrentUser({ id: scope });
    localStorage.setItem(ns(core.LS.usageEnabled, scope), "1");
    // 故意不播种 consent：上传被 consent 门拦下（零网络），事件仍正常落盘。
    usage.usageOnAccountChanged(); bench.benchfbOnAccountChanged();
}
function eventsOf(scope, kind) { return usage.usageEventsForScope(scope).filter((e) => e.k === kind); }

/* ---------- (a) 当屏条目快照 + ImpressionContext 形状 ---------- */
const items = usage.usageImpressionItems([
    { dataset_uid: "d1", score: 0.123456, reason: "x".repeat(200) },
    { dataset_uid: "d2" },
]);
check("impression items carry uid/pos/rounded score/truncated reason",
    items.length === 2 && items[0].uid === "d1" && items[0].pos === 1
    && items[0].score === 0.1235 && items[0].reason.length === 160, JSON.stringify(items));
check("missing score/reason omitted not fabricated",
    items[1].pos === 2 && !("score" in items[1]) && !("reason" in items[1]), JSON.stringify(items[1]));
const imp0 = usage.usageMakeImpression({ tid: "t-x", iid: "i-x", policy: "p0", items: items });
check("impression context is frozen", Object.isFrozen(imp0));
check("blank tid/iid stored as null (honest absence)",
    usage.usageMakeImpression({ tid: "", iid: undefined }).tid === null
    && usage.usageMakeImpression({}).iid === null);

/* ---------- (b) 卡绑定快照：换轮/重渲不串号；无绑定显式 null ---------- */
setup("imp1");
const tid1 = U.usageBeginTurn();
const iid1 = U.usageBeginImpression();
const ctx = usage.usageMakeImpression({ tid: tid1, iid: iid1, policy: "p1", items: items });
const card = {};   // WeakMap 键：任意对象即可，不需要真 DOM
usage.usageBindImpression(card, ctx);
check("impression of bound card", usage.usageImpressionOf(card) === ctx);
usage.usageLogCardAction(card, "open", { uid: "d1", pos: 1 }, "imp1");
const openEv = eventsOf("imp1", "open").pop();
check("card action carries snapshot tid/iid/policy",
    !!openEv && openEv.tid === tid1 && openEv.iid === iid1 && openEv.policy === "p1" && openEv.uid === "d1",
    JSON.stringify(openEv));
U.usageBeginTurn(); U.usageBeginImpression();   // 新一轮/新展示
usage.usageLogCardAction(card, "fav", { uid: "d1", pos: 1 }, "imp1");
const favEv = eventsOf("imp1", "fav").pop();
check("bound card keeps old snapshot after new turn",
    !!favEv && favEv.tid === tid1 && favEv.iid === iid1, JSON.stringify(favEv));
usage.usageLogCardAction(null, "fav", { uid: "d9" }, "imp1");
const nullEv = eventsOf("imp1", "fav").filter((e) => e.uid === "d9").pop();
check("unbound card action has explicit null tid/iid",
    !!nullEv && nullEv.tid === null && nullEv.iid === null, JSON.stringify(nullEv));
check("unbound card action still gets sid",
    !!nullEv && typeof nullEv.sid === "string" && nullEv.sid.indexOf("sid-") === 0);

/* ---------- (c) imp 事件：形状、空 items 拒发、显式 null 压住全局注入 ---------- */
const n0 = usage.usageEventsForScope("imp1").length;
check("empty-items impression refused",
    usage.usageLogImpression(usage.usageMakeImpression({ tid: tid1, iid: iid1, items: [] }), "imp1") === false
    && usage.usageEventsForScope("imp1").length === n0);
check("impression logged", usage.usageLogImpression(ctx, "imp1") === true);
const impEv = eventsOf("imp1", "imp").pop();
check("imp event shape",
    !!impEv && impEv.tid === tid1 && impEv.iid === iid1 && impEv.policy === "p1"
    && Array.isArray(impEv.items) && impEv.items.length === 2 && impEv.sid.indexOf("sid-") === 0,
    JSON.stringify(impEv));
usage.usageLogImpression(usage.usageMakeImpression({ tid: null, iid: null, policy: "p2", items: items }), "imp1");
const histImp = eventsOf("imp1", "imp").pop();
check("history impression keeps explicit null tid despite active turn",
    !!histImp && histImp.tid === null && histImp.iid === null, JSON.stringify(histImp));

/* ---------- (d) search 事件：policy_id 优先、items 移走、回退 policyParts ---------- */
usage.usageLogSearch(
    { results: [{ dataset_uid: "d1", score: 1 }], result_total: 1,
        policy_id: { schema: "biodata-policy-id/1", corpus: { snapshot_id: "snap-7", n_records: 784 },
            sources: ["10x Genomics"], ranking: { strategy: "auto", rerank: "off", recall: "cross" },
            model: "", app_version: "2.6.0", router_version: "turn-route/v1" },
        policy_id_str: "bpol1:snap=snap-7;strategy=auto;h=abc" },
    "肺癌", { handSubmit: true, telemetryScope: "imp1", policyParts: { strategy: "auto", rerank: "off", recall: "cross" } });
const sEv = eventsOf("imp1", "search").filter((e) => e.q === "肺癌").pop();
check("search prefers backend policy_id_str", !!sEv && sEv.policy === "bpol1:snap=snap-7;strategy=auto;h=abc", sEv && sEv.policy);
check("search policy never stringifies object", !!sEv && sEv.policy !== "[object Object]", sEv && sEv.policy);
check("search no longer carries inline items", !!sEv && !("items" in sEv), JSON.stringify(sEv));
usage.usageLogSearch({ results: [], result_total: 0 }, "肺癌2",
    { handSubmit: true, telemetryScope: "imp1", policyParts: { strategy: "auto", rerank: "off", recall: "cross" } });
const sEv2 = eventsOf("imp1", "search").filter((e) => e.q === "肺癌2").pop();
check("search falls back to policy parts", !!sEv2 && sEv2.policy.indexOf("auto/off/cross") === 0, sEv2 && sEv2.policy);

/* ---------- (e) benchfb label 事件：rev 递增、tid 归属、记录删除后台账兜底 ---------- */
setup("imp2");
const tidB = U.usageBeginTurn();
bench.benchfbTurnBegin("人类肺癌", { scope: "imp2" });
bench.benchfbTurnSearch({ query: "人类肺癌" },
    { results: [{ dataset_uid: "d1" }, { dataset_uid: "d2" }], result_total: 2 },
    { scope: "imp2", handSubmit: true, query: "人类肺癌" });
const recs = bench.benchfbRecordsForScope("imp2");
check("benchfb record persisted", recs.length === 1, String(recs.length));
const recId = recs[0].id;
const updated = bench.benchfbRateRecord(recId, { completion: "done" });
check("rate returns merged record while record exists", !!updated);
let labelEv = eventsOf("imp2", "label").pop();
check("label rev1 carries record tid, explicit null iid and recId",
    !!labelEv && labelEv.rev === 1 && labelEv.tid === tidB && labelEv.iid === null
    && labelEv.recId === recId && labelEv.completion === "done", JSON.stringify(labelEv));
bench.benchfbRateRecord(recId, { usefulIdx: [2] });
labelEv = eventsOf("imp2", "label").pop();
check("label rev2 resolves useful_uids from record results",
    !!labelEv && labelEv.rev === 2 && JSON.stringify(labelEv.useful_uids) === '["d2"]'
    && JSON.stringify(labelEv.useful_idx) === "[2]" && labelEv.completion === "done", JSON.stringify(labelEv));
bench.benchfbRemoveRecordsForScope("imp2", [recId]);   // 模拟上传 ACK 精确删除
check("record removed (ack simulated)", bench.benchfbRecordsForScope("imp2").length === 0);
check("rate after ack returns null but still emits", bench.benchfbRateRecord(recId, { comment: "不错" }) === null);
labelEv = eventsOf("imp2", "label").pop();
check("label rev3 from ledger keeps tid, recId and old useful_uids",
    !!labelEv && labelEv.rev === 3 && labelEv.tid === tidB && labelEv.recId === recId
    && labelEv.comment === "不错"
    && JSON.stringify(labelEv.useful_idx) === "[2]" && JSON.stringify(labelEv.useful_uids) === '["d2"]',
    JSON.stringify(labelEv));
bench.benchfbRateRecord(recId, { usefulIdx: [1] });
labelEv = eventsOf("imp2", "label").pop();
check("label rev4 changed useful_idx empties uids (never fabricated)",
    !!labelEv && labelEv.rev === 4 && JSON.stringify(labelEv.useful_idx) === "[1]"
    && JSON.stringify(labelEv.useful_uids) === "[]", JSON.stringify(labelEv));

/* ---------- 收尾：全程零网络 ---------- */
check("no network call without consent", netCalls === 0, String(netCalls));

console.log(failed ? `\n${failed} 条失败` : "\n全部通过\nTELEMETRY_IMPRESSION_SPEC_OK");
process.exit(failed ? 1 : 0);
