from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from policy_api.evaluation.metrics import compute_rag_v2_metrics


RagTerminal = Literal["answered", "needs_clarification", "abstained"]
RagActualTerminal = Literal[
    "answered", "needs_clarification", "abstained", "failed"
]

_TERMINALS = {"answered", "needs_clarification", "abstained"}
_CASE_FIELDS = {
    "id",
    "category",
    "question",
    "expected_status",
    "expected_facts",
    "expected_sources",
    "tags",
}
_MANIFEST_FIELDS = {
    "expected_status",
    "required_clarification_topics",
    "must_not_assert",
    "must_not_retrieve_disabled",
}
IMPLEMENTATION_PATHS = (
    "backend/src/policy_api/config.py",
    "backend/src/policy_api/retrieval/query_variants.py",
    "backend/src/policy_api/retrieval/types.py",
    "backend/src/policy_api/retrieval/fusion.py",
    "backend/src/policy_api/retrieval/repository.py",
    "backend/src/policy_api/retrieval/service.py",
    "backend/src/policy_api/answers/schemas.py",
    "backend/src/policy_api/answers/llm_client.py",
    "backend/src/policy_api/answers/evidence.py",
    "backend/src/policy_api/answers/model_output.py",
    "backend/src/policy_api/answers/prompt.py",
    "backend/src/policy_api/answers/citations.py",
    "backend/src/policy_api/answers/service.py",
    "backend/src/policy_api/knowledge/tools.py",
    "backend/src/policy_api/evaluation/rag_v2.py",
    "backend/src/policy_api/evaluation/runner.py",
)
RETRIEVAL_FINGERPRINT_FIELDS = {
    "query_variant_max_count",
    "retrieval_lexical_candidate_limit",
    "retrieval_vector_candidate_limit",
    "retrieval_fused_top_k",
    "retrieval_allow_vector_only_fallback",
    "evidence_semantic_threshold",
    "evidence_lexical_threshold",
    "evidence_dual_channel_minimum",
    "evidence_max_chunks",
}


class RagV2InputError(ValueError):
    pass


class EvaluationFingerprintMismatch(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RagV2CaseContract:
    case_id: str
    expected_status: RagTerminal
    required_clarification_topics: tuple[str, ...]
    must_not_assert: tuple[str, ...]
    must_not_retrieve_disabled: bool


@dataclass(frozen=True, slots=True)
class RagV2Trace:
    actual_status: RagActualTerminal
    answer_text: str | None
    clarification_questions: tuple[str, ...]
    citation_sources: tuple[str, ...]
    retrieved_top_5_sources: tuple[str, ...]
    retrieved_disabled_sources: tuple[str, ...]
    citation_validation_passed: bool | None
    retrieval_latency_ms: int
    end_to_end_latency_ms: int
    failure_code: str | None = None


_CLARIFICATION_ALIASES = {
    "城市档位": ("城市档位", "一线城市", "其他城市", "哪类城市"),
    "住宿晚数": ("住宿几晚", "住几晚", "住宿晚数", "实际住宿"),
    "全天供餐": ("全天供餐", "全天餐食", "全天伙食", "主办方供餐"),
    "展会超标审批": ("展会", "超标", "事前批准", "入住前批准"),
    "入职日期": ("入职日期", "哪天入职", "入职时间"),
}


def _invalid(code: str, case_id: str | None = None) -> RagV2InputError:
    suffix = f":{case_id}" if case_id else ""
    return RagV2InputError(f"{code}{suffix}")


def _string_list(value: object, *, case_id: str) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item.strip() for item in value)
        or len(set(value)) != len(value)
    ):
        raise _invalid("invalid_rag_v2_catalog", case_id)
    return value


