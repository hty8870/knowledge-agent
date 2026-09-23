"use strict";

/* 本文件是 ES Module：core 的工具、shell 的 getConfig、interactions 的 queryForRetrieval/
   getDateRange、search 的 LAST_RECOMMEND_DATA（活绑定只读）、results 的三个分面状态（活绑定只读）、
   usage_log/usage_core/act_core 经 import 取。_tpPlan/_tpChosen 导出成活绑定供 act.js 只读
   （同 LAST_RECOMMEND_DATA 例）。results.js/search.js 经 import 取
   resetTaskPack/syncTaskPackBar（绞杀桥已全退役）。
   2026-08-30：「真实数据下载」改道——不再服务端代下，面板底部主按钮把勾选
   交给统一下载引擎 core/downloads.js（/api/files 取主文件 → 浏览器直下，面板内队列区可追加/取消）。
   服务端代下 UI（旧 _dl 状态机）已退役；后端 /api/download/* 保留给 MCP「下载到本机」。 */
import { API, $, escapeHtml, sampleFrom, toast } from "#core";
import { getConfig } from "#shell";
import { queryForRetrieval, getDateRange } from "#interactions";
import { LAST_RECOMMEND_DATA } from "#search";
import { _facetFilters, _suppressed, _lenientDims } from "#results";
import { usageLog } from "#usage_log";
import { USAGE_KINDS } from "#usage_core";
import { tpBytes, actCountNoteZh, actExcludedFilesNote } from "#act_core";
import { dlqBind, dlqEnqueueDatasets, dlqFilenameFrom, dlqFireBlob, dlqRender } from "#downloads";

/* 一句话任务包的界面层：先看清单，确认后再产包。

   刻意不做「一句话直接产包」：用户没勾过任何东西就打包，等于替他决定了「哪几条算数」，
   而这份材料是要拿去投稿的。先把「会收录什么、装不了什么、缺什么」摊开给他看。

   面板里坏消息排在最前面——装不了什么、只取主文件的声明，都在四档计数之前。

   ## 下载（2026-08-30）

   面板底部现在有两颗按钮，语义完全不同：
   - 主按钮「下载勾选的数据集文件」：把勾选的**主文件**经统一下载引擎（core/downloads.js）
     直接交给浏览器下载（几十 GB 的文件只能走这条原生通道，见 downloads.js 文件头）。
     队列区长在面板下方：排队项可取消、浏览器拦下的可重发、暂不入队的逐条点名。
   - 次按钮「仍生成任务包（清单/下载脚本/引文）」：走既有 zip 链不变，是主按钮的兜底。

   诚实约束：行动流只放关键行、长明细收 <details>；unsupported/失败逐条列出，
   绝不报成「全部成功」。 */

export let _tpPlan = null;      // 上一次预览拿到的清单（act.js 经活绑定只读：回执要复述口径行）
export let _tpChosen = new Set();
/* 409（服务端发现「和你刚才看到的不是同一批了」）的兜底句唯一真源——
   产包回执与 tpShowStale 面板共用，后端 message_zh 缺席时两处必须说同一句话。 */
const TP_STALE_FALLBACK = "这份清单已经不是刚才那一批了。";
let _tpSeq = 0;          // 并发代号：晚到的旧预览一律丢弃
let _tpBusy = false;
/* 用户这次说的条数与实际用上的条数。两者不等必须**显式告诉用户**，不许静默偏离。
   {said, used} 都是 0 表示这次没有从话里读出条数（走默认口径）。 */
let _tpCount = { said: 0, used: 0 };

export function resetTaskPack() {
    /* 下载队列状态在 downloads.js（不在本文件），重置面板**不再**触碰队列——
       结果重渲/换查询把面板清掉后，下载面板下次重开时队列记录原样复活（dlqRender 重画）。
       旧 _dl 守卫（下载中跳过重置）随之退役：现在没有任何「重置能杀掉的在途状态」。 */
    _tpPlan = null;
    _tpChosen = new Set();
    _tpBusy = false;
    _tpCount = { said: 0, used: 0 };
    const panel = $("taskPackPanel");
    if (panel) { panel.hidden = true; panel.innerHTML = ""; }
    const btn = $("taskPackBtn");
    if (btn) btn.hidden = true;
}

