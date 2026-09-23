# -*- coding: utf-8 -*-
"""执行侧 agent 的 pydantic 契约层（2026-08-06 硬约束收到 schema 层）。

两个方向各一刀（设计动机：此前 agent_exec 是全仓唯一没过 pydantic 的工具面）：

1. **返回契约**（`agent_exec.LOOP_TOOLS` 的 run() 出口）：`DbStatusResult` /
   `CheckUpdatesResult` / `SearchOnlineResult` 三个模型从 `corpus_status.db_status` /
   `corpus_curation.check_updates` / `plan_search_online + apply_search_online` 的
   **真实返回形状**提炼（字段依据见各模型注释）；execute 节点在 run() 出口
   `model_validate` 一遍——返回形状残缺/类型不对 = ValidationError = 与工具抛异常
   同路（step.ok=False 如实记，不炸图），ok 语义升级为「没抛异常**且形状合法**」。
   `Step` 模型约束 step 实录的字段齐整（实录仍落纯 dict，契约不变）。
   各模型一律 `extra="allow"`——unknown 额外字段放行，不误杀 additive 演进。
2. **入参契约**（LLM-facing 工具表）：`verb_parameters_schema(spec)` 由
   `action_plan.VERB_SPECS` 逐动词 `create_model` + `model_json_schema()` 生成
   OpenAI function parameters，取代手写拼 JSON schema dict。schema 是**提示层**，
   必填语义仍归 `build_plan_from_raw`/`_finalize` 机械护栏**裁决**（两层不合并）。

**依赖纪律**：pydantic 是 fastapi（webapp 硬依赖）的**传递必装依赖**——与 langchain 的
「可选扩展」地位不同，模块级 import 安全；agent_exec「未装扩展零代价 import」的纪律
只针对 langchain 系，不受本模块影响。本模块自身不 import langchain 系（纯 pydantic + 词表真源）。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from . import action_plan as _ap
from ..retrieval import search_request as _sr

__all__ = [
    "Confidence",
    "SourceName",
    "DbStatusResult",
    "CheckUpdatesResult",
    "SearchOnlineResult",
    "SyncUpdatesResult",
    "Step",
    "LOOP_RESULT_MODELS",
    "verb_parameters_schema",
]


# ==============================================================================================
# 入参契约（第二刀）：LLM-facing 工具表的 pydantic 生成
# ==============================================================================================

#: confidence 的合法取值——与旧手写 schema 的枚举**逐位一致**（build_plan_from_raw 对
#: 枚举外取值一律降 low，提示层收窄不改变裁决层行为）。
Confidence = Literal["high", "low"]

#: 受控来源规范名集合：从检索侧 `search_request.SOURCE_ALIASES` 的规范名**程序取**
#: （同一份词表真源，不硬抄字符串——词表扩了枚举自动跟随）。
_SOURCE_NAMES: tuple[str, ...] = tuple(source for source, _aliases in _sr.SOURCE_ALIASES)
#: source 槽的真枚举。`Literal.__getitem__(tuple)` == Literal[逐元素]——运行期展开，
#: 不手写 9 个字面量（手写 = 与词表第二份拷贝，必漂移）。
SourceName = Literal.__getitem__(_SOURCE_NAMES)


def _registry_source_labels() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(联网搜可用源 labels, 检查/同步覆盖源 labels)——**懒取** corpus_curation 两个真实
    注册表（旧实现所有 verb 共用检索词表枚举，ENCODE/EBI SCEA
    在枚举里 search_online 却接不了、Zenodo 接得了却不在枚举里——LLM 被允许提议死路、
    被禁止点名活路）。懒 import：corpus_curation 体量大，且本模块只在本函数被调时
    才真正需要它。"""
    from ..corpus import corpus_curation as _cc
    search_labels = tuple(str(s["label"]) for s in _cc.SOURCE_ADAPTERS.values())
    check_labels = tuple(str(s["label"]) for s in _cc.CHECK_UPDATE_SOURCES.values())
    return search_labels, check_labels


def source_names_for(verb: str) -> tuple[str, ...]:
    """source 槽按 verb 取真枚举：联网搜给能搜的、检查/同步给能查的
    其余 verb（当前没有带 source 槽的）回退检索词表全集。"""
    search_labels, check_labels = _registry_source_labels()
    if verb == "curate.search_online":
        return search_labels
    if verb in ("curate.check_updates", "curate.sync_updates"):
        return check_labels
    return _SOURCE_NAMES


def source_candidates_zh(verb: str) -> str:
    """该 verb 的 source 候选清单人读串（decide 规则表与 schema 枚举**同一真源**，防漂移）。"""
    return " / ".join(source_names_for(verb))


#: source 槽描述按 verb 分派（评审裁决的提示层半）：候选清单不抄进文字（名单真源是
#: 本字段的 enum，两处同出 `source_names_for`，文字再抄一份必漂移）；「接不了如实说」的
#: 口径按动作写清。多点名只填第一个的纪律不变。
_SOURCE_SLOT_DESCRIPTIONS_ZH: dict[str, str] = {
    "curate.search_online": (
        "数据来源，受控规范名（联网搜只覆盖本字段候选里列出的源；候选外的来源系统会如实"
        "回答接不了）。用户原话点名了来源时**必填**（填规范名）；**点名多个来源时只填最先"
        "点名的那个**（剩下的由后续步骤逐一处理），不许把多个来源挤进一个槽；没点名就不填。"
    ),
    "curate.check_updates": (
        "数据来源，受控规范名（候选见本字段清单；其中只有部分来源能在线比对，离线快照源会"
        "如实报告本地快照信息）。用户原话点名了来源时**必填**（填规范名）；**点名多个来源时"
        "只填最先点名的那个**（剩下的由后续步骤逐一处理），不许把多个来源挤进一个槽；"
        "没点名就不填（不填=查全部）。"
    ),
    "curate.sync_updates": (
        "数据来源，受控规范名（候选见本字段清单；只有能在线比对且有入库适配器的来源才会"
        "真的自动入库，其余来源如实写明做不到）。用户原话点名了来源时**必填**（填规范名）；"
        "**点名多个来源时只填最先点名的那个**（剩下的由后续步骤逐一处理），不许把多个来源"
        "挤进一个槽；没点名就不填（不填=查全部）。"
    ),
}

