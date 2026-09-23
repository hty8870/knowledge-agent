"use strict";
import { usagePolicyRef } from "#usage_core";
/* 追踪数据层（IndexedDB adapter + 追踪 CRUD + JSON 备份导入导出 + profile 生命周期钩子）
 *
 * ## 本文件是什么 / 不是什么
 *
 * - 是：追踪（project）的**纯数据层**。设计见 `设计文档
 *   §2（概念模型）/ §3.1（数据模型）/ §3.2（存储裁决）。更新检查 diff 闭环与导出中心
 *   将直接建在本文件的 schema 之上，字段语义注释按「更新检查/导出中心直接消费」的标准写。
 * - 不是：UI。零 DOM、零窗口、零 localStorage、零网络——界面、上下文卡、活动 tab 持久化全在
 * UI 层，本文件不碰。明确「localStorage 只存活动 tab/活动追踪 id
 *   等轻量 UI 态」，那是 UI 层的职责，数据层不许越界。
 * - 唯一依赖 `#usage_core` 的纯函数 `usagePolicyRef`，保证追踪 provenance 与遥测使用同一策略串；
 *   两者均无 DOM/存储/网络副作用，node 规格仍可直跑。
 *
 * ## 存储模型（裁决）
 *
 * - IndexedDB 新库 `biodata-artifacts`（本文件常量 ARTIFACTS_DB_NAME），单 object store `projects`。
 * - **复合主键带 profile scope**：记录键 `pk = scope + "\u0000p:" + project_id` 的确定性编码
 *   （scope 为空串 = 匿名命名空间，与 core.js `currentAccountScope()`/`nsKeyFor` 的账户语义逐字对齐；
 *   登录用户 = 服务端随机 id）。之所以不直接用 IDB 数组 keyPath `["scope","project_id"]`：
 *   数组键在真机与测试替身之间行为差异多（对象按引用比较等），字符串编码确定、可逆、可断言，
 *   仓库里 `nsKeyFor(base, scope)` 的 `base::u:<scope>` 前缀式编码已有先例。另建 `by_scope` 索引
 * 支持按 profile 列追踪。**绝不上传**（遥测只记计数，追踪内容不进遥测—— 隐私红线）。
 * - 写路径统一捕获 `QuotaExceededError` 并如实报错（reject 的 Error.name 即错误名）；
 *   `navigator.storage.estimate()` 仅作预警（artifactsStorageEstimate），**不自动淘汰**用户内容——
 *   设计 §3.2：清理只允许用户明示（可重建的 diff 临时快照除外，那是更新检查的事）。
 *
 * ## profile 生命周期（accounts.js 接线点）
 *
 * accounts.js 的重置点（登录/登出/切换账户）调用 `artifactsOnProfileSwitched()`：
 * 清空内存缓存与活动追踪句柄。追踪数据本体按 scope 隔离在 IndexedDB 里，切换只断引用不删数据。
 * 活动追踪句柄（artifactsActiveProjectId/SetActiveProjectId）是纯内存 UI 态；
 * 活动 tab 的**持久化**是 localStorage 的事，归 UI 层。
 *
 * ## 纯核 / adapter 分层（node 可测的全部理由）
 *
 * 上层函数（normalize/validate/备份序列化/候选与检查条件变换）全是**纯函数**：不碰 IDB、
 * 时间一律经 `artifactsSetClock` 注入点（默认 Date.now）——node 规格可逐字段断言。
 * adapter 层（artifactsOpen 起的 CRUD）经 `artifactsSetIdbFactory` 注入 IndexedDB 工厂：
 * node 规格自带进程内 fake IndexedDB 端到端断言真行为；真机走全局 `indexedDB`。
 */

/* ============================================================================
 * 常量与 schema 版本
 * ----------------------------------------------------------------------------
 * ARTIFACTS_SCHEMA 是**数据 schema 版本**（每条追踪记录必备 schema_version 字段）。
 * 更新检查/导出中心落新字段时 +1 并在 artifactsParseBackup 的迁移段补逐版本迁移（见该函数注释），
 * 不许原地改字段后让老数据悄悄缺字段——老库里的追踪是用户的真内容。
 * ========================================================================== */
export const ARTIFACTS_SCHEMA = 1;                        // 追踪记录 schema 版本（当前 v1）
export const ARTIFACTS_DB_NAME = "biodata-artifacts";     // IndexedDB 库名（设计 §3.2）
export const ARTIFACTS_DB_VERSION = 1;                    // 库结构版本（加 store/索引时 +1）
export const ARTIFACTS_STORE = "projects";                // object store 名
export const ARTIFACTS_SCOPE_INDEX = "by_scope";          // profile scope 索引
export const ARTIFACTS_BACKUP_SCHEMA = "biodata-artifacts-backup/1";  // 备份 JSON 的 schema 标记

/* 候选状态枚举（原文：候选|待核验|已核验|已排除）。
   语义不是强状态机（数据层不裁决业务流），只钉枚举值与默认值：
   - 待核验 = 新发现未审（watch 新增一律落这里——「新候选默认待核验；没有自动纳入」）
   - 候选 = 已进入候选视野、尚未终裁；
   - 已核验 = 用户确认纳入；已排除 = 用户决定排除（二者是用户终态，verified_at 落戳见
     artifactsSetCandidateStatus）。 */
