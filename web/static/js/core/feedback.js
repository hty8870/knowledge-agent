"use strict";

/* 意见反馈对话框 · UI 壳（2026-08-22；纯逻辑在 feedback_dialog_core.js，
 * 队列/遮蔽/加密在 feedback_core.js，唯一出网通道 usage_upload.js 的 sendFeedback()）
 *
 * ## 落点与加载
 * - 设置抽屉「使用反馈」卡片新增「向开发者发送意见」按钮（#feedbackSendEntryBtn）→ 本对话框
 *   （#feedbackModal，复用 benchfbModal 同款 modal 组件语言）。
 * - 本文件经 index.html 的 <script type="module"> 加载并**自接线**（不进 boot）：boot 的
 *   `#` 静态 import 会牵动两页 importmap 与 parity 门，而 dataset.html 归 F2 并行包——
 *   与 usage_upload.js / feedback_core.js 的「不进静态图」同哲学（见 usage_log.js 头部注释）。
 *   usage_upload.js 同样只**动态 import**（它的既有红线：静态 import 会牵动两页 importmap）。
 *
 * ## 授权语义（设计 §8，评审①阻断2 裁决）
 * - 点「发送」= 对该条**不可变记录**的明示单次授权：feedbackEnqueue 入 per-profile
 *   feedback_pending 队列（feedback_id/授权时间/正文/诊断快照定格），再 sendFeedback()
 *   只发该 profile 的 pending 队列——**不捎带** usage/benchfb/mcp 任何其他队列；
 *   关遥测也能发（sendFeedback 不问 usageEnabled/consent，点发送即单次授权）。
 * - 公钥未配置 / WebCrypto 不可用（feedback_core.hasSendChannel()=false）→ **不提供发送按钮**，
 *   只给「复制意见（含诊断信息）到剪贴板」兜底 + 「当前未配置加密传输通道，请粘贴发给开发者」。
 * - 发送失败如实呈现，并提供「复制到剪贴板」（记录保留 pending，自动重试仍会发出）与
 *   「复制并取消自动重试」（复制后从队列移除该条，避免「已粘贴给开发者 + 又自动发出」的双通道重复）。
 *
 * ## 诊断信息
 * - 「附诊断信息」默认勾选；说明文字 = 版本/平台/最近错误/功能计数 allowlist 聚合（只读既有
 *    usage 事件，绝不为了诊断重启采集）；遥测关闭时如实显示「无可用统计」。
 *
 * ## 埋点
 * - feedback_sent{with_diag} 计数在**接收端入库时**完成（B3 已实现），客户端不另埋文本类事件
 *   （设计 §8/§10「接收端侧」），故本文件不调用 usageLog。
 */

import { $, toast, cacheGeneration, copyTextAny, currentAccountScope } from "#core";
import { usageEnabledForScope, usageEventsForScope } from "#usage_log";
import { feedbackClipboardText, feedbackDiagBuild, feedbackEntryBuild, feedbackNewId, feedbackTextState } from "./feedback_dialog_core.js";
import { feedbackEnqueue, feedbackRemoveForScope, hasSendChannel as feedbackHasSendChannel } from "./feedback_core.js";
import { closeModal, openModal, trapModalFocus } from "./copy.js";

/* ---------- 模块内状态（仅 UI 态；数据真源在 feedback_core 队列） ---------- */

/* 当前对话框会话：{fid, channel, diag, state}。state: "idle"→"sending"→"done"|"failed"|"cancelled" */
let _dialog = null;

/* usage_upload.js 只经动态 import 取（它的静态 import 会牵动两页 importmap——本模块同红线）。 */
let _uploadModulePromise = null;
function _loadUploadModule() {
    if (!_uploadModulePromise) _uploadModulePromise = import("./usage_upload.js");
    return _uploadModulePromise;
}

function _scope() { return currentAccountScope(); }