#: 槽位描述必须**专职**（2026-08-03 问题：「检查10x更新」被填成 source=ArrayExpress）。
#: 此前所有非 limit 槽共用一句泛泛模板「按原话里的说法填」——LLM 没有受控清单可依，
#: 随手填了它唯一眼熟的在线源。每个槽写清「填什么、何时必填、何时不填」；
#: 即便如此 LLM 仍可能填错，所以 validate 还有一道不依赖 LLM 自觉的机械校验兜底（见
#: `agent_exec._named_source_violation`）——两层防线各管一半。
#: 2026-08-08 B1a 调研六候选批：补具体值示例与出处锚（k09 多来源只检一个、j03/l07 条件成立后
#: 搜索步被放弃两处问题的描述层锚；与 decide 铁律 rule 4/9 互文——提示词规则负责解释
#: 工具描述当 checklist）。
#: （本字典原在 agent_exec，2026-08-06 随 schema 生成迁入本模块——它是入参契约的一部分。）
#: 互指：keywords 出处判定句的另一语境变体在
#: agent_exec LOOP_TOOLS["curate.search_online"].decide_zh（decide 工具描述）；
#: loop_action.md 已改指针式引用。改出处规则时两处变体须同步评估。
_SLOT_DESCRIPTIONS_ZH: dict[str, str] = {
    "source": (
        "数据来源，受控规范名（候选见本字段清单）。"
        "用户原话点名了来源时**必填**（填规范名）；**点名多个来源时只填最先点名的那个**"
        "（剩下的由后续步骤逐一处理），不许把多个来源挤进一个槽；没点名就不填（不填=查全部）。"
    ),
    "keywords": (
        "联网搜的关键词：从原话提取主题词（病种、组织、技术等），"
        "不含「联网搜一下/检查/有没有」这类操作词；没有主题就不填。"
        "联网源（ArrayExpress / CELLxGENE / HuBMAP / Single Cell Portal）都是**英文源**："
        "主题词优先给英文（「人类肺」→ human lung），照填中文大概率搜不到。"
        "主题词必须有真实出处：原话里找得到（可以翻译成英文），"
        "或逐字取自已完成步骤的真实结果（如检查更新发现的疑似新增条目标题）；"
        "两头都没有就不填、不发明。"
    ),
    "species": "物种（如 Human / Mouse）；用户没说就不填。",
    "target": "用户点名的对象原文片段（如 upload_mouse_lung.json 这类文件名、或编号）；没有就不填。",
    "query": (
        "改写后的检索句：把当前查询换成规则更容易正确解析的说法，语义等价、"
        "不新增用户没表达的条件；当前没有可改的查询就不填。"
    ),
    # 2026-09-13 夜批 rerank 双通道：relax 槽（结构化定向放松，零 LLM）。
    "relax": (
        "定向放松清单（数组，元素取上次 rerank 结果 probe_zh 报告里列出的 key："
        "dim:维度 / exclude:维度 / raw / date / only:维度，可多选）。"
        "填了就走零-LLM 定向放松重检（不再改写）；报告里没有的 key 不要发明，"
        "拿不准就不填（走默认改写通道）。"
    ),
    # display 槽已退役（2026-09-13 rerank 处置批）：上屏唯一出口是 results.show 动词，
    # rank/rerank 不再声明该槽——原「检索结果是否更新到结果区」描述随之摘除。
    "handle": (
        "要上屏的试跑结果句柄：latest（最近一次检索/重查试跑，缺省值）/ latest_rank / "
        "latest_rerank / 步号（如 \"3\"）；没有就不填（=latest）。"
    ),
    "target_route": (
        "要换到的处理路线：search=检索向（找数据/改条件/贴编号）、"
        "action=动作向（下载/联网搜库/检查更新/入库/管护）、"
        "general=全能兜底（拿不准就走它）；必填。"
    ),
    # 通用兜底句（2026-08-17 缝合）：声明 reason 槽的动词都应登记下方专职描述；
    # 本句只保证键存在（test_every_declared_slot_has_a_dedicated_description 的口径）。
    "reason": "补充理由（一句中文）；没有就不填。",
    # 2026-08-18 环内四工具批：compare.datasets 的 a/b 与 compat.find / fair.check 的 uid
    # ——三个槽都接「编号或名称」，缺省语义各自写清（缺省 = 当前结果第 N 条）。
    "a": (
        "第一个数据集的**编号或名称**（可选）：用户原话点名了对比对象就填（如 GSE…、"
        "E-MTAB…、cxg:…、数据集名）；「第一条/第二个/这个/它」这类**指代词不是编号**，"
        "不要填——不填时缺省会取当前结果第一条（结论里会说明）。"
    ),
    "b": (
        "第二个数据集的**编号或名称**（可选）：原话点名了才填；「第一个/前两条/这个/它」"
        "这类**指代词不是编号**，不要填——不填时缺省会取当前结果第二条（结论里会说明）。"
    ),
    "uid": (
        "数据集的**编号或名称**（可选）：用户原话点名了才填（如 GSE…、E-MTAB…、cxg:…、"
        "数据集名）；「第一条/第二条/这个/它」这类**指代词不是编号**，不要填——"
        "不填时缺省会取当前结果第一条（输出里会说明）。"
    ),
    # 2026-08-20：cite.export 的 uids 数组槽——按编号清单导出（真实消费）
    # 数组元素可为真实编号，也可为同批前序检索结果的占位引用（`$<N>.top[<i>].dataset_uid`，
    # 形状与流向规则见 prompts/loop_core.md 依赖占位节），两种可混用。
    "uids": (
        "要导出引文的数据集编号清单（数组，最多 20 个；元素可以是真实 dataset_uid，"
        "也可以是同批前序检索结果的占位引用 `$<N>.top[<i>].dataset_uid`，两种可混用）。"
        "用户原话点名了编号/条数才填；没点名就不填（缺省 = 当前结果）。"
    ),
}

