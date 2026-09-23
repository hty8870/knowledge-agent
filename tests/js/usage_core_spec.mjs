"use strict";

/* ============================================================================
 * usage_core_spec.js —— 使用反馈聚合纯核「真行为」规格（node 跑）
 * ----------------------------------------------------------------------------
 * 由 tests/test_usage_telemetry_contract.py 经 `node <this>` 驱动；断言失败 → 非零退出。
 * 存在意义：web_smoke 只静态查字符串、node --check 只验语法，两门都测不出
 * **聚合出来的那段文字对不对**。而这个功能的全部产物就是那段文字 —— 它要是把
 * 「省略了 30 条查询」说成没省、把「AI 坏了」和「AI 没开」混成一句，
 * 反馈本身就成了误导来源。所以这里逐条断言真实输出。
 * 相对路径 import，避开中文路径入 argv。
 * ========================================================================== */

import * as U from "../../web/static/js/core/usage_core.js";

const T0 = 1753000000000;   // 固定基准，确定性（脚本内禁用 Date.now）
const MIN = 60000;

let failures = 0;
function check(name, cond, detail) {
    if (cond) { console.log(`  ok   ${name}`); }
    else { failures++; console.log(`  FAIL ${name}${detail ? "  —— " + detail : ""}`); }
}

let _t = 0;
function ev(k, payload) { _t += MIN; return Object.assign({ t: T0 + _t, k: k }, payload || {}); }
function search(q, n, extra) { return ev(U.USAGE_KINDS.search, Object.assign({ q: q, n: n }, extra || {})); }

console.log("usage_core 真行为规格");

/* ---------- 0. 排序实验分臂：确定、完整、坏配置 fail closed ---------- */
{
    const arms = "control|0.8|fixed|off|off;candidate|0.2|auto|off|cross_encoder";
    const one = U.telemetryExperimentAssign("profile-a", "rank-e1", arms);
    const again = U.telemetryExperimentAssign("profile-a", "rank-e1", arms);
    check("同 profile 稳定分臂", JSON.stringify(one) === JSON.stringify(again), JSON.stringify(one));
    check("分臂带真实 propensity 和执行参数", !!one && [0.8, 0.2].includes(one.propensity)
        && !!one.overrides.strategy && !!one.experimentArm, JSON.stringify(one));
    check("权重不合计 1 → 非实验", U.telemetryExperimentAssign("p", "e", "a|0.4|fixed|off|off;b|0.4|auto|off|off") === null);
    check("未知排序后端 → 非实验", U.telemetryExperimentAssign("p", "e", "a|0.5|fixed|bad|off;b|0.5|auto|off|off") === null);
}

/* ---------- 1. 空输入不假装有数据 ---------- */
{
    const r = U.usageSummarize([], { installId: "ab12" });
    check("空事件 → empty=true", r.empty === true);
    check("空事件 → 明说没有记录到使用", /没有记录到任何使用/.test(r.text), r.text);
    check("空事件 → 不编造检索次数", !/检索 \d+ 次/.test(r.text), r.text);
}

/* ---------- 2. 查询按次数降序 + 零返回如实标注 ---------- */
{
    const events = [
        search("人类肺癌单细胞", 30), search("人类肺癌单细胞", 30), search("人类肺癌单细胞", 12),
        search("小鼠脑 10x", 0),
        search("高血压相关的免疫细胞", 0),
    ];
    const r = U.usageSummarize(events, { installId: "ab12" });
    const lines = r.text.split("\n");
    const qi = lines.findIndex((l) => l.indexOf("■ 搜过什么") === 0);
    check("有「搜过什么」小节", qi > 0);
    check("最常搜的排最前并带次数", lines[qi + 1] === "3× 人类肺癌单细胞", lines[qi + 1]);
    check("零返回查询带「← 0 条」标注", lines.some((l) => l === "小鼠脑 10x  ← 0 条"), r.text);
    check("非零返回查询不带零标注", !/人类肺癌单细胞.*← 0 条/.test(r.text));
    check("零返回单列一段并给百分比", /■ 一条都没搜到：2 次（占 40%）/.test(r.text), r.text);
    check("检索总次数是 5", /检索 5 次/.test(r.text), r.text);
}

