"use strict";

/* 结果覆盖策略 · 纯逻辑核（设计 §10.3）
 *
 * ## 本文件是什么 / 不是什么
 *
 * - 是：`/api/utterance` 两阶段（preliminary 初屏 → final 落屏）下**统一选择**哪一版结果上屏的
 *   纯逻辑。后端批次带 `scope_fingerprint`（规范化检索范围指纹）与 `search_trace`；前端据
 *   「排序层级别」决定是否自动换屏、据「scope + 有序记录键向量」判同去重。设计见
 *   内部设计文档 §10.3。
 * - 不是：DOM、fetch、墙钟、状态。一切决定做成纯函数，node 规格可直跑（零 `#` import、
 *   零相对 import——self-contained，不进 import 环）。
 *
 * ## 规则（设计 §10.3 + 条件变更修正，用户既定覆盖规则）
 *
 * - `rankingLevel(trace)`：规则=1，+local_semantic=2，+llm_rerank=3（polish 不计）；
 *   **未知/缺失 trace = null = 不可比较**，不得默认 1 后自动覆盖（同 scope 择优时）。
 * - **条件变更批**（双侧都有 scope_fingerprint 且**不同** = 真换了检索词）：
 *   `mode="display"` 整屏覆盖，**包括 0 命中批**（空结果集如实上屏；空集无记录键是事实，
 *   不是「不稳定」）；不比较排序层级别（用户既定规则：条件变更重检索成功——含 0 条——
 *   则上一次结果直接抛弃、新结果上屏）。
 * - **同 scope 的重检批**：候选自动换屏 ⇔ rankingLevel **严格大于**当前批（且双侧 trace
 *   都已知、双侧记录键稳定）。
 * - scope_fingerprint 相同**且**级别不更高且同批（记录键向量相等）→ 去重：不新增 pill、不换屏。
 * - 同 scope 重检但结果不同/较弱（或不可比/缺稳定键）→ 保守 `mode="alternate"`：保持当前屏，
 *   回执如实（去黑话；不再提「上方切换」，因为结果头部切换器已整体退役）。
 * - 有序记录键向量 + scope 联合判同；任一侧缺稳定键 → 保守不去重、不自动覆盖（同 scope 内）。
 *
 * ## 输出契约（selectDisplayBatch）
 *
 * 返回一个**决定**，调用方（search/tool 两档）据此落地，不再各自猜：
 *
 * ```js
 * {
 *   mode: "display" | "dedupe" | "alternate",
 *   view: <完整结果视图对象（payload + result_batches + active_batch），或 null>,
 *   query: <该视图的生效检索句>,
 *   sysText: <如实系统回执，空串=不额外发>,
 *   stripPrelimBadge: <bool>,
 *   mergedBatches: <供切换器渲染的批组>,
 *   activeBatchId: <应在 mergedBatches 里高亮的批 id>,
 * }
 * ```
 *
 * - `display`：`view` 非空 → 调用方经 `runRecommend(prefetched=view)` 落地（新检索/严格升级）。
 * - `dedupe`：`view` 为 null → 保持当前屏（不重渲、不新增 pill、摘徽标），系统回执如实。
 * - `alternate`：`view` 为 null → 保持当前屏，但调用方用 `mergedBatches + activeBatchId`
 *   做一次轻量 batchBar 刷新（`renderBatchSwitcher`），把换词批作为非活动备选 pill。
 */

/* ---------- 排序层级别（设计 §10.3） ---------- */

/* 轨迹 → 排序层级别。trace 缺/非数组/无步骤 → null（不可比较）；polish 不计。
   local_semantic / llm_rerank 只要 status=used 即计入（fallback/skipped 不计）。 */
export function rankingLevel(trace) {
    if (!trace || !Array.isArray(trace.steps) || trace.steps.length === 0) return null;
    const used = new Set();
    for (const s of trace.steps) {
        if (s && s.status === "used" && s.id) used.add(s.id);
    }
    let level = 1;
    if (used.has("local_semantic")) level = 2;
    if (used.has("llm_rerank")) level = 3;
    return level;
}

/* 从「批对象」或「裸视图（payload）」里取 search_trace：
   批对象带 .payload（payload.search_trace）；裸视图直接 .search_trace。 */