#: reason 槽的**按动词专职描述**（2026-08-17 缝合，与 `_QUERY_SLOT_DESCRIPTIONS_ZH`
#: 同型）：rerank 的「为什么优化检索词」与 route.request 的「为什么换路线」是两个语义，
#: 通用句谁贴都不对——未登记的动词回退通用兜底句。
_REASON_SLOT_DESCRIPTIONS_ZH: dict[str, str] = {
    "rerank": "为什么需要优化检索词（一句中文，如「原句太口语化」）；没有就不填。",
    "route.request": "为什么要换路线（一句中文，如「用户要找数据、不是管护动作」）；没有就不填。",
    # 2026-09-14 YOLO ask.relax：发起放宽询问的理由（随选择卡展示给用户，要诚实具体）。
    "ask.relax": "为什么判定当前查询的条件不合理/互相打架（一句中文，会展示给用户）；没有就不填。",
}

#: query 槽的**按动词专职描述**：「改写后的检索句…」那句只贴合
#: search.rerun（改写当前查询），rank（裸新检索）与 rerank（原始坏 query）各自需要
#: 自己的口径——与 `_SOURCE_SLOT_DESCRIPTIONS_ZH` 同型；未登记的动词回退通用描述。
_QUERY_SLOT_DESCRIPTIONS_ZH: dict[str, str] = {
    "rank": (
        "要检索的完整检索句：物种/组织/疾病/平台等实体写规范名，去掉口语操作词；必填。"
    ),
    "rerank": (
        "质量差、需要优化的**原始**检索句（逐字取当前查询或原话，不要自己先改）；必填。"
    ),
}


def _args_model_for(spec: "_ap.VerbSpec") -> type[BaseModel]:
    """单个 verb 的入参模型：通用槽（quoted/confidence/reason，执行类加 cancelled）+
    路由检索动词的 effective_query + 该 verb 声明的 slots——与旧手写版的字段集**逐位一致**。
    字段一律无默认值（模型只用于**生成** schema，从不实例化校验——LLM 入参的裁决层是
    `build_plan_from_raw`/`_finalize`，不在这里）。"""
    fields: dict[str, Any] = {
        "quoted": (str, Field(
            description="用户原话里逐字出现的一段连续文字；执行类动作必填，给不出就改选 none。")),
        "confidence": (Confidence, Field(description="拿不准填 low。")),
        "reason": (str, Field(description="一句中文理由，20 字以内。")),
    }
    if spec.kind == _ap.EXEC:
        fields["cancelled"] = (bool, Field(
            description="用户明确说不做这个动作时填 true（动词照选，由执行层决定不执行）。"))
    if spec.verb in _ap.ROUTE_QUERY_VERBS:
        # 留痕：ROUTE_QUERY 三动词在 agent 环内的投影已退役——
        # scoped 各套件面都不装它们（_SUITE_UNDERSTAND_VERBS 不含、route.request 不进
        # 首步面），本分支生成的 schema 在生产**不可达**（只被全表生成遍历到，从不
        # 被 bind 给模型）。刻意保留不摘：保底 plan_action 的提示词/护栏仍在消费
        # effective_query 机制（单源真源 VERB_SPECS 不拆），且全表 schema 生成被
        # test_agent_schemas 逐动词钉住——摘了要动一片钉，换不来行为差。
        fields["effective_query"] = (str, Field(
            description="完整、可独立执行的检索句（本类动词必填；其余动词不填）。"))
    for slot in spec.slots:
        if slot == "limit":
            # 上下界与裁决层同源：`_resolve_limit` 的 <1 丢弃、>MAX_LIMIT clamp。
            fields["limit"] = (int, Field(
                ge=1, le=_ap.MAX_LIMIT, description="用户明确说了条数才填数字，否则不填。"))
        elif slot == "source":
            # 评审裁决：枚举与描述都按 verb 分派（联网搜给能搜的、检查/同步给能查的）
            # 名单与 decide 规则表的候选串同出 `source_names_for`——一个真源三处用。
            fields["source"] = (
                Literal.__getitem__(source_names_for(spec.verb)),
                Field(description=_SOURCE_SLOT_DESCRIPTIONS_ZH.get(
                    spec.verb, _SLOT_DESCRIPTIONS_ZH["source"])),
            )
        # display 布尔槽的 schema 分支已随该槽退役摘除（2026-09-13 rerank 处置批，
        # 见 _SLOT_DESCRIPTIONS_ZH 的退役注记）；handle 等字符串槽走下方通用 else。
        elif slot == "uids":
            # 数组槽（2026-08-20 批）：cite.export 的编号清单——schema 层给
            # list[str] 类型引导（元素可混占位引用）；形状/流向裁决归 build_plan_from_raw
            # 与 execute 解析层，required 恒空铁律不变。
            fields["uids"] = (list[str], Field(description=_SLOT_DESCRIPTIONS_ZH["uids"]))
        elif slot == "relax":
            # 数组槽（2026-09-13 夜批 rerank 双通道）：探测报告 key 清单（可多选）——
            # schema 层给 list[str] 类型引导；key 合法性裁决归 `_relax_to_suppressed`。
            fields["relax"] = (list[str], Field(description=_SLOT_DESCRIPTIONS_ZH["relax"]))
        else:
            description = _SLOT_DESCRIPTIONS_ZH.get(
                slot, f"{slot} 槽位：按原话里的说法填，没有就不填。")
            if slot == "query":
                # query 槽按动词取专职描述（rank/rerank；search.rerun 回退通用句——
                # 该句被 tests/test_agent_schemas.py 逐字钉住，不得改动）。
                description = _QUERY_SLOT_DESCRIPTIONS_ZH.get(spec.verb, description)
            if slot == "reason":
                # reason 槽按动词取专职描述（rerank/route.request；2026-08-17 缝合）。
                description = _REASON_SLOT_DESCRIPTIONS_ZH.get(spec.verb, description)
            fields[slot] = (str, Field(
                # 专职描述优先（见 _SLOT_DESCRIPTIONS_ZH 的问题说明）；词表将来新增
                # 未登记的槽位时回退泛模板，不崩——但那意味着该补一条专职描述。
                description=description))
    return create_model(f"_ToolArgs_{spec.verb.replace('.', '_')}", **fields)


