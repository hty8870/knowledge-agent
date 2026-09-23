"use strict";

/* Generic one-record-per-key localStorage queue. Feature modules own schemas,
   ids and retention policy; this core owns enumeration, migration and deletion
   so multi-tab-safe queues cannot drift into separate implementations. */
export function queueRead(storage, prefix, compare) {
    const out = [];
    try {
        for (let i = 0; i < storage.length; i++) {
            const key = storage.key(i);
            if (!key || !key.startsWith(prefix)) continue;
            const value = JSON.parse(storage.getItem(key));
            if (value && typeof value === "object") out.push(value);
        }
    } catch (_e) {}
    if (compare) out.sort(compare);
    return out;
}

export function queueWrite(storage, prefix, id, value) {
    try { storage.setItem(prefix + String(id), JSON.stringify(value)); return true; }
    catch (_e) { return false; }
}

export function queueRemoveIds(storage, prefix, ids) {
    (ids || []).forEach((id) => { try { storage.removeItem(prefix + String(id)); } catch (_e) {} });
}

export function queueRemovePrefix(storage, prefix) {
    const keys = [];
    try {
        for (let i = 0; i < storage.length; i++) {
            const key = storage.key(i); if (key && key.startsWith(prefix)) keys.push(key);
        }
        keys.forEach((key) => storage.removeItem(key));
    } catch (_e) {}
}

export function queueMigrateLegacyArray(storage, options) {
    const opts = options || {};
    let rows = [];
    try { rows = JSON.parse(storage.getItem(opts.legacyKey) || "[]"); } catch (_e) { rows = []; }
    if (!Array.isArray(rows) || !rows.length) return true;
    let complete = true;
    rows.forEach((raw, index) => {
        const value = opts.normalize ? opts.normalize(raw, index) : raw;
        if (!queueWrite(storage, opts.prefix, opts.id(value, index), value)) complete = false;
    });
    if (complete) { try { storage.removeItem(opts.legacyKey); } catch (_e) {} }
    return complete;
}
