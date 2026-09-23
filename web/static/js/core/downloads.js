"use strict";

/* ============================================================================
 * downloads.js —— 全应用唯一浏览器下载引擎（2026-08-30 dl-browser-queue）
 * ----------------------------------------------------------------------------
 * 单通道原则（用户 2026-08-30 定，AGENTS.md §2）：**一切浏览器下载只走这里**——
 * 数据文件直下、引文/投稿材料 blob、任务包 zip、追踪导出、反馈包，一视同仁。
 * 其它模块不许再手写 `<a download>` 发射样板或自带一份队列；要下载就调本模块。
 *
 * ## 为什么队列在浏览器侧
 *
 * fetch→blob 对几十 GB 的数据文件会爆内存，Service Worker 截流也救不了浏览器侧内存。
 * 所以数据文件的唯一可行通道是浏览器原生下载：同源走 `<a download>` 原地静默下载；
 * 跨域（download 属性被忽略）走 `window.open(url, "_blank")` 新标签——
 * 绝不劫持当前页（双通道与被拦检测详见 _fire 注释）。
 *
 * ## 诚实红线（这条比功能重要）
 *
 * 文件一旦经 `<a>` 交给浏览器，页面就**再也拿不到进度、也无法取消它**——
 * 进度/暂停/取消只能在浏览器自带的下载管理里做。因此：
 *   - fired 状态只许说「已交给浏览器」，绝不许说「已下载完成」；
 *   - 面板能取消的只有还没发射的 queued 项；
 *   - 脚注恒把这个边界说给用户听，不装成我们能管在途文件。
 *
 * ## 状态机
 *
 *   queued ──发射──▶ fired（url 类，「已交给浏览器」；浏览器可能因多文件授权拦截，
 *   │                 拦截后可用行内「重下」或「继续下载」在用户手势里重发）
 *   │──▶ done（blob 类：downloadBlobAs 同步成功即完成）
 *   ├─▶ cancelled（仅 queued 可取消）
 *   ├─▶ error（blob 发射抛错 / 清单拉取失败）
 *   └─▶ unsupported（无直链 / 巡检标记存疑——逐条带原因，绝不混进成功数）
 *
 * 发射错峰 150ms 单 timer 链：一次入队 50 个文件也不会同帧点 50 个锚。
 *
 * ## import 方向（tests/test_frontend_import_graph.py 看守，本模块不许入环）
 *
 *   downloads → #core（$/escapeHtml/toast/downloadBlobAs/API/isHttp）+ #act_core（tpBytes）
 *   + #usage_core/#usage_log（数据直下在「真的交给浏览器」语义点记一条 dl/what:"data"，
 *     与旧服务端代下「真启动才记」同纪律；blob 类的打点留在各自调用方，不进本模块）。
 *   #core / #act_core 绝不 import 本模块；task_pack / act / reuse_pack / project_exports /
 *   benchfb / dataset_page → downloads。面板回调（unsupported→生成任务包）经 dlqBind 注入，
 *   不是 downloads 反向 import task_pack。
 * ========================================================================== */

import { $, API, downloadBlobAs, escapeHtml, isHttp, toast } from "#core";
import { tpBytes } from "#act_core";
import { USAGE_KINDS } from "#usage_core";
import { usageLog } from "#usage_log";
import { COPY } from "./copy.js";

/* 队列条目的类别。kind 决定面板上的小签与（数据类之外的）来源区分；
   新类别只许加在这里——面板/遥测都按这张表取文案。 */
export const DLQ_KINDS = { data: "数据文件", cite: "引文", pack: "任务包", reuse: "投稿材料", export: "导出", other: "下载" };

/* 状态文案单一真源（面板行 + 测试锚钉共用）。fired 的措辞就是诚实红线本身，改它=改合同。 */
export const DLQ_STATUS_LABELS = {
    queued: "排队中",
    fired: "已交给浏览器",
    done: "已下载",
    cancelled: "已取消",
    error: "失败",
    unsupported: "暂不支持",
};

const FIRE_GAP_MS = 150;   // 相邻两次发射的最小间隔（浏览器多文件授权也靠这个节奏消化）

/* 下载文件名取自响应头 Content-Disposition 的唯一实现（task_pack / project_exports 等
   各下载出口共用）；取不到 → ""（调用方给各自缺省名）。 */