/* 候选池只有 10/20/50 三档（服务端 ALLOWED_LIMITS），但**勾选可以是候选池的任意子集**——
   `plan_spec` 刻意不含 selected_uids，产包时服务端按 selected_uids 重建。
   于是「前5条」不必把档位改成 5：取 10 条的池子、只勾前 5 条即可，且这 5 条就是确定性排序前 5 条。 */
const TP_POOL_TIERS = [10, 20, 50];
export function tpPoolFor(count) {
    for (let i = 0; i < TP_POOL_TIERS.length; i += 1) {
        if (TP_POOL_TIERS[i] >= count) return TP_POOL_TIERS[i];
    }
    return TP_POOL_TIERS[TP_POOL_TIERS.length - 1];
}

/* 每次有新结果落地都作废上一份清单。
   只在「没有结果」时才作废是不够的：搜 A → 打开面板拿到 A 的清单 → 改成搜 B → 有结果、
   按钮照常在 → 再点开面板，`_tpPlan` 还在，直接画出 **A 的清单**并允许照它产包。
   清单便宜（点开就重新预览一次），而拿着上一条查询的材料去投稿不便宜。 */
export function syncTaskPackBar(data) {
    const btn = $("taskPackBtn");
    if (!btn) return;
    const hasResults = !!(data && (data.results || []).length);
    resetTaskPack();
    btn.hidden = !hasResults;
}

/* 按钮只负责展开/收起面板；产包是面板底部那颗独立按钮。
   一个按钮既开面板又产文件，用户点第二下时不知道会发生什么。 */
function toggleTaskPackPanel() {
    const panel = $("taskPackPanel");
    if (!panel) return;
    if (!panel.hidden) { panel.hidden = true; return; }
    panel.hidden = false;
    if (_tpPlan) { renderTaskPackPlan(_tpPlan); return; }
    previewTaskPack();
}

function tpRequestBody(extra) {
    const cfg = getConfig();
    const query = ($("queryInput").value || "").trim();
    const data = LAST_RECOMMEND_DATA || {};
    const rewritten = (data.audit && data.audit.used && data.audit.rewritten_query) || "";
    const body = Object.assign({
        query: queryForRetrieval(query),
        query_effective: rewritten,
        use_llm: false,
        sources: cfg.sources,
        auto_parse_sources: cfg.auto_parse_sources,
        facet_filters: _facetFilters.map(function (f) { return { dim: f.dim, value: f.value }; }),
        suppressed_constraints: _suppressed.slice(),
        lenient_dims: _lenientDims.slice(),
        limit: 10,
        scope: "primary"
    }, getDateRange(query));
    return Object.assign(body, extra || {});
}

/* 返回 `{ok, plan?, chosen?, error?, stale?}`。
   **必须有返回值**：调用方（尤其是「一句话直接执行」的派发器）要据此决定敢不敢接着产包，
   也要据此渲染回执——没有返回值就只能照「成功」渲染，断网/409/后端无命中全会被写成「已经打包好了」。 */
