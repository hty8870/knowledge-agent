"use strict";

/* ============================================================================
 * examples.js —— 操作样例库界面层（用户挑选入库）
 * ----------------------------------------------------------------------------
 * 数据流：后端机械收录（一遍过的成功管护操作）只进**候选池**，不注入；
 * 本模块在记忆弹窗里展示候选（同账户 + 同端点指纹分区才可见），用户勾选
 * 「入库所选」才迁入正式库——understand 注入侧只读正式库。「忽略所选」从池里删掉。
 * 诚实语义：列表为空就如实说「没有候选」；入库/忽略以后端返回的真实计数上屏。
 * ========================================================================== */

import { API, $, escapeHtml, toast } from "#core";
import { selectedValues, setStatusLine } from "../core/copy.js";

/* 端点坐标（分区键入参）直接读设置表单两个输入框——刻意不 import #shell 的 getConfig：
   memory.js 已 import 本模块，而 #shell 经 browse 间接回到 #memory，再引 shell 会成环
   （tests/test_frontend_import_graph.py：环只缩不涨）。 */
function _endpointCfg() {
    const b = $("cfgBaseUrl"), m = $("cfgModel");
    return { base_url: ((b && b.value) || "").trim(), model: ((m && m.value) || "").trim() };
}

function _endpointQuery() {
    const cfg = _endpointCfg();
    return "base_url=" + encodeURIComponent(cfg.base_url || "") + "&model=" + encodeURIComponent(cfg.model || "");
}

function _endpointBody() {
    return _endpointCfg();
}

function examplesStatus(text, isError) {
    setStatusLine($("examplesStatus"), text, isError);
}

function _stepLabel(step) {
    return String((step && (step.args || step.verb)) || "");
}

async function _post(url, ids) {
    const res = await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: ids, ..._endpointBody() }),
    });
    const data = await res.json().catch(function () { return {}; });
    if (!res.ok || !data.ok) throw new Error((data && data.detail) || "操作失败，请稍后再试");
    return data;
}

function _selectedIds(box) {
    return selectedValues(box, "[data-ex-id]:checked", "data-ex-id");
}

function _renderList(rows) {
    const box = $("examplesList");
    if (!box) return;
    box.innerHTML = "";
    const head = document.createElement("div");
    head.className = "dream-preview-head";
    head.innerHTML = "<strong>待挑选 " + rows.length + " 条</strong><small>勾选后入库；不想要的可忽略（只移出候选，不影响任何数据）</small>";
    box.appendChild(head);
    const list = document.createElement("div");
    list.className = "dream-preview-list";
    rows.forEach(function (row) {
        const steps = (Array.isArray(row.steps) ? row.steps : []).map(_stepLabel).filter(Boolean).join(" → ");
        const el = document.createElement("label");
        el.className = "dream-cand";
        el.innerHTML = '<input type="checkbox" data-ex-id="' + escapeHtml(String(row.id || "")) + '">'
            + '<span class="dream-cand-copy"><strong>' + escapeHtml(String(row.utterance || "")) + "</strong>"
            + (steps ? "<small>" + escapeHtml(steps) + "</small>" : "")
            + (row.ts ? "<small>" + escapeHtml(String(row.ts).replace("T", " ").slice(0, 19)) + "</small>" : "")
            + "</span>";
        list.appendChild(el);
    });
    box.appendChild(list);
    const acts = document.createElement("div");
    acts.className = "dream-preview-acts";
    const approve = document.createElement("button");
    approve.type = "button"; approve.className = "btn btn-primary"; approve.textContent = "入库所选";
    approve.addEventListener("click", function () { examplesDecide(true); });
    const dismiss = document.createElement("button");
    dismiss.type = "button"; dismiss.className = "btn"; dismiss.textContent = "忽略所选";
    dismiss.addEventListener("click", function () { examplesDecide(false); });
    acts.appendChild(approve); acts.appendChild(dismiss);
    box.appendChild(acts);
    box.hidden = false;
}

async function examplesDecide(approveThem) {
    const box = $("examplesList");
    const ids = box ? _selectedIds(box) : [];
    if (!ids.length) { examplesStatus("一条都没选。", false); return; }
    try {
        const data = await _post(approveThem ? API.curateExamplesApprove : API.curateExamplesDismiss, ids);
        if (approveThem) {
            const dup = Number(data.duplicated) || 0;
            examplesStatus("已入库 " + (Number(data.approved) || 0) + " 条"
                + (dup ? "；另有 " + dup + " 条与库中已有样例重复，跳过" : "") + "。", false);
            toast("已入库 " + (Number(data.approved) || 0) + " 条样例");
        } else {
            examplesStatus("已忽略 " + (Number(data.dismissed) || 0) + " 条候选。", false);
        }
    } catch (err) {
        examplesStatus(String((err && err.message) || err), true);
        return;
    }
    await renderExampleCandidates(true);
}

/* 记忆弹窗打开时由 memory.js 调用：拉本分区候选。空池如实告知（不藏入口——
   用户需要知道「为什么这里没有东西」）。
   preserveStatus=true（入库/忽略后的重渲染）时不动状态行——操作结果文案要留屏。 */
export async function renderExampleCandidates(preserveStatus) {
    const box = $("examplesList");
    if (box) box.hidden = true;
    try {
        const res = await fetch(API.curateExamplesPending + "?" + _endpointQuery());
        const data = await res.json().catch(function () { return {}; });
        if (!res.ok || !data.ok) throw new Error((data && data.detail) || "候选加载失败");
        const rows = Array.isArray(data.candidates) ? data.candidates : [];
        if (!rows.length) {
            if (!preserveStatus) examplesStatus("暂时没有候选——干净完成一次数据库维护操作后，会出现在这里等你挑选。", false);
            return;
        }
        if (!preserveStatus) examplesStatus("", false);
        _renderList(rows);
    } catch (err) {
        examplesStatus(String((err && err.message) || err), true);
    }
}
