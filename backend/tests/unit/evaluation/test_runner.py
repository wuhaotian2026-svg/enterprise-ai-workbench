from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from policy_api.answers.model_output import ModelAnswerError
from policy_api.evaluation import runner as runner_module
from policy_api.evaluation.rag_v2 import (
    EvaluationFingerprintMismatch,
    RagV2CaseContract,
    RagV2Trace,
)
from policy_api.evaluation.runner import (
    EvaluationRunner,
    RagV2EvaluationRunner,
    RunnerConfigurationError,
)


def cases() -> list[dict]:
    return [
        {"id":"a","question":"q1","expected_status":"answered","expected_facts":["500元"],"expected_sources":["p.md"],"tags":["train"]},
        {"id":"b","question":"q2","expected_status":"abstained","expected_facts":[],"expected_sources":[],"tags":["train"]},
        {"id":"h","question":"hidden","expected_status":"answered","expected_facts":["secret"],"expected_sources":["p.md"],"tags":["holdout"]},
    ]


def test_runner_persists_each_case_resumes_and_redacts_configuration(tmp_path: Path) -> None:
    output = tmp_path / "report.json"; calls: list[str] = []
    def evaluate(case: dict) -> dict:
        calls.append(case["id"])
        return {"actual_status":case["expected_status"],"answer_text":"500元","citation_sources":case["expected_sources"],"latency_ms":10}
    runner = EvaluationRunner(evaluate=evaluate, configuration={"chat_model":"demo","api_key":"do-not-write","code_version":"abc"})
    first = runner.run(cases()[:1], output)
    second = runner.run(cases()[:2], output)
    assert calls == ["a", "b"] and len(second["results"]) == 2
    assert second["results"][0]["answer_text"] == "500元"
    assert second["results"][0]["citation_sources"] == ["p.md"]
    assert first["configuration"] == {"chat_model":"demo","code_version":"abc"}
    assert "do-not-write" not in output.read_text(encoding="utf-8")


def test_runner_excludes_holdout_from_tuning_and_requires_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []
    runner = EvaluationRunner(evaluate=lambda case: seen.append(case) or {"actual_status":"abstained","answer_text":None,"citation_sources":[],"latency_ms":1}, configuration={})
    runner.tuning_cases(cases())
    assert [case["id"] for case in seen] == ["a", "b"]
    assert all("expected_status" not in case and "expected_facts" not in case for case in seen)
    for key in ("DATABASE_URL", "MODEL_BASE_URL", "MODEL_API_KEY", "CHAT_MODEL", "EMBEDDING_MODEL"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RunnerConfigurationError, match="runtime_not_configured"):
        EvaluationRunner.from_environment()


def test_environment_factory_wires_runtime_and_close_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[str] = []
    for key in ("DATABASE_URL", "MODEL_BASE_URL", "MODEL_API_KEY", "CHAT_MODEL", "EMBEDDING_MODEL"):
        monkeypatch.setenv(key, "configured")
    evaluator = lambda case: {
        "actual_status": "abstained", "answer_text": None,
        "citation_sources": [], "latency_ms": 1,
    }
    monkeypatch.setattr(
        runner_module,
        "_build_environment_runtime",
        lambda: (evaluator, {"chat_model": "real", "api_key": "hidden"}, lambda: closed.append("closed")),
    )
    runtime = EvaluationRunner.from_environment()
    assert runtime.evaluate({"question": "q"})["actual_status"] == "abstained"
    assert runtime.configuration == {"chat_model": "real"}
    runtime.close()
    runtime.close()
    assert closed == ["closed"]


def test_uploaded_txt_source_names_are_reported_as_dataset_markdown_names() -> None:
    assert runner_module._evaluation_source_name("员工休假与考勤制度.txt") == "员工休假与考勤制度.md"
    assert runner_module._evaluation_source_name("policy.PDF") == "policy.PDF"


