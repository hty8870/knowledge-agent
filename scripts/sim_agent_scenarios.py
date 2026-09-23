# -*- coding: utf-8 -*-
"""执行侧 Agent 路由模拟剧本。

用途：把一批**真实用户句**过 langgraph 图，打印路由表供人工核对——图的机械部分
（工具表生成、护栏、repair、trace）在这里被端到端跑一遍，LLM 由「启发式理想替身」扮演。

用法：
    .venv/Scripts/python.exe scripts/sim_agent_scenarios.py          # 离线：启发式替身 + 断言
    .venv/Scripts/python.exe scripts/sim_agent_scenarios.py --live   # 真 LLM：只打印不断言（人工核对）

替身是「如果 LLM 表现正常，它大概会这么调」的下限模拟——真 LLM 的方差用 --live 看。
退出码：离线模式有任何 FAIL 为 1；--live 恒为 0。
"""
from __future__ import annotations

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(_ROOT / "src"))
os.chdir(_ROOT)

import pytest  # noqa: E402  # 仅借 importorskip 的语义，不引测试框架

pytest.importorskip("langgraph", reason="langchain 扩展未安装")
from langchain_core.messages import AIMessage  # noqa: E402

from dataset_recommender.agent import agent_exec, turn  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig, load_llm_config  # noqa: E402

# ---------------------------------------------------------------- 剧本
# (utterance, expected_verb, has_results, 备注)
SCENARIOS = [
    ("检查10x是否有更新", "curate.check_updates", False, "病灶句"),
    ("看看 ENCODE 数据库有没有新数据", "curate.check_updates", False, "检查更新≠联网搜"),
    ("检查10x是否有更新，有的话下载下来", "curate.sync_updates", False, "检查+入库一条说完=复合流"),
    ("联网搜一下 ArrayExpress 有没有新的人类肺单细胞数据", "curate.search_online", False, "设计文档例句"),
    ("上网找找 GEO 有没有新的人脑数据", "curate.search_online", False, ""),
    ("删掉我上传的 10x 数据", "curate.remove", False, "回收站式"),
    ("把刚导入的那个文件删掉", "curate.remove", False, ""),
    ("把删掉的上传文件恢复回来", "curate.restore", False, ""),
    ("我上传了哪些数据？回收站里有什么？", "curate.list", False, ""),
    ("汇报数据库的当前状态", "curate.db_status", False, "状态汇报（observe 图内读工具）"),
    ("导入这个本地文件", "curate.import", False, ""),
    ("下载top3", "pack.download", True, "limit=3"),
    ("把前 5 条打包给我", "pack.download", True, "limit=5"),
    ("不要打包了", "pack.download", True, "cancelled=True"),
    ("先看看会打包哪些东西", "pack.preview", True, ""),
    ("导出引文，要 RIS", "cite.export", True, ""),
    ("整理一下投稿要用的数据可用性声明", "reuse.pack", True, ""),
    ("这批数据量够吗，一共多少细胞", "feasibility.run", True, ""),
    ("第一条结果里都有哪些文件", "files.show", True, ""),
    ("换成小鼠的乳腺癌数据", "refine.conditions", True, "带 effective_query"),
    ("推荐有 FASTQ 的人类乳腺癌数据", "search.new", False, "纯检索句"),
    ("小鼠肺癌单细胞", "search.new", False, "短检索句"),
    ("今天天气怎么样", "none", False, "闲聊→none"),
]


# ---------------------------------------------------------------- 启发式理想替身
def _source_of(text: str) -> str | None:
    for name in ("ArrayExpress", "10x", "ENCODE", "EBI SCEA", "HCA", "CELLxGENE", "GEO"):
        if name.lower() in text.lower():
            return name
    return None


def _quoted(text: str) -> str:
    """quoted 必须逐字子串——取原话中段一段连续文字（替身纪律与真 LLM 相同）。"""
    return text[2:12] if len(text) > 12 else text[:4]