export function dlqFilenameFrom(disposition) {
    const match = /filename="([^"]+)"/.exec(String(disposition || ""));
    return match ? match[1] : "";
}

let _seq = 0;              // 条目 id / 排序共用单调计数
const _queue = [];         // 全量记录（含终态），先入先出序
const _zones = new Map();  // zoneId -> { onPackForUnsupported }（面板宿主注册表）
let _pumpTimer = null;     // 发射链句柄（单链，绝不并发第二条）
let _inited = false;       // initDownloads 幂等闸

function _mkItem(raw) {
    _seq += 1;
    return {
        id: "dlq" + _seq,
        kind: DLQ_KINDS[raw.kind] ? raw.kind : "other",
        name: String(raw.name || raw.title || COPY.common.unnamedDataset),
        url: String(raw.url || ""),
        bytes: Number(raw.bytes) || 0,
        uid: String(raw.uid || ""),
        title: String(raw.title || ""),
        status: raw.status || "queued",
        note: String(raw.note || ""),
        ts: _seq,
    };
}

function _nameFromUrl(url) {
    try {
        const path = new URL(String(url)).pathname;
        const last = decodeURIComponent(path.split("/").filter(Boolean).pop() || "");
        return last || "下载文件";
    } catch (_e) { return "下载文件"; }
}

/* blob 缺省分类：引文按后缀（.bib/.ris），zip 归任务包/导出（调用方一般都显式传 kind，
   这里只是兜底），其余归 other。 */
function _blobKind(name) {
    if (/\.(bib|ris)$/i.test(String(name || ""))) return "cite";
    if (/\.zip$/i.test(String(name || ""))) return "pack";
    return "other";
}

/* 同 URL 已在排队或已交给浏览器：不重复占排、不重复发射。
   重复点「下载」不该把 5.5GB 的主文件再下一遍；fired 想重下走行内「重下」（dlqRetryItem）。 */
function _activeSameUrl(url) {
    return _queue.some(function (q) {
        return q.url === url && (q.status === "queued" || q.status === "fired");
    });
}

/* ---------------- 发射 ---------------- */

/* url 类条目的唯一发射点。两条通道按 origin 分（2026-09-12 探针实测定稿）：
   - 同源（/api 代理、静态资源）：临时锚点 + download 属性，原地静默下载，不开标签；
   - 跨域：window.open(url, "_blank")——跨域锚点若服务端没回 Content-Disposition 会
     **导航**顶掉当前页，必须另开标签；新建标签才可能触发弹窗拦截，被拦时 window.open
     返回 null（标准行为，可如实检测）。
   不用 noopener 特性：它使 window.open 恒返回 null（2026-09-12 实测），「被拦」与
   「成功」无法区分；改用手动 w.opener=null 防 tabnabbing。也不用具名复用：复用已注册
   窗口时同样返回 null（实测），null 语义会被污染。
   返回 false = 被浏览器拦截（条目留 queued，等用户手势经 dlqResume 重发）。 */
function _fire(item) {
    let sameOrigin = false;
    try { sameOrigin = new URL(item.url, window.location.href).origin === window.location.origin; } catch (_e) { /* 非法 url 走跨域通道兜底 */ }
    if (sameOrigin) {
        const a = document.createElement("a");
        a.href = item.url;
        a.download = item.name || "";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        item.status = "fired";
        return true;
    }
    const w = window.open(item.url, "_blank");
    if (!w) return false;
    try { w.opener = null; } catch (_e) { /* 防 tabnabbing；置 opener 失败不挡下载 */ }
    item.status = "fired";
    return true;
}

/* 单链错峰泵：每次只发射队首一个 queued（有 url 的），150ms 后再看下一个。
   不入队不清链——enqueue 追加的条目会被在途的链自然带走。 */
