"use strict";

/* 数据集页一键同步 · 薄 DOM 壳（对应设计 v2 §7「F4 数据集页一键同步」）
 *
 * 纯逻辑在 sync_button_core.js（零 DOM，node 可单测）；本文件只管 DOM 与网络：
 *   - 空闲态：GET /api/curate/sync-status 显示「上次同步：X 天前」（实例级事实；从未同步如实写）
 *   - 进行中：防重入禁用按钮 + 进度文字，无取消按钮（同步 HTTP 不可取消）
 *   - 结果摘要：新增 X / 已存在 Z / 失败 W（失败项逐条列原因；绝不写「更新 Y」）
 *   - sync_busy：另一同步进行中（agent 可能在对话里发起）→「另一个同步任务进行中，请稍候」
 *   - 一键撤销：按 operation_id 调 /api/curate/recall，结果如实呈现
 *   - 2026-08-26 起：网页形态（guard on）下 sync-updates 改异步——返回
 *     {async:true, job}，本壳轮询 GET /api/curate/sync-updates/status 到终态再呈现回执；
 *     本机形态响应逐字节不变（同步返回 result）。「并检查 N 个追踪的更新」
 *     联动已随全体批量按钮一并退役。
 *
 * 本页加载序与 dataset_page.js 同源（dataset.html 只加载自身模块子集）：自挂 DOMContentLoaded，
 * 不经 boot（boot 是 index.html 入口）。后端三端点契约见 webapp.py:2883-2926。
 */

import { API, $, escapeHtml } from "#core";
import { USAGE_KINDS } from "#usage_core";
import { usageLog } from "#usage_log";
import { SYNC_BUSY_COPY, SYNC_RUNNING_COPY, SYNC_SUB_COPY,
    syncClassifyError, syncFailureLines, syncLastSyncedText,
    syncReceipt, syncReceiptText, syncRecallResult, syncRecallText } from "#sync_button_core";

/* ---------- 模块内状态（本模块唯一有状态区） ---------- */

let _lastOpId = null;        // 本次会话最近一次 sync 的 operation_id（撤销依据；刷新即失，结果摘要不跨页持久）
let _busyTimer = null;       // busy 态轮询 timer（另一个同步任务可能在 chat 里发起，轮询等到它结束）
let _busyTries = 0;          // 已轮询次数
const BUSY_POLL_MS = 5000;   // busy 态轮询间隔
const BUSY_POLL_MAX = 24;    // 上限 ≈ 2 分钟；到点如实回到空闲（再点会拿到最新的 sync_busy）

/* ---------- 渲染（每次整块重渲 #dsSync；状态简单，无细粒度更新） ---------- */

function _runBtnHtml(disabled) {
    return '<div class="ds-sync-row">'
        + '<button class="cta cta-soft ds-sync-run" type="button"' + (disabled ? " disabled" : "") + ">同步数据集</button>"
        + '<span class="ds-sync-sub">' + escapeHtml(SYNC_SUB_COPY) + "</span>"
        + "</div>";
}

function _statusLine(text, cls) {
    return '<div class="ds-sync-status' + (cls ? " " + cls : "") + '" role="status">' + escapeHtml(text) + "</div>";
}

/* 空闲态（按钮可用 + 上次同步时间；busy=false 时后端 last_sync_at 才可信） */
function _renderIdle(status) {
    _clearBusyPoll();
    const node = $("dsSync");
    if (!node) return;
    const text = syncLastSyncedText(status && status.last_sync_at);
    node.innerHTML = _runBtnHtml(false) + _statusLine(text);
    node.querySelector(".ds-sync-run").addEventListener("click", _startSync);
}

/* 进行中（防重入：禁用按钮 + 进度文字；无取消按钮——同步不可取消，不提供假出口） */
function _renderRunning() {
    const node = $("dsSync");
    if (!node) return;
    node.innerHTML = _runBtnHtml(true) + _statusLine(SYNC_RUNNING_COPY, "ds-sync-busy");
}

/* busy 冲突（另一同步任务进行中，可能是 agent 在对话里发起的）：显示设计 §7 固定文案，
   禁用按钮防重复撞锁，并轻量轮询等到它结束（到点上限如实回到空闲）。 */
function _renderBusy(detail) {
    const node = $("dsSync");
    if (!node) return;
    let html = _runBtnHtml(true) + _statusLine(SYNC_BUSY_COPY, "ds-sync-busy");
    if (detail) html += '<div class="ds-sync-detail">' + escapeHtml(detail) + "</div>";
    node.innerHTML = html;
    _startBusyPoll();
}

/* 一般失败（网络/非 sync_busy 的 HTTP 错误）：如实展示原因，按钮恢复可用可重试 */
function _renderError(message) {
    const node = $("dsSync");
    if (!node) return;
    node.innerHTML = _runBtnHtml(false) + _statusLine(message, "ds-sync-error");
    const btn = node.querySelector(".ds-sync-run");
    if (btn) btn.addEventListener("click", _startSync);
}

