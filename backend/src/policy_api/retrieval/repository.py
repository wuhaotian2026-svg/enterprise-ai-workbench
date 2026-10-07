from __future__ import annotations

from functools import lru_cache
import re

from sqlalchemy import Integer, String, bindparam, func, literal, or_, select, union_all
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from policy_api.models import Document, DocumentChunk, DocumentStatus
from policy_api.retrieval.errors import RetrievalError
from policy_api.retrieval.types import RetrievalCandidate


_LEXICAL_PROBE_LIMIT = 24
_LEXICAL_TERM = re.compile(r"[A-Za-z0-9]+|[\u3400-\u9fff]+")


def _lexical_probes(query: str) -> tuple[str, ...]:
    """Keep the full query and add bounded, fact-preserving lexical probes.

    PostgreSQL trigrams treat an unsegmented Chinese sentence as one word.  A
    concrete entity inserted before a policy topic can therefore make the full
    sentence fail both trigram predicates.  Adjacent Han probes recover the
    topic overlap without dropping or interpreting any part of the question.
    """
    normalized = " ".join(query.split())
    probes: list[str] = [normalized]
    for term in _LEXICAL_TERM.findall(normalized):
        if term.isascii() or len(term) < 2:
            probes.append(term)
            continue
        probes.extend(term[index : index + 2] for index in range(len(term) - 1))
    return tuple(dict.fromkeys(probe for probe in probes if probe))[:_LEXICAL_PROBE_LIMIT]


def _eligible_expressions():
    return (
        Document.is_enabled.is_(True),
        Document.status == DocumentStatus.ENABLED,
        Document.active_run_id == DocumentChunk.ingestion_run_id,
        func.length(func.btrim(DocumentChunk.text)) > 0,
    )


def _lexical_select_for_slots(probe_slots, *, variant_index: int, limit_clause):
    scores = tuple(
        expression
        for probe in probe_slots
        for expression in (
            func.similarity(DocumentChunk.text, probe),
            func.word_similarity(probe, DocumentChunk.text),
        )
    )
    score = func.greatest(*scores)
    predicates = tuple(
        predicate
        for probe in probe_slots
        for predicate in (
            DocumentChunk.text.op("%")(probe),
            DocumentChunk.text.op("%>")(probe),
        )
    )
    return (
        select(
            literal(variant_index).label("variant_index"),
            DocumentChunk.id.label("chunk_id"),
            Document.id.label("document_id"),
            Document.display_name.label("document_name"),
            DocumentChunk.text.label("text"),
            DocumentChunk.page_number.label("page"),
            DocumentChunk.heading_path.label("heading_path"),
            DocumentChunk.location.label("location"),
            score.label("lexical_score"),
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(*_eligible_expressions(), or_(*predicates))
        .order_by(score.desc(), DocumentChunk.id)
        .limit(limit_clause)
    )


@lru_cache(maxsize=8)
def _lexical_statement_template(variant_count: int):
    if variant_count < 1:
        raise ValueError("lexical_variant_count_invalid")
    statements = tuple(
        _lexical_select_for_slots(
            tuple(
                bindparam(
                    f"variant_{variant_index}_probe_{probe_index}",
                    type_=String(),
                )
                for probe_index in range(_LEXICAL_PROBE_LIMIT)
            ),
            variant_index=variant_index,
            limit_clause=bindparam("lexical_limit", type_=Integer()),
        )
        for variant_index in range(variant_count)
    )
    return union_all(*statements)


def _lexical_statement_parameters(
    queries: tuple[str, ...],
    *,
    limit: int,
) -> dict[str, str | int | None]:
    parameters: dict[str, str | int | None] = {"lexical_limit": limit}
    for variant_index, query in enumerate(queries):
        probes = _lexical_probes(query)
        for probe_index in range(_LEXICAL_PROBE_LIMIT):
            parameters[f"variant_{variant_index}_probe_{probe_index}"] = (
                probes[probe_index] if probe_index < len(probes) else None
            )
    return parameters


class SqlAlchemyRetrievalRepository:
    def __init__(self, database: Session) -> None:
        self.database = database

    @staticmethod
    def _eligible():
        return _eligible_expressions()

    @staticmethod
    def _candidate(
        chunk: DocumentChunk,
        document: Document,
        *,
        lexical_score: float | None = None,
        vector_score: float | None = None,
    ) -> RetrievalCandidate:
        return RetrievalCandidate(chunk_id=chunk.id, document_id=document.id,
            document_name=document.display_name, text=chunk.text, page=chunk.page_number,
            heading_path=chunk.heading_path, location=chunk.location,
            lexical_score=lexical_score, vector_score=vector_score)

    def _lexical_select(self, query: str, limit: int, variant_index: int):
        probes = _lexical_probes(query)
        probe_slots = tuple(
            literal(
                probes[index] if index < len(probes) else None,
                type_=String(),
            )
            for index in range(_LEXICAL_PROBE_LIMIT)
        )
        return _lexical_select_for_slots(
            probe_slots,
            variant_index=variant_index,
            limit_clause=limit,
        )

    def lexical_search_many(
        self,
        queries: tuple[str, ...],
        limit: int,
    ) -> list[list[RetrievalCandidate]]:
        if not queries:
            return []
        statement = _lexical_statement_template(len(queries))
        parameters = _lexical_statement_parameters(queries, limit=limit)
        try:
            with self.database.begin_nested():
                self.database.execute(
                    select(
                        func.set_config(
                            "pg_trgm.similarity_threshold", "0.10", True
                        )
                    )
                )
                self.database.execute(
                    select(
                        func.set_config(
                            "pg_trgm.word_similarity_threshold", "0.20", True
                        )
                    )
                )
                rows = self.database.execute(statement, parameters).all()
        except SQLAlchemyError as exc:
            raise RetrievalError("lexical_search_failed") from exc
        grouped: list[list[RetrievalCandidate]] = [[] for _query in queries]
        for row in rows:
            grouped[int(row.variant_index)].append(
                RetrievalCandidate(
                    chunk_id=row.chunk_id,
                    document_id=row.document_id,
                    document_name=row.document_name,
                    text=row.text,
                    page=row.page,
                    heading_path=row.heading_path,
                    location=row.location,
                    lexical_score=float(row.lexical_score),
                    vector_score=None,
                )
            )
        for ranking in grouped:
            ranking.sort(
                key=lambda candidate: (
                    -float(candidate.lexical_score or 0),
                    str(candidate.chunk_id),
                )
            )
        return grouped

    def lexical_search(self, query: str, limit: int) -> list[RetrievalCandidate]:
        return self.lexical_search_many((query,), limit)[0]

    def vector_search(self, embedding: list[float], limit: int) -> list[RetrievalCandidate]:
        distance = DocumentChunk.embedding.cosine_distance(embedding)
        rows = self.database.execute(
            select(DocumentChunk, Document, distance.label("distance"))
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(*self._eligible(), DocumentChunk.embedding.is_not(None))
            .order_by(distance, DocumentChunk.id)
            .limit(limit)
        ).all()
        return [self._candidate(chunk, document, vector_score=1.0 - float(row_distance))
            for chunk, document, row_distance in rows if row_distance is not None]
