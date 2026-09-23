"use strict";

/* 追踪更新检查 · UI 壳（设计 v2 §4「追踪更新检查闭环」）
 *
 * 纯逻辑在 project_updates_core.js（零 DOM，node 可单测）；本文件只管 DOM 与网络：
 *   - 追踪详情检查条件区：规范化检索规格 + display_query + 双时间戳
 *     （「本地目录同步于……」取 GET /api/curate/sync-status；「该追踪检查于……」取
 *     last_checked_at）+「检查更新」按钮；baseline=null → 按钮文案「重试生成基线」。
 *     检查后：无变化如实「本次检查无变化 · 刚检查过」；有变化进「待查看更新」列表
 *     （新增/指纹变化 uid 逐条「纳入候选」（默认待核验进 candidates）/「忽略」；
 *     真实消失只提示「已不在结果中」+ 忽略——**绝不自动改纳入表**，设计 §4.4）。
 *   - 单追踪「检查更新」向上追溯编排（仅网页形态 guard on）：先按
 *     追踪 spec 的来源触发语料同步 job（POST /api/curate/sync-updates 异步）→ 轮询
 *     GET /api/curate/sync-updates/status 到终态 → 再 POST /api/watch/check 确定性重跑，
 *     合并渲染「上游同步：新增 N 条入库/已是最新」+ 既有 delta；本机形态保持直接
 *     watch/check。全体「检查 N 个追踪的更新」按钮与联动钩子已撤（用户：点一下
 *     太耗费资源）——批量诉求由下方语料代哨兵自动刷新承接。
 *   - 登录后自动刷新（仅 guard on）：health 到达/账户切换时比对
 *     health.corpus.gen 与 localStorage 按账户 nsKey 存的 biodata_watch_gen——相同零成本
 *     跳过；不同且有追踪 → 后台顺序重跑全部追踪的 watch/check（零 LLM，每条间隔 300ms
 *     防突发），全部完成才写回新 gen（失败静默，下次启动再试）；有 delta 一次性 toast
 *     「N 个追踪有新数据」+ 通知列表重渲上既有 delta 徽标。
 *
 * ## 加载与接线
 * - 本文件**自接线不进 boot**（与 sync_button.js / feedback.js 同哲学）：index.html 由
 *   projects.js 经 `#project_updates` import 取 p4DetailMount / runProjectCheck /
 *   setWatchesRefreshedHook，boot 不加键；dataset.html 不再加载本模块（联动钩子已撤）。
 * - 详情挂点 [data-p4-mount-check] 是 projects.js renderProjectDetail 动态渲染的骨架
 *   div（预留，初始 hidden）——本模块每次收到 p4DetailMount(p) 调用时重建其内容
 *   并从会话内存态恢复「待查看更新」（_deltas）；刷新页面 = 会话态清空，如实回到
 *   只有持久化事实（baseline/candidates/last_checked_at）的界面。
 * - 健康/账户钩子：shell.js setHealthArrivedHook、accounts.js setAccountChangedHook
 *   （注册式反转防环；本模块 import #shell/#accounts，它们不反向 import 本模块）。
 *
 * ## 埋点（计数型无文本，usage_log 既有通道；设计 §10）
 * - watch_checked{changed}：每次单追踪检查完成（1=有 material change，0=无）。
 * - delta_review_completed{}：某追踪的「待查看更新」从非空逐条处理到空（用户处理完）。
 * 追踪名/uid 不进遥测（设计 §1.3 隐私红线）。
 */

import { API, $, currentAccountScope, escapeHtml, fmtTime, nsKey, readJSON, toast, writeJSON } from "#core";
import { buildCard } from "#cards";
import { catalogLookup, ensureDatasetsLoaded } from "#browse";
import { webGuardOn, healthSnapshot, setHealthArrivedHook } from "#shell";
import { setAccountChangedHook } from "#accounts";
import { USAGE_KINDS } from "#usage_core";
import { usageLog } from "#usage_log";
import { artifactsAddCandidate, artifactsGetProject, artifactsListProjects,
    artifactsSetBaseline, artifactsTouchCheckedAt, artifactsUpdateProject } from "#artifacts";