export function traceOf(x) {
    const p = (x && x.payload && typeof x.payload === "object") ? x.payload : x;
    return (p && p.search_trace) || null;
}

/* ---------- 有序记录键向量 + scope 判同（设计 §10.3） ---------- */

/* 记录键向量：payload.results 的有序 dataset_uid 列表（保序、不去重）。
   stable = 长度>0 且全部非空（任一空 uid → 不折叠、不稳定，判同走保守）。 */
export function recordKeyVector(x) {
    const p = (x && x.payload && typeof x.payload === "object") ? x.payload : x;
    const results = (p && Array.isArray(p.results)) ? p.results : [];
    const keys = results.map((r) => String((r && r.dataset_uid) || "").trim());
    const stable = keys.length > 0 && keys.every((k) => !!k);
    return { keys, stable };
}

/* 双侧记录键向量逐位相等（保序），且双侧都稳定。任一侧不稳 → false（保守不去重）。 */
export function sameRecordVector(a, b) {
    const va = recordKeyVector(a);
    const vb = recordKeyVector(b);
    if (!va.stable || !vb.stable) return false;
    if (va.keys.length !== vb.keys.length) return false;
    for (let i = 0; i < va.keys.length; i += 1) {
        if (va.keys[i] !== vb.keys[i]) return false;
    }
    return true;
}

/* scope 指纹（契约级身份键）：后端在组卷时按规范化 query+sources+facet/suppressed/lenient+date
   计算。批对象带 `.scope_fingerprint`；裸视图/缺失 → null（判同走保守）。 */
export function scopeFingerprintOf(x) {
    if (x && typeof x.scope_fingerprint === "string" && x.scope_fingerprint) return x.scope_fingerprint;
    return null;
}

/* scope 相同 ⇔ 双侧都有非空指纹且相等。 */
export function sameScope(a, b) {
    const fa = scopeFingerprintOf(a);
    const fb = scopeFingerprintOf(b);
    return Boolean(fa && fb && fa === fb);
}

/* 同批 ⇔ scope 相同 **且** 有序记录键向量逐位相等（双侧稳定）。任一侧缺稳定键 → 保守不同批。 */
export function sameBatch(a, b) {
    return sameScope(a, b) && sameRecordVector(a, b);
}

/* 批内同 scope 时保留更高排序层的那个（稳定比较；trace 未知的保守不替换）。 */
function _betterBatch(a, b) {
    const ra = rankingLevel(traceOf(a));
    const rb = rankingLevel(traceOf(b));
    if (ra == null && rb == null) return a;
    if (ra == null) return b;
    if (rb == null) return a;
    return ra >= rb ? a : b;
}

/* 归并批组：existing（当前视图已在的批）+ incoming（本轮新批），同批（scope+记录键向量）只留更高层。
   任一侧批形状不全（无 payload）跳过（与回退同纪律）。 */
export function mergeBatches(existing, incoming) {
    const out = [];
    for (const b of (existing || [])) {
        if (b && b.payload && typeof b.payload === "object") out.push(b);
    }
    for (const c of (incoming || [])) {
        if (!c || !c.payload || typeof c.payload !== "object") continue;
        let hitIdx = -1;
        for (let i = 0; i < out.length; i += 1) {
            if (sameBatch(out[i], c)) { hitIdx = i; break; }
        }
        if (hitIdx >= 0) {
            out[hitIdx] = _betterBatch(out[hitIdx], c);
        } else {
            out.push(c);
        }
    }
    return out;
}

/* ---------- 批 id 归一（缺 batch_id 的批补稳定合成 id）----------
   renderBatchSwitcher / switchBatch 都用 `b.batch_id || ("b"+(idx+1))` 作键。selectDisplayBatch
   之前用**裸** `ref.batch_id` 回填 activeBatchId——参考批缺 batch_id（preliminary 批常不带）时
   activeBatchId 塌成 ""，下游渲染高亮错位（两枚 pill 都标「规则排序」、无一枚 is-on）、
   switchBatch 的空比对把缺 id 的 pill 变成不可点。这里统一用归一 id，保证 activeBatchId
   永远非空且指向「真正在屏的那批」。 */
