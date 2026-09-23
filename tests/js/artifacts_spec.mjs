"use strict";

/* ============================================================================
 * artifacts_spec.mjs —— 课题数据层「真行为」规格（node 跑）
 * ----------------------------------------------------------------------------
 * 由 tests/test_artifacts_contract.py 经 `node <this>` 驱动；断言失败 → 非零退出。
 * 存在意义：web_smoke 只静态查字符串、node --check 只验语法，两门都测不出
 * **IndexedDB 数据层的真实行为**——CRUD 是否写对键、profile 隔离是否真的隔离、
 * 配额错误是否如实上报、备份导入导出是否不丢不造数据。这些是 diff 闭环与
 * 导出中心要直接踩的地基，错一处后面全歪，所以这里逐条断言真行为。
 *
 * IndexedDB 在 node 里不存在：本文件自带一个**进程内 fake IndexedDB**，只实现本模块
 * 用到的 API 面（open/createObjectStore/createIndex/transaction/get/put/delete/getAll/
 * index.getAll），结果经 setTimeout(0) 异步派发（与真 IDB 一样异步——防「假同步」掩盖
 * 真实时序错误）。经 artifactsSetIdbFactory 注入；验证走全局 indexedDB，同一套代码。
 * 相对路径 import，避开中文路径入 argv。
 * ========================================================================== */

import * as A from "../../web/static/js/core/artifacts.js";

const T0 = 1753000000000;   //  固定基准： 前后（脚本内一切时间由此推出，确定性）

let failures = 0;
function check(name, cond, detail) {
    if (cond) { console.log(`  ok   ${name}`); }
    else { failures++; console.log(`  FAIL ${name}${detail ? "  —— " + detail : ""}`); }
}
async function expectReject(name, fn, errName) {
    try {
        await fn();
        failures++; console.log(`  FAIL ${name}  —— 未抛错`);
    } catch (e) {
        if (e && e.name === errName) { console.log(`  ok   ${name}`); }
        else { failures++; console.log(`  FAIL ${name}  —— 错误名「${e && e.name}」，期望「${errName}」`); }
    }
}

const iso = (ms) => new Date(ms).toISOString();
function fixture(overrides) {
    return Object.assign({
        project_id: "p1",
        name: "肺癌单细胞",
        goal: "找可用数据集",
        include_conditions: ["人类", "肺癌"],
        exclude_conditions: ["小鼠"],
    }, overrides || {});
}

/* ============================================================================
 * fake IndexedDB（最小实现，只服务本规格）
 * ========================================================================== */
function createFakeIndexedDB() {
    const stores = new Map();   // storeName → { keyPath, index: Map<name,{keyPath}>, rows: Map<key, row> }
    let dbVersion = 0;
    const clone = (v) => JSON.parse(JSON.stringify(v));

    function newRequest() {
        return { onsuccess: null, onerror: null, result: undefined, error: null };
    }
    function succeed(req, result) {
        req.result = result;
        setTimeout(() => { if (req.onsuccess) req.onsuccess(); }, 0);
    }
    function failReq(req, name, message) {
        req.error = { name: name, message: message || name };
        setTimeout(() => { if (req.onerror) req.onerror(); }, 0);
    }
    function abortTx(tx) {
        setTimeout(() => { if (tx.onabort) tx.onabort(); }, 0);
    }
    function ensureStore(name) {
        let s = stores.get(name);
        if (!s) { s = { keyPath: null, index: new Map(), rows: new Map() }; stores.set(name, s); }
        return s;
    }
    function dbHandle() {
        const db = {
            objectStoreNames: { contains: (n) => stores.has(n) },
            createObjectStore(name, opts) {
                const s = ensureStore(name);
                s.keyPath = opts && opts.keyPath;
                return { createIndex: (iname, keyPath, iopts) => { s.index.set(iname, { keyPath: keyPath, unique: !!(iopts && iopts.unique) }); } };
            },
            close() {},
        };
        db.transaction = (storeNames, mode) => makeTx(storeNames, mode);
        return db;
    }
    function makeTx(storeNames, mode) {
        const tx = {
            objectStore: (n) => storeProxy(n, tx),
            oncomplete: null, onerror: null, onabort: null, error: null,
        };
        return tx;
    }
    function storeProxy(storeName, tx) {
        const s = ensureStore(storeName);
        const keyOf = (v) => (s.keyPath ? v[s.keyPath] : undefined);
        const req = newRequest();
        return {
            get(key) {
                const row = s.rows.get(key);
                if (row === undefined) succeed(req, undefined);
                else succeed(req, clone(row));
                return req;
            },
            put(value) {
                if (api.forceQuota) { failReq(req, "QuotaExceededError", "磁盘配额不足（fake）"); abortTx(tx); return req; }
                const k = keyOf(value);
                if (k === undefined) { failReq(req, "DataError", "记录缺主键"); abortTx(tx); return req; }
                s.rows.set(k, clone(value));
                succeed(req, k);
                return req;
            },
            delete(key) {
                s.rows.delete(key);
                succeed(req, undefined);
                return req;
            },
            getAll(query) {
                const out = [];
                s.rows.forEach((row) => { out.push(clone(row)); });
                succeed(req, out);
                return req;
            },
            index(iname) {
                const idx = s.index.get(iname);
                return {
                    getAll(query) {
                        const out = [];
                        if (!idx) { succeed(req, []); return req; }
                        s.rows.forEach((row) => { if (row[idx.keyPath] === query) out.push(clone(row)); });
                        succeed(req, out);
                        return req;
                    },
                };
            },
        };
    }
    const api = {
        forceQuota: false,
        open(name, version) {
            const req = newRequest();
            setTimeout(() => {
                if (dbVersion < version) {
                    dbVersion = version;
                    req.result = dbHandle();
                    if (req.onupgradeneeded) req.onupgradeneeded();
                }
                req.result = req.result || dbHandle();
                if (req.onsuccess) req.onsuccess();
            }, 0);
            return req;
        },
    };
    return api;
}