function _pump() {
    if (_pumpTimer) return;
    for (let i = 0; i < _queue.length; i += 1) {
        if (_queue[i].status === "queued" && _queue[i].url) {
            if (!_fire(_queue[i])) {
                /* 被拦（无用户激活的自动发射，如 chat 异步回执）：留队列 + sticky 提醒
                   + 断链。面板「开始下载/继续下载」按钮是真实手势，经 dlqResume 重发时
                   5s 激活窗内错峰链可逐发通过。 */
                toast(COPY.common.popupBlocked, { sticky: true });
                _renderZones();
                return;
            }
            break;
        }
    }
    _renderZones();
    const more = _queue.some(function (q) { return q.status === "queued" && q.url; });
    if (more) {
        _pumpTimer = setTimeout(function () { _pumpTimer = null; _pump(); }, FIRE_GAP_MS);
    }
}

/* 有 queued 就启动/重启发射链；浏览器拦了多文件下载时，本函数也是「继续下载」按钮的语义。 */
export function dlqResume() { _pump(); }

/* ---------------- 入队 ---------------- */

/* 通用入口：items = [{kind?, name?, url?, bytes?, uid?, title?, note?}]。
   无 url 的条目直接落 unsupported（note 带原因），与 queued 同批入队列——
   「这部分下不了」必须和「这部分在排队」出现在同一个面板里，不许静默丢。
   auto 缺省 true：入队即发射；auto:false 留在排队态等面板「开始下载」。 */
export function dlqEnqueue(items, opts) {
    opts = opts || {};
    const added = [];
    (Array.isArray(items) ? items : []).forEach(function (raw) {
        if (!raw) return;
        if (!isHttp(raw.url || "")) {
            /* 同 uid 的 unsupported/error 记录已在队列：不重复登记（重复点「下载」
               不该让「暂不支持」行成倍增长）。 */
            const kind = DLQ_KINDS[raw.kind] ? raw.kind : "other";
            if (raw.uid && _queue.some(function (q) {
                return q.uid === String(raw.uid) && q.kind === kind
                    && (q.status === "unsupported" || q.status === "error");
            })) return;
            const it = _mkItem(Object.assign({}, raw, {
                status: raw.status === "error" ? "error" : "unsupported",
                note: raw.note || "没有可用的下载直链",
            }));
            _queue.push(it); added.push(it);
            return;
        }
        if (_activeSameUrl(raw.url)) return;   // 同 URL 已在排队/已交给浏览器：不重复占排
        const it = _mkItem(raw);
        _queue.push(it); added.push(it);
    });
    _renderZones();
    if (opts.auto !== false && added.some(function (q) { return q.status === "queued"; })) _pump();
    return added;
}

/* blob 类（引文/任务包/导出/反馈包/投稿材料）的唯一出口：包一层 downloadBlobAs（core 里
   的唯一基元不动），补一条队列记录。返回 true/false 与旧 downloadTextBlob 语义一致，
   调用方的 toast/usageLog 纪律原样保留在调用方。 */
export function dlqFireBlob(name, blob, opts) {
    const kind = (opts && opts.kind) || _blobKind(name);
    try {
        downloadBlobAs(blob, name);
        _queue.push(_mkItem({ kind: kind, name: name, bytes: (blob && blob.size) || 0, status: "done" }));
        _renderZones();
        return true;
    } catch (err) {
        _queue.push(_mkItem({
            kind: kind, name: name, bytes: (blob && blob.size) || 0,
            status: "error", note: String((err && err.message) || err),
        }));
        _renderZones();
        return false;
    }
}

/* 数据集直下入口（act.js pack.download / 面板「下载勾选的数据」共用）：
   逐 uid 取 /api/files，**只取 is_primary 主文件**（与旧服务端代下同口径——
   旧 download_plan 也只下主文件）；无 primary 标记时取第一个有直链的文件。
   巡检标了 problem 的主文件不下（旧服务端代下也会跳过），落 unsupported 并带原因。
   返回汇总给调用方写回执：{ok, n_datasets, n_files, bytes, unsupported[], failed[]}。 */