import { WATCH_CHECK_COPY, WATCH_KIND_ADDED, WATCH_KIND_FP, WATCH_KIND_REMOVED,
    WATCH_RETRY_BASELINE_COPY, WATCH_SYNC_BUSY_COPY, WATCH_SYNC_POLL_MAX, WATCH_SYNC_POLL_MS,
    WATCH_SYNC_TIMEOUT_COPY, watchAutoRefreshToast, watchChangedCount, watchChangedFlag,
    watchCheckableProjects, watchDeltaEntries, watchDiff, watchDiffCounts, watchGenChanged, watchReceiptText,
    watchSpecSources, watchSyncJobState, watchUpstreamText } from "./project_updates_core.js";

/* ---------- 模块内状态（本文件唯一有状态区；数据真源在 artifacts.js / _deltas） ---------- */

/* 会话内「待查看更新」：projectId → [{uid, kind}]（kind ∈ added/fp/removed）。
   不落库（artifacts.js schema 白名单无此字段，「追踪内待查看更新」由本模块持有）；
   刷新页面清空——已纳入候选的 uid 经 candidates 持久，如实不丢用户决定。 */
const _deltas = new Map();

/* 单追踪检查在途防重入（projectId 集合；详情检查按钮随动禁用） */
const _checking = new Set();

function _scope() { return currentAccountScope(); }
function _esc(v) { return escapeHtml(v); }

/* ---------- 会话「待查看更新」内存态 ---------- */
function _deltasOf(projectId) { return _deltas.get(String(projectId)) || []; }
function _deltasSet(projectId, entries) {
    _deltas.set(String(projectId), Array.isArray(entries) ? entries : []);
}
/* delta_review_completed（设计 §10）：用户把某追踪的「待查看更新」逐条处理到空时计一次
   （计数型无文本）。只在用户处理动作后调用——检查覆盖（_watchCheckOnce 里的 _deltasSet）
   不算「用户处理完」，不埋。 */
function _deltaReviewMaybeCompleted(projectId) {
    const id = String(projectId);
    if ((_deltas.get(id) || []).length > 0) return;
    try { usageLog(USAGE_KINDS.delta_review_completed, {}); } catch (_e) {}
}

/* ============================================================================
 * 追踪详情检查条件区（projects.js renderProjectDetail 调 p4DetailMount(p)）
 * ========================================================================== */

/* 挂点定位：详情 body 内预留的骨架 div（projects.js 每次重渲详情都会重建它）。 */
function _detailMountEl() {
    const body = $("artifactsWinBody");
    return body ? body.querySelector(".prj-check-mount[data-p4-mount-check]") : null;
}

/* 渲染详情检查条件区。p 为追踪对象（projects.js 现读传入）；每次调用整区重建
   （幂等），「待查看更新」从 _deltas 会话态恢复。
   **刻意非 async**：projects.js 以 try/catch 探测式降级调用，async 函数体的 rejected
   promise 接不住——对外函数一律同步返回，内部异步（读 sync-status）自己 catch。 */
export function p4DetailMount(p) {
    try {
        const mount = _detailMountEl();
        if (!mount || !p || !p.check_condition || !p.check_condition.spec) return;   // 无检查条件不渲染
        const projectId = p.project_id;
        const cc = p.check_condition;
        const hasBaseline = !!(cc.baseline && Array.isArray(cc.baseline.uids));
        const deltas = _deltasOf(projectId);

        mount.hidden = false;
        mount.innerHTML = "";

        /* 双时间戳行（设计 §4.3：分开显示）——本地目录同步（实例级事实，异步补；失败如实） */
        const timeRow = document.createElement("div");
        timeRow.className = "wd-times";
        mount.appendChild(timeRow);
        _renderSyncTimeLine(timeRow);
        const checkedLine = document.createElement("span");
        const checkedAt = cc.last_checked_at ? fmtTime(cc.last_checked_at) : "";
        checkedLine.textContent = checkedAt ? "该追踪检查于 " + checkedAt : "";
        timeRow.appendChild(checkedLine);

        /* 操作行：检查按钮（baseline=null → 「重试生成基线」，包描述原文） */
        const acts = document.createElement("div");
        acts.className = "wd-acts";
        const runBtn = document.createElement("button");
        runBtn.type = "button";
        runBtn.className = "btn wd-run";
        runBtn.textContent = hasBaseline ? WATCH_CHECK_COPY : WATCH_RETRY_BASELINE_COPY;
        runBtn.addEventListener("click", async () => {
            await runProjectCheck(projectId, runBtn);
            try { const fresh = await artifactsGetProject(_scope(), projectId); p4DetailMount(fresh || p); } catch (_e) { p4DetailMount(p); }
        });
        acts.appendChild(runBtn);
        mount.appendChild(acts);

        /* 待查看更新列表（会话态恢复；无则不渲染） */
        if (deltas.length) {
            mount.appendChild(_renderDeltaList(projectId, p, deltas));
        }
    } catch (_e) { /* 渲染失败静默：检查条件区保持空白，不崩（探测式降级兜底） */ }
}