def load_rag_v2_cases(path: str | Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                raw = json.loads(line)
                if not isinstance(raw, dict) or set(raw) != _CASE_FIELDS:
                    raise _invalid("invalid_rag_v2_catalog", str(line_number))
                case_id = raw.get("id")
                if not isinstance(case_id, str) or not case_id:
                    raise _invalid("invalid_rag_v2_catalog", str(line_number))
                if (
                    not isinstance(raw.get("category"), str)
                    or not isinstance(raw.get("question"), str)
                    or not raw["question"].strip()
                    or raw.get("expected_status") not in _TERMINALS
                ):
                    raise _invalid("invalid_rag_v2_catalog", case_id)
                _string_list(raw.get("expected_facts"), case_id=case_id)
                _string_list(raw.get("expected_sources"), case_id=case_id)
                tags = _string_list(raw.get("tags"), case_id=case_id)
                if "specific_scenario" not in tags:
                    raise _invalid("invalid_rag_v2_catalog", case_id)
                cases.append(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise _invalid("invalid_rag_v2_catalog") from exc
    ids = [case["id"] for case in cases]
    if not cases or len(ids) != len(set(ids)):
        raise _invalid("invalid_rag_v2_catalog")
    return cases


def load_rag_v2_manifest(
    path: str | Path,
    *,
    cases: Sequence[dict[str, Any]],
) -> dict[str, RagV2CaseContract]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _invalid("invalid_rag_v2_manifest") from exc
    case_ids = [case.get("id") for case in cases]
    if (
        not isinstance(payload, dict)
        or set(payload) != set(case_ids)
        or len(case_ids) != len(set(case_ids))
    ):
        raise _invalid("invalid_rag_v2_manifest")
    contracts: dict[str, RagV2CaseContract] = {}
    for case in cases:
        case_id = str(case["id"])
        raw = payload.get(case_id)
        if not isinstance(raw, dict) or set(raw) != _MANIFEST_FIELDS:
            raise _invalid("invalid_rag_v2_manifest", case_id)
        status = raw.get("expected_status")
        topics = _string_list(
            raw.get("required_clarification_topics"), case_id=case_id
        )
        prohibited = _string_list(raw.get("must_not_assert"), case_id=case_id)
        disabled = raw.get("must_not_retrieve_disabled")
        if (
            status not in _TERMINALS
            or status != case["expected_status"]
            or not isinstance(disabled, bool)
            or (
                status == "needs_clarification"
                and not 1 <= len(topics) <= 3
            )
            or (status != "needs_clarification" and bool(topics))
        ):
            raise _invalid("invalid_rag_v2_manifest", case_id)
        contracts[case_id] = RagV2CaseContract(
            case_id=case_id,
            expected_status=status,
            required_clarification_topics=tuple(topics),
            must_not_assert=tuple(prohibited),
            must_not_retrieve_disabled=disabled,
        )
    return contracts


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _implementation_bundle_sha256(
    *,
    root: Path,
    overrides: Mapping[str, str | bytes],
) -> str:
    digest = hashlib.sha256()
    for relative in IMPLEMENTATION_PATHS:
        override = overrides.get(relative, overrides.get(Path(relative).name))
        content = (
            override.encode("utf-8")
            if isinstance(override, str)
            else override
            if isinstance(override, bytes)
            else (root / relative).read_bytes()
        )
        name = relative.encode("utf-8")
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def build_rag_v2_fingerprint(
    *,
    cases: Sequence[dict[str, Any]],
    manifest_payload: Mapping[str, Any],
    provider_schema: object,
    chat_model: str,
    embedding_model: str,
    retrieval_settings: Mapping[str, Any],
    implementation_root: str | Path | None = None,
    implementation_overrides: Mapping[str, str | bytes] | None = None,
) -> dict[str, object]:
    if set(retrieval_settings) != RETRIEVAL_FINGERPRINT_FIELDS:
        raise _invalid("invalid_rag_v2_fingerprint")
    root = (
        Path(implementation_root)
        if implementation_root is not None
        else Path(__file__).resolve().parents[4]
    )
    return {
        "dataset_sha256": _canonical_sha256(list(cases)),
        "manifest_sha256": _canonical_sha256(dict(manifest_payload)),
        "evaluator_schema_version": 2,
        "implementation_paths": list(IMPLEMENTATION_PATHS),
        "implementation_bundle_sha256": _implementation_bundle_sha256(
            root=root,
            overrides=implementation_overrides or {},
        ),
        "provider_schema_sha256": _canonical_sha256(provider_schema),
        "chat_model": chat_model,
        "embedding_model": embedding_model,
        "retrieval_settings": dict(retrieval_settings),
    }


def load_rag_v2_resumable_results(
    path: str | Path,
    *,
    configuration: Mapping[str, Any],
    fingerprint: Mapping[str, Any],
    allowed_case_ids: set[str],
    force_fresh: bool = False,
) -> dict[str, dict[str, Any]]:
    report_path = Path(path)
    if force_fresh or not report_path.exists():
        return {}
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationFingerprintMismatch(
            "evaluation_fingerprint_mismatch"
        ) from exc
    if (
        not isinstance(report, dict)
        or report.get("schema_version") != 2
        or report.get("fingerprint") != dict(fingerprint)
        or report.get("configuration") != dict(configuration)
    ):
        raise EvaluationFingerprintMismatch("evaluation_fingerprint_mismatch")
    results = report.get("results")
    if not isinstance(results, list):
        raise EvaluationFingerprintMismatch("evaluation_fingerprint_mismatch")
    resumable: dict[str, dict[str, Any]] = {}
    for result in results:
        case_id = result.get("case_id") if isinstance(result, dict) else None
        if (
            not isinstance(case_id, str)
            or case_id not in allowed_case_ids
            or case_id in resumable
        ):
            raise EvaluationFingerprintMismatch("evaluation_fingerprint_mismatch")
        resumable[case_id] = result
    return resumable


def _normalized(value: object) -> str:
    return "".join(
        character.casefold()
        for character in str(value)
        if character.isalnum()
    )


def _is_subsequence(expected: str, actual: str) -> bool:
    position = 0
    for character in actual:
        if position < len(expected) and character == expected[position]:
            position += 1
    return position == len(expected)


def _fact_is_supported(fact: str, answer_text: str) -> bool:
    expected = _normalized(fact)
    answer = _normalized(answer_text)
    return bool(expected) and (
        expected in answer
        or (len(expected) >= 4 and _is_subsequence(expected, answer))
    )


def _clarification_topics(
    questions: Sequence[str],
) -> tuple[set[str], bool]:
    topics: set[str] = set()
    unknown = False
    for question in questions:
        normalized = question.casefold()
        matched = {
            topic
            for topic, aliases in _CLARIFICATION_ALIASES.items()
            if any(alias.casefold() in normalized for alias in aliases)
        }
        if not matched:
            unknown = True
        topics.update(matched)
    return topics, unknown


def score_rag_v2_case(
    case: Mapping[str, Any],
    contract: RagV2CaseContract,
    trace: RagV2Trace,
) -> dict[str, Any]:
    answer_text = trace.answer_text or ""
    expected_sources = set(str(value) for value in case["expected_sources"])
    citation_sources = set(trace.citation_sources)
    retrieved_sources = set(trace.retrieved_top_5_sources)
    expected_facts = [str(value) for value in case["expected_facts"]]
    facts_passed = all(
        _fact_is_supported(fact, answer_text) for fact in expected_facts
    )
    prohibited_asserted = any(
        _normalized(statement) in _normalized(answer_text)
        for statement in contract.must_not_assert
        if _normalized(statement)
    )
    citation_support = (
        expected_sources <= citation_sources
        if trace.actual_status in {"answered", "needs_clarification"}
        else None
    )
    asked_topics, unknown_topic = _clarification_topics(
        trace.clarification_questions
    )
    expected_topics = set(contract.required_clarification_topics)
    clarification_precision = (
        bool(asked_topics)
        and not unknown_topic
        and asked_topics <= expected_topics
        if trace.actual_status == "needs_clarification"
        else None
    )
    clarification_recall = (
        trace.actual_status == "needs_clarification"
        and expected_topics <= asked_topics
        if contract.expected_status == "needs_clarification"
        else None
    )
    answer_correctness = (
        trace.actual_status == "answered"
        and facts_passed
        and citation_support is True
        and trace.citation_validation_passed is True
        and not prohibited_asserted
        if contract.expected_status == "answered"
        else None
    )
    unsupported_definitive = trace.actual_status == "answered" and (
        contract.expected_status != "answered"
        or not facts_passed
        or citation_support is not True
        or trace.citation_validation_passed is not True
        or prohibited_asserted
    )
    return {
        "case_id": contract.case_id,
        "category": case["category"],
        "expected_status": contract.expected_status,
        "actual_status": trace.actual_status,
        "failure_code": trace.failure_code,
        "answer_text": trace.answer_text,
        "clarification_questions": list(trace.clarification_questions),
        "citation_sources": list(trace.citation_sources),
        "retrieved_top_5_sources": list(trace.retrieved_top_5_sources),
        "retrieved_disabled_sources": list(trace.retrieved_disabled_sources),
        "retrieval_recall_at_5_passed": (
            expected_sources <= retrieved_sources if expected_sources else None
        ),
        "specific_terminal_correctness_passed": (
            trace.actual_status == contract.expected_status
        ),
        "answer_correctness_passed": answer_correctness,
        "clarification_precision_passed": clarification_precision,
        "clarification_recall_passed": clarification_recall,
        "citation_support_passed": citation_support,
        "citation_validation_passed": (
            trace.citation_validation_passed
            if trace.actual_status in {"answered", "needs_clarification"}
            else None
        ),
        "correct_abstention_passed": (
            trace.actual_status == "abstained"
            if contract.expected_status == "abstained"
            else None
        ),
        "disabled_retrieval_violation": bool(
            trace.retrieved_disabled_sources
        ),
        "unsupported_definitive_answer": unsupported_definitive,
        "no_evidence_conflict_must_abstain_passed": (
            trace.actual_status == "abstained"
            if contract.expected_status == "abstained"
            else None
        ),
        "prohibited_assertion_detected": prohibited_asserted,
        "retrieval_latency_ms": trace.retrieval_latency_ms,
        "end_to_end_latency_ms": trace.end_to_end_latency_ms,
    }


def _at_least(metrics: Mapping[str, Any], name: str, threshold: float) -> bool:
    metric = metrics.get(name)
    value = metric.get("value") if isinstance(metric, dict) else None
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= threshold


def _at_most(metrics: Mapping[str, Any], name: str, threshold: float) -> bool:
    metric = metrics.get(name)
    value = metric.get("value") if isinstance(metric, dict) else None
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value <= threshold


def rag_v2_quality_gate(metrics: Mapping[str, Any]) -> dict[str, object]:
    checks = {
        "retrieval_recall_at_5": _at_least(metrics, "retrieval_recall_at_5", 0.9),
        "answer_correctness": _at_least(metrics, "answer_correctness", 0.85),
        "citation_support": _at_least(metrics, "citation_support", 0.9),
        "correct_abstention": _at_least(metrics, "correct_abstention", 0.9),
        "specific_terminal_correctness": _at_least(
            metrics, "specific_terminal_correctness", 0.95
        ),
        "clarification_precision": _at_least(
            metrics, "clarification_precision", 0.95
        ),
        "clarification_recall": _at_least(
            metrics, "clarification_recall", 0.95
        ),
        "citation_validation": _at_least(metrics, "citation_validation", 1.0),
        "no_evidence_conflict_must_abstain": _at_least(
            metrics, "no_evidence_conflict_must_abstain", 1.0
        ),
        "disabled_retrieval_violations": _at_most(
            metrics, "disabled_retrieval_violations", 0.0
        ),
        "unsupported_definitive_answers": _at_most(
            metrics, "unsupported_definitive_answers", 0.0
        ),
        "retrieval_p95_ms": _at_most(metrics, "retrieval_p95_ms", 500),
        "end_to_end_p95_ms": _at_most(metrics, "end_to_end_p95_ms", 10_000),
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {"passed": not failed, "failed_metrics": failed, "checks": checks}


_SENSITIVE_CONFIGURATION_KEYS = {
    "api_key",
    "authorization",
    "password",
    "secret",
    "session_secret",
    "token",
}


def _redact_configuration(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _redact_configuration(item)
            for key, item in value.items()
            if str(key).casefold() not in _SENSITIVE_CONFIGURATION_KEYS
        }
    if isinstance(value, list):
        return [_redact_configuration(item) for item in value]
    return value


def build_rag_v2_report(
    *,
    cases: Sequence[dict[str, Any]],
    manifest: Mapping[str, RagV2CaseContract],
    results: Sequence[dict[str, Any]],
    configuration: Mapping[str, Any],
    fingerprint: Mapping[str, Any],
    complete: bool,
) -> dict[str, Any]:
    if set(manifest) != {str(case["id"]) for case in cases}:
        raise _invalid("invalid_rag_v2_manifest")
    metrics = compute_rag_v2_metrics(results)
    return {
        "schema_version": 2,
        "status": "complete" if complete else "in_progress",
        "configuration": _redact_configuration(configuration),
        "fingerprint": dict(fingerprint),
        "coverage": {
            "catalog_total": len(cases),
            "result_total": len(results),
            "by_category": {
                category: sum(case["category"] == category for case in cases)
                for category in sorted({str(case["category"]) for case in cases})
            },
            "by_expected_status": {
                status: sum(case["expected_status"] == status for case in cases)
                for status in sorted(_TERMINALS)
            },
        },
        "results": list(results),
        "metrics": metrics,
        "quality_gate": rag_v2_quality_gate(metrics),
    }