/* ---------- 3. 截断绝不静默 ---------- */
{
    const events = [];
    for (let i = 0; i < 40; i++) events.push(search("查询" + i, 5));
    const r = U.usageSummarize(events, { installId: "ab12", maxQueryLines: 10 });
    check("超出上限时报告省略条数", r.omittedQueries === 30, "omittedQueries=" + r.omittedQueries);
    check("正文里明说省略了多少条", /另有 30 条不同的查询、合计 30 次，为了长度这次省略/.test(r.text), r.text);
    const listed = r.text.split("\n").filter((l) => /^查询\d+$/.test(l)).length;
    check("实际只列出上限条数", listed === 10, "listed=" + listed);
}
{
    // 兜底截断同样必须留痕
    const events = [];
    for (let i = 0; i < 40; i++) events.push(search("很长很长的一句查询词句" + i, 5));
    const r = U.usageSummarize(events, { installId: "ab12", maxChars: 200 });
    check("硬截断时 truncated=true", r.truncated === true);
    check("硬截断在正文里留痕", /为了能直接粘贴这里截断了/.test(r.text), r.text);
    check("硬截断后长度不超预算", r.chars <= 200, "chars=" + r.chars);
}

/* ---------- 4. 「AI 没开」与「AI 坏了」在反馈包里也必须分开 ---------- */
{
    const events = [
        search("测试", 3),
        ev(U.USAGE_KINDS.ai, { step: "llm_rerank", ok: true }),
        ev(U.USAGE_KINDS.ai, { step: "llm_rerank", ok: false, why: "llm_call_failed" }),
        ev(U.USAGE_KINDS.ai, { step: "llm_polish", ok: false, why: "invalid_llm_answer" }),
    ];
    const r = U.usageSummarize(events, { installId: "ab12" });
    check("成功的 AI 层进「真的用上了」", /真的用上了：AI 重排 1/.test(r.text), r.text);
    check("失败的 AI 层进「没能完成」", /没能完成：/.test(r.text), r.text);
    check("失败原因带在标签里", /AI 重排（llm_call_failed）/.test(r.text), r.text);
    check("两档不合并成一个数", r.text.indexOf("真的用上了") < r.text.indexOf("没能完成"), r.text);
}

/* ---------- 5. 点击名次分桶（排序质量的直接读数）---------- */
{
    const b = U.usageRankBuckets([1, 1, 2, 5, 9, null, 0]);
    check("名次分桶 first", b.first === 2, JSON.stringify(b));
    check("名次分桶 second", b.second === 1, JSON.stringify(b));
    check("名次分桶 rest", b.rest === 2, JSON.stringify(b));
    check("名次未知不冒充第一条", b.unknown === 2, JSON.stringify(b));

    const events = [
        search("测试", 9),
        ev(U.USAGE_KINDS.open, { what: "intro", r: 1 }),
        ev(U.USAGE_KINDS.open, { what: "files", r: 4 }),
    ];
    const r = U.usageSummarize(events, { installId: "ab12" });
    check("点开分类计数", /数据集详情 1 · 文件清单 1/.test(r.text), r.text);
    check("名次写进正文", /点的是：第1条 1 · 再往后 1/.test(r.text), r.text);
}

/* ---------- 5b. 「一条都没点」不许串到别的小节里去 ---------- */
{
    //  验证 dump 抓到的排版缺陷：没有任何 open 事件时，「有结果但一条都没点」
    // 会孤零零挂在上一节（未用词）末尾，读起来像是那一节的内容。
    const events = [
        search("甲", 20, { unused: ["免疫细胞"] }),
        search("乙", 20),
    ];
    const r = U.usageSummarize(events, { installId: "ab12" });
    const lines = r.text.split("\n");
    const barrenIdx = lines.findIndex((l) => l.indexOf("有结果但一条都没点") === 0);
    check("「一条都没点」这行存在", barrenIdx > 0, r.text);
    const headBefore = lines.slice(0, barrenIdx).reverse().find((l) => l.indexOf("■") === 0);
    check("它归属「点开了哪些结果」而不是上一节", headBefore === "■ 点开了哪些结果", headBefore + "\n" + r.text);
    check("零点击时明说一条都没点开过", /一条都没点开过/.test(r.text), r.text);
}

