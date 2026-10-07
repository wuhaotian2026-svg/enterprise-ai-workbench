from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from policy_api.answers.evidence import EvidenceSelector
from policy_api.answers.model_output import ModelAnswer
from policy_api.answers.schemas import AnswerOutcome
from policy_api.answers.service import AnswerService
from policy_api.config import Settings
from policy_api.models import Document, DocumentStatus
from policy_api.retrieval.query_variants import QueryVariantBuilder
from policy_api.retrieval.repository import SqlAlchemyRetrievalRepository
from policy_api.retrieval.service import RetrievalService
from policy_api.retrieval.types import RetrievalResult


@dataclass(frozen=True, slots=True)
class AnswerRuntime:
    settings: Settings
    embed_queries: Callable[[list[str]], list[list[float]]]
    generate: Callable[..., ModelAnswer]
    observe_retrieval_ms: Callable[[int], None] | None = None

    def answer_with_evidence(
        self,
        database: Session,
        question: str,
    ) -> tuple[AnswerOutcome, dict[str, RetrievalResult]]:
        repository = SqlAlchemyRetrievalRepository(database)
        retrieval = RetrievalService(
            repository=repository,
            embed=self.embed_queries,
            variant_builder=QueryVariantBuilder(
                self.settings.query_variant_max_count
            ),
            lexical_candidate_limit=(
                self.settings.retrieval_lexical_candidate_limit
            ),
            vector_candidate_limit=self.settings.retrieval_vector_candidate_limit,
            allow_vector_only_fallback=(
                self.settings.retrieval_allow_vector_only_fallback
            ),
        )
        retrieval_started = time.perf_counter()
        retrieved = retrieval.retrieve(
            question, top_k=self.settings.retrieval_fused_top_k
        )
        retrieval_ms = max(
            0,
            round((time.perf_counter() - retrieval_started) * 1000),
        )
        if self.observe_retrieval_ms is not None:
            self.observe_retrieval_ms(retrieval_ms)
        by_id = {str(item.chunk_id): item for item in retrieved}
        active_cache: dict[UUID, bool] = {}

        def source_is_active(document_id: UUID) -> bool:
            if document_id not in active_cache:
                document = database.get(Document, document_id)
                active_cache[document_id] = bool(
                    document
                    and document.is_enabled
                    and document.status == DocumentStatus.ENABLED
                )
            return active_cache[document_id]

        selector = EvidenceSelector(
            semantic_threshold=self.settings.evidence_semantic_threshold,
            lexical_threshold=self.settings.evidence_lexical_threshold,
            dual_channel_minimum=self.settings.evidence_dual_channel_minimum,
            max_chunks=self.settings.evidence_max_chunks,
        )
        outcome = AnswerService(
            retrieve=lambda _question: retrieved,
            source_is_active=source_is_active,
            generate=self.generate,
            selector=selector,
        ).answer(question)
        return outcome, by_id