def test_fact_matching_normalizes_spacing_and_allows_explanatory_words() -> None:
    assert runner_module._fact_is_supported("500元", "每晚最多报销 500 元。")
    assert runner_module._fact_is_supported("最小0.5个工作日", "最小申请单位为 0.5 个工作日。")
    assert runner_module._fact_is_supported("应发起采购", "应按规定发起采购申请。")
    assert runner_module._fact_is_supported("升级须事前书面批准", "升级须在购票前取得书面批准。")
    assert runner_module._fact_is_supported("不得先买后补", "不得先购买后报销。")
    assert not runner_module._fact_is_supported("500元", "上限为5000元。")
    assert not runner_module._fact_is_supported("升级须事前书面批准", "原则上应乘坐经济舱。")


def test_environment_runtime_delegates_pipeline_assembly_to_answer_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[tuple[object, object, object]] = []
    model_kwargs: list[dict[str, object]] = []

    class Secret:
        def get_secret_value(self) -> str:
            return "runtime-placeholder"

    settings = SimpleNamespace(
        database_url="postgresql://placeholder",
        resolved_embedding_base_url="http://embedding.invalid",
        resolved_embedding_api_key="runtime-placeholder",
        embedding_model="multilingual-e5-small",
        embedding_dimension=384,
        model_timeout_seconds=5,
        embedding_query_prefix="query: ",
        embedding_passage_prefix="passage: ",
        model_base_url="http://model.invalid",
        model_api_key=Secret(),
        chat_model="deepseek-v4-flash",
        model_disable_thinking=True,
        retrieval_top_k=5,
        evidence_threshold=0.1,
        query_variant_max_count=3,
        retrieval_lexical_candidate_limit=20,
        retrieval_vector_candidate_limit=20,
        retrieval_fused_top_k=5,
        retrieval_allow_vector_only_fallback=False,
        evidence_semantic_threshold=0.7,
        evidence_lexical_threshold=0.2,
        evidence_dual_channel_minimum=0.1,
        evidence_max_chunks=5,
    )

    class Engine:
        def dispose(self) -> None:
            pass

    class SessionContext:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args: object) -> None:
            pass

    class Embedding:
        def __init__(self, **_kwargs: object) -> None:
            self.embed_queries = lambda values: [[0.0] * 384 for _ in values]

        def close(self) -> None:
            pass

    class Model:
        def __init__(self, **kwargs: object) -> None:
            model_kwargs.append(kwargs)
            self.generate = lambda **_kwargs: None

        def close(self) -> None:
            pass

    class Runtime:
        def __init__(
            self,
            *,
            settings: object,
            embed_queries: object,
            generate: object,
            observe_retrieval_ms: object,
        ) -> None:
            constructed.append((settings, embed_queries, generate))

        def answer_with_evidence(self, _database: object, question: str) -> tuple[object, dict]:
            assert question == "具体制度问题"
            return (
                SimpleNamespace(
                    status="abstained",
                    text=None,
                    citations=(),
                    refusal_reason=None,
                    evidence_score=None,
                ),
                {},
            )

    monkeypatch.setattr(runner_module, "Settings", lambda: settings)
    monkeypatch.setattr(runner_module, "create_database_engine", lambda _url: Engine())
    monkeypatch.setattr(
        runner_module,
        "create_session_factory",
        lambda _engine: lambda: SessionContext(),
    )
    monkeypatch.setattr(runner_module, "EmbeddingClient", Embedding)
    monkeypatch.setattr(runner_module, "AnswerModelClient", Model)
    monkeypatch.setattr(runner_module, "AnswerRuntime", Runtime)

    evaluate, configuration, close = runner_module._build_environment_runtime()
    result = evaluate({"question": "具体制度问题"})
    close()

    assert len(constructed) == 1
    assert constructed[0][0] is settings
    assert model_kwargs[0]["disable_thinking"] is True
    assert configuration["model_disable_thinking"] is True
    assert result["actual_status"] == "abstained"