/* ---------- 5c. 同一天不写「X 至 X」 ---------- */
{
    const one = U.usageSummarize([search("甲", 3)], { installId: "ab12" });
    check("同一天只写一个日期", !/至/.test(one.text.split("\n")[1]), one.text.split("\n")[1]);
}

/* ---------- 6. 「有结果但一条都没点」 ---------- */
{
    const barren = U.usageBarrenSearches([
        search("甲", 10),                                   // 没点 → 算
        search("乙", 10), ev(U.USAGE_KINDS.open, { r: 1 }),  // 点了 → 不算
        search("丙", 0),                                    // 零返回 → 不算（另有统计，不重复归罪）
        search("丁", 10),                                   // 末尾未跟动作 → 算
    ]);
    check("无效检索只数「有结果却没点」的那几次", barren === 2, "barren=" + barren);
}

/* ---------- 7. 没被当成条件的词（词表缺口的直接读数）---------- */
{
    const events = [
        search("肺癌免疫细胞", 20, { unused: ["免疫", "细胞"] }),
        search("肺癌免疫", 20, { unused: ["免疫"] }),
    ];
    const r = U.usageSummarize(events, { installId: "ab12" });
    check("未用词按次数汇总", /免疫 2 · 细胞 1/.test(r.text), r.text);
}

/* ---------- 8. 检索失败单列 ---------- */
{
    const events = [search("甲", 3), ev(U.USAGE_KINDS.err, { msg: "服务未返回推荐结果" })];
    const r = U.usageSummarize(events, { installId: "ab12" });
    check("检索失败单列一段", /■ 检索直接失败：1 次/.test(r.text), r.text);
    check("失败原文进正文", /服务未返回推荐结果/.test(r.text), r.text);
}

/* ---------- 9. 计数排序稳定（同次数按首次出现，不按字典序）---------- */
{
    const pairs = U.usageCountBy(["乙", "甲", "甲", "乙", "丙"], (x) => x);
    check("同次数按首次出现顺序", pairs[0][0] === "乙" && pairs[1][0] === "甲", JSON.stringify(pairs));
    check("次数少的排后面", pairs[2][0] === "丙", JSON.stringify(pairs));
}

/* ---------- 10. 日期用 UTC，跨机器确定性 ---------- */
{
    check("日期戳格式 MM-DD", U.usageDayStamp(Date.UTC(2026, 6, 29, 23, 30)) === "07-29");
    check("UTC 而非本地时区", U.usageDayStamp(Date.UTC(2026, 0, 1, 0, 0)) === "01-01");
}

/* ---------- 11. 默认产物长度在「能直接粘贴」的量级 ---------- */
{
    // 一个月的重度使用：60 次检索 + 各类动作
    const events = [];
    for (let i = 0; i < 60; i++) {
        events.push(search("单细胞查询" + (i % 25), i % 7 === 0 ? 0 : 20, { unused: i % 5 === 0 ? ["免疫"] : [] }));
        events.push(ev(U.USAGE_KINDS.open, { what: "intro", r: (i % 5) + 1 }));
        if (i % 4 === 0) events.push(ev(U.USAGE_KINDS.facet, { d: "物种" }));
        if (i % 9 === 0) events.push(ev(U.USAGE_KINDS.dl, { what: "pack", n: 5 }));
        events.push(ev(U.USAGE_KINDS.ai, { step: "llm_rerank", ok: true }));
    }
    const r = U.usageSummarize(events, { installId: "ab12" });
    check("重度使用一个月仍在默认预算内", r.chars <= U.USAGE_MAX_CHARS, "chars=" + r.chars);
    check("重度使用不触发硬截断（靠查询省略先收敛）", r.truncated === false, "chars=" + r.chars);
    check("覆盖了全部事件", r.events === events.length);
}

/* ---------- 12. 不记的东西，聚合层也变不出来 ---------- */
{
    // 就算调用方误传了敏感字段，聚合层也只读它认识的键 —— 不做通用序列化。
    const events = [Object.assign(search("查询", 3), { api_key: "sk-should-never-appear", user: "someone" })];
    const r = U.usageSummarize(events, { installId: "ab12" });
    check("未登记字段不会被写进产物", r.text.indexOf("sk-should-never-appear") === -1 && r.text.indexOf("someone") === -1, r.text);
}