function freshDb() {
    A.artifactsClose();
    const fake = createFakeIndexedDB();
    A.artifactsSetIdbFactory(() => fake);
    return fake;
}

console.log("artifacts 数据层真行为规格");

/* ============================================================================
 * 1. 常量与 schema 版本
 * ========================================================================== */
{
    check("ARTIFACTS_SCHEMA = 1", A.ARTIFACTS_SCHEMA === 1, String(A.ARTIFACTS_SCHEMA));
    check("库名 biodata-artifacts", A.ARTIFACTS_DB_NAME === "biodata-artifacts");
    check("新候选默认待核验", A.DEFAULT_CANDIDATE_STATUS === A.PROJECT_STATUS.PENDING);
    check("状态枚举齐全", ["候选", "待核验", "已核验", "已排除"].every((s) => A.PROJECT_STATUS_VALUES.includes(s)));
}

/* ============================================================================
 * 2. 纯 normalize：白名单、默认值、时间戳（固定时钟）
 * ========================================================================== */
{
    A.artifactsSetClock(() => T0);
    const p = A.artifactsNormalizeProject(fixture({ junk: "不许进库", candidates: [{ uid: "c1" }] }), { now: T0 });
    check("schema_version 必备且为当前版本", p.schema_version === A.ARTIFACTS_SCHEMA, JSON.stringify(p));
    check("未知键被白名单剔除", !("junk" in p), JSON.stringify(Object.keys(p)));
    check("新候选默认待核验", p.candidates[0].status === "待核验", JSON.stringify(p.candidates[0]));
    check("新候选 added_at 落 ISO", p.candidates[0].added_at === iso(T0));
    check("缺省时间戳为固定时钟 ISO", p.created_at === iso(T0) && p.updated_at === iso(T0));
    check("数组字段全部强制数组", Array.isArray(p.include_conditions) && Array.isArray(p.exclude_conditions)
        && Array.isArray(p.candidates) && Array.isArray(p.exports) && Array.isArray(p.activity));
    check("check_condition 缺省为空", p.check_condition === null);
    check("provenance 缺省为空", p.provenance === null);
    check("project_id 缺省为空串", A.artifactsNormalizeProject({}, { now: T0 }).project_id === "");

    const nine = A.artifactsNormalizeProject(fixture({
        include_conditions: ["一", "二", "三", "四", "五", "六", "七", "八", "九"],
    }), { now: T0 });
    check("超 8 条不静默截断（保留全量，交由校验报错）", nine.include_conditions.length === 9, String(nine.include_conditions.length));
    check("校验对超 8 条如实报错", A.artifactsValidateProject(nine).some((e) => e.indexOf("纳入条件超过") === 0),
        JSON.stringify(A.artifactsValidateProject(nine)));
}