export const PROJECT_STATUS = Object.freeze({
    CANDIDATE: "候选",
    PENDING: "待核验",
    VERIFIED: "已核验",
    EXCLUDED: "已排除",
});
export const PROJECT_STATUS_VALUES = Object.freeze(Object.values(PROJECT_STATUS));
/* 新候选默认状态—— 硬性：**任何自动流程不得直接改纳入表**，只能默认「待核验」
   等用户逐条比较后经 artifactsSetCandidateStatus 决定去留。 */
export const DEFAULT_CANDIDATE_STATUS = PROJECT_STATUS.PENDING;

/* 追踪字段硬限制（纳入/排除条件各 ≤8 条）。 */
export const ARTIFACTS_LIMITS = Object.freeze({
    MAX_INCLUDE: 8,   // 纳入条件最多 8 条
    MAX_EXCLUDE: 8,   // 排除条件最多 8 条
});

/* ============================================================================
 * 模块内状态（有状态区集中在这里，其余全纯函数）
 * ----------------------------------------------------------------------------
 * _db：已打开的 IDBDatabase。库实例与 profile 无关（scope 编码在键里），全局一份。
 * _dbFactory：IndexedDB 工厂。默认取 globalThis.indexedDB；node 规格 / 无标准实现宿主
 *             经 artifactsSetIdbFactory 注入替身。换工厂后需 artifactsClose() 重开。
 * _now：时钟注入点（null = Date.now）。规格注入固定时钟保证时间戳确定性。
 * _cacheScope/_cache：**当前 profile 的内存镜像**（写穿 + get 命中；list 永远以 DB 为准并回灌）。
 * 切换 profile 时经 artifactsOnProfileSwitched 清空—— 的「清内存缓存」。
 * _activeProjectId：活动追踪句柄（纯内存 UI 态，UI 层经 Set/Active 存取）。
 * ========================================================================== */
let _db = null;
let _dbFactory = null;
let _now = null;
let _cacheScope = null;
let _cache = null;
let _activeProjectId = null;
let _expSeq = 0;   // 导出记录 id 的模块内单调后缀（同毫秒内多次导出不撞名）

/* ---------- 时间与时钟注入（确定性） ---------- */
export function artifactsNow() { return _now ? _now() : Date.now(); }
export function artifactsSetClock(fn) { _now = (typeof fn === "function") ? fn : null; }
/* ts 缺省 = 当前时钟；数字毫秒或 ISO 串均可 → 输出 ISO 串（写入与排序的统一时间格式）。 */
export function artifactsIsoNow(ts) {
    const t = (ts === undefined || ts === null) ? artifactsNow() : ts;
    let d;
    if (t instanceof Date) {
        d = t;
    } else if (typeof t === "string" && t.trim() !== "" && !Number.isFinite(Number(t))) {
        /* ISO 字符串（如服务端响应的 checked_at）：Number()=NaN，走数值路径会被 `|| 0`
           错成 epoch 0（2026-08-22 修复：追踪「上次检查于 20687 天前」事故）。 */
        d = new Date(t);
    } else {
        d = new Date(Number(t) || 0);
    }
    return Number.isFinite(d.getTime()) ? d.toISOString() : "";
}

/* ---------- profile scope 规范化（与 core.js currentAccountScope 语义对齐） ---------- */
export function artifactsScope(scope) {
    /* "" = 匿名命名空间（旧行为兼容）；其余一律字符串化。拒绝对象当 scope（防 `[object Object]`
       这种隐蔽串扰——不同对象会编出同一个键）。 */
    return (scope === null || scope === undefined) ? "" : String(scope);
}

/* ---------- 复合主键：scope + project_id 的确定性编码 ---------- */
export function artifactsKey(scope, projectId) {
    return artifactsScope(scope) + "\u0000p:" + String(projectId || "");
}
export function artifactsKeyParts(pk) {
    /* 逆编码。非法串（无分隔符）视作「匿名 scope + 整体当 project_id」——防御性解读，不抛。 */
    const s = String(pk || "");
    const i = s.indexOf("\u0000p:");   // 分隔符三字符：\u0000 p :
    if (i < 0) return { scope: "", projectId: s };
    return { scope: s.slice(0, i), projectId: s.slice(i + 3) };
}

/* ============================================================================
 * 纯函数区（无 DOM / 无 IDB / 无网络 / 无墙钟——node 规格直接 import 断言）
 * ========================================================================== */

function _strList(v) {
    return (Array.isArray(v) ? v : []).map((x) => String(x || "").trim()).filter(Boolean);
}

function _normCondition(v) { return String(v || "").trim(); }

/* 候选记录规整。uid 必填；状态非法/缺失一律回落默认「待核验」——**绝不把无效状态悄悄
   降成「已核验」**（那等于替用户做决定）；已核验/已排除的老记录保持原状。 */
function _normCandidate(c, nowIso) {
    if (!c || typeof c !== "object") return null;
    const uid = String(c.uid || "").trim();
    if (!uid) return null;
    const status = PROJECT_STATUS_VALUES.includes(String(c.status || "")) ? String(c.status) : DEFAULT_CANDIDATE_STATUS;
    return {
        uid: uid,
        status: status,
        reason: String(c.reason || "").trim(),
        verified_at: c.verified_at ? String(c.verified_at) : "",
        added_at: c.added_at ? String(c.added_at) : nowIso,
    };
}