/* 结果摘要（设计 §7）：新增 X / 已存在 Z / 失败 W；失败项逐条列原因；created_files 非空才给
   「撤销本次同步」（没有可撤的东西就不给假按钮）。 */
function _renderResult(result) {
    _clearBusyPoll();
    const node = $("dsSync");
    if (!node) return;
    const s = syncReceipt(result);
    const fails = syncFailureLines(result);
    const createdCount = (result && Array.isArray(result.created_files)) ? result.created_files.length : 0;
    _lastOpId = String((result && result.operation_id) || "");

    let html = _runBtnHtml(false)
        + _statusLine(syncLastSyncedText(result && result.checked_at), "ds-sync-ok")
        + '<div class="ds-sync-result">'
        + '<div class="ds-sync-summary">' + escapeHtml(syncReceiptText(s)) + "</div>";
    if (fails.lines.length) {
        html += '<ul class="ds-sync-fails">'
            + fails.lines.map(function (f) {
                return "<li><b>" + escapeHtml(f.label) + "</b>：" + escapeHtml(f.reason) + "</li>";
            }).join("")
            + "</ul>";
        if (fails.more > 0) html += '<div class="ds-sync-more">另有 ' + fails.more + " 个来源失败</div>";
    }
    html += '<div class="ds-sync-acts">';
    if (createdCount > 0 && _lastOpId) {
        html += '<button class="btn ds-sync-recall" type="button">撤销本次同步</button>';
    }
    // 2026-08-26：「并检查 N 个追踪的更新」联动已撤
    // （全体批量按钮退役，用户：点一下太耗费资源；批量诉求由登录后语料代哨兵自动刷新承接）。
    html += "</div></div>";
    node.innerHTML = html;

    node.querySelector(".ds-sync-run").addEventListener("click", _startSync);
    const recall = node.querySelector(".ds-sync-recall");
    if (recall) recall.addEventListener("click", _recall);

    // 埋点（计数型无文本，usage_log 通道；设计 §10）：sync_button_used{added,skipped,failed}。
    // 关闭遥测时 usageLog 第一行即返回，静默少记——打点绝不把主功能带崩（usage_log.js 纪律）。
    try { usageLog(USAGE_KINDS.sync_button_used, { added: s.added, skipped: s.skipped, failed: s.failed }); } catch (_e) {}
}

/* 撤销结果（设计 §7「结果如实呈现」）：显示撤销摘要；随后只原位刷新「上次同步」行——
   不能用 _loadStatus() 整块重渲（会把撤销结果瞬间抹成空闲态，用户看不到撤掉了什么）。 */
function _renderRecall(recall) {
    const node = $("dsSync");
    if (!node) return;
    const s = syncRecallResult(recall);
    let html = _runBtnHtml(false) + _statusLine(syncRecallText(s), "ds-sync-ok");
    if (recall && recall.hint_zh) html += '<div class="ds-sync-detail">' + escapeHtml(recall.hint_zh) + "</div>";
    node.innerHTML = html;
    const btn = node.querySelector(".ds-sync-run");
    if (btn) btn.addEventListener("click", _startSync);
    _refreshLastSyncedLine();   // 撤销后刷新状态显示（设计 §7）
}

/* 只更新当前 DOM 里的「上次同步」行文本（busy 时不动——另一任务进行中，行不抢跑） */
async function _refreshLastSyncedLine() {
    const node = $("dsSync");
    if (!node) return;
    try {
        const res = await fetch(API.curateSyncStatus);
        const j = await res.json();
        if (res.ok && j && j.ok && j.result && j.result.busy !== true) {
            const line = node.querySelector(".ds-sync-status");
            if (line) line.textContent = syncLastSyncedText(j.result.last_sync_at);
        }
    } catch (_e) { /* 状态行刷新失败静默——撤销结果已如实呈现，不额外打扰 */ }
}

/* ---------- 网络 ---------- */

/* 读取实例级同步状态（只读、不写盘；「上次同步」是实例级事实，不落 per-profile localStorage） */
async function _loadStatus() {
    const node = $("dsSync");
    if (!node) return;
    try {
        const res = await fetch(API.curateSyncStatus);
        const j = await res.json();
        if (!res.ok || !j || !j.ok || !j.result) throw new Error((j && j.detail) || "同步状态不可用");
        const status = j.result;
        if (status.busy === true) { _renderBusy(); return; }
        _renderIdle(status);
    } catch (_e) {
        // 状态端点失败：按钮仍可用（点同步会给出真实错误），状态行如实说明读不到
        node.innerHTML = _runBtnHtml(false) + _statusLine("同步状态读取失败，可点「同步数据集」重试", "ds-sync-error");
        const btn = node.querySelector(".ds-sync-run");
        if (btn) btn.addEventListener("click", _startSync);
    }
}

