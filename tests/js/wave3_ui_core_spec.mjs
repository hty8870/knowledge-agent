import assert from "node:assert/strict";
import { COPY, armTwoStepConfirm } from "../../web/static/js/core/copy.js";
import { queueMigrateLegacyArray, queueRead, queueRemoveIds, queueWrite } from "../../web/static/js/core/storage_queue.js";

class Storage {
    constructor() { this.rows = new Map(); }
    get length() { return this.rows.size; }
    key(i) { return Array.from(this.rows.keys())[i] ?? null; }
    getItem(k) { return this.rows.has(k) ? this.rows.get(k) : null; }
    setItem(k, v) { this.rows.set(k, String(v)); }
    removeItem(k) { this.rows.delete(k); }
}

assert.equal(COPY.common.confirmDelete, "再点一次确认删除");
assert.equal(COPY.conditions.include, "纳入条件");
assert.equal(COPY.boardZones.prefer_off.title, "这次没有拿它排先后");

const classes = new Set();
const button = {
    dataset: {}, textContent: "删除", _twoStepTimer: null,
    classList: { add: (x) => classes.add(x), remove: (x) => classes.delete(x) },
};
assert.equal(armTwoStepConfirm(button, { idleText: "删除", timeoutMs: 10000 }), false);
assert.equal(button.dataset.confirmArmed, "1");
assert.equal(armTwoStepConfirm(button, { idleText: "删除", timeoutMs: 10000 }), true);
assert.equal(button.dataset.confirmArmed, undefined);
assert.equal(button.textContent, "删除");

const storage = new Storage();
storage.setItem("legacy", JSON.stringify([{ id: "a", t: 2 }, { id: "b", t: 1 }]));
assert.equal(queueMigrateLegacyArray(storage, {
    legacyKey: "legacy", prefix: "q:", id: (row) => row.id,
}), true);
assert.equal(storage.getItem("legacy"), null);
assert.deepEqual(queueRead(storage, "q:", (a, b) => a.t - b.t).map((x) => x.id), ["b", "a"]);
assert.equal(queueWrite(storage, "q:", "c", { id: "c", t: 3 }), true);
queueRemoveIds(storage, "q:", ["a", "c"]);
assert.deepEqual(queueRead(storage, "q:").map((x) => x.id), ["b"]);

console.log("wave3 ui core specs passed");
