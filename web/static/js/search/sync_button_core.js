"use strict";

/* 数据集页一键同步 · 纯逻辑核（对应设计 v2 §7「F4 数据集页一键同步」）
 *
 * 设计要点（逐字对照 §7）：
 *   - 副文案明示行为「检查官方源更新并导入（仅入外部库，可一键撤销）」——上屏语是行为承诺。
 *   - 三态：空闲（「上次同步：X 天前」，取 GET /api/curate/sync-status，**实例级事实**，从未同步
 *     如实写「从未同步」）→ 进行中（防重入，无取消）→ 结果摘要（**新增 X / 已存在 Z / 失败 W**，
 *     失败项如实列原因；sync 没有「更新既有记录」语义——**绝不写「更新 Y」**，设计 §1.1 如实第一）。
 *   - sync_busy 冲突（agent 可能在 chat 里发起同步）→「另一个同步任务进行中，请稍候」。
 *   - 一键撤销：按 operation_id 调 POST /api/curate/recall，结果如实呈现（撤掉 N 个/失败 M）。
 *   - 2026-08-26：「并检查 N 个追踪的更新」联动随全体批量
 *     按钮一并退役（SYNC_P4_HOOK/syncP4MountText/syncP4InfoN 删除）；批量诉求由登录后语料代
 *     哨兵自动刷新承接（project_updates.js）。
 *
 * 本文件**零 DOM / 零网络 / 零 localStorage / 零 #import**——只做文案与结构推导，node 可单测；
 * 一切界面与请求在 sync_button.js（薄 DOM 壳）。埋点（计数型无文本）由壳层调 usageLog。
 */

/* 副文案（设计 §7 原文，行为明示） */
export const SYNC_SUB_COPY = "检查官方源更新并导入（仅入外部库，可一键撤销）";
/* sync_busy 冲突时的固定上屏文案（设计 §7；agent 可能在对话里发起了同步） */
export const SYNC_BUSY_COPY = "另一个同步任务进行中，请稍候";

/* 进行中进度文案（防重入期间的唯一可见状态文字；无取消按钮——同步 HTTP 不可取消） */
export const SYNC_RUNNING_COPY = "正在检查官方源更新并导入…";

/* 一次同步最多渲染的失败项条数（失败明细逐条如实列，但控制摘要区体量） */
export const SYNC_FAIL_LINES_MAX = 8;

/* 距今整天数。入参为后端 ISO 时间串；空/坏值 → null（「从未同步」）。
   未来时间（时钟偏差）按 0 处理——不做负天数。 */
let _syncNow = Date.now;   // 测试可注入（与 projectsSetClock 同款；不注入时恒真实时钟）
export function syncSetClock(fn) { _syncNow = (typeof fn === "function") ? fn : Date.now; }
export function syncDayDiff(iso) {
    if (!iso) return null;
    const t = Date.parse(String(iso));
    if (Number.isNaN(t)) return null;
    const now = _syncNow();
    return Math.max(0, Math.floor((now - t) / 86400000));
}

/* 空闲态「上次同步」文案：从未同步如实写「从未同步」，当天写「今天」，否则「X 天前」。
   「上次同步」是**实例级事实**（设计 §7），只从后端取，不落 per-profile localStorage。 */
export function syncLastSyncedText(iso) {
    const days = syncDayDiff(iso);
    if (days === null) return "从未同步";
    if (days === 0) return "上次同步：今天";
    return "上次同步：" + days + " 天前";
}

/* 从 sync-updates 的 operation receipt 提取三计数（设计 §7 摘要口径）：
   added   = imported_total（本次成功导入外部库的条数；缺省回退 created_files 条数）
   skipped = skipped_existing（疑似新增其实已在库、未重复入库的累计条数）
   failed  = failed_sources[]（逐源错误明细条数）
   sync 无「更新既有记录」语义，这里**没有** updated 字段——「更新 Y」永远不可能出现（设计 §1.1）。 */
export function syncReceipt(result) {
    result = (result && typeof result === "object") ? result : {};
    let added = Number(result.imported_total);
    if (!Number.isFinite(added) || added < 0) {
        added = Array.isArray(result.created_files) ? result.created_files.length : 0;
    }
    let skipped = Number(result.skipped_existing);
    if (!Number.isFinite(skipped) || skipped < 0) skipped = 0;
    const failed = Array.isArray(result.failed_sources) ? result.failed_sources.length : 0;
    return { added: added, skipped: skipped, failed: failed };
}

