"use strict";

/* User-facing interaction vocabulary. This is the browser copy anchor: feature
   modules select a key and add facts; they do not invent confirm/retry/empty
   variants locally. Cross-language domain labels are guarded by tests. */
export const COPY = Object.freeze({
    common: Object.freeze({
        retry: "重试",
        failedRetry: "操作失败，请稍后重试",
        emptySelection: "还没有选中任何项。",
        delete: "删除",
        confirmDelete: "再点一次确认删除",
        clear: "清空",
        confirmClear: "再点一次确认清空",
        unnamedDataset: "（未命名）",
        /* 弹窗/下载被浏览器拦截时的统一提醒（2026-09-09 用户点图：试用时下载触发弹窗拦截，
           用户未必发现窗口被拦、也不知那意味着什么）。唯一定义点：openPopup（core.js）
           被拦 toast、downloads.js 发射 toast 与面板页脚、act.js 下载回执共用这一句，
           不许在各调用点另写变体。 */
        popupBlocked: "没反应？多半是浏览器拦截了弹窗或下载：看地址栏右侧的拦截提示图标，允许本站的弹窗/下载后重试。",
    }),
    conditions: Object.freeze({ include: "纳入条件", exclude: "排除条件" }),
    introductionFacts: Object.freeze(["数据来源", "物种", "组织", "疾病", "技术与平台", "样本量", "发表时间", "原始数据"]),
    boardZones: Object.freeze({
        query: Object.freeze({ title: "你这次要找的条件", note: "这几条用来从全部数据集里挑。" }),
        prefer: Object.freeze({ title: "只用来排先后", note: "你写了「优先」的这几项没有筛掉任何数据，只是让符合的排在前面。所以结果里仍然会有不符合这几项的数据集。" }),
        facet: Object.freeze({ title: "在已筛出的结果里再缩小", note: "只在上面挑出来的那批里继续缩小，不会把新的数据集找回来。" }),
        lenient: Object.freeze({ title: "已放宽", note: "没填这一项的，或来源只给了抽样、填得不全的，都算符合；只有填了、而且和你要的明显不一样的，才排除。" }),
        suppressed: Object.freeze({ title: "这次没有用它筛", note: "这个条件我读出来了，但按你的要求没拿它筛。" }),
        prefer_off: Object.freeze({ title: "这次没有拿它排先后", note: "你写的「优先」我读出来了，但按你的要求这次没拿它排先后。它本来也不筛数据，所以停用它之后结果条数不会变。" }),
    }),
});

export function setStatusLine(element, text, isError) {
    if (!element) return;
    element.hidden = !text;
    element.textContent = text || "";
    element.classList.toggle("is-error", !!isError);
}

export function selectedValues(container, selector, attribute) {
    if (!container) return [];
    return Array.from(container.querySelectorAll(selector))
        .map((node) => attribute ? node.getAttribute(attribute) : node.value)
        .filter(Boolean);
}

export function resetTwoStepConfirm(button, idleText, idleHtml) {
    if (!button) return;
    if (button._twoStepTimer) clearTimeout(button._twoStepTimer);
    button._twoStepTimer = null;
    button.classList.remove("armed");
    delete button.dataset.confirmArmed;
    if (idleHtml != null) button.innerHTML = idleHtml;
    else if (idleText != null) button.textContent = idleText;
}

/* Returns true only on the confirming click. Callers perform the destructive
   action in that branch, which keeps the helper independent of feature state. */
export function armTwoStepConfirm(button, options) {
    if (!button) return false;
    const opts = options || {};
    const idleText = opts.idleText == null ? COPY.common.delete : opts.idleText;
    if (button.dataset.confirmArmed === "1") {
        resetTwoStepConfirm(button, idleText, opts.idleHtml);
        return true;
    }
    button.dataset.confirmArmed = "1";
    button.classList.add("armed");
    button.textContent = opts.confirmText || COPY.common.confirmDelete;
    button._twoStepTimer = setTimeout(() => resetTwoStepConfirm(button, idleText, opts.idleHtml), opts.timeoutMs || 3000);
    return false;
}

const _modalFocus = new WeakMap();
const _FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';

export function openModal(modal, options) {
    if (!modal) return false;
    const opts = options || {};
    _modalFocus.set(modal, opts.returnFocus || document.activeElement || null);
    modal.hidden = false;
    if (opts.lockBody !== false) document.body.classList.add("modal-lock");
    const target = opts.initialFocus || modal.querySelector("[autofocus], " + _FOCUSABLE);
    if (target && target.focus) target.focus();
    return true;
}

export function closeModal(modal, options) {
    if (!modal || modal.hidden) return false;
    const opts = options || {};
    modal.hidden = true;
    if (opts.lockBody !== false) document.body.classList.remove("modal-lock");
    const target = opts.returnFocus === false ? null : (opts.returnFocus || _modalFocus.get(modal));
    _modalFocus.delete(modal);
    if (target && document.body.contains(target) && target.focus) target.focus();
    return true;
}

export function trapModalFocus(event, modal) {
    if (!event || event.key !== "Tab" || !modal || modal.hidden) return false;
    const nodes = Array.from(modal.querySelectorAll(_FOCUSABLE))
        .filter((node) => !node.hidden && node.getClientRects().length);
    if (!nodes.length) return false;
    const first = nodes[0], last = nodes[nodes.length - 1];
    if (!modal.contains(document.activeElement)) { event.preventDefault(); first.focus(); return true; }
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); return true; }
    if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); return true; }
    return false;
}
