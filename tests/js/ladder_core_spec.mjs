"use strict";

/* ============================================================================
 * ladder_core_spec.mjs —— 下一步行动纯逻辑核「真行为」规格（node 跑）
 * ----------------------------------------------------------------------------
 * 由 tests/test_ladder_contract.py 经 `node <this>` 驱动；断言失败 → 非零退出。
 * 覆盖：chip 选取规则（2–4 颗/优先级/needsAgent 隐藏/收藏门/raw_only 门）、
 * 过宽收窄建议（阈值/固定维度跳过/顶部取值）、template_originated 判定、任务卡状态构造。
 * 本规格零 DOM、零网络、零 localStorage——纯函数直测。
 * ========================================================================== */

const core = await import(new URL("../../web/static/js/search/ladder_core.js", import.meta.url));

let failed = 0;
function check(label, ok, detail) {
    if (ok) console.log("PASS", label);
    else { failed += 1; console.error("FAIL", label, detail || ""); }
}

/* ---------- 0. 常量 ---------- */
check("收窄阈值=100（设计约定 例）", core.LADDER_NARROW_TOTAL_MIN === 100);
check("chip 上限=4（设计约定的 2–4 颗）", core.LADDER_MAX_CHIPS === 4);
check("只看原始数据可用的分面值与后端 has_raw_data 取值同源",
    core.LADDER_RAW_ONLY_FACET.dim === "has_raw_data" && core.LADDER_RAW_ONLY_FACET.value === "有 FASTQ");

/* ---------- 1. LADDER_RECIPES 形状 ---------- */
{
    const ids = Object.keys(core.LADDER_RECIPES);
    check("recipe 表非空", ids.length >= 5, ids.join(","));
    let allShaped = true;
    ids.forEach(function (id) {
        const r = core.LADDER_RECIPES[id];
        if (!r || !r.verb || !r.chipLabel || !r.template || !r.scopeZh || !r.outputZh || !r.networkZh
            || typeof r.needsAgent !== "boolean") allShaped = false;
    });
    check("每项 recipe 含 verb/chipLabel/template/三段说明/needsAgent", allShaped);
    check("recipe id 与后端 allowlist 同形（后端校验在 pytest 契约门）",
        ids.indexOf("compare_datasets") >= 0 && ids.indexOf("feasibility") >= 0);
}

/* ---------- 2. chip 选取：无结果屏 ---------- */
{
    const out = core.ladderSelect({ resolution_status: "no_match", results: [] }, { agentOn: true });
    check("无结果 → 空 chips", out.chips.length === 0);
    const out2 = core.ladderSelect({ resolution_status: "results", results: [] }, { agentOn: true });
    check("结果数组空 → 空 chips", out2.chips.length === 0);
}