/* 本地目录同步时间行（实例级事实，GET /api/curate/sync-status；失败如实标注不虚构） */
function _renderSyncTimeLine(timeRow) {
    const span = document.createElement("span");
    span.className = "wd-sync-line";
    span.textContent = "本地目录同步于：读取中…";
    timeRow.insertBefore(span, timeRow.firstChild);
    fetch(API.curateSyncStatus).then((res) => res.json()).then((j) => {
        const st = (j && j.ok && j.result) ? j.result : null;
        const last = st ? st.last_sync_at : null;
        span.textContent = last
            ? "本地目录同步于 " + fmtTime(last)
            : "本地目录同步于：从未同步";
    }).catch(() => {
        span.textContent = "本地目录同步于：读取失败";
    });
}

/* 待查看更新列表（卡片化，视觉 spec §6.2）：每条 uid → .prj-cand-wrap 包 compact 卡；目录能解析
   → buildCard(variant:"library")；removed/找不到 → 文字行 + 「已不在结果中/已下架」；load_error → 目录未加载。
   「查看数据集 / 纳入候选 / 忽略」按钮进 .prj-cand-acts（removed 只提示不提供纳入）。 */
function _renderDeltaList(projectId, p, deltas) {
    const box = document.createElement("div");
    box.className = "wd-deltas";
    const head = document.createElement("div");
    head.className = "wd-delta-head";
    head.textContent = "待查看更新（" + deltas.length + "）";
    box.appendChild(head);

    deltas.forEach((d) => {
        const kindText = d.kind === WATCH_KIND_ADDED ? "新增"
            : d.kind === WATCH_KIND_FP ? "信息变化" : "已不在结果中";
        const wrap = document.createElement("div");
        wrap.className = "prj-cand-wrap wd-delta";
        wrap.dataset.uid = d.uid;
        wrap.dataset.kind = d.kind;

        const badge = document.createElement("span");
        badge.className = "prj-cand-st" + (d.kind === WATCH_KIND_REMOVED ? " st-已排除" : " st-候选");
        badge.textContent = kindText;
        wrap.appendChild(badge);

        let lookup = null;
        try { lookup = (typeof catalogLookup === "function") ? catalogLookup(d.uid) : null; } catch (_e) { lookup = null; }
        const found = lookup && lookup.status === "found";
        const notFound = lookup && lookup.status === "not_found";
        const loadErr = lookup && lookup.status === "load_error";
        if (found) {
            try { wrap.appendChild(buildCard(lookup.item, { variant: "library" })); } catch (_e) {
                wrap.appendChild(_deltaTextRow(d, notFound, loadErr));
            }
        } else {
            wrap.appendChild(_deltaTextRow(d, notFound, loadErr));
        }

        const acts = document.createElement("div");
        acts.className = "prj-cand-acts";
        const view = document.createElement("a");
        view.className = "btn wd-delta-view";
        view.href = "/dataset?uid=" + encodeURIComponent(d.uid);
        view.target = "_blank";
        view.rel = "noopener";
        view.textContent = "查看数据集";
        acts.appendChild(view);
        if (d.kind !== WATCH_KIND_REMOVED) {
            const adopt = document.createElement("button");
            adopt.type = "button";
            adopt.className = "btn wd-delta-adopt";
            adopt.textContent = "纳入候选";
            adopt.title = "以「待核验」状态加入追踪候选（不会自动纳入，可在此核验/排除）";
            adopt.addEventListener("click", () => _adoptDelta(projectId, d.uid, adopt));
            acts.appendChild(adopt);
        }
        const ignore = document.createElement("button");
        ignore.type = "button";
        ignore.className = "btn wd-delta-ignore";
        ignore.textContent = "忽略";
        ignore.title = "本条变化不再提示（仅本次检查会话；不做任何写库）";
        ignore.addEventListener("click", () => _ignoreDelta(projectId, d.uid, ignore));
        acts.appendChild(ignore);
        wrap.appendChild(acts);

        box.appendChild(wrap);
    });
    if (deltas.some((d) => { let l = null; try { l = (typeof catalogLookup === "function") ? catalogLookup(d.uid) : null; } catch (_e) { l = null; } return l && l.status === "load_error"; }) && typeof ensureDatasetsLoaded === "function") {
        ensureDatasetsLoaded().then(() => { try { const fresh = artifactsGetProject(_scope(), projectId).then((np) => p4DetailMount(np || p)); } catch (_e) {} }).catch(() => {});
    }
    return box;
}
function _deltaTextRow(d, notFound, loadErr) {
    const row = document.createElement("div");
    row.className = "prj-cand" + (d.kind === WATCH_KIND_REMOVED ? " wd-delta-gone" : "");
    row.innerHTML = '<div class="prj-cand-main"><span class="prj-cand-uid">' + _esc(d.uid) + "</span>"
        + (d.kind === WATCH_KIND_REMOVED ? '<span class="gone-badge">已下架</span>' : "")
        + (loadErr ? '<span class="prj-cand-meta">目录未加载，稍后重试</span>' : "")
        + "</div>";
    return row;
}