def test_rag_v2_runner_persists_each_case_resumes_and_hides_expectations(
    tmp_path: Path,
) -> None:
    evaluation_cases = [
        {
            "id": "RAG-ONE",
            "category": "travel",
            "question": "具体问题一",
            "expected_status": "answered",
            "expected_facts": ["350元"],
            "expected_sources": ["差旅费用管理制度.md"],
            "tags": ["specific_scenario"],
        },
        {
            "id": "RAG-TWO",
            "category": "travel",
            "question": "具体问题二",
            "expected_status": "abstained",
            "expected_facts": [],
            "expected_sources": [],
            "tags": ["specific_scenario"],
        },
    ]
    contracts = {
        "RAG-ONE": RagV2CaseContract(
            "RAG-ONE", "answered", (), (), False
        ),
        "RAG-TWO": RagV2CaseContract(
            "RAG-TWO", "abstained", (), (), False
        ),
    }
    seen: list[dict] = []

    def evaluate(case: dict) -> RagV2Trace:
        seen.append(case)
        answered = case["id"] == "RAG-ONE"
        return RagV2Trace(
            actual_status="answered" if answered else "abstained",
            answer_text="其他城市住宿标准为350元。" if answered else None,
            clarification_questions=(),
            citation_sources=("差旅费用管理制度.md",) if answered else (),
            retrieved_top_5_sources=("差旅费用管理制度.md",) if answered else (),
            retrieved_disabled_sources=(),
            citation_validation_passed=True if answered else None,
            retrieval_latency_ms=20,
            end_to_end_latency_ms=200,
        )

    output = tmp_path / "rag-v2.json"
    runner = RagV2EvaluationRunner(
        cases=evaluation_cases,
        manifest=contracts,
        evaluate=evaluate,
        configuration={"mode": "real_model"},
        fingerprint={"dataset_sha256": "a" * 64},
    )
    report = runner.run(output)
    resumed = runner.run(output)

    assert len(seen) == 2
    assert all(
        "expected_status" not in case
        and "expected_facts" not in case
        and "expected_sources" not in case
        for case in seen
    )
    assert report["status"] == "complete"
    assert resumed["results"] == report["results"]

    stale = RagV2EvaluationRunner(
        cases=evaluation_cases,
        manifest=contracts,
        evaluate=evaluate,
        configuration={"mode": "real_model"},
        fingerprint={"dataset_sha256": "b" * 64},
    )
    with pytest.raises(
        EvaluationFingerprintMismatch,
        match="evaluation_fingerprint_mismatch",
    ):
        stale.run(output)


def test_rag_v2_runner_records_model_failure_and_continues(
    tmp_path: Path,
) -> None:
    evaluation_cases = [
        {
            "id": "RAG-FAIL",
            "category": "safety",
            "question": "第一个问题",
            "expected_status": "abstained",
            "expected_facts": [],
            "expected_sources": [],
            "tags": ["specific_scenario"],
        },
        {
            "id": "RAG-NEXT",
            "category": "safety",
            "question": "第二个问题",
            "expected_status": "abstained",
            "expected_facts": [],
            "expected_sources": [],
            "tags": ["specific_scenario"],
        },
    ]
    contracts = {
        case["id"]: RagV2CaseContract(
            case["id"], "abstained", (), (), False
        )
        for case in evaluation_cases
    }
    seen: list[str] = []

    def evaluate(case: dict) -> RagV2Trace:
        seen.append(case["id"])
        if case["id"] == "RAG-FAIL":
            raise ModelAnswerError("model_output_invalid")
        return RagV2Trace(
            actual_status="abstained",
            answer_text=None,
            clarification_questions=(),
            citation_sources=(),
            retrieved_top_5_sources=(),
            retrieved_disabled_sources=(),
            citation_validation_passed=None,
            retrieval_latency_ms=10,
            end_to_end_latency_ms=20,
        )

    report = RagV2EvaluationRunner(
        cases=evaluation_cases,
        manifest=contracts,
        evaluate=evaluate,
        configuration={"mode": "real_model"},
        fingerprint={"dataset_sha256": "a" * 64},
    ).run(tmp_path / "rag-v2-provider-failure.json")

    assert seen == ["RAG-FAIL", "RAG-NEXT"]
    assert report["status"] == "complete"
    failed, succeeded = report["results"]
    assert failed["actual_status"] == "failed"
    assert failed["failure_code"] == "model_output_invalid"
    assert failed["specific_terminal_correctness_passed"] is False
    assert failed["correct_abstention_passed"] is False
    assert succeeded["actual_status"] == "abstained"
    assert succeeded["correct_abstention_passed"] is True
    assert report["quality_gate"]["passed"] is False