export async function dlqEnqueueDatasets(uids, opts) {
    opts = opts || {};
    const list = Array.isArray(uids) ? uids.slice() : [];
    const res = { ok: true, n_datasets: list.length, n_files: 0, bytes: 0, already: 0, unsupported: [], failed: [] };
    if (!list.length) { res.ok = false; res.error = "没有选中任何数据集"; return res; }
    const rows = await Promise.all(list.map(async function (uid) {
        try {
            const d = await (await fetch(API.files + "?uid=" + encodeURIComponent(uid))).json();
            return { uid: uid, ok: !!(d && d.ok), files: (d && Array.isArray(d.files)) ? d.files : [] };
        } catch (err) {
            return { uid: uid, ok: false, files: [], error: String((err && err.message) || err) };
        }
    }));
    const items = [];
    rows.forEach(function (r) {
        const title = String((r.files[0] && (r.files[0].dataset_title || r.files[0].title)) || r.uid);
        if (!r.ok) {
            const reason = "没能取到文件清单" + (r.error ? "：" + r.error : "");
            res.failed.push({ uid: r.uid, title: title, reason: reason });
            items.push({ kind: "data", uid: r.uid, title: title, name: title, status: "error", note: reason });
            return;
        }
        const withUrl = r.files.filter(function (f) { return f && isHttp(f.download_url || ""); });
        let pick = withUrl.filter(function (f) { return f.is_primary; });
        if (!pick.length && withUrl.length) pick = withUrl.slice(0, 1);
        const clean = pick.filter(function (f) { return !f.problem; });
        const flagged = pick.filter(function (f) { return f.problem; });
        if (!clean.length) {
            const reason = flagged.length
                ? (String(flagged[0].problem_reason || "最近核验发现该文件有完整性问题"))
                : "没有可直下的主文件";
            res.unsupported.push({ uid: r.uid, title: title, reason: reason });
            items.push({ kind: "data", uid: r.uid, title: title, name: title, status: "unsupported", note: reason });
            return;
        }
        clean.forEach(function (f) {
            res.n_files += 1;
            res.bytes += Number(f.bytes) || 0;
            items.push({
                kind: "data", uid: r.uid, title: title,
                name: String(f.filename || f.title || title),
                url: f.download_url, bytes: Number(f.bytes) || 0,
            });
        });
    });
    /* 去重计数：同 URL 已在排队/已交给浏览器的不进队列、不算新发射，单列 res.already——
       调用方据此说「这批已在下载中，没有重复下载」，而不是误判成「零直下」降级打包。 */
    const fresh = [];
    items.forEach(function (it) {
        if (it.url && !it.status && _activeSameUrl(it.url)) { res.already += 1; return; }
        fresh.push(it);
    });
    const added = dlqEnqueue(fresh, { auto: opts.auto });
    /* auto:true 时 dlqEnqueue 内已同步泵出队首（本批第一个 queued→fired），统计必须含 fired——
       只数 queued 会把单文件直下错算成 0：调用方误判「零直下」走降级打包分支、遥测漏记
       （2026-08-31 PR#7 自动评审实锤）。unsupported/error 条目无 url，天然排除在外。 */
    res.queued = added.filter(function (q) { return q.url && (q.status === "queued" || q.status === "fired"); }).length;
    /* 遥测语义点 = 「数据文件真的开始交给浏览器」（与旧服务端代下「真启动才记」同纪律）。
       只在这一个出口记：面板主按钮 / act.js 动词 / 未来任何调用方都经本函数，单通道不单漏。
       blob 类（引文/任务包/导出）的打点留在各自调用方（它们有产物自证，语义点不同）。 */
    if (res.queued) usageLog(USAGE_KINDS.dl, { what: "data", n: res.queued });
    return res;
}

/* ---------------- 队列操作 ---------------- */

export function dlqCancelItem(id) {
    const it = _queue.find(function (q) { return q.id === id; });
    if (!it || it.status !== "queued") return false;
    it.status = "cancelled";
    _renderZones();
    return true;
}

/* 取消全部还在排队的（已交给浏览器的不归我们管，面板文案如实说）。返回取消条数。 */
export function dlqCancelQueued() {
    let n = 0;
    _queue.forEach(function (q) { if (q.status === "queued") { q.status = "cancelled"; n += 1; } });
    _renderZones();
    return n;
}

/* fired/error/cancelled 且有 url → 回 queued 重发（浏览器拦了多文件下载时的补救）。 */
export function dlqRetryItem(id) {
    const it = _queue.find(function (q) { return q.id === id; });
    if (!it || !it.url || it.status === "queued") return false;
    it.status = "queued";
    it.note = "";
    _renderZones();
    _pump();
    return true;
}

