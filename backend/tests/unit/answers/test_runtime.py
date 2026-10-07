from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import policy_api.answers.runtime as runtime_module
from policy_api.answers.model_output import ModelAnswer
from policy_api.answers.runtime import AnswerRuntime
from policy_api.config import Settings
from policy_api.models import Document, DocumentStatus
from policy_api.retrieval.types import RetrievalResult


def settings(tmp_path: Path) -> Settings:
    credential_placeholder = "-".join(("test", "placeholder"))
    return Settings(
        app_env="test",
        database_url="postgresql+psycopg://unused:unused@localhost/runtime_test",
        session_secret="s" * 32,
        frontend_origins="http://localhost:5173",
        upload_root=tmp_path,
        max_upload_bytes=1024,
        model_base_url="https://models.example.test/v1",
        model_api_key=credential_placeholder,
        chat_model="chat-test",
        embedding_model="embedding-test",
        query_variant_max_count=3,
        retrieval_lexical_candidate_limit=7,
        retrieval_vector_candidate_limit=9,
        retrieval_fused_top_k=5,
        evidence_semantic_threshold=0.85,
        evidence_lexical_threshold=0.31,
        evidence_dual_channel_minimum=0.21,
        evidence_max_chunks=4,
    )


def retrieval_result(
    name: str,
    document_id: uuid.UUID,
    *,
    vector_score: float,
) -> RetrievalResult:
    return RetrievalResult(
        chunk_id=uuid.uuid5(uuid.NAMESPACE_DNS, name),
        document_id=document_id,
        document_name="travel.txt",
        text="其他城市住宿标准为350元每晚。",
        page=1,
        heading_path="差旅",
        location="page:1",
        lexical_score=None,
        vector_score=vector_score,
        fused_score=0.01,
        matched_by=("vector",),
    )


def test_runtime_uses_one_production_flow_and_caches_source_activity(
    monkeypatch, tmp_path: Path
) -> None:
    document_id = uuid.uuid4()
    candidates = [
        retrieval_result("first", document_id, vector_score=0.91),
        retrieval_result("second", document_id, vector_score=0.89),
    ]

    class FakeRetrievalService:
        init_kwargs: dict[str, object] = {}
        top_k: int | None = None

        def __init__(self, **kwargs):
            type(self).init_kwargs = kwargs

        def retrieve(self, question: str, *, top_k: int):
            assert question == "南京住宿多少钱？"
            type(self).top_k = top_k
            return candidates

    monkeypatch.setattr(runtime_module, "RetrievalService", FakeRetrievalService)

    class FakeDatabase:
        def __init__(
            self,
            *,
            is_enabled: bool = True,
            status: DocumentStatus = DocumentStatus.ENABLED,
        ) -> None:
            self.get_calls: list[tuple[type[object], uuid.UUID]] = []
            self.document = SimpleNamespace(
                is_enabled=is_enabled,
                status=status,
            )

        def get(self, model, identifier):
            self.get_calls.append((model, identifier))
            return self.document

    database = FakeDatabase()
    generated = 0

    def generate(_messages, *, allowed_citation_ids):
        nonlocal generated
        generated += 1
        identifier = str(candidates[0].chunk_id)
        assert identifier in allowed_citation_ids
        return ModelAnswer(
            "answered",
            "其他城市住宿标准为350元每晚。",
            (identifier,),
        )

    embed_queries = lambda _texts: [[0.1]]  # noqa: E731
    runtime = AnswerRuntime(settings(tmp_path), embed_queries, generate)
    outcome, by_id = runtime.answer_with_evidence(  # type: ignore[arg-type]
        database, "南京住宿多少钱？"
    )

    assert outcome.status == "answered" and generated == 1
    assert set(by_id) == {str(item.chunk_id) for item in candidates}
    assert FakeRetrievalService.top_k == 5
    assert FakeRetrievalService.init_kwargs["embed"] is embed_queries
    assert FakeRetrievalService.init_kwargs["lexical_candidate_limit"] == 7
    assert FakeRetrievalService.init_kwargs["vector_candidate_limit"] == 9
    builder = FakeRetrievalService.init_kwargs["variant_builder"]
    assert builder.max_count == 3  # type: ignore[union-attr]
    assert database.get_calls == [(Document, document_id)]

    for inactive_database in (
        FakeDatabase(is_enabled=False),
        FakeDatabase(status=DocumentStatus.DISABLED),
    ):
        inactive_runtime = AnswerRuntime(
            settings(tmp_path),
            embed_queries,
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
        )
        inactive_outcome, _ = inactive_runtime.answer_with_evidence(  # type: ignore[arg-type]
            inactive_database, "南京住宿多少钱？"
        )
        assert inactive_outcome.status == "abstained"
        assert inactive_outcome.refusal_reason is not None
        assert inactive_database.get_calls == [(Document, document_id)]


def test_runtime_reports_only_retrieval_latency_to_an_optional_observer(
    monkeypatch, tmp_path: Path
) -> None:
    document_id = uuid.uuid4()
    candidate = retrieval_result("timed", document_id, vector_score=0.91)

    class FakeRetrievalService:
        def __init__(self, **_kwargs):
            pass

        def retrieve(self, _question: str, *, top_k: int):
            assert top_k == 5
            return [candidate]

    class FakeDatabase:
        def get(self, _model, _identifier):
            return SimpleNamespace(
                is_enabled=True,
                status=DocumentStatus.ENABLED,
            )

    ticks = iter((10.0, 10.123))
    monkeypatch.setattr(runtime_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(runtime_module.time, "perf_counter", lambda: next(ticks))
    observed: list[int] = []
    runtime = AnswerRuntime(
        settings(tmp_path),
        lambda _texts: [[0.1]],
        lambda *_args, **_kwargs: ModelAnswer(
            "answered",
            "其他城市住宿标准为350元每晚。",
            (str(candidate.chunk_id),),
        ),
        observe_retrieval_ms=observed.append,
    )

    outcome, _ = runtime.answer_with_evidence(  # type: ignore[arg-type]
        FakeDatabase(), "南京住宿多少钱？"
    )

    assert outcome.status == "answered"
    assert observed == [123]