/* 「纳入候选」：以默认「待核验」状态加入 candidates（绝不自动改纳入表——加入后仍待
   用户逐条核验/排除，design §3.1/§4.4 硬性）。uid 已在候选 → 如实提示不重复加。 */
async function _adoptDelta(projectId, uid, btn) {
    const scope = _scope();
    if (btn) { btn.disabled = true; }
    try {
        const p = await artifactsGetProject(scope, projectId);
        if (!p) { toast("追踪不存在"); return; }
        const existed = (p.candidates || []).some((c) => c.uid === uid);
        await artifactsUpdateProject(scope, projectId, (np) => artifactsAddCandidate(np, uid));
        _deltasSet(projectId, _deltasOf(projectId).filter((d) => d.uid !== uid));
        _deltaReviewMaybeCompleted(projectId);
        toast(existed ? "该数据集已在候选表中（状态不变）" : "已加入候选（待核验）——请到候选区核验/排除");
        const p2 = await artifactsGetProject(scope, projectId);
        p4DetailMount(p2 || p);
    } catch (e) {
        toast("纳入候选失败：" + ((e && e.message) || "未知错误"));
        if (btn) { btn.disabled = false; }
    }
}

/* 「忽略」：仅把本条从会话待查看列表移除（不写库）；处理完触发 delta_review_completed 埋点。 */
async function _ignoreDelta(projectId, uid, btn) {
    if (btn) { btn.disabled = true; }
    _deltasSet(projectId, _deltasOf(projectId).filter((d) => d.uid !== uid));
    _deltaReviewMaybeCompleted(projectId);
    const p = await artifactsGetProject(_scope(), projectId);
    if (p) p4DetailMount(p);
    else p4DetailMount({ project_id: projectId });
}

/* 单追踪「检查更新」（行级入口）：POST /api/watch/check（确定性重跑，零 LLM）→ 相对保存的
   baseline diff → 回填 baseline + last_checked_at（设计 §4.4「检查完回填」）→ 更新会话待查看列表。
   导出 `runProjectCheck(projectId, btnEl)`：**进入立刻占 _checking + 禁按钮**（修竞态——
   旧实现首个 await 后才占位，双击可重入），返回结构化 outcome `{changed, added, fp, removed, error,
   baselineCreated}`，调用方据此 toast + 行级重渲。细节/列表按钮都走它。 */
let _lastFail = new Map();   // 会话内上次检查失败的追踪（行内「检查失败，可重试」标记）
export function checkFailed(projectId) { return _lastFail.has(String(projectId)); }
export function pendingDeltaCount(projectId) { return _deltasOf(projectId).length; }