/* 清掉一切非排队记录（fired/done/cancelled/error/unsupported）；queued 不受影响。 */
export function dlqClearFinished() {
    for (let i = _queue.length - 1; i >= 0; i -= 1) {
        if (_queue[i].status !== "queued") _queue.splice(i, 1);
    }
    _renderZones();
}

/* 只读快照（测试锚钉 / act.js 回执用；绝不暴露内部数组引用）。 */
export function dlqSnapshot() {
    return _queue.map(function (q) { return Object.assign({}, q); });
}

/* ---------------- 面板 ---------------- */

function _rowHtml(q) {
    const kind = DLQ_KINDS[q.kind] || DLQ_KINDS.other;
    const size = q.bytes ? tpBytes(q.bytes) : "";
    let status = DLQ_STATUS_LABELS[q.status] || q.status;
    if (q.note && (q.status === "error" || q.status === "unsupported")) status += "：" + q.note;
    let act = "";
    if (q.status === "queued") {
        act = '<button type="button" class="dlq-x" data-dlq-action="cancel" data-dlq-id="' + q.id + '">取消</button>';
    } else if (q.url && q.status !== "unsupported") {
        act = '<button type="button" class="dlq-x" data-dlq-action="retry" data-dlq-id="' + q.id + '">重下</button>';
    }
    return '<li class="dlq-row dlq-' + escapeHtml(q.status) + '">'
        + '<span class="dlq-kind">' + escapeHtml(kind) + '</span>'
        + '<span class="dlq-name" title="' + escapeHtml(q.title && q.title !== q.name ? q.title + " · " + q.name : q.name) + '">'
        + escapeHtml(q.name) + '</span>'
        + (size ? '<span class="dlq-size">' + escapeHtml(size) + '</span>' : "")
        + '<span class="dlq-status">' + escapeHtml(status) + '</span>'
        + act + "</li>";
}

function _dlqHtml(info) {
    if (!_queue.length) return "";
    const queued = _queue.filter(function (q) { return q.status === "queued"; });
    const finished = _queue.length - queued.length;
    const unsData = _queue.filter(function (q) { return q.status === "unsupported" && q.kind === "data"; });
    let html = '<div class="dlq"><div class="dlq-head"><strong>下载队列</strong>'
        + '<span class="dlq-count">' + _queue.length + '</span>'
        + '<span class="dlq-head-acts">';
    if (queued.length) {
        html += '<button type="button" class="btn btn-primary dlq-btn" data-dlq-action="resume">'
            + (_queue.some(function (q) { return q.status === "fired"; }) ? "继续下载" : "开始下载")
            + "（" + queued.length + " 个排队中）</button>"
            + '<button type="button" class="btn dlq-btn" data-dlq-action="cancel-queued">取消排队</button>';
    }
    if (finished) {
        html += '<button type="button" class="btn dlq-btn" data-dlq-action="clear">清除记录</button>';
    }
    html += "</span></div>";
    html += '<ul class="dlq-list">' + _queue.map(_rowHtml).join("") + "</ul>";
    if (_queue.some(function (q) { return q.status === "fired"; })) {
        html += '<p class="dlq-foot">「已交给浏览器」的文件，进度与取消在浏览器自带的下载管理里（Ctrl+J）；'
            + "本面板能取消的是还没开始的排队项。若浏览器询问「是否允许下载多个文件」，请选允许；"
            + escapeHtml(COPY.common.popupBlocked)
            + "被拦下的条目点行内「重下」即可重发。</p>";
    }
    if (unsData.length) {
        html += '<p class="dlq-uns">' + unsData.length + " 个数据集暂不支持浏览器直下："
            + unsData.map(function (q) { return escapeHtml(q.title || q.name); }).join("、") + "</p>"
            + '<details class="dlq-uns-why"><summary>为什么这 ' + unsData.length + " 个下不了</summary><ul>"
            + unsData.map(function (q) { return "<li>" + escapeHtml(q.title || q.name) + "：" + escapeHtml(q.note || "未知原因") + "</li>"; }).join("")
            + "</ul></details>";
        if (info && typeof info.onPackForUnsupported === "function") {
            html += '<div class="dlq-acts"><button type="button" class="btn dlq-btn" data-dlq-action="pack-unsupported">'
                + "为这部分生成任务包（" + unsData.length + " 个）</button></div>";
        }
    }
    return html + "</div>";
}