/* 结果摘要一行：新增 X / 已存在 Z / 失败 W（设计 §7 原文格式；不写「更新 Y」）。
   全零时也如实呈现「新增 0 / 已存在 0 / 失败 0」——空结果不是错误，诚实展示。 */
export function syncReceiptText(s) {
    s = s || {};
    const added = Number(s.added) || 0, skipped = Number(s.skipped) || 0, failed = Number(s.failed) || 0;
    return "新增 " + added + " / 已存在 " + skipped + " / 失败 " + failed;
}

/* 失败项明细（设计 §7「失败项如实列出原因」）：failed_sources[] → [{label, reason}]。
   label 取后端给的中文来源名（前端不自己造口径）；reason 取该源 note_zh（后端逐源错误说明）。
   最多截 SYNC_FAIL_LINES_MAX 条，其余并入「另有 N 个来源失败」。 */
export function syncFailureLines(result) {
    const raw = (result && Array.isArray(result.failed_sources)) ? result.failed_sources : [];
    const out = [];
    raw.forEach(function (f) {
        const label = String((f && f.label) || (f && f.source) || "未知来源");
        const reason = String((f && f.note_zh) || (f && f.error) || "未知原因");
        out.push({ label: label, reason: reason });
    });
    if (out.length > SYNC_FAIL_LINES_MAX) {
        const more = out.length - SYNC_FAIL_LINES_MAX;
        return { lines: out.slice(0, SYNC_FAIL_LINES_MAX), more: more };
    }
    return { lines: out, more: 0 };
}

/* 从 recall 回执提取三计数（设计 §7「撤掉 N 个文件/失败 M」）：
   recalled = recalled_files[]（已移入回收站）
   skipped  = skipped_files[]（已不在外部库、可重入跳过）
   failed   = failed_files[]（移动失败明细） */
export function syncRecallResult(r) {
    r = (r && typeof r === "object") ? r : {};
    return {
        recalled: Array.isArray(r.recalled_files) ? r.recalled_files.length : 0,
        skipped: Array.isArray(r.skipped_files) ? r.skipped_files.length : 0,
        failed: Array.isArray(r.failed_files) ? r.failed_files.length : 0,
    };
}

/* 撤销结果摘要一行（如实呈现；回收站可恢复是后端语义，不在此重复造口径）。 */
export function syncRecallText(s) {
    s = s || {};
    const recalled = Number(s.recalled) || 0, failed = Number(s.failed) || 0, skipped = Number(s.skipped) || 0;
    if (recalled > 0 && failed > 0) return "已撤销 " + recalled + " 个文件，失败 " + failed;
    if (recalled > 0) return "已撤销 " + recalled + " 个文件";
    if (failed > 0) return "撤回失败 " + failed + " 个文件";
    if (skipped > 0) return "没有可撤回的文件（" + skipped + " 个已不在外部库）";
    return "没有可撤回的文件";
}

/* 把一次 POST /api/curate/sync-updates 的失败分类成可上屏的状态：
   { kind, message, detail }
   - kind "sync_busy"：HTTP 400 且 detail 含「同步」（sync_updates 唯一会抛的 CurateError，
     corpus_curation.py 注释「唯一会抛的情形 = 另一个 sync 正在跑」；400 只此一义）
   - kind "http"：其它非 2xx（detail 原文如实透出）
   - kind "network"：请求根本没到后端（网络/解析失败）
   message = 固定上屏文案（sync_busy 用设计 §7 文案）；detail = 后端原文，作次要行如实展示。 */
export function syncClassifyError(status, body, err) {
    if (status >= 400) {
        if (body && typeof body.detail === "string") {
            if (status === 400 && body.detail.indexOf("同步") !== -1) {
                return { kind: "sync_busy", message: SYNC_BUSY_COPY, detail: body.detail };
            }
            return { kind: "http", message: "同步失败：" + body.detail, detail: body.detail };
        }
        // 非 JSON 错误体（如 5xx HTML）：仍是 HTTP 失败，如实报状态码——不能误归类成网络问题
        return { kind: "http", message: "同步失败（HTTP " + status + "）", detail: "" };
    }
    const why = (err && err.message) ? String(err.message) : "";
    return { kind: "network", message: "网络请求失败，请重试" + (why ? "（" + why + "）" : ""), detail: why };
}
