"use strict";

import { upsertMemory, getUserMemories, renderMemoryManager, renderMemorySuggestions } from "#memory";
import { dreamCollectConversations, dreamClipConversations, dreamFilterNew, DREAM_MAX_CONV } from "#dream_core";
import { API, LS, nsKey, $, escapeHtml, getHist, toast } from "#core";
import { getConfig } from "#shell";
import { selectedValues, setStatusLine } from "../core/copy.js";

/* ============================================================================
 * dream.js —— dream 记忆界面层（**ES Modules 试点**：前端模块化改造的第一个模块）
 * ----------------------------------------------------------------------------
 * 加载：index.html 以 `<script type="module">` 引入（deferred，先于 DOMContentLoaded 执行完）。
 * 依赖全部经 import：#memory / #dream_core、#core 与 #shell（window 桥接已全部退役、
 * 不再经 window）；boot.js（同为 ESM）经 `import { initDream } from "#dream"` 取入口。
 *
 * dream 是什么：手动点「用历史对话整理记忆」→ 历史对话发给**用户自己配置的 LLM** →
 * 封闭 JSON 记忆候选 → **预览勾选后才写入**现有 memory 存储（kind:"dream"、徽标「AI 整理」）。
 * 诚实语义：首次使用弹知情说明（历史会离开本机）；无 key 如实报「先去配置」；
 * 整理不出就如实说「这次没整理出」——绝不编造、绝不静默。
 * ========================================================================== */

function dreamConsentGiven() {
    try { return localStorage.getItem(nsKey(LS.dreamConsent)) === "1"; } catch (_e) { return false; }
}
function dreamConsentGive() {
    try { localStorage.setItem(nsKey(LS.dreamConsent), "1"); } catch (_e) {}
}

function dreamStatus(text, isError) {
    setStatusLine($("dreamStatus"), text, isError);
}

function dreamHidePreview() {
    const box = $("dreamPreview");
    if (box) { box.hidden = true; box.innerHTML = ""; }
}

/* 取对话素材：历史快照（core.js getHist）→ 纯核组织/裁剪。 */
function dreamMaterial() {
    const hist = (typeof getHist === "function") ? getHist() : [];
    return dreamClipConversations(dreamCollectConversations(hist, DREAM_MAX_CONV));
}

async function dreamRequest(convs) {
    const cfg = getConfig();
    const res = await fetch(API.dream, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            conversations: convs,
            provider: cfg.provider, api_key: cfg.api_key,
            base_url: cfg.base_url, model: cfg.model,
        }),
    });
    const data = await res.json().catch(function () { return {}; });
    if (!res.ok || !data.ok) throw new Error((data && data.detail) || "整理失败，请稍后再试");
    return data;
}

/* 预览清单：每条一个勾选（默认全选），底部「写入所选 / 放弃」。
   每条附出处摘录（服务端出处核验放行的 evidence 原文片段）；
   被机械闸拦下的条数如实告知（没通过出处核验或形态审查）。 */
function dreamShowPreview(candidates, dropped) {
    const box = $("dreamPreview");
    if (!box) return;
    box.innerHTML = "";
    const head = document.createElement("div");
    head.className = "dream-preview-head";
    head.innerHTML = "<strong>整理出 " + candidates.length + " 条，勾选后写入</strong><small>都是 AI 从历史对话提炼的，写入前请过目"
        + (dropped ? "；另有 " + dropped + " 条没通过出处核验或形态审查，已拦下" : "") + "</small>";
    box.appendChild(head);
    const list = document.createElement("div");
    list.className = "dream-preview-list";
    candidates.forEach(function (c, i) {
        const ev = (Array.isArray(c.evidence) ? c.evidence : []).filter(Boolean);
        const row = document.createElement("label");
        row.className = "dream-cand";
        row.innerHTML = '<input type="checkbox" checked data-dream-i="' + i + '">'
            + '<span class="dream-cand-copy"><strong>' + escapeHtml(c.text) + "</strong>"
            + (c.summary ? "<small>" + escapeHtml(c.summary) + "</small>" : "")
            + (ev.length ? "<small>出处：「" + escapeHtml(ev.join("」「")) + "」</small>" : "")
            + "</span>";
        list.appendChild(row);
    });
    box.appendChild(list);
    const acts = document.createElement("div");
    acts.className = "dream-preview-acts";
    const write = document.createElement("button");
    write.type = "button"; write.className = "btn btn-primary"; write.textContent = "写入所选";
    write.addEventListener("click", function () { dreamWriteAccepted(candidates); });
    const drop = document.createElement("button");
    drop.type = "button"; drop.className = "btn"; drop.textContent = "放弃";
    drop.addEventListener("click", function () { dreamHidePreview(); dreamStatus("已放弃，这次没有写入任何记忆。", false); });
    acts.appendChild(write); acts.appendChild(drop);
    box.appendChild(acts);
    box.hidden = false;
}

