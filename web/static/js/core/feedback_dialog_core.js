"use strict";

/* 意见反馈对话框 · 纯逻辑核心（2026-08-22，UI 壳在 feedback.js）
 *
 * 纯逻辑、零 DOM、零网络、零存储、零墙钟依赖（now/id 由调用方注入或本模块生成但不读
 * localStorage）：只做对话框状态要用的确定性计算——
 * - `feedbackTextState()`：意见正文的必填/长度校验（上限与 feedback_core.FEEDBACK_MAX_TEXT
 *   同源，避免「UI 说能写、入队被悄悄截断」的两套口径）；
 * - `feedbackDiagBuild()`：把 usage 事件数组交给 feedback_core.buildDiagSnapshot 聚合，
 *   并生成给用户看的**中文摘要行**（版本/平台/最近错误/功能使用计数；遥测关闭 events=null
 *   → available:false + 「无可用统计」，**不得为诊断重启采集**——设计 §8）；
 * - `feedbackEntryBuild()`：构造入队用的不可变记录条目（feedback_id/授权时间/遮蔽由
 *   feedback_core.feedbackEnqueue 兜底，这里只定格授权语义）；
 * - `feedbackClipboardText()`：复制到剪贴板的正文（勾选附诊断时追加诊断块）；
 * - `feedbackNewId()`：feedback_id 生成（唯一实现本就在 feedback_core，本模块 re-export
 *   供 UI 壳一处取齐，格式永不分叉）。
 *
 * 分层（与 usage_core/benchfb_core 同哲学）：本文件只被 UI 壳 feedback.js 相对 import，
 * 不进 importmap/静态图、不进任何 import 环；存储（per-profile feedback_pending 队列）在
 * feedback_core.js，唯一出网通道在 usage_upload.js（sendFeedback），本文件一个都不碰。
 */
import { FEEDBACK_MAX_TEXT, buildDiagSnapshot, feedbackNewId } from "./feedback_core.js";
export { feedbackNewId };

/* 诊断摘要里功能计数的中文标签（键与 feedback_core.FEEDBACK_DIAG_KINDS 同值同源；ai 在
   buildDiagSnapshot 里只产 ai_ok（ok:false 的 ai 并入错误计数、不另立 ai_fail），err 并入
   错误计数，这里都只作展示映射，对不上键就回退显示原始 kind，不报错）。 */
export const FEEDBACK_DIALOG_DIAG_LABELS = {
    search: "搜索", open: "打开详情", dl: "下载", facet: "筛选", relax: "放宽条件",
    conv: "对话", undo: "撤销", fav: "收藏", view: "视图", imp: "展示", label: "标注",
    ai_ok: "AI 成功",
};

/* 遥测关闭/无事件可用时对话框如实显示的文案（设计 §8：显示「无可用统计」，不采集）。 */
export const FEEDBACK_DIAG_UNAVAILABLE_TEXT = "无可用统计（遥测未开启，不为此采集）";

/* ---------- 意见正文校验 ---------- */

/* 必填 + 长度上限（FEEDBACK_MAX_TEXT=2000 与入队截断同源）。count 按 Unicode 码元计
   （与 feedback_core 的 slice 语义一致）；必填按 trim 后判定（入队时同样会 trim，
   纯空白 = 未填）。 */
export function feedbackTextState(text) {
    const t = String(text || "");
    return {
        ok: t.trim().length > 0 && t.length <= FEEDBACK_MAX_TEXT,
        count: t.length,
        max: FEEDBACK_MAX_TEXT,
    };
}

/* ---------- 诊断信息（展示 + 摘要行） ---------- */

/* events：usage 事件数组；null/非数组 = 遥测关闭/无可用统计（原样透传 buildDiagSnapshot
   语义，绝不为了诊断重启采集）。opts.version/platform 供展示。返回对象在 buildDiagSnapshot
   结果上追加 `summary`（给 UI 的复选框说明行 / 剪贴板用块文本的短版）。 */
export function feedbackDiagBuild(events, opts) {
    opts = opts || {};
    const snap = buildDiagSnapshot(events, {
        version: String(opts.version || ""),
        platform: String(opts.platform || ""),
    });
    if (snap.available !== true) {
        return { available: false, summary: FEEDBACK_DIAG_UNAVAILABLE_TEXT };
    }
    const parts = [];
    parts.push("版本 " + snap.version);
    parts.push("平台 " + snap.platform);
    parts.push("最近错误 " + snap.errors + " 次");
    const names = Object.keys(snap.features || {})
        .filter(function (k) { return (snap.features[k] || 0) > 0; })
        .sort();
    if (names.length) {
        parts.push("功能使用：" + names.map(function (k) {
            return (FEEDBACK_DIALOG_DIAG_LABELS[k] || k) + " " + snap.features[k] + " 次";
        }).join("、"));
    } else {
        parts.push("功能使用：暂无记录");
    }
    return Object.assign({}, snap, { summary: parts.join(" · ") });
}

/* ---------- 入队条目构造（明示单次授权语义） ---------- */

/* 构造 feedbackEnqueue 的 entry：feedback_id 未传则生成；authorized_at 未传取当前时间；
   text trim；with_diag 勾选时附 diag（剔除 UI 专用 summary 字段，记录只留结构化部分），
   未勾选 diag=null。真正落库时的遮蔽/截断/幂等仍由 feedback_core.feedbackEnqueue 兜底。 */
export function feedbackEntryBuild(text, withDiag, diag, opts) {
    opts = opts || {};
    let diagRecord = null;
    if (withDiag) {
        diagRecord = (diag && typeof diag === "object") ? Object.assign({}, diag) : { available: false };
        delete diagRecord.summary;
    }
    return {
        feedback_id: String(opts.feedback_id || feedbackNewId()),
        authorized_at: String(opts.authorized_at || new Date().toISOString()),
        text: String(text || "").trim(),
        diag: diagRecord,
        with_diag: !!withDiag,
    };
}

/* ---------- 剪贴板正文 ---------- */

/* 复制内容：正文 +（勾选附诊断时）诊断块。diag 传 null/未勾选 → 只有正文；
   diag.available=false → 如实附「无可用统计」行（复制兜底场景下用户应知道没带上统计）。 */
export function feedbackClipboardText(text, diag) {
    const lines = [String(text || "")];
    if (diag && diag.available === true) {
        lines.push("");
        lines.push("（诊断信息）");
        lines.push("版本：" + diag.version);
        lines.push("平台：" + diag.platform);
        lines.push("最近错误：" + diag.errors + " 次");
        const names = Object.keys(diag.features || {})
            .filter(function (k) { return (diag.features[k] || 0) > 0; })
            .sort();
        lines.push("功能使用：" + (names.length ? names.map(function (k) {
            return (FEEDBACK_DIALOG_DIAG_LABELS[k] || k) + " " + diag.features[k] + " 次";
        }).join("、") : "暂无记录"));
    } else if (diag) {
        lines.push("");
        lines.push("（诊断信息）" + FEEDBACK_DIAG_UNAVAILABLE_TEXT);
    }
    return lines.join("\n");
}

/* ---------- feedback_id 生成（唯一实现在 feedback_core.feedbackNewId，本模块 re-export） ---------- */