/* ---------- 13. v2 新维度：活跃天数 / 检索速度 / 弃权原话 ---------- */
{
    const day2 = T0 + 26 * 3600 * 1000;   // 跨 UTC 日界 → 活跃 2 天
    const events = [
        { t: T0, k: U.USAGE_KINDS.search, q: "人类肺癌", n: 12, ms: 800 },
        { t: T0 + MIN, k: U.USAGE_KINDS.search, q: "人类肺癌", n: 12, cached: true },
        { t: day2, k: U.USAGE_KINDS.search, q: "一段系统不敢回答的话", n: 0, abstain: true, ms: 2400 },
    ];
    const r = U.usageSummarize(events, { installId: "ab12" });
    check("活跃天数进头部", /活跃 2 天/.test(r.text), r.text);
    check("秒出单独计数", /秒出（相同查询直接沿用上次结果）：1 次/.test(r.text), r.text);
    check("实测耗时给平均与最慢（缓存不计入）", /实测耗时（不含秒出）：平均 1\.6 秒 · 最慢 2\.4 秒（2 次）/.test(r.text), r.text);
    check("弃权查询原话列出", /弃权的是：「一段系统不敢回答的话」/.test(r.text), r.text);
}
{
    // 单日使用不缀「活跃 1 天」（日期本身已说明）；无 cached/ms 数据时不出「检索速度」小节
    const r = U.usageSummarize([search("甲", 3)], { installId: "ab12" });
    check("单日不写活跃天数", !/活跃 \d+ 天/.test(r.text), r.text);
    check("无速度数据不出该小节", !/■ 检索速度/.test(r.text), r.text);
}

