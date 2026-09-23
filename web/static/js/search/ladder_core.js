"use strict";

/* 下一步行动 · 结果页阶梯纯核——零 DOM / 零网络 /
 * 零存储 / 零 `#` import，node 可单测（tests/js/ladder_core_spec.mjs）。
 *
 * 职责边界：
 * - 这里是「选哪几颗 chip、收窄建议怎么算、模板 origination 怎么判」的全部规则，
 *   纯函数、确定性、零 LLM——UI 壳（ladder.js）与任务卡弹窗（task_card.js）只负责
 *   渲染与事件接线，任何「要不要显示 / 显示什么」的判据都不得在壳里另写一份。
 * - suggested_recipe 的**服务端单一真源**是 `action_plan.SUGGESTED_RECIPES`（Python）；
 *   本文件 LADDER_RECIPES 是它的前端镜像（文案/模板/范围说明），id 与动词必须 ⊆ 后端
 *   （契约门 `tests/test_suggested_recipe.py` 钉死，防止两端各抄一份后漂移）。
 *
 * ## 三类行为（表格严格照做）
 * ① 本地确定性低成本 → 直接执行（「只看原始数据可用」= 直接套既有 has_raw_data 分面）；
 * ② 确定性导出 → 直接生成（「导出下载清单 / 导出引文」——探测式依赖导出中心，
 *    壳层探测不到时整颗隐藏，见 ladder.js 的导出探测注释）；
 * ③ 需 LLM / 多步 → 打开可编辑任务卡（绑定既有能力 recipe 或纯普通路由）。
 * 不做任何「chip 填输入框」的迂回。
 */

/* 结果过宽阈值：result_total 严格大于该值才显示收窄建议（「如 total>100」）。 */
export const LADDER_NARROW_TOTAL_MIN = 100;

/* 阶梯 chip 选取上限（「规则式确定性选取 2–4 颗」：最多 4 颗）。 */
export const LADDER_MAX_CHIPS = 4;

/* 收窄建议优先维度顺序（如「限定『物种』或『组织』」）。 */
export const LADDER_NARROW_DIMS = ["species", "tissue", "disease"];

/* 只看原始数据可用：直接套用的分面取值（与后端 retriever.facets 的 has_raw_data
   取值同源：`{True: "有 FASTQ", False: "无 FASTQ"}`）。 */
export const LADDER_RAW_ONLY_FACET = { dim: "has_raw_data", value: "有 FASTQ", display: "有 FASTQ", label: "原始数据" };

/* 前端镜像 allowlist（服务端单一真源 = action_plan.SUGGESTED_RECIPES）。
 * 每项：verb（既有已验证能力）、chipLabel（按钮文案直接写结果，不用抽象词）、
 * needsAgent（依赖 agent 图执行 → AI 执行关闭时隐藏）、template（任务卡模板文本，
 * 未经编辑发送时携带 suggested_recipe + template_originated=true）、
 * scopeZh/outputZh/networkZh（任务卡「说明范围 / 预计输出 / 联网行为」三段说明）。 */
export const LADDER_RECIPES = {
    compare_datasets: {
        verb: "compare.datasets",
        chipLabel: "对比前两条",
        needsAgent: true,
        template: "对比当前结果的前两条数据集（只比较元数据字段，不评价哪个更好）",
        scopeZh: "当前结果前两条（可按文本改为任意两条）",
        outputZh: "一张字段级对比表（名称/来源/物种/组织/疾病/平台/样本量/发表时间/文件数）",
        networkZh: "只读本地元数据，不访问外部网站",
    },
    fair_check: {
        verb: "fair.check",
        chipLabel: "检查 FAIR 就绪度",
        needsAgent: true,
        template: "检查当前结果第一条数据集的 FAIR 就绪度（13 项元数据自检 + 投稿数据可用性声明）",
        scopeZh: "当前结果第一条（可按文本改为任意一条）",
        outputZh: "13 项 FAIR 自检：每项 pass/partial/unknown + 改进建议 + 投稿数据可用性声明",
        networkZh: "只读本地元数据，不访问外部网站",
    },
    compat_find: {
        verb: "compat.find",
        chipLabel: "找兼容数据集",
        needsAgent: true,
        template: "给当前结果第一条数据集找元数据上兼容的其它数据集（共享物种，且平台或 chemistry 相同）",
        scopeZh: "当前结果第一条（可按文本改为任意一条）",
        outputZh: "兼容数据集清单（结论恒带「必要非充分」的诚实边界）",
        networkZh: "只读本地元数据，不访问外部网站",
    },
    feasibility: {
        verb: "feasibility.run",
        chipLabel: "统计数据总量",
        needsAgent: false,
        template: "统计当前这批结果的数据量：候选数、总细胞量下限、来源/物种/平台分布与缺口",
        scopeZh: "当前这批结果（全部命中，不只已展示的前几条）",
        outputZh: "候选数 / 总细胞量下限 / 来源、物种、平台、年份分布 / 缺口说明",
        networkZh: "只读本地元数据，不访问外部网站",
    },
    manifest: {
        verb: "pack.download",
        chipLabel: "生成下载清单",
        needsAgent: false,
        template: "把当前这批结果打包成下载清单（每个数据集一个代表性主文件）",
        scopeZh: "当前这批结果（默认前 10 条，可按文本改为条数）",
        outputZh: "下载清单 / 下载脚本 / FAIR 自检 / 引文（既有打包能力）",
        networkZh: "生成清单只读本地元数据；实际下载文件时才会访问各数据源官网",
    },
    file_list: {
        verb: "files.show",
        chipLabel: "看第一条文件",
        needsAgent: false,
        template: "打开当前结果第一条数据集的文件清单",
        scopeZh: "当前结果第一条",
        outputZh: "该数据集的全部文件清单（文件名 / 大小 / 校验和 / 下载链接）",
        networkZh: "只读本地元数据，不访问外部网站",
    },
    reuse_pack: {
        verb: "reuse.pack",
        chipLabel: "整理投稿材料",
        needsAgent: false,
        template: "整理当前结果的投稿材料：数据可用性声明、复用出处清单与 RIS/BibTeX 引文",
        scopeZh: "当前这批结果（投稿材料从收藏与当前结果生成）",
        outputZh: "英文出处说明段落 + 引用清单 + RIS / BibTeX 引文文件",
        networkZh: "只读本地元数据，不访问外部网站",
    },
};

