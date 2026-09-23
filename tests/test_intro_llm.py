# -*- coding: utf-8 -*-
"""N5 · LLM 中文介绍层的**确定性**测试（无网络）。

覆盖：默认关零成本、mock 短路（`call_mock_llm` 绝不用于介绍）、体裁门、无 key 门、成功路径、
fail-open（provider 失败/异常→确定性保留）、prompt 接地护栏、additive（确定性字段不变）。
真 LLM 产出质量只能靠真 provider 复验——不在本门覆盖（见模块头诚实说明）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest

from dataset_recommender.llm import act_summary_llm, intro_llm, llm_client
from dataset_recommender.content import summary_genre
from dataset_recommender.content.introduction import build_dataset_introduction
from dataset_recommender.llm.llm_client import LLMConfig, LLMResult


def _prose_item():
    return {
        "dataset_name": "Single cell RNA sequencing of human lung",
        "source": "ArrayExpress",
        "species": "Human", "tissue": "lung", "disease": "lung cancer",
        "platform": "Chromium",
        "description": "E-MTAB-1 · scRNA-seq · We profiled immune cells of human lung tumours "
                       "by single-cell RNA sequencing to characterise the microenvironment across patients.",
    }


def _title_item():
    return {
        "dataset_name": "A big atlas",
        "source": "CELLxGENE Discover",
        "species": "Human", "tissue": "blood",
        "description": "Cross-tissue immune cell analysis reveals tissue-specific features · DOI: 10.1038/x",
    }


def _scaffold_item():
    return {"dataset_name": "X", "source": "10x Genomics", "species": "Human", "description": ""}


def _cfg(enable=True, provider="openai-compatible", key="sk-test", mock=False):
    return LLMConfig(enable_llm=enable, mock_llm=mock, provider=provider, api_key=key)


# ---------------------------------------------------------------- 门控

def test_default_off_returns_disabled(monkeypatch):
    """默认关（config.enable_llm=False）→ llm_summary=None、status=disabled，且**绝不**调 provider。
    config 经 `load_llm_config`（含 .env）加载——刻意不做 os.getenv 快路径（那会在新进程里把 .env 已开
    误判成关，真机 E2E 抓到过）。"""
    monkeypatch.setattr(act_summary_llm, "load_llm_config", lambda *a, **k: _cfg(enable=False))
    monkeypatch.setattr(llm_client, "call_openai_compatible",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该调 provider")))

    out = intro_llm.enrich_introduction_with_llm(_prose_item())
    assert out["llm_summary"] is None
    assert out["llm_summary_source"] is None
    assert out["llm_status"] == "disabled"


def test_config_loaded_from_env_not_os_getenv(monkeypatch):
    """回归：ENABLE_LLM 只在 .env、不在 os.environ 时，也必须走 load_llm_config 检测到「开」
    （防 os.getenv 快路径复活——那正是真机 webapp 里 LLM 层静默失效的根因）。"""
    monkeypatch.delenv("ENABLE_LLM", raising=False)   # os.environ 里没有
    monkeypatch.setattr(act_summary_llm, "load_llm_config", lambda *a, **k: _cfg(enable=True))  # 但 .env 载出来是开
    called = {}
    def fake(prompt, cfg):
        called["yes"] = True
        return LLMResult(text="导读", attempted=True, succeeded=True, response_used=False,
                         provider="openai-compatible", model="m")
    monkeypatch.setattr(llm_client, "call_openai_compatible", fake)
    out = intro_llm.enrich_introduction_with_llm(_prose_item())
    assert called.get("yes") is True and out["llm_status"] == "ok"


def test_mock_is_short_circuited():
    """mock 一律判否——call_mock_llm 忽略 prompt 吐 curator 表，绝不用于介绍。"""
    ok, reason = intro_llm.should_use_llm(_cfg(mock=True), summary_genre.GENRE_PROSE)
    assert ok is False and reason == "mock_not_used"
    ok2, reason2 = intro_llm.should_use_llm(_cfg(provider="mock"), summary_genre.GENRE_PROSE)
    assert ok2 is False and reason2 == "mock_not_used"


def test_mock_config_never_calls_provider(monkeypatch):
    """即便显式传 mock config（enable=True），也回退确定性、绝不调任何 provider。"""
    monkeypatch.setattr(llm_client, "call_openai_compatible",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("mock 不该走真 provider")))
    out = intro_llm.enrich_introduction_with_llm(_prose_item(), config=_cfg(mock=True))
    assert out["llm_summary"] is None
    assert out["llm_status"] == "mock_not_used"


def test_not_translatable_genre_blocked():
    ok, reason = intro_llm.should_use_llm(_cfg(), summary_genre.GENRE_SCAFFOLD)
    assert ok is False and reason == "not_translatable"
    ok2, reason2 = intro_llm.should_use_llm(_cfg(), summary_genre.GENRE_FACTOR_LIST)
    assert ok2 is False and reason2 == "not_translatable"


def test_no_key_blocked():
    ok, reason = intro_llm.should_use_llm(_cfg(key=None), summary_genre.GENRE_PROSE)
    assert ok is False and reason == "no_key"


def test_disabled_blocked():
    ok, reason = intro_llm.should_use_llm(_cfg(enable=False), summary_genre.GENRE_PROSE)
    assert ok is False and reason == "disabled"


def test_ready_when_all_conditions_met():
    ok, reason = intro_llm.should_use_llm(_cfg(), summary_genre.GENRE_PROSE)
    assert ok is True and reason == "ready"


# ---------------------------------------------------------------- 成功 / fail-open

def test_success_attaches_llm_summary(monkeypatch):
    monkeypatch.setattr(llm_client, "call_openai_compatible",
                        lambda prompt, cfg: LLMResult(text="这是一段中文导读。", attempted=True,
                                                      succeeded=True, response_used=False,
                                                      provider="openai-compatible", model="gpt-x"))
    out = intro_llm.enrich_introduction_with_llm(_prose_item(), config=_cfg())
    assert out["llm_summary"] == "这是一段中文导读。"
    assert out["llm_summary_source"] == "llm"
    assert out["llm_status"] == "ok"
    assert out["llm_model"] == "gpt-x"
    # 确定性字段仍在（additive，不替换证据）
    assert out["facts"] and out["caveat"] and out["summary"]


def test_fail_open_on_provider_failure(monkeypatch):
    """provider 返回失败 → llm_summary=None、status=failed:*，确定性 summary 一字不动。"""
    det = build_dataset_introduction(_prose_item())
    monkeypatch.setattr(llm_client, "call_openai_compatible",
                        lambda prompt, cfg: LLMResult(text=None, attempted=True, succeeded=False,
                                                      response_used=False, provider="openai-compatible",
                                                      model="gpt-x", error="HTTP 500 boom"))
    out = intro_llm.enrich_introduction_with_llm(_prose_item(), config=_cfg())
    assert out["llm_summary"] is None
    assert out["llm_status"].startswith("failed:")
    assert out["summary"] == det["summary"]      # 确定性证据不变
    assert out["facts"] == det["facts"]


def test_fail_open_on_provider_exception(monkeypatch):
    det = build_dataset_introduction(_prose_item())
    monkeypatch.setattr(llm_client, "call_openai_compatible",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down")))
    out = intro_llm.enrich_introduction_with_llm(_prose_item(), config=_cfg())
    assert out["llm_summary"] is None
    assert out["llm_status"].startswith("error:")
    # 异常路径也必须原样保留确定性证据（不只查 llm_summary）
    assert out["summary"] == det["summary"] and out["facts"] == det["facts"] and out["caveat"] == det["caveat"]


def test_empty_llm_text_fails_open(monkeypatch):
    monkeypatch.setattr(llm_client, "call_openai_compatible",
                        lambda prompt, cfg: LLMResult(text="   ", attempted=True, succeeded=True,
                                                      response_used=False, provider="openai-compatible",
                                                      model="gpt-x"))
    out = intro_llm.enrich_introduction_with_llm(_prose_item(), config=_cfg())
    assert out["llm_summary"] is None
    assert out["llm_status"].startswith("failed:")


# ---------------------------------------------------------------- prompt 接地护栏

def test_prompt_prose_has_guardrails_and_residual():
    item = _prose_item()
    intro = build_dataset_introduction(item)
    prompt = intro_llm.build_intro_prompt(intro, item, summary_genre.GENRE_PROSE)
    assert "开创性" in prompt or "独占性" in prompt        # 禁开创性/独占性断言（N2 受众不变量措辞）
    assert "忠实改写" in prompt
    assert "immune cells of human lung" in prompt          # residual 接地进 prompt
    assert "未标注" in prompt or "未说明" in prompt


def test_prompt_title_frames_as_paper_title():
    item = _title_item()
    intro = build_dataset_introduction(item)
    prompt = intro_llm.build_intro_prompt(intro, item, summary_genre.GENRE_TITLE)
    assert "关联论文的标题" in prompt
    assert "不是数据集摘要" in prompt
    assert "不要据标题编造" in prompt


def test_prompt_truncation_note_when_truncated():
    item = _prose_item()
    item["description"] = item["description"] + "…"        # 制造截断
    intro = build_dataset_introduction(item)
    assert intro["truncated"] is True
    prompt = intro_llm.build_intro_prompt(intro, item, summary_genre.GENRE_PROSE)
    assert "截断" in prompt


def test_prompt_pure_deterministic():
    item = _prose_item()
    intro = build_dataset_introduction(item)
    a = intro_llm.build_intro_prompt(intro, item, summary_genre.GENRE_PROSE)
    b = intro_llm.build_intro_prompt(intro, item, summary_genre.GENRE_PROSE)
    assert a == b


# ---------------------------------------------------------------- additive 不变量

def test_llm_layer_is_additive():
    """开关无关：确定性 summary/facts/caveat 恒等于 build_dataset_introduction 的产出。"""
    item = _prose_item()
    det = build_dataset_introduction(item)
    off = intro_llm.enrich_introduction_with_llm(item, config=_cfg(enable=False))
    assert off["summary"] == det["summary"]
    assert off["facts"] == det["facts"]
    assert off["caveat"] == det["caveat"]
    assert "llm_summary" in off and off["llm_summary"] is None


# ------------------------------------------------- 硬校验 #2 满分化：受控词表随 prompt 接地（术语落表）

def test_prompt_injects_matched_vocab_terms():
    """prompt 含「术语规范」块 + 本条记录命中的规范词 + 落表指令。"""
    item = _prose_item()   # Human / lung / lung cancer
    intro = build_dataset_introduction(item)
    prompt = intro_llm.build_intro_prompt(intro, item, summary_genre.GENRE_PROSE)
    assert "----- 术语规范（本条记录命中的受控词表规范词，译文必须照用） -----" in prompt
    assert "物种：原文「Human」→ 规范词「人类」" in prompt
    assert "组织：原文「lung」→ 规范词「肺」" in prompt
    assert "疾病：原文「lung cancer」→ 规范词「肺癌」" in prompt
    # 落表指令（铁律 7）：必须使用规范词、不得自造译名
    assert "译文必须使用对应" in prompt and "规范词" in prompt and "不得自造译名" in prompt


def test_prompt_grounding_excludes_irrelevant_vocab():
    """未命中的维度/条目零注入：无关词表内容绝不进 prompt。"""
    item = _prose_item()
    intro = build_dataset_introduction(item)
    prompt = intro_llm.build_intro_prompt(intro, item, summary_genre.GENRE_PROSE)
    for irrelevant in ("斑马鱼", "大象", "帕金森", "胸腺", "空间转录组", "MERFISH"):
        assert irrelevant not in prompt
    # 无关维度连「原文→规范词」行都不存在（本条记录三维齐全，故构造缺疾病的记录再验）
    item2 = {"species": "Mouse", "tissue": "blood", "disease": "",
             "source": "ArrayExpress", "description": "We profiled mouse blood by scRNA-seq."}
    intro2 = build_dataset_introduction(item2)
    prompt2 = intro_llm.build_intro_prompt(intro2, item2, summary_genre.GENRE_PROSE)
    assert "疾病：原文「" not in prompt2
    assert "规范词「小鼠」" in prompt2 and "规范词「血」" in prompt2


def test_prompt_no_grounding_block_when_nothing_matches():
    """三维都没命中词表 → 整块不出现；铁律 7 仍在（约束未覆盖术语保持朴素直译）。"""
    item = {"species": "Lepidoptera", "tissue": "", "disease": "unknown",
            "source": "ArrayExpress", "description": "We profiled butterfly wing discs."}
    intro = build_dataset_introduction(item)
    prompt = intro_llm.build_intro_prompt(intro, item, summary_genre.GENRE_PROSE)
    assert "----- 术语规范（" not in prompt
    assert "不得自造译名" in prompt          # 铁律 7 常驻
    assert "规范词「" not in prompt           # 无任何落表行


def test_grounding_latin_names_land_on_canonical_terms():
    """拉丁学名必须落到词表通用规范词（#2 点名的风险面）。"""
    cases = [
        ("Danio rerio", "斑马鱼"),
        ("Mus musculus", "小鼠"),
        ("Rattus norvegicus", "大鼠"),
        ("Rhesus macaque", "恒河猴"),
        ("Homo sapiens", "人类"),
    ]
    for latin, canonical in cases:
        terms = intro_llm._ground_terms_for_value(latin, "species")
        assert terms == [canonical], f"{latin} → {terms}，期望 [{canonical}]"


