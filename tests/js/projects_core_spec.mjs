"use strict";

/* ============================================================================
 * projects_core_spec.mjs —— 课题 UI 纯逻辑核心「真行为」规格（node 跑）
 * ----------------------------------------------------------------------------
 * 由 tests/test_projects_contract.py 经 `node <this>` 驱动；断言失败 → 非零退出。
 * 存在意义：web_smoke 只静态查字符串、node --check 只验语法，两门都测不出「存为课题」
 * 的规格构造是否与 /api/watch/check 入参逐字段对齐、上下文卡序列化是否守 2000 字
 * 硬 cap 且不静默截断、上次检查文案是否如实——这些是用户可见核心逻辑，错一处全歪。
 *
 * 纯函数直测：projects_core.js 只相对 import artifacts.js（同为零 DOM 纯模块），
 * node 直接相对路径 import，无 fake IndexedDB 需求。
 * ========================================================================== */

import * as P from "../../web/static/js/core/projects_core.js";
import * as A from "../../web/static/js/core/artifacts.js";

const T0 = 1753000000000;   // 固定基准（与 artifacts_spec 同源）
let failures = 0;
function check(name, cond, detail) {
    if (cond) { console.log(`  ok   ${name}`); }
    else { failures++; console.log(`  FAIL ${name}${detail ? "  —— " + detail : ""}`); }
}

/* ============================================================================
 * 1. 规格构造（check_condition.spec ↔ /api/watch/check 入参逐字段对齐）
 * ========================================================================== */
function specSuite() {
    const spec = P.projectsSpecFromRequest({
        query: "人类 肺癌",
        sources: ["10x", "CELLxGENE", "10x"],
        facet_filters: [{ dim: "物种", value: "人类" }, { dim: "物种", value: "人类" }, { dim: "组织", value: "肺" }, { dim: "", value: "脏" }, { dim: "空值", value: "  " }],
        suppressed_constraints: ["exclude:物种", " exclude:物种 "],
        lenient_dims: ["species"],
        date_from: "2024-01-01",
        date_to: "2024-12-31",
    });
    check("spec_version 恒 v1", spec.spec_version === "v1");
    check("query 收白", spec.query === "人类 肺癌");
    check("sources 去重保序", JSON.stringify(spec.sources) === JSON.stringify(["10x", "CELLxGENE"]), JSON.stringify(spec.sources));
    check("facet_filters 单值去重", spec.facet_filters.length === 2
        && spec.facet_filters[0].dim === "物种" && spec.facet_filters[0].value === "人类"
        && spec.facet_filters[1].dim === "组织" && spec.facet_filters[1].value === "肺", JSON.stringify(spec.facet_filters));
    check("suppressed/lenient 收白", JSON.stringify(spec.suppressed_constraints) === JSON.stringify(["exclude:物种"])
        && JSON.stringify(spec.lenient_dims) === JSON.stringify(["species"]));
    check("日期收白", spec.date_from === "2024-01-01" && spec.date_to === "2024-12-31");
    check("空入参构造空 spec（不抛）", (() => {
        const s = P.projectsSpecFromRequest(null);
        return s.spec_version === "v1" && s.query === "" && s.sources.length === 0
            && s.facet_filters.length === 0 && s.date_from === "" && s.date_to === "";
    })());
}

/* ============================================================================
 * 2. 「存为课题」草稿构造
 * ========================================================================== */
function draftSuite() {
    P.projectsSetClock(() => T0);
    const d = P.projectsDraftFromSearch({
        query: " 人类肺癌，要 FASTQ 的 ",
        uids: ["u1", "u2", "u1", ""],
        specParts: {
            query: "人类 肺癌", sources: ["10x"], facet_filters: [{ dim: "物种", value: "人类" }],
            suppressed_constraints: [], lenient_dims: [], date_from: "", date_to: "",
        },
        provenanceParts: {
            query: "人类肺癌，要 FASTQ 的",
            retrieval_params: { top_k: 10, strategy: "auto" },
            policy_id: "auto/llm/off@20260822-ad1",
            result: { uids: ["u1", "u2"], truncated: false },
        },
    }, { now: T0, project_id: "prj-fixed" });
    const inp = d.input;
    check("project_id 可注入", inp.project_id === "prj-fixed");
    check("名称取检索句（长句截断）", inp.name === "人类肺癌，要 FASTQ 的");
    check("目标即检索句", inp.goal === "人类肺癌，要 FASTQ 的");
    check("候选 uid 去重且默认待核验", inp.candidates.length === 2
        && inp.candidates.every((c) => c.status === "待核验")
        && inp.candidates[0].uid === "u1" && inp.candidates[1].uid === "u2");
    check("check_condition 带 display_query+spec，baseline=null", inp.check_condition !== null
        && inp.check_condition.display_query === "人类肺癌，要 FASTQ 的"
        && inp.check_condition.spec.query === "人类 肺癌"
        && inp.check_condition.spec.facet_filters[0].value === "人类"
        && inp.check_condition.baseline === null && inp.check_condition.last_checked_at === "");
    check("provenance 落齐", inp.provenance.query === "人类肺癌，要 FASTQ 的"
        && inp.provenance.retrieval_params.top_k === 10
        && JSON.stringify(inp.provenance.result.uids) === JSON.stringify(["u1", "u2"])
        && inp.provenance.result.truncated === false
        && inp.provenance.policy_id === "auto/llm/off@20260822-ad1");
    check("provenance.retrieved_at 由时钟落戳", inp.provenance.retrieved_at === new Date(T0).toISOString());
    check("空查询兜底命名", P.projectsDraftFromSearch({ query: "", uids: [] }, { now: T0 }).input.name === "未命名追踪");
    // 规整往返：草稿可直接过 artifacts 校验（写库前的形状契约）
    const errs = A.artifactsValidateProject(A.artifactsNormalizeProject(inp));
    check("草稿过 artifacts 校验", errs.length === 0, JSON.stringify(errs));
}