/* ============================================================================
 * 3. validate：结构校验逐项
 * ========================================================================== */
{
    check("合法课题零错误", A.artifactsValidateProject(A.artifactsNormalizeProject(fixture(), { now: T0 })).length === 0);
    check("缺 project_id 报错", A.artifactsValidateProject(A.artifactsNormalizeProject(fixture({ project_id: " " }), { now: T0 })).some((e) => e.indexOf("project_id") === 0));
    check("缺名称报错", A.artifactsValidateProject(A.artifactsNormalizeProject(fixture({ name: " " }), { now: T0 })).some((e) => e.indexOf("追踪名称") === 0));
    const badStatus = A.artifactsNormalizeProject(fixture({ candidates: [{ uid: "c1", status: "胡说" }] }), { now: T0 });
    check("非法状态回落待核验（不冒充已核验）", badStatus.candidates[0].status === "待核验");
}

/* ============================================================================
 * 4. 复合主键：scope + project_id 编码
 * ========================================================================== */
{
    check("不同 scope 同 id 键不同", A.artifactsKey("u1", "p1") !== A.artifactsKey("u2", "p1"));
    check("匿名 scope 与登录 scope 键不同", A.artifactsKey("", "p1") !== A.artifactsKey("u1", "p1"));
    check("scope 规范化（null→空串）", A.artifactsKey(null, "p1") === A.artifactsKey("", "p1"));
    const k = A.artifactsKey("u1", "p1");
    const parts = A.artifactsKeyParts(k);
    check("主键往返可解码", parts.scope === "u1" && parts.projectId === "p1", JSON.stringify(parts));
    check("非法串防御性解读", A.artifactsKeyParts("无分隔符").scope === "" && A.artifactsKeyParts("无分隔符").projectId === "无分隔符");
}

/* ============================================================================
 * 5. 纯变换：候选流（默认待核验闸门）
 * ========================================================================== */
{
    let p = A.artifactsNormalizeProject(fixture(), { now: T0 });
    p = A.artifactsAddCandidate(p, "c1", { now: T0 });
    p = A.artifactsAddCandidate(p, "c1", { now: T0 });   // 幂等去重
    check("加候选默认待核验", p.candidates.length === 1 && p.candidates[0].status === "待核验", JSON.stringify(p.candidates));
    p = A.artifactsSetCandidateStatus(p, "c1", A.PROJECT_STATUS.VERIFIED, "样本量充足", { now: T0 + 60000 });
    check("裁决到已核验落 verified_at", p.candidates[0].status === "已核验" && p.candidates[0].verified_at === iso(T0 + 60000), JSON.stringify(p.candidates[0]));
    check("reason 带进记录", p.candidates[0].reason === "样本量充足");
    p = A.artifactsSetCandidateStatus(p, "c1", "非法状态", undefined, { now: T0 + 120000 });
    check("非法状态不动原记录", p.candidates[0].status === "已核验", JSON.stringify(p.candidates[0]));
    p = A.artifactsSetCandidateStatus(p, "c1", A.PROJECT_STATUS.PENDING, "再看", { now: T0 + 180000 });
    check("回退到非终态不新落 verified_at（保留旧戳）", p.candidates[0].status === "待核验" && p.candidates[0].verified_at === iso(T0 + 60000), JSON.stringify(p.candidates[0]));
    p = A.artifactsRemoveCandidate(p, "c1", { now: T0 });
    check("移除候选生效", p.candidates.length === 0);
    p = A.artifactsRemoveCandidate(p, "不存在", { now: T0 });
    check("移除不存在的候选原样返回", p.candidates.length === 0);
    check("addCandidate 只收 (project, uid, opts)——无 status 形参（默认待核验是结构性的）", A.artifactsAddCandidate.length === 3, String(A.artifactsAddCandidate.length));
}