/* ---------- 3. chip 选取：正常结果屏 ---------- */
function baseData(n, extra) {
    const data = {
        resolution_status: "results",
        results: Array.from({ length: n }, function (_, i) {
            return { dataset_uid: "uid" + (i + 1), dataset_name: "d" + (i + 1) };
        }),
        result_total: n,
        query_constraints: [],
        facets: [
            { dim: "species", label: "物种", values: [{ value: "homo sapiens", display: "Homo sapiens", count: 30 }, { value: "mus musculus", display: "Mus musculus", count: 10 }] },
            { dim: "has_raw_data", label: "原始数据", values: [{ value: "有 FASTQ", display: "有 FASTQ", count: 25 }, { value: "无 FASTQ", display: "无 FASTQ", count: 15 }] },
        ],
    };
    return Object.assign(data, extra || {});
}
{
    const ctx = { agentOn: true, favCount: 0, facetDims: [], rawConstrained: false };
    const out = core.ladderSelect(baseData(10), ctx);
    check("2 条以上结果 + agent 开 → 至少 3 颗（raw_only/compare/fair 均在池）", out.chips.length >= 3, out.chips.map(c => c.id).join(","));
    check("池上限 4 颗", out.chips.length <= 4, String(out.chips.length));
    check("首颗优先 raw_only（确定性顺序）", out.chips[0] && out.chips[0].id === "raw_only");
    check("raw_only 是 action 类（直接执行）", out.chips[0].kind === "action" && out.chips[0].recipe === null);
    const recipe = out.chips.find(function (c) { return c.id === "compare_datasets"; });
    check("compare_datasets 绑定 recipe 且带模板", recipe && recipe.recipe === "compare_datasets" && !!recipe.template);
    check("raw_only 出现在前、recipe chips 在后（顺序确定）",
        out.chips.findIndex(c => c.id === "raw_only") < out.chips.findIndex(c => c.id === "compare_datasets"));
}
{
    const ctx = { agentOn: false, favCount: 0, facetDims: [], rawConstrained: false };
    const out = core.ladderSelect(baseData(10), ctx);
    check("agent 关 → 无 needsAgent chips（compare/fair/plain 隐藏）",
        out.chips.every(function (c) { return c.needsAgent !== true; }), out.chips.map(c => c.id).join(","));
    check("agent 关 → raw_only 仍在（① 本地确定性不依赖 agent）",
        out.chips.some(function (c) { return c.id === "raw_only"; }));
}
{
    const ctx = { agentOn: true, favCount: 0, facetDims: ["has_raw_data"], rawConstrained: false };
    const out = core.ladderSelect(baseData(10), ctx);
    check("已应用 has_raw_data 分面 → raw_only 隐藏（不重复筛）",
        out.chips.every(function (c) { return c.id !== "raw_only"; }));
}
{
    const ctx = { agentOn: true, favCount: 0, facetDims: [], rawConstrained: true };
    const out = core.ladderSelect(baseData(10), ctx);
    check("查询已固定原始数据 → raw_only 隐藏", out.chips.every(function (c) { return c.id !== "raw_only"; }));
}
{
    const data = baseData(10, { facets: [{ dim: "species", label: "物种", values: [{ value: "homo sapiens", display: "Homo sapiens", count: 40 }, { value: "mus musculus", display: "Mus musculus", count: 10 }] }] });
    const ctx = { agentOn: true, favCount: 0, facetDims: [], rawConstrained: false };
    const out = core.ladderSelect(data, ctx);
    check("无 has_raw_data 分面组 → raw_only 隐藏（没有可筛的差异）",
        out.chips.every(function (c) { return c.id !== "raw_only"; }));
    check("无 raw_only 时首颗为 compare_datasets（优先级顺延）",
        out.chips[0] && out.chips[0].id === "compare_datasets");
}
{
    // 有收藏 → reuse_pack 进入候选池：agent 关 + 1 条结果 + 无 has_raw_data 组时，
    // 非 agent 候选（file_list/feasibility/manifest/reuse_pack）恰好 4 颗全进。
    const data = baseData(1, { facets: [{ dim: "species", label: "物种", values: [{ value: "homo sapiens", display: "Homo sapiens", count: 1 }] }] });
    const ctx = { agentOn: false, favCount: 3, facetDims: [], rawConstrained: false };
    const out = core.ladderSelect(data, ctx);
    check("有收藏 → reuse_pack 进入候选池", out.chips.some(function (c) { return c.id === "reuse_pack"; }), out.chips.map(c => c.id).join(","));
    const ctx0 = { agentOn: false, favCount: 0, facetDims: [], rawConstrained: false };
    const out0 = core.ladderSelect(data, ctx0);
    check("无收藏 → reuse_pack 隐藏", out0.chips.every(function (c) { return c.id !== "reuse_pack"; }));
}
{
    // 只有 1 条结果：compare（≥2 条）隐藏，fair 在
    const ctx = { agentOn: true, favCount: 0, facetDims: [], rawConstrained: false };
    const out = core.ladderSelect(baseData(1), ctx);
    check("1 条结果 → 无 compare_datasets",
        out.chips.every(function (c) { return c.id !== "compare_datasets"; }));
    check("1 条结果 → fair_check 在", out.chips.some(function (c) { return c.id === "fair_check"; }));
}
{
    // 相同输入两次 → 相同输出（确定性）
    const a = core.ladderSelect(baseData(10), { agentOn: true, favCount: 0, facetDims: [], rawConstrained: false });
    const b = core.ladderSelect(baseData(10), { agentOn: true, favCount: 0, facetDims: [], rawConstrained: false });
    check("选取确定性（两次同输入同输出）",
        JSON.stringify(a.chips.map(c => c.id)) === JSON.stringify(b.chips.map(c => c.id)));
}