export async function previewTaskPack(opts) {
    opts = opts || {};
    const panel = $("taskPackPanel");
    if (!panel) return { ok: false, error: "页面上没有任务包面板" };
    // 这次话里说的条数（0 = 没说）。档位按它取，勾选按它截。
    const wantSaid = Math.max(0, parseInt(opts.count, 10) || 0);
    const want = wantSaid > 50 ? 50 : wantSaid;
    const myGen = ++_tpSeq;
    /* 2026-08-16：预览**不再自动打开面板**（原 panel.hidden=false 在此 + renderTaskPackPlan 末尾，
       连「下载top5」这类自动打包都会把面板弹到用户脸上；且用户手动关面板后迟到的 fetch 会把它重开
       ——此 race 随本处一并消掉）。显式开面板的调用方（toggleTaskPackPanel /
       actFixClick panel 分支 / cbRouteAsFirstBox / actRunPackPreview 的 pack.preview 分流）
       都是自己先 unhide 再调本函数，零影响。 */
    panel.innerHTML = '<p class="tp-loading">正在整理这一批数据能打包成什么…</p>';
    let payload = null;
    try {
        const body = tpRequestBody({
            limit: want ? tpPoolFor(want) : (opts.limit || 10),
            keep_selected: want ? [] : Array.from(_tpChosen)
        });
        const res = await fetch(API.taskPackPreview, {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
        });
        payload = await res.json();
        if (!res.ok || !payload.ok) throw new Error(payload.detail || "没能整理出这一批的清单");
    } catch (err) {
        if (myGen !== _tpSeq) return { ok: false, stale: true, error: "已被更晚的一次预览取代" };
        // **失败必须作废上一份清单**。以前这里只改 panel 就 return，`_tpPlan` 原封不动——
        // 而 buildTaskPack 的全部前置条件只有 `_tpPlan && _tpChosen.size`。于是「预览失败 → 接着产包」
        // 会拿**上一条查询**的候选池产出一个货真价实的 zip，四道 409 锁一个都不会响
        //（它们锁的是「回传参数与重跑自洽」，而回传的整套参数都是旧的、完全自洽）。
        // 今天这条路走不通只是因为错误面板里没渲染产包按钮；一旦有代码直接调 buildTaskPack 就绕开了。
        _tpPlan = null; _tpChosen = new Set(); _tpCount = { said: 0, used: 0 };
        const msg = String((err && err.message) || err);
        panel.innerHTML = '<p class="tp-msg">没能整理出这一批的清单：' + escapeHtml(msg) + "</p>";
        return { ok: false, error: msg };
    }
    if (myGen !== _tpSeq) return { ok: false, stale: true, error: "已被更晚的一次预览取代" };
    await tpIdentityLookup();   // 同名行身份锚点（发表日期）用的全库索引；本地服务亚秒级，失败静默降级
    if (myGen !== _tpSeq) return { ok: false, stale: true, error: "已被更晚的一次预览取代" };
    if (!payload.plan) {
        _tpPlan = null; _tpChosen = new Set(); _tpCount = { said: 0, used: 0 };
        const msg = payload.message_zh || "这次检索没有可以打包的内容。";
        panel.innerHTML = '<p class="tp-msg">' + escapeHtml(msg) + "</p>";
        return { ok: false, error: msg };
    }
    _tpPlan = payload.plan;
    const pool = _tpPlan.candidate_uids || [];
    if (want) {
        // 用户明确说了条数 → 只勾前 want 条（少选合法）。够不够得看池子实际有多少，见 _tpCount。
        _tpChosen = new Set(pool.slice(0, want));
    } else {
        const keep = payload.keep_selected || [];
        _tpChosen = new Set(keep.length ? keep : pool);
    }
    _tpCount = { said: wantSaid, used: want ? _tpChosen.size : 0 };
    renderTaskPackPlan(_tpPlan, payload);
    return { ok: true, plan: _tpPlan, chosen: _tpChosen.size, count: Object.assign({}, _tpCount) };
}

/* 「你说的条数」和「实际用上的条数」不一致时说清楚（措辞锚点：act_core.actCountNoteZh）。
   静默按 min(说的, 实际) 执行而不吭声，正是本项目反复修过的「静默偏离」。 */
function tpCountNoteZh() {
    return actCountNoteZh(_tpCount.said, _tpCount.used);
}