/* ============================================================================
 * 3. 上下文卡序列化（≤2000 硬 cap / 字段优先级截断 / 不静默截断）
 * ========================================================================== */
function ctxSuite() {
    P.projectsSetClock(() => T0);
    // 3.1 全量注入（未超限）：目标+纳入+排除+候选 uid:状态
    const full = {
        goal: "找肺癌空间转录组",
        include_conditions: ["人类", "肺癌"],
        exclude_conditions: ["小鼠"],
        candidates: [
            { uid: "u1", status: "待核验" }, { uid: "u2", status: "已核验" },
            { uid: "u3", status: "已排除" }, { uid: "u4", status: "候选" },
        ],
    };
    const r1 = P.projectsContextSerialize(full);
    check("全量注入不截断、omitted=0", r1.omitted === 0 && r1.text.includes("研究目标：找肺癌空间转录组")
        && r1.text.includes("纳入条件：1. 人类；2. 肺癌") && r1.text.includes("排除条件：1. 小鼠")
        && r1.text.includes("候选：u1:待核验；u2:已核验；u3:已排除；u4:候选"), r1.text);

    // 3.2 候选 >20：只注入前 20，omitted 如实计
    const many = { goal: "g", include_conditions: [], exclude_conditions: [], candidates: [] };
    for (let i = 0; i < 25; i += 1) many.candidates.push({ uid: "c" + i, status: "待核验" });
    const r2 = P.projectsContextSerialize(many);
    check("候选 >20 注入 20 条、omitted=5", r2.omitted === 5
        && !r2.text.includes("c20") && r2.text.includes("c19") && r2.text.includes("c0"), r2.text);

    // 3.3 硬 cap：长目标 + 长候选列表压过 2000 字 → 先从候选尾部砍，仍超再砍排除/纳入
    const long = { goal: "目标".repeat(150), include_conditions: ["纳入".repeat(30)], exclude_conditions: ["排除".repeat(30)], candidates: [] };
    for (let i = 0; i < 20; i += 1) long.candidates.push({ uid: "很长uid" + i + "x".repeat(90), status: "待核验" });
    const r3 = P.projectsContextSerialize(long);
    check("硬 cap：text ≤2000 字符", Array.from(r3.text).length <= 2000, "len=" + Array.from(r3.text).length);
    check("硬 cap：omitted>0（不静默截断）", r3.omitted > 0, "omitted=" + r3.omitted);
    check("截断保字段优先级：目标恒在", r3.text.includes("研究目标：目标".repeat(1)) && r3.text.startsWith("研究目标：目标"), r3.text.slice(0, 40));
    // 3.4 候选 0：无候选段不渲染「候选：」
    const none = { goal: "g", include_conditions: [], exclude_conditions: [], candidates: [] };
    const r4 = P.projectsContextSerialize(none);
    check("无候选不渲染候选段", !r4.text.includes("候选：") && r4.text === "研究目标：g", JSON.stringify(r4.text));
}

/* ============================================================================
 * 4. 上次检查文案 / 天数
 * ========================================================================== */
function timeSuite() {
    P.projectsSetClock(() => T0);
    const iso = (ms) => new Date(ms).toISOString();
    check("无检查条件 → 空串", P.projectsLastCheckedText({ check_condition: null }, T0) === "");
    check("check_condition 无 last_checked_at → 空串", P.projectsLastCheckedText({ check_condition: { last_checked_at: "" } }, T0) === "");
    check("今天检查过", P.projectsLastCheckedText({ check_condition: { last_checked_at: iso(T0 - 3600000) } }, T0) === "今天检查过");
    check("1 天前", P.projectsLastCheckedText({ check_condition: { last_checked_at: iso(T0 - 86400000) } }, T0) === "上次检查于 1 天前");
    check("3 天前", P.projectsLastCheckedText({ check_condition: { last_checked_at: iso(T0 - 3 * 86400000) } }, T0) === "上次检查于 3 天前");
    check("未来戳 → 今天（不伪造负数）", P.projectsDaysAgo(iso(T0 + 86400000), T0) === 0);
    check("空串 → null", P.projectsDaysAgo("", T0) === null && P.projectsDaysAgo(null, T0) === null);
    const c = P.projectsStatusCounts([
        { status: "待核验" }, { status: "已核验" }, { status: "已核验" }, { status: "已排除" }, { status: "非法态" }, null,
    ]);
    check("状态计数四键恒在", c["待核验"] === 1 && c["已核验"] === 2 && c["已排除"] === 1 && c["候选"] === 0);
}

/* ============================================================================
 * 串行执行
 * ========================================================================== */
(async function main() {
    try {
        specSuite();
        draftSuite();
        ctxSuite();
        timeSuite();
    } catch (e) {
        failures++;
        console.log("  FAIL suite 顶层抛错：", e && e.stack ? e.stack : e);
    }
    console.log(failures ? `\n${failures} 条失败` : "\n全部通过\nOK projects_core_spec.mjs");
    process.exit(failures ? 1 : 0);
})();