async function _startSync() {
    const node = $("dsSync");
    if (!node) return;
    _renderRunning();
    try {
        const res = await fetch(API.curateSyncUpdates, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({}),   // sources 缺省 = 全部已注册来源（后端契约）
        });
        const j = await res.json().catch(function () { return null; });
        if (res.ok && j && j.ok && j.result) { _renderResult(j.result); return; }
        // 2026-08-26 起：网页形态异步响应（{async:true, job}）→ 轮询 job 到终态再呈现
        if (res.ok && j && j.ok && j.async === true) { _pollSyncJob(); return; }
        // 失败分类：400 + detail 含「同步」= sync_busy（另一任务进行中）；其余如实报
        const cls = syncClassifyError(res.status, j, null);
        if (cls.kind === "sync_busy") { _renderBusy(cls.detail); return; }
        _renderError(cls.message);
    } catch (e) {
        _renderError(syncClassifyError(0, null, e).message);
    }
}

/* 异步 job 轮询（网页形态）：GET /api/curate/sync-updates/status 到
   done/failed 再呈现回执/错误；上限约 20 分钟（job 含向量重建，分钟级正常），到点如实提示。 */
const JOB_POLL_MS = 3000;
const JOB_POLL_MAX = 400;
let _jobPollTimer = null;
let _jobPollTries = 0;
function _pollSyncJob() {
    if (_jobPollTimer) { clearInterval(_jobPollTimer); _jobPollTimer = null; }
    _jobPollTries = 0;
    _jobPollTimer = setInterval(async function () {
        _jobPollTries += 1;
        if (_jobPollTries > JOB_POLL_MAX) {
            _clearJobPoll();
            _renderError("同步仍在后台进行，请稍后重新打开本页查看结果");
            return;
        }
        try {
            const res = await fetch(API.curateSyncJobStatus);
            const j = await res.json();
            const job = (res.ok && j && j.ok && j.job) ? j.job : null;
            if (!job || job.status === "running" || job.status === "idle") return;
            _clearJobPoll();
            if (job.status === "done" && job.result) { _renderResult(job.result); return; }
            const err = String((job && job.error) || "未知错误");
            if (err.indexOf("同步") !== -1) { _renderBusy(err); return; }
            _renderError("同步失败：" + err);
        } catch (_e) { /* 单次轮询失败静默，下轮再试（只读端点） */ }
    }, JOB_POLL_MS);
}
function _clearJobPoll() {
    if (_jobPollTimer) { clearInterval(_jobPollTimer); _jobPollTimer = null; }
    _jobPollTries = 0;
}

async function _recall() {
    const node = $("dsSync");
    if (!node || !_lastOpId) return;
    const opId = _lastOpId;
    const btn = node.querySelector(".ds-sync-recall");
    if (btn) { btn.disabled = true; btn.textContent = "正在撤销…"; }
    try {
        const res = await fetch(API.curateRecall, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ operation_id: opId }),
        });
        const j = await res.json().catch(function () { return null; });
        if (res.ok && j && j.ok && j.result) { _renderRecall(j.result); return; }
        // 撤不回（如 unknown_operation 400）：如实报后端原因，按钮恢复
        const detail = (j && typeof j.detail === "string") ? j.detail : "撤销失败，请重试";
        if (btn) { btn.disabled = false; btn.textContent = "撤销本次同步"; }
        const status = node.querySelector(".ds-sync-status");
        if (status) status.textContent = "撤销失败：" + detail;
    } catch (e) {
        if (btn) { btn.disabled = false; btn.textContent = "撤销本次同步"; }
        const status = node.querySelector(".ds-sync-status");
        if (status) status.textContent = "撤销失败：网络请求失败，请重试";
    }
}

/* ---------- busy 轮询（另一个同步任务可能在对话里发起；轻量只读轮询，到点上限如实回到空闲） ---------- */

function _startBusyPoll() {
    _clearBusyPoll();
    _busyTries = 0;
    _busyTimer = setInterval(async function () {
        _busyTries += 1;
        // 到点上限：重新读状态（如实回到最新事实；直接 renderIdle(null) 会把「从未同步」错当成
        // 忙等超时的结论——上次同步过就还是显示上次同步，不虚构）
        if (_busyTries > BUSY_POLL_MAX) { _loadStatus(); return; }
        try {
            const res = await fetch(API.curateSyncStatus);
            const j = await res.json();
            if (res.ok && j && j.ok && j.result && j.result.busy !== true) {
                _renderIdle(j.result);
            }
        } catch (_e) { /* 单次轮询失败静默，下轮再试（只读端点） */ }
    }, BUSY_POLL_MS);
}

function _clearBusyPoll() {
    if (_busyTimer) { clearInterval(_busyTimer); _busyTimer = null; }
    _busyTries = 0;
}

/* ---------- 初始化（dataset.html 自挂 DOMContentLoaded，与 dataset_page.js 同源；挂点不存在即静默） ---------- */

function initSyncButton() {
    if (!$("dsSync")) return;
    _loadStatus();
}

document.addEventListener("DOMContentLoaded", initSyncButton);