/* ---------- 4. 过宽收窄建议 ---------- */
{
    const data = baseData(10, { result_total: 428 });
    const out = core.ladderNarrowSuggestions(data);
    check("total=428>100 → 有建议", out.length >= 1, JSON.stringify(out));
    check("建议 ≤2 条", out.length <= 2);
    check("首选物种（LADDER_NARROW_DIMS 顺序）", out[0] && out[0].dim === "species");
    check("建议取该维度出现最多的取值（homo sapiens）", out[0] && out[0].value === "homo sapiens");
    check("建议带计数与 total", out[0] && out[0].count === 30 && out[0].total === 428);
}
{
    const out = core.ladderNarrowSuggestions({ result_total: 50, facets: [], query_constraints: [] });
    check("total=50 ≤100 → 无建议（不过宽不打扰）", out.length === 0);
    const out2 = core.ladderNarrowSuggestions({ result_total: 428, facets: [], query_constraints: [] });
    check("无分面组 → 无建议", out2.length === 0);
    const out3 = core.ladderNarrowSuggestions({ result_total: 428, query_constraints: [], facets: [{ dim: "species", label: "物种", values: [{ value: "homo sapiens", display: "Homo sapiens", count: 428 }] }] });
    check("单值维度（无可收窄）→ 无建议", out3.length === 0);
}
{
    // 查询已固定物种（query_constraints 消费后端解析结果，不写 JS 关键词解析器）→ 建议落到组织
    const data = baseData(10, {
        result_total: 428,
        query_constraints: [{ dim: "species", filter_id: "include:species", label: "物种", values: ["homo sapiens"] }],
        facets: [
            { dim: "species", label: "物种", values: [{ value: "homo sapiens", display: "Homo sapiens", count: 300 }] },
            { dim: "tissue", label: "组织", values: [{ value: "lung", display: "肺", count: 120 }, { value: "blood", display: "血液", count: 90 }] },
            { dim: "has_raw_data", label: "原始数据", values: [{ value: "有 FASTQ", display: "有 FASTQ", count: 25 }, { value: "无 FASTQ", display: "无 FASTQ", count: 15 }] },
        ],
    });
    const out = core.ladderNarrowSuggestions(data);
    check("物种已被查询固定 → 建议跳过物种、落到组织", out.length >= 1 && out[0] && out[0].dim === "tissue", JSON.stringify(out));
}

/* ---------- 5. template_originated ---------- */
check("模板原样 → true", core.ladderTemplateOriginated("对比前两条", "对比前两条") === true);
check("模板 trim 差异 → true（纯空白不算编辑）", core.ladderTemplateOriginated(" 对比前两条 ", "对比前两条") === true);
check("编辑过 → false", core.ladderTemplateOriginated("对比前两条", "对比前三条") === false);
check("空模板空文本 → true（无内容无编辑）", core.ladderTemplateOriginated("", "") === true);

/* ---------- 6. 任务卡状态构造 ---------- */
{
    const chip = { recipe: "fair_check", template: "检查第一条", scopeZh: "s", outputZh: "o", networkZh: "n" };
    const st = core.ladderTaskCardState(chip);
    check("recipe 卡状态带 recipe/模板/三段说明",
        st.recipe === "fair_check" && st.template === "检查第一条" && st.scopeZh === "s" && st.outputZh === "o" && st.networkZh === "n");
    const st0 = core.ladderTaskCardState(null);
    check("null → 空状态（普通路由无 recipe）", st0.recipe === null && st0.template === "");
}

/* ---------- 7. 导出 chips 描述 ---------- */
check("导出 chips 为两颗（下载清单/引文），id 供埋点",
    core.LADDER_EXPORT_CHIPS.length === 2
    && core.LADDER_EXPORT_CHIPS[0].id === "export_manifest"
    && core.LADDER_EXPORT_CHIPS[1].id === "export_citations");

if (failed) {
    console.error("LADDER_CORE_SPEC_FAILED", failed);
    process.exit(1);
}
console.log("LADDER_CORE_SPEC_OK");