/* ============================================================================
 * 6. 纯变换：check_condition / baseline / last_checked_at
 * ========================================================================== */
{
    let p = A.artifactsNormalizeProject(fixture(), { now: T0 });
    p = A.artifactsSetCheckCondition(p, {
        display_query: "人类 肺癌 10x",
        spec: { query: "人类 肺癌", sources: ["10x"], suppressed_constraints: ["exclude:物种"], lenient_dims: ["species"], date_from: "2024-01-01", facet_filters: [{ dim: "物种", value: "人类" }] },
    }, { now: T0 });
    check("display_query 单独保存", p.check_condition.display_query === "人类 肺癌 10x");
    check("spec 归整（query/sources/facet_filters/suppressed_constraints/lenient_dims/日期/spec_version，与 /api/watch/check 对齐）",
        p.check_condition.spec.query === "人类 肺癌" && p.check_condition.spec.sources[0] === "10x"
        && p.check_condition.spec.facet_filters[0].dim === "物种" && p.check_condition.spec.facet_filters[0].value === "人类"
        && p.check_condition.spec.suppressed_constraints[0] === "exclude:物种" && p.check_condition.spec.lenient_dims[0] === "species"
        && p.check_condition.spec.date_from === "2024-01-01" && p.check_condition.spec.spec_version === "v1",
        JSON.stringify(p.check_condition.spec));
    p = A.artifactsSetBaseline(p, { uids: ["a", "b", "a"], fingerprints: { a: "fp-a", b: "fp-b" }, result_total: 2, truncated: false }, { now: T0 + 1000 });
    check("baseline uids 去重保序（无序集合展平）", JSON.stringify(p.check_condition.baseline.uids) === JSON.stringify(["a", "b"]));
    check("baseline fingerprints/result_total/truncated 落齐", p.check_condition.baseline.fingerprints.a === "fp-a"
        && p.check_condition.baseline.result_total === 2 && p.check_condition.baseline.truncated === false);
    check("baseline generated_at 落戳", p.check_condition.baseline.generated_at === iso(T0 + 1000));
    p = A.artifactsTouchCheckedAt(p, T0 + 5000, { now: T0 + 5000 });
    check("last_checked_at 落戳", p.check_condition.last_checked_at === iso(T0 + 5000));
    /*ISO 字符串时间戳（服务端 checked_at 直传）不得落成 epoch 0；
       已被旧版污染的 epoch 读数在归一化时修复为「从未检查」。 */
    p = A.artifactsTouchCheckedAt(p, "2026-08-22T02:00:00Z", { now: T0 + 5000 });
    check("last_checked_at 接受 ISO 字符串（服务端 checked_at 直传）", p.check_condition.last_checked_at === "2026-08-22T02:00:00.000Z",
        p.check_condition.last_checked_at);
    {
        const raw = fixture();
        raw.check_condition = {
            display_query: "q",
            spec: { query: "人类 肺癌", sources: ["10x"], suppressed_constraints: [], lenient_dims: [], facet_filters: [] },
            baseline: null,
            last_checked_at: "1970-01-01T00:00:00.000Z",
        };
        const rp = A.artifactsNormalizeProject(raw, { now: T0 });
        check("epoch 0 污染的 last_checked_at 归一化为「从未检查」", rp.check_condition.last_checked_at === "",
            rp.check_condition.last_checked_at);
        raw.check_condition.last_checked_at = "2026-08-22T02:00:00.000Z";
        const rp2 = A.artifactsNormalizeProject(raw, { now: T0 });
        check("合法 last_checked_at 归一化后保留", rp2.check_condition.last_checked_at === "2026-08-22T02:00:00.000Z");
    }
    p = A.artifactsSetCheckCondition(p, null, { now: T0 + 6000 });
    check("清空 check_condition 置 null", p.check_condition === null);
    p = A.artifactsSetBaseline(p, { uids: ["x"] }, { now: T0 + 7000 });
    check("无 check_condition 时 baseline 不动（原样返回）", p.check_condition === null);
}

/* ============================================================================
 * 7. provenance 全字段（导出中心直接消费）
 * ========================================================================== */
{
    const prov = A.artifactsProvenance({
        query: "人类肺癌 10x",
        retrieval_params: { strategy: "fixed", recall: "off" },
        search_trace: [{ step: "local_semantic" }],
        filters: { active: ["物种:人类"], suppressed: ["小鼠"], lenient: [] },
        corpus_digest: "sha256:abc",
        policy_id: { ranking: { strategy: "auto", rerank: "llm", recall: "cross_encoder" }, schema: "biodata-policy-id/1" },
        trace_turn_id: "t-1",
        result: { uids: ["a", "b", "a"], truncated: false },
    }, { now: T0 });
    check("query/retrieval_params/search_trace 透传", prov.query === "人类肺癌 10x"
        && prov.retrieval_params.strategy === "fixed" && prov.search_trace.length === 1);
    check("filters 三桶齐全", prov.filters.active.length === 1 && prov.filters.suppressed[0] === "小鼠" && prov.filters.lenient.length === 0);
    check("corpus_digest/policy_id/trace_turn_id 透传", prov.corpus_digest === "sha256:abc"
        && prov.policy_id.startsWith("bpol-json:") && prov.policy_id.indexOf("[object Object]") < 0 && prov.trace_turn_id === "t-1");
    check("result uids 去重 + truncated 落齐", JSON.stringify(prov.result.uids) === JSON.stringify(["a", "b"]) && prov.result.truncated === false);
    check("retrieved_at 由时钟落戳", prov.retrieved_at === iso(T0));
    const empty = A.artifactsProvenance(undefined, { now: T0 });
    check("缺省 provenance 全字段空值不抛", empty.query === "" && empty.filters.active.length === 0 && empty.result.uids.length === 0);
}

