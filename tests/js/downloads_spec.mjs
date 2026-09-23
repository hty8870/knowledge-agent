"use strict";
/* 下载队列（core/downloads.js）计数语义规格。钉 2026-08-31 PR#7 自动评审实锤的回归：
   auto:true 时 dlqEnqueue 内同步泵出队首（本批第一个 queued→fired），dlqEnqueueDatasets
   的 res.queued 必须把 fired 也算进「本批新发射」——只数 queued 会把单文件直下错算成 0，
   调用方（act.js 回执 / task_pack 主按钮）据此误判「零直下」走降级打包分支、遥测漏记。 */

// 桩 window=globalThis 本物（浏览器里 window 即全局）；core 顶层读 matchMedia/localStorage。
globalThis.window = globalThis;
const _store = new Map();
globalThis.localStorage = {
    getItem: (k) => (_store.has(k) ? _store.get(k) : null),
    setItem: (k, v) => { _store.set(k, String(v)); },
    removeItem: (k) => { _store.delete(k); },
};

/* _fire 的 DOM 出口桩：createElement("a").click() 即「交给浏览器」；
   不注册任何 zone → _renderZones 零迭代，不碰查询 DOM。 */
const _fired = [];
globalThis.document = {
    createElement: function () {
        const node = { href: "", download: "", target: "", rel: "" };
        node.click = function () { _fired.push(node.href); };
        return node;
    },
    body: { appendChild: function () {}, removeChild: function () {} },
    addEventListener: function () {},
};

/* fetch 桩：按 uid 回主文件清单（dlqEnqueueDatasets 逐 uid 取 /api/files）。
   usageLog 等其余 fetch 一律回空 ok，不干扰断言。 */
const _filesByUid = {
    u1: [{ filename: "a.fastq.gz", download_url: "https://x.example/a.fastq.gz", bytes: 10, is_primary: true }],
    u2: [{ filename: "b1.fastq.gz", download_url: "https://x.example/b1.fastq.gz", bytes: 20, is_primary: true },
         { filename: "b2.fastq.gz", download_url: "https://x.example/b2.fastq.gz", bytes: 30, is_primary: true }],
    ubad: [{ filename: "c.txt", download_url: "", bytes: 0 }],
};
globalThis.fetch = async function (url) {
    const m = /[?&]uid=([^&]+)/.exec(String(url));
    const uid = m ? decodeURIComponent(m[1]) : "";
    return { json: async () => ({ ok: true, files: _filesByUid[uid] || [] }) };
};

const ns = await import("../../web/static/js/core/downloads.js");
const { dlqEnqueueDatasets, dlqSnapshot } = ns;

let checks = 0;
function ok(cond, what) {
    checks += 1;
    if (!cond) { console.error("FAIL: " + what); process.exit(1); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
/* 用例间隔离 = 只等泵链打完（fired 记录留队——already 去重语义依赖它；
   dlqClearFinished 会把 fired 一并清掉，破坏后续 already 断言）。 */
async function settle() { await sleep(400); }

/* 单文件直下：res.queued 必须为 1（修复前 = 0 —— 队首被泵打成 fired 后漏数）。 */
let res = await dlqEnqueueDatasets(["u1"], { auto: true });
ok(res.queued === 1, "单文件直下 res.queued 应为 1，实得 " + res.queued);
ok(res.n_files === 1 && res.bytes === 10, "单文件 n_files/bytes 应为 1/10");
await settle();
ok(_fired.length === 1 && _fired[0].endsWith("a.fastq.gz"), "单文件应已被泵发射给浏览器");

/* 同 URL 重下：不重复占排、不算新发射，already 单列出数。 */
res = await dlqEnqueueDatasets(["u1"], { auto: true });
ok(res.queued === 0 && res.already === 1, "重复直下应 queued=0 / already=1，实得 " + res.queued + "/" + res.already);

/* 双主文件直下：res.queued 必须为 2（修复前 = 1）。 */
res = await dlqEnqueueDatasets(["u2"], { auto: true });
ok(res.queued === 2, "双文件直下 res.queued 应为 2，实得 " + res.queued);
await settle();
ok(_fired.length === 3, "至此应共发射 3 个文件，实得 " + _fired.length);

/* 无直链：unsupported 桶如实登记，不进 queued。 */
res = await dlqEnqueueDatasets(["ubad"], { auto: true });
ok(res.queued === 0 && res.unsupported.length === 1, "无直链应 queued=0 / unsupported=1");
ok(dlqSnapshot().some(function (q) { return q.status === "unsupported" && q.uid === "ubad"; }),
    "unsupported 条目必须留在队列面板可见");
await settle();

/* auto:false：留在排队态不发射，queued 照数（手动开始路径不受 fired 混算影响）。 */
res = await dlqEnqueueDatasets(["u1"], { auto: false });
ok(res.already === 1, "u1 的 URL 已在 fired 记录里，应计入 already");
ok(res.queued === 0 && _fired.length === 3, "auto:false 且全重复时不得新发射");

console.log("downloads_spec: PASS（" + checks + " 项断言）");
