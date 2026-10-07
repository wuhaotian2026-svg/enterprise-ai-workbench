from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from policy_api.answers.model_output import ModelAnswerError
from policy_api.answers.llm_client import AnswerModelClient
from policy_api.answers.runtime import AnswerRuntime
from policy_api.config import Settings
from policy_api.database import create_database_engine, create_session_factory
from policy_api.ingestion.embedding_client import EmbeddingClient
from policy_api.models import Document, DocumentStatus
from policy_api.evaluation.metrics import compute_metrics
from policy_api.evaluation.rag_v2 import (
    RagV2CaseContract,
    RagV2Trace,
    build_rag_v2_report,
    load_rag_v2_resumable_results,
    score_rag_v2_case,
)


class RunnerConfigurationError(RuntimeError):
    pass


SENSITIVE_KEYS = {"api_key", "model_api_key", "session_secret", "password", "token"}
EXPECTED_KEYS = {"expected_status", "expected_facts", "expected_sources"}
MODEL_FAILURE_CODES = {
    "model_timeout",
    "model_rate_limited",
    "model_request_failed",
    "model_content_filtered",
    "model_output_invalid",
}


def _redact(configuration: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in configuration.items() if key.lower() not in SENSITIVE_KEYS}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _evaluation_source_name(display_name: str) -> str:
    """Match uploaded TXT display names to the Markdown names in the fixed dataset."""
    path = Path(display_name)
    return path.with_suffix(".md").name if path.suffix.lower() == ".txt" else path.name


def _fact_is_supported(fact: Any, answer_text: str) -> bool:
    def normalize(value: Any) -> str:
        return re.sub(r"[^0-9A-Za-z\u3400-\u9fff.]", "", str(value)).lower()

    expected = normalize(fact)
    answer = normalize(answer_text)
    if expected in answer:
        return True
    if len(expected) < 4:
        return False
    expected_numbers = re.findall(r"\d+(?:\.\d+)?", expected)
    answer_numbers = set(re.findall(r"\d+(?:\.\d+)?", answer))
    if any(number not in answer_numbers for number in expected_numbers):
        return False
    previous = [0] * (len(answer) + 1)
    for expected_character in expected:
        current = [0]
        for index, answer_character in enumerate(answer, start=1):
            if expected_character == answer_character:
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(previous[index], current[-1]))
        previous = current
    return previous[-1] >= len(expected) - 1


def _build_environment_runtime() -> tuple[
    Callable[[dict[str, Any]], dict[str, Any]], dict[str, Any], Callable[[], None]
]:
    settings = Settings()  # type: ignore[call-arg]
    engine = create_database_engine(settings.database_url)
    sessions = create_session_factory(engine)
    embedding_client: EmbeddingClient | None = None
    answer_client: AnswerModelClient | None = None
    try:
        embedding_client = EmbeddingClient(
            base_url=settings.resolved_embedding_base_url,
            api_key=settings.resolved_embedding_api_key,
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
            timeout=settings.model_timeout_seconds,
            query_prefix=settings.embedding_query_prefix,
            passage_prefix=settings.embedding_passage_prefix,
        )
        answer_client = AnswerModelClient(
            base_url=str(settings.model_base_url),
            api_key=settings.model_api_key.get_secret_value(),
            model=settings.chat_model,
            timeout=settings.model_timeout_seconds,
            disable_thinking=settings.model_disable_thinking,
        )
    except Exception:
        if embedding_client is not None:
            embedding_client.close()
        if answer_client is not None:
            answer_client.close()
        engine.dispose()
        raise

    retrieval_latencies: list[int] = []
    runtime = AnswerRuntime(
        settings=settings,
        embed_queries=embedding_client.embed_queries,
        generate=answer_client.generate,
        observe_retrieval_ms=retrieval_latencies.append,
    )

    def evaluate(case: dict[str, Any]) -> dict[str, Any]:
        question = str(case["question"])
        started = time.perf_counter()
        retrieval_latencies.clear()
        with sessions() as database:
            outcome, retrieved = runtime.answer_with_evidence(database, question)
            retrieved_results = list(retrieved.values())
            citation_sources = [
                _evaluation_source_name(retrieved[citation].document_name)
                for citation in outcome.citations
                if citation in retrieved
            ]
            retrieved_sources = [
                _evaluation_source_name(item.document_name)
                for item in retrieved_results[:5]
            ]
            disabled_sources: list[str] = []
            document_cache: dict[object, object] = {}
            for item in retrieved_results:
                if item.document_id not in document_cache:
                    document_cache[item.document_id] = database.get(
                        Document, item.document_id
                    )
                document = document_cache[item.document_id]
                if not (
                    document
                    and document.is_enabled
                    and document.status == DocumentStatus.ENABLED
                ):
                    disabled_sources.append(
                        _evaluation_source_name(item.document_name)
                    )
        return {
            "actual_status": outcome.status,
            "answer_text": outcome.text,
            "clarification_questions": list(
                getattr(outcome, "clarification_questions", ())
            ),
            "citation_sources": list(dict.fromkeys(citation_sources)),
            "retrieved_top_5_sources": list(dict.fromkeys(retrieved_sources)),
            "retrieved_disabled_sources": list(dict.fromkeys(disabled_sources)),
            "citation_validation_passed": (
                True
                if outcome.status in {"answered", "needs_clarification"}
                else None
            ),
            "refusal_reason": outcome.refusal_reason.value if outcome.refusal_reason else None,
            "evidence_score": outcome.evidence_score,
            "retrieval_latency_ms": (
                retrieval_latencies[-1] if retrieval_latencies else 0
            ),
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }

    def close() -> None:
        embedding_client.close()
        answer_client.close()
        engine.dispose()

    configuration = {
        "chat_model": settings.chat_model,
        "model_disable_thinking": settings.model_disable_thinking,
        "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.embedding_dimension,
        "query_variant_max_count": settings.query_variant_max_count,
        "retrieval_lexical_candidate_limit": (
            settings.retrieval_lexical_candidate_limit
        ),
        "retrieval_vector_candidate_limit": settings.retrieval_vector_candidate_limit,
        "retrieval_fused_top_k": settings.retrieval_fused_top_k,
        "retrieval_allow_vector_only_fallback": (
            settings.retrieval_allow_vector_only_fallback
        ),
        "evidence_semantic_threshold": settings.evidence_semantic_threshold,
        "evidence_lexical_threshold": settings.evidence_lexical_threshold,
        "evidence_dual_channel_minimum": settings.evidence_dual_channel_minimum,
        "evidence_max_chunks": settings.evidence_max_chunks,
        "model_timeout_seconds": settings.model_timeout_seconds,
        "source_suffix_normalization": ".txt -> .md for evaluation reporting only",
    }
    return evaluate, configuration, close