/* 面板行的身份锚点：同名数据集（uid …-ff-ultima 与 …-ff-ultima-4，同名同来源）
   在行里长成无法区分的两行——12/14 的文件数差异埋在 tier_evidence 长句里，算不上身份。
   预览投影（webapp _preview_projection）不带日期/样本量字段，而候选池是服务端**确定性排序**
   截的（可复现口径，见面板脚注），与屏幕展示集（可开大模型重排、top_k 不同）不是同一刀——
   实测池里的 -4 条根本不在展示结果里，从 LAST_RECOMMEND_DATA 借身份会漏。故用全库真源
   /api/datasets（browse.js 本就整取它）：本地服务一次拉取、模块级缓存，面板开前 await。
   每条取最紧凑的身份——发表日期（这对同名记录是 2025-02-20 vs 2025-07-24；只给年份 2025
   区分不开，必须全日期），缺日期退样本量（count+unit 按 core.sampleFrom 客户端格式化，
   与后端 format_sample_size 逐位对齐）。拉取失败静默降级：行维持原样，不硬编、不报错。 */
let _tpIdentityMap = null;      // Map<uid, string>；null=未拉取
let _tpIdentityPromise = null;
function tpIdentityLookup() {
    if (_tpIdentityMap) return Promise.resolve(_tpIdentityMap);
    if (_tpIdentityPromise) return _tpIdentityPromise;
    _tpIdentityPromise = fetch(API.datasets)
        .then(function (res) { return res.json(); })
        .then(function (d) {
            const map = new Map();
            ((d && d.records) || []).forEach(function (r) {
                const uid = String(r.dataset_uid || "");
                if (!uid || map.has(uid)) return;
                const date = String(r.published_date || "").slice(0, 10);
                if (date) { map.set(uid, "发表 " + date); return; }
                const sample = String(r.count || "").trim() ? sampleFrom(r.count, r.unit) : "";
                if (sample) map.set(uid, "样本量 " + sample);
            });
            _tpIdentityMap = map;
            return map;
        })
        .catch(function () { _tpIdentityPromise = null; return new Map(); });   // 下次开面板重试
    return _tpIdentityPromise;
}
function tpRowIdentity(uid) {
    return (_tpIdentityMap && _tpIdentityMap.get(String(uid || ""))) || "";
}

/* 面板里的每一个数字都必须描述**当前勾选的那几条**。
   以前只有按钮上的数字跟着勾选走，「只取主文件」那句话、四档计数、装不了什么全停在候选池口径——
   勾掉一个带 FASTQ 的数据集，屏幕上仍写着「其中原始测序数据 3 个」，用户会以为它们还在包里。
   凡是随勾选变的，一律在这里按 `_tpChosen` 重算；不随勾选变的（口径政策、候选池怎么截的）
   照实标明是「候选池」口径，不混着说。 */