export async function runProjectCheck(projectId, btnEl) {
    const id = String(projectId);
    if (_checking.has(id)) return { checking: true };
    _checking.add(id);   // 先占位，再 await（修竞态）
    // 行按钮 spinner / 文字按钮「检查中…」（两种按钮形态：.ra-btn 带 svg；.wd-run/脚本形文字钮）
    const svg = btnEl && btnEl.querySelector("svg");
    if (btnEl) { btnEl.disabled = true; if (svg) { svg.classList.add("ra-spin"); btnEl.title = "检查中…"; } else { btnEl.textContent = "检查中…"; } }
    let outcome = { changed: 0, added: 0, fp: 0, removed: 0, error: null, baselineCreated: false };
    try {
        const scope = _scope();
        const p = await artifactsGetProject(scope, projectId);
        if (!p || !p.check_condition || !p.check_condition.spec) { toast("这个追踪没有检查条件"); outcome.error = "no_spec"; return outcome; }
        /* 2026-08-26 起：网页形态（guard on）先向上追溯——按追踪 spec 的
           来源触发语料同步 job 并等终态，再确定性重跑；本机形态保持直接 watch/check。 */
        let upstreamText = null;
        if (typeof webGuardOn === "function" && webGuardOn()) {
            const up = await _upstreamSyncAndWait(watchSpecSources(p.check_condition.spec));
            if (up && up.timeout) {
                outcome.error = "sync_timeout";
                toast(WATCH_SYNC_TIMEOUT_COPY);   // 后台仍在同步：本次不重跑，如实提示稍后再看
                return outcome;
            }
            if (up && up.error) {
                // job 冲突/失败不阻断检查：友好文案后按当前目录继续（诚实降级）
                toast(up.busy ? WATCH_SYNC_BUSY_COPY : ("上游同步失败：" + up.error + "；按当前目录检查"));
            } else if (up && up.ok) {
                upstreamText = watchUpstreamText(up.result);
                const gen = await _fetchCorpusGen();   // 手动同步完成写回新语料代，防自动刷新紧接重跑
                if (gen) _writeGen(gen);
            }
        }
        const out = await _watchCheckOnce(p);
        if (out && out.error) {
            _lastFail.set(id, true);
            outcome.error = out.error;
            toast((out.error === "rule_updated")
                ? "检索规则已更新，无法按旧规则检查——请重新保存追踪或等待规则适配"
                : "检查失败：" + out.error);
        } else if (out && out.diff) {
            _lastFail.delete(id);
            const diffCounts = watchDiffCounts(out.diff);   // {added, fp, removed}
            outcome = Object.assign({}, outcome, diffCounts, {
                changed: watchChangedCount(out.diff),
                baselineCreated: !(p.check_condition && p.check_condition.baseline && Array.isArray(p.check_condition.baseline.uids)),
                error: null,
            });
            // 回执如实说清「检查了 N 条，X 条有更新，Y 条已是最新」，并点明「检查不自动改候选表」
            //   ——否则用户看到「候选 10 · 待核验 10」不动误以为坏了（设计 §4.4：绝不自动改纳入表）。
            if (outcome.baselineCreated) {
                toast("基线已生成（首次检查）——之后的更新会列在「待查看更新」。候选表未自动改变。");
            } else {
                toast((upstreamText ? upstreamText + "；" : "")
                    + watchReceiptText((out.diff && out.diff.resultTotal) || 0, outcome.changed, diffCounts)
                    + "；候选表未自动改变，更新的数据集请到「待查看更新」逐条「纳入候选」。");
            }
        }
    } catch (e) {
        _lastFail.set(id, true);
        outcome.error = ((e && e.message) || "网络请求失败，请重试");
        toast("检查失败：" + (((e && e.message) || "网络请求失败")));
    } finally {
        _checking.delete(id);
        if (btnEl) {
            btnEl.disabled = false;
            const svg2 = btnEl.querySelector("svg");
            if (svg2) { svg2.classList.remove("ra-spin"); btnEl.removeAttribute("title"); }
            else { btnEl.textContent = ""; }   // 由调用方重渲恢复文案
        }
    }
    return outcome;
}

/* 单追踪检查核心（详情与批量共用）：请求 + diff + 回填 baseline/last_checked_at + 会话态。
   返回 {diff} | {error: "rule_updated"|详情}。**绝不自动改纳入表**——只回填基线时间戳。 */