def verb_parameters_schema(spec: "_ap.VerbSpec") -> dict[str, Any]:
    """单个 verb 的 OpenAI function parameters：**pydantic `model_json_schema()` 生成**。

    与旧手写版的逐字段 diff 只允许：source 多 `enum`（受控规范名真枚举）、limit 多
    `minimum`/`maximum`（1..MAX_LIMIT）；description 文本逐字不变。

    **铁律：`required` 恒空。** schema 是**提示层**（引导 LLM 怎么填），必填与否由
    `build_plan_from_raw`/`_finalize` 机械护栏**裁决**——两层不合并，否则
    violations→repair 自修通道会被 schema 层绕过（模型拒答必填缺失，LLM 连自修机会都没有）。"""
    schema = _args_model_for(spec).model_json_schema()
    props: dict[str, Any] = {}
    for name, prop in schema["properties"].items():
        # pydantic 给每个字段自动带的 "title"（字段名首字母大写）是 schema 自身的元信息，
        # 旧手写版没有、LLM 看了只会稀释提示面——逐字段剥掉，保持提示面逐字稳定。
        prop = dict(prop)
        prop.pop("title", None)
        props[name] = prop
    return {"type": "object", "properties": props, "required": []}


# ==============================================================================================
# 返回契约（第一刀）：LOOP_TOOLS 三工具的出口形状 + Step 实录
#
# 字段提炼依据（2026-08-06 逐路径核对）：
# - `corpus_status.db_status`：generated_at / sources[{source,label,local_count,snapshot_date}] /
#   total_records / external_files[{filename,record_count,curatable,modified_at}] /
#   recycle[{original_filename,record_count,moved_at}] / ledger{entries,by_endpoint,recent}；
#   「单点失败降级为如实空缺」是设计契约 → generated_at 与 ledger 内层字段给默认（测试替身
#   的紧凑形状同此口径），**键集/类型**才是闸的对象。
# - `corpus_curation.check_updates`：checked_at / sources[条目按 mode 三态] / hint_zh。
#   条目公共键 source+mode 恒在（unknown 条目也只有它们 + note_zh）；label/local_count/site_url/
#   snapshot_date/online_recent/new_count/new_candidates/note_zh 按 mode 与成败路径有无 → 可空。
#   mode 三值是下游（_step_projection/_steps_report_fallback_zh）的分支依据 → Literal 收窄。
# - `_loop_search_online` 的 plan/apply 合并 dict：七键恒在且类型稳定（source_label/query/species/
#   sample_titles/record_count/filename/warnings）——任一缺失 = 上游形状已破 → 全字段必填非空。
# ==============================================================================================


class DbStatusSource(BaseModel):
    """db_status.sources 单源条目（`corpus_status._sources_status` 实产四键）。"""

    model_config = ConfigDict(extra="allow")

    source: str
    label: str
    local_count: int
    snapshot_date: str | None = None


class DbStatusExternalFile(BaseModel):
    """db_status.external_files 条目（外部库清单的紧凑投影）。"""

    model_config = ConfigDict(extra="allow")

    filename: str
    record_count: int | None = None
    curatable: bool = False
    modified_at: str = ""


class DbStatusRecycleEntry(BaseModel):
    """db_status.recycle 条目（回收站清单的紧凑投影）。"""

    model_config = ConfigDict(extra="allow")

    original_filename: str
    record_count: int | None = None
    moved_at: str = ""


class DbStatusLedgerRecent(BaseModel):
    """db_status.ledger.recent 单条回显（审计摘要的紧凑投影）。"""

    model_config = ConfigDict(extra="allow")

    ts: str = ""
    endpoint: str = ""
    query: str = ""
    records: int = 0
    error: str | None = None