function renderTaskPackPlan(plan, payload) {
    const panel = $("taskPackPanel");
    if (!panel) return;
    const items = (plan.items || []).filter(function (it) { return _tpChosen.has(it.dataset_uid); });
    let html = '<div class="tp-head"><strong>下载这批数据</strong>'
        + '<button type="button" class="btn" id="taskPackCloseBtn">✕ 关闭</button></div>';

    // 坏消息先说——但只说勾选里的那几条。
    const cannot = (plan.cannot_include || []).filter(function (m) { return _tpChosen.has(m.dataset_uid); });
    if (cannot.length) {
        html += '<div class="tp-cannot"><strong>装不了什么</strong><ul>'
            + cannot.map(function (m) {
                return "<li>" + escapeHtml(m.dataset_name) + "（" + escapeHtml(m.source) + "）："
                    + escapeHtml(m.why_zh) + "</li>";
            }).join("") + "</ul></div>";
    }

    let files = 0, bytes = 0;
    const tierCount = {}, tierRows = {};
    items.forEach(function (it) {
        files += it.rows_planned || 0;
        bytes += it.bytes_selected || 0;
        tierCount[it.tier] = (tierCount[it.tier] || 0) + 1;
        tierRows[it.tier] = (tierRows[it.tier] || 0) + (it.rows_planned || 0);
    });

    const countNote = tpCountNoteZh();
    if (countNote) html += '<p class="tp-msg">' + escapeHtml(countNote) + "</p>";

    const allScope = String(plan.scope || "primary") !== "primary";
    html += '<p class="tp-primary">' + escapeHtml(plan.primary_only_policy_zh || plan.primary_only_zh) + "</p>";
    const excludedNote = actExcludedFilesNote(items);
    html += '<p class="tp-primary">已勾选 ' + items.length + " 个数据集，共 " + files + (allScope ? " 个文件" : " 个主文件")
        + (excludedNote ? ("；" + excludedNote) : "")
        + "。</p>";

    html += '<div class="tp-tiers">' + (plan.tiers || []).map(function (t) {
        const count = tierCount[t.tier] || 0, rows = tierRows[t.tier] || 0;
        if (!count) return "";
        const note = (count > 0 && rows === 0) ? "（这一档有数据集，但没有为它们生成下载命令）" : "";
        return '<span class="tp-tier-chip" title="' + escapeHtml(t.explain_zh) + '">'
            + escapeHtml(t.label_zh) + " " + count + " 个 · 下载命令 " + rows + " 条"
            + escapeHtml(note) + "</span>";
    }).join("") + "</div>";

    if (payload && payload.message_zh) html += '<p class="tp-msg">' + escapeHtml(payload.message_zh) + "</p>";
    if (plan.ledger && plan.ledger.degrade_sentence_zh) {
        html += '<p class="tp-msg">' + escapeHtml(plan.ledger.degrade_sentence_zh) + "</p>";
    }

    html += '<div class="tp-rows">' + (plan.items || []).map(function (it) {
        const checked = _tpChosen.has(it.dataset_uid) ? " checked" : "";
        // 运行中允许改勾——勾选只影响「待更新」差量集（不即时提交），
        // 与在途集不一致时下载区出现「更新下载」按钮；不再 disabled。
        // 名称之外补一个最紧凑的身份锚点（发表日期），同名两行由此可区分；
        // 仍塞在同一个 .tp-row-meta 静音行里，不动行高与布局节奏。
        const identity = tpRowIdentity(it.dataset_uid);
        return '<label class="tp-row"><input type="checkbox" data-tp-uid="' + escapeHtml(it.dataset_uid) + '"'
            + checked + '><span class="tp-row-name">' + escapeHtml(it.dataset_name) + "</span>"
            + '<span class="tp-row-meta">' + escapeHtml(it.source)
            + (identity ? " · " + escapeHtml(identity) : "") + " · "
            + escapeHtml(it.tier_evidence) + "</span></label>";
    }).join("") + "</div>";

    // 这一段是按**整个候选池**算的（后端一次算好，逐条拆不开），所以标题里就把口径写死，
    // 不让它冒充「你勾的这几条需要确认的事」。
    html += '<details class="tp-todo"><summary>需要你自己确认的事（按候选池 '
        + (plan.candidate_uids || []).length + " 条统计，共 " + (plan.todo || []).length
        + " 条）</summary><ul>"
        + (plan.todo || []).map(function (t) { return "<li>" + escapeHtml(t) + "</li>"; }).join("")
        + "</ul></details>";

    // dl-browser-queue 主/次双按钮：主按钮（浏览器直下主文件）恒在（有文件时），label 明确
    // 「真实数据共约 X」——这是清单里文件的真实体积（preview 逐文件 bytes 求和），不是 zip 体积。
    // 次按钮（任务包 zip 兜底）恒在。
    const dlLabel = files > 0
        ? ("下载勾选的数据集文件（" + items.length + " 个数据集 · " + files
           + (allScope ? " 个文件" : " 个主文件") + " · 真实数据共约 " + tpBytes(bytes) + "）")
        : "";
    const packLabel = files > 0
        ? ("仍生成任务包（" + items.length + " 个数据集 · " + files
           + (allScope ? " 个文件" : " 个主文件") + " · 真实数据共约 " + tpBytes(bytes) + "）")
        : "这几条都没有可下载的文件，包里不会有下载命令（仍会生成清单、FAIR 与引文）";
    html += '<div class="tp-actions">'
        + (dlLabel ? '<button type="button" class="btn btn-primary" id="tpDlEnqueueBtn">' + escapeHtml(dlLabel) + "</button>" : "")
        + '<button type="button" class="btn tp-build-btn" id="taskPackBuildBtn">'
        + escapeHtml(packLabel) + "</button></div>";
    // 排序口径必须说清楚：候选池是服务端用**不含大模型重排**的确定性检索截的，
    // 页面上那一屏可能开着重排或向量召回。不说，用户会默认「包里就是我看到的前几条」。
    html += '<p class="tp-foot">本包由检索语句「' + escapeHtml(plan.retrieval_params.query || "")
        + '」在 ' + escapeHtml(plan.retrieval.date) + " 生成，取的是不含大模型重排的确定性排序前 "
        + (plan.candidate_uids || []).length + " 条（这样这一包才能被原样复现）。"
        + "如果你在页面上开了大模型重排或向量召回，屏幕上的顺序可能与这里不同；"
        + "这也不是「最相关的几条」的保证。勾掉不要的不会让这份清单失效。</p>";
    // 下载队列区：插在动作区之后；状态单一真源在 downloads.js，dlqRender 按队列重画这一块。
    html += '<div id="tpDlZone"></div>';
    panel.innerHTML = html;
    renderTaskPackDownloadZone();
    /* 渲染不接管显隐（原 panel.hidden=false 已删）——面板开不开由调用方决定；
       本函数既服务「面板开着时刷新清单」，也服务 pack.download 的隐藏预览。 */
}

