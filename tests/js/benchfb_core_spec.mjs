"use strict";

/* ============================================================================
 * benchfb_core_spec.mjs —— benchmark 采集反馈纯核「真行为」规格（node 跑）
 * ----------------------------------------------------------------------------
 * 由 tests/test_benchfb_contract.py 经 `node <this>` 驱动；断言失败 → 非零退出。
 * 这个功能的产物是两样东西：①落盘的交互记录（脱敏对不对）②导出的反馈包
 * （schema/文件名/标注解析对不对）。两处错了都是静默脏数据——Key 泄进包、
 * 标注名次数对错结果数组——所以逐条断言真实输出，不靠代码验证兜底。
 * 相对路径 import，避开中文路径入 argv。纯核无墙钟：一切时间/随机由规格注入。
 * ========================================================================== */

import * as B from "../../web/static/js/core/benchfb_core.js";

let failures = 0;
function check(name, cond, detail) {
    if (cond) { console.log(`  ok   ${name}`); }
    else { failures++; console.log(`  FAIL ${name}${detail ? "  —— " + detail : ""}`); }
}

console.log("benchfb_core 真行为规格");

/* ---------- 1. 脱敏：api_key 整键删、base_url 只留主机、原对象不被改 ---------- */
{
    const req = { query: "小鼠脑", api_key: "sk-secret-123", base_url: "https://api.deepseek.com/v1/chat", model: "deepseek-chat", top_k: 10 };
    const out = B.benchfbStripRequest(req);
    check("api_key 整键删除", !("api_key" in out), JSON.stringify(out));
    check("base_url 只剩主机", out.base_url === "api.deepseek.com", out.base_url);
    check("其余字段原样保留", out.query === "小鼠脑" && out.model === "deepseek-chat" && out.top_k === 10);
    check("原请求对象不被改动（还要发往后端）", req.api_key === "sk-secret-123" && req.base_url.indexOf("https://") === 0);
    const raw = JSON.stringify(out);
    check("脱敏产物里搜不到 Key 影子", raw.indexOf("sk-secret") < 0, raw);
}
{
    check("非法 base_url → 空串（不炸、不透出）", B.benchfbEndpointHost("not a url") === "");
    check("空 base_url → 空串", B.benchfbEndpointHost("") === "" && B.benchfbEndpointHost(undefined) === "");
    check("带端口/路径的代理只留主机：端口", B.benchfbEndpointHost("http://127.0.0.1:8317/proxy/v1") === "127.0.0.1:8317");
}

/* ---------- 2. 评分合并：三选/原因/标注/评语，不改旧记录；不再写 stars---------- */
{
    const rec = { id: "r1", rating: null };
    const rated = B.benchfbRate(rec, { completion: "partial", reasons: ["排序不对", "其他", "排序不对", "编造原因"], usefulIdx: [2, 1, 2, 7, -1, 3.5], comment: "第 2 条对", ratedAt: 1234 });
    check("完成度入库", rated.rating.completion === "partial");
    check("原因白名单过滤 + 去重 + 按白名单序", JSON.stringify(rated.rating.reasons) === "[\"排序不对\",\"其他\"]", JSON.stringify(rated.rating.reasons));
    check("标注去重排序、剔非法", JSON.stringify(rated.rating.useful_idx) === "[1,2,7]", JSON.stringify(rated.rating.useful_idx));
    check("评语入库", rated.rating.comment === "第 2 条对");
    check("评分时刻注入", rated.rating.rated_at === 1234);
    check("不再写 stars", !("stars" in rated.rating), JSON.stringify(rated.rating));
    check("旧记录不被改", rec.rating === null);
    const bad = B.benchfbRate(rated, { completion: "非常好", ratedAt: 2000 });
    check("非法完成度不覆盖已评", bad.rating.completion === "partial", String(bad.rating.completion));
    const cleared = B.benchfbRate(rated, { completion: null, ratedAt: 2100 });
    check("completion 传 null = 显式取消选择", cleared.rating.completion === null);
    check("取消完成度时原因保留", JSON.stringify(cleared.rating.reasons) === "[\"排序不对\",\"其他\"]");
    const noReasons = B.benchfbRate(rated, { reasons: [], ratedAt: 2200 });
    check("原因传空数组 = 清空", noReasons.rating.reasons.length === 0);
    const longC = B.benchfbRate(rec, { comment: "长".repeat(900), ratedAt: 1 });
    check("评语截断到上限", longC.rating.comment.length === B.BENCHFB_MAX_COMMENT, String(longC.rating.comment.length));
    const partial = B.benchfbRate(rated, { comment: "改评语", ratedAt: 3000 });
    check("只改评语时完成度/原因/标注保留", partial.rating.completion === "partial" && partial.rating.reasons.length === 2 && partial.rating.useful_idx.length === 3);
    const legacy = B.benchfbRate({ id: "r2", rating: { stars: 4, useful_idx: [1], comment: "旧形状", rated_at: 100 } }, { completion: "done", ratedAt: 5000 });
    check("旧形状记录重评后转为新形状（stars 不携带）", legacy.rating.completion === "done" && !("stars" in legacy.rating), JSON.stringify(legacy.rating));
    check("旧形状的标注/评语在新形状里保留", legacy.rating.useful_idx.length === 1 && legacy.rating.comment === "旧形状");
}