class EvaluationRunner:
    def __init__(self, *, evaluate: Callable[[dict[str, Any]], dict[str, Any]], configuration: dict[str, Any],
                 close: Callable[[], None] | None = None) -> None:
        self.evaluate = evaluate
        self.configuration = _redact(configuration)
        self._close = close
        self._closed = False

    @classmethod
    def from_environment(cls) -> "EvaluationRunner":
        required = ("DATABASE_URL", "MODEL_BASE_URL", "MODEL_API_KEY", "CHAT_MODEL", "EMBEDDING_MODEL")
        if not all(os.getenv(key) for key in required):
            raise RunnerConfigurationError("runtime_not_configured")
        try:
            evaluate, configuration, close = _build_environment_runtime()
        except Exception as exc:
            raise RunnerConfigurationError("runtime_initialization_failed") from exc
        return cls(evaluate=evaluate, configuration=configuration, close=close)

    def close(self) -> None:
        if not self._closed and self._close is not None:
            self._closed = True
            self._close()

    def tuning_cases(self, cases: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        outputs = []
        for case in cases:
            if "holdout" in case.get("tags", []):
                continue
            blind = {key: value for key, value in case.items() if key not in EXPECTED_KEYS}
            outputs.append(self.evaluate(blind))
        return outputs

    def run(self, cases: Sequence[dict[str, Any]], output: Path) -> dict[str, Any]:
        report: dict[str, Any] = {"configuration": self.configuration, "results": []}
        if output.exists():
            report = json.loads(output.read_text(encoding="utf-8"))
            report["configuration"] = self.configuration
        completed = {item["case_id"] for item in report["results"]}
        for case in cases:
            if case["id"] in completed:
                continue
            actual = self.evaluate(case)
            answer_text = actual.get("answer_text") or ""
            expected_facts = case["expected_facts"]
            expected_sources = set(case["expected_sources"])
            actual_sources = set(actual.get("citation_sources", []))
            result = {
                "case_id": case["id"], "expected_status": case["expected_status"],
                "actual_status": actual["actual_status"],
                "answer_text": answer_text or None,
                "citation_sources": sorted(actual_sources),
                "refusal_reason": actual.get("refusal_reason"),
                "evidence_score": actual.get("evidence_score"),
                "facts_passed": all(_fact_is_supported(fact, answer_text) for fact in expected_facts),
                "citations_passed": expected_sources <= actual_sources if case["expected_status"] == "answered" else None,
                "latency_ms": int(actual["latency_ms"]), "human_reviewed": False,
            }
            report["results"].append(result)
            _atomic_write(output, report)
        report["metrics"] = compute_metrics(report["results"])
        _atomic_write(output, report)
        return report


class RagV2EvaluationRunner:
    def __init__(
        self,
        *,
        cases: Sequence[dict[str, Any]],
        manifest: dict[str, RagV2CaseContract],
        evaluate: Callable[[dict[str, Any]], RagV2Trace],
        configuration: dict[str, Any],
        fingerprint: dict[str, Any],
        close: Callable[[], None] | None = None,
        warmup: Callable[[], None] | None = None,
    ) -> None:
        case_ids = {str(case.get("id")) for case in cases}
        if case_ids != set(manifest):
            raise RunnerConfigurationError("rag_v2_catalog_manifest_mismatch")
        self.cases = tuple(cases)
        self.manifest = dict(manifest)
        self.evaluate = evaluate
        self.configuration = _redact(configuration)
        self.fingerprint = dict(fingerprint)
        self._close = close
        self._warmup = warmup
        self._warmed = False
        self._closed = False

    @classmethod
    def from_environment(
        cls,
        *,
        cases: Sequence[dict[str, Any]],
        manifest: dict[str, RagV2CaseContract],
        fingerprint: dict[str, Any],
    ) -> "RagV2EvaluationRunner":
        try:
            evaluate, configuration, close = _build_environment_runtime()
        except Exception as exc:
            raise RunnerConfigurationError("runtime_initialization_failed") from exc

        def evaluate_trace(case: dict[str, Any]) -> RagV2Trace:
            actual = evaluate(case)
            return RagV2Trace(
                actual_status=actual["actual_status"],
                answer_text=actual.get("answer_text"),
                clarification_questions=tuple(
                    actual.get("clarification_questions", ())
                ),
                citation_sources=tuple(actual.get("citation_sources", ())),
                retrieved_top_5_sources=tuple(
                    actual.get("retrieved_top_5_sources", ())
                ),
                retrieved_disabled_sources=tuple(
                    actual.get("retrieved_disabled_sources", ())
                ),
                citation_validation_passed=actual.get(
                    "citation_validation_passed"
                ),
                retrieval_latency_ms=int(actual["retrieval_latency_ms"]),
                end_to_end_latency_ms=int(actual["latency_ms"]),
            )

        def warmup() -> None:
            evaluate(
                {
                    "id": "RAG-PERFORMANCE-WARMUP",
                    "category": "performance_warmup",
                    "question": "请问企业制度中的费用审批、出差、休假期限和报销标准如何规定？",
                    "tags": ["performance_warmup"],
                }
            )

        configuration = dict(configuration)
        configuration.update(
            {
                "performance_measurement": "warmed_steady_state",
                "warmup_runs": 1,
            }
        )

        return cls(
            cases=cases,
            manifest=manifest,
            evaluate=evaluate_trace,
            configuration=configuration,
            fingerprint=fingerprint,
            close=close,
            warmup=warmup,
        )

    def close(self) -> None:
        if not self._closed and self._close is not None:
            self._closed = True
            self._close()

    def run(self, output: Path) -> dict[str, Any]:
        completed = load_rag_v2_resumable_results(
            output,
            configuration=self.configuration,
            fingerprint=self.fingerprint,
            allowed_case_ids=set(self.manifest),
        )
        results = list(completed.values())
        has_incomplete_cases = any(
            str(case["id"]) not in completed for case in self.cases
        )
        if has_incomplete_cases and not self._warmed and self._warmup is not None:
            try:
                self._warmup()
            except Exception as exc:
                raise RunnerConfigurationError("rag_v2_warmup_failed") from exc
            self._warmed = True
        for case in self.cases:
            case_id = str(case["id"])
            if case_id in completed:
                continue
            blind = {
                key: case[key]
                for key in ("id", "category", "question", "tags")
            }
            case_started = time.perf_counter()
            try:
                trace = self.evaluate(blind)
            except ModelAnswerError as exc:
                code = str(exc)
                trace = RagV2Trace(
                    actual_status="failed",
                    answer_text=None,
                    clarification_questions=(),
                    citation_sources=(),
                    retrieved_top_5_sources=(),
                    retrieved_disabled_sources=(),
                    citation_validation_passed=None,
                    retrieval_latency_ms=0,
                    end_to_end_latency_ms=max(
                        0,
                        round((time.perf_counter() - case_started) * 1000),
                    ),
                    failure_code=(
                        code if code in MODEL_FAILURE_CODES else "model_failure"
                    ),
                )
            result = score_rag_v2_case(
                case,
                self.manifest[case_id],
                trace,
            )
            results.append(result)
            partial = build_rag_v2_report(
                cases=self.cases,
                manifest=self.manifest,
                results=results,
                configuration=self.configuration,
                fingerprint=self.fingerprint,
                complete=False,
            )
            _atomic_write(output, partial)
        report = build_rag_v2_report(
            cases=self.cases,
            manifest=self.manifest,
            results=results,
            configuration=self.configuration,
            fingerprint=self.fingerprint,
            complete=True,
        )
        _atomic_write(output, report)
        return report