async function _watchCheckOnce(p) {
    const scope = _scope();
    const projectId = p.project_id;
    const cc = p.check_condition;
    const spec = cc.spec;
    try {
        const res = await fetch(API.watchCheck, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(spec),
        });
        const j = await res.json().catch(() => null);
        if (!res.ok || !j || !j.ok || !j.result) {
            const detail = (j && typeof j.detail === "string") ? j.detail : "";
            /* spec_version 与当前端点支持版本不一致 → 「检索规则已更新」单列（§4.3），
               不伪装成目录变化；不回填基线（旧规则的检查结果无意义）。 */
            if (detail.indexOf("spec_version") !== -1) {
                return { error: "rule_updated" };
            }
            return { error: detail || ("HTTP " + res.status) };
        }
        const r = j.result;
        const diff = watchDiff(r, cc.baseline, String(spec.spec_version || ""));
        /* 检查完回填 baseline + last_checked_at（设计 §4.4 明文；baseline=null 时即
           「重试生成基线」的回填路径）。 */
        try {
            await artifactsUpdateProject(scope, projectId, (np) => {
                let next = np;
                if (np.check_condition) {
                    next = artifactsSetBaseline(np, {
                        uids: r.uids, fingerprints: r.fingerprints || {},
                        result_total: Number(r.result_total) || 0, truncated: r.truncated === true,
                        generated_at: String(r.checked_at || ""),
                    });
                    next = artifactsTouchCheckedAt(next, r.checked_at);
                }
                return next;
            });
        } catch (_e) {
            /* 基线回填失败：diff 仍可展示（如实），下轮检查会重试——不掀翻检查本身 */
        }
        /* 会话「待查看更新」：diff 结果进内存态（刷新即清；纳入候选的已持久）。 */
        _deltasSet(projectId, diff.kind === "diff" ? watchDeltaEntries(diff) : []);
        /* 埋点（计数型无文本，设计 §10）：watch_checked{changed} */
        try { usageLog(USAGE_KINDS.watch_checked, { changed: watchChangedFlag(diff) }); } catch (_e) {}
        return { diff: diff };
    } catch (e) {
        return { error: ((e && e.name === "AbortError") ? "已取消" : ((e && e.message) || "网络请求失败，请重试")) };
    }
}

/* ============================================================================
 * 上游同步编排（2026-08-26 起）：启动/附着语料同步 job 并轮询到终态。
 * 只在 guard on 调用（异步协议；job 层单飞吸收并发——attach 即等同一个任务，不会撞 sync_busy）。
 * ========================================================================== */

function _sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

async function _readProjects() {
    try { return await artifactsListProjects(_scope()); } catch (_e) { return []; }
}

/* 返回 {ok:true, result} / {error, busy} / {timeout:true} / null（非异步响应 = 协议漂移防御，
   调用方当作无上游直接走检查）。轮询 1.5s × WATCH_SYNC_POLL_MAX ≈ 5 分钟上限。 */
async function _upstreamSyncAndWait(sources) {
    let j;
    try {
        const res = await fetch(API.curateSyncUpdates, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(sources ? { sources: sources } : {}),
        });
        j = await res.json().catch(() => null);
        if (!res.ok || !j || !j.ok) {
            const detail = (j && typeof j.detail === "string") ? j.detail : ("HTTP " + res.status);
            return { error: detail, busy: detail.indexOf("同步") !== -1 };
        }
    } catch (e) {
        return { error: ((e && e.message) || "网络请求失败，请重试") };
    }
    if (j.async !== true || !j.job) return null;
    for (let i = 0; i < WATCH_SYNC_POLL_MAX; i++) {
        const st = watchSyncJobState(j.job);
        if (st.done) {
            if (st.error) return { error: st.error, busy: st.error.indexOf("同步") !== -1 };
            return { ok: true, result: st.result };
        }
        await _sleep(WATCH_SYNC_POLL_MS);
        try {
            const r2 = await fetch(API.curateSyncJobStatus);
            const j2 = await r2.json().catch(() => null);
            if (r2.ok && j2 && j2.ok && j2.job) j = j2;
        } catch (_e) { /* 单次轮询失败静默，下轮再试（只读端点） */ }
    }
    return { timeout: true };
}

/* ============================================================================
 * 登录后自动刷新（2026-08-26 起，仅 guard on）：health 到达/账户切换时比对
 * 语料代哨兵（health.corpus.gen）与本地按账户 nsKey 存的 biodata_watch_gen——相同零成本
 * 跳过；不同且有追踪 → 后台**顺序**重跑全部追踪的 watch/check（零 LLM，复用
 * _watchCheckOnce，每条间隔 300ms 防突发），全部完成才写回新 gen（失败静默，下次启动
 * 再试）；有 delta 的追踪在列表行显示既有 delta 徽标 + 一次性 toast「N 个追踪有新数据」。
 * ========================================================================== */