/* 无 hint 任务卡示例 chip（表格第 3 行的「核验前 N 条」）：**不新造后端 recipe**
 * （动词表没有「按纳入排除核验前 N 条并生成理由表」这类单一动作）——
 * 走「可编辑任务卡 + 普通路由」，发送不带 suggested_recipe。依赖 agent（LLM/多步），
 * AI 执行关闭时隐藏。 */
export const LADDER_PLAIN_TASK = {
    id: "verify_samples",
    chipLabel: "核验样本量与可得性",
    needsAgent: true,
    recipe: null,
    template: "核验当前结果前 10 条的样本量与原始数据可得性，逐条给出结论与核验时间",
    scopeZh: "当前结果前 10 条（可按文本改为任意条数）",
    outputZh: "逐条核验结论：样本量（是否标注/数值）+ 原始数据可得性（有/无 FASTQ）",
    networkZh: "只读本地元数据，不访问外部网站",
};

/* 导出中心的两颗确定性导出 chip：探测式依赖导出中心——
   壳层动态 import 导出中心模块成功才渲染（存在时接导出能力，选最简可靠路径），否则整颗隐藏、
   不报错。id 供埋点 ladder_clicked{action} 用。 */
export const LADDER_EXPORT_CHIPS = [
    { id: "export_manifest", label: "导出下载清单", kind: "export" },
    { id: "export_citations", label: "导出引文", kind: "export" },
];

/* 单颗 chip 描述：{kind, id, recipe?, template, scopeZh, outputZh, networkZh, needsAgent}。
   kind："action"（① 直接执行）/ "recipe"（③ 绑定 recipe 的任务卡）/ "plain"（③ 无 hint
   任务卡）/ "export"（② 确定性导出，导出探测通过才由壳层渲染）。 */
function _chip(kind, id, extra) {
    return Object.assign({ kind: kind, id: id }, extra || {});
}

/* 规则式确定性选取 2–4 颗（零 LLM）。输入：
   - data：/api/recommend 响应（读 resolution_status/results/facets/query_constraints/result_total）；
   - ctx：{facetDims: 已应用分面 dim 数组, rawConstrained: 查询是否已固定原始数据,
     favCount: 收藏条数, agentOn: 「AI 执行」是否可用}。
   返回 {chips: chip[]}（chips 恒按下面固定优先级排序；无结果屏返回空）。 */
