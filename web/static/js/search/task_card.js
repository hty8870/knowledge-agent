"use strict";

/* 下一步行动 · 任务卡弹窗壳
 *
 * ## 职责
 * 结果页阶梯 chip 里「需 LLM / 联网 / 多步」的动作 → 打开可编辑任务卡：说明范围 /
 * 预计输出 / 联网行为 + 可编辑任务文本 + 「开始」按钮才发送。**不做静默执行、不做
 * 填输入框迂回**（验证交互裁决）。
 *
 * ## template_originated（全链路的客户端起点）
 * 文本**未经编辑直接发送** → 调用方收到 `templateOriginated:true`（并携带 recipe 的
 * suggested_recipe）；编辑过 → false 且 suggested_recipe 清空（回普通路由）；普通手打
 * 无此路径。该轮 benchfb 轮次记录由调用方（ladder.js → ubSubmit opts）带上对应键。
 * 这里同时发 usage 计数 `template_originated{edited}`（计数型无文本）。
 *
 * ## 加载与接线
 * 本文件经 index.html <script type="module"> 加载并**自接线**（不进 boot，同 feedback.js
 * 哲学）：纯逻辑核 ladder_core.js 走**相对 import**（同 feedback.js → feedback_dialog_core.js），
 * 不进静态 # 图、不牵动 dataset.html importmap。
 */
import { $, escapeHtml } from "#core";
import { USAGE_KINDS } from "#usage_core";
import { usageLog } from "#usage_log";
import { ladderTaskCardState, ladderTemplateOriginated } from "./ladder_core.js";
import { closeModal, openModal, trapModalFocus } from "../core/copy.js";

/* 当前会话：{recipe, original, onSubmit}。数据真源在发起方（ladder.js 的 chip 描述），
   本模块只持「这份弹窗正在对哪个模板/回调查事」的 UI 态。 */
let _state = null;
let _returnFocus = null;

function _open(chip, onSubmit) {
    const modal = $("taskCardModal");
    if (!modal) return;
    const st = ladderTaskCardState(chip);
    _state = { recipe: st.recipe, original: st.template, onSubmit: onSubmit };
    const meta = $("taskCardMeta");
    if (meta) {
        meta.innerHTML = ""
            + (st.scopeZh ? `<div class="tc-meta-row"><b>范围</b>：${escapeHtml(st.scopeZh)}</div>` : "")
            + (st.outputZh ? `<div class="tc-meta-row"><b>预计输出</b>：${escapeHtml(st.outputZh)}</div>` : "")
            + (st.networkZh ? `<div class="tc-meta-row"><b>联网行为</b>：${escapeHtml(st.networkZh)}</div>` : "");
    }
    const ta = $("taskCardText");
    if (ta) ta.value = st.template;
    const fail = $("taskCardFail");
    if (fail) fail.hidden = true;
    const status = $("taskCardStatus");
    if (status) status.hidden = true;
    _returnFocus = document.activeElement;
    openModal(modal, { returnFocus: _returnFocus, initialFocus: ta });
    if (ta) ta.setSelectionRange(ta.value.length, ta.value.length);
}

function _close() {
    const modal = $("taskCardModal");
    if (!closeModal(modal)) return;
    _state = null;
}

function _submit() {
    if (!_state) return;
    const ta = $("taskCardText");
    const text = ta ? String(ta.value || "").trim() : "";
    const fail = $("taskCardFail");
    if (!text) {
        if (fail) { fail.hidden = false; fail.textContent = "任务内容不能为空——可以直接修改后开始。"; }
        if (ta) ta.focus();
        return;
    }
    // template_originated：未经编辑 → true；编辑过 → false。suggested_recipe
    // 只在**未经编辑**时携带（编辑后清空回普通路由）。
    const templateOriginated = ladderTemplateOriginated(_state.original, text);
    const suggestedRecipe = (templateOriginated && _state.recipe) ? _state.recipe : null;
    try {
        usageLog(USAGE_KINDS.template_originated, { edited: templateOriginated ? false : true });
    } catch (_e) { /* 埋点失败绝不影响任务发送 */ }
    const onSubmit = _state.onSubmit;
    _close();
    if (typeof onSubmit === "function") {
        onSubmit({ text: text, templateOriginated: templateOriginated, suggestedRecipe: suggestedRecipe });
    }
}

/* 供 ladder.js 调用的唯一入口：chip = ladder_core 产出的 chip 描述（recipe/template/
   scopeZh/outputZh/networkZh）；onSubmit({text, templateOriginated, suggestedRecipe})。 */
export function taskCardOpen(chip, onSubmit) {
    _open(chip, onSubmit);
}

function _init() {
    const modal = $("taskCardModal");
    if (!modal) return;
    const closeBtn = $("taskCardCloseBtn");
    if (closeBtn) closeBtn.addEventListener("click", _close);
    const cancel = $("taskCardCancelBtn");
    if (cancel) cancel.addEventListener("click", _close);
    const start = $("taskCardStartBtn");
    if (start) start.addEventListener("click", _submit);
    // 点遮罩关闭 + Esc 关闭 + Tab 焦点圈（与 feedbackModal/consentModal 同款）。
    modal.addEventListener("click", function (e) { if (e.target === modal) _close(); });
    document.addEventListener("keydown", function (e) {
        const m = $("taskCardModal");
        if (!m || m.hidden) return;
        if (e.key === "Escape") { _close(); return; }
        trapModalFocus(e, m);
    });
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _init);
} else {
    _init();
}