/* 确定性检索规格（check_condition.spec）。字段名与 `/api/watch/check`
   入参**逐一对齐**（= /api/recommend 入参子集）：query（解析后的
   检索词字符串）/sources/facet_filters[{dim,value}（单值，同 workflow.sanitize_facet_filters
   口径）]/suppressed_constraints/lenient_dims/date_from/date_to/spec_version（字符串，
   当前 "v1"，与 record_fingerprint_schema 同版）。spec_version 是规格自身的版本，
   与追踪记录 schema_version 分开。 */
function _normSpec(s) {
    s = (s && typeof s === "object") ? s : {};
    return {
        spec_version: String(s.spec_version || "v1").trim() || "v1",
        query: String(s.query || "").trim(),
        sources: _strList(s.sources),
        facet_filters: (Array.isArray(s.facet_filters) ? s.facet_filters : [])
            .map((f) => (f && typeof f === "object" && String(f.dim || "").trim() && String(f.value || "").trim())
                ? { dim: String(f.dim).trim(), value: String(f.value).trim() } : null)
            .filter(Boolean),
        suppressed_constraints: _strList(s.suppressed_constraints),
        lenient_dims: _strList(s.lenient_dims),
        date_from: String(s.date_from || "").trim(),
        date_to: String(s.date_to || "").trim(),
    };
}

/* 基线快照（check_condition.baseline）：uids 是**无序集合**——展平成数组保存
   比较按集合语义（diff 不得按数组顺序比对）；fingerprints = uid → 语义指纹
   result_total/truncated 如实记录「命中 ≤200 存全量、>200 truncated=true 且不得声称
   某条已从全部结果消失」。 */
function _normBaseline(b, nowIso) {
    if (!b || typeof b !== "object") return null;
    return {
        uids: artifactsUidSet(b.uids),
        fingerprints: (b.fingerprints && typeof b.fingerprints === "object") ? b.fingerprints : {},
        result_total: Number(b.result_total) || 0,
        truncated: b.truncated === true,
        generated_at: b.generated_at ? String(b.generated_at) : nowIso,
    };
}

/* 读时修复：旧版 artifactsIsoNow 不接受 ISO 字符串，曾把
   last_checked_at 落成 epoch 0；本功能 2026-08 才存在，早于 2026-08-01 的读数一律视为
   「从未检查」（不展示、不伪造），下次成功检查会覆写正确值。 */
function _normCheckedAt(v) {
    const s = v ? String(v) : "";
    if (!s) return "";
    const t = new Date(s).getTime();
    if (!Number.isFinite(t) || t < Date.UTC(2026, 7, 1)) return "";
    return s;
}

function _normCheckCondition(cc, nowIso) {
    if (!cc || typeof cc !== "object") return null;
    return {
        display_query: String(cc.display_query || "").trim(),   // 原始文本，仅展示（设计 §3.1）
        spec: _normSpec(cc.spec),
        baseline: _normBaseline(cc.baseline, nowIso),
        last_checked_at: _normCheckedAt(cc.last_checked_at),
    };
}

/* provenance 全字段（为导出与方法草稿服务；范式同 content/task_pack.py 的
   provenance.json：query / retrieval_params / 检索日期 / scope 语义字段）。P5 导出中心
   直接消费本对象，字段语义如下： */
function _normProvenance(p) {
    if (!p || typeof p !== "object") return null;
    return {
        query: String(p.query || "").trim(),                          // 原始 query（用户原话）
        retrieval_params: (p.retrieval_params && typeof p.retrieval_params === "object")
            ? p.retrieval_params : {},                                // 确定性请求参数（strategy=fixed 等）
        search_trace: Array.isArray(p.search_trace) ? p.search_trace : [],  // 检索轨迹（与后端 search_trace 同构）
        filters: (p.filters && typeof p.filters === "object") ? {
            active: _strList(p.filters.active),       // 生效中的分面筛选
            suppressed: _strList(p.filters.suppressed), // 被抑制的筛选
            lenient: _strList(p.filters.lenient),     // 宽容态维度
        } : { active: [], suppressed: [], lenient: [] },
        corpus_digest: String(p.corpus_digest || "").trim(),          // 语料快照 digest
        retrieved_at: String(p.retrieved_at || "").trim(),            // 检索日期（ISO）
        policy_id: usagePolicyRef(p.policy_id),                       // 与遥测同一规范策略串
        trace_turn_id: String(p.trace_turn_id || "").trim(),          // 可选：关联的对话轮 id
        result: (p.result && typeof p.result === "object") ? {
            uids: artifactsUidSet(p.result.uids),                     // 结果 uid 集合
            truncated: p.result.truncated === true,                   // 截断状态（>200）
        } : { uids: [], truncated: false },
    };
}

/* 导出记录台账（时间、目录版本、相对上次导出的变化、可命名关键节点）。
   P5 导出中心在此之上建 UI 与「默认只展示最新、历史折叠」——这里只保证台账条目形状稳定。 */
function _normExport(r, nowIso) {
    if (!r || typeof r !== "object") r = {};
    _expSeq += 1;
    const nowMs = artifactsNow();
    return {
        id: String(r.id || "exp-" + nowMs + "-" + _expSeq.toString(36)),
        kind: String(r.kind || "export").trim(),       // 导出类型（下载清单/引文/筛选记录/全材料）
        name: String(r.name || "").trim(),             // 可命名关键节点（如「初筛」「投稿前复核」）
        at: r.at ? String(r.at) : nowIso,              // 导出时间
        dataset_version: String(r.dataset_version || "").trim(),  // 导出时的目录版本
        changes: (r.changes && typeof r.changes === "object") ? r.changes : null,  // 相对上次导出的变化
        note: String(r.note || "").trim(),             // 备注（自由文本，不进遥测）
    };
}

