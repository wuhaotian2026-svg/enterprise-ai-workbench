from __future__ import annotations

import pytest

from policy_api.evaluation.metrics import EvaluationInputError, compute_metrics


def test_metrics_compute_correctness_citation_abstention_and_p95() -> None:
    results = [
        {"expected_status":"answered","actual_status":"answered","facts_passed":True,"citations_passed":True,"latency_ms":100,"human_reviewed":True},
        {"expected_status":"answered","actual_status":"answered","facts_passed":False,"citations_passed":False,"latency_ms":200,"human_reviewed":False},
        {"expected_status":"abstained","actual_status":"abstained","facts_passed":True,"citations_passed":None,"latency_ms":300,"human_reviewed":True},
        {"expected_status":"abstained","actual_status":"answered","facts_passed":False,"citations_passed":False,"latency_ms":400,"human_reviewed":False},
    ]
    metrics = compute_metrics(results)
    assert metrics == {"sample_count":4,"answer_correctness":.5,"citation_support":.5,"correct_abstention":.5,"p95_latency_ms":400,"human_reviewed_count":2,"unreviewed_count":2}


def test_metrics_reject_empty_results_instead_of_reporting_perfect_score() -> None:
    with pytest.raises(EvaluationInputError, match="empty_results"):
        compute_metrics([])