def test_grounding_specific_disease_swallows_generic_cancer():
    """长优先消费：「lung cancer」命中「肺癌」后不再重复命中 generic「癌」。"""
    terms = intro_llm._ground_terms_for_value("lung cancer", "disease")
    assert terms == ["肺癌"]
    item = _prose_item()
    intro = build_dataset_introduction(item)
    prompt = intro_llm.build_intro_prompt(intro, item, summary_genre.GENRE_PROSE)
    assert "规范词「癌」" not in prompt      # generic 癌症条目不重复落表


def test_grounding_multi_value_fields():
    """多值字段（逗号分隔）每个值分别落表，按原文出现顺序。"""
    assert intro_llm._ground_terms_for_value("Human, Mouse", "species") == ["人类", "小鼠"]
    assert intro_llm._ground_terms_for_value("Mouse, Human", "species") == ["小鼠", "人类"]
    tissue_terms = intro_llm._ground_terms_for_value("Embryonic Kidney, Embryo", "tissue")
    assert tissue_terms == ["胚胎", "肾"]


def test_grounding_ascii_word_boundary():
    """ASCII 词边界：「foreskin」不误中「skin」、「Meyer」不误中「eye」——宁可不落表也不错接地。"""
    assert intro_llm._ground_terms_for_value("foreskin", "tissue") == []
    assert intro_llm._ground_terms_for_value("Meyer", "tissue") == []
    # 连字符/空白是合法边界：「non-human primate」里的 human 仍可命中
    assert intro_llm._ground_terms_for_value("non-human primate", "species") != []