function _normId(batch, idx) {
    const raw = String((batch && batch.batch_id) || "").trim();
    return raw || ("b" + (idx + 1));
}
/* 求「当前在屏批」在 merged 批组里的归一 id。优先按引用找（mergeBatches 保留原对象引用，
   参考批常就是 merged 里的同一对象）；引用不在（裸当前屏/跨轮回看）按 batch_id 匹配；
   仍找不到 → 取 merged 末批（与渲染缺省口径一致：active_batch 缺省 = 最后一批）；空组 → "". */
function _activeBatchIdFor(ref, merged) {
    const arr = merged || [];
    if (ref) {
        for (let i = 0; i < arr.length; i += 1) {
            if (arr[i] === ref) return _normId(arr[i], i);
        }
        const rawRef = String((ref && ref.batch_id) || "").trim();
        if (rawRef) {
            let hit = -1;
            for (let i = 0; i < arr.length; i += 1) {
                if (String(arr[i].batch_id || "") === rawRef) { hit = i; break; }
            }
            if (hit >= 0) return _normId(arr[hit], hit);
            return rawRef;   // 引用与批组都对不上，但参考批自带 id → 直接用它（dedupe 裸当前屏时 merged 为空）
        }
    }
    if (arr.length) return _normId(arr[arr.length - 1], arr.length - 1);
    return "";
}

/* ---------- 统一选择函数（设计 §10.3） ---------- */

/* 候选批 = 本轮最终要落的那一批（reply.active_batch，缺省取最后一批）——初步批只作参考，
   不作为「要展示的新结果」。 */
function _activeBatch(reply) {
    const batches = (reply && Array.isArray(reply.result_batches)) ? reply.result_batches : [];
    if (!batches.length) return null;
    const activeId = String((reply && reply.active_batch) || "");
    if (activeId) {
        const found = batches.find((b) => b && String(b.batch_id || "") === activeId);
        if (found) return found;
    }
    return batches[batches.length - 1];
}

/* 参考批 = 当前屏上的批。优先本轮 preliminary（屏上正是初屏）；否则当前视图的活跃批。 */
function _referenceBatch(reply, currentView) {
    const batches = (reply && Array.isArray(reply.result_batches)) ? reply.result_batches : [];
    if (reply && reply._prelimShown) {
        const prelim = batches.find((b) => b && String(b.kind || "") === "preliminary");
        if (prelim) return prelim;
    }
    if (currentView && Array.isArray(currentView.result_batches)) {
        const activeId = String((currentView && currentView.active_batch) || "");
        if (activeId) {
            const found = currentView.result_batches.find((b) => b && String(b.batch_id || "") === activeId);
            if (found) return found;
        }
        if (currentView.result_batches.length) return currentView.result_batches[currentView.result_batches.length - 1];
    }
    /* 裸视图（无 result_batches，如纯 /api/recommend 落地）：当作参考，但 scope 指纹缺失 → 判同走保守。 */
    if (currentView && currentView.ok) return currentView;
    return null;
}

/* 回执短语（设计 §10.3，**如实**）——「已更新为更匹配结果」只许在真正升级时（display）由调用方说，
   dedupe / alternate 一律换成本页给的如实句。 */
export const DEDUPE_SYS_TEXT = "这次没有得出更优结果，当前结果保持不变。";
/* 换词/未知 trace 的弱批不再作「备选 pill」——按「只展示一份最终结果 + supersede 即丢弃」，
   较弱/被覆盖的批直接丢弃，不展示也不存储；回执改成如实的不变句（不再提「上方切换」，因为没有切换器）。
   条件变更（scope 不同）已改走 display 整屏覆盖，本档只留给「**同 scope** 的重检较弱批」——
   文案去黑话（去掉误导性的「按新条件」，说明实际是同一条件下这次重检没更好，保住当前结果）。 */
export const ALTERNATE_SYS_TEXT = "这次重检没有得出更优结果，当前结果保持不变。";

/* 主入口：给定本轮回复（reply，含 result_batches/active_batch/result_payload/_prelimShown/
   preliminary_final）与当前屏视图（currentView=LAST_RECOMMEND_DATA 或 null），输出落屏决定。
   纯函数：不读 DOM、不调网络、不写状态；node 规格直接 import 跑。 */
