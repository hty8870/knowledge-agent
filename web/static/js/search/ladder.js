"use strict";

/* 下一步行动 · 结果页阶梯 UI 壳（纯规则在 ladder_core.js）
 *
 * ## 挂点
 * - results.js 每次结果区重建（renderResults）经 `setLadderRenderHook` 调本模块
 *   `ladderAfterRender(data)`——**注册式反转防环**（results 不 import 本模块；本模块
 *   import #results 取 setter 与分面态，单向边，不进 SCC）。
 * - 渲染进 #resultsHead 内两个骨架：#ladderNarrow（过宽收窄建议，顶部）与 #ladderBar
 *   （2–4 颗阶梯 chips）。调用方唯一（renderResults，data 恒非空——空态/失败/弃权各
 *   分支也带 data 落地）。
 *
 * ## 三类行为（判据全在 ladder_core，这里只接线）
 * ① 直接执行（raw_only / 收窄建议）→ 套既有 facet 筛选 + 重跑（确定性、零 LLM）；
 * ② 确定性导出（导出中心两颗）→ **探测式依赖导出中心**：动态 import("#export_center")
 *    解析成功（导出中心提供 openExport(kind)）才渲染；失败/未挂载 → 整颗隐藏不报错；
 * ③ 需 LLM/多步 → task_card.js 打开可编辑任务卡，确认后走 ubSubmit（普通提交路径：
 *    在途闸/consent/进度/benchfb 轮次全复用），未经编辑携带 suggested_recipe +
 *    template_originated=true（经 ubSubmit opts → benchfbTurnBegin，见 benchfb.js）。
 *
 * ## 埋点（全部计数型无文本）
 * ladder_shown{n}（每次展示）、ladder_clicked{action}（每颗 chip 点击，action=动作 id）、
 * template_originated{edited}（任务卡发送，task_card.js 完成）。
 */
import { $, escapeHtml, getFavs, prettyFacetValue } from "#core";
import { USAGE_KINDS } from "#usage_core";
import { usageLog } from "#usage_log";
import { setFacetState, setLadderRenderHook, _facetFilters } from "#results";
import { runRecommend } from "#search";
import { getConfig, agentExtMissing } from "#shell";
import { cbLogPush, ubSubmit } from "#board";
import { ladderSelect, ladderNarrowSuggestions, LADDER_EXPORT_CHIPS, LADDER_RAW_ONLY_FACET } from "./ladder_core.js";
import { taskCardOpen } from "./task_card.js";

/* 导出中心探测（②）：动态 import 一个**可能不存在**的模块 specifier 会在
   解析期 reject（try/catch 兜住）；导出中心把 #export_center 键加进 importmap 并提供
   具名导出 `openExport(kind)`（kind 取 LADDER_EXPORT_CHIPS 的 id），本行零改动自动接上。
   这是跨模块耦合的标准探测式降级（目标不存在 → 隐藏入口，不报错不虚构）。 */
const EXPORT_CENTER_SPECIFIER = "#export_center";
let _exportCenterPromise = null;
function _probeExportCenter() {
    if (_exportCenterPromise) return _exportCenterPromise;
    _exportCenterPromise = import(EXPORT_CENTER_SPECIFIER).then(function (m) {
        return (m && typeof m.openExport === "function") ? m : null;
    }).catch(function () { return null; });
    return _exportCenterPromise;
}

/* 渲染代次哨兵：导出探测是异步的，期间结果区可能已被新一轮检索重建（hook 换 data）——
   探测回来后按代次核对，不把旧屏的导出 chip 挂到新屏上。 */
let _renderSeq = 0;

function _renderNarrow(narrow, data) {
    const box = $("ladderNarrow");
    if (!box) return;
    if (!narrow.length) { box.hidden = true; box.innerHTML = ""; return; }
    box.hidden = false;
    const chips = narrow.map(function (s) {
        return `<button type="button" class="ladder-chip narrow-chip" data-narrow-dim="${escapeHtml(s.dim)}"`
            + ` data-narrow-val="${escapeHtml(s.value)}" data-narrow-disp="${escapeHtml(s.display)}"`
            + ` data-narrow-label="${escapeHtml(s.label)}" title="${escapeHtml(s.display)}（${s.count} 条）">`
            + `<span class="lc-t">只看「${escapeHtml(s.display)}」</span><span class="lc-n">${s.count} 条</span></button>`;
    }).join("");
    const total = (narrow[0] && narrow[0].total) || (data && data.result_total) || 0;
    box.innerHTML = `<span class="ladder-narrow-txt">找到 <b>${total}</b> 条。按物种或组织收窄更容易比较：</span>`
        + `<span class="ladder-chips">${chips}</span>`;
    box.querySelectorAll(".narrow-chip").forEach(function (b) {
        b.addEventListener("click", function () {
            usageLog(USAGE_KINDS.ladder_clicked, { action: "narrow:" + b.dataset.narrowDim });
            _applyFacet({
                dim: b.dataset.narrowDim, value: b.dataset.narrowVal,
                display: b.dataset.narrowDisp, label: b.dataset.narrowLabel,
            });
        });
    });
}