/* ==================== 浏览器下载队列区（dl-browser-queue，2026-08-30） ====================
   面板下方的下载分区 = 统一下载引擎（core/downloads.js）的队列区。本文件不再自带任何
   下载状态机/轮询/分级：队列状态的单一真源在 downloads.js，这里只剩三个薄适配——
   渲染出口、主按钮入队、unsupported「生成任务包」回调注入（dlqBind，保持单向 import）。
   服务端代下 UI（_dl 状态机：plan/confirm/running/poll/update/cancel）已退役；
   后端 /api/download/* 端点保留——MCP「下载到本机」工具走那条服务端通道，与本面板无关。 */
function renderTaskPackDownloadZone() {
    dlqRender($("tpDlZone"));
}

/* 主按钮「下载勾选的数据集文件」：当前勾选 → dlqEnqueueDatasets（逐 uid 取 /api/files 主文件 →
   入队即发射）。一个直下文件都没有时如实 toast；队列区同屏逐条展示（含 unsupported 与原因）。 */
async function tpEnqueueChosen() {
    const uids = Array.from(_tpChosen);
    if (!uids.length) { toast("先勾选至少一个数据集"); return; }
    const res = await dlqEnqueueDatasets(uids, { auto: true });
    if (!res.queued) {
        if (res.already && !res.unsupported.length && !res.failed.length) {
            toast("这批主文件都已在下载队列里或已交给浏览器，没有重复下载；要重下请点对应行的「重下」");
        } else if (res.unsupported.length || res.failed.length) {
            toast("这批没有能直接下载的主文件，明细见下方下载队列；仍可生成任务包");
        } else {
            toast("这批没有可下载的文件");
        }
    }
}

/* unsupported 桶的「为这部分生成任务包」：只把这部分数据集（仍在当前清单勾选里的）窄化打包。 */
async function tpPackForUnsupported(uids) {
    const keep = (uids || []).filter(function (uid) { return _tpPlan && _tpChosen.has(uid); });
    if (!keep.length) { toast("这些数据集不在当前清单的勾选里，没法为它们生成任务包"); return; }
    const saved = _tpChosen;
    _tpChosen = new Set(keep);
    try { await buildTaskPack(); } finally {
        _tpChosen = saved;
        if (_tpPlan) renderTaskPackPlan(_tpPlan);
    }
}