/* ---------- 2b. 会话级评分降频闸（主动卡 ≤2 张、连续忽略 2 次不再主动出）---------- */
{
    let st = B.benchfbRateSession(null);
    check("空状态规范化", st.shown.length === 0 && st.pending === null && st.ignored === 0);
    check("首两张主动卡放行", B.benchfbProactiveAllowed(st, "a") && (st = B.benchfbNoteShown(st, "a", false), B.benchfbProactiveAllowed(st, "b")));
    st = B.benchfbNoteShown(st, "b", false);   // a 未评分 → ignored 变 1
    check("pending 未评分 → 连续忽略 +1", st.ignored === 1 && st.pending === "b", JSON.stringify(st));
    check("配额满（2 张）→ 第三张不主动出", !B.benchfbProactiveAllowed(st, "c"));
    check("已计额的卡重画仍放行", B.benchfbProactiveAllowed(st, "a") && B.benchfbProactiveAllowed(st, "b"));
    const st2 = B.benchfbNoteRated(st, "b");
    check("评分打断连续忽略", st2.ignored === 0 && st2.pending === null, JSON.stringify(st2));
    const st3 = B.benchfbNoteDismissed(B.benchfbRateSession({ shown: ["x"], pending: "x", ignored: 1 }), "x", false);
    check("未评分即收起 → 连续忽略 +1 达阈值", st3.ignored === 2 && st3.pending === null);
    check("连续忽略 2 次 → 不再主动出卡", !B.benchfbProactiveAllowed(st3, "y"));
    const st4 = B.benchfbNoteDismissed(B.benchfbRateSession({ shown: ["x"], pending: "x", ignored: 0 }), "x", true);
    check("已评分的收起只是 UI 偏好、不计忽略", st4.ignored === 0);
    const again = B.benchfbNoteShown(st3, "x", false);
    check("同一卡重复上屏幂等（重画不产生新计数）", again.shown.length === 1 && again.ignored === 2, JSON.stringify(again));
    const dirty = B.benchfbRateSession({ shown: ["a", "", "a", 7], pending: "zzz", ignored: -3 });
    check("脏状态规范化（去重去空、pending 必须在 shown 里、ignored 不为负）", dirty.shown.length === 2 && dirty.pending === null && dirty.ignored === 0, JSON.stringify(dirty));
}

/* ---------- 3. FIFO 裁剪：条数与体量双闸，从最旧端丢 ---------- */
{
    const many = [];
    for (let i = 0; i < 10; i++) many.push({ id: "r" + i, t: i });
    const trimmed = B.benchfbTrim(many, 3, 10 ** 9);
    check("条数闸：留最新 3 条", trimmed.length === 3 && trimmed[0].id === "r7", JSON.stringify(trimmed.map((r) => r.id)));
    const big = [{ id: "old", pad: "x".repeat(5000) }, { id: "mid", pad: "x".repeat(5000) }, { id: "new" }];
    const t2 = B.benchfbTrim(big, 99, 6000);
    check("体量闸：装得下两条就留两条", t2.length === 2 && t2[0].id === "mid", JSON.stringify(t2.map((r) => r.id)));
    const t3 = B.benchfbTrim(big, 99, 4500);
    check("体量闸：装不下就再丢，直到只剩最新", t3.length === 1 && t3[0].id === "new", JSON.stringify(t3.map((r) => r.id)));
    check("输入数组不被改", many.length === 10 && big.length === 3);
}