/* ---------- 14. 遥测上传包构造（buildTelemetryPackage）---------- */
{
    // 构造侧脱敏：usage 事件与 benchfb 记录里若混入敏感字段（防御性兜底），
    // 必须在**入包前**被剔除——与 benchfb 红线一致（api_key 整键删、端点只留主机、不记密码/账户名）。
    const events = [
        Object.assign(ev(U.USAGE_KINDS.search, { q: "人类肺癌", n: 12, ms: 800 }),
            {
                api_key: "sk-should-never-appear", password: "hunter2", accountUsername: "someone",
                token: "tk-should-never-appear", secret: "s3cr3t-value",
                authorization: "Bearer abc", cookie: "session=xyz", email: "user@example.com",
            }),
        ev(U.USAGE_KINDS.open, { what: "intro", r: 1 }),
    ];
    const benchfb = [
        {
            id: "r123", kind: "search", t: T0, q: "人类肺癌", src: "hero", end: T0 + MIN,
            // base_url 带端口：端口有意保留（复现所需），规格钉死这一行为
            env: { model: "demo", provider: "mock", endpoint_host: "api.deepseek.com", base_url: "https://api.deepseek.com:8443/v1?token=abc" },
            search: { req: { query: "人类肺癌", api_key: "sk-x", token: "tk-nested", base_url: "https://api.deepseek.com/v1" }, res: { results: [], result_total: 0 }, cached: false, ms: 100 },
            rating: { stars: 4, comment: "不错", useful_idx: [1] },
        },
    ];
    const pkg = U.buildTelemetryPackage(events, benchfb, {
        installId: "ab12", packetId: "pkt-1", clientId: "client-1", profileId: "profile-1",
        exportedAt: "2026-08-19T12:00:00.000Z",
        app: { cache_generation: "20260819-01", ua: "node-test", lang: "zh-CN" },
        promptVersion: "route-p7", experimentId: "rank-e1", experimentArm: "candidate",
        propensity: 0.2, trainingConsent: true,
        dropReport: { revision: 3, dropped_count: 4, by_queue: { usage: 2, benchfb: 1, storage_error: 1 } },
    });
    check("schema 是 biodata-telemetry/1", pkg.schema === U.TELEMETRY_SCHEMA, pkg.schema);
    check("合同 v2 顶层与记录同源", pkg.contract_version === U.TELEMETRY_CONTRACT_VERSION
        && pkg.usage_events[0].contract_version === 2 && pkg.benchfb_records[0].contract_version === 2);
    check("prompt/实验/propensity 可归因", pkg.prompt_version === "route-p7" && pkg.experiment_id === "rank-e1"
        && pkg.experiment_arm === "candidate" && pkg.propensity === 0.2
        && pkg.usage_events[0].experiment_arm === "candidate");
    check("训练授权独立显式", pkg.training_consent === true);
    check("丢弃增量无内容只报计数", pkg.drop_report.dropped_count === 4
        && pkg.drop_report.by_queue.storage_error === 1);
    check("install_id 透传", pkg.install_id === "ab12");
    check("幂等与匿名身份 id 透传", pkg.packet_id === "pkt-1" && pkg.client_id === "client-1" && pkg.profile_id === "profile-1");
    check("exported_at 透传", pkg.exported_at === "2026-08-19T12:00:00.000Z");
    check("app 环境透传", pkg.app.cache_generation === "20260819-01" && pkg.app.ua === "node-test" && pkg.app.lang === "zh-CN");
    check("usage 事件剔除账户名", !("accountUsername" in pkg.usage_events[0]));
    check("usage 事件剔除 token", !("token" in pkg.usage_events[0]), JSON.stringify(pkg.usage_events[0]));
    check("usage 事件剔除 secret", !("secret" in pkg.usage_events[0]));
    check("usage 事件剔除 authorization", !("authorization" in pkg.usage_events[0]));
    check("usage 事件剔除 cookie", !("cookie" in pkg.usage_events[0]));
    check("usage 事件剔除 email", !("email" in pkg.usage_events[0]));
    check("usage 事件保留非敏感字段", pkg.usage_events[0].q === "人类肺癌" && pkg.usage_events[0].ms === 800, JSON.stringify(pkg.usage_events[0]));
    check("benchfb 保留 id/kind/q", pkg.benchfb_records[0].id === "r123" && pkg.benchfb_records[0].kind === "search" && pkg.benchfb_records[0].q === "人类肺癌");
    check("benchfb 嵌套 req 剔除 api_key", !("api_key" in pkg.benchfb_records[0].search.req), JSON.stringify(pkg.benchfb_records[0].search.req));
    check("benchfb 嵌套 req 剔除 token", !("token" in pkg.benchfb_records[0].search.req), JSON.stringify(pkg.benchfb_records[0].search.req));
    check("benchfb base_url 只留主机名", pkg.benchfb_records[0].search.req.base_url === "api.deepseek.com", pkg.benchfb_records[0].search.req.base_url);
    check("benchfb env.base_url 保留端口（复现所需）", pkg.benchfb_records[0].env.base_url === "api.deepseek.com:8443", pkg.benchfb_records[0].env.base_url);
    check("评分与评语原样保留", pkg.benchfb_records[0].rating.stars === 4 && pkg.benchfb_records[0].rating.comment === "不错");
}
{
    const pkg = U.buildTelemetryPackage(null, "not-an-array", {});
    check("非数组输入按空数组（不抛）",
        Array.isArray(pkg.usage_events) && pkg.usage_events.length === 0
        && Array.isArray(pkg.benchfb_records) && pkg.benchfb_records.length === 0);
    check("缺省 opts 不抛", pkg.schema === U.TELEMETRY_SCHEMA && pkg.install_id === "");
    check("非实验/非训练默认诚实为空", pkg.contract_version === 2 && pkg.prompt_version === null
        && pkg.experiment_arm === null && pkg.propensity === null && pkg.training_consent === false);
}
{
    // 构造必须返回新结构，不得改动原数组/对象（调用方上传后还要按快照截断原数据）
    const src = [{ t: T0, k: U.USAGE_KINDS.search, q: "x" }];
    U.buildTelemetryPackage(src, [], {});
    check("构造不动原事件数组", src.length === 1 && src[0].q === "x", JSON.stringify(src));
}