function dreamWriteAccepted(candidates) {
    const box = $("dreamPreview");
    const indexes = selectedValues(box, "[data-dream-i]:checked", "data-dream-i");
    const accepted = indexes.map(function (index) { return candidates[Number(index)]; }).filter(Boolean);
    if (!accepted.length) { dreamStatus("一条都没选，没有写入。", false); return; }
    let written = 0;
    accepted.forEach(function (c) { if (upsertMemory("dream", c.text, c.summary)) written += 1; });
    dreamHidePreview();
    renderMemoryManager(); renderMemorySuggestions();
    if (written < accepted.length) {
        dreamStatus("写入 " + written + " 条；有 " + (accepted.length - written) + " 条因本地存储已满没写进。", true);
    } else {
        dreamStatus("已写入 " + written + " 条记忆（徽标「AI 整理」，可随时删或「存为偏好」）。", false);
    }
    toast("已整理出 " + written + " 条记忆");
}

async function dreamRun() {
    const btn = $("dreamRunBtn");
    dreamHidePreview();
    const convs = dreamMaterial();
    if (!convs.length) { dreamStatus("还没有历史对话可以整理——先检索几轮再来。", false); return; }
    // 首次使用：知情说明（历史对话会发给用户自己配置的 LLM）。同意一次，以后不再问。
    if (!dreamConsentGiven()) {
        const consent = $("dreamConsent");
        if (consent) consent.hidden = false;
        return;
    }
    await dreamRunInner(convs);
}

async function dreamRunInner(convs) {
    const btn = $("dreamRunBtn");
    dreamStatus("整理中…（历史对话发给你配置的 AI，一般几秒）", false);
    if (btn) btn.disabled = true;
    try {
        const data = await dreamRequest(convs);
        const fresh = dreamFilterNew(data.memories || [], getUserMemories());
        const dropped = (data.dropped && (Number(data.dropped.injection) || 0) + (Number(data.dropped.evidence) || 0)) || 0;
        if (!fresh.length) {
            dreamStatus((data.count || 0) > 0
                ? "整理出的内容与已有记忆重复，没有新东西可写。"
                : (dropped > 0
                    ? "这次整理出 " + dropped + " 条，但都没通过出处核验或形态审查，已拦下——对话再多几轮再来试试。"
                    : "这次没整理出值得记住的——对话再多几轮再来试试。"), false);
            return;
        }
        dreamStatus("", false);
        dreamShowPreview(fresh, dropped);
    } catch (err) {
        dreamStatus(String((err && err.message) || err), true);
    } finally {
        if (btn) btn.disabled = false;
    }
}

export function initDream() {
    const btn = $("dreamRunBtn");
    if (btn) btn.addEventListener("click", dreamRun);
    const ok = $("dreamConsentOk");
    if (ok) ok.addEventListener("click", function () {
        dreamConsentGive();
        const consent = $("dreamConsent");
        if (consent) consent.hidden = true;
        dreamRun();   // 同意后接着刚才那次跑
    });
    const cancel = $("dreamConsentCancel");
    if (cancel) cancel.addEventListener("click", function () {
        const consent = $("dreamConsent");
        if (consent) consent.hidden = true;
        dreamStatus("已取消。历史对话没有发送。", false);
    });
}
