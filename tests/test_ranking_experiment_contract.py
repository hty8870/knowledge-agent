# -*- coding: utf-8 -*-
"""排序实验必须可执行、可归因，且半截合同 fail closed。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from dataset_recommender.app.webapp import RecommendRequest, UtteranceRequest, _experiment_response


@pytest.mark.parametrize("model,required", [
    (RecommendRequest, {"query": "lung"}),
    (UtteranceRequest, {"utterance": "lung"}),
])
def test_experiment_contract_is_all_or_none(model, required):
    with pytest.raises(ValidationError):
        model(**required, experiment_id="rank-e1")
    with pytest.raises(ValidationError):
        model(**required, experiment_id="rank-e1", experiment_arm="candidate", propensity=0)

    payload = model(**required, experiment_id="rank-e1", experiment_arm="candidate", propensity=0.2)
    assert _experiment_response(payload) == {"id": "rank-e1", "arm": "candidate", "propensity": 0.2}


def test_observational_request_is_not_labeled_as_experiment():
    assert _experiment_response(RecommendRequest(query="lung")) is None


@pytest.mark.parametrize("field,value", [
    ("experiment_id", ""), ("experiment_id", "bad id"), ("experiment_arm", "bad/arm"),
])
def test_invalid_experiment_identifiers_are_rejected(field, value):
    body = {"query": "人类肺癌", "experiment_id": "rank-e1",
            "experiment_arm": "candidate", "propensity": 0.2}
    body[field] = value
    with pytest.raises(ValidationError):
        RecommendRequest(**body)