/* activity[]：关联对话/运行的轻量引用（可选）。type 宽松收（conv/run）
   只保证是字符串；ref 是引用（如 convId）；at 是发生时间。 */
function _normActivity(a, nowIso) {
    if (!a || typeof a !== "object") return null;
    if (!String(a.ref || "").trim() && !String(a.type || "").trim()) return null;
    return {
        type: String(a.type || "").trim(),
        ref: String(a.ref || "").trim(),
        at: a.at ? String(a.at) : nowIso,
    };
}

/* ---------- 无序集合工具（baseline.uids / provenance.result.uids 共用） ---------- */
export function artifactsUidSet(uids) {
    /* 去重、保插入序。语义是**集合**：P4 diff 按 uid 集合比较「真实新增/消失」，
       绝不按数组下标比对名次（不比较名次）。 */
    const seen = new Set();
    const out = [];
    (Array.isArray(uids) ? uids : []).forEach((u) => {
        const s = String(u || "").trim();
        if (s && !seen.has(s)) { seen.add(s); out.push(s); }
    });
    return out;
}

/* ---------- 追踪记录规整（写库/读库统一入口） ----------
   白名单化：只认下表字段，未知键一律不进库（防异物键漂移——P4/P5 扩展走 schema_version
   迁移而不是「容忍任意键」）。数组字段强制数组、字符串强制字符串、时间戳落 ISO。
   **不静默截断**：纳入/排除超 8 条时保留全量、由 artifactsValidateProject 如实报错
   （如实第一：数据层不许悄悄丢用户内容；数量约束是校验不是裁剪）。 */
export function artifactsNormalizeProject(input, opts) {
    opts = opts || {};
    const src = (input && typeof input === "object") ? input : {};
    const nowIso = artifactsIsoNow(opts.now);
    const projectId = String(src.project_id || "").trim();
    const include = (Array.isArray(src.include_conditions) ? src.include_conditions : [])
        .map(_normCondition).filter(Boolean);
    const exclude = (Array.isArray(src.exclude_conditions) ? src.exclude_conditions : [])
        .map(_normCondition).filter(Boolean);
    return {
        schema_version: ARTIFACTS_SCHEMA,          // 必备（设计/包要求）：记录结构版本
        project_id: projectId,                     // profile 命名空间内唯一
        name: String(src.name || "").trim(),       // 追踪名称
        goal: String(src.goal || "").trim(),       // 研究目标
        include_conditions: include,               // 纳入条件（≤8 条，超限由校验报错）
        exclude_conditions: exclude,               // 排除条件（≤8 条，超限由校验报错）
        candidates: (Array.isArray(src.candidates) ? src.candidates : [])
            .map((c) => _normCandidate(c, nowIso)).filter(Boolean),  // 候选表（新候选默认「待核验」）
        check_condition: _normCheckCondition(src.check_condition, nowIso),  // 可空
        exports: (Array.isArray(src.exports) ? src.exports : [])
            .map((r) => _normExport(r, nowIso)),   // 导出记录台账
        activity: (Array.isArray(src.activity) ? src.activity : [])
            .map((a) => _normActivity(a, nowIso)).filter(Boolean),  // 轻量引用
        provenance: _normProvenance(src.provenance),               // 全字段溯源（可空）
        created_at: src.created_at ? String(src.created_at) : nowIso,   // 保留原值，缺省落戳
        updated_at: src.updated_at ? String(src.updated_at) : nowIso,   // 写路径自行刷新（见各写函数）
    };
}

/* ---------- 结构校验（返回错误串数组；空数组 = 合法） ---------- */
export function artifactsValidateProject(project) {
    const errors = [];
    const p = (project && typeof project === "object") ? project : {};
    if (!String(p.project_id || "").trim()) errors.push("project_id 缺失");
    if (!String(p.name || "").trim()) errors.push("追踪名称缺失");
    if (!Array.isArray(p.include_conditions)) errors.push("include_conditions 必须是数组");
    else if (p.include_conditions.length > ARTIFACTS_LIMITS.MAX_INCLUDE) {
        errors.push("纳入条件超过 " + ARTIFACTS_LIMITS.MAX_INCLUDE + " 条");
    } else {
        p.include_conditions.forEach((c, i) => { if (!String(c || "").trim()) errors.push("纳入条件第 " + (i + 1) + " 条为空"); });
    }
    if (!Array.isArray(p.exclude_conditions)) errors.push("exclude_conditions 必须是数组");
    else if (p.exclude_conditions.length > ARTIFACTS_LIMITS.MAX_EXCLUDE) {
        errors.push("排除条件超过 " + ARTIFACTS_LIMITS.MAX_EXCLUDE + " 条");
    } else {
        p.exclude_conditions.forEach((c, i) => { if (!String(c || "").trim()) errors.push("排除条件第 " + (i + 1) + " 条为空"); });
    }
    (Array.isArray(p.candidates) ? p.candidates : []).forEach((c, i) => {
        if (!c || !String(c.uid || "").trim()) { errors.push("候选第 " + (i + 1) + " 条缺 uid"); return; }
        if (!PROJECT_STATUS_VALUES.includes(String(c.status || ""))) errors.push("候选「" + String(c.uid) + "」状态非法：" + String(c.status));
    });
    if (p.check_condition !== null && p.check_condition !== undefined) {
        if (typeof p.check_condition !== "object") errors.push("check_condition 必须是对象");
        else if (typeof p.check_condition.spec !== "object") errors.push("check_condition.spec 缺失");
    }
    return errors;
}

