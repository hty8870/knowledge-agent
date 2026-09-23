"use strict";

/* ============================================================================
 * act_core.js —— 一句话执行层「纯核」（无副作用、可单测）
 * ----------------------------------------------------------------------------
 * 从 act.js 抽出（2026-08-02）：回执/注记的构造全是**纯函数**，
 * 却是这个功能「回执写不出假话」最吃重的部分。与 memory_rank/board_core/dream_core
 * 同一哲学：ES Module——act.js / task_pack.js 经 import 取用，
 * node 由 tests/test_act_frontend.py 的临时 .mjs import 跑真行为断言。
 *
 * 设计不变量：
 *   · 成功与失败是**两个互不相通的模板**（ACT_LEAD）：失败那支从头到尾取不到「已」字——
 *     不是拼字符串时小心翼翼地绕开它，而是那条分支根本没有那个字面量。配 node 真行为门。
 *   · 「做了什么」只能由**真实返回值**构造：文件名取自 Content-Disposition、字节数取自 blob。
 *   · `confidence` 只能影响回执排版（在本文件），派发层一次都不许看它。
 *   · tpBytes 也住这里（纯格式化）：task_pack.js / act.js 共用单一真源。
 * ========================================================================== */

/* 回执抬头的两个模板。**分开成两个函数**是有意的：这样「失败模板里有没有『已』字」
   是一个可以被直接调用、直接断言的问题，而不是要去读某个分支的源码猜。
   （曾有的第三支 pending 属两步确认时代：2026-08-03 全自动化后 runner 链式 plan→apply
   无人工停点，生产侧再无任何路径产出 outcome.pending，该支随之删除——若未来恢复
   人工确认闸，连模板带门一起重写，别复活旧分支。） */
export const ACT_LEAD = {
    done: (zh) => "已" + String(zh || ""),
    fail: (zh) => "这一步没有完成：" + String(zh || ""),
};

export function actLeadZh(ok, verbZh) {
    return (ok ? ACT_LEAD.done : ACT_LEAD.fail)(verbZh);
}

/* 【留档 2026-08-03：生产侧注记形态已退役（执行全程由 act_run 行动流 + 总结 sys 呈现，
   不再有「回执在结果区」）；同日起 actChatNote 死函数删除（已无生产调用方）。】 */

/* 上一步还在跑时这一句**根本没有执行**，注记必须这么说。
   它只对 runner 派发（plan 无 steps = 后端一个工具都没跑）可达：图内已执行的 plan
   在 busy 闸之前的渲染通道就被接走（act.js，2026-08-04）——这句注记永远不会
   贴在一次真实发生过的写入上。 */
export const ACT_BUSY_NOTE = "上一步还在执行，这一句没有执行";

/* 「差额」的第二类：`said == used` 但 `used ≠ 实际产物`。
   `webapp.py` 里 `candidate_uids = ordered_uids[:limit]` —— limit 只是**上限**。
   说「打包前50条」而库里只有 12 条时没有任何 delta，但回执绝不能写「已打包 50 条」。 */
export function actSecondOrderGaps(promised, actual, commands) {
    const out = [];
    const p = Number(promised) || 0, a = Number(actual) || 0;
    if (p > 0 && a > 0 && a < p) {
        out.push("你说的是 " + p + " 条，这一批实际只有 " + a + " 条可用，装进去的是 " + a + " 条。");
    }
    if (a > 0 && commands != null && Number(commands) < a) {
        out.push("这 " + a + " 条里只有 " + Number(commands) + " 条能生成下载命令，其余的来源没有可直接下载的文件。");
    }
    return out;
}

/* 「差额」的第一类：`said ≠ used`（说了 80 被钳到 50 / 说了 20 但只检索到 12）。
   任务包面板的计数注记（task_pack tpCountNoteZh）用的就是这三态措辞；
   静默按 min(说的, 实际) 执行而不吭声，正是本项目反复修过的「静默偏离」。 */
export function actCountNoteZh(said, used) {
    said = Number(said) || 0; used = Number(used) || 0;
    if (!said || said === used) return "";
    if (said > 50 && used === 50) return "你说的是 " + said + " 条，一次最多打包 50 条，这次装了 50 条。";
    if (said > 50) return "你说的是 " + said + " 条，一次最多打包 50 条；这次实际只有 " + used + " 条可装。";
    return "你说的是 " + said + " 条，这次检索只有 " + used + " 条可装，装了 " + used + " 条。";
}

/* 「另有 N 个文件没有列入」的唯一构造点（任务包面板汇总句与执行回执共用）：
   按传入的勾选集算 Σ max(0, n_files_total − n_files_selected)。
   返回片段**不带**前导「；」与句末「。」——面板把它嵌进「共 N 个主文件；…。」长句，
   执行回执自成一句（调用方各自补标点），措辞本体只在这里有一份。 */
export function actExcludedFilesNote(items) {
    let excluded = 0;
    (items || []).forEach(function (it) {
        excluded += Math.max(0, ((it && it.n_files_total) || 0) - ((it && it.n_files_selected) || 0));
    });
    return excluded > 0 ? "这几个数据集的来源清单里另有 " + excluded + " 个文件没有列入" : "";
}

/* 槽位来源五态里，`said` / `default` 折叠不说，其余三态**必须**出现在回执里。
   这是「不许静默偏离」第一次有机械落点。 */