/* ============================================================================
 * 8. 备份导出 / 导入解析（纯函数段）
 * ========================================================================== */
{
    A.artifactsSetClock(() => T0);
    const projects = [fixture()].map((x) => A.artifactsNormalizeProject(x, { now: T0 }));
    const text = A.artifactsExportText(projects, { scope: "u1", exported_at: T0 });
    const doc = JSON.parse(text);
    check("备份带 schema 标记与版本", doc.schema === A.ARTIFACTS_BACKUP_SCHEMA && doc.schema_version === A.ARTIFACTS_SCHEMA);
    check("备份带导出时间与 scope 元信息", doc.exported_at === iso(T0) && doc.scope === "u1");
    check("备份 count 与 projects 对齐", doc.count === 1 && doc.projects.length === 1);

    const ok = A.artifactsParseBackup(text);
    check("解析成功", ok.ok === true && ok.projects.length === 1, JSON.stringify(ok));
    check("解析出的课题仍合法", A.artifactsValidateProject(ok.projects[0]).length === 0);
    check("坏 JSON 如实拒绝", A.artifactsParseBackup("不是json{").ok === false);
    check("schema 标记不符拒绝", A.artifactsParseBackup(JSON.stringify({ schema: "别的/1", schema_version: 1, projects: [] })).ok === false);
    check("未来版本拒绝", A.artifactsParseBackup(JSON.stringify({ schema: A.ARTIFACTS_BACKUP_SCHEMA, schema_version: 99, projects: [] })).ok === false);
    const bad = A.artifactsParseBackup(JSON.stringify({ schema: A.ARTIFACTS_BACKUP_SCHEMA, schema_version: 1, projects: [{ project_id: "", name: "" }] }));
    check("备份内不合法课题拒绝", bad.ok === false && bad.error.indexOf("project_id") > 0, JSON.stringify(bad));
}

/* ============================================================================
 * 9. IDB CRUD 端到端（fake 注入）
 * ========================================================================== */
async function crudSuite() {
    const fake = freshDb();
    A.artifactsSetClock(() => T0);
    await A.artifactsOpen();

    const created = await A.artifactsCreateProject("u1", fixture(), { now: T0 });
    check("create 返回规整后的记录", created.project_id === "p1" && created.schema_version === A.ARTIFACTS_SCHEMA);

    const got = await A.artifactsGetProject("u1", "p1");
    check("get 读回同一条（字段规整）", got.project_id === "p1" && got.name === "肺癌单细胞" && got.candidates.length === 0, JSON.stringify(got));

    await expectReject("重复 create 报 ConstraintError", () => A.artifactsCreateProject("u1", fixture(), { now: T0 }), "ConstraintError");
    check("get 不存在 → null", await A.artifactsGetProject("u1", "nope") === null);
    check("get 别的 scope → null（隔离在键层）", await A.artifactsGetProject("u2", "p1") === null);

    const updated = await A.artifactsUpdateProject("u1", "p1", (p) => A.artifactsAddCandidate(p, "c1", { now: T0 + 5000 }), { now: T0 + 5000 });
    check("update 经 mutator 落新状态", updated.candidates.length === 1 && updated.candidates[0].status === "待核验");
    check("update 刷新 updated_at", updated.updated_at === iso(T0 + 5000), updated.updated_at);
    const again = await A.artifactsGetProject("u1", "p1");
    check("写穿缓存读到新值", again.candidates.length === 1, JSON.stringify(again));

    await expectReject("update 不存在报 NotFoundError", () => A.artifactsUpdateProject("u1", "nope", (p) => p, { now: T0 }), "NotFoundError");
    await expectReject("update 改 project_id 被拒", () => A.artifactsUpdateProject("u1", "p1", (p) => Object.assign({}, p, { project_id: "p2" }), { now: T0 }), "InvalidStateError");
    await expectReject("update mutator 返回空被拒", () => A.artifactsUpdateProject("u1", "p1", () => null, { now: T0 }), "InvalidStateError");

    const list1 = await A.artifactsListProjects("u1");
    check("list 只有当前 scope 的课题", list1.length === 1 && list1[0].project_id === "p1", JSON.stringify(list1));

    check("delete 存在 → true", await A.artifactsDeleteProject("u1", "p1") === true);
    check("delete 不存在 → false", await A.artifactsDeleteProject("u1", "p1") === false);
    check("delete 后 get 为 null", await A.artifactsGetProject("u1", "p1") === null);
    check("delete 后 list 为空", (await A.artifactsListProjects("u1")).length === 0);
}