/* ---------- 纯变换：候选流（都返回新对象，不改入参） ---------- */
export function artifactsAddCandidate(project, uid, opts) {
    /* 加候选。**一律默认「待核验」**（硬性：任何自动流程不得直接改纳入表）——
       本函数不接受 status 参数，改状态必须走 artifactsSetCandidateStatus。同 uid 幂等去重。 */
    opts = opts || {};
    const p = artifactsNormalizeProject(project, opts);
    const u = String(uid || "").trim();
    if (!u || p.candidates.some((c) => c.uid === u)) return p;
    p.candidates = p.candidates.concat([{
        uid: u, status: DEFAULT_CANDIDATE_STATUS, reason: "",
        verified_at: "", added_at: artifactsIsoNow(opts.now),
    }]);
    p.updated_at = artifactsIsoNow(opts.now);
    return p;
}

export function artifactsSetCandidateStatus(project, uid, status, reason, opts) {
    /* 改候选状态（用户逐条比较后的裁决点）。verified_at 只在进入「已核验/已排除」终态时落戳
       ——两个终态都需要理由（reason 不传则保留原值）。uid 不存在或状态非法 → 原样返回。 */
    opts = opts || {};
    const p = artifactsNormalizeProject(project, opts);
    const u = String(uid || "").trim();
    const st = PROJECT_STATUS_VALUES.includes(String(status || "")) ? String(status) : null;
    if (!u || !st) return p;
    const nowIso = artifactsIsoNow(opts.now);
    p.candidates = p.candidates.map((c) => {
        if (c.uid !== u) return c;
        const next = {
            uid: c.uid, status: st,
            reason: (reason === undefined) ? c.reason : String(reason).trim(),
            added_at: c.added_at,
        };
        if (st === PROJECT_STATUS.VERIFIED || st === PROJECT_STATUS.EXCLUDED) next.verified_at = nowIso;
        else next.verified_at = c.verified_at;
        return next;
    });
    p.updated_at = nowIso;
    return p;
}

export function artifactsRemoveCandidate(project, uid, opts) {
    opts = opts || {};
    const p = artifactsNormalizeProject(project, opts);
    const u = String(uid || "").trim();
    if (!u) return p;
    const next = p.candidates.filter((c) => c.uid !== u);
    if (next.length === p.candidates.length) return p;
    p.candidates = next;
    p.updated_at = artifactsIsoNow(opts.now);
    return p;
}

/* ---------- 纯变换：check_condition ---------- */
export function artifactsSetCheckCondition(project, cc, opts) {
    /* 写/清检查条件（cc 为 null/undefined = 清除）。spec 经 _normSpec 归整——保存的是
       解析后的确定性检索规格，不是用户原始文本（display_query 单独存、仅展示）。 */
    opts = opts || {};
    const p = artifactsNormalizeProject(project, opts);
    p.check_condition = cc ? _normCheckCondition(cc, artifactsIsoNow(opts.now)) : null;
    p.updated_at = artifactsIsoNow(opts.now);
    return p;
}

export function artifactsSetBaseline(project, baseline, opts) {
    /* 仅回填 check_condition.baseline（watch-check 保存/更新基线，：基线用确定性管线
       独立生成，不拿显示结果冒充）。无 check_condition 时不动。 */
    opts = opts || {};
    const p = artifactsNormalizeProject(project, opts);
    if (!p.check_condition) return p;
    p.check_condition.baseline = baseline ? _normBaseline(baseline, artifactsIsoNow(opts.now)) : null;
    p.updated_at = artifactsIsoNow(opts.now);
    return p;
}

export function artifactsTouchCheckedAt(project, ts, opts) {
    /* 检查完成后记「上次检查于」时间戳（双时间戳之一：「该追踪检查于」）。 */
    opts = opts || {};
    const p = artifactsNormalizeProject(project, opts);
    if (!p.check_condition) return p;
    p.check_condition.last_checked_at = artifactsIsoNow(ts === undefined ? opts.now : ts);
    p.updated_at = artifactsIsoNow(opts.now);
    return p;
}

/* ---------- 纯变换：台账与轻量引用 ---------- */
export function artifactsAddExport(project, record, opts) {
    opts = opts || {};
    const p = artifactsNormalizeProject(project, opts);
    p.exports = p.exports.concat([_normExport(record, artifactsIsoNow(opts.now))]);
    p.updated_at = artifactsIsoNow(opts.now);
    return p;
}

/* ---------- 纯变换：provenance 构造（默认全字段空值） ---------- */
export function artifactsProvenance(parts, opts) {
    /* 调用方（检索/对话管线）在保存追踪时把当次运行事实注入；字段语义见 _normProvenance。
       检索日期缺省由时钟落戳（调用方已提供则保留——那是真实的检索时刻）。 */
    opts = opts || {};
    const merged = Object.assign({}, parts || {});
    if (!merged.retrieved_at) merged.retrieved_at = artifactsIsoNow(opts.now);
    return _normProvenance(merged);
}