function _platformText() {
    try {
        const uad = navigator.userAgentData;
        if (uad && uad.platform) return String(uad.platform);
    } catch (_e) {}
    return String(navigator.platform || "");
}

/* ---------- 剪贴板（写入走 core.copyTextAny：clipboard API → execCommand 兜底，能力基元唯一实现） ---------- */

/* 当前对话框内容（正文 + 勾选时附诊断）复制到剪贴板。 */
function _copyCurrent() {
    if (!_dialog) return;
    const ta = $("feedbackText");
    const chk = $("feedbackDiagChk");
    const withDiag = !!(chk && chk.checked);
    copyTextAny(feedbackClipboardText(ta ? ta.value : "", withDiag ? _dialog.diag : null), { okMsg: "已复制到剪贴板" });
}

/* ---------- 渲染 ---------- */

function _setStatus(text) {
    const st = $("feedbackStatus");
    if (!st) return;
    st.textContent = text;
    st.hidden = !text;
}

function _renderCount() {
    const ta = $("feedbackText");
    const node = $("feedbackCount");
    if (!ta || !node) return;
    const st = feedbackTextState(ta.value);
    node.textContent = st.count + " / " + st.max;
}

/* 打开时按通道渲染操作区：有通道 → 只「发送」；无通道 → 只「复制意见（含诊断信息）到剪贴板」。 */
function _renderActions() {
    const send = $("feedbackSendBtn");
    const copy = $("feedbackCopyBtn");
    const note = $("feedbackTransmitNote");
    if (_dialog && _dialog.channel) {
        if (send) { send.hidden = false; send.disabled = true; }
        if (copy) copy.hidden = true;
        if (note) note.textContent = "传输方式：内容经开发者公钥加密后发送；请勿包含 API Key、密码。";
    } else {
        if (send) send.hidden = true;
        if (copy) copy.hidden = false;
        if (note) note.textContent = "当前未配置加密传输通道，请复制意见粘贴发给开发者；请勿包含 API Key、密码。";
    }
}

function _renderDiagSummary() {
    const node = $("feedbackDiagSummary");
    if (!node) return;
    node.textContent = _dialog ? _dialog.diag.summary : "";
}

/* ---------- 开合 ---------- */

function _openDialog() {
    const modal = $("feedbackModal");
    if (!modal) return;
    const scope = _scope();
    const channel = feedbackHasSendChannel();
    // 遥测关闭 → events=null → buildDiagSnapshot 返回 available:false（「无可用统计」，不重启采集）。
    const events = usageEnabledForScope(scope) ? usageEventsForScope(scope) : null;
    const diag = feedbackDiagBuild(events, {
        version: cacheGeneration(),
        platform: _platformText(),
    });
    _dialog = { fid: feedbackNewId(), channel: channel, diag: diag, state: "idle" };
    const ta = $("feedbackText");
    if (ta) ta.value = "";
    const chk = $("feedbackDiagChk");
    if (chk) chk.checked = true;
    const fail = $("feedbackFailWrap");
    if (fail) fail.hidden = true;
    _renderActions();
    _renderDiagSummary();
    _renderCount();
    _setStatus("");
    openModal(modal, { initialFocus: ta });
}

function _closeDialog() {
    const modal = $("feedbackModal");
    closeModal(modal);
    _dialog = null;
}

/* ---------- 发送（明示单次授权：入队 + sendFeedback） ---------- */