/* ---------- 4. 标注名次 → 数据集解析（导出时对照当时那屏结果）---------- */
{
    const rec = {
        id: "r9",
        search: { res: { results: [
            { dataset_uid: "uid-a", dataset_name: "甲" },
            { dataset_uid: "uid-b", dataset_name: "乙" },
            { dataset_uid: "uid-c", dataset_name: "丙" },
        ] } },
        rating: { completion: "partial", reasons: ["排序不对"], useful_idx: [1, 3, 9], comment: "" },
    };
    const resolved = B.benchfbResolveUseful(rec);
    check("名次 1/3 解出 uid", resolved.length === 2 && resolved[0].uid === "uid-a" && resolved[1].uid === "uid-c", JSON.stringify(resolved));
    check("越界名次如实丢弃", !resolved.some((x) => x.idx === 9));
    check("无评分记录 → 空", B.benchfbResolveUseful({ id: "x" }).length === 0);
    const exp = B.benchfbForExport(rec);
    check("导出形态带 useful_resolved", exp.rating.useful_resolved.length === 2);
    check("导出形态带 useful_uids（随新形状带出）", JSON.stringify(exp.rating.useful_uids) === "[\"uid-a\",\"uid-c\"]", JSON.stringify(exp.rating.useful_uids));
    check("导出不动原记录", !("useful_resolved" in (rec.rating || {})) && !("useful_uids" in (rec.rating || {})));
}

/* ---------- 5. 导出包：schema / 注入字段 / 文件名 ---------- */
{
    const pkg = B.benchfbBuildPackage(
        [{ id: "r1", kind: "search", t: 100, rating: { completion: "done", reasons: [], useful_idx: [1] },
            search: { res: { results: [{ dataset_uid: "u1" }] } } }],
        { installId: "ab12", clientId: "client-1", profileId: "profile-1", exportedAt: "2026-08-13T11:00:00.000Z", app: { cache_generation: "20260813-bf1", ua: "UA", lang: "zh-CN" } });
    check("schema 版本钉死", pkg.schema === "biodata-benchfb/1", pkg.schema);
    check("install_id / exported_at 注入", pkg.install_id === "ab12" && pkg.exported_at === "2026-08-13T11:00:00.000Z");
    check("client/profile id 注入", pkg.client_id === "client-1" && pkg.profile_id === "profile-1");
    check("app 环境入包", pkg.app.cache_generation === "20260813-bf1" && pkg.app.lang === "zh-CN");
    check("记录经导出形态转换", pkg.records[0].rating.useful_resolved[0].uid === "u1");
    const name = B.benchfbFileName(new Date(Date.UTC(2026, 7, 13, 3, 4, 5)));
    check("文件名是中文+日期（UTC 确定性）", name === "biodata-反馈包-2026-08-13.json", name);
}

/* ---------- 6. 包内容统计（导出弹窗的大白话清单；完成度分项取代星级均值）---------- */
{
    const sum = B.benchfbPackageSummary([
        { id: "a", kind: "search", t: 100, rating: { completion: "done", reasons: [], useful_idx: [1, 2] } },
        { id: "b", kind: "tool", t: 200, rating: { completion: "failed", reasons: ["执行没完成"], useful_idx: [] } },
        { id: "c", kind: "none", t: 300, rating: null },
        { id: "d", kind: "error", t: 400, rating: null },
        { id: "e", kind: "search", t: 500, rating: { stars: 4, useful_idx: [] } },   // 旧形状：计入 rated，不贡献完成度分项
    ]);
    check("轮次计数", sum.turns === 5 && sum.search === 2 && sum.tool === 1 && sum.none === 1 && sum.error === 1);
    check("已评分计数（新旧形状都算）", sum.rated === 3);
    check("完成度分项", sum.comp_done === 1 && sum.comp_failed === 1 && sum.comp_partial === 0, JSON.stringify(sum));
    check("标注计数", sum.marked === 2);
    check("时间跨度取首末", sum.first_ts === 100 && sum.last_ts === 500);
    const empty = B.benchfbPackageSummary([]);
    check("空包不炸", empty.turns === 0 && empty.rated === 0);
}

/* ---------- 7. 记录 id：注入时间戳+随机串，可复现 ---------- */
{
    check("id 由注入值决定", B.benchfbMakeId(1755e7, "AB12cd") === B.benchfbMakeId(1755e7, "ab12cd"));
    check("id 带 r 前缀", B.benchfbMakeId(100, "x").indexOf("r") === 0);
}

console.log(failures ? `\n${failures} 条断言失败` : "\n全部通过");
process.exit(failures ? 1 : 0);