/* ---------- 备份 JSON：导出（纯函数）/ 导入（解析纯函数 + 写库 adapter） ---------- */
export function artifactsExportText(projects, meta) {
    /* 追踪数组 → 备份 JSON 文本。纯函数：不读库、不写盘；schema 标记 + 版本 + 导出时间
       由导入端校验。projects 逐条过 normalize（防调用方传未规整对象）。 */
    meta = meta || {};
    const list = (Array.isArray(projects) ? projects : [])
        .map((p) => artifactsNormalizeProject(p)).filter(Boolean);
    return JSON.stringify({
        schema: ARTIFACTS_BACKUP_SCHEMA,
        schema_version: ARTIFACTS_SCHEMA,
        exported_at: artifactsIsoNow(meta.exported_at),
        exported_by: "biodata-agent",
        scope: artifactsScope(meta.scope),
        count: list.length,
        projects: list,
    }, null, 2);
}

export function artifactsParseBackup(text) {
    /* 备份 JSON 文本 → 校验结果（不写库；写库走 artifactsImportBackup）。
       不信任任何字段：schema 标记不符 / 版本超前 → 拒绝并说清原因，绝不猜测解析。
       版本落后（未来 P4/P5 bump）→ 在这里逐版本迁移后继续：`if (v < 2) { ... }`——迁移
       必须产生**完整**的新版本记录，不许半迁移后让老字段静默消失。 */
    let doc = null;
    try { doc = JSON.parse(String(text || "")); } catch (_e) {
        return { ok: false, error: "备份文件不是有效 JSON" };
    }
    if (!doc || typeof doc !== "object") return { ok: false, error: "备份文件结构为空" };
    if (String(doc.schema || "") !== ARTIFACTS_BACKUP_SCHEMA) {
        return { ok: false, error: "备份 schema 标记不符：" + String(doc.schema || "(无)") };
    }
    const v = Number(doc.schema_version);
    if (!Number.isFinite(v)) return { ok: false, error: "备份缺少 schema_version" };
    if (v > ARTIFACTS_SCHEMA) {
        return { ok: false, error: "备份来自更新版本（" + v + " > " + ARTIFACTS_SCHEMA + "），当前版本不能导入" };
    }
    if (!Array.isArray(doc.projects)) return { ok: false, error: "备份缺少 projects 数组" };
    const projects = doc.projects.map((p) => artifactsNormalizeProject(p)).filter(Boolean);
    const errors = [];
    projects.forEach((p) => {
        artifactsValidateProject(p).forEach((e) => errors.push(p.project_id + ": " + e));
    });
    if (errors.length) return { ok: false, error: "备份含不合法追踪：" + errors.join("；") };
    return {
        ok: true,
        projects: projects,
        meta: {
            schema_version: v,
            exported_at: String(doc.exported_at || ""),
            scope: artifactsScope(doc.scope),   // 仅元信息：导入时数据一律落**当前** profile 命名空间
        },
    };
}

/* ============================================================================
 * IndexedDB adapter 区（浏览器运行；node 规格经 artifactsSetIdbFactory 注入替身）
 * ========================================================================== */
export function artifactsSetIdbFactory(fn) {
    _dbFactory = (typeof fn === "function") ? fn : null;
}

function _idb() {
    /* 工厂是「返回实现」的函数：注入时每次调用取最新替身（测试可热换），
       未注入则回退全局 indexedDB。 */
    let f = _dbFactory ? _dbFactory() : null;
    if (!f && typeof globalThis !== "undefined" && globalThis.indexedDB) f = globalThis.indexedDB;
    if (!f) throw new Error("indexedDB 不可用：未注入工厂且宿主无全局实现");
    return f;
}

function _idbError(name, message) {
    const e = new Error(message);
    e.name = name;
    return e;
}

function _requestError(req, fallbackName) {
    const err = req && req.error;
    if (err && err.name) return _idbError(err.name, err.message || String(err.name));
    return _idbError(fallbackName || "UnknownError", "IndexedDB 请求失败");
}

export function artifactsOpen(opts) {
    /* 打开（幂等）/ 初始化库：upgrade 建 store 与 by_scope 索引。返回 Promise<IDBDatabase>。
       失败（如宿主禁用 IDB）reject 并如实报错——数据层不静默降级到 localStorage。 */
    if (_db) return Promise.resolve(_db);
    return new Promise((resolve, reject) => {
        let idb;
        try { idb = _idb(); } catch (e) { reject(e); return; }
        let req;
        try { req = idb.open(ARTIFACTS_DB_NAME, ARTIFACTS_DB_VERSION); } catch (e) { reject(e); return; }
        req.onupgradeneeded = () => {
            const db = req.result;
            if (!db.objectStoreNames.contains(ARTIFACTS_STORE)) {
                const os = db.createObjectStore(ARTIFACTS_STORE, { keyPath: "pk" });
                os.createIndex(ARTIFACTS_SCOPE_INDEX, "scope", { unique: false });
            }
        };
        req.onsuccess = () => { _db = req.result; resolve(_db); };
        req.onerror = () => reject(_requestError(req, "OpenError"));
    });
}

export function artifactsClose() {
    /* 关闭库连接（测试换替身/页面卸载用）。关闭后下一次 artifactsOpen 重开。 */
    if (_db) {
        try { _db.close(); } catch (_e) {}
        _db = null;
    }
}

/* 键寻址字段 pk / scope 只服务 IDB 键与索引（scope + project_id 复合主键编码），
   **不进追踪记录本体**：读出时剥掉，保证 get/list 返回的就是规整后的追踪记录
   （白名单里没有这两个字段）。 */
