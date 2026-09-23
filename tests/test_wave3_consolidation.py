from __future__ import annotations

import os
import shutil
import subprocess
import io
import tokenize
from pathlib import Path

import pytest

from dataset_recommender.content.labels import (
    INTRO_FACT_LABELS,
    PROJECT_CONDITION_LABELS,
    RAW_FASTQ_GUESS,
    RAW_FASTQ_NO,
    RAW_FASTQ_UNKNOWN,
    RAW_FASTQ_YES,
    UNNAMED_DATASET,
    raw_fastq_status,
)
from dataset_recommender.corpus import corpus_curation


ROOT = Path(__file__).resolve().parents[1]


def test_fastq_and_shared_presentation_labels_are_single_source() -> None:
    assert [raw_fastq_status(x) for x in (True, False, None)] == [
        RAW_FASTQ_YES, RAW_FASTQ_NO, RAW_FASTQ_UNKNOWN]
    assert raw_fastq_status(False, guessed_false=True) == RAW_FASTQ_GUESS
    assert PROJECT_CONDITION_LABELS == {"include": "纳入条件", "exclude": "排除条件"}
    assert UNNAMED_DATASET == "（未命名）"
    assert len(INTRO_FACT_LABELS) == 8
    browser_copy = (ROOT / "web/static/js/core/copy.js").read_text(encoding="utf-8")
    for label in (*PROJECT_CONDITION_LABELS.values(), *INTRO_FACT_LABELS, UNNAMED_DATASET):
        assert label in browser_copy, f"browser copy contract lost backend label: {label}"
    for relative in (
        "src/dataset_recommender/app/recommend_rows.py",
        "src/dataset_recommender/app/workflow.py",
        "src/dataset_recommender/content/introduction.py",
        "src/dataset_recommender/llm/llm_client.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert 'return "✅ 包含 FASTQ"' not in text


def test_agent_has_one_graph_and_four_registered_suites() -> None:
    agent = (ROOT / "src/dataset_recommender/agent/agent_exec.py").read_text(encoding="utf-8")
    turn = (ROOT / "src/dataset_recommender/agent/turn.py").read_text(encoding="utf-8")
    assert "entry_mode" not in agent
    assert "_route_turn_serial" not in turn
    assert "BIODATA_RAG_CONCURRENT" not in turn
    assert '"rescue": tuple(v for v in ("search.rerun",)' in agent
    assert (ROOT / "prompts/loop_rescue.md").is_file()


def test_retired_outside_agent_llm_flags_are_absent_from_production() -> None:
    tokens = ("rerank_audit", "degrade_with_llm", "action_audit")
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        executable = "".join(
            token.string for token in tokenize.generate_tokens(io.StringIO(text).readline)
            if token.type != tokenize.COMMENT
        )
        for token in tokens:
            assert token not in executable, f"retired outside-Agent path remains in {path}: {token}"


def test_every_online_check_kind_is_registered() -> None:
    expected = {
        str(spec["net_kind"])
        for spec in corpus_curation.CHECK_UPDATE_SOURCES.values()
        if spec.get("net_kind")
    }
    assert expected == set(corpus_curation._ONLINE_CHECK_NET)


def test_wave3_browser_cores() -> None:
    node = os.environ.get("BIODATA_NODE") or shutil.which("node") or shutil.which("node.exe")
    if not node:
        pytest.skip("node unavailable")
    subprocess.run(
        [node, str(ROOT / "tests/js/wave3_ui_core_spec.mjs")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