class DbStatusLedger(BaseModel):
    """db_status.ledger（近期联网审计摘要）。内层字段全默认：空根/无账本时实产即此。"""

    model_config = ConfigDict(extra="allow")

    entries: int = 0
    by_endpoint: dict[str, int] = {}
    recent: list[DbStatusLedgerRecent] = []


class DbStatusResult(BaseModel):
    """`curate.db_status`（`corpus_status.db_status`）的出口契约。"""

    model_config = ConfigDict(extra="allow")

    generated_at: str | None = None
    sources: list[DbStatusSource]
    total_records: int
    external_files: list[DbStatusExternalFile]
    recycle: list[DbStatusRecycleEntry]
    ledger: DbStatusLedger


class CheckUpdatesNewCandidate(BaseModel):
    """check_updates 疑似新增条目（实产 {accession, title}，`_step_grounding_texts` 逐字引用）。"""

    model_config = ConfigDict(extra="allow")

    accession: str
    title: str = ""


class CheckUpdatesEntry(BaseModel):
    """check_updates.sources 单源条目：公共键 source+mode 必填；其余按 mode 与成败路径可空。
    mode 三值（在线比对 / 离线快照如实报告 / 来源名认不出）是下游分支依据 → Literal 收窄。"""

    model_config = ConfigDict(extra="allow")

    source: str
    mode: Literal["online", "snapshot", "unknown"]
    label: str | None = None
    local_count: int | None = None
    site_url: str | None = None
    snapshot_date: str | None = None
    online_recent: int | None = None
    new_count: int | None = None
    new_candidates: list[CheckUpdatesNewCandidate] | None = None
    note_zh: str | None = None


class CheckUpdatesResult(BaseModel):
    """`curate.check_updates`（`corpus_curation.check_updates`）的出口契约。"""

    model_config = ConfigDict(extra="allow")

    checked_at: str
    sources: list[CheckUpdatesEntry]
    hint_zh: str


class SearchOnlineResult(BaseModel):
    """`curate.search_online`（plan/apply 合并 dict）的出口契约：七键**必填**——
    合并 dict 的键集由 `agent_exec._loop_search_online` 固定构造，任一缺失/错型
    即上游（plan/apply）形状已破，必须当失败如实记，不许带残缺形状报 ok。
    filename 例外可为 None：候选全部已在库中的零写入是
    **合法诚实回报**（`corpus_curation.apply_search_online` 既有契约，
    tests/test_corpus_curation.py 钉死 `filename is None`）——它是「未重复入库」，
    不是形状破，形状闸不得把它误判成 bad_result_shape。"""

    model_config = ConfigDict(extra="allow")

    source_label: str
    query: str
    species: str
    sample_titles: list[str]
    record_count: int
    filename: str | None
    warnings: list[str]


class SyncUpdatesEntry(BaseModel):
    """sync_updates.sources 单源条目（`corpus_curation.sync_updates` 实产形状）：
    公共键 source/mode/imported_count/filename/imported_titles/note_zh 恒在；
    label/local_count/new_count 随上游 check_updates 条目可空。
    mode 沿用 check_updates 的三值口径（下游分支依据）→ Literal 收窄。"""

    model_config = ConfigDict(extra="allow")

    source: str | None = None
    label: str | None = None
    mode: Literal["online", "snapshot", "unknown"] | None = None
    local_count: int | None = None
    new_count: int | None = None
    imported_count: int
    filename: str | None = None
    imported_titles: list[str]
    note_zh: str


class SyncUpdatesResult(BaseModel):
    """`curate.sync_updates`（`corpus_curation.sync_updates`）的出口契约。"""

    model_config = ConfigDict(extra="allow")

    checked_at: str | None = None
    sources: list[SyncUpdatesEntry]
    imported_total: int
    hint_zh: str


class SearchRerunResult(BaseModel):
    """`search.rerun`（`agent_exec._loop_search_rerun` 机械闸合并 dict）的出口契约：
    六键恒在——adopted（机械闸是否采纳改写）/ reason（adopted /
    rewrite_no_change_kept_original / structured_context_lost_kept_original）/
    query（改写句）/
    n_after（改写后条数，未采纳时也如实记实算值）/ n_before（基准条数；无基准查询可比时
    如实为 None）/ replace_screen（rescue 入口恒 true，链内恒 false）。
    payload 仅 adopted 时非 None（/api/recommend 同形 dict，由 app.recommend_rows.recommend_payload
    构造）。
    设计决定：**命中 0 条也采纳**（空结果集照常上屏，是条件变更重检的诚实
    答案）——原「改空拒」档 rewrite_empty_kept_original 退役，拒绝只剩同集/条件丢失两档。
    rescue2additive 两键：dropped_terms（采纳时改写句里消失的未收录词，
    机械子串比对；未采纳恒 []）、disclosure_zh（采纳档确定性披露句，未采纳为 None）。
    nl-Aadditive 两键：n_before_total/n_after_total（**屏口径**未截断命中
    总数——采纳档 n_after_total 与屏单源 = payload.result_total，n_before_total 为基准
    同管线硬过滤存活数；无基准时 n_before_total 同 n_before 如实为 None）。n_before/
    n_after 保持择优闸口径（top-k 截断，步骤卡明示）不变。"""

    model_config = ConfigDict(extra="allow")

    adopted: bool
    reason: str
    query: str
    n_before: int | None = None
    n_after: int
    n_before_total: int | None = None
    n_after_total: int | None = None
    replace_screen: bool
    payload: dict[str, Any] | None = None
    dropped_terms: list[str] = []
    disclosure_zh: str | None = None