export const ACT_SOURCE_ZH = {
    clamped: "被截到了上限",
    guessed: "是大模型替你填的（你这句话里没有这个数）",
    dropped: "没能用上，改用了默认值",
};

export function actSlotSourceNotes(plan) {
    const out = [];
    const sources = (plan && plan.slot_sources) || {};
    /* `deltas` 里已经逐字说过的槽位不再说第二遍。后端对 clamped / dropped 是**两处都填**的
       （一条 delta + 一个 slot_source），照单全收就会在同一栏里把同一件事说两遍，
       用户读到的是「两处偏离」。本仓库在 coverage_caveats 上栽过一次同形的「双算」。 */
    const spoken = {};
    ((plan && plan.deltas) || []).forEach(function (d) { if (d && d.slot) spoken[d.slot] = true; });
    Object.keys(sources).forEach(function (slot) {
        if (spoken[slot]) return;
        const zh = ACT_SOURCE_ZH[sources[slot]];
        if (zh) out.push("「" + slot + "」这一项" + zh + "。");
    });
    return out;
}

/* 字节数人性化（task_pack / act 共用单一真源；此前住 task_pack.js，C0 归入纯核）。 */
export function tpBytes(size) {
    if (!size) return "未知";
    let value = size;
    const units = ["B", "KB", "MB", "GB", "TB"];
    for (let i = 0; i < units.length; i += 1) {
        if (value < 1024 || i === units.length - 1) return value.toFixed(1) + " " + units[i];
        value /= 1024;
    }
    return size + " B";
}

/* ---------------- 执行披露（归信息流） ----------------
   2026-08-18 上线的「执行了 N 次检索」摘要句通道（actToolSummary + .cbh-exec-summary）
   已随信息流结构纠偏整体退役：工具调用计数压缩句由 core/flow_trace.js 的
   compressFlow 产出（同口径的 FLOW_TOOL_KIND 平铺表在那里维护），渲染为回执气泡上方
   可展开的一行（entry.flow）。本文件不再做工具计数统计。 */

/* ---------------- 回执两行 ----------------
   2026-08-02：六行精简成两行——「做了什么」+「没做到的」。
   - 「依据」删除：卡片抬头「你说：…」已逐字引了原话，再引一遍是纯重复。
   - 「和你说的不一样 / 口径 / 要留意」三栏合并为「没做到的」：用户要的是
     「我可能期望、但系统没做到的」，一栏简明说尽。内容**一行不少**——
     「不许静默偏离」的铁律不变，变的只是分栏。
   - 手动 backup（打开面板自己挑）照旧挂在最新一张卡上。
     （2026-08-16：「按原话重搜 / 别自动执行」两颗 chip 退役，agent 能力已足够。） */

export function actWhatHappened(plan, outcome) {
    const lead = actLeadZh(outcome.ok, plan.verb_zh);
    if (!outcome.ok) return lead + "（" + String(outcome.error || "原因未知") + "）";
    const a = outcome.artifact || {};
    const bits = [];
    if (a.n_datasets != null) bits.push(a.n_datasets + " 个数据集");
    if (a.n_records != null) bits.push("记录 " + a.n_records + " 条");
    if (a.commands != null) bits.push("下载命令 " + a.commands + " 条");
    if (a.n_files != null) bits.push("共 " + a.n_files + " 个文件");
    if (a.n_recycle != null) bits.push("回收站 " + a.n_recycle + " 个");
    if (a.n_sources != null) bits.push("来源 " + a.n_sources + " 个");
    if (a.dataset_name) bits.push("「" + a.dataset_name + "」");
    if (a.restored_name) bits.push("文件 " + a.restored_name);   // 移回回执显示库里恢复原名的叶子（2026-08-05：此前显示回收站时间戳全名，前缀叠加冗长）
    else if (a.filename) bits.push("文件 " + a.filename);
    if (a.bytes != null) bits.push(tpBytes(a.bytes));
    return lead + (bits.length ? "：" + bits.join(" · ") : "") + "。";
}

export function actReceiptFrom(plan, outcome, utterance) {
    const rows = [];
    rows.push({ k: "做了什么", v: [actWhatHappened(plan, outcome)] });

    // 「没做到的」＝用户可能期望、但系统没做到/做的不一样的事（三栏合一，内容不少一行）。
    const gaps = (plan.deltas || []).map(function (d) {
        return "「" + d.slot + "」你说的是 " + d.said + "，实际用的是 " + d.used + "：" + d.why_zh;
    }).concat(actSlotSourceNotes(plan)).concat(outcome.gaps || [])
        .concat((outcome.policy || []).slice());

    if (plan.caveat_zh) gaps.push(plan.caveat_zh);
    if (plan.uncertainty_zh) gaps.push(plan.uncertainty_zh);
    if (plan.confidence === "low" && !plan.caveat_zh) {
        gaps.push("这一句大模型自己也拿不太准，请核对上方原话是不是你要的。");
    }
    if ((plan.rejected || []).length) {
        gaps.push("你这句话里还提到了本工具做不了的动作：" + plan.rejected.join("、") + "，这几项没有做。");
    }
    if (outcome.onDisk) gaps.push("文件已经在你的下载目录里，本工具删不掉它。");
    if (gaps.length) rows.push({ k: "没做到的", v: gaps });

    if (outcome.extra) rows.push({ k: "内容", v: [outcome.extra] });
    return { rows: rows, utterance: String(utterance || ""), ok: !!outcome.ok };
}