async function isolationSuite() {
    const fake = freshDb();
    A.artifactsSetClock(() => T0);
    await A.artifactsOpen();
    await A.artifactsCreateProject("u1", fixture({ project_id: "p1", name: "甲的课题" }), { now: T0 });
    await A.artifactsCreateProject("u2", fixture({ project_id: "p1", name: "乙的课题" }), { now: T0 });
    await A.artifactsCreateProject("", fixture({ project_id: "anon1", name: "匿名课题" }), { now: T0 });
    const l1 = await A.artifactsListProjects("u1");
    const l2 = await A.artifactsListProjects("u2");
    const lAnon = await A.artifactsListProjects("");
    check("u1 只见自己的 p1", l1.length === 1 && l1[0].name === "甲的课题", JSON.stringify(l1.map((x) => x.name)));
    check("u2 只见自己的 p1（同 id 不同 scope 互不覆盖）", l2.length === 1 && l2[0].name === "乙的课题", JSON.stringify(l2.map((x) => x.name)));
    check("匿名命名空间独立", lAnon.length === 1 && lAnon[0].name === "匿名课题", JSON.stringify(lAnon.map((x) => x.name)));

    // 排序：updated_at 倒序（新写的在前）
    await A.artifactsUpdateProject("u1", "p1", (p) => p, { now: T0 + 30000 });
    await A.artifactsCreateProject("u1", fixture({ project_id: "p2", name: "更新的课题" }), { now: T0 + 60000 });
    const ordered = await A.artifactsListProjects("u1");
    check("list 按 updated_at 倒序（最新在前）", ordered[0].project_id === "p2" && ordered[1].project_id === "p1",
        JSON.stringify(ordered.map((x) => x.project_id + "@" + x.updated_at)));
}

async function quotaSuite() {
    const fake = freshDb();
    A.artifactsSetClock(() => T0);
    fake.forceQuota = true;
    await A.artifactsOpen();
    await expectReject("配额满时 create 报 QuotaExceededError",
        () => A.artifactsCreateProject("u1", fixture(), { now: T0 }), "QuotaExceededError");
    await expectReject("配额满时 save 报 QuotaExceededError",
        () => A.artifactsSaveProject("u1", fixture(), { now: T0 }), "QuotaExceededError");
    // 配额恢复后照常写
    fake.forceQuota = false;
    const p = await A.artifactsCreateProject("u1", fixture(), { now: T0 });
    check("配额恢复后写入成功", p.project_id === "p1");
    // 存储预估：node 无 navigator → null（不抛）
    const est = await A.artifactsStorageEstimate();
    check("无 navigator 时存储预估为 null", est === null, JSON.stringify(est));
}