def _ideal_answer(text: str, has_results: bool) -> AIMessage:
    """「如果 LLM 表现正常」的工具调用下限模拟（关键词启发式，非产品代码）。"""
    t = text.strip()
    cancel = bool(re.search(r"(不要|别|先不).{0,4}(打包|下载|删|导入|搜)", t))
    verb, args = "none", {}
    if re.search(r"(检查|看看).{0,16}(更新|新数据|新条目)|有没有更新", t) and _source_of(t):
        verb, args = "curate.check_updates", {"source": _source_of(t)}
    elif re.search(r"(联网|上网|在线|网上)", t) and re.search(r"(搜|找|有没有)", t):
        verb, args = "curate.search_online", {}
        if _source_of(t):
            args["source"] = _source_of(t)
    elif re.search(r"(恢复|找回|撤销删除)", t):
        verb = "curate.restore"
    elif re.search(r"(删|移除)", t):
        verb = "curate.remove"
    elif re.search(r"(导入|加进库)", t):
        verb = "curate.import"
    elif re.search(r"(上传了哪些|回收站里|清点)", t):
        verb = "curate.list"
    elif re.search(r"(汇报|当前状态|现在有什么|有什么变动|多少条)", t):
        verb = "curate.db_status"
    elif re.search(r"可行性|够不够|多少细胞|缺口", t):
        verb = "feasibility.run"
    elif re.search(r"引文|RIS|BibTeX|参考文献", t, re.I):
        verb = "cite.export"
    elif re.search(r"投稿|可用性声明|复用出处", t):
        verb = "reuse.pack"
    elif re.search(r"哪些文件|文件列表", t):
        verb = "files.show"
    elif re.search(r"(看看|预览).{0,6}(打包|清单)", t):
        verb = "pack.preview"
    elif re.search(r"(打包|下载|存下来|zip)", t, re.I):
        verb = "pack.download"
        m = re.search(r"(?:top|前)\s*(\d+)", t, re.I)
        if m:
            args["limit"] = int(m.group(1))
    elif re.search(r"(换成|去掉|再加|放宽)", t):
        verb = "refine.conditions"
        args["effective_query"] = re.sub(r"(换成|去掉|再加|放宽)", "", t)
    elif len(t) >= 4 and re.search(r"(数据|细胞|癌|瘤|FASTQ|小鼠|人|肺|脑|乳腺)", t):
        verb, args = "search.new", {"effective_query": t}
    if verb.startswith("curate.") or verb in (
        "pack.download", "pack.preview", "cite.export", "reuse.pack",
        "feasibility.run", "files.show",
    ):
        args.setdefault("quoted", _quoted(t))
    if cancel and verb != "none":
        args["cancelled"] = True
    return AIMessage(
        content="",
        tool_calls=[{"name": verb.replace(".", "_"), "args": args, "id": "sim1"}],
    )


class _IdealModel:
    def __init__(self, utterance: str, has_results: bool):
        self._answer = _ideal_answer(utterance, has_results)

    def bind_tools(self, tools, tool_choice=None):
        return self

    def invoke(self, messages):
        return self._answer


# ---------------------------------------------------------------- 跑表
def main() -> int:
    live = "--live" in sys.argv
    cfg = load_llm_config() if live else LLMConfig(enable_llm=True, api_key="sk-sim")
    fails = 0
    print(f"{'#':>2} {'期望':<22} {'实得':<22} {'槽位/取消':<30} {'trace 节点'}  判定")
    print("-" * 110)
    for i, (utt, want, has_results, note) in enumerate(SCENARIOS, 1):
        model = None if live else _IdealModel(utt, has_results)
        try:
            plan, trace = agent_exec.plan_with_agent(
                utt, has_results=has_results,
                result_total=34 if has_results else 0,
                config=cfg, retrieval=None, current_query="", current_filters=None,
                chat_model=model,
            )
            got = plan["verb"]
            slots = {k: v for k, v in (plan.get("slots") or {}).items() if v not in (None, "")}
            flag = f"cancelled " if plan.get("cancelled") else ""
            nodes = ">".join(dict.fromkeys(e["node"] for e in trace))
            ok = got == want or (want == "none" and plan.get("kind") != "exec")
        except agent_exec.AgentError as exc:
            got, slots, flag, nodes, ok = f"!!{type(exc).__name__}", {}, "", "-", False
        mark = "PASS" if ok else "FAIL"
        fails += 0 if ok or live else 1
        print(f"{i:>2} {want:<22} {got:<22} {str(slots)[:28]:<30} {flag}{nodes}  {mark} {note}")
    print("-" * 110)
    if live:
        print("--live 模式：未断言，请人工核对上表。")
        return 0
    print(f"共 {len(SCENARIOS)} 条，FAIL {fails} 条。")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