/* ---------- 15. 遥测 ID 层（schema v2）---------- */
{
    check("view 事件已登记", U.USAGE_KINDS.view === "view");
    const sid1 = U.usageSessionId(), sid2 = U.usageSessionId();
    check("sid 稳定（同标签页同一个）", sid1 === sid2 && sid1.indexOf("sid-") === 0, sid1);
    const t1 = U.usageBeginTurn(), t2 = U.usageBeginTurn();
    check("tid 每轮换新且唯一", t1 !== t2 && U.usageActiveTurnId() === t2, t1 + " vs " + t2);
    const i1 = U.usageBeginImpression(), i2 = U.usageBeginImpression();
    check("iid 每次渲染换新且唯一", i1 !== i2 && U.usageActiveImpressionId() === i2, i1 + " vs " + i2);
    check("policy 串形如 strategy/rerank/recall@gen",
        U.usagePolicyId({ strategy: "auto", rerank: "llm", recall: "cross_encoder", gen: "20260820-01" }) === "auto/llm/cross_encoder@20260820-01");
    check("policy 段缺省 unknown、无 gen 不缀 @",
        U.usagePolicyId({}) === "unknown/unknown/unknown" && U.usagePolicyId({ strategy: "auto" }) === "auto/unknown/unknown");
    const backendA = { schema: "biodata-policy-id/1", corpus: { n_records: 784, snapshot_id: "snap-a" },
        ranking: { rerank: "off", strategy: "auto" }, sources: ["10x Genomics"] };
    const backendB = { sources: ["10x Genomics"], ranking: { strategy: "auto", rerank: "off" },
        corpus: { snapshot_id: "snap-a", n_records: 784 }, schema: "biodata-policy-id/1" };
    check("后端紧凑串优先", U.usagePolicyRef({ policy_id: backendA, policy_id_str: "bpol1:server" }) === "bpol1:server");
    check("结构体 fallback 键序稳定", U.usagePolicyRef(backendA) === U.usagePolicyRef(backendB),
        U.usagePolicyRef(backendA) + " vs " + U.usagePolicyRef(backendB));
    check("结构体 fallback 不会变 [object Object]", U.usagePolicyRef(backendA).startsWith("bpol-json:")
        && !U.usagePolicyRef(backendA).includes("[object Object]"));
    check("旧字符串兼容", U.usagePolicyRef("legacy-policy") === "legacy-policy");
    check("污染字符串降级而非保留", U.usagePolicyRef("[object Object]", { strategy: "fixed" }) === "fixed/unknown/unknown");
}

/* ---------- 16. 值级遮蔽（手机号/证件号/邮箱）---------- */
{
    check("手机号遮蔽", U.telemetryMaskString("联系电话 13812345678 谢谢") === "联系电话 [手机号] 谢谢",
        U.telemetryMaskString("联系电话 13812345678 谢谢"));
    check("证件号遮蔽", U.telemetryMaskString("证件 11010119900307431X 尾") === "证件 [证件号] 尾",
        U.telemetryMaskString("证件 11010119900307431X 尾"));
    check("邮箱遮蔽", U.telemetryMaskString("邮 zhang.san99@example.com 退") === "邮 [邮箱] 退",
        U.telemetryMaskString("邮 zhang.san99@example.com 退"));
    check("普通查询原样", U.telemetryMaskString("人类肺癌 10x 数据") === "人类肺癌 10x 数据");
    // 遮蔽递归作用于整个包，且不改原事件
    const src = [ev(U.USAGE_KINDS.search, { q: "我的手机 13812345678", n: 1 })];
    const pkg = U.buildTelemetryPackage(src, [], {});
    check("包内事件值被遮蔽", pkg.usage_events[0].q === "我的手机 [手机号]", pkg.usage_events[0].q);
    check("原事件不被改动", src[0].q === "我的手机 13812345678", src[0].q);
}

/* ---------- 17. mcp_records 中继附加---------- */
{
    const mcp = [{ call_id: "c1", tool: "search", args: "打给 13900001111", token: "tk-x" }];
    const pkg = U.buildTelemetryPackage([], [], { mcpRecords: mcp });
    check("mcp_records 附进顶层", Array.isArray(pkg.mcp_records) && pkg.mcp_records.length === 1);
    check("mcp 记录过键级剔除", !("token" in pkg.mcp_records[0]), JSON.stringify(pkg.mcp_records[0]));
    check("mcp 记录过值级遮蔽", pkg.mcp_records[0].args === "打给 [手机号]", pkg.mcp_records[0].args);
    check("原 mcp 数组不被改动", mcp[0].args === "打给 13900001111" && "token" in mcp[0]);
    check("无 mcpRecords 时不带该键", !("mcp_records" in U.buildTelemetryPackage([], [], {})));
    check("空数组也不带该键", !("mcp_records" in U.buildTelemetryPackage([], [], { mcpRecords: [] })));
}