export function ladderSelect(data, ctx) {
    const hasResults = !!(data && data.resolution_status === "results"
        && Array.isArray(data.results) && data.results.length > 0);
    if (!hasResults) return { chips: [] };
    ctx = ctx || {};
    const facetDims = Array.isArray(ctx.facetDims) ? ctx.facetDims : [];
    const agentOn = ctx.agentOn !== false;
    const nResults = data.results.length;

    const pool = [];
    // ① 直接执行：只看原始数据可用——分面组存在「有 FASTQ」且未固定/未应用时才有意义。
    const rawGroup = (Array.isArray(data.facets) ? data.facets : [])
        .find((g) => g && g.dim === "has_raw_data" && Array.isArray(g.values));
    const rawHasFilter = rawGroup && rawGroup.values.some((v) => v && v.value === "有 FASTQ");
    if (rawHasFilter && !ctx.rawConstrained && !facetDims.includes("has_raw_data")) {
        pool.push(_chip("action", "raw_only", {
            chipLabel: "只看原始数据可用", scopeZh: "当前这批结果",
            outputZh: "筛出有 FASTQ 原始数据的结果，去掉没有原始数据的", networkZh: "仅本地筛选，不联网",
            recipe: null,
        }));
    }
    // ③ 绑定既有能力 recipe 的任务卡 chips（needsAgent 的依赖 agent 图执行）。
    const recipeChips = [
        ["compare_datasets", nResults >= 2],
        ["fair_check", nResults >= 1],
        ["compat_find", nResults >= 1],
        ["feasibility", nResults >= 1],
        ["manifest", nResults >= 1],
        ["file_list", nResults >= 1],
        ["reuse_pack", Number(ctx.favCount) > 0],
    ];
    recipeChips.forEach(function (pair) {
        const id = pair[0], eligible = pair[1];
        if (!eligible) return;
        const rec = LADDER_RECIPES[id];
        if (rec.needsAgent && !agentOn) return;   // AI 执行关闭：依赖 agent 的 chip 隐藏
        pool.push(_chip("recipe", id, {
            recipe: id, chipLabel: rec.chipLabel, needsAgent: rec.needsAgent,
            template: rec.template, scopeZh: rec.scopeZh, outputZh: rec.outputZh,
            networkZh: rec.networkZh,
        }));
    });
    // 无 hint 任务卡 chip（不新造 recipe）：同样依赖 agent。
    if (agentOn && nResults >= 1) {
        pool.push(_chip("plain", LADDER_PLAIN_TASK.id, {
            recipe: null, chipLabel: LADDER_PLAIN_TASK.chipLabel,
            needsAgent: true, template: LADDER_PLAIN_TASK.template,
            scopeZh: LADDER_PLAIN_TASK.scopeZh, outputZh: LADDER_PLAIN_TASK.outputZh,
            networkZh: LADDER_PLAIN_TASK.networkZh,
        }));
    }
    return { chips: pool.slice(0, LADDER_MAX_CHIPS) };
}

/* 过宽收窄建议：result_total 超阈值且存在「未被查询固定」的可细化维度时，
   返回 1–2 条 {dim, label, value, display, count, total}——点击直接套对应 facet 筛选
   （value 取该维度出现最多的分面值，点击后立即可见收窄效果）。
   - 被查询固定的维度判定消费响应内 query_constraints（**不另写 JS 关键词解析器**）；
   - 已应用的分面维度后端不会出现在 data.facets（retriever.facets 排除 active），天然覆盖。 */
export function ladderNarrowSuggestions(data) {
    if (!data || !(typeof data.result_total === "number") || data.result_total <= LADDER_NARROW_TOTAL_MIN) {
        return [];
    }
    const total = data.result_total;
    const constrained = new Set(
        (Array.isArray(data.query_constraints) ? data.query_constraints : [])
            .map(function (c) { return c && (c.dim || ""); })
            .filter(Boolean));
    const groups = Array.isArray(data.facets) ? data.facets : [];
    const picks = [];
    LADDER_NARROW_DIMS.forEach(function (dim) {
        if (picks.length >= 2) return;
        if (constrained.has(dim)) return;
        const g = groups.find(function (x) { return x && x.dim === dim && Array.isArray(x.values); });
        if (!g || g.values.length < 2) return;   // 单值维度无收窄意义
        const top = g.values[0];                 // facets.js 已按条数降序（year 除外，本表无 year）
        if (!top || !(Number(top.count) > 0)) return;
        if (Number(top.count) >= total) return;  // 顶部值占满全集 = 收窄无意义（防御）
        picks.push({
            dim: dim, label: String(g.label || dim), value: String(top.value),
            display: String(top.display || top.value || ""), count: Number(top.count), total: total,
        });
    });
    return picks;
}

/* template_originated 判定：任务卡/chip 生成文本**未经编辑直接发送** → true；
   编辑过 → false。按 trim 后的逐字比较（纯空白改动不算编辑）。 */
export function ladderTemplateOriginated(original, current) {
    return String(original || "").trim() === String(current || "").trim();
}

/* 任务卡初始状态构造（task_card.js 消费）：recipe 为 null 的 plain 卡不带 suggested_recipe；
   带 recipe 的卡在模板未被编辑时携带。 */
export function ladderTaskCardState(chip) {
    if (!chip) {
        return { recipe: null, template: "", scopeZh: "", outputZh: "", networkZh: "" };
    }
    return {
        recipe: chip.recipe || null,
        template: String(chip.template || ""),
        scopeZh: String(chip.scopeZh || ""),
        outputZh: String(chip.outputZh || ""),
        networkZh: String(chip.networkZh || ""),
    };
}