async function _onSend() {
    if (!_dialog || _dialog.state !== "idle") return;
    const ta = $("feedbackText");
    const st = feedbackTextState(ta ? ta.value : "");
    if (!st.ok) {
        toast(st.count > st.max ? "意见太长，最多 " + st.max + " 字" : "请先写下意见");
        if (ta) ta.focus();
        return;
    }
    const chk = $("feedbackDiagChk");
    const withDiag = !!(chk && chk.checked);
    const scope = _scope();
    // 入队 = 对该条不可变记录的明示单次授权（feedback_id 在本会话开窗时定格，供失败后精确撤单）。
    const entry = feedbackEntryBuild(ta.value, withDiag, _dialog.diag, {
        feedback_id: _dialog.fid,
        authorized_at: new Date().toISOString(),
    });
    _dialog.state = "sending";
    const sendBtn = $("feedbackSendBtn");
    if (sendBtn) sendBtn.disabled = true;
    _setStatus("发送中……");
    let sent = false;
    try {
        feedbackEnqueue(scope, entry, {});
        const upload = await _loadUploadModule();
        sent = await upload.sendFeedback(scope);
    } catch (_e) { sent = false; }
    if (!_dialog || _dialog.state !== "sending") return;   // 发送期间被关窗/重开 → 静默放弃展示
    if (sent) {
        _dialog.state = "done";
        _setStatus("已发送，谢谢反馈");
        const t = setTimeout(_closeDialog, 1200);
        if (t && typeof t.unref === "function") t.unref();
    } else {
        _dialog.state = "failed";
        if (sendBtn) sendBtn.disabled = false;
        const fail = $("feedbackFailWrap");
        if (fail) fail.hidden = false;
        const ft = $("feedbackFailText");
        if (ft) ft.textContent = "发送失败，未送达。可复制意见手动发给开发者，避免自动重试与手动粘贴重复发送：";
        _setStatus("");
    }
}

/* 失败态「复制到剪贴板」：复制但**保留**队列记录（自动重试仍会发出）。 */
function _onFailCopy() {
    if (!_dialog || _dialog.state !== "failed") return;
    _copyCurrent();
}

/* 失败态「复制并取消自动重试」：复制后从队列移除该条（feedback_id 精确撤单），
   避免「已粘贴给开发者 + 自动重试又发出」双通道重复。 */
function _onFailCopyCancel() {
    if (!_dialog || _dialog.state !== "failed") return;
    _copyCurrent();
    feedbackRemoveForScope(_scope(), [_dialog.fid]);
    _dialog.state = "cancelled";
    const fail = $("feedbackFailWrap");
    if (fail) fail.hidden = true;
    const sendBtn = $("feedbackSendBtn");
    if (sendBtn) sendBtn.disabled = true;
    _setStatus("已复制；该条已取消自动重试，不会再经此通道发送");
}

/* ---------- 接线（自接线，全 DOM 自守卫；不进 boot/importmap 键表——见文件头） ---------- */

function _init() {
    const entry = $("feedbackSendEntryBtn");
    if (entry) entry.addEventListener("click", _openDialog);
    const close = $("feedbackCloseBtn");
    if (close) close.addEventListener("click", _closeDialog);
    const modal = $("feedbackModal");
    if (modal) modal.addEventListener("click", function (e) { if (e.target === modal) _closeDialog(); });
    const ta = $("feedbackText");
    if (ta) ta.addEventListener("input", function () {
        _renderCount();
        const send = $("feedbackSendBtn");
        if (send && _dialog && _dialog.state === "idle") send.disabled = !feedbackTextState(ta.value).ok;
    });
    const send = $("feedbackSendBtn");
    if (send) send.addEventListener("click", _onSend);
    const copy = $("feedbackCopyBtn");
    if (copy) copy.addEventListener("click", _copyCurrent);
    const failCopy = $("feedbackFailCopyBtn");
    if (failCopy) failCopy.addEventListener("click", _onFailCopy);
    const failCancel = $("feedbackFailCopyCancelBtn");
    if (failCancel) failCancel.addEventListener("click", _onFailCopyCancel);
    // Esc 关闭 + Tab 焦点圈（与 consentModal 同款；只在本弹窗可见时生效）。
    document.addEventListener("keydown", function (e) {
        const m = $("feedbackModal");
        if (!m || m.hidden) return;
        if (e.key === "Escape") { _closeDialog(); return; }
        trapModalFocus(e, m);
    });
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _init);
} else {
    _init();
}