export function selectDisplayBatch(reply, currentView) {
    const batches = (reply && Array.isArray(reply.result_batches)) ? reply.result_batches : [];
    const candidate = _activeBatch(reply);
    const ref = _referenceBatch(reply, currentView);

    /* 无批组的 legacy 响应（旧后端/非 agent 路径）：直接落地 result_payload，行为与现状逐位一致。 */
    const legacyPayload = (reply && reply.result_payload && reply.result_payload.ok === true) ? reply.result_payload : null;
    if (batches.length === 0) {
        if (legacyPayload) {
            return {
                mode: "display", view: legacyPayload, query: "",
                sysText: "", stripPrelimBadge: true, mergedBatches: [], activeBatchId: "",
            };
        }
        return { mode: "dedupe", view: null, query: "", sysText: "", stripPrelimBadge: false, mergedBatches: [], activeBatchId: "" };
    }

    const candRank = rankingLevel(traceOf(candidate));
    const refRank = ref ? rankingLevel(traceOf(ref)) : null;
    const comparable = candRank != null && refRank != null;

    /* 首次落屏（无参考批）：直接展示候选批。 */
    if (!ref) {
        const merged = mergeBatches([], batches);
        const activeId = _activeBatchIdFor(candidate, merged);
        const view = Object.assign({}, candidate.payload, { result_batches: merged, active_batch: activeId });
        return {
            mode: "display", view, query: String((candidate && candidate.query_effective) || "") || String(reply.query || ""),
            sysText: "", stripPrelimBadge: true, mergedBatches: merged, activeBatchId: activeId,
        };
    }

    /* 判同（scope + 有序记录键向量，任何一侧缺稳定键 → 保守不同批）。 */
    const same = sameBatch(candidate, ref);
    /* 条件变更批（双侧都有 scope_fingerprint 且**不同** = 真换了检索词）：整屏覆盖，**包括 0 命中批**——
       空结果集没有记录键是事实、不是「不稳定」，recordKeyVector.stable=false 在此不得拦截。
       排序层级别比较只保留给「同 scope 的重检批」的择优用途，条件变更不比较级别（用户既定规则：
       条件变更重检索成功——含命中 0 条——则上一次结果直接抛弃、新结果（空集）上屏）。
       任一侧缺指纹（如 legacy 裸视图）→ 无法确证「换了条件」，走保守（不在此分支）。 */
    const _candScope = scopeFingerprintOf(candidate);
    const _refScope = ref ? scopeFingerprintOf(ref) : null;
    if (_candScope && _refScope && _candScope !== _refScope) {
        /* 跨轮泄漏修复：display 整屏覆盖后，本轮回执的 pill 严格 = 本轮 reply.result_batches
           （经下游 _discardSuperseded 存活的批）——**不得**再并入 currentView（LAST_RECOMMEND_DATA）
           的批次（那是上一轮的屏态，混入会让本轮回执带出上一轮 pill）。currentView 只用于求 ref
           （判 scope/级别），不用于扩充 pill 组。 */
        const merged = mergeBatches([], batches);
        const activeId = _activeBatchIdFor(candidate, merged);
        const view = Object.assign({}, candidate.payload, { result_batches: merged, active_batch: activeId });
        return {
            mode: "display", view, query: String((candidate && candidate.query_effective) || ""),
            sysText: "", stripPrelimBadge: true, mergedBatches: merged, activeBatchId: activeId,
        };
    }

    /* 同 scope：覆盖稳定性——自动换屏还要求双侧记录键都稳定（缺稳定键 → 保守不自动覆盖）。
       （不可比较/不稳定时，宁保当前结果，不给「已更新为更匹配」的假话。） */
    const stableEnough = recordKeyVector(candidate).stable
        && (ref ? recordKeyVector(ref).stable : true);
    const upgradeComparable = comparable && candRank > refRank;

    /* 严格更高级且双侧记录键稳定 → 自动换屏（真正的「更匹配」才配说已更新）。
       同上——display 只并入本轮 reply.result_batches，不并入 currentView（防跨轮泄漏）。 */
    if (upgradeComparable && stableEnough) {
        const merged = mergeBatches([], batches);
        const activeId = _activeBatchIdFor(candidate, merged);
        const view = Object.assign({}, candidate.payload, { result_batches: merged, active_batch: activeId });
        return {
            mode: "display", view, query: String((candidate && candidate.query_effective) || ""),
            sysText: "", stripPrelimBadge: true, mergedBatches: merged, activeBatchId: activeId,
        };
    }

    /* 同批（scope+记录键向量稳定相等）→ 去重（不新增 pill、不换屏）。 */
    if (same) {
        return {
            mode: "dedupe", view: null, query: "",
            sysText: DEDUPE_SYS_TEXT, stripPrelimBadge: true,
            mergedBatches: (currentView && currentView.result_batches) || [],
            activeBatchId: _activeBatchIdFor(ref, (currentView && currentView.result_batches) || []),
        };
    }
    /* 同 scope 重检但结果不同/较弱（或不可比/缺稳定键）→ 保守：保持当前屏，如实回执（去黑话）。 */
    const merged = mergeBatches((currentView && currentView.result_batches) || [], batches);
    const activeId = _activeBatchIdFor(ref, merged);
    return {
        mode: "alternate", view: null, query: "",
        sysText: ALTERNATE_SYS_TEXT, stripPrelimBadge: true, mergedBatches: merged, activeBatchId: activeId,
    };
}

