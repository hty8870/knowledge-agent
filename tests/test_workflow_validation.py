"""validate_llm_answer 加固单测：反捏造校验器必须做**逐行「数据集名↔下载链接」配对**，
而非只做两个全局集合的成员检查。堵住 LLM 把 A 的名字配 B 的真实数据文件直链。

背景：阶段二把表格「下载链接」列从数据集页面 url 换成了真实文件下载直链。若校验器只把
所有名字、所有链接各自并成一个全局白名单分别查成员，LLM 一旦张冠李戴（top-K 常是同族不同
样本、名字/URL 仅差一个 token），用户会拿到**另一个数据集的真实 .h5 数据**而校验器放行。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.corpus import downloads  # noqa: E402
from dataset_recommender.retrieval.normalizer import DatasetRecord  # noqa: E402
from dataset_recommender.retrieval.retriever import RetrievedCandidate  # noqa: E402
from dataset_recommender.app.workflow import TABLE_HEADER, TABLE_SEPARATOR, validate_llm_answer  # noqa: E402

_A = "multiome-gemx-5k-mouse-kidney"
_B = "multiome-gemx-10k-human-brain"


class _Intent:
    has_raw_data_required = None


def _cand(name: str, uid: str) -> RetrievedCandidate:
    rec = DatasetRecord(
        dataset_name=name, species="", tissue="", disease="", chemistry="", count="", unit="",
        has_raw_data=True, url=f"https://www.10xgenomics.com/datasets/{uid}", source_file="x",
        description="", raw={"dataset_uid": uid},
    )
    return RetrievedCandidate(record=rec, score=1.0)


def _row(name: str, link: str) -> str:
    return f"| {name} | - | - | - | - | 未说明 | ✅ 包含 FASTQ | [点击下载]({link}) |"


def _table(rows: list[str]) -> str:
    return "\n".join([TABLE_HEADER, TABLE_SEPARATOR, *rows])


_CANDS = [_cand("Alpha Dataset", _A), _cand("Beta Dataset", _B)]


def test_correct_pairing_accepted():
    la, lb = downloads.primary_url(_A), downloads.primary_url(_B)
    ok = _table([_row("Alpha Dataset", la), _row("Beta Dataset", lb)])
    valid, reason = validate_llm_answer(ok, _CANDS, _Intent(), finish_reason="stop")
    assert valid, reason


def test_mispaired_link_rejected():
    # Alpha 的名字配 Beta 的真实数据文件直链 —— 必须拒（这正是加固要防的张冠李戴）。
    la, lb = downloads.primary_url(_A), downloads.primary_url(_B)
    bad = _table([_row("Alpha Dataset", lb), _row("Beta Dataset", la)])
    valid, reason = validate_llm_answer(bad, _CANDS, _Intent(), finish_reason="stop")
    assert not valid and reason and "not its own" in reason


def test_own_page_url_accepted():
    # mock/规则路径可能用数据集页面 url 作链接 —— 每条用自己的页面 url 应通过。
    pg = _table([
        _row("Alpha Dataset", f"https://www.10xgenomics.com/datasets/{_A}"),
        _row("Beta Dataset", f"https://www.10xgenomics.com/datasets/{_B}"),
    ])
    valid, reason = validate_llm_answer(pg, _CANDS, _Intent(), finish_reason="stop")
    assert valid, reason