function _stripKeyFields(row) {
    if (!row || typeof row !== "object") return row;
    const out = Object.assign({}, row);
    delete out.pk;
    delete out.scope;
    return out;
}

/* 单请求 Promise 包装（只读）。 */
function _storeGet(db, scope, projectId) {
    return new Promise((resolve, reject) => {
        const tx = db.transaction(ARTIFACTS_STORE, "readonly");
        const req = tx.objectStore(ARTIFACTS_STORE).get(artifactsKey(scope, projectId));
        req.onsuccess = () => resolve(req.result ? _stripKeyFields(req.result) : null);
        req.onerror = () => reject(_requestError(req, "ReadError"));
    });
}

function _storeGetAllByScope(db, scope) {
    return new Promise((resolve, reject) => {
        const tx = db.transaction(ARTIFACTS_STORE, "readonly");
        const req = tx.objectStore(ARTIFACTS_STORE).index(ARTIFACTS_SCOPE_INDEX).getAll(artifactsScope(scope));
        req.onsuccess = () => resolve(Array.isArray(req.result) ? req.result.map(_stripKeyFields) : []);
        req.onerror = () => reject(_requestError(req, "ReadError"));
    });
}

function _storeDelete(db, scope, projectId) {
    return new Promise((resolve, reject) => {
        const tx = db.transaction(ARTIFACTS_STORE, "readwrite");
        const req = tx.objectStore(ARTIFACTS_STORE).delete(artifactsKey(scope, projectId));
        req.onsuccess = () => resolve();
        req.onerror = () => reject(_requestError(req, "WriteError"));
    });
}

/* 写包装（put）：**QuotaExceededError 捕获点**——请求 error 带原名 reject（如
   QuotaExceededError），交易级 onabort 兜底（请求已成功但交易被后续请求中断也算写失败）。
   写入如实报错，绝不静默吞掉「写不进去」这件事。 */
function _storePut(db, scope, project) {
    return new Promise((resolve, reject) => {
        const tx = db.transaction(ARTIFACTS_STORE, "readwrite");
        // pk = 复合主键编码（scope + project_id），keyPath 寻址用；scope = 索引物化列；
        // 两者读出时由 _stripKeyFields 剥掉
        const req = tx.objectStore(ARTIFACTS_STORE).put(Object.assign({}, project, {
            pk: artifactsKey(scope, project.project_id),
            scope: artifactsScope(scope),
        }));
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(_requestError(req, "WriteError"));
        tx.onabort = () => {
            if (!req.error) reject(_idbError("AbortError", "写入事务被中止"));
        };
    });
}

/* ---------- 内存镜像（写穿；get 命中；list 以 DB 为真源并回灌） ---------- */
function _cacheEnsure(scope) {
    if (_cacheScope !== scope) { _cacheScope = scope; _cache = new Map(); }
    return _cache;
}
function _cachePut(scope, project) { _cacheEnsure(scope).set(project.project_id, project); }
function _cacheDelete(scope, projectId) { const c = _cacheEnsure(scope); if (c.has(projectId)) c.delete(projectId); }

function _stampForWrite(project, opts) {
    const p = artifactsNormalizeProject(project, opts);
    p.updated_at = artifactsIsoNow(opts.now);   // 每次写 = 一次「更新于」刷新（列表按此倒序）
    return p;
}

/* ---------- 追踪 CRUD ---------- */
export async function artifactsCreateProject(scope, input, opts) {
    /* 建追踪。project_id 重复 → ConstraintError（如实报「已存在」，不许静默覆盖用户内容）。 */
    opts = opts || {};
    const db = await artifactsOpen(opts);
    scope = artifactsScope(scope);
    const project = _stampForWrite(input, opts);
    if (!project.project_id) throw _idbError("InvalidStateError", "追踪缺少 project_id");
    const errors = artifactsValidateProject(project);
    if (errors.length) throw _idbError("InvalidStateError", "追踪数据不合法：" + errors.join("；"));
    const existing = await _storeGet(db, scope, project.project_id);
    if (existing) throw _idbError("ConstraintError", "追踪已存在：" + project.project_id);
    await _storePut(db, scope, project);
    _cachePut(scope, project);
    return project;
}

export async function artifactsGetProject(scope, projectId, opts) {
    opts = opts || {};
    const db = await artifactsOpen(opts);
    scope = artifactsScope(scope);
    const id = String(projectId || "");
    /* 会话镜像命中（同一会话内重复读不重复打 IDB）。注意：返回的是镜像**引用**——
       改动必须走写路径（artifactsUpdateProject 及纯变换函数），不要原地改返回对象
       （镜像被改不会坏库里数据——IDB put 是克隆——但会让后续命中看到半成品）。 */
    const c = _cacheEnsure(scope);
    if (id && c.has(id)) return c.get(id);
    const p = await _storeGet(db, scope, id);
    if (p) { _cachePut(scope, p); return p; }
    return null;
}

export async function artifactsListProjects(scope, opts) {
    /* 当前 profile 全部追踪，updated_at 倒序（最新在前；同刻按 project_id 升序，确定性）。 */
    opts = opts || {};
    const db = await artifactsOpen(opts);
    scope = artifactsScope(scope);
    const rows = await _storeGetAllByScope(db, scope);
    rows.sort((a, b) => {
        const d = String(b.updated_at || "").localeCompare(String(a.updated_at || ""));
        return d !== 0 ? d : String(a.project_id || "").localeCompare(String(b.project_id || ""));
    });
    // list 是缓存刷新点：整桶重建（清掉跨标签页删除/改名后的旧镜像）
    const c = _cacheEnsure(scope);
    c.clear();
    rows.forEach((p) => c.set(p.project_id, p));
    return rows;
}