class RankResult(BaseModel):
    """`rank`（`agent_exec._loop_rank`）的出口契约：六键恒在——
    query（生效检索句）/ total（命中总数，meta.result_total 口径 = /api/recommend 未截断
    命中总数）/ filters（生效条件，meta.active_filters 原样 = **投影 dict 的列表**，
    workflow._active_filters 投影形状）/ top（前 3 条紧凑 digest）/
    displayed（是否上屏）/ batch（批次原料 dict——kind/label/
    query_raw/query_effective/payload，batch_id/seq/created_at 由轮级装配补齐；否则 None）。
    上屏语义（2026-09-13 rerank 处置批）：display 槽已退役——结果经规则程序判定健康时
    由后端**不经 LLM 自动上屏**（auto-show，results.show 的机械直调），displayed/batch
    即由它产生；不健康时两键为 False/None，屏态改经 results.show 动词。
    top 条目 2026-08-20 扩字段：dataset_uid + rank（1 起序号）——同批依赖占位的
    解析源（`$<N>.top[<i>].dataset_uid`），decide 据它挑对象、execute 据它解析。
    relax_live（2026-09-14 YOLO 批，additive）：degenerate 试跑顺带的 ask.relax
    适用性情报（零 LLM 预演「放松哪维能救回几条」，key/label/count 紧凑 digest）；
    None=未探测（healthy/弃权/异常档不探）。"""

    model_config = ConfigDict(extra="allow")

    query: str
    total: int
    filters: list[dict[str, Any]] = []
    top: list[dict[str, Any]] = []
    displayed: bool = False
    batch: dict[str, Any] | None = None
    relax_live: list[dict[str, Any]] | None = None


class RerankResult(BaseModel):
    """`rerank`（`agent_exec._loop_rerank`）的出口契约：在 RankResult
    口径上把 query 拆成三键——original_query（原始坏 query）/ rewritten_query（实际生效
    的检索句；改写未通过机械健全性检查时 = 原句）/ rewritten（改写是否被采纳，如实标注）。
    top 条目同 RankResult 的批扩字段（dataset_uid + rank，依赖占位解析源）。
    试跑化（2026-09-13 rerank 处置批）：rerank 不再直接上屏，只**试跑**并机械对照
    新旧结果健康度，三态出口——①基线健康+试跑恶化 → rejected=true + comparison_zh
    对照（无 batch，不上屏）；②基线恶化+试跑健康 → 自动上屏（auto_shown=true，
    displayed/batch 由它产生）；③其余 → 仅 advisory 对照（baseline + comparison_zh，
    明显改善由模型经 results.show 上屏）。生死线只拦「健康→恶化」，双方都健康时
    机械层只报事实不判「更好」。
    双通道（2026-09-13 夜批）：applied 标记本次走的通道——"relax"（结构化放松，
    零改写确定性重检原句，relaxed 列出实际生效的 suppressed filter_id）/
    "rewrite"（改写被采纳）/ "none"（改写未通过机械检查退回原句）；
    probe_zh 回传本次注入改写调用的松弛试算报告（仅改写通道非空）——decide 侧据此
    决定下一轮是否走 relax 通道（报告 key 即 relax 槽合法取值）。
    relax_live（2026-09-14 YOLO 批，additive）：同 RankResult——degenerate 试跑
    顺带的 ask.relax 适用性情报（按生效检索句 + 原始结构化现场预演，与
    `_loop_ask_relax` 的弹卡口径同源）。
    双闸（2026-09-18 触发子集批，additive）：
    - 触发闸——屏面 healthy/unknown 且结果充足（>3 条）时工具如实拒绝执行
      （零 LLM、零试跑）：refused=true + refuse_reason="screen_healthy" + note_zh
      如实句（拒绝是数据不是故障，同 ask.relax 的 screen_not_degenerate 先例）；
    - 语义闸——改写定稿经 `_constraint_diff` 机械对比原句，未授权语义变化即
      semantic_blocked=true：屏面健康 → rejected=true + reject_reason=
      "semantic_change"（生死线扩档，原档记 "trial_degenerate"）；空屏面放行
      auto-show 但 disclosure_zh 如实标注语义变化明细；其余档 advisory 附警示。
    repair 术式（2026-09-18 专家化批 WP3，additive）：改写通道前的零 LLM 局部
    纠错路由——原句因未收录词弃权时，受控词表唯一最近邻打补丁（applied="repair"），
    纠正明细随 comparison_zh 给出、空屏 auto-show 时 disclosure_zh 如实标注
    「请核实」；不适用/不采纳时 repair 键为 None 或候选 advisory（adopted=False），
    改写通道照旧兜底。repair 键形状：{terms: [{term, candidates: [{dim, display,
    alias, targets, absent, dist}]}], adopted, patched_query, adopted_fixes}。"""

    model_config = ConfigDict(extra="allow")

    original_query: str
    rewritten_query: str
    rewritten: bool
    applied: str = ""
    relaxed: list[str] = []
    probe_zh: str = ""
    repair: dict[str, Any] | None = None
    total: int
    filters: list[dict[str, Any]] = []
    top: list[dict[str, Any]] = []
    displayed: bool = False
    batch: dict[str, Any] | None = None
    baseline: dict[str, Any] | None = None
    comparison_zh: str = ""
    rejected: bool = False
    auto_shown: bool = False
    relax_live: list[dict[str, Any]] | None = None
    refused: bool = False
    refuse_reason: str = ""
    note_zh: str = ""
    reject_reason: str = ""
    semantic_blocked: bool = False
    disclosure_zh: str = ""