/* 套既有 facet 筛选（确定性，零 LLM）：与 facets.js toggleFacet 同口径（同维单选换值），
   经属主 setter 重赋值 + cbLogPush 留对话痕迹 + runRecommend 重跑。 */
function _applyFacet(f) {
    if (!f || !f.dim || !f.value) return;
    setFacetState({ facetFilters: _facetFilters.filter((x) => x.dim !== f.dim).concat([f]) });
    cbLogPush("refine", "加：" + (f.label || f.dim) + " = " + prettyFacetValue(f.display || f.value));
    usageLog(USAGE_KINDS.facet, {
        d: String(f.label || f.dim || ""), dim: String(f.dim || ""), v: String(f.display || f.value || ""),
    });
    runRecommend({ keepFacets: true });
}

function _renderChips(chips) {
    const bar = $("ladderBar");
    if (!bar) return;
    if (!chips.length) { bar.hidden = true; bar.innerHTML = ""; return; }
    bar.hidden = false;
    bar.innerHTML = chips.map(function (c) {
        const label = String(c.chipLabel || c.label || c.id || "");
        return `<button type="button" class="ladder-chip" data-ladder-id="${escapeHtml(c.id)}">${escapeHtml(label)}</button>`;
    }).join("");
    bar.querySelectorAll(".ladder-chip").forEach(function (btn) {
        btn.addEventListener("click", function () { _onChipClick(btn.dataset.ladderId); });
    });
}

function _onChipClick(id) {
    const chip = _chipsById[id];
    if (!chip) return;
    usageLog(USAGE_KINDS.ladder_clicked, { action: String(id) });
    if (chip.kind === "action" && chip.id === "raw_only") {
        _applyFacet(Object.assign({}, LADDER_RAW_ONLY_FACET));
        return;
    }
    if (chip.kind === "export") {
        _probeExportCenter().then(function (m) {
            if (m) { try { m.openExport(chip.id); } catch (_e) {} }
        });
        return;
    }
    // recipe / plain：打开可编辑任务卡，确认「开始」才发送（不做静默执行）。
    taskCardOpen(chip, function (out) {
        if (!out || !out.text) return;
        // 仅模板未编辑时携带 suggested_recipe（编辑后清空回普通路由）；手打无此路径。
        ubSubmit("chat", {
            text: out.text,
            suggestedRecipe: out.suggestedRecipe,
            templateOriginated: out.templateOriginated === true ? true : false,
        }).catch(function () {});
    });
}

let _chipsById = {};   // 本次渲染的 chip 描述（id → chip），供点击路由（重渲染即整体覆盖）

/* results.js 渲染钩子（setLadderRenderHook 注册；调用方唯一 renderResults，data 恒非空）。 */
export function ladderAfterRender(data) {
    _renderSeq += 1;
    const seq = _renderSeq;
    const bar = $("ladderBar");
    const narrow = $("ladderNarrow");
    if (!bar || !narrow) return;

    const cfg = getConfig() || {};
    const agentOn = !!cfg.agent && !agentExtMissing();
    let favCount = 0;
    try { favCount = (getFavs() || []).length; } catch (_e) {}
    const ctx = {
        facetDims: _facetFilters.map(function (f) { return f.dim; }),
        rawConstrained: ((data.query_constraints) || []).some(function (c) {
            return c && String(c.filter_id || "").indexOf("raw:") === 0;
        }),
        favCount: favCount,
        agentOn: agentOn,
    };
    const sel = ladderSelect(data, ctx);
    const chips = sel.chips.slice();
    _chipsById = {};
    chips.forEach(function (c) { _chipsById[c.id] = c; });

    _renderNarrow(ladderNarrowSuggestions(data), data);
    _renderChips(chips);
    if (chips.length) usageLog(USAGE_KINDS.ladder_shown, { n: chips.length });

    // ② 确定性导出（导出探测通过才渲染）：异步探测回来后核对代次，避免旧屏 chip 挂到新屏。
    _probeExportCenter().then(function (m) {
        if (!m || seq !== _renderSeq) return;
        const bar2 = $("ladderBar");
        if (!bar2 || bar2.hidden) return;
        LADDER_EXPORT_CHIPS.forEach(function (c) { _chipsById[c.id] = c; });
        const exportBtns = LADDER_EXPORT_CHIPS.map(function (c) {
            return `<button type="button" class="ladder-chip export-chip" data-ladder-id="${escapeHtml(c.id)}">${escapeHtml(c.label)}</button>`;
        }).join("");
        const frag = document.createElement("span");
        frag.className = "ladder-chips export-chips";
        frag.innerHTML = exportBtns;
        frag.querySelectorAll(".ladder-chip").forEach(function (btn) {
            btn.addEventListener("click", function () { _onChipClick(btn.dataset.ladderId); });
        });
        bar2.appendChild(frag);
    });
}

/* 接线：结果区渲染钩子（results.js 不动本模块；本模块主动注册，单向边）。 */
function _init() {
    if (typeof setLadderRenderHook === "function") {
        setLadderRenderHook(ladderAfterRender);
    }
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _init);
} else {
    _init();
}