export async function artifactsUpdateProject(scope, projectId, mutator, opts) {
    /* 读-改-写。mutator(project) → 新追踪；project_id 不可变（改 id = 删旧建新，另走接口）；
       mutator 返回空 → 拒绝写 null（防误删）。 */
    opts = opts || {};
    const db = await artifactsOpen(opts);
    scope = artifactsScope(scope);
    const id = String(projectId || "");
    const existing = await _storeGet(db, scope, id);
    if (!existing) throw _idbError("NotFoundError", "追踪不存在：" + projectId);
    const next = (typeof mutator === "function") ? mutator(existing) : existing;
    if (!next) throw _idbError("InvalidStateError", "mutator 返回空——拒绝把追踪写成 null");
    if (String(next.project_id || "") !== id) {
        throw _idbError("InvalidStateError", "project_id 不可变（改 id 请走删除+重建）");
    }
    const project = _stampForWrite(next, opts);
    const errors = artifactsValidateProject(project);
    if (errors.length) throw _idbError("InvalidStateError", "追踪数据不合法：" + errors.join("；"));
    await _storePut(db, scope, project);
    _cachePut(scope, project);
    return project;
}

export async function artifactsSaveProject(scope, project, opts) {
    /* 低层直写（导入备份 / 已规整数据用）：upsert 语义、不做存在性检查，写前仍校验形状。 */
    opts = opts || {};
    const db = await artifactsOpen(opts);
    scope = artifactsScope(scope);
    const p = _stampForWrite(project, opts);
    if (!p.project_id) throw _idbError("InvalidStateError", "追踪缺少 project_id");
    const errors = artifactsValidateProject(p);
    if (errors.length) throw _idbError("InvalidStateError", "追踪数据不合法：" + errors.join("；"));
    await _storePut(db, scope, p);
    _cachePut(scope, p);
    return p;
}

export async function artifactsDeleteProject(scope, projectId, opts) {
    opts = opts || {};
    const db = await artifactsOpen(opts);
    scope = artifactsScope(scope);
    const id = String(projectId || "");
    const existing = await _storeGet(db, scope, id);
    if (!existing) return false;
    await _storeDelete(db, scope, id);
    _cacheDelete(scope, id);
    return true;
}

/* ---------- 备份导出 / 导入 ---------- */
export async function artifactsExportAll(scope, opts) {
    /* 当前 profile 全量导出：{text, projects}（text 即 artifactsExportText 产物）。 */
    opts = opts || {};
    const projects = await artifactsListProjects(scope, opts);
    return {
        text: artifactsExportText(projects, { scope: artifactsScope(scope), exported_at: artifactsNow() }),
        projects: projects,
    };
}

export async function artifactsImportBackup(scope, text, opts) {
    /* 备份导入（upsert 合并，按 project_id 覆盖同 id 记录）。**逐追踪独立成败并如实上报**：
       单条失败（如配额）不中止其余——返回 {ok, imported, failed[]}，UI 层据此「含失败与
       部分失败如实呈现」。备份里的 scope 仅元信息，数据一律落当前 profile。 */
    const parsed = artifactsParseBackup(text);
    if (!parsed.ok) throw _idbError("InvalidStateError", parsed.error);
    opts = opts || {};
    const db = await artifactsOpen(opts);
    scope = artifactsScope(scope);
    const results = [];
    for (const p of parsed.projects) {
        const stamped = _stampForWrite(p, opts);
        try {
            await _storePut(db, scope, stamped);
            _cachePut(scope, stamped);
            results.push({ project_id: stamped.project_id, status: "ok" });
        } catch (e) {
            results.push({ project_id: stamped.project_id, status: "error",
                error: e.name + (e.message ? ": " + e.message : "") });
        }
    }
    return {
        ok: true,
        imported: results.filter((r) => r.status === "ok").length,
        failed: results.filter((r) => r.status === "error"),
        meta: parsed.meta,
    };
}

/* ---------- 存储预警（仅预警，不淘汰） ---------- */
export async function artifactsStorageEstimate() {
    /* navigator.storage.estimate() 仅作预警。不可用/失败 → null（不抛、不打断主流程）。 */
    try {
        if (typeof navigator !== "undefined" && navigator.storage && typeof navigator.storage.estimate === "function") {
            const e = await navigator.storage.estimate();
            return (e && typeof e === "object") ? { usage: Number(e.usage) || 0, quota: Number(e.quota) || 0 } : null;
        }
    } catch (_e) {}
    return null;
}

/* ---------- profile 生命周期钩子（accounts.js 日后接线点） ---------- */
export function artifactsOnProfileSwitched() {
    /* 登录/登出/切换账户时调用：清空内存缓存与活动追踪句柄。
       追踪数据本体按 scope 隔离在 IndexedDB 里——**只断引用、不删数据**（的
       「切换 profile 时清空活动追踪、上下文卡与内存缓存」；上下文卡的关闭是 UI 层职责，
       由各自 UI 态处理）。 */
    _cacheScope = null;
    _cache = null;
    _activeProjectId = null;
}

export function artifactsActiveProjectId() { return _activeProjectId; }
export function artifactsSetActiveProjectId(id) { _activeProjectId = id ? String(id) : null; }