async function importSuite() {
    const fake = freshDb();
    A.artifactsSetClock(() => T0);
    await A.artifactsOpen();
    // 预置一条会被覆盖的同 id 课题
    await A.artifactsCreateProject("u1", fixture({ project_id: "p1", name: "旧名字" }), { now: T0 });
    const backupText = A.artifactsExportText([
        fixture({ project_id: "p1", name: "新名字" }),
        fixture({ project_id: "p2", name: "全新课题" }),
    ], { scope: "u1", exported_at: T0 });

    const res = await A.artifactsImportBackup("u1", backupText, { now: T0 });
    check("导入全部成功", res.ok === true && res.imported === 2 && res.failed.length === 0, JSON.stringify(res));
    const p1 = await A.artifactsGetProject("u1", "p1");
    check("同 id upsert 覆盖", p1.name === "新名字", JSON.stringify(p1));
    check("导入后 p2 存在", (await A.artifactsGetProject("u1", "p2")).name === "全新课题");
    check("导入不影响其它 scope", (await A.artifactsListProjects("u2")).length === 0);

    // 部分失败：配额恢复为 false，先制造一条失败再验证失败名单
    fake.forceQuota = true;
    const partialText = A.artifactsExportText([fixture({ project_id: "p3" })], {},);
    const partial = await A.artifactsImportBackup("u1", partialText, { now: T0 });
    check("单条失败不上报成功", partial.imported === 0 && partial.failed.length === 1, JSON.stringify(partial));
    check("失败原因带错误名", partial.failed[0].error.indexOf("QuotaExceededError") === 0, JSON.stringify(partial.failed[0]));

    await expectReject("坏文本导入直接拒绝", () => A.artifactsImportBackup("u1", "垃圾", { now: T0 }), "InvalidStateError");
    await expectReject("未来版本导入拒绝", () => A.artifactsImportBackup("u1",
        JSON.stringify({ schema: A.ARTIFACTS_BACKUP_SCHEMA, schema_version: 99, projects: [] }), { now: T0 }), "InvalidStateError");
}

async function profileSuite() {
    freshDb();
    A.artifactsSetClock(() => T0);
    await A.artifactsOpen();
    await A.artifactsCreateProject("u1", fixture(), { now: T0 });
    A.artifactsSetActiveProjectId("p1");
    check("活动课题句柄可读", A.artifactsActiveProjectId() === "p1");
    A.artifactsOnProfileSwitched();
    check("切换后活动课题句柄清空", A.artifactsActiveProjectId() === null);
    check("切换后同 id 再设生效", (A.artifactsSetActiveProjectId("p1"), A.artifactsActiveProjectId() === "p1"));
    A.artifactsSetActiveProjectId(null);
    check("SetActive(null) 清空", A.artifactsActiveProjectId() === null);
    // 数据本体仍在库里：切换只断引用不删数据（设计约定）
    const after = await A.artifactsGetProject("u1", "p1");
    check("切换后数据仍在 IndexedDB（只断引用不删数据）", after !== null && after.project_id === "p1");
    // 缓存清空后 get 走 DB 仍能读回（缓存刷新验证：写回新值，缓存命中必须是新值）
    await A.artifactsUpdateProject("u1", "p1", (p) => Object.assign({}, p, { name: "改过的名字" }), { now: T0 + 1000 });
    check("写穿后缓存命中新值", (await A.artifactsGetProject("u1", "p1")).name === "改过的名字");
    A.artifactsOnProfileSwitched();
    const reread = await A.artifactsGetProject("u1", "p1");
    check("清缓存后从 DB 读回一致", reread.name === "改过的名字", JSON.stringify(reread));
}

async function roundTripSuite() {
    freshDb();
    A.artifactsSetClock(() => T0);
    await A.artifactsOpen();
    await A.artifactsCreateProject("u1", fixture({ project_id: "p1", candidates: [{ uid: "c1" }] }), { now: T0 });
    const exported = await A.artifactsExportAll("u1", { now: T0 });
    check("exportAll 返回文本与课题列表", exported.projects.length === 1 && typeof exported.text === "string");
    const parsed = A.artifactsParseBackup(exported.text);
    check("导出文本可被解析回等量课题", parsed.ok === true && parsed.projects.length === 1
        && parsed.projects[0].project_id === "p1" && parsed.projects[0].candidates[0].uid === "c1", JSON.stringify(parsed));
}

/* ============================================================================
 * 全部 async 套件串行执行：fake 工厂是模块级单例，并行会互相覆盖连接状态
 * ========================================================================== */
(async function main() {
    try {
        await crudSuite();
        await isolationSuite();
        await quotaSuite();
        await importSuite();
        await profileSuite();
        await roundTripSuite();
    } catch (e) {
        failures++;
        console.log("  FAIL suite 顶层抛错：", e && e.stack ? e.stack : e);
    }
    console.log(failures ? `\n${failures} 条失败` : "\n全部通过\nOK artifacts_spec.mjs");
    process.exit(failures ? 1 : 0);
})();