class ShowResult(BaseModel):
    """`results.show`（`agent_exec._loop_show`）的出口契约（2026-09-13 rerank 处置批，
    屏态唯一出口）：八键恒在——shown（是否真上屏；句柄无对应试跑或指纹复核不符时
    False，fail-closed 如实回报）/ handle（入参原样）/ origin（试跑来源动词：
    rank / rerank / search.rerun）/ query_effective（生效检索句）/ total（命中总数）/
    overridden（是否覆盖了屏面既有结果）/ batch（批次原料 dict，形状与 rank/rerank
    批次同——kind 取内容来源动词，前端按 kind 渲染无感）/ displayed（=shown，
    与 RankResult 口径对齐）。拒绝原因如实落在 note_zh（extra allow）。"""

    model_config = ConfigDict(extra="allow")

    shown: bool
    handle: str = ""
    origin: str = ""
    query_effective: str = ""
    total: int = 0
    overridden: bool = False
    batch: dict[str, Any] | None = None
    displayed: bool = False


class AskRelaxResult(BaseModel):
    """`ask.relax`（`agent_exec._loop_ask_relax`）的出口契约（2026-09-14 YOLO 批，
    「问用户放松哪个条件」选择卡）：六键恒在——asked（是否真发起了询问；闸内情拒绝
    时 False + refuse_reason 落在 extra，拒绝是数据不是故障）/ query（被判坏的原查询）/
    reason（模型给的判定理由，随卡展示）/ options（给模型看的选项摘要 list，无 suppress
    细节）/ ask_relax（真发起时的前端选择卡载荷 dict：query + reason + options〔含
    suppress；不含 preview——2026-09-14 用户评审：示例名塞按钮信息效率太低〕；拒绝/
    未发起时 None）/ note_zh（人读如实句，成功与拒绝恒在）。
    本工具不上屏（屏态唯一出口仍是 results.show），只发问卡。"""

    model_config = ConfigDict(extra="allow")

    asked: bool
    query: str = ""
    reason: str = ""
    options: list[dict[str, Any]] = []
    ask_relax: dict[str, Any] | None = None
    note_zh: str = ""


class RouteRequestResult(BaseModel):
    """`route.request`（`agent_exec._loop_route_request`）的出口契约（2026-08-17 M1
    逃生口）：三键恒在——requested_route（目标路线，机械校验 ∈ search/action/general）/
    switched（恒 True——能产出本结果即放行；路线切换由 execute 据 slots 写 state.route_scope）/
    reason（模型给的换线理由，可空串）。"""

    model_config = ConfigDict(extra="allow")

    requested_route: str
    switched: bool
    reason: str = ""


class RollbackResult(BaseModel):
    """`curate.rollback`（`agent_exec._loop_curate_rollback`）的出口契约（2026-08-17 rb1
    回滚动词化）：十键恒在——snapshot_id（回退锚；无可回滚步时如实 None）/ rolled_back
    （是否真回了）/ reason（rolled_back / no_rollbackable_step / snapshot_not_finalized /
    snapshot_unavailable / snapshot_error / rollback_incomplete）/ verb（被回退步的动词，拒绝时 ""）/
    recycled / restored（文件名清单）/ skipped / unrestorable / errors（
    `trace.rollback.apply_rollback` 的如实清单原样透传）/ note_zh（人读如实句，
    成功与拒绝都恒在——execute 摘要与兜底汇报直接引用它，不二次概括）。
    拒绝是数据不是故障（search.rerun adopted=False 同哲学）：机械闸拒绝不抛异常。"""

    model_config = ConfigDict(extra="allow")

    snapshot_id: str | None = None
    rolled_back: bool
    reason: str
    verb: str = ""
    recycled: list[str] = []
    restored: list[str] = []
    skipped: list[dict[str, Any]] = []
    unrestorable: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    note_zh: str


class CompareField(BaseModel):
    """compare.datasets 的单字段差异条目（确定性 diff 产出的展示/事实一体行）：
    field（item_view 键名）/ label_zh（中文展示名）/ a / b（两侧值，缺失为 ""）/
    status（same / different / only_a / only_b / both_missing——下游分支与计数依据）。"""

    model_config = ConfigDict(extra="allow")

    field: str
    label_zh: str
    a: str = ""
    b: str = ""
    status: str


class CompareResult(BaseModel):
    """`compare.datasets`（`agent_exec._loop_compare_datasets`）的出口契约（2026-08-18
    四工具批）：a / b（条目 digest：dataset_uid + dataset_name + source）/ assumption_zh
    （未指定对比对象时说明默认假设，指定则空串）/ fields（确定性 diff 逐字段——
    **事实层**）/ n_same / n_diff / n_unknown / identical（n_diff==0——字段全同是如实
    结论不是失败）/ comparison_zh（用户可见结论：LLM 措辞或确定性兜底，恒在）/
    wording_source（llm / deterministic——措辞是否过了机械交叉核验）/ degraded /
    degrade_reason（无结果 / 找不到指定数据集 / 歧义 / 只有一条可比时的诚实降级；
    降级时 comparison_zh 即降级句，fields 为空）/ caveat_zh（本对比覆盖范围的诚实边界）。"""

    model_config = ConfigDict(extra="allow")

    a: dict[str, Any]
    b: dict[str, Any]
    assumption_zh: str = ""
    fields: list[CompareField] = []
    n_same: int = 0
    n_diff: int = 0
    n_unknown: int = 0
    identical: bool = False
    comparison_zh: str
    wording_source: str = "deterministic"
    degraded: bool = False
    degrade_reason: str = ""
    caveat_zh: str = ""