def test_grounding_missing_sentinel_fields_no_line():
    """缺失哨兵（unknown/not specified…）视同未标注，不产生落表行。"""
    assert intro_llm._vocab_grounding_lines({"species": "unknown", "tissue": "not specified",
                                             "disease": "n/a"}) == []


def test_grounding_block_in_title_genre_prompt():
    """CXG 体裁（只译论文标题）同样注入词表接地，且标题框定不被削弱。"""
    item = _title_item()   # Human / blood，CELLxGENE 论文标题
    intro = build_dataset_introduction(item)
    prompt = intro_llm.build_intro_prompt(intro, item, summary_genre.GENRE_TITLE)
    assert "规范词「人类」" in prompt and "规范词「血」" in prompt
    assert "关联论文的标题" in prompt and "不是数据集摘要" in prompt


def test_enrich_sends_grounded_prompt_to_provider(monkeypatch):
    """端到端（桩 provider）：实际发给 LLM 的 prompt 带术语规范块——全程无真 LLM。"""
    sent = {}
    def fake(prompt, cfg):
        sent["prompt"] = prompt
        return LLMResult(text="导读", attempted=True, succeeded=True, response_used=False,
                         provider="openai-compatible", model="m")
    monkeypatch.setattr(llm_client, "call_openai_compatible", fake)
    out = intro_llm.enrich_introduction_with_llm(_prose_item(), config=_cfg())
    assert out["llm_status"] == "ok"
    assert "----- 术语规范（" in sent["prompt"]
    assert "规范词「人类」" in sent["prompt"] and "规范词「肺癌」" in sent["prompt"]