const WATCH_GEN_LS = "biodata_watch_gen";
const WATCH_AUTO_INTERVAL_MS = 300;
let _autoRefreshRunning = false;
const _autoRefreshDone = {};   // scope::gen → true（health 钩子与账户钩子都会来，本会话内防双跑）

function _readGen() {
    try {
        const v = readJSON(nsKey(WATCH_GEN_LS), null);
        return (typeof v === "string" && v) ? v : null;
    } catch (_e) { return null; }
}
function _writeGen(gen) {
    try { writeJSON(nsKey(WATCH_GEN_LS), String(gen)); } catch (_e) {}
}

/* 当前语料代哨兵：health 快照取（钩子触发时快照刚写入）。不可得 → null（降级跳过）。 */
function _currentCorpusGen() {
    try {
        const h = (typeof healthSnapshot === "function") ? healthSnapshot() : null;
        const gen = h && h.corpus && h.corpus.gen;
        return (typeof gen === "string" && gen) ? gen : null;
    } catch (_e) { return null; }
}

/* 重新拉一次 health 取语料代（手动同步完成后写回；快照可能还是旧的）。 */
async function _fetchCorpusGen() {
    try {
        const h = await (await fetch(API.health, { cache: "no-store" })).json();
        const gen = h && h.corpus && h.corpus.gen;
        return (typeof gen === "string" && gen) ? gen : null;
    } catch (_e) { return null; }
}

/* 自动刷新完成钩子（注册式反转防环：projects.js 注册列表重渲上 delta 徽标，本模块不反向
   import projects）。 */
const _watchesRefreshedHooks = [];
export function setWatchesRefreshedHook(fn) {
    if (typeof fn === "function" && !_watchesRefreshedHooks.includes(fn)) _watchesRefreshedHooks.push(fn);
}
function _fireWatchesRefreshed() {
    _watchesRefreshedHooks.forEach((h) => { try { h(); } catch (_e) {} });
}

async function _maybeAutoRefreshWatches() {
    if (_autoRefreshRunning) return;
    try {
        if (typeof webGuardOn !== "function" || !webGuardOn()) return;   // 本机形态不自动刷
        const gen = _currentCorpusGen();
        if (!gen) return;                                 // 哨兵不可得 → 跳过，不报错
        const scope = _scope();
        if (!scope) return;                               // 未登录；登录后账户钩子会再触发
        if (!watchGenChanged(_readGen(), gen)) return;    // 零成本跳过
        const doneKey = scope + "::" + gen;
        if (_autoRefreshDone[doneKey]) return;
        const checkable = watchCheckableProjects(await _readProjects());
        _autoRefreshRunning = true;
        let changed = 0;
        for (let i = 0; i < checkable.length; i++) {
            try {
                const out = await _watchCheckOnce(checkable[i]);
                if (out && out.diff && watchChangedCount(out.diff) > 0) changed++;
            } catch (_e) { /* 单条失败不连累其余 */ }
            if (i < checkable.length - 1) await _sleep(WATCH_AUTO_INTERVAL_MS);
        }
        _writeGen(gen);            // 全部完成才写回（失败静默、下次启动再试）
        _autoRefreshDone[doneKey] = true;
        const note = watchAutoRefreshToast(changed);
        if (note) { toast(note); _fireWatchesRefreshed(); }
    } catch (_e) { /* 失败静默：不写回 gen，下次启动再试 */ }
    finally { _autoRefreshRunning = false; }
}

/* ---------- 初始化（index.html 自挂 DOMContentLoaded；挂点不存在即静默） ---------- */

function initProjectUpdates() {
    /* 全体「检查 N 个追踪的更新」按钮（浮窗头 + 旧联动钩子）已撤（2026-08-26，
       用户：点一下太耗费资源）——批量诉求由语料代哨兵自动刷新承接。 */
    if (typeof setHealthArrivedHook === "function") setHealthArrivedHook(_maybeAutoRefreshWatches);
    if (typeof setAccountChangedHook === "function") setAccountChangedHook(_maybeAutoRefreshWatches);
}

document.addEventListener("DOMContentLoaded", initProjectUpdates);