/* 从「打包前20条 / 下载前5个 / 导出这3份」这类话里认出**用户说的条数**；认不出返回 0（调用方用默认口径）。

   旧写法 `tpLimitFromUtterance` 取整句里第一个 1-3 位数字、且只认 10/20/50 三档，两处都错：
     · 「2020年后的人类肺癌数据，打包前20条」——`/(\d{1,3})/` 咬中 2020 的前三位「202」→ 不在三档 → 落默认 10。
       用户说了 20 拿到 10，屏幕上没有任何提示。
     · 「打包前5条」——5 不在三档 → 同样静默变 10，**多给**了 5 条，正是本文件自己反对的行为。

   现在改成「数字必须带量词收尾」：
     · 高置信：前/头/取/首/这/那 + 数字 + 条/个/份/项
     · 次之：数字 + 条/个/份/项，且量词后面就是句读或结尾
   这两条天然避开 10x、COVID-19、GSE123456、2020年 —— 它们都没有量词收尾。
   刻意**不**在这里手抄一份执行词表：本函数只在服务端已经把这句话判成执行档之后才被调用，
   「是不是执行诉求」那一问已经有单一真源（vocabulary.ACTION_MARKERS），这里只负责把数字读出来。 */
export function tpCountFromUtterance(text) {
    const s = String(text || "");
    const strong = s.match(/(?:前|头|取|首|这|那)\s*(\d{1,3})\s*(?:条|个|份|项)/);
    if (strong) return parseInt(strong[1], 10);
    const plain = s.match(/(\d{1,3})\s*(?:条|个|份|项)(?=$|[，。,.、；;！!？?\s])/);
    if (plain) return parseInt(plain[1], 10);
    return 0;
}

/* tpBytes 已归入纯核 act_core.js（2026-08-02，task_pack / act 共用单一真源）——
   经 import 取（见文件头）。 */

function toggleTaskPackItem(uid) {
    if (_tpChosen.has(uid)) _tpChosen.delete(uid); else _tpChosen.add(uid);
    /* dl-browser-queue：勾选只改清单与按钮口径；下载队列与勾选**不联动**——
       已入队的条目留在队列里（要取消在队列区点「取消」），想下新勾的再点一次主按钮。 */
    if (_tpPlan) renderTaskPackPlan(_tpPlan);
}

/* 返回 `{ok, artifact?, error?}`。
   `artifact` 里只放**真实产物**能证明的东西：文件名取自 Content-Disposition、字节数取自 blob。
   数据集条数单列在 `requested` 里并标明它是「送去打包的条数」而不是产物自证的数字——
   zip 响应体里没有条数字段，把 `_tpChosen.size` 说成「产物里有 N 条」就是拿请求参数冒充产物证据。 */