/* ---------- 18. schema v3 与新 kind 登记---------- */
{
    check("USAGE_SCHEMA 升到 3", U.USAGE_SCHEMA === 3, String(U.USAGE_SCHEMA));
    check("imp 事件已登记", U.USAGE_KINDS.imp === "imp");
    check("label 事件已登记", U.USAGE_KINDS.label === "label");
}

/* ---------- 19. 曝光可见状态机（usageSeenTick/Pause）---------- */
{
    // 卡 1：同侧连续在屏累计达阈值才 seen；达阈值后再 tick 不再重复触发
    const st = U.usageSeenCreate(500);
    check("卡1 起始 0ms 未达阈值", U.usageSeenTick(st, 1, true, 0) === false);
    check("卡1 累计 400ms 未达阈值", U.usageSeenTick(st, 1, true, 400) === false);
    check("卡1 累计 500ms 达标", U.usageSeenTick(st, 1, true, 500) === true && st.seen.has(1));
    check("卡1 已 seen 再 tick 恒 false", U.usageSeenTick(st, 1, true, 900) === false && U.usageSeenTick(st, 1, false, 1000) === false);
    // 卡 2：无 pause 的离屏 = 清零重计
    check("卡2 起始 0ms", U.usageSeenTick(st, 2, true, 0) === false);
    check("卡2 离屏（无 pause）清零", U.usageSeenTick(st, 2, false, 300) === false);
    check("卡2 回屏 1000ms 重开", U.usageSeenTick(st, 2, true, 1000) === false);
    check("卡2 累计 499ms 未达", U.usageSeenTick(st, 2, true, 1499) === false);
    check("卡2 累计 500ms 达标", U.usageSeenTick(st, 2, true, 1500) === true && st.seen.has(2));
    // 卡 3：pause 离屏 = 冻结续计（不清零）
    check("卡3 起始 0ms", U.usageSeenTick(st, 3, true, 0) === false);
    check("卡3 离屏 pause 冻结 300ms", U.usageSeenTick(st, 3, false, 300, { pause: true }) === false);
    check("卡3 回屏 1000ms 重开区间", U.usageSeenTick(st, 3, true, 1000) === false);
    check("卡3 冻结 300 + 在屏 200 = 达标", U.usageSeenTick(st, 3, true, 1200) === true && st.seen.has(3));
    // usageSeenPause：整体冻结在途区间，回屏后续计
    const st2 = U.usageSeenCreate(500);
    U.usageSeenTick(st2, 1, true, 0);
    U.usageSeenPause(st2, 400);
    check("pause 后 1000ms 回屏未即时达标（冻结 400）", U.usageSeenTick(st2, 1, true, 1000) === false);
    check("冻结 400 + 在屏 100 = 达标", U.usageSeenTick(st2, 1, true, 1100) === true);
    // 从未在屏的卡不应有累计（离屏 tick 不凭空开区间）
    const st3 = U.usageSeenCreate(500);
    U.usageSeenTick(st3, 9, false, 0);
    U.usageSeenTick(st3, 9, false, 5000);
    check("离屏卡再来在屏从 0 起计", U.usageSeenTick(st3, 9, true, 6000) === false
        && U.usageSeenTick(st3, 9, true, 6499) === false
        && U.usageSeenTick(st3, 9, true, 6500) === true);
}

/* ---------- 20. ID 形态：randomUUID 优先---------- */
{
    // node ≥19 有全局 crypto.randomUUID；sid/tid/iid 应呈 uuid 段形态而非旧 random 段
    check("sid 带 uuid 段", /^sid-[0-9a-f]{8}-[0-9a-f]{4}-/.test(U.usageSessionId()), U.usageSessionId());
    check("tid 带 uuid 段", /^t-[0-9a-f]{8}-[0-9a-f]{4}-/.test(U.usageBeginTurn()), U.usageActiveTurnId());
    check("iid 带 uuid 段", /^i-[0-9a-f]{8}-[0-9a-f]{4}-/.test(U.usageBeginImpression()), U.usageActiveImpressionId());
}

console.log(failures ? `\n${failures} 条失败` : "\n全部通过\nUSAGE_CORE_SPEC_OK");
process.exit(failures ? 1 : 0);