function _renderZones() {
    _zones.forEach(function (info, id) {
        const el = $(id);
        if (el) el.innerHTML = _dlqHtml(info);
    });
}

/* 渲染出口：task_pack 面板的 #tpDlZone 每次重渲后调它重画（zone 元素会被 panel innerHTML
   重建，注册表按 id 存活，重建后第一次 dlqRender 即复活）。 */
export function dlqRender(zone) {
    if (!zone || !zone.id) return;
    if (!_zones.has(zone.id)) _zones.set(zone.id, {});
    zone.innerHTML = _dlqHtml(_zones.get(zone.id));
}

/* 宿主注册回调：onPackForUnsupported(uids) 由 task_pack 注入（窄化勾选 + buildTaskPack），
   保持 downloads 不反向 import。 */
export function dlqBind(zoneId, opts) {
    _zones.set(zoneId, Object.assign({}, _zones.get(zoneId) || {}, opts || {}));
    const el = $(zoneId);
    if (el) el.innerHTML = _dlqHtml(_zones.get(zoneId));
}

function _unsupportedDataUids() {
    return _queue.filter(function (q) { return q.status === "unsupported" && q.kind === "data" && q.uid; })
        .map(function (q) { return q.uid; });
}

function _firstPackCb() {
    let cb = null;
    _zones.forEach(function (info) { if (!cb && info && typeof info.onPackForUnsupported === "function") cb = info.onPackForUnsupported; });
    return cb;
}

/* ---------------- 全局委托（幂等） ---------------- */

/* 两类点击全部收口在这里：
   1. `a[data-dlq]`：卡片 CTA / 介绍行 / 文件弹窗 / 数据集页页头的直链——拦截默认行为，
      改走队列（即刻发射 + 留痕），全站不再有别的地方发射 url 下载。
   2. `[data-dlq-action]`：面板按钮（cancel/retry/resume/cancel-queued/clear/pack-unsupported）。
   document 级委托 = 面板/弹窗 innerHTML 重建多少次都不掉线。 */
export function initDownloads() {
    if (_inited) return;
    _inited = true;
    document.addEventListener("click", function (ev) {
        const t = ev.target;
        const anchor = (t && t.closest) ? t.closest("a[data-dlq]") : null;
        if (anchor) {
            const href = anchor.href || "";
            if (isHttp(href)) {
                ev.preventDefault();
                const name = anchor.getAttribute("data-dlq-name") || _nameFromUrl(href);
                const added = dlqEnqueue([{
                    kind: anchor.getAttribute("data-dlq") || "data",
                    url: href, name: name,
                    uid: anchor.getAttribute("data-dlq-uid") || "",
                    title: anchor.getAttribute("data-dlq-title") || name,
                }], { auto: true });
                /* 判定必须含 fired：dlqEnqueue(auto:true) 内已同步泵出队首（单文件点下载时
                   唯一条目已被发成 fired）——只数 queued 会把**首次成功下载**误判进
                   「没有重复下载」分支，拦截提醒 sticky toast 永不可达（与 2026-08-31
                   dlqEnqueueDatasets 修正同类，2026-09-12 用户实测实锤）。 */
                if (added.some(function (q) { return q.url && (q.status === "queued" || q.status === "fired"); })) {
                    toast("已开始下载：" + name + "（记录见下载面板）。" + COPY.common.popupBlocked, { sticky: true });
                } else {
                    toast("「" + name + "」已在下载队列里或已交给浏览器，没有重复下载；要重下请到下载面板点「重下」。");
                }
            }
            return;
        }
        const btn = (t && t.closest) ? t.closest("[data-dlq-action]") : null;
        if (!btn) return;
        const act = btn.getAttribute("data-dlq-action");
        if (act === "cancel") dlqCancelItem(btn.getAttribute("data-dlq-id"));
        else if (act === "retry") dlqRetryItem(btn.getAttribute("data-dlq-id"));
        else if (act === "resume") dlqResume();
        else if (act === "cancel-queued") dlqCancelQueued();
        else if (act === "clear") dlqClearFinished();
        else if (act === "pack-unsupported") {
            const cb = _firstPackCb();
            if (cb) cb(_unsupportedDataUids());
        }
    });
}