/* ============================================================================
 * 零命中放松收编为单一通道（2026-09-14）——纯逻辑核（board.js 渲染选择卡用；
 * 本文件只放纯函数，node 可测）。「放松哪个条件」的用户触点统一为 ask.relax 选择卡：
 * 渲染唯一（board.js askRelaxShow）、执行唯一（cbChoose askrelax → cbCommit suppressed）、
 * 数据源唯一（retriever.relaxation_options，include_dead=False）。触发点两个：环内 LLM
 * （agent 路径，degenerate 机械闸）+ 本函数的零命中自动弹卡兜底（非 agent 路径也有救回）。
 * 旧「救回选择条 / 空态卡 chips / 换词兜底」三套平行实现已全部退役。
 * ========================================================================== */

/* 批是否零命中：payload.results 是数组且 length 0。 */
export function isZeroHitBatch(b) {
    if (!b) return false;
    const p = (b.payload && typeof b.payload === "object") ? b.payload : {};
    return Array.isArray(p.results) && p.results.length === 0;
}

/* 从零命中批载荷派生 ask.relax 选择卡 payload（与环内 ask.relax 渲同一张卡）。
   返回 { query, reason:"", options:[{key,kind,label,count,suppress}] }；
   批非零命中 / relaxation_options 为空（死维不给选）→ null（调用方不弹卡）。
   suppress 由后端按「整组替换」语义预先合并好（请求级已忽略+本项放松量），前端原样下发、
   零合并逻辑。选项不带 preview（2026-09-14 用户评审：示例名塞按钮信息效率太低；
    relaxation_options[].results 预览仍是 HTTP 契约，本卡不再消费）。 */
export function deriveRelaxCard(batch) {
    if (!isZeroHitBatch(batch)) return null;   // 非零命中批不弹卡（防御：调用方已查 pill 零命中）
    const p = (batch.payload && typeof batch.payload === "object") ? batch.payload : {};
    const raw = Array.isArray(p.relaxation_options) ? p.relaxation_options : [];
    const options = [];
    raw.forEach(function (o) {
        if (!o || typeof o !== "object") return;
        const label = String(o.label || "").trim();
        const suppress = (Array.isArray(o.suppress) ? o.suppress : [])
            .map(function (s) { return String(s || "").trim(); }).filter(Boolean);
        if (!label || !suppress.length) return;   // 没有可执行放松量的项不给选（防死卡）
        options.push({
            key: String(o.key || ""),
            kind: (o.kind === "only") ? "only" : "drop",
            label: label,
            count: (typeof o.count === "number") ? o.count : 0,
            suppress: suppress,
        });
    });
    if (!options.length) return null;
    return {
        query: String(batch.query_effective || batch.label || batch.query_raw || ""),
        reason: "",
        options: options,
    };
}

/* 最新结果判定：最后一个带 pill 的回执 entry 的活跃批 id。
   「该批是最后一个回执 entry 的活跃批」＝该零命中结果是最新结果（此时零命中 pill 才显示「点击处理」）。 */
export function latestActiveBatchId(entries) {
    if (!Array.isArray(entries)) return "";
    for (let i = entries.length - 1; i >= 0; i--) {
        const e = entries[i];
        if (!e || !Array.isArray(e.pills) || !e.pills.length) continue;
        const act = e.pills.find(function (p) { return !!p.active; });
        if (act) return String(act.batchId || "");
    }
    return "";
}