def test_rag_v2_environment_factory_adapts_production_runtime_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    evaluation_cases = [
        {
            "id": "RAG-ENV",
            "category": "travel",
            "question": "南京出差三天多少钱？",
            "expected_status": "needs_clarification",
            "expected_facts": [],
            "expected_sources": ["差旅费用管理制度.md"],
            "tags": ["specific_scenario"],
        }
    ]
    contracts = {
        "RAG-ENV": RagV2CaseContract(
            "RAG-ENV",
            "needs_clarification",
            ("城市档位", "住宿晚数", "全天供餐"),
            ("最终总额",),
            False,
        )
    }
    closed: list[str] = []

    def evaluate(_case: dict) -> dict:
        return {
            "actual_status": "needs_clarification",
            "answer_text": "制度给出了住宿和餐补标准。",
            "clarification_questions": [
                "适用哪一城市档位？",
                "实际住宿几晚？",
                "是否提供全天餐食？",
            ],
            "citation_sources": ["差旅费用管理制度.md"],
            "retrieved_top_5_sources": ["差旅费用管理制度.md"],
            "retrieved_disabled_sources": [],
            "citation_validation_passed": True,
            "retrieval_latency_ms": 30,
            "latency_ms": 300,
        }

    monkeypatch.setattr(
        runner_module,
        "_build_environment_runtime",
        lambda: (
            evaluate,
            {"chat_model": "deepseek-v4-flash"},
            lambda: closed.append("closed"),
        ),
    )
    runner = RagV2EvaluationRunner.from_environment(
        cases=evaluation_cases,
        manifest=contracts,
        fingerprint={"dataset_sha256": "c" * 64},
    )

    report = runner.run(tmp_path / "environment.json")
    runner.close()
    runner.close()

    assert report["results"][0]["clarification_recall_passed"] is True
    assert report["results"][0]["retrieval_latency_ms"] == 30
    assert closed == ["closed"]


def test_rag_v2_warms_once_before_incomplete_cases_and_not_on_complete_resume(
    tmp_path: Path,
) -> None:
    evaluation_cases = [
        {
            "id": "RAG-WARM-CONTRACT",
            "category": "travel",
            "question": "具体制度问题",
            "expected_status": "abstained",
            "expected_facts": [],
            "expected_sources": [],
            "tags": ["specific_scenario"],
        }
    ]
    contracts = {
        "RAG-WARM-CONTRACT": RagV2CaseContract(
            "RAG-WARM-CONTRACT", "abstained", (), (), False
        )
    }
    events: list[str] = []

    def evaluate(_case: dict) -> RagV2Trace:
        events.append("case")
        return RagV2Trace(
            actual_status="abstained",
            answer_text=None,
            clarification_questions=(),
            citation_sources=(),
            retrieved_top_5_sources=(),
            retrieved_disabled_sources=(),
            citation_validation_passed=None,
            retrieval_latency_ms=10,
            end_to_end_latency_ms=20,
        )

    runner = RagV2EvaluationRunner(
        cases=evaluation_cases,
        manifest=contracts,
        evaluate=evaluate,
        configuration={"performance_measurement": "warmed_steady_state"},
        fingerprint={"dataset_sha256": "d" * 64},
        warmup=lambda: events.append("warmup"),
    )
    output = tmp_path / "warm.json"

    first = runner.run(output)
    second = runner.run(output)

    assert events == ["warmup", "case"]
    assert len(first["results"]) == len(second["results"]) == 1
    assert first["configuration"]["performance_measurement"] == "warmed_steady_state"