export async function buildTaskPack() {
    if (!_tpPlan) return { ok: false, error: "现在没有可用的打包清单" };
    if (_tpBusy) return { ok: false, error: "上一次打包还在进行中" };
    if (!_tpChosen.size) { toast("先勾选至少一个数据集"); return { ok: false, error: "还没有勾选任何数据集" }; }
    const btn = $("taskPackBuildBtn");
    _tpBusy = true;
    let replaced = false;   // 面板已被别的内容接管（例如 409 的「没有生成任何文件」）
    let outcome = { ok: false, error: "打包没有完成" };
    try {
        const res = await fetch(API.taskPackBuild, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                plan_token: _tpPlan.plan_token,
                selected_uids: Array.from(_tpChosen),
                snapshot_id: _tpPlan.retrieval.snapshot_id,
                content_digest: _tpPlan.retrieval.content_digest,
                retrieval_date: _tpPlan.retrieval.date,
                scope: _tpPlan.scope || "primary",
                retrieval_params: _tpPlan.retrieval_params,
                format: "zip"
            })
        });
        if (res.status === 409) {
            const info = await res.json();
            tpShowStale(info);
            replaced = true;   // 下面的 finally 不许再重画清单，否则这条提示当场被盖掉
            outcome = { ok: false, stale: true, error: info.message_zh || TP_STALE_FALLBACK };
            return outcome;
        }
        if (!res.ok) {
            let detail = "生成失败";
            try { const j = await res.json(); detail = j.message_zh || j.detail || detail; } catch (_e) {}
            toast(detail);
            outcome = { ok: false, error: detail };
            return outcome;
        }
        const blob = await res.blob();
        const name = dlqFilenameFrom(res.headers.get("content-disposition")) || "biodata-task-pack.zip";
        dlqFireBlob(name, blob, { kind: "pack" });   // 单通道：zip 也进统一下载引擎（队列留痕）
        // 只在**真拿到产物之后**记 —— 记在函数入口会把失败的打包也算成成功，
        // 那就成了「反馈包自己在撒谎」，正是这个项目一直在修的那类毛病。
        usageLog(USAGE_KINDS.dl, { what: "pack", n: _tpChosen.size });
        toast("任务包已生成，解压后先看 00-START-HERE.txt");
        outcome = {
            ok: true,
            artifact: { filename: name, bytes: blob.size },
            requested: { n_datasets: _tpChosen.size, count: Object.assign({}, _tpCount) }
        };
        return outcome;
    } catch (err) {
        const msg = String((err && err.message) || err);
        toast("生成失败：" + msg);
        outcome = { ok: false, error: msg };
        return outcome;
    } finally {
        _tpBusy = false;
        if (btn) { btn.disabled = false; }
        if (_tpPlan && !replaced) renderTaskPackPlan(_tpPlan);
    }
}

/* 服务端发现「和你刚才看到的不是同一批了」——四种原因分开说，
   因为用户要做的事不一样：目录变了 vs 内容被更新 vs 检索条件对不上。 */
function tpShowStale(info) {
    const panel = $("taskPackPanel");
    if (!panel) return;
    panel.innerHTML = '<div class="tp-head"><strong>没有生成任何文件</strong></div>'
        + '<p class="tp-msg">' + escapeHtml(info.message_zh || TP_STALE_FALLBACK) + "</p>"
        + '<p class="tp-msg-detail">' + escapeHtml(info.hint_zh || "") + "</p>"
        + '<div class="tp-actions"><button type="button" class="btn btn-primary" id="taskPackRetryBtn">重新预览</button></div>';
    panel.hidden = false;
}

/* blob 发射基元 downloadBlobAs 在 #core，记录/队列在 #downloads（dlqFireBlob）——
   本文件只调 dlqFireBlob，不再直连基元（单通道原则）。
   文件名取响应头 Content-Disposition 的唯一实现：#downloads.dlqFilenameFrom。 */

export function initTaskPack() {
    const btn = $("taskPackBtn");
    if (btn) btn.addEventListener("click", toggleTaskPackPanel);
    const panel = $("taskPackPanel");
    if (!panel) return;
    // 队列区宿主登记 + unsupported 回调注入（dlqBind 保持 downloads 不反向 import 本模块）。
    dlqBind("tpDlZone", { onPackForUnsupported: tpPackForUnsupported });
    panel.addEventListener("click", function (event) {
        const target = event.target;
        if (target.id === "taskPackCloseBtn") { panel.hidden = true; return; }
        if (target.id === "taskPackBuildBtn") { buildTaskPack(); return; }
        if (target.id === "taskPackRetryBtn") { previewTaskPack(); return; }
        // dl-browser-queue：主按钮把勾选交给统一下载引擎（队列区按钮由 downloads.js 全局委托自理）
        if (target.id === "tpDlEnqueueBtn") { tpEnqueueChosen(); return; }
    });
    panel.addEventListener("change", function (event) {
        const uid = event.target.dataset ? event.target.dataset.tpUid : null;
        if (uid) toggleTaskPackItem(uid);
    });
}
