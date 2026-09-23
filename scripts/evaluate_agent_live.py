# -*- coding: utf-8 -*-
"""执行侧 agent 的**集成性能验证**（报告含「失败聚类」段与
number_grounded「汇报数字出处」不变量维度）。

与单测的本质区别：单测用 fake model 钉行为（永远满分，无评估意义）；本验证用**真实 LLM**
（`load_llm_config()` 的当前配置）+ **剧本化工具结果**（LOOP_TOOLS 整体换成假工具——
可控地制造 new_count=0 / 网络失败 / 来源认不出 / 形状残缺等边缘情形）+ **沙箱项目根**
（每用例一个 tmp 目录，账本/外部库全落 tmp，**绝不碰真库、绝不真联网下载**）。

被测对象 = LLM 的三处出口：understand（动词与槽位 fidelity）、decide（续步/停环判断）、
narrate（汇报忠实度）。机械护栏不在被测之列（那是单测的地），但护栏**拦截事件**是
重要的性能信号（护栏拦得多 = LLM 出口歪得多），逐项统计；`expect_invalid` 用例里
护栏拒收（AgentPlanInvalid）本身就是**设计正确结局**，按 guard_intercept 维计分。

用例集：`eval/agent_live_cases_v1.jsonl`（一行一个用例；字段见下）。
结果：`eval/agent_live_run_<tag>.jsonl`（逐用例原始记录，供复盘）+
`eval/agent_live_report_<tag>.md`（聚合报告 + 失败聚类 + 失败画廊）。

用例字段：
  id / cat / utterance / note（压榨点说明）
  context（可选）：{"has_results": bool, "result_total": int, "current_query": str,
                   "current_filters": dict}——屏上语境，缺省保持「无结果」现状
  allow_no_exec（可选 true）：显式豁免「first 钉了可执行动词就必须钉执行步」的自检——
                   只给「多条合法路径的执行步集合路径相关」的路由观察类用例用
  tools: {verb: outcome}——outcome = {"const": 负载名} | {"raise": {code,hint}} |
         {"malformed": true} | {"by_source": {归一化来源名: outcome}, "default": outcome}
         （by_source 的键必须已是 `_norm_source` 归一形——运行期查表不做二次归一）
         tools 就是探针世界的**注册表**：LLM 选了 tools 外的 loop 动词 = off-script（见下）。
  expect（全软断言，逐项计分，不存在「一错全否」）：
    first: 首步动词（字符串或 one_of 列表）
    first_slots: {槽位名: [可接受值...]}——对 plan 首步 slots 的大小写不敏感子串匹配
                 （嵌套 dict 拍平一层；路由类动词的主题词落在 effective_query，折成同名伪槽位）
    must_steps: 必须出现的执行步（按序子序列，允许中间插入别的步）。条目两种形态：
                 字符串形 "curate.check_updates"——只钉动词；
                 对象形 {"verb": ..., "source": ..., "keywords": ...}——verb 必填，
                 source 按来源别名组等价比对（`search_request.SOURCE_ALIASES` 词表真源，
                 「10x」与「10x Genomics」同槽），keywords 大小写不敏感子串。
                 v5.3（高-2）起命中步还带**剧本 ok 核对**（单步也核，detail 写清）：
                 const 该 ok=True、raise/malformed 该 ok=False，不一致即 must 记败。
    must_steps_unordered: 条目形同上，但**集合存在性、无序**（各自消耗一个不同步骤）——
                 给 first 允许两种语序的用例（i08/l04 型）替代有序断言
    must_when: [{"if_first": 动词, "must": [条目...]}]——**路径条件断言**（2026-08-08
                 v5.2，取代行动型用例的 allow_no_exec）：按 plan.verb 选子句，命中子句的
                 must 按 must_steps 同判定（按序子序列、字符串/对象形条目均可）；
                 **同一 if_first 允许多个子句（OR，任一满足即过，v5.3 中-3）**——
                 db-first 型两种合法形各自给完整子句。无子句匹配（none/路由类零步结局）
                 → 空过不计败。命中子句 ≥2 条且命中时 chain_complete 同口径参评。
    check_sources: [来源名...]——所有 ok 的 check_updates 步的 slots.source 并集
                 （`_source_equiv` 别名组等价口径）必须覆盖清单——只钉动词不钉来源的
                 通道清零（点 ENCODE 却查 AE 计败）。
    search_topics: [主题串...]——所有 ok 的 search_online 步的 keywords 并集
                 （大小写不敏感拼接）必须逐串含各主题子串——空关键词/漏主题计败。
    forbid_steps / max_steps / zero_writes / cancelled
    ideal_steps: 最优路径步数上界——max_steps 是合法上界，ideal_steps 是最优路径；
                 超 ideal 但低于 max = 浪费动作，单独记该维
    steps_exact: len(steps) 精确等于——截断类用例的钉法（生产上限 MAX_STEPS=8（2026-08-15
                 由 3 放宽，同批用例期望值随新语义重钉）；写 max_steps 恒过无信息量，
                 2026-08-08 v5 起全换成 steps_exact）
    expect_invalid: true 时本例的**唯一正确结局是护栏拒收**——exc 为 AgentPlanInvalid
                 （isinstance 判定，v5.2 收窄：AgentError 基类其余成员如 AgentUnavailable
                 属基础设施异常，不算护栏拦截）且零成功写步 → guard_intercept 计过；
                 护栏没拦 / 非护栏异常 → 计败
    or_invalid: true 时**双结局**（v5.2）：护栏拒收（同上口径）→ guard_intercept 计过；
                 无异常 → 按正常 expect 块照常评分（agent 将来学会正确处理劣质指令
                 也算过）。与 expect_invalid 互斥（启动自检闸）。
    no_ungrounded: true 时逐个 search_online 步过 `_ungrounded_keyword_tokens` 机械核验
    report_contains / report_not_contains: 对 plan.report_zh 的子串断言

自动参与的维（无需在 expect 里点名）：
    chain_complete: must_steps（或 must_steps_unordered，或 must_when 命中子句）≥2 条时
                 自动参评——命中**且逐步 ok 与「剧本隐含期望」一致**（2026-08-08 v5
                 升级，τ² 全链成败口径的剧本感知版）：按 matched step 的 slots.source
                 （`_norm_source` 口径）查该动词的 by_source/default 剧本——
                 raise/malformed/未登记来源期望 ok=False，const 期望 ok=True；剧本
                 确定不了的步不判 ok。首步剧本必败的长链（k08 型）由此机械可过，
                 而「剧本该成却败」依旧记败。
    on_script:     plan.verb / steps 里出现 **tools 外的 loop 动词** → False（2026-08-08
                 v5：off-script 空执行此前是恒过通道——execute 对注册表外动词空过，
                 用例 tools 就是探针世界的注册表）。全程没碰 loop 动词的用例不参评
                 （避免恒过注水）。
    faithful:      （既有）LLM 汇报与 steps 实录的矛盾机械后检，仅 report_source=="llm"
                 且 steps 非空时参评。
    report_covers: 与 faithful 同口径参评——按步序核对每个 ok 步的关键事实在 report_zh
                 中出现：search_online→record_count；check_updates→每个 online 源的
                 new_count；sync_updates→imported_total；db_status→total_records。
                 数字匹配（v5 加固 + v5.2 双修）：边界安全正则
                 `(?<![0-9.,])N(?![0-9.,])`（先剥离千分位逗号；0 额外排除日期/时间
                 邻接 `-` `:` `/`，防快照日期里的「08」被当成 0）；1-10 接受中文小写
                 数字（一/二/两/…/十）。**数字 >0 时必须数字命中**（不再接受 label
                 顶替）；==0 时全部收进**同小句**纪律（v5.2 中-6a + v5.3 中-4）：
                 0/零、固定否定短语、label 后备的命中位置都必须落在该源 label 所在
                 小句（按 `_CLAUSE_BREAKS_ZH` 隔断切句）——「网络没有异常；
                 ArrayExpress 检查完成。」不再误覆盖 new_count=0；label 不在汇报里
                 退到任一同步源 label 的小句，连 label 都没有 → 记缺（残余近似，
                 见已知未覆盖 6）。位置游标按步序消费命中、取正则与措辞候选的最早命中；
                 **游标未命中时从 0 起不消费地全局再找一次**（v5.2 中-6b：保守放行
                 「库内共4756条；本次新增2条」式顺序倒置的诚实汇报）。如实登记的
                 风险：同值多步复用同一处数字不再被游标拦下（两次 record_count=2 的
                 搜索，汇报里一个「2」现在能盖住两次——拿这个假阳性换顺序假阴性，
                 是刻意的取舍）。
    number_grounded:（v6）汇报数字出处不变量——steps 非空且 report_zh 非空时参评
                 （空 steps / 空汇报不适用不计分）。汇报里每个整数（先剥千分位逗号）
                 必须能在 steps 的工具返回 JSON（每步 result 的序列化文本）中作为
                 **数值相等**命中——词边界 `(?<![0-9])N(?![0-9])`，「2」不许在
                 「12」里命中。豁免（防误伤）：≤9 且紧邻「步」字的步骤序号、
                 \d+% 百分比、20\d\d年 年份、\d{4}-\d{2} 日期头。找不到出处 →
                 记败，detail 列无出处的数字。

报告头部三行分：「总分（本集 · 旧维口径，剔除 v4/v5/v6 新维）」/「严格分（全维）」/
「v3 子集参照分」（只统计 `_V3_IDS` 的 89 条、只用 `_V3_DIMS` 的 v3 时代 10 维；
**用例定义已演进，非同一测量，仅供粗略参照**——v3 基线 84/89 (94.4%)；v5.2 起
仅当本次运行覆盖全部 89 个 v3 id 时才显示该行，--only/--limit 子集跑不显示，
防分母不足误标）。头部另记 model/provider/base_url/git commit/工作树 dirty 标记/
用例集 sha256（全 64 位）/harness 与 agent_exec 各自 sha256 前缀/deterministic
兜底占比（fallback 率信号，不计分；逐例 `_report_fallback` 信号维记入 run 记录）。

启动自检（load 即跑，中文报错带行号）：id 唯一、cat 合法、tools 动词/payload 名存在、
by_source 键已归一、raise 的 code/hint 为 str、expect 字段拼写合法且非空、
first/must/forbid 动词在 `action_plan.VERB_SPECS` 封闭表内、must∩forbid 无冲突、
max/ideal/steps_exact 严格 int（拒 bool）且 ideal<=max、steps_exact>=1、
first_slots/context 形状类型、report_contains/not_contains 元素为 str、
must_when 形状（if_first 合法且唯一、must 非空、条目同 must_steps 形）、
check_sources/search_topics 元素为非空 str、or_invalid 与 expect_invalid 互斥、
空 must/must_when 数组拒绝、顶层未知字段拒绝、重复 JSON key 拒绝、
**first 钉了 tools 提供的 loop 动词就必须钉执行**（must_steps/_unordered 含该 verb，
或有 must_when 子句；must_when 在案时 first 提供的每个 loop 动词都要有子句）、
**first 含 tools 未提供的 loop 动词直接报错**（off-script 必败分支不留——豁免仅
allow_no_exec / expect_invalid / or_invalid）——从源头消灭「只测路由不测执行」；
每个 PAYLOAD 过 `agent_schemas.LOOP_RESULT_MODELS` 形状闸、db_status 类负载
sum(sources.local_count)==total_records。

--repeat K（K>=1，K<1 直接报错退出）：每条用例连跑 K 次（每次独立沙箱），run 文件
每用例每轮一条记录（带 round 字段）；报告出「pass^k 总览」（K 次全过的用例占比，
τ-bench pass^k 口径）与「不稳定清单」（0<通过率<1 的用例 + 整例恒败但维度级抖动的
用例，各附翻转维度）。exc 轮次 checks 记伪维 no_exception=False、正常轮次补记
no_exception=True（v5.2 低-9：维度级抖动清单才能看到异常维翻转；维度统计不虚高）。

已知未覆盖（验证盲区清单，刻意只记录不实现）：
  1. 条件因果：剧本静态钉「哪些步该出现、ok 该怎样」，测不出 LLM 是否**因为**条件
     成立/不成立才行动（行为对但因果错分辨不了）。
  2. ok=true 的语义边界：ok = 没抛异常且返回形状合法；形状合法但内容张冠李戴
     （如标题串台）不在 ok 口径内。
  3. fs diff：沙箱文件系统的实际落盘差异未断言——断言只到 steps 实录层。
  4. 注入攻击：utterance 携带 prompt injection、工具结果夹带指令的对抗输入未覆盖。
  5. 部分写入：多步写中途失败的磁盘一致性/回滚行为未覆盖。
  6. report_covers 是保守近似：中文数字/否定语境判断可能误伤（只拉低该维）或放行；
     v5.2 起同值多步复用同一处数字也不再拦（见上报导 covers 段的登记）。
  7. 单轮 utterance：跨轮对话上下文（多轮指代、会话状态）未覆盖。
  8. expect_invalid/or_invalid 的「零成功写步」条件：exc 路径 plan 被清空、steps 恒空，
     该条件恒真——只防「部分执行后才炸」的理论路径，当前实现分辨不了（v5.2 登记）。
  9. 剧本工具不验证真实网络与文件系统：工具结果是剧本常量，沙箱 fs 只到账本层——
     真实适配器的网络行为/磁盘差异不在测量面（v5.3 收尾登记）。
 10. 延迟统计排除异常轮：p50/p95/max 只收 not exc 的记录，炸掉的轮次不进分布。
 11. 严格分失败也 exit=0：本探针是**人工观察/趋势工具，非 CI 准入门**——非零退出
     只给启动自检不过 / --repeat 误用（v5.3 收尾登记）。

跑法（仓库根，真实 API，串行约 15-25 分钟）：
  ./.venv/Scripts/python.exe scripts/evaluate_agent_live.py [--only 子串] [--limit N] [--tag v1] [--repeat K]
离线自验（零 API，2026-08-08）：`--selftest`——启动自检过当前用例集 + 全维度
双向合成断言（过/不过各至少一例）+ 坏样本逐类行号断言，任一不符非零退出。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.agent import action_plan as _ap  # noqa: E402
from dataset_recommender.agent import agent_exec as ax  # noqa: E402
from dataset_recommender.agent import agent_schemas as _schemas  # noqa: E402
from dataset_recommender.retrieval import search_request as _sr  # noqa: E402
from dataset_recommender.llm.llm_client import load_llm_config  # noqa: E402

# --------------------------------------------------------------------------- 剧本化工具负载
# 形状与 agent_schemas.LOOP_RESULT_MODELS 的真实出口契约逐位一致（假工具也过形状闸；
# 启动自检逐个 model_validate，见 _check_payloads）。

PAYLOADS: dict[str, dict] = {
    "db_status_ok": {
        "generated_at": "2026-08-07T00:00:00+08:00",
        "sources": [{"source": "10x", "label": "10x Genomics", "local_count": 774,
                     "snapshot_date": "2026-08-01"},
                    {"source": "cellxgene", "label": "CELLxGENE", "local_count": 2198,
                     "snapshot_date": "2026-08-01"},
                    {"source": "arrayexpress", "label": "ArrayExpress", "local_count": 1784,
                     "snapshot_date": "2026-08-01"}],
        # 2026-08-08 v5（高-4）：total_records 与 corpus_status 定义一致 = 各源 local_count
        # 合计（774+2198+1784=4756）；旧值 5712 无出处（PAYLOAD 语义漂移），自检现在钉死。
        "total_records": 4756, "external_files": [], "recycle": [],
        "ledger": {"entries": 3, "by_endpoint": {"agent_exec:curate.check_updates": 3},
                   "recent": []},
    },
    "db_status_empty": {
        "generated_at": "2026-08-07T00:00:00+08:00", "sources": [], "total_records": 0,
        "external_files": [], "recycle": [], "ledger": {"entries": 0, "by_endpoint": {},
                                                        "recent": []},
    },
    "check_ae2": {
        "checked_at": "2026-08-07T00:00:00+08:00",
        "sources": [{"source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
                     "local_count": 1784, "online_recent": 12, "new_count": 2,
                     "snapshot_date": "2026-08-01",
                     "new_candidates": [{"accession": "E-MTAB-9001",
                                         "title": "human lung atlas refresh"},
                                        {"accession": "E-MTAB-9002",
                                         "title": "human lung tumor single cell"}]}],
        "hint_zh": "",
    },
    "check_zero": {
        "checked_at": "2026-08-07T00:00:00+08:00",
        "sources": [{"source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
                     "local_count": 1784, "online_recent": 0, "new_count": 0,
                     "snapshot_date": "2026-08-01", "new_candidates": []}],
        "hint_zh": "",
    },
    "check_10x_unknown": {
        "checked_at": "2026-08-07T00:00:00+08:00",
        "sources": [{"source": "10x", "mode": "unknown",
                     "note_zh": "来源名认不出或不在检查清单里"}],
        "hint_zh": "",
    },
    "check_many": {
        "checked_at": "2026-08-07T00:00:00+08:00",
        "sources": [{"source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
                     "local_count": 1784, "online_recent": 60, "new_count": 47,
                     "snapshot_date": "2026-08-01",
                     "new_candidates": [{"accession": f"E-MTAB-91{i:02d}",
                                         "title": t} for i, t in enumerate([
                                             "human lung atlas refresh", "mouse brain cortex seq",
                                             "human breast cancer visium", "zebrafish embryo atlas",
                                             "human colon crc single cell"])]}],
        "hint_zh": "",
    },
    # 2026-08-08 v4 新增：小鼠脑主题的两条疑似新增（I 类 i09「ENCODE 有小鼠脑新增就搜」
    # 的条件成立侧；check_ae2 的条目全是人肺主题，撑不起小鼠脑条件的成立分支）。
    "check_brain2": {
        "checked_at": "2026-08-07T00:00:00+08:00",
        "sources": [{"source": "encode", "label": "ENCODE", "mode": "online",
                     "local_count": 40, "online_recent": 9, "new_count": 2,
                     "snapshot_date": "2026-08-01",
                     "new_candidates": [{"accession": "ENCSR900A1",
                                         "title": "mouse brain cortex seq"},
                                        {"accession": "ENCSR900A2",
                                         "title": "mouse brain development atlas"}]}],
        "hint_zh": "",
    },
    "search_ok": {
        "source_label": "ArrayExpress", "query": "human lung", "species": "Human",
        "sample_titles": ["human lung atlas refresh"], "record_count": 2,
        "filename": "upload_20260807_curate_arrayexpress.json", "warnings": [],
    },
    "search_zero": {
        "source_label": "ArrayExpress", "query": "human lung", "species": "",
        "sample_titles": [], "record_count": 0, "filename": "", "warnings": [],
    },
    # 2026-08-15：search_online「全部撞重零写入」合法契约（corpus 层 filename=None +
    # warnings 说明，tests/test_corpus_curation.py:350 钉死）——钉 exec-tools H1 修复：
    # 形状闸不得再把 filename=None 误判 bad_result_shape。
    "search_dup": {
        "source_label": "ArrayExpress", "query": "human lung", "species": "Human",
        "sample_titles": ["human lung atlas refresh"], "record_count": 3,
        "filename": None,
        "warnings": ["候选共 3 条全部已在库中，未重复入库"],
    },
    "sync_ok2": {
        "checked_at": "2026-08-07T00:00:00+08:00",
        "sources": [{"source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
                     "local_count": 1784, "new_count": 2, "imported_count": 2,
                     "filename": "curate_sync_20260807_abcd.json",
                     "imported_titles": ["human lung atlas refresh",
                                         "human lung tumor single cell"],
                     "note_zh": ""}],
        "imported_total": 2, "hint_zh": "",
    },
    "sync_zero": {
        "checked_at": "2026-08-07T00:00:00+08:00",
        "sources": [{"source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
                     "local_count": 1784, "new_count": 0, "imported_count": 0,
                     "filename": None, "imported_titles": [], "note_zh": "没有疑似新增"}],
        "imported_total": 0, "hint_zh": "",
    },
    "sync_cant_close": {
        "checked_at": "2026-08-07T00:00:00+08:00",
        "sources": [{"source": "encode", "label": "ENCODE", "mode": "online",
                     "local_count": 40, "new_count": 3, "imported_count": 0,
                     "filename": None, "imported_titles": [],
                     "note_zh": "检到 3 条疑似新增，但该来源没有自动入库通道"}],
        "imported_total": 0, "hint_zh": "",
    },
    # 2026-08-18 四工具批：环内结果处理四工具的剧本负载（形状与
    # agent_schemas.LOOP_RESULT_MODELS 的四份新契约逐位一致，假工具也过形状闸）。
    "compare_ok": {
        "a": {"dataset_uid": "xenium-ffpe-human-breast-1-standard",
              "dataset_name": "Xenium FFPE Human Breast", "source": "10x Genomics"},
        "b": {"dataset_uid": "xenium-prime-ffpe-human-breast-cancer",
              "dataset_name": "FFPE Human Breast Cancer", "source": "10x Genomics"},
        "assumption_zh": "未指定对比对象，默认取当前结果的前两条进行对比。",
        "fields": [
            {"field": "dataset_name", "label_zh": "数据集名称",
             "a": "Xenium FFPE Human Breast", "b": "FFPE Human Breast Cancer",
             "status": "different"},
            {"field": "sample_size", "label_zh": "样本量",
             "a": "576963 Cells", "b": "699110 Cells", "status": "different"},
            {"field": "species", "label_zh": "物种",
             "a": "Human", "b": "Human", "status": "same"},
        ],
        "n_same": 9, "n_diff": 3, "n_unknown": 0, "identical": False,
        "comparison_zh": "两个数据集有 9 个字段一致、3 个字段不同"
                         "（样本量 576963 vs 699110 Cells；发表时间 2023-04-18 vs 2024-10-24）。",
        "wording_source": "deterministic", "degraded": False, "degrade_reason": "",
        "caveat_zh": "",
    },
    "cite_export_ok": {
        "n_datasets": 2, "uids": ["xenium-ffpe-human-breast-1-standard",
                                  "xenium-prime-ffpe-human-breast-cancer"],
        "files": [
            {"filename": "reused-public-datasets-20260819.ris", "format": "ris", "bytes": 1000},
            {"filename": "reused-public-datasets-20260819.bib", "format": "bibtex", "bytes": 1488},
        ],
        "out_dir": "C:/x/.userdata/citations",
        "note_zh": "已导出 2 个数据集的引文，RIS 与 BibTeX 两种格式都已落盘。",
    },
    "compat_find_ok": {
        "seed": {"dataset_uid": "xenium-ffpe-human-breast-1-standard",
                 "dataset_name": "Xenium FFPE Human Breast", "source": "10x Genomics"},
        "criteria": {"species": ["human"], "chemistry": "Xenium In Situ Gene Expression",
                     "platform_family": "xenium"},
        "total": 34,
        "compatible": [
            {"dataset_uid": "xenium-v1-human-breast-ffpe",
             "dataset_name": "Xenium v1 Human Breast FFPE",
             "_compat_basis": "chemistry=Xenium In Situ Gene Expression、platform=xenium"},
        ],
        "caveat": "「元数据兼容」只表示物种一致、且 chemistry 或平台相同——这是可整合的"
                  "必要非充分条件，实际能否整合还取决于批次效应等，本工具不做这些判断。",
        "note_zh": "已按「Xenium FFPE Human Breast」的元数据找到 34 个兼容数据集"
                   "（共享物种，且 chemistry 或平台相同）。",
        "degraded": False, "degrade_reason": "",
    },
    "fair_check_ok": {
        "dataset_name": "Xenium FFPE Human Breast with Custom Add-on Panel",
        "source": "10x Genomics",
        "fair": {
            "checks": [{"principle": "F", "id": "F1", "label": "持久标识符",
                        "status": "partial", "evidence": "只有来源页 URL",
                        "action": "到来源确认公开指认方式"}],
            "summary": {"pass": 11, "partial": 2, "unknown": 0, "total": 13,
                        "readiness_pct": 92,
                        "statement": "13 项复用就绪度检查：11 项充分、2 项部分、0 项未知"
                                     "（未知 = 来源未标注，或本工具未核验；都不等于不满足）。"},
            "gaps": [{"id": "F1", "label": "持久标识符", "action": "到来源确认"}],
        },
        "data_availability": {"statement": "The spatial transcriptomics dataset …",
                              "missing": [], "notes": ""},
        "note_zh": "「Xenium FFPE Human Breast」的 FAIR 复用就绪度：92%"
                   "（11 项充分 / 2 项部分 / 0 项未知）——这是复用者视角的就绪度自检，"
                   "不是官方 FAIR 认证，也不是对数据质量的评价。",
        "degraded": False, "degrade_reason": "",
    },
    # 2026-08-22：rank 剧本负载（混合诉求用例）——六键过 RankResult 形状闸；
    # displayed=True 但 batch=None（剧本不装配批次原料，RankResult 允许 None）。
    "rank_ok": {
        "query": "human lung", "total": 2, "filters": [],
        "top": [{"dataset_uid": "10x-human-lung-1", "dataset_name": "Human Lung scRNA 1",
                 "species": "Human", "tissue": "Lung", "disease": "",
                 "source": "10x Genomics", "rank": 1},
                {"dataset_uid": "10x-human-lung-2", "dataset_name": "Human Lung scRNA 2",
                 "species": "Human", "tissue": "Lung", "disease": "",
                 "source": "10x Genomics", "rank": 2}],
        "displayed": True, "batch": None,
    },
}


class _ToolBoom(Exception):
    def __init__(self, code: str, hint: str):
        super().__init__(hint)
        self.code = code
        self.hint = hint


# label_zh 一律程序取唯一真源 action_plan.VERB_BY_NAME 的 .zh，不手抄第二份词表。
_TOOL_META = {
    "curate.db_status": {"label_zh": _ap.VERB_BY_NAME["curate.db_status"].zh, "card_kind": "db_status",
                         "readonly": True, "report": True, "observation": True},
    "curate.check_updates": {"label_zh": _ap.VERB_BY_NAME["curate.check_updates"].zh, "card_kind": "check_updates",
                             "readonly": True},
    "curate.search_online": {"label_zh": _ap.VERB_BY_NAME["curate.search_online"].zh, "card_kind": "search_online",
                             "readonly": False},
    "curate.sync_updates": {"label_zh": _ap.VERB_BY_NAME["curate.sync_updates"].zh, "card_kind": "sync_updates",
                            "readonly": False},
    # 2026-08-18 四工具批：环内结果处理四工具入剧本面（缺省对象由剧本语境承载——
    # 工具结果是常量负载，真实语料/落盘不在探针测量面）。刻意**不**带 needs_context：
    # 剧本 stub 的 run 是 (slots, root) 二参签名，execute 按注册项有无该键决定注入第三参。
    "compare.datasets": {"label_zh": _ap.VERB_BY_NAME["compare.datasets"].zh, "card_kind": "compare",
                         "readonly": True},
    "cite.export": {"label_zh": _ap.VERB_BY_NAME["cite.export"].zh, "card_kind": "cite_export",
                    "readonly": False},
    "compat.find": {"label_zh": _ap.VERB_BY_NAME["compat.find"].zh, "card_kind": "compat_find",
                    "readonly": True},
    "fair.check": {"label_zh": _ap.VERB_BY_NAME["fair.check"].zh, "card_kind": "fair_check",
                   "readonly": True},
    # 2026-08-22：rank 入剧本面（混合诉求用例的检索半）。同样刻意**不**带
    # needs_context——剧本 stub 的 run 是 (slots, root) 二参签名（同上方四工具批注释）。
    "rank": {"label_zh": _ap.VERB_BY_NAME["rank"].zh, "card_kind": "rank", "readonly": True},
}

#: 图内可执行的 loop 动词（= LOOP_RESULT_MODELS 覆盖的四动词）——on_script 维与
#: 「first 钉了就必须钉执行」自检的判定集合。与 _TOOL_META 同一份真源。
_LOOP_VERBS: tuple[str, ...] = tuple(_TOOL_META)

#: search_online 负载的来源 label 映射（高-4：named source 的 label 跟随槽位）——
#: 从 `search_request.SOURCE_ALIASES` 词表真源程序取（规范名 + 全部别名的归一形
#: 都映射到规范名），不手抄第二份字符串表。
_SEARCH_SOURCE_LABELS: dict[str, str] = {
    **{ax._norm_source(name): name for name, _aliases in _sr.SOURCE_ALIASES},
    **{ax._norm_source(alias): name for name, aliases in _sr.SOURCE_ALIASES for alias in aliases},
}


def _source_equiv(a, b) -> bool:
    """来源名等价判定：`_norm_source` 归一后相等，或落在同一个别名组
    （`search_request.SOURCE_ALIASES` 的规范名 ∪ 别名，归一比对）——
    「10x」与「10x Genomics」同槽，must_steps 对象形的 source 断言用这个口径。"""
    na, nb = ax._norm_source(a), ax._norm_source(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    for canonical, aliases in _sr.SOURCE_ALIASES:
        group = {ax._norm_source(canonical)} | {ax._norm_source(x) for x in aliases}
        if na in group and nb in group:
            return True
    return False


def _outcome_to_run(outcome: dict):
    if "by_source" in outcome:
        table = {str(k): v for k, v in outcome["by_source"].items()}
        default = outcome.get("default")

        def run(slots, root):
            key = ax._norm_source(slots.get("source"))
            picked = table.get(key, default)
            if picked is None:
                raise _ToolBoom("source_not_registered", "该来源本工具接不了")
            return _outcome_to_result(picked, slots)
        return run

    def run(slots, root):
        return _outcome_to_result(outcome, slots)
    return run


def _outcome_to_result(outcome: dict, slots: dict | None = None):
    if outcome.get("malformed"):
        return {"bogus": 1}  # 形状残缺 → 形状闸应按 bad_result_shape 拦下
    if "raise" in outcome:
        exc = _ToolBoom(str(outcome["raise"].get("code") or "network_error"),
                        str(outcome["raise"].get("hint") or "网络抖动"))
        raise exc
    result = json.loads(json.dumps(PAYLOADS[outcome["const"]], ensure_ascii=False))
    slots = slots or {}
    # 来源感知修正（2026-08-07 用例校准）：点名来源的检查/同步，负载的 source/label 跟着
    # 槽位走——否则 ENCODE 的检查会背一份 ArrayExpress 字样的负载，汇报措辞被假数据带歪。
    named = str(slots.get("source") or "").strip()
    if named and result.get("sources") and isinstance(result["sources"][0], dict):
        result["sources"][0]["source"] = ax._norm_source(named)
        result["sources"][0]["label"] = named
    # 2026-08-08 v5（高-4）+ v5.2（中-5 补全）：来源感知修正扩展到 search_online 负载——
    # source_label 跟 slots.source（经 _SEARCH_SOURCE_LABELS 词表映射）、query 跟
    # slots.keywords、sample_titles 由 keywords 生成（保持原负载样本条数）、species
    # 槽给了跟随、没给置 ""（旧版留负载默认 "Human" = 残余漂移）、filename 按来源 slug
    # 生成；否则「在 ENCODE 搜 mouse brain」会背一份「ArrayExpress / human lung / Human」
    # 字样的负载，汇报忠实度被假数据带歪。
    if "source_label" in result:
        if named:
            result["source_label"] = _SEARCH_SOURCE_LABELS.get(ax._norm_source(named), named)
            if result.get("filename"):
                result["filename"] = f"upload_20260807_curate_{ax._norm_source(named)}.json"
        keywords = str(slots.get("keywords") or "").strip()
        if keywords:
            result["query"] = keywords
            result["sample_titles"] = [
                f"{keywords} dataset {i + 1}"
                for i in range(len(result.get("sample_titles") or []))]
        result["species"] = str(slots.get("species") or "").strip()
    return result


def build_tools(spec: dict) -> dict:
    return {verb: {"run": _outcome_to_run(outcome), **_TOOL_META[verb]}
            for verb, outcome in (spec or {}).items()}


# --------------------------------------------------------------------------- 用例集启动自检
# load 时即校验，中文报错带行号——加用例的人保存后一跑就知道手误在哪行。
# 2026-08-08 v5（中-6）：payload↔契约、动词合法性、冲突/类型/形状/重复键/未知字段、
# 「first 钉了可执行动词就必须钉执行步」全部收到 load 期。

_LEGAL_CATS = {
    "A单步路由", "B多步链", "C取消否定", "D歧义陷阱", "E出处幻觉", "F工具变异",
    "G汇报忠实", "H历史病例", "I典型场景", "J劣质指令", "K长程任务", "L复杂约束",
}
_LEGAL_EXPECT = {
    "first", "first_slots", "must_steps", "must_steps_unordered", "must_when",
    "forbid_steps", "max_steps", "ideal_steps", "steps_exact", "zero_writes", "cancelled",
    "expect_invalid", "or_invalid", "no_ungrounded", "check_sources", "search_topics",
    "report_contains", "report_not_contains",
}
_LEGAL_CASE_FIELDS = {
    "id", "cat", "utterance", "note", "context", "tools", "expect", "allow_no_exec",
}
_LEGAL_CONTEXT_FIELDS = {"has_results", "result_total", "current_query", "current_filters"}
#: first/must/forbid 的合法动词全集 = action_plan.VERB_SPECS 封闭表（含 loop 四动词）。
_KNOWN_VERBS = set(_ap.ACTION_VERBS)

#: PAYLOAD 名前缀 → 动词（启动自检按此过 LOOP_RESULT_MODELS 形状闸）。
#: 2026-08-18 四工具批：compare/cite/compat/fair 四前缀随剧本面登记同步加入。
_PAYLOAD_VERB_PREFIX = {
    "db": "curate.db_status", "check": "curate.check_updates",
    "search": "curate.search_online", "sync": "curate.sync_updates",
    "compare": "compare.datasets", "cite": "cite.export",
    "compat": "compat.find", "fair": "fair.check",
    "rank": "rank",  # 2026-08-22：rank 剧本负载随混合用例登记
}


def _unique_pairs(pairs: list) -> dict:
    """json object_pairs_hook：重复 JSON key 拒绝（手误两行同键时后值静默盖前值）。"""
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"重复 JSON 键 {key!r}")
        obj[key] = value
    return obj


def _check_payloads(errors: list[str]) -> None:
    """每个 PAYLOAD 过对应 LOOP_RESULT_MODELS 的 model_validate；db_status 类负载
    另钉 sum(sources.local_count)==total_records（与 corpus_status 定义同口径）。"""
    for name, payload in PAYLOADS.items():
        prefix = name.split("_", 1)[0]
        verb = _PAYLOAD_VERB_PREFIX.get(prefix)
        if verb is None:
            errors.append(f"PAYLOADS[{name!r}]：名前缀 {prefix!r} 映射不到动词"
                          f"（合法前缀：{sorted(_PAYLOAD_VERB_PREFIX)}）")
            continue
        model = _schemas.LOOP_RESULT_MODELS[verb]
        try:
            model.model_validate(payload)
        except Exception as err:  # pydantic ValidationError
            first_line = str(err).splitlines()[0] if str(err) else type(err).__name__
            errors.append(f"PAYLOADS[{name!r}]：过不了 {model.__name__} 形状闸：{first_line}")
        if verb == "curate.db_status":
            total = sum(int(s.get("local_count") or 0)
                        for s in payload.get("sources") or [] if isinstance(s, dict))
            if total != payload.get("total_records"):
                errors.append(f"PAYLOADS[{name!r}]：sources.local_count 合计 {total} "
                              f"≠ total_records {payload.get('total_records')}")


def _check_outcome(outcome, where: str, errors: list[str]) -> None:
    """tools 剧本 outcome 的递归形状校验（const/raise/malformed/by_source+default）。"""
    if not isinstance(outcome, dict):
        errors.append(f"{where}：outcome 必须是对象，收到 {type(outcome).__name__}")
        return
    keys = set(outcome)
    if "by_source" in keys:
        extra = keys - {"by_source", "default"}
        if extra:
            errors.append(f"{where}：by_source 与 {sorted(extra)} 混用，形状非法")
        table = outcome["by_source"]
        if not isinstance(table, dict) or not table:
            errors.append(f"{where}：by_source 必须是非空对象")
        else:
            for src, sub in table.items():
                # 运行期 `_outcome_to_run` 用归一化后的槽位值直接查表、不做二次归一——
                # 键必须已是归一形，否则剧本写了也永远接不住。
                if not ax._norm_source(src):
                    errors.append(f"{where}.by_source[{src}]：键经 _norm_source 归一后为空")
                elif ax._norm_source(src) != src:
                    errors.append(f"{where}.by_source[{src}]：键必须是 _norm_source 归一形"
                                  f"（应为 {ax._norm_source(src)!r}）")
                _check_outcome(sub, f"{where}.by_source[{src}]", errors)
        if "default" in keys:
            _check_outcome(outcome["default"], f"{where}.default", errors)
        return
    extra = keys - {"const", "raise", "malformed"}
    if extra:
        errors.append(f"{where}：不识别的 outcome 键 {sorted(extra)}")
    main = [k for k in ("const", "raise", "malformed") if k in keys]
    if len(main) != 1:
        errors.append(f"{where}：const/raise/malformed 必须且只能取其一，收到 {main}")
        return
    if "const" in keys and outcome["const"] not in PAYLOADS:
        errors.append(f"{where}：payload 名 {outcome['const']!r} 不在 PAYLOADS 里"
                      f"（现有：{sorted(PAYLOADS)}）")
    if "raise" in keys:
        if not isinstance(outcome["raise"], dict):
            errors.append(f"{where}：raise 必须是 {{code, hint}} 对象")
        else:
            for rk in ("code", "hint"):
                if rk in outcome["raise"] and not isinstance(outcome["raise"][rk], str):
                    errors.append(f"{where}：raise.{rk} 必须是 str")
    if "malformed" in keys and outcome["malformed"] is not True:
        errors.append(f"{where}：malformed 只接受 true")


def _check_step_want(want, where: str, errors: list[str]) -> None:
    """must_steps / must_steps_unordered 条目形状：字符串（动词）或
    {"verb": 必填, "source"/"keywords": 可选 str} 对象。"""
    if isinstance(want, str):
        if want not in _KNOWN_VERBS:
            errors.append(f"{where}：动词 {want!r} 不在 VERB_SPECS 封闭表里")
        return
    if not isinstance(want, dict):
        errors.append(f"{where}：条目必须是字符串或对象，收到 {type(want).__name__}")
        return
    extra = set(want) - {"verb", "source", "keywords"}
    if extra:
        errors.append(f"{where}：对象形条目不识别键 {sorted(extra)}")
    verb = want.get("verb")
    if not isinstance(verb, str) or not verb:
        errors.append(f"{where}：对象形条目 verb 必填（str）")
    elif verb not in _KNOWN_VERBS:
        errors.append(f"{where}：动词 {verb!r} 不在 VERB_SPECS 封闭表里")
    for opt in ("source", "keywords"):
        if opt in want and not isinstance(want[opt], str):
            errors.append(f"{where}：{opt} 必须是 str")
    if isinstance(want.get("source"), str) and not ax._norm_source(want["source"]):
        errors.append(f"{where}：source 经 _norm_source 归一后为空")


def _want_verb(want) -> str:
    return want if isinstance(want, str) else str(want.get("verb") or "")


def load_cases(path: Path) -> list[dict]:
    """读 JSONL 用例集并做启动自检；任一不符打印中文错误（带行号）并非零退出。"""
    errors: list[str] = []
    _check_payloads(errors)  # PAYLOAD↔契约校验与用例行无关，先集中记
    cases: list[dict] = []
    seen_ids: set[str] = set()
    for ln_no, ln in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not ln.strip():
            continue
        try:
            case = json.loads(ln, object_pairs_hook=_unique_pairs)
        except json.JSONDecodeError as err:
            errors.append(f"第{ln_no}行：JSON 解析失败：{err}")
            continue
        except ValueError as err:  # _unique_pairs 的重复键
            errors.append(f"第{ln_no}行：{err}")
            continue
        if not isinstance(case, dict):
            errors.append(f"第{ln_no}行：用例必须是对象")
            continue
        where = f"第{ln_no}行（{case.get('id') or '?'}）"
        extra_fields = set(case) - _LEGAL_CASE_FIELDS
        if extra_fields:
            errors.append(f"{where}：顶层未知字段 {sorted(extra_fields)}"
                          f"（合法：{sorted(_LEGAL_CASE_FIELDS)}）")
        for field in ("id", "cat", "utterance"):
            if not case.get(field):
                errors.append(f"{where}：缺必填字段 {field}")
        cid = str(case.get("id") or "")
        if cid in seen_ids:
            errors.append(f"{where}：id 重复")
        seen_ids.add(cid)
        if case.get("cat") not in _LEGAL_CATS:
            errors.append(f"{where}：cat {case.get('cat')!r} 非法（合法：{sorted(_LEGAL_CATS)}）")
        if "allow_no_exec" in case and not isinstance(case["allow_no_exec"], bool):
            errors.append(f"{where}：allow_no_exec 必须是 bool")
        ctx = case.get("context")
        if ctx is not None:
            if not isinstance(ctx, dict):
                errors.append(f"{where}：context 必须是对象")
            else:
                extra_ctx = set(ctx) - _LEGAL_CONTEXT_FIELDS
                if extra_ctx:
                    errors.append(f"{where}：context 未知字段 {sorted(extra_ctx)}"
                                  f"（合法：{sorted(_LEGAL_CONTEXT_FIELDS)}）")
                if "has_results" in ctx and not isinstance(ctx["has_results"], bool):
                    errors.append(f"{where}：context.has_results 必须是 bool")
                if "result_total" in ctx and type(ctx["result_total"]) is not int:
                    errors.append(f"{where}：context.result_total 必须是 int")
                if "current_query" in ctx and not isinstance(ctx["current_query"], str):
                    errors.append(f"{where}：context.current_query 必须是 str")
                if "current_filters" in ctx and not isinstance(ctx["current_filters"], dict):
                    errors.append(f"{where}：context.current_filters 必须是对象")
        tools = case.get("tools") or {}
        if not isinstance(tools, dict):
            errors.append(f"{where}：tools 必须是对象")
            tools = {}
        for verb, outcome in tools.items():
            if verb not in _TOOL_META:
                errors.append(f"{where}：tools 引用了未登记动词 {verb!r}"
                              f"（合法：{sorted(_TOOL_META)}）")
            else:
                _check_outcome(outcome, f"{where}.tools[{verb}]", errors)
        exp = case.get("expect") or {}
        if not isinstance(exp, dict):
            errors.append(f"{where}：expect 必须是对象")
            exp = {}
        if not exp:
            errors.append(f"{where}：expect 非空——没有断言的用例是恒过通道")
        for key in exp:
            if key not in _LEGAL_EXPECT:
                errors.append(f"{where}：expect 字段 {key!r} 拼写非法"
                              f"（合法：{sorted(_LEGAL_EXPECT)}）")
        first = exp.get("first")
        first_list: list[str] = []
        if "first" in exp:
            if isinstance(first, str) and first:
                first_list = [first]
            elif isinstance(first, list) and first and all(
                    isinstance(v, str) and v for v in first):
                first_list = list(first)
            else:
                errors.append(f"{where}：expect.first 必须是非空字符串或非空字符串数组")
            for v in first_list:
                if v not in _KNOWN_VERBS:
                    errors.append(f"{where}：first 动词 {v!r} 不在 VERB_SPECS 封闭表里")
        if "first_slots" in exp:
            fs = exp["first_slots"]
            if not isinstance(fs, dict):
                errors.append(f"{where}：expect.first_slots 必须是对象")
            else:
                for sk, sv in fs.items():
                    if not isinstance(sk, str):
                        errors.append(f"{where}：first_slots 的键必须是 str")
                    if not (isinstance(sv, str)
                            or (isinstance(sv, list) and all(isinstance(x, str) for x in sv))):
                        errors.append(f"{where}：first_slots[{sk!r}] 必须是 str 或 str 数组")
        for key in ("must_steps", "must_steps_unordered", "forbid_steps"):
            if key in exp:
                if not isinstance(exp[key], list):
                    errors.append(f"{where}：expect.{key} 必须是数组")
                elif key == "forbid_steps":
                    if not exp[key]:
                        errors.append(f"{where}：expect.forbid_steps 空数组——"
                                      f"没有内容的断言是恒过通道（b09 型手误）")
                    for v in exp[key]:
                        if not isinstance(v, str) or v not in _KNOWN_VERBS:
                            errors.append(f"{where}：forbid_steps 动词 {v!r} 非法"
                                          f"（须在 VERB_SPECS 封闭表内）")
                elif not exp[key]:
                    errors.append(f"{where}：expect.{key} 空数组——没有内容的断言是恒过通道")
                else:
                    for i, want in enumerate(exp[key]):
                        _check_step_want(want, f"{where}.expect.{key}[{i}]", errors)
        # must_when（v5.2 高-2 / v5.3 中-3）：[{if_first: 合法动词, must: 非空条目数组}]
        # 同一 if_first 允许多个子句（OR：任一满足即过——db-first 型两种合法形各自
        # 给完整子句），不再要求唯一。
        must_when_clauses: list[str] = []
        if "must_when" in exp:
            mw = exp["must_when"]
            if not isinstance(mw, list) or not mw:
                errors.append(f"{where}：expect.must_when 必须是非空数组")
            elif isinstance(mw, list):
                for i, clause in enumerate(mw):
                    cw = f"{where}.expect.must_when[{i}]"
                    if not isinstance(clause, dict) or set(clause) - {"if_first", "must"}:
                        errors.append(f"{cw}：子句必须是 {{if_first, must}} 对象")
                        continue
                    iv = clause.get("if_first")
                    if not isinstance(iv, str) or iv not in _KNOWN_VERBS:
                        errors.append(f"{cw}：if_first {iv!r} 不是合法动词")
                    else:
                        must_when_clauses.append(iv)
                    cm = clause.get("must")
                    if not isinstance(cm, list) or not cm:
                        errors.append(f"{cw}：must 必须是非空数组")
                    else:
                        for j, want in enumerate(cm):
                            _check_step_want(want, f"{cw}.must[{j}]", errors)
        must_verbs = {_want_verb(w) for w in exp.get("must_steps") or []}
        must_verbs |= {_want_verb(w) for w in exp.get("must_steps_unordered") or []}
        forbid_verbs = set(exp.get("forbid_steps") or [])
        conflict = must_verbs & forbid_verbs
        if conflict:
            errors.append(f"{where}：must_steps 与 forbid_steps 冲突 {sorted(conflict)}")
        for key in ("max_steps", "ideal_steps", "steps_exact"):
            if key in exp and type(exp[key]) is not int:  # 严格 int：拒 bool 拒 "3"
                errors.append(f"{where}：expect.{key} 必须是 int（收到 "
                              f"{type(exp[key]).__name__}）")
        if "steps_exact" in exp and type(exp["steps_exact"]) is int and exp["steps_exact"] < 1:
            errors.append(f"{where}：steps_exact 必须 >= 1（0/负数是恒过通道）")
        if "ideal_steps" in exp and type(exp["ideal_steps"]) is int and exp["ideal_steps"] < 1:
            errors.append(f"{where}：ideal_steps 必须 >= 1")
        if "max_steps" in exp and type(exp["max_steps"]) is int and exp["max_steps"] < 0:
            errors.append(f"{where}：max_steps 必须 >= 0")
        if ("ideal_steps" in exp and "max_steps" in exp
                and type(exp["ideal_steps"]) is int and type(exp["max_steps"]) is int
                and exp["ideal_steps"] > exp["max_steps"]):
            errors.append(f"{where}：ideal_steps({exp['ideal_steps']}) > "
                          f"max_steps({exp['max_steps']})——最优路径不能超出合法上界")
        for key in ("zero_writes", "cancelled", "no_ungrounded", "expect_invalid",
                    "or_invalid"):
            if key in exp and not isinstance(exp[key], bool):
                errors.append(f"{where}：expect.{key} 必须是 bool")
        if exp.get("expect_invalid") and exp.get("or_invalid"):
            errors.append(f"{where}：expect_invalid 与 or_invalid 互斥（单结局 vs 双结局）")
        if (exp.get("or_invalid")
                and not any(k in exp for k in ("must_steps", "must_steps_unordered",
                                               "must_when"))):
            errors.append(f"{where}：or_invalid 正常分支无执行断言（must_steps/"
                          f"must_steps_unordered/must_when 至少其一）——双结局的"
                          f"「正确处理」半边不能是零执行通道")
        for key in ("check_sources", "search_topics"):
            if key in exp:
                if not isinstance(exp[key], list) or not exp[key]:
                    errors.append(f"{where}：expect.{key} 必须是非空数组")
                else:
                    for v in exp[key]:
                        if not isinstance(v, str) or not v.strip():
                            errors.append(f"{where}：expect.{key} 的元素必须是非空 str")
                        elif key == "check_sources" and not ax._norm_source(v):
                            errors.append(f"{where}：check_sources 元素 {v!r} 归一后为空")
        for key in ("report_contains", "report_not_contains"):
            if key in exp:
                if not isinstance(exp[key], list):
                    errors.append(f"{where}：expect.{key} 必须是数组")
                elif not all(isinstance(x, str) for x in exp[key]):
                    errors.append(f"{where}：expect.{key} 的元素必须都是 str")
        # 2026-08-08 v5+ v5.2（/）：first 钉了 tools 提供的 loop 动词
        # → 必须钉执行（must_steps/_unordered 含该 verb，或有 must_when 子句；must_when
        # 在案时 first 提供的**每个** loop 动词都要有子句）；first 含 tools 未提供的
        # loop 动词 → 直接报错（off-script 必败分支不留）。豁免仅 allow_no_exec /
        # expect_invalid / or_invalid（零步设计或护栏结局，没有执行步可钉）。
        exempt = (case.get("allow_no_exec") or exp.get("expect_invalid")
                  or exp.get("or_invalid"))
        if first_list and not exempt:
            loop_in_first = [v for v in first_list if v in _LOOP_VERBS]
            off_script = [v for v in loop_in_first if v not in tools]
            if off_script:
                errors.append(
                    f"{where}：first 含 tools 未提供的 loop 动词 {off_script}——"
                    f"选它必被 on_script 判败（请补 tools 剧本或从 first 剔除；"
                    f"确属设计请显式豁免）")
            provided = [v for v in loop_in_first if v in tools]
            if provided:
                covered = must_verbs | set(must_when_clauses)
                if exp.get("must_when"):
                    no_clause = [v for v in provided if v not in covered]
                    if no_clause:
                        errors.append(
                            f"{where}：first 提供的 loop 动词 {no_clause} 缺 must_when "
                            f"子句——该路径零断言（每条合法路径都要有自己的钉法）")
                elif isinstance(first, list):
                    if not any(v in covered for v in provided):
                        errors.append(
                            f"{where}：first 含 tools 提供的 loop 动词 {provided}，"
                            f"must_steps/must_steps_unordered/must_when 至少钉住其一"
                            f"（多解路径用例请用 must_when 或显式 \"allow_no_exec\": true）")
                elif provided[0] not in covered:
                    errors.append(
                        f"{where}：first 钉了 tools 提供的 loop 动词 {provided[0]!r}，"
                        f"must_steps/must_steps_unordered/must_when 必须包含该 verb"
                        f"（只测路由不测执行是恒过通道；确属零步设计请显式 "
                        f"\"allow_no_exec\": true）")
        cases.append(case)
    if errors:
        print("用例集启动自检失败（逐行修复后重跑）：", file=sys.stderr)
        for msg in errors:
            print(f"  - {msg}", file=sys.stderr)
        raise SystemExit(2)
    return cases


# --------------------------------------------------------------------------- 断言引擎

#: 不计入「旧维口径」总分的新维（v4 三个 + v5 五个 + v5.2 三个 + v6 一个）——旧维集合不变 =
#: 口径没变，变的是又加了几把尺；v3 参照走 _V3_DIMS 的 v3 子集口径（更严）。
_NEW_STRICT_DIMS = ("report_covers", "chain_complete", "ideal_steps",
                    "on_script", "steps_exact", "guard_intercept", "no_exception",
                    "must_steps_unordered", "must_when", "check_sources", "search_topics",
                    "number_grounded")

#: v3 原始 89 条用例 id（出处：`eval/agent_live_run_v3.jsonl` 的用例集——v4 扩容
#: 新增 a21 / i 类 / j 类 / k 类 / l 类之前的原始集合），用于「v3 子集复测分」。
_V3_IDS = frozenset(
    "a01 a02 a03 a04 a05 a06 a07 a08 a09 a10 a11 a12 a13 a14 a15 a16 a17 a18 a19 a20 "
    "b01 b02 b03 b04 b05 b06 b07 b08 b09 b10 b11 b12 b13 b14 b15 "
    "c01 c02 c03 c04 c05 "
    "d01 d02 d03 d04 d05 d06 d07 d08 d09 d10 d11 d12 d13 d14 d15 "
    "e01 e02 e03 e04 e05 e06 e07 e08 e09 e10 e11 e12 "
    "f01 f02 f03 f04 f05 f06 f07 f08 "
    "g01 g02 g03 g04 g05 g06 "
    "h01 h02 h03 h04 h05 h06 h07 h08".split())

#: v3 时代的计分维度集（出处：`eval/agent_live_report_v3.md` 的「维度得分」节，
#: 共 10 维）——v3 子集复测分只统计这些维，才能与 84/89 (94.4%) 横比。
_V3_DIMS = frozenset({
    "cancelled", "faithful", "first", "forbid_steps", "max_steps", "must_steps",
    "no_ungrounded", "report_contains", "report_not_contains", "zero_writes",
})

#: expect_invalid/or_invalid 口径里的「护栏拒收」判定（2026-08-08 收窄）：
#: 只认 `AgentPlanInvalid`（isinstance 判定，见 `_execute_case`）——AgentError 基类的
#: 其余成员（AgentUnavailable 等）是基础设施异常，**不算**护栏拦截。字符串类名只在
#: selftest 合成场景里作展示，判定一律走 isinstance 传入的 exc_guard 标志。


def _steps_of(plan: dict) -> list[dict]:
    return list(plan.get("steps") or [])


def _one_of(value, expected) -> bool:
    if isinstance(expected, list):
        return value in expected
    return value == expected


def _step_matches(step: dict, want) -> bool:
    """must 条目匹配：字符串形只比动词；对象形 verb + 可选 source（别名组等价）
    / keywords（大小写不敏感子串）。"""
    if isinstance(want, str):
        return str(step.get("verb") or "") == want
    if str(step.get("verb") or "") != str(want.get("verb") or ""):
        return False
    slots = step.get("slots") or {}
    if want.get("source") is not None and not _source_equiv(slots.get("source"), want["source"]):
        return False
    if want.get("keywords") is not None:
        if str(want["keywords"]).lower() not in str(slots.get("keywords") or "").lower():
            return False
    return True


def _match_ordered(steps: list[dict], wants: list) -> list[int]:
    """按序子序列匹配，返回命中步骤的下标（len < len(wants) = 没钉全）。"""
    idx, matched = 0, []
    for want in wants:
        while idx < len(steps) and not _step_matches(steps[idx], want):
            idx += 1
        if idx < len(steps):
            matched.append(idx)
            idx += 1
    return matched


def _match_unordered(steps: list[dict], wants: list) -> tuple[list[int], list]:
    """集合存在性匹配：每个 want 消耗一个不同的步骤；返回 (命中下标, 未命中 want 清单)。"""
    remaining = list(range(len(steps)))
    matched: list[int] = []
    missing: list = []
    for want in wants:
        found = next((i for i in remaining if _step_matches(steps[i], want)), None)
        if found is None:
            missing.append(want)
        else:
            remaining.remove(found)
            matched.append(found)
    return matched, missing


def _script_expected_ok(tools: dict, verb: str, slots: dict) -> "bool | None":
    """剧本隐含期望：从用例 tools 剧本推导该步应有的 ok——
    按 step 的 slots.source（`_norm_source` 口径，与 `_outcome_to_run` 同一张查表逻辑）
    查 by_source/default：raise/malformed/未登记来源（raise _ToolBoom 的分支）期望
    ok=False；const 期望 ok=True；tools 没提供该动词 → None（剧本确定不了，不判）。"""
    outcome = (tools or {}).get(verb)
    if not isinstance(outcome, dict):
        return None
    if "by_source" in outcome:
        key = ax._norm_source((slots or {}).get("source"))
        picked = outcome["by_source"].get(key, outcome.get("default"))
        if picked is None:
            return False  # 与 _outcome_to_run 同路：未登记来源 → source_not_registered
        outcome = picked
    if outcome.get("malformed") or "raise" in outcome:
        return False
    if "const" in outcome:
        return True
    return None


def _script_verdicts(tools: dict, steps: list[dict], matched: list[int],
                     step_verbs: list[str]) -> tuple[bool, list[str]]:
    """matched 步的**剧本 ok 核对**：每个命中步的 ok 必须与
    剧本隐含期望一致（const 该 True、raise/malformed/未登记来源该 False；剧本确定
    不了的步不判）。单步也核——a01 型（const 却 ok=False）与 b11/f04/g01 型
    （剧本失败却 ok=True）都被咬住。返回 (全部一致?, 人读核对清单)。"""
    verdicts: list[str] = []
    all_ok = True
    for i in matched:
        expected = _script_expected_ok(tools, step_verbs[i], steps[i].get("slots") or {})
        if expected is None:
            continue
        actual = bool(steps[i].get("ok"))
        verdicts.append(f"{step_verbs[i]}:ok={actual}/expect={expected}")
        if actual != expected:
            all_ok = False
    return all_ok, verdicts


def _want_text(want) -> str:
    return want if isinstance(want, str) else json.dumps(want, ensure_ascii=False)


# ---- report_covers 数字匹配加固----

#: 中文小写数字等价（1-10；2 收「二」「两」）；0 的豁免走 _ZERO_PHRASES。
_CN_NUMERALS: dict[int, tuple[str, ...]] = {
    1: ("一",), 2: ("二", "两"), 3: ("三",), 4: ("四",), 5: ("五",),
    6: ("六",), 7: ("七",), 8: ("八",), 9: ("九",), 10: ("十",),
}
_ZERO_PHRASES: tuple[str, ...] = ("零", "没有", "无")
#: new_count/imported_total == 0 的否定措辞（check/sync 共用——同步零新增同 check 口径）
_NEG_CHECK_PHRASES: tuple[str, ...] = (
    "没有新增", "无新增", "没有疑似新增", "无疑似新增", "没有新数据", "无新数据",
    "未发现新增", "没有新的",
)
#: record_count == 0 的否定措辞
_NEG_SEARCH_PHRASES: tuple[str, ...] = (
    "没搜到", "没有搜到", "没找到", "没有找到", "未搜到", "未找到", "没有结果", "无结果",
)
#: label+否定语境的否定词（在原始汇报全文里找语境，不消费游标）
_NEG_CONTEXT_WORDS: tuple[str, ...] = ("没有", "无", "未", "零")


def _num_re(n: int) -> re.Pattern:
    """边界安全数字正则：前后不粘数字/小数点/千分位逗号（「47」不许命中「4,756」）。
    0 额外排除 `-` `:` `/` 邻接——快照日期/时间里的「08」「00」
    不许被当成数字 0 命中。"""
    extra = ":/-" if n == 0 else ""
    return re.compile(rf"(?<![0-9.,{extra}]){n}(?![0-9.,{extra}])")


class _CoverCursor:
    """report_covers 的位置游标：先剥离千分位逗号，按步序找命中位置，
    已消费的位置不再复用（两次 record_count=2 的搜索，一个「2」盖不住两次）。"""

    def __init__(self, report: str):
        self.text = re.sub(r"(?<=\d),(?=\d)", "", report)
        self.pos = 0

    def hit(self, *candidates: str) -> bool:
        """任一候选子串命中即消费（游标前进到命中末尾；取最早命中）。
        游标未命中时从 0 起**不消费地**全局再找一次（2026-08-08 v5.2 中-6b：
        保守放行顺序倒置的诚实汇报）。如实登记的风险：同值多步复用同一处措辞
        不再被拦——拿这个假阳性换顺序假阴性，是刻意的取舍。"""
        best: "tuple[int, int] | None" = None
        for cand in candidates:
            if not cand:
                continue
            idx = self.text.find(cand, self.pos)
            if idx >= 0 and (best is None or idx < best[0]):
                best = (idx, idx + len(cand))
        if best is not None:
            self.pos = best[1]
            return True
        return any(bool(cand) and self.text.find(cand, 0) >= 0 for cand in candidates)

    def hit_number(self, n: int, *, _bare_zero_ok: bool = True) -> bool:
        """数字命中：边界安全正则；1-10 接受中文小写数字；0 接受 零（/没有/无）豁免。
        **取正则与措辞候选的最早命中消费**（2026-08-08 v5 画廊实证：正则先行会跳到
        报告尾部的「外部文件0」，把中间的「4756」甩在游标后造成误判缺失）。
        `_bare_zero_ok=False`（check/sync 零值专用，v5.2 中-6a）：0 只认 0/零，
        裸「没有/无」太松——「网络没有异常」不许覆盖 new_count=0。
        游标未命中时同样从 0 起不消费地全局再找一次（中-6b，风险见 hit）。"""
        n = int(n)
        best: "tuple[int, int] | None" = None
        m = _num_re(n).search(self.text, self.pos)
        if m:
            best = (m.start(), m.end())
        if n == 0:
            candidates: tuple[str, ...] = ("零",) if not _bare_zero_ok else _ZERO_PHRASES
        else:
            candidates = _CN_NUMERALS.get(n, ())
        for cand in candidates:
            if not cand:
                continue
            idx = self.text.find(cand, self.pos)
            if idx >= 0 and (best is None or idx < best[0]):
                best = (idx, idx + len(cand))
        if best is not None:
            self.pos = best[1]
            return True
        if _num_re(n).search(self.text, 0):
            return True
        return any(bool(cand) and self.text.find(cand, 0) >= 0 for cand in candidates)


def _clause_span(text: str, idx: int, breaks: tuple[str, ...] | None = None) -> str:
    """text 中 idx 所在的**小句**（默认按 `agent_exec._CLAUSE_BREAKS_ZH` 隔断切句）。"""
    parts = breaks if breaks is not None else ax._CLAUSE_BREAKS_ZH
    start = max(text.rfind(p, 0, idx) for p in parts) + 1
    ends = [text.find(p, idx) for p in parts if text.find(p, idx) >= 0]
    end = min(ends) if ends else len(text)
    return text[start:end]


#: 零值同小句判定的隔断表：`_CLAUSE_BREAKS_ZH` 去掉「，」「：」
#: ——「ArrayExpress 检查完成，没有新增」「ArrayExpress：没有新增」是标准句形，逗号/
#: 冒号当隔断会把它们误判缺。代价（如实登记）：逗号/冒号连接的跨小句借否定仍会误盖。
_ZERO_CLAUSE_BREAKS: tuple[str, ...] = ("。", "；", "！", "？", "\n")


def _report_covers_misses(steps: list[dict], report: str) -> list[str]:
    """按步序核对每个 ok 步的关键事实在 report_zh 中出现（缺失清单，空 = 全盖到）。
    数字 >0 必须数字命中（label 不再顶替）；==0 全部收进**同小句**纪律
    （2026-08-08 v5.2 中-6a + v5.3 中-4）：0/零、固定否定短语、label 后备的命中
    位置都必须落在该源 label 所在小句。游标语义见 _CoverCursor（含中-6b 全局赦免）。"""
    cur = _CoverCursor(report)
    misses: list[str] = []

    def zero_covered(label: str, all_labels: list[str]) -> bool:
        """零值覆盖（v5.3 中-4）：同小句纪律——该源 label 的小句内含零值信号
        （边界安全 0 / 零 / 固定否定短语 / 否定词）才算盖到；label 空串或不在汇报里
        时退到本结果任一源 label 的小句；一个都找不到 → 记缺。
        残余近似如实登记：不点名来源的诚实零值汇报（「检查完成，没有新增。」）从此
        计缺——拿这个假阴性换「网络没有异常；ArrayExpress 检查完成。」式跨小句
        借否定。
        2026-08-07 续批：同一 label 的**所有出现处**都参与判定（此前只查首现小句，
        「库容清单里枚举过 ArrayExpress 1784 条、后文再述其检查结果 0 新增」的合法
        句形被误判缺）；命中才消费游标，未命中不消费。"""
        anchors = ([label] if label else []) + [lb for lb in all_labels if lb and lb != label]
        for anchor in anchors:
            for from_pos, consume in ((cur.pos, True), (0, False)):  # 中-6b：先游标后全局
                idx = cur.text.find(anchor, from_pos)
                while idx >= 0:
                    clause = _clause_span(cur.text, idx, _ZERO_CLAUSE_BREAKS)
                    if (_num_re(0).search(clause) or any(p in clause for p in _NEG_CHECK_PHRASES)
                            or any(w in clause for w in _NEG_CONTEXT_WORDS)):
                        if consume:
                            cur.pos = idx + len(anchor)
                        return True
                    idx = cur.text.find(anchor, idx + len(anchor))
        return False

    for s in steps:
        if not s.get("ok"):
            continue
        result = s.get("result")
        if not isinstance(result, dict):
            continue
        verb = s.get("verb")
        try:
            if verb == "curate.search_online":
                rc = result.get("record_count")
                if rc is None:
                    continue
                if int(rc) > 0:
                    if not cur.hit_number(int(rc)):
                        misses.append(f"search_online.record_count={rc}")
                elif not (cur.hit_number(0) or cur.hit(*_NEG_SEARCH_PHRASES)):
                    misses.append("search_online.record_count=0（缺「没搜到」类否定措辞）")
            elif verb == "curate.check_updates":
                all_labels = [str(x.get("label") or "")
                              for x in result.get("sources") or [] if isinstance(x, dict)]
                for src in result.get("sources") or []:
                    if not isinstance(src, dict) or src.get("mode") != "online":
                        continue
                    nc = src.get("new_count")
                    if nc is None:
                        continue
                    label = str(src.get("label") or "")
                    if int(nc) > 0:
                        if not cur.hit_number(int(nc)):
                            misses.append(
                                f"check_updates[{label or src.get('source')}].new_count={nc}")
                    elif not zero_covered(label, all_labels):
                        misses.append(
                            f"check_updates[{label or src.get('source')}].new_count=0"
                            f"（源 label 同小句内缺零值信号）")
            elif verb == "curate.sync_updates":
                total = result.get("imported_total")
                if total is None:
                    continue
                labels = [str(x.get("label") or "")
                          for x in result.get("sources") or [] if isinstance(x, dict)]
                if int(total) > 0:
                    if not cur.hit_number(int(total)):
                        misses.append(f"sync_updates.imported_total={total}")
                elif not zero_covered(labels[0] if labels else "", labels):
                    misses.append("sync_updates.imported_total=0（源 label 同小句内缺零值信号）")
            elif verb == "curate.db_status":
                tr = result.get("total_records")
                if tr is not None and not cur.hit_number(int(tr)):
                    misses.append(f"db_status.total_records={tr}")
        except (TypeError, ValueError):
            continue  # 形状意外 → 保守放行（形状闸已在执行侧记过 ok=False）
    return misses


# ---- number_grounded 汇报数字出处不变量----

_INT_RE = re.compile(r"\d+")


def _number_grounded_misses(steps: list[dict], report: str) -> list[str]:
    """汇报整数的出处核对（无出处清单，空 = 全部有出处）。
    提取汇报里的全部整数（先剥千分位逗号），每个数字必须能在 steps 的工具返回
    JSON（每步 result 的序列化文本）中作为**数值相等**出现——词边界正则
    `(?<![0-9])N(?![0-9])`，「2」不许在「12」里命中；前导零写法按数值等价
    （「08」≡ 8）。豁免（防误伤）：① ≤9 且紧邻「步」字（「3 步」步骤序号）；
    ② \d+% 百分比 / 20\d\d年 年份 / \d{4}-\d{2} 日期头。steps 为空或 report
    为空时本维不参评（score_case 处闸，与「条件不满足不计入分母」惯例一致）。"""
    text = re.sub(r"(?<=\d),(?=\d)", "", report)
    blob = re.sub(r"(?<=\d),(?=\d)", "", json.dumps(
        [s.get("result") for s in steps if isinstance(s.get("result"), (dict, list))],
        ensure_ascii=False))
    # ③ 零值豁免的判定素材（v17aug 实战误伤驱动）：result 里有空容器（[] / {}）或显式
    # 零值（": 0）时，汇报里的「0」有诚实出处形态（「外部库 0 个文件」对应
    # "external_files": []——空数组没有字面 0）。放松面在案：真编造的 0 与同存空容器
    # 时会漏拦——0 的语义天然与「空」纠缠，条数级幻觉由 faithful/count_mismatch 闸兜。
    zero_grounded = bool(re.search(r"\[\]|\{\}|[\":]\s*0(?![0-9])", blob))
    misses: list[str] = []
    seen: set[str] = set()
    for m in _INT_RE.finditer(text):
        tok, tail = m.group(), text[m.end():]
        if int(tok) <= 9 and re.match(r"\s*步", tail):
            continue  # ① 步骤序号（「3 步」「3步」）
        if tail.startswith("%"):
            continue  # ② 百分比
        if len(tok) == 4 and tok.startswith("20") and re.match(r"\s*年", tail):
            continue  # ② 年份（2026 年）
        if len(tok) == 4 and re.match(r"-\d{2}", tail):
            continue  # ② 日期头（2026-08-…）
        if int(tok) == 0 and zero_grounded:
            continue  # ③ 空容器/显式零值在场的「0」（v17aug 误伤主体，2026-08-08）
        if tok in seen:
            continue
        seen.add(tok)
        if not any(re.search(rf"(?<![0-9]){c}(?![0-9])", blob)
                   for c in {tok, str(int(tok))}):
            misses.append(tok)
    return misses


def score_case(case: dict, plan: dict, trace: list[dict], exc_kind: str = "",
               exc_guard: bool = False) -> list[dict]:
    """逐项软断言：返回 [{'dim', 'ok', 'detail'}]。任一维度失败只记该维度。
    exc_kind 非空 = plan_with_agent 抛了异常（类名只作展示）；exc_guard =
    isinstance(err, ax.AgentPlanInvalid)（护栏拒收判定，v5.2 中-8：只认它）。"""
    exp = case.get("expect") or {}
    tools = case.get("tools") or {}
    steps = _steps_of(plan)
    step_verbs = [str(s.get("verb") or "") for s in steps]
    report = str(plan.get("report_zh") or "")
    out: list[dict] = []

    def add(dim, ok, detail=""):
        out.append({"dim": dim, "ok": bool(ok), "detail": detail})

    # ---- 异常路径（2026-08-08 收窄 + 双结局）----
    if exc_kind:
        if exp.get("expect_invalid") or exp.get("or_invalid"):
            # 护栏拒收是（唯一/之一）设计正确结局：AgentPlanInvalid 且零成功写步 → 计过
            writes = [s["verb"] for s in steps if not s.get("readonly") and s.get("ok")]
            ok = exc_guard and not writes
            add("guard_intercept", ok,
                f"exc={exc_kind} 护栏型={exc_guard} 成功写步={writes}")
            if not ok:
                add("no_exception", False, f"非护栏异常 {exc_kind}")
        else:
            # 非预期 exc：伪维 no_exception=False——exc 轮次 checks 不再为空，维度统计不虚高
            add("no_exception", False, f"意外异常 {exc_kind}")
        return out
    # v5.2 低-9：正常轮次补记 no_exception=True——维度级抖动清单才能看到异常维翻转
    add("no_exception", True, "")
    if exp.get("expect_invalid"):
        add("guard_intercept", False, "护栏该拦没拦（本例设计结局 = AgentPlanInvalid）")
        return out
    # or_invalid 且无异常：双结局的另一半——按正常 expect 块照常评分（落下面全维度）

    if "first" in exp:
        add("first", _one_of(plan.get("verb"), exp["first"]),
            f"verb={plan.get('verb')} expect={exp['first']}")
    if "first_slots" in exp:
        # 首步 slots 软断言：嵌套 dict 拍平一层（dotted key）；路由类动词
        # （refine.conditions/search.new）没有声明槽位、主题词落在 effective_query——
        # 折成同名伪槽位参与匹配。
        slots_view: dict = {}
        for k, v in (plan.get("slots") or {}).items():
            if isinstance(v, dict):
                for sk, sv in v.items():
                    slots_view[f"{k}.{sk}"] = sv
            else:
                slots_view[k] = v
        if plan.get("effective_query"):
            slots_view["effective_query"] = plan["effective_query"]
        slot_miss = []
        for name, accepts in (exp["first_slots"] or {}).items():
            opts = accepts if isinstance(accepts, list) else [accepts]
            hay = str(slots_view.get(name) or "").lower()
            if not any(str(a).lower() in hay for a in opts):
                slot_miss.append(f"{name}={slots_view.get(name)!r}∌{opts}")
        add("first_slots", not slot_miss,
            f"slots={slots_view} miss={slot_miss}")
    # on_script：plan.verb / steps 里出现 tools 外的 loop 动词
    # → False（off-script 空执行此前是恒过通道）。全程没碰 loop 动词则不参评。
    loop_used = ([plan.get("verb")] if str(plan.get("verb") or "") in _LOOP_VERBS else [])
    loop_used += [s.get("verb") for s in steps if str(s.get("verb") or "") in _LOOP_VERBS]
    if loop_used:
        off = [v for v in loop_used if v not in tools]
        add("on_script", not off, f"loop 动词使用={loop_used} tools 外={off}")
    chain_done = False
    if "must_steps" in exp:
        wants = exp["must_steps"]
        matched = _match_ordered(steps, wants)
        missing = [_want_text(w) for w in wants[len(matched):]]
        # v5.3 高-2：命中的**每个**步过剧本 ok 核对（单步也核），作为 must 匹配的
        # 一部分而非独立维——a01 型（const 却 ok=False）与 b11/f04/g01 型（剧本失败
        # 却 ok=True）从此都被咬住。
        script_ok, verdicts = _script_verdicts(tools, steps, matched, step_verbs)
        add("must_steps", not missing and script_ok,
            f"steps={step_verbs} missing={missing} 剧本核对=[{'; '.join(verdicts)}]")
        # chain_complete（v5 升级，剧本隐含 ok 口径）：≥2 个 must_steps 时自动参评——
        # 子序列命中且剧本核对全一致。
        if len(wants) >= 2:
            add("chain_complete", len(matched) == len(wants) and script_ok,
                f"steps={step_verbs} matched={matched} 剧本核对=[{'; '.join(verdicts)}]")
            chain_done = True
    if "must_steps_unordered" in exp:
        wants = exp["must_steps_unordered"]
        matched, missing_wants = _match_unordered(steps, wants)
        missing = [_want_text(w) for w in missing_wants]
        add("must_steps_unordered", not missing,
            f"steps={step_verbs} missing={missing}")
        if not chain_done and len(wants) >= 2:
            script_ok, verdicts = _script_verdicts(tools, steps, matched, step_verbs)
            add("chain_complete", len(matched) == len(wants) and script_ok,
                f"steps={step_verbs} matched={sorted(matched)} 剧本核对=[{'; '.join(verdicts)}]")
            chain_done = True
    if "must_when" in exp:
        # 路径条件断言：按 plan.verb 选子句，命中
        # 子句的 must 按 must_steps 同判定（按序子序列 + 剧本 ok 核对）；**同一
        # if_first 允许多个子句（OR）——任一满足即过**（db-first 型两种合法形各自
        # 给完整子句）。无子句匹配（none/路由类零步结局）→ 空过不计败。
        verb0 = str(plan.get("verb") or "")
        clauses = [c for c in exp["must_when"] if c.get("if_first") == verb0]
        if not clauses:
            add("must_when", True, f"首步 {verb0!r} 无路径子句（零步/路由结局不钉步）")
        else:
            best = None  # (ok, wants, detail)；任一子句满足即过
            for clause in clauses:
                wants = clause["must"]
                matched = _match_ordered(steps, wants)
                missing = [_want_text(w) for w in wants[len(matched):]]
                script_ok, verdicts = _script_verdicts(tools, steps, matched, step_verbs)
                ok = not missing and script_ok
                detail = f"missing={missing} 剧本核对=[{'; '.join(verdicts)}]"
                if best is None or ok:
                    best = (ok, wants, detail)
                if ok:
                    break
            add("must_when", best[0],
                f"路径={verb0}（{len(clauses)} 子句 OR）steps={step_verbs} {best[2]}")
            if not chain_done and len(best[1]) >= 2:
                add("chain_complete", best[0],
                    f"路径={verb0} steps={step_verbs} {best[2]}")
                chain_done = True
    if "check_sources" in exp:
        # 覆盖维：所有 ok check_updates 步的 slots.source
        # 并集（`_source_equiv` 别名组等价）必须覆盖清单——只钉动词不钉来源的通道清零。
        covered_srcs = [str((s.get("slots") or {}).get("source") or "")
                        for s in steps
                        if s.get("verb") == "curate.check_updates" and s.get("ok")]
        src_miss = [want for want in exp["check_sources"]
                    if not any(_source_equiv(have, want) for have in covered_srcs)]
        add("check_sources", not src_miss,
            f"ok check 源={covered_srcs} 缺={src_miss}")
    if "search_topics" in exp:
        # 覆盖维：所有 ok search_online 步的 keywords 并集
        # （大小写不敏感拼接）必须逐串含各主题子串——空关键词/漏主题计败。
        blob = " ".join(str((s.get("slots") or {}).get("keywords") or "")
                        for s in steps
                        if s.get("verb") == "curate.search_online" and s.get("ok")).lower()
        topic_miss = [t for t in exp["search_topics"] if str(t).lower() not in blob]
        add("search_topics", not topic_miss,
            f"ok search 关键词={blob!r} 缺={topic_miss}")
    if "forbid_steps" in exp:
        hit = [v for v in step_verbs if v in exp["forbid_steps"]]
        add("forbid_steps", not hit, f"steps={step_verbs} hit={hit}")
    if "max_steps" in exp:
        add("max_steps", len(steps) <= int(exp["max_steps"]),
            f"steps={len(steps)} max={exp['max_steps']}")
    if "ideal_steps" in exp:
        # 最优路径步数上界：超 ideal 但低于 max = 浪费动作（白跑一次工具），单独记该维
        add("ideal_steps", len(steps) <= int(exp["ideal_steps"]),
            f"steps={len(steps)} ideal={exp['ideal_steps']}")
    if "steps_exact" in exp:
        # 截断类用例的钉法（生产上限 MAX_STEPS=8，写 max_steps 恒过无信息量）
        add("steps_exact", len(steps) == int(exp["steps_exact"]),
            f"steps={len(steps)} exact={exp['steps_exact']}")
    if exp.get("zero_writes"):
        # 「零写入」的诚实口径 = 没有**成功**的写步（失败写步没写进任何东西，不算破戒）
        writes = [s["verb"] for s in steps if not s.get("readonly") and s.get("ok")]
        add("zero_writes", not writes, f"成功写步={writes}")
    if "cancelled" in exp:
        add("cancelled", bool(plan.get("cancelled")) == bool(exp["cancelled"]),
            f"cancelled={plan.get('cancelled')}")
    if exp.get("no_ungrounded"):
        grounding = str(case.get("utterance") or "")
        bad_all: list[str] = []
        texts = ax._step_grounding_texts(steps)
        if texts:
            grounding += "\n" + "\n".join(texts)
        for s in steps:
            if s.get("verb") == "curate.search_online" and s.get("ok"):
                bad_all += ax._ungrounded_keyword_tokens(
                    (s.get("slots") or {}).get("keywords"), grounding)
        add("no_ungrounded", not bad_all, f"无出处 token={bad_all}")
    for sub in exp.get("report_contains") or []:
        add("report_contains", sub in report, f"缺「{sub}」")
    for sub in exp.get("report_not_contains") or []:
        add("report_not_contains", sub not in report, f"多「{sub}」")
    # 护栏拦截事件（性能信号，不计分）：quoted 违规 / 接地违规 / 矛盾后检
    guard_events = []
    for entry in trace or []:
        if entry.get("node") == "validate" and not entry.get("ok"):
            guard_events.append("validate:" + str(entry.get("detail"))[:60])
        if entry.get("discard_reason"):
            guard_events.append("discard:" + str(entry.get("discard_reason")))
    if guard_events:
        out.append({"dim": "_guard_events", "ok": True, "detail": "; ".join(guard_events)})
    # 汇报忠实度机械后检（信号项：仅 LLM 汇报时参与计分——确定性兜底天然一致）
    if plan.get("report_source") == "llm" and steps:
        reason = ax._report_contradiction_reason(report, steps)
        add("faithful", reason is None, f"contradiction={reason}")
        # report_covers（汇报忠实乘性门的机械近似）：v5 加固版——边界安全数字正则 +
        # 中文小写数字 + 零值否定豁免 + 位置游标，见 _report_covers_misses。保守近似，
        # 误伤只拉低该维不冤枉事实（faithful 维才是矛盾判定的主闸）。
        misses = _report_covers_misses(steps, report)
        add("report_covers", not misses, f"report 缺关键事实={misses}")
    # number_grounded（2026-08-08 汇报数字出处不变量）：steps 非空且汇报非空时
    # 参评——汇报里每个整数都要能在 steps 的工具返回 JSON 里数值相等命中；豁免规则
    # 见 _number_grounded_misses。 deterministic 兜底汇报同样参评（事实直填天然该过，
    # 过不了说明兜底模板串了数字——同样是真信号）。
    if steps and report:
        ungrounded = _number_grounded_misses(steps, report)
        add("number_grounded", not ungrounded, f"无出处数字={ungrounded}")
    return out


# --------------------------------------------------------------------------- 主流程

#: 失败聚类的概括文案模板（按首败维度模板化；未知名维度原样显示维度名）
_CLUSTER_SUMMARY: dict[str, str] = {
    "chain_complete": "链没走完",
    "must_steps": "少跑了步",
    "report_contains": "汇报缺内容",
    "faithful": "汇报与实录矛盾",
    "first": "首步判错",
}


def _cluster_failures(fails: list[dict]) -> list[tuple[tuple[str, str, str], list[str], str]]:
    """失败聚类：把失败记录按 (首败维度, 失败发生的首个 agent 节点,
    verb) 三元组聚簇，返回 [(三元组, 用例 id 列表, 人读概括)]，按簇大小降序（同大
    按三元组字典序）。首败维度 = checks 里第一个 ok=false 的计分维（`_` 前缀信号维
    不算）；首个 agent 节点 = trace 里第一个 ok=false 的节点名（没有则 "—"）。
    目的：让报告读者一眼看出「这批失败是同一类还是各有各的病」。"""
    clusters: dict[tuple[str, str, str], list[str]] = {}
    for r in fails:
        dim = next((c["dim"] for c in r.get("checks") or []
                    if not c["ok"] and not str(c["dim"]).startswith("_")), "—")
        node = next((str(t.get("node")) for t in r.get("trace") or []
                     if t.get("ok") is False), "—")
        key = (dim, node, str(r.get("verb") or "—"))
        clusters.setdefault(key, []).append(str(r.get("id")))
    out = [(key, ids, _CLUSTER_SUMMARY.get(key[0], key[0]))
           for key, ids in clusters.items()]
    out.sort(key=lambda item: (-len(item[1]), item[0]))
    return out


def _channel_of(trace: list[dict]) -> str:
    for entry in trace or []:
        if entry.get("node") == "understand":
            detail = str(entry.get("detail") or "")
            if "自动档" in detail:
                return "tools(auto)"
            if "工具调用模式" in detail:
                return "tools"
            if "内容 JSON 模式" in detail:
                return "json"
    return "?"


def _git_commit() -> str:
    """当前仓库 commit（subprocess git rev-parse --short HEAD；拿不到写 unknown）。"""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="eval/agent_live_cases_v1.jsonl")
    ap.add_argument("--tag", default="v1")
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--repeat", type=int, default=1,
                    help="每条用例连跑 K 次（默认 1 行为不变）；报告出 pass^k 总览与不稳定清单")
    ap.add_argument("--selftest", action="store_true",
                    help="离线自验（零 API）：启动自检 + 全维度双向合成断言 + 坏样本行号断言")
    args = ap.parse_args()

    if args.repeat < 1:  # 2026-08-08 v5：pass^k 的 K 必须 >=1，拒绝静默空跑
        print(f"--repeat 必须 >= 1（收到 {args.repeat}）", file=sys.stderr)
        return 2
    if args.selftest:
        return _run_selftest(ROOT / args.cases)

    cases = load_cases(ROOT / args.cases)  # 启动自检不过 → 中文报错行号 + SystemExit(2)
    if args.only:
        cases = [c for c in cases if args.only in c["id"] or args.only in c["cat"]]
    if args.limit is not None:  # default=None：None 不切片；显式 --limit 0 = 空集健康检查
        cases = cases[: args.limit]
    if not cases:
        # 空集路径（--limit 0 / --only 未命中）：只验证加载与自检，**不落任何产物文件**——
        # 2026-08-08 两次事故：默认 tag=v1 的空跑/误跑覆写了历史基线产物。
        print("用例集为空（健康检查路径），不写产物文件。")
        return 0
    cfg = load_llm_config()
    print(f"model={getattr(cfg, 'model', '?')} cases={len(cases)} repeat={args.repeat}")

    run_path = ROOT / "eval" / f"agent_live_run_{args.tag}.jsonl"
    report_path = ROOT / "eval" / f"agent_live_report_{args.tag}.md"
    old_tools, old_root = ax.LOOP_TOOLS, ax._agent_project_root
    records: list[dict] = []
    t0 = time.monotonic()

    def _execute_case(case: dict, round_no: int) -> dict:
        """单用例单轮：独立沙箱跑一条，返回原始记录（round 字段标识轮次）。"""
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            ax.LOOP_TOOLS = build_tools(case.get("tools") or {})
            ax._agent_project_root = lambda: Path(tmp)
            try:
                # 可选屏上语境（缺省保持「无结果」现状，旧用例行为逐位不变）
                case_ctx = case.get("context") or {}
                plan, trace = ax.plan_with_agent(
                    str(case["utterance"]),
                    has_results=bool(case_ctx.get("has_results", False)),
                    result_total=int(case_ctx.get("result_total") or 0),
                    config=cfg, retrieval=None,
                    current_query=str(case_ctx.get("current_query") or ""),
                    current_filters=case_ctx.get("current_filters"))
                exc, exc_kind, exc_guard = "", "", False
            except Exception as err:  # noqa: BLE001
                plan, trace = {}, []
                exc = f"{type(err).__name__}: {str(err)[:160]}"
                exc_kind = type(err).__name__
                # 护栏拒收判定（v5.2 中-8）：isinstance 只认 AgentPlanInvalid——
                # AgentError 基类其余成员（AgentUnavailable 等）是基础设施异常，不算
                exc_guard = isinstance(err, ax.AgentPlanInvalid)
            ledger = []
            led = Path(tmp) / ".userdata" / "curate_net_ledger.jsonl"
            if led.is_file():
                ledger = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines()
                          if x.strip()]
        ms = int((time.monotonic() - started) * 1000)
        checks = score_case(case, plan, trace, exc_kind=exc_kind, exc_guard=exc_guard)
        if not exc and plan.get("report_source") == "deterministic":
            # fallback 率信号（2026-08-08 不计分）：确定性兜底汇报逐例留痕
            checks.append({"dim": "_report_fallback", "ok": True,
                           "detail": "LLM 汇报没接上，按确定事实兜底"})
        scored = [c for c in checks if not c["dim"].startswith("_")]
        # 旧口径（剔除 v4/v5 新维）/ v3 子集口径（只用 v3 时代维度）/ 严格分 = 全部计分维
        old = [c["ok"] for c in scored if c["dim"] not in _NEW_STRICT_DIMS]
        v3 = [c["ok"] for c in scored if c["dim"] in _V3_DIMS]
        return {"id": case["id"], "cat": case["cat"], "round": round_no,
                "utterance": case["utterance"],
                "verb": plan.get("verb"), "steps": [s.get("verb") for s in _steps_of(plan)],
                "step_ok": [s.get("ok") for s in _steps_of(plan)],
                "report_source": plan.get("report_source"), "report_zh": plan.get("report_zh"),
                "channel": _channel_of(trace), "ms": ms, "exc": exc,
                "checks": checks,
                "passed": bool(scored) and all(c["ok"] for c in scored),
                "passed_old": bool(old) and all(old),
                "passed_v3": bool(v3) and all(v3),
                "trace": trace, "plan": plan, "ledger": ledger}

    with run_path.open("w", encoding="utf-8", newline="\n") as fh:
        for round_no in range(1, args.repeat + 1):
            for i, case in enumerate(cases, 1):
                rec = _execute_case(case, round_no)
                records.append(rec)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                mark = "PASS" if rec["passed"] else ("ERR " if rec["exc"] else "FAIL")
                head = (f"[{i:>3}/{len(cases)}]" if args.repeat == 1
                        else f"[r{round_no} {i:>3}/{len(cases)}]")
                print(f"{head} {mark} {case['id']} verb={rec['verb']} "
                      f"steps={rec['steps']} {rec['ms'] / 1000:.1f}s")
    ax.LOOP_TOOLS, ax._agent_project_root = old_tools, old_root

    # ---------------- 聚合报告 ----------------
    def _rate(items):
        return f"{sum(items)}/{len(items)} ({(sum(items) / len(items) * 100) if items else 0:.1f}%)"

    total = [r["passed"] for r in records]
    total_old = [r["passed_old"] for r in records]
    total_v3 = [r["passed_v3"] for r in records if r["id"] in _V3_IDS]
    by_cat: dict[str, list[bool]] = {}
    dim_stat: dict[str, list[bool]] = {}
    for r in records:
        by_cat.setdefault(r["cat"], []).append(r["passed"])
        for c in r["checks"]:
            if c["dim"].startswith("_"):
                continue
            dim_stat.setdefault(c["dim"], []).append(c["ok"])
    lat = sorted(r["ms"] for r in records if not r["exc"])
    channels: dict[str, int] = {}
    for r in records:
        channels[r["channel"]] = channels.get(r["channel"], 0) + 1
    guards = [(r["id"], c["detail"]) for r in records for c in r["checks"]
              if c["dim"] == "_guard_events"]
    n_fallback = sum(1 for r in records if r["report_source"] == "deterministic")
    # 低-9（v5.2 统计溯源）：用例集 sha256 全 64 位 + harness/agent_exec 各自 sha256
    # 前缀 + 工作树 dirty 标记（git status --porcelain 非空即 dirty）
    cases_sha = hashlib.sha256((ROOT / args.cases).read_bytes()).hexdigest()
    harness_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    agent_exec_sha = hashlib.sha256(Path(ax.__file__).read_bytes()).hexdigest()[:12]

    def _worktree_dirty() -> bool:
        try:
            out = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                                 capture_output=True, text=True, timeout=15)
            return out.returncode == 0 and bool(out.stdout.strip())
        except Exception:  # noqa: BLE001
            return True  # 拿不到状态按 dirty 记（诚实方向）

    lines = ["# agent 真机性能探针报告", "",
             "- **定位：本探针是人工观察/趋势工具，非 CI 准入门**（严格分失败也 exit=0；"
             "已知未覆盖清单见 harness docstring）",
             f"- 用例集 `{args.cases}`（{len(cases)} 条 × {args.repeat} 轮 = {len(records)} 条记录）；"
             f"模型 `{getattr(cfg, 'model', '?')}`（provider `{getattr(cfg, 'provider', '?')}`，"
             f"base_url `{getattr(cfg, 'base_url', '?')}`）；git `{_git_commit()}`"
             f"（dirty={_worktree_dirty()}）；用例集 sha256 `{cases_sha}`；"
             f"harness sha256 `{harness_sha}`；agent_exec sha256 `{agent_exec_sha}`；"
             f"总耗时 {int(time.monotonic() - t0)}s",
             f"- 汇报通道信号（不计分）：deterministic 兜底 {n_fallback}/{len(records)} "
             f"({(n_fallback / len(records) * 100) if records else 0:.1f}%)"
             f"——LLM 汇报没接上的占比（fallback 率）",
             f"- **总分（本集 {len(cases)} 条 · 旧维口径，剔除 v4/v5/v6 新维） {_rate(total_old)}**",
             f"- **严格分（全维） {_rate(total)}**"]
    # 高-1（v5.2）：仅当本次运行覆盖全部 89 个 v3 id 时才显示参照行（子集跑不显示，
    # 防分母不足误标）；且 35 例定义相对 v3 已演进——非同一测量，仅供粗略参照。
    if total_v3 and _V3_IDS <= {r["id"] for r in records}:
        lines.append(f"- **v3 子集参照分（用例定义已演进，非同一测量，仅供粗略参照） "
                     f"{_rate(total_v3)}**（v3 原始 {len(_V3_IDS)} id · v3 时代 "
                     f"{len(_V3_DIMS)} 维；v3 基线 84/89 (94.4%)）")
    lines.append("")
    if args.repeat > 1:
        # pass^k（τ-bench 口径）：同一条用例 K 次全过才算过——单次过的运气不算数
        by_case: dict[str, list[dict]] = {}
        for r in records:
            by_case.setdefault(r["id"], []).append(r)
        all_pass = {cid: all(r["passed"] for r in rs) for cid, rs in by_case.items()}
        n_cases = len(by_case)
        lines += [f"## pass^k 总览（k={args.repeat}）", "",
                  f"- pass^{args.repeat}（K 次全过的用例占比）："
                  f"{sum(all_pass.values())}/{n_cases} "
                  f"({(sum(all_pass.values()) / n_cases * 100) if n_cases else 0:.1f}%)",
                  f"- 全败用例（0/{args.repeat}）："
                  f"{sum(1 for rs in by_case.values() if not any(r['passed'] for r in rs))} 条",
                  ""]

        def _flaky_dims(rs: list[dict]) -> list[str]:
            dim_vals: dict[str, set] = {}
            for r in rs:
                for c in r["checks"]:
                    if not c["dim"].startswith("_"):
                        dim_vals.setdefault(c["dim"], set()).add(bool(c["ok"]))
            return sorted(d for d, vs in dim_vals.items() if len(vs) > 1)

        unstable = [(cid, rs) for cid, rs in by_case.items()
                    if 0 < sum(r["passed"] for r in rs) < len(rs)]
        lines += [f"## 不稳定清单（0<通过率<1，共 {len(unstable)} 条）", ""]
        for cid, rs in unstable:
            flaky = _flaky_dims(rs)
            lines.append(f"- {cid}: 通过率 {sum(r['passed'] for r in rs)}/{len(rs)}；"
                         f"维度抖动：{flaky or ['（仅整例层面抖动）']}")
        # 2026-08-08 v5：维度级抖动补列——整例恒败但某维有翻转的同样是不稳定信号
        hard_flaky = [(cid, rs) for cid, rs in by_case.items()
                      if not any(r["passed"] for r in rs) and _flaky_dims(rs)]
        if hard_flaky:
            lines += ["", f"### 整例恒败但维度级抖动（{len(hard_flaky)} 条）", ""]
            for cid, rs in hard_flaky:
                lines.append(f"- {cid}: 0/{len(rs)}；维度抖动：{_flaky_dims(rs)}")
        lines.append("")
    lines += ["## 分类得分", ""]
    for cat, items in sorted(by_cat.items()):
        lines.append(f"- {cat}: {_rate(items)}")
    lines += ["", "## 维度得分", ""]
    for dim, items in sorted(dim_stat.items()):
        lines.append(f"- {dim}: {_rate(items)}")
    lines += ["", "## 通道与延迟", "",
              f"- understand 通道：{channels}",
              f"- 延迟 ms：p50={lat[len(lat) // 2] if lat else 0} "
              f"p95={lat[int(len(lat) * 0.95)] if lat else 0} max={lat[-1] if lat else 0}",
              "", f"## 护栏拦截事件（{len(guards)} 起，性能信号非扣分项）", ""]
    lines += [f"- {i}: {d}" for i, d in guards]
    fails = [r for r in records if not r["passed"]]
    clusters = _cluster_failures(fails)
    lines += ["", f"## 失败聚类（{len(clusters)} 簇 / {len(fails)} 条失败记录）", ""]
    for (dim, node, verb), ids, summary in clusters:
        lines.append(f"- {dim} × {node} × {verb}"
                     f"（{len(ids)} 条：{'、'.join(ids)}）：{summary}")
    lines += ["", f"## 失败画廊（{len(fails)} 条记录）", ""]
    for r in fails:
        bad = [c for c in r["checks"] if not c["ok"]]
        head = (f"### {r['id']}（{r['cat']}）" if args.repeat == 1
                else f"### {r['id']}（{r['cat']}，第 {r['round']} 轮）")
        lines += [head, f"- 原话：{r['utterance']}",
                  f"- verb={r['verb']} steps={r['steps']} exc={r['exc'] or '无'}",
                  f"- 失败维度：" + "；".join(f"{c['dim']}({c['detail'][:80]})" for c in bad),
                  f"- report：{str(r['report_zh'])[:200]}", ""]
    report_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"\n旧口径 {_rate(total_old)} | 严格分 {_rate(total)} → {report_path}")
    return 0


# --------------------------------------------------------------------------- 离线自验（--selftest）
# 零真实 API：合成 plan 直接过 score_case，新旧全部维度过/不过各至少一例；
# 另喂一批坏样本验证启动自检逐类带行号报出。

def _run_selftest(cases_path: Path) -> int:
    fails: list[str] = []
    n_checks = 0

    def check(name: str, fn) -> None:
        nonlocal n_checks
        n_checks += 1
        try:
            ok, detail = fn()
        except Exception as err:  # noqa: BLE001
            ok, detail = False, f"自验本身炸了：{type(err).__name__}: {err}"
        print(f"[{'PASS' if ok else 'FAIL'}] {n_checks:>2}. {name}" + ("" if ok else f" — {detail}"))
        if not ok:
            fails.append(name)

    # 0. 当前用例集启动自检（含 PAYLOAD 契约校验）必须全绿
    real_cases: list[dict] = []
    check("当前用例集启动自检通过", lambda: (
        (lambda cs: (bool(cs), f"load_cases 返回 {len(cs)} 条"))(
            real_cases.extend(load_cases(cases_path)) or real_cases)))

    def _case(tools, expect, utterance="联网搜 human lung 的数据"):
        return {"id": "t01", "cat": "A单步路由", "utterance": utterance,
                "tools": tools, "expect": expect}

    def _step(verb, ok=True, slots=None, readonly=True, result=None):
        s = {"verb": verb, "ok": ok, "slots": slots or {}, "readonly": readonly}
        if result is not None:
            s["result"] = result
        return s

    def _dims(case, plan, exc_kind="", exc_guard=False):
        return {c["dim"]: c["ok"] for c in score_case(case, plan, [], exc_kind=exc_kind,
                                                      exc_guard=exc_guard)}

    tools_ae = {"curate.check_updates": {"const": "check_ae2"},
                "curate.search_online": {"const": "search_ok"},
                "curate.db_status": {"const": "db_status_ok"}}
    # k08 型剧本：首步必败（raise）+ ENCODE 检查 + db
    tools_k08 = {"curate.search_online": {"raise": {"code": "network_error", "hint": "网络抖动"}},
                 "curate.check_updates": {"by_source": {"encode": {"const": "check_zero"}},
                                          "default": {"const": "check_zero"}},
                 "curate.db_status": {"const": "db_status_ok"}}
    wants_k08 = [{"verb": "curate.search_online"},
                 {"verb": "curate.check_updates", "source": "encode"},
                 {"verb": "curate.db_status"}]
    steps_k08_ok = [_step("curate.search_online", ok=False, readonly=False,
                          slots={"keywords": "human lung"}),
                    _step("curate.check_updates", ok=True, slots={"source": "ENCODE"}),
                    _step("curate.db_status", ok=True)]

    check("first 过/不过双向", lambda: (
        (lambda d1, d2: (d1.get("first") is True and d2.get("first") is False,
                         f"{d1} / {d2}"))(
            _dims(_case({}, {"first": "curate.check_updates"}),
                  {"verb": "curate.check_updates"}),
            _dims(_case({}, {"first": "curate.check_updates"}),
                  {"verb": "curate.db_status"}))))

    check("must_steps 字符串形过/缺", lambda: (
        (lambda d1, d2: (d1.get("must_steps") is True and d2.get("must_steps") is False,
                         f"{d1} / {d2}"))(
            _dims(_case(tools_ae, {"must_steps": ["curate.check_updates"]}),
                  {"verb": "curate.check_updates",
                   "steps": [_step("curate.check_updates")]}),
            _dims(_case(tools_ae, {"must_steps": ["curate.check_updates",
                                                  "curate.db_status"]}),
                  {"verb": "curate.check_updates",
                   "steps": [_step("curate.check_updates")]}))))

    check("must_steps 对象形 source 归一/别名组（10x ≡ 10x Genomics）", lambda: (
        (lambda d1, d2, d3: (d1.get("must_steps") is True and d2.get("must_steps") is True
                             and d3.get("must_steps") is False, f"{d1} / {d2} / {d3}"))(
            _dims(_case(tools_ae, {"must_steps": [{"verb": "curate.check_updates",
                                                   "source": "encode"}]}),
                  {"verb": "curate.check_updates",
                   "steps": [_step("curate.check_updates", slots={"source": "ENCODE"})]}),
            _dims(_case(tools_ae, {"must_steps": [{"verb": "curate.search_online",
                                                   "source": "10x"}]}),
                  {"verb": "curate.search_online",
                   "steps": [_step("curate.search_online", readonly=False,
                                   slots={"source": "10x Genomics"})]}),
            _dims(_case(tools_ae, {"must_steps": [{"verb": "curate.check_updates",
                                                   "source": "arrayexpress"}]}),
                  {"verb": "curate.check_updates",
                   "steps": [_step("curate.check_updates", slots={"source": "ENCODE"})]}))))

    check("must_steps 对象形 keywords 大小写不敏感子串", lambda: (
        (lambda d1, d2: (d1.get("must_steps") is True and d2.get("must_steps") is False,
                         f"{d1} / {d2}"))(
            _dims(_case(tools_ae, {"must_steps": [
                      {"verb": "curate.search_online", "keywords": "Human Lung"}]}),
                  {"verb": "curate.search_online",
                   "steps": [_step("curate.search_online", readonly=False,
                                   slots={"keywords": "human lung data"})]}),
            _dims(_case(tools_ae, {"must_steps": [
                      {"verb": "curate.search_online", "keywords": "mouse brain"}]}),
                  {"verb": "curate.search_online",
                   "steps": [_step("curate.search_online", readonly=False,
                                   slots={"keywords": "human lung"})]}))))

    check("must_steps_unordered 无序存在/缺件", lambda: (
        (lambda d1, d2: (d1.get("must_steps_unordered") is True
                         and d2.get("must_steps_unordered") is False, f"{d1} / {d2}"))(
            _dims(_case(tools_ae, {"must_steps_unordered": [
                      {"verb": "curate.db_status"},
                      {"verb": "curate.check_updates", "source": "arrayexpress"}]}),
                  {"verb": "curate.check_updates",
                   "steps": [_step("curate.check_updates", slots={"source": "ArrayExpress"}),
                             _step("curate.db_status")]}),
            _dims(_case(tools_ae, {"must_steps_unordered": [
                      {"verb": "curate.db_status"},
                      {"verb": "curate.check_updates", "source": "encode"}]}),
                  {"verb": "curate.check_updates",
                   "steps": [_step("curate.check_updates", slots={"source": "ArrayExpress"}),
                             _step("curate.db_status")]}))))

    check("chain_complete 剧本隐含 ok：k08 型首步剧本必败机械可过", lambda: (
        (lambda d: (d.get("must_steps") is True and d.get("chain_complete") is True, f"{d}"))(
            _dims(_case(tools_k08, {"must_steps": wants_k08}),
                  {"verb": "curate.search_online", "steps": steps_k08_ok}))))

    check("chain_complete 剧本该成却败 → 记败（v5.3 起 must_steps 同步咬住）", lambda: (
        (lambda d: (d.get("must_steps") is False and d.get("chain_complete") is False, f"{d}"))(
            _dims(_case(tools_k08, {"must_steps": wants_k08}),
                  {"verb": "curate.search_online",
                   "steps": [steps_k08_ok[0],
                             _step("curate.check_updates", ok=False,
                                   slots={"source": "ENCODE"}),
                             steps_k08_ok[2]]}))))

    check("chain_complete 剧本该败却「成」→ 记败（谎称成功的机械对面）", lambda: (
        (lambda d: (d.get("chain_complete") is False, f"{d}"))(
            _dims(_case(tools_k08, {"must_steps": wants_k08}),
                  {"verb": "curate.search_online",
                   "steps": [_step("curate.search_online", ok=True, readonly=False,
                                   slots={"keywords": "human lung"}),
                             steps_k08_ok[1], steps_k08_ok[2]]}))))

    check("on_script：tools 外 loop 动词记败 / tools 内过 / 非 loop 不参评", lambda: (
        (lambda d1, d2, d3: (d1.get("on_script") is False and d2.get("on_script") is True
                             and "on_script" not in d3, f"{d1} / {d2} / {d3}"))(
            _dims(_case({}, {"first": "curate.db_status"}),
                  {"verb": "curate.db_status"}),
            _dims(_case({"curate.db_status": {"const": "db_status_ok"}},
                        {"first": "curate.db_status"}),
                  {"verb": "curate.db_status",
                   "steps": [_step("curate.db_status")]}),
            _dims(_case({}, {"first": "pack.download"}), {"verb": "pack.download"}))))

    check("steps_exact 过/不过", lambda: (
        (lambda d1, d2: (d1.get("steps_exact") is True and d2.get("steps_exact") is False,
                         f"{d1} / {d2}"))(
            _dims(_case(tools_ae, {"steps_exact": 3}),
                  {"verb": "x", "steps": steps_k08_ok}),
            _dims(_case(tools_ae, {"steps_exact": 3}),
                  {"verb": "x", "steps": steps_k08_ok[:2]}))))

    check("expect_invalid：护栏拒收计过 / 非护栏异常与没拦计败（isinstance 收窄口径）", lambda: (
        (lambda d1, d2, d3, d4: (
            d1.get("guard_intercept") is True and "no_exception" not in d1
            and d2.get("guard_intercept") is False and d2.get("no_exception") is False
            and d3.get("guard_intercept") is False and d3.get("no_exception") is True
            and d4.get("no_exception") is False,
            f"{d1} / {d2} / {d3} / {d4}"))(
            _dims(_case({}, {"expect_invalid": True}), {},
                  exc_kind="AgentPlanInvalid", exc_guard=True),
            _dims(_case({}, {"expect_invalid": True}), {},
                  exc_kind="AgentUnavailable", exc_guard=False),
            _dims(_case({}, {"expect_invalid": True}), {"verb": "none"}),
            _dims(_case({}, {"first": "none"}), {}, exc_kind="RuntimeError"))))

    check("or_invalid 双结局：护栏拒收计过 / 无异常走正常 expect 双向", lambda: (
        (lambda d1, d2, d3: (
            d1.get("guard_intercept") is True and "first" not in d1
            and d2.get("first") is True and d2.get("no_exception") is True
            and "guard_intercept" not in d2
            and d3.get("first") is False,
            f"{d1} / {d2} / {d3}"))(
            _dims(_case({}, {"or_invalid": True, "first": "curate.check_updates"}), {},
                  exc_kind="AgentPlanInvalid", exc_guard=True),
            _dims(_case({}, {"or_invalid": True, "first": "curate.check_updates"}),
                  {"verb": "curate.check_updates"}),
            _dims(_case({}, {"or_invalid": True, "first": "curate.check_updates"}),
                  {"verb": "curate.db_status"}))))

    tools_mw = {**tools_ae, "curate.sync_updates": {"const": "sync_ok2"}}
    mw_clauses = [{"if_first": "curate.check_updates",
                   "must": ["curate.check_updates", "curate.search_online"]},
                  {"if_first": "curate.sync_updates", "must": ["curate.sync_updates"]}]
    check("must_when：子句命中过 / 缺件败 / 无子句零步结局空过（+chain 剧本核对）", lambda: (
        (lambda d1, d2, d3: (
            d1.get("must_when") is True and d1.get("chain_complete") is True
            and d2.get("must_when") is False and d2.get("chain_complete") is False
            and d3.get("must_when") is True and "chain_complete" not in d3,
            f"{d1} / {d2} / {d3}"))(
            _dims(_case(tools_mw, {"must_when": mw_clauses}),
                  {"verb": "curate.check_updates",
                   "steps": [_step("curate.check_updates"),
                             _step("curate.search_online", readonly=False)]}),
            _dims(_case(tools_mw, {"must_when": mw_clauses}),
                  {"verb": "curate.check_updates",
                   "steps": [_step("curate.check_updates")]}),
            _dims(_case(tools_mw, {"must_when": mw_clauses}),
                  {"verb": "none"}))))

    tools_raise = {"curate.check_updates": {"raise": {"code": "network_error",
                                                    "hint": "网络抖动"}}}
    check("must_steps 剧本 ok 核对（高-2）：const 却败 / 剧本该败却「成」都咬住", lambda: (
        (lambda d1, d2, d3, d4: (
            d1.get("must_steps") is True and d2.get("must_steps") is False
            and d3.get("must_steps") is True and d4.get("must_steps") is False,
            f"{d1} / {d2} / {d3} / {d4}"))(
            _dims(_case(tools_ae, {"must_steps": ["curate.check_updates"]}),
                  {"verb": "curate.check_updates",
                   "steps": [_step("curate.check_updates", ok=True)]}),
            _dims(_case(tools_ae, {"must_steps": ["curate.check_updates"]}),
                  {"verb": "curate.check_updates",
                   "steps": [_step("curate.check_updates", ok=False)]}),
            _dims(_case(tools_raise, {"must_steps": ["curate.check_updates"]}),
                  {"verb": "curate.check_updates",
                   "steps": [_step("curate.check_updates", ok=False)]}),
            _dims(_case(tools_raise, {"must_steps": ["curate.check_updates"]}),
                  {"verb": "curate.check_updates",
                   "steps": [_step("curate.check_updates", ok=True)]}))))

    check("must_when 子句同样带剧本 ok 核对（高-2）", lambda: (
        (lambda d1, d2: (d1.get("must_when") is True and d2.get("must_when") is False,
                         f"{d1} / {d2}"))(
            _dims(_case(tools_raise, {"must_when": [{"if_first": "curate.check_updates",
                                                     "must": ["curate.check_updates"]}]}),
                  {"verb": "curate.check_updates",
                   "steps": [_step("curate.check_updates", ok=False)]}),
            _dims(_case(tools_raise, {"must_when": [{"if_first": "curate.check_updates",
                                                     "must": ["curate.check_updates"]}]}),
                  {"verb": "curate.check_updates",
                   "steps": [_step("curate.check_updates", ok=True)]}))))

    or_clauses = [{"if_first": "curate.db_status",
                   "must": ["curate.db_status", "curate.sync_updates"]},
                  {"if_first": "curate.db_status",
                   "must": ["curate.db_status", {"verb": "curate.check_updates",
                                                 "source": "arrayexpress"}]}]
    check("must_when OR 子句（中-3）：同 if_first 双子句任一满足即过 / [db,db] 败", lambda: (
        (lambda d1, d2, d3: (
            d1.get("must_when") is True and d1.get("chain_complete") is True
            and d2.get("must_when") is True
            and d3.get("must_when") is False and d3.get("chain_complete") is False,
            f"{d1} / {d2} / {d3}"))(
            _dims(_case(tools_mw, {"must_when": or_clauses}),
                  {"verb": "curate.db_status",
                   "steps": [_step("curate.db_status"),
                             _step("curate.sync_updates", readonly=False)]}),
            _dims(_case(tools_mw, {"must_when": or_clauses}),
                  {"verb": "curate.db_status",
                   "steps": [_step("curate.db_status"),
                             _step("curate.check_updates",
                                   slots={"source": "ArrayExpress"})]}),
            _dims(_case(tools_mw, {"must_when": or_clauses}),
                  {"verb": "curate.db_status",
                   "steps": [_step("curate.db_status"),
                             _step("curate.db_status")]}))))

    check("check_sources：覆盖过 / 缺源败 / 别名组（10x≡10x Genomics）/ 失败步不算", lambda: (
        (lambda d1, d2, d3, d4: (
            d1.get("check_sources") is True and d2.get("check_sources") is False
            and d3.get("check_sources") is True and d4.get("check_sources") is False,
            f"{d1} / {d2} / {d3} / {d4}"))(
            _dims(_case(tools_ae, {"check_sources": ["encode"]}),
                  {"verb": "x", "steps": [_step("curate.check_updates",
                                                slots={"source": "ENCODE"})]}),
            _dims(_case(tools_ae, {"check_sources": ["arrayexpress"]}),
                  {"verb": "x", "steps": [_step("curate.check_updates",
                                                slots={"source": "ENCODE"})]}),
            _dims(_case(tools_ae, {"check_sources": ["10x"]}),
                  {"verb": "x", "steps": [_step("curate.check_updates",
                                                slots={"source": "10x Genomics"})]}),
            _dims(_case(tools_ae, {"check_sources": ["encode"]}),
                  {"verb": "x", "steps": [_step("curate.check_updates", ok=False,
                                                slots={"source": "ENCODE"})]}))))

    check("search_topics：覆盖过 / 缺主题败 / 空关键词计败 / 多步并集", lambda: (
        (lambda d1, d2, d3, d4: (
            d1.get("search_topics") is True and d2.get("search_topics") is False
            and d3.get("search_topics") is False and d4.get("search_topics") is True,
            f"{d1} / {d2} / {d3} / {d4}"))(
            _dims(_case(tools_ae, {"search_topics": ["human lung"]}),
                  {"verb": "x", "steps": [_step("curate.search_online", readonly=False,
                                                slots={"keywords": "Human Lung data"})]}),
            _dims(_case(tools_ae, {"search_topics": ["mouse brain"]}),
                  {"verb": "x", "steps": [_step("curate.search_online", readonly=False,
                                                slots={"keywords": "human lung"})]}),
            _dims(_case(tools_ae, {"search_topics": ["human lung"]}),
                  {"verb": "x", "steps": [_step("curate.search_online", readonly=False,
                                                slots={"keywords": ""})]}),
            _dims(_case(tools_ae, {"search_topics": ["human lung", "mouse brain"]}),
                  {"verb": "x", "steps": [_step("curate.search_online", readonly=False,
                                                slots={"keywords": "human lung"}),
                                          _step("curate.search_online", readonly=False,
                                                slots={"keywords": "mouse brain"})]}))))

    check("no_exception 正常轮次补记 True（低-9：维度级抖动可见异常维翻转）", lambda: (
        (lambda d: (d.get("no_exception") is True, f"{d}"))(
            _dims(_case({}, {"first": "none"}), {"verb": "none"}))))

    def _covers(steps, report):
        case = _case(tools_ae, {"first": "none"})
        plan = {"verb": steps[0]["verb"], "steps": steps,
                "report_zh": report, "report_source": "llm"}
        return _dims(case, plan).get("report_covers")

    db_step = _step("curate.db_status", ok=True,
                    result=json.loads(json.dumps(PAYLOADS["db_status_ok"])))
    check("report_covers 千分位命中 + 边界安全（47560 盖不住 4756）", lambda: (
        (lambda a, b: (a is True and b is False, f"{a} / {b}"))(
            _covers([db_step], "库里有 4,756 条数据。"),
            _covers([db_step], "库里有 47560 条数据。"))))

    check_step = _step("curate.check_updates", ok=True, slots={"source": "ArrayExpress"},
                       result=json.loads(json.dumps(PAYLOADS["check_ae2"])))
    check("report_covers 中文小写数字（新增两条 ≡ 2）", lambda: (
        (lambda a: (a is True, f"{a}"))(
            _covers([check_step], "ArrayExpress 新增两条疑似数据。"))))

    check("report_covers 数字>0 必须数字命中（label 不再顶替）", lambda: (
        (lambda a: (a is False, f"{a}"))(
            _covers([check_step], "ArrayExpress 检查完成，有新增。"))))

    check_zero_step = _step("curate.check_updates", ok=True,
                            result=json.loads(json.dumps(PAYLOADS["check_zero"])))
    check("report_covers 零新增（中-4 同小句纪律）：label 小句内否定过 / 无 label 记缺", lambda: (
        (lambda a, b, c, d: (a is True and b is True and c is False and d is False,
                             f"{a} / {b} / {c} / {d}"))(
            _covers([check_zero_step], "ArrayExpress 检查完成，没有新增。"),
            _covers([check_zero_step], "ArrayExpress 近期没有任何新数据。"),
            _covers([check_zero_step], "ArrayExpress 状态良好。"),
            # 残余近似登记：不点名来源的诚实零值汇报从此计缺（换跨小句借否定）
            _covers([check_zero_step], "检查完成，没有新增。"))))

    search_step = _step("curate.search_online", ok=True, readonly=False,
                        result=json.loads(json.dumps(PAYLOADS["search_ok"])))
    check("report_covers 顺序倒置放行（全局不消费再找）+ 真缺仍报", lambda: (
        (lambda a, b: (a is True and b is False, f"{a} / {b}"))(
            # 「库内共4756条；本次新增2条」：db 要的 4756 在 check 要的 2 之前——
            # v5.2 中-6b 前：游标越过 2 后 4756 漏报（顺序假阴性）；现在全局赦免盖到
            _covers([check_step, db_step], "库内共4756条；本次新增2条。"),
            _covers([check_step, db_step], "本次新增2条。"))))

    check("report_covers 零值同小句否定（中-6a）：隔壁小句的「没有」不许借", lambda: (
        (lambda a, b, c: (a is False and b is True and c is True, f"{a} / {b} / {c}"))(
            # 例：「网络没有异常」的没有不许覆盖 ArrayExpress 的 new_count=0
            _covers([check_zero_step], "网络没有异常；ArrayExpress 检查完成。"),
            # label 所在小句内含否定词 → 盖到
            _covers([check_zero_step], "网络没有异常；ArrayExpress 本次没有任何新内容。"),
            # 0/零 数字形仍认（严格模式保留「零」）
            _covers([check_zero_step], "ArrayExpress 新增为零。"))))

    search_zero_step = _step("curate.search_online", ok=True, readonly=False,
                             result=json.loads(json.dumps(PAYLOADS["search_zero"])))
    check("report_covers 搜索零：「没搜到」类否定措辞", lambda: (
        (lambda a, b: (a is True and b is False, f"{a} / {b}"))(
            _covers([search_zero_step], "没搜到符合条件的数据。"),
            _covers([search_zero_step], "搜索完成。"))))

    sync_step = _step("curate.sync_updates", ok=True, readonly=False,
                      result=json.loads(json.dumps(PAYLOADS["sync_ok2"])))
    check("report_covers sync：imported_total>0 必须数字命中", lambda: (
        (lambda a, b: (a is True and b is False, f"{a} / {b}"))(
            _covers([sync_step], "已同步入库 2 条。"),
            _covers([sync_step], "已同步入库。"))))

    # number_grounded（v6）：双向 + 豁免 + 不适用口径。db_step 的 result 即
    # db_status_ok 负载（total_records=4756，snapshot_date 2026-08-01）。
    def _grounded(steps, report):
        plan = {"verb": steps[0]["verb"] if steps else "none", "steps": steps,
                "report_zh": report}
        return _dims(_case(tools_ae, {"first": "none"}), plan).get("number_grounded")

    check("number_grounded：有出处过 / 无出处败 / 词边界（47560、47 都盖不住 4756）", lambda: (
        (lambda a, b, c, d: (a is True and b is False and c is False and d is False,
                             f"{a} / {b} / {c} / {d}"))(
            _grounded([db_step], "库内共 4756 条数据。"),
            _grounded([db_step], "库内共 9999 条数据。"),
            _grounded([db_step], "库内共 47560 条数据。"),
            _grounded([db_step], "库内共 47 条数据。"))))

    check("number_grounded：千分位 + 豁免（步序号/百分比/年份/日期头）不误伤", lambda: (
        (lambda a, b: (a is True and b is True, f"{a} / {b}"))(
            _grounded([db_step], "库内共 4,756 条数据。"),
            # 3 步=步骤序号豁免；100%/60% 百分比豁免；2026 年=年份豁免；
            # 2026-08-01 的 2026 是日期头豁免，08/01 在负载 snapshot_date 里有出处
            _grounded([db_step], "本次共跑 3 步，覆盖率 100%，来源占 60%，"
                                 "截至 2026 年（快照 2026-08-01），库内 4756 条。"))))

    check("number_grounded：空 steps / 空汇报不适用不参评（不进分母）", lambda: (
        (lambda d1, d2: ("number_grounded" not in d1 and "number_grounded" not in d2,
                         f"{d1} / {d2}"))(
            _dims(_case(tools_ae, {"first": "none"}),
                  {"verb": "none", "steps": [], "report_zh": "共 123 条。"}),
            _dims(_case(tools_ae, {"first": "none"}),
                  {"verb": "curate.db_status", "steps": [db_step], "report_zh": ""}))))

    check("number_grounded：零值豁免——空容器在场的 0 过 / 无空容器且无 0 出处的 0 仍拦", lambda: (
        # db_step 负载里 external_files=[]/recycle=[]（空容器在场）→「0 个文件」豁免；
        # check_ae2 无空容器、无字面 0 →「0 条新增」仍拦（编造零值继续咬）。
        (lambda a, b: (a is True and b is False, f"{a} / {b}"))(
            _grounded([db_step], "外部库与回收站均为 0 个文件。"),
            _grounded([check_step], "本次检查到 0 条疑似新增。"))))

    check("失败聚类：同三元组聚一簇（首败维=首个 ok=false）/ 异簇分开 / 未知维概括原样", lambda: (
        (lambda cl: (len(cl) == 2 and cl[0][1] == ["x01", "x02"]
                     and cl[0][0] == ("must_steps", "execute", "curate.db_status")
                     and cl[0][2] == "少跑了步"
                     and cl[1][0] == ("weird_dim", "—", "none")
                     and cl[1][2] == "weird_dim", f"{cl}"))(
            _cluster_failures([
                {"id": "x01", "verb": "curate.db_status",
                 "checks": [{"dim": "must_steps", "ok": False, "detail": ""}],
                 "trace": [{"node": "understand", "ok": True},
                           {"node": "execute", "ok": False}]},
                {"id": "x02", "verb": "curate.db_status",
                 "checks": [{"dim": "first", "ok": True, "detail": ""},
                            {"dim": "must_steps", "ok": False, "detail": ""}],
                 "trace": [{"node": "execute", "ok": False}]},
                {"id": "x03", "verb": "none",
                 "checks": [{"dim": "weird_dim", "ok": False, "detail": ""}],
                 "trace": []}]))))

    # 2026-08-08 v5.1 游标回归钉：hit_number 取正则与措辞候选的
    # 最早命中消费——报告尾部的孤立「0」（外部文件0）不许把游标拽过中间的「4756」。
    zero_a = _step("curate.check_updates", ok=True, slots={"source": "ArrayExpress"},
                   result=json.loads(json.dumps(PAYLOADS["check_zero"])))
    zero_b = _step("curate.check_updates", ok=True, slots={"source": "ENCODE"},
                   result=json.loads(json.dumps(PAYLOADS["check_zero"])))
    db_4756 = _step("curate.db_status", ok=True,
                    result=json.loads(json.dumps(PAYLOADS["db_status_ok"])))
    check("report_covers 游标回归：尾部孤立「0」不甩中间「4756」，真缺必报", lambda: (
        (lambda m1, m2: (m1 == [] and any("4756" in m for m in m2),
                         f"全量 misses={m1} / 缺 4756 时 misses={m2}"))(
            _report_covers_misses(
                [zero_a, zero_b, db_4756],
                "ArrayExpress 检查完成，没有新增。ENCODE 检查完成，没有新增。"
                "当前库内总记录 4756 条。外部文件0个。"),
            _report_covers_misses(
                [zero_a, zero_b, db_4756],
                "ArrayExpress 检查完成，没有新增。ENCODE 检查完成，没有新增。"
                "外部文件0个。"))))

    check("search 负载 slots 跟随（中-5）：label/query/species/sample_titles/filename", lambda: (
        (lambda r1, r2: (
            r1["source_label"] == "ENCODE" and r1["query"] == "mouse brain"
            and r1["species"] == "Mouse"
            and len(r1["sample_titles"]) == 1
            and all("mouse brain" in t for t in r1["sample_titles"])
            and "encode" in r1["filename"]
            and r2["species"] == "" and r2["source_label"] == "ArrayExpress"
            and r2["query"] == "mouse brain",
            f"r1={json.dumps(r1, ensure_ascii=False)[:160]} / "
            f"r2={json.dumps(r2, ensure_ascii=False)[:160]}"))(
            _outcome_to_result({"const": "search_ok"},
                               {"source": "ENCODE", "keywords": "mouse brain",
                                "species": "Mouse"}),
            _outcome_to_result({"const": "search_ok"}, {"keywords": "mouse brain"}))))

    check("no_ungrounded 在 sync 单步路径空过不炸（e01/f05 型 v5.1 sync 合法化口径）", lambda: (
        (lambda d: (d.get("no_ungrounded") is True, f"{d}"))(
            _dims(_case({"curate.sync_updates": {"const": "sync_ok2"}},
                        {"no_ungrounded": True},
                        utterance="看看有没有什么新数据，有的话拿回来"),
                  {"verb": "curate.sync_updates",
                   "steps": [_step("curate.sync_updates", ok=True, readonly=False,
                                   slots={"source": "ArrayExpress"})]}))))

    check("zero_writes 诚实口径（成功写步破戒 / 失败写步不算）", lambda: (
        (lambda d1, d2: (d1.get("zero_writes") is False and d2.get("zero_writes") is True,
                         f"{d1} / {d2}"))(
            _dims(_case(tools_ae, {"zero_writes": True}),
                  {"verb": "x", "steps": [_step("curate.search_online", ok=True,
                                                readonly=False)]}),
            _dims(_case(tools_ae, {"zero_writes": True}),
                  {"verb": "x", "steps": [_step("curate.search_online", ok=False,
                                                readonly=False)]}))))

    check("no_ungrounded 过/不过（臆造关键词记败）", lambda: (
        (lambda d1, d2: (d1.get("no_ungrounded") is True
                         and d2.get("no_ungrounded") is False, f"{d1} / {d2}"))(
            _dims(_case(tools_ae, {"no_ungrounded": True},
                        utterance="联网搜 human lung 的数据"),
                  {"verb": "curate.search_online",
                   "steps": [_step("curate.search_online", ok=True, readonly=False,
                                   slots={"keywords": "human lung"})]}),
            _dims(_case(tools_ae, {"no_ungrounded": True},
                        utterance="看看有没有什么新数据，有的话拿回来"),
                  {"verb": "curate.search_online",
                   "steps": [_step("curate.search_online", ok=True, readonly=False,
                                   slots={"keywords": "zebrafish embryo"})]}))))

    check("ideal_steps / max_steps 过与不过", lambda: (
        (lambda d1, d2: (d1.get("ideal_steps") is True and d1.get("max_steps") is True
                         and d2.get("ideal_steps") is False and d2.get("max_steps") is False,
                         f"{d1} / {d2}"))(
            _dims(_case(tools_ae, {"ideal_steps": 2, "max_steps": 3}),
                  {"verb": "x", "steps": steps_k08_ok[:2]}),
            _dims(_case(tools_ae, {"ideal_steps": 1, "max_steps": 2}),
                  {"verb": "x", "steps": steps_k08_ok}))))

    # 坏样本：每类手误一行（+1 行合法对照 + 1 行重复 id 的载体），全部带行号报出
    def _bad_samples():
        good = json.dumps({"id": "x01", "cat": "A单步路由", "utterance": "u",
                           "tools": {}, "expect": {"first": "none"}}, ensure_ascii=False)
        bads = [
            good.replace('"utterance": "u"', '"utterance": "另一行同 id"'),       # 第2行：id 重复（与第1行同 x01）
            "{not json",                                                           # 第3行：JSON 语法
            json.dumps({"id": "x04", "cat": "A单步路由"}, ensure_ascii=False),     # 第4行：缺 utterance
            json.dumps({"id": "x05", "cat": "ZZ", "utterance": "u", "expect": {"first": "none"}}, ensure_ascii=False),  # 第5行：cat 非法
            json.dumps({"id": "x06", "cat": "A单步路由", "utterance": "u",
                        "tools": {"curate.fly": {"const": "search_ok"}},
                        "expect": {"first": "none"}}, ensure_ascii=False),         # 第6行：tools 未登记动词
            json.dumps({"id": "x07", "cat": "A单步路由", "utterance": "u",
                        "tools": {"curate.db_status": {"const": "nope"}},
                        "expect": {"first": "none"}}, ensure_ascii=False),         # 第7行：payload 名不存在
            json.dumps({"id": "x08", "cat": "A单步路由", "utterance": "u",
                        "expect": {"firs": "none"}}, ensure_ascii=False),          # 第8行：expect 字段拼写
            json.dumps({"id": "x09", "cat": "A单步路由", "utterance": "u",
                        "expect": {"first": "none", "max_steps": "3"}}, ensure_ascii=False),  # 第9行：max_steps 类型
            '{"id": "x10", "id": "x10b", "cat": "A单步路由", "utterance": "u", "expect": {"first": "none"}}',  # 第10行：重复 JSON 键
            json.dumps({"id": "x11", "cat": "A单步路由", "utterance": "u",
                        "tools": {"curate.db_status": {"const": "db_status_ok"}},
                        "expect": {"first": "curate.db_status"}}, ensure_ascii=False),  # 第11行：钉路由没钉执行
            json.dumps({"id": "x12", "cat": "A单步路由", "utterance": "u",
                        "expect": {"first": "none", "must_steps": ["curate.db_status"],
                                   "forbid_steps": ["curate.db_status"]}}, ensure_ascii=False),  # 第12行：must∩forbid
            json.dumps({"id": "x13", "cat": "A单步路由", "utterance": "u",
                        "expect": {"first": "none"}, "foo": 1}, ensure_ascii=False),  # 第13行：顶层未知字段
            json.dumps({"id": "x14", "cat": "A单步路由", "utterance": "u",
                        "tools": {}, "expect": {"first": "curate.db_status",
                                                "must_steps": ["curate.db_status"]}},
                       ensure_ascii=False),  # 第14行：first 的 loop 动词 tools 没提供（off-script 必败分支）
            json.dumps({"id": "x15", "cat": "A单步路由", "utterance": "u",
                        "expect": {"first": "none",
                                   "must_when": [{"if_first": "curate.db_status",
                                                  "must": []}]}}, ensure_ascii=False),  # 第15行：must_when 空 must
            json.dumps({"id": "x16", "cat": "A单步路由", "utterance": "u",
                        "expect": {"expect_invalid": True, "or_invalid": True}},
                       ensure_ascii=False),  # 第16行：expect_invalid 与 or_invalid 互斥
            json.dumps({"id": "x17", "cat": "A单步路由", "utterance": "u",
                        "expect": {"first": "none", "steps_exact": 0}},
                       ensure_ascii=False),  # 第17行：steps_exact 零（恒过通道）
            json.dumps({"id": "x18", "cat": "A单步路由", "utterance": "u",
                        "expect": {"or_invalid": True, "first": "none"}},
                       ensure_ascii=False),  # 第18行：or_invalid 正常分支无执行断言
            json.dumps({"id": "x19", "cat": "A单步路由", "utterance": "u",
                        "expect": {"first": "none", "forbid_steps": []}},
                       ensure_ascii=False),  # 第19行：forbid_steps 空数组
        ]
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = Path(tmp) / "bad.jsonl"
            bad_path.write_text("\n".join([good, *bads]) + "\n", encoding="utf-8")
            buf = io.StringIO()
            try:
                with contextlib.redirect_stderr(buf):
                    load_cases(bad_path)
            except SystemExit:
                pass
            else:
                return False, "坏样本没触发 SystemExit"
            errtext = buf.getvalue()
        missing_lines = [ln for ln in range(2, 20) if f"第{ln}行" not in errtext]
        good_leak = "第1行" in errtext
        ok = not missing_lines and not good_leak
        return ok, f"缺行号={missing_lines} 合法行误伤={good_leak}\n{errtext}"

    check("坏样本 18 类手误逐行报出（合法行不误伤）", _bad_samples)

    print(f"\n自验 {n_checks} 项：{n_checks - len(fails)} 过 / {len(fails)} 败")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