class CiteExportFile(BaseModel):
    """cite.export 的单份产物（RIS 或 BibTeX）：filename / format（ris / bibtex）/ bytes。"""

    model_config = ConfigDict(extra="allow")

    filename: str
    format: str
    bytes: int


class CiteExportResult(BaseModel):
    """`cite.export`（`agent_exec._loop_cite_export`）的出口契约：
    n_datasets（导出条数）/ uids（导出对象清单）/ files（RIS+BibTeX 两份产物的
    文件名与字节数——前端 runner 旧路径只下 .ris 的缺口在此补上，回执带路径）/
    out_dir（落盘目录，`.userdata/citations/`）/ note_zh（人读回执句，execute 摘要
    与兜底汇报直接引用——含目录路径，用户据此取文件）。写盘产物由 trace 快照锚定
    （readonly=False），可被 curate.rollback 的机械闸看到。"""

    model_config = ConfigDict(extra="allow")

    n_datasets: int
    uids: list[str]
    files: list[CiteExportFile]
    out_dir: str
    note_zh: str


class CompatFindResult(BaseModel):
    """`compat.find`（`agent_exec._loop_compat_find`）的出口契约：
    seed（种子条目 digest）/ criteria（兼容判据：species / chemistry / platform_family）/
    total（兼容总数）/ compatible（前 N 个兼容条目，含 `_compat_basis` 凭据）/
    caveat（`compatibility.CAVEAT_ZH` 诚实边界——**恒带**，绝不越过「元数据兼容」说
    「可整合」）/ note_zh（人读句：种子定位方式 + 兼容数；降级时即降级句）/
    degraded / degrade_reason（种子找不到 / 缺省对象不可得时如实降级）。"""

    model_config = ConfigDict(extra="allow")

    seed: dict[str, Any] = {}
    criteria: dict[str, Any] = {}
    total: int = 0
    compatible: list[dict[str, Any]] = []
    caveat: str = ""
    note_zh: str
    degraded: bool = False
    degrade_reason: str = ""


class FairCheckResult(BaseModel):
    """`fair.check`（`agent_exec._loop_fair_check`）的出口契约：
    dataset_name / source / fair（`fair.assess_fair` 的 13 项检查 + summary + gaps——
    **复用者视角就绪度**，不是官方 FAIR 认证）/ data_availability（投稿数据可用性
    声明）/ note_zh（人读句：readiness_pct + 边界句）/ degraded / degrade_reason。"""

    model_config = ConfigDict(extra="allow")

    dataset_name: str = ""
    source: str = ""
    fair: dict[str, Any] = {}
    data_availability: dict[str, Any] = {}
    note_zh: str
    degraded: bool = False
    degrade_reason: str = ""


#: LOOP_TOOLS 各动词的返回契约模型（execute 节点在 run() 出口 model_validate 的登记处）。
#: 注册表（含测试替身整体替换 LOOP_TOOLS 的场景）按 verb 查这里——替身形状与真表同约，
#: 同步漂移在此被闸住（tests.md 的补位：替身副本与真表从此共享同一份形状闸）。
LOOP_RESULT_MODELS: dict[str, type[BaseModel]] = {
    "curate.db_status": DbStatusResult,
    "curate.check_updates": CheckUpdatesResult,
    "curate.search_online": SearchOnlineResult,
    "curate.sync_updates": SyncUpdatesResult,
    "search.rerun": SearchRerunResult,
    # RAG 工具组与逃生口（常驻，原开关登记分支摘除）。
    "rank": RankResult,
    "rerank": RerankResult,
    # 2026-09-13 rerank 处置批：屏态唯一出口。
    "results.show": ShowResult,
    # 2026-09-14 YOLO 批：「问用户放松哪个条件」选择卡（不上屏，只发问）。
    "ask.relax": AskRelaxResult,
    "route.request": RouteRequestResult,
    # 2026-08-17 rb1 回滚动词化。
    "curate.rollback": RollbackResult,
    # 2026-08-18 环内四工具批：compare.datasets / cite.export / compat.find / fair.check。
    "compare.datasets": CompareResult,
    "cite.export": CiteExportResult,
    "compat.find": CompatFindResult,
    "fair.check": FairCheckResult,
}


class Step(BaseModel):
    """execute 节点 step 实录的**字段齐整**契约。

    实录仍落**纯 dict**（plan.steps 的 JSON 契约逐位不变）——经本模型构造再
    `model_dump(exclude_none=True)`：成功步无 error/error_code 键、失败步无 result 键，
    与历史形状逐位一致；缺字段/错类型在构造期即 ValidationError——字段齐整由代码
    保证，不再靠两处字典字面量各自自觉。
    rb1（2026-08-17 回滚动词化）additive：snapshot_id——写动词步的 trace 快照锚
    （capture 成功才非 None；只读步/快照缺失为 None，exclude_none 不落键，
    既有步骤字节契约不变），curate.rollback 的机械闸从 steps 实录现取。"""

    model_config = ConfigDict(extra="allow")

    verb: str
    verb_zh: str
    ok: bool
    card_kind: str
    readonly: bool
    slots: dict[str, Any] = {}
    ms: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None
    snapshot_id: str | None = None
