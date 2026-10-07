from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from policy_api.ingestion.embedding_client import EmbeddingError
from policy_api.ingestion.indexer import persist_index_atomically, prepare_chunks
from policy_api.ingestion.parsers.base import ParseError
from policy_api.ingestion.parsers.factory import parser_for
from policy_api.models import Document, DocumentStatus, IngestionRun, IngestionStage, IngestionStatus


SAFE_RUNNING_STAGES = {
    IngestionStage.VALIDATING,
    IngestionStage.PARSING,
    IngestionStage.CHUNKING,
    IngestionStage.EMBEDDING,
}


def is_safe_to_resume(status: IngestionStatus, stage: IngestionStage) -> bool:
    return status == IngestionStatus.QUEUED or (
        status == IngestionStatus.RUNNING and stage in SAFE_RUNNING_STAGES
    )


def claimable_runs(runs: Iterable[IngestionRun]) -> list[IngestionRun]:
    return [run for run in runs if is_safe_to_resume(run.status, run.stage)]


class IngestionExecutor:
    def __init__(self, *, session_factory: sessionmaker[Session], upload_root: Path,
                 embed: Callable[[list[str]], list[list[float]]], batch_size: int = 32,
                 max_chars: int = 1200) -> None:
        self._sessions = session_factory
        self._root = upload_root.resolve()
        self._embed = embed
        self._batch_size = batch_size
        self._max_chars = max_chars

    def process_next(self) -> bool:
        claimed = self._claim_next()
        if claimed is None:
            return False
        run_id, document_id, storage_key = claimed
        try:
            source = (self._root / storage_key).resolve()
            if source.parent != self._root:
                raise ParseError("storage_path_outside_root")
            content = source.read_bytes()
            parser = parser_for(source.suffix)
            self._set_stage(run_id, IngestionStage.PARSING)
            blocks = parser(content)
        except (ParseError, OSError, ValueError) as exc:
            self._fail(run_id, document_id, self._safe_error_code(exc, "parse_failed"), parse_failure=True)
        except Exception:
            self._fail(run_id, document_id, "parse_failed", parse_failure=True)
        else:
            try:
                self._set_stage(run_id, IngestionStage.CHUNKING)
                self._set_stage(run_id, IngestionStage.EMBEDDING)
                chunks = prepare_chunks(blocks, embed=self._embed,
                    batch_size=self._batch_size, max_chars=self._max_chars)
                if not chunks:
                    raise ValueError("document_contains_no_indexable_text")
                self._set_stage(run_id, IngestionStage.PERSISTING)
                with self._sessions() as db:
                    persist_index_atomically(db, document_id, run_id, chunks)
            except (EmbeddingError, ValueError) as exc:
                self._fail(run_id, document_id, self._safe_error_code(exc, "index_failed"), parse_failure=False)
            except Exception:
                self._fail(run_id, document_id, "index_failed", parse_failure=False)
        return True

    def _claim_next(self) -> tuple[object, object, str] | None:
        with self._sessions() as db:
            candidates = list(db.scalars(select(IngestionRun).where(
                IngestionRun.status.in_([IngestionStatus.QUEUED, IngestionStatus.RUNNING])
            ).order_by(IngestionRun.created_at).with_for_update(skip_locked=True)))
            run = next(iter(claimable_runs(candidates)), None)
            if run is None:
                return None
            document = db.get(Document, run.document_id)
            if document is None:
                run.stage = IngestionStage.FAILED
                run.status = IngestionStatus.FAILED
                run.error_code = "document_not_found"
                run.finished_at = datetime.now(timezone.utc)
                db.commit()
                return None
            run.status = IngestionStatus.RUNNING
            run.stage = IngestionStage.VALIDATING
            run.started_at = run.started_at or datetime.now(timezone.utc)
            run.error_code = None
            document.status = DocumentStatus.PROCESSING
            document.error_code = None
            db.commit()
            return run.id, document.id, document.storage_key

    def _set_stage(self, run_id: object, stage: IngestionStage) -> None:
        with self._sessions() as db:
            run = db.get(IngestionRun, run_id)
            if run is None:
                raise ValueError("ingestion_run_not_found")
            run.stage = stage
            run.status = IngestionStatus.RUNNING
            db.commit()

    def _fail(self, run_id: object, document_id: object, error_code: str, *, parse_failure: bool) -> None:
        with self._sessions() as db:
            run = db.get(IngestionRun, run_id)
            document = db.get(Document, document_id)
            if run is not None:
                run.stage = IngestionStage.FAILED
                run.status = IngestionStatus.FAILED
                run.error_code = error_code
                run.finished_at = datetime.now(timezone.utc)
            if document is not None:
                # A failed reindex must not replace or disable the last known-good index.
                if document.active_run_id is None:
                    document.status = DocumentStatus.PARSE_FAILED if parse_failure else DocumentStatus.INDEX_FAILED
                    document.is_enabled = False
                else:
                    document.status = DocumentStatus.ENABLED if document.is_enabled else DocumentStatus.DISABLED
                document.error_code = error_code
            db.commit()

    @staticmethod
    def _safe_error_code(exc: Exception, fallback: str) -> str:
        candidate = str(exc)
        if candidate and len(candidate) <= 80 and all(char.isalnum() or char == "_" for char in candidate):
            return candidate
        return fallback


class IngestionWorker:
    def __init__(self, executor: IngestionExecutor, *, poll_seconds: float = 1.0) -> None:
        self._executor = executor
        self._poll_seconds = poll_seconds
        self._stop = Event()
        self._thread: Thread | None = None

    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.is_alive:
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="policy-ingestion", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = self._executor.process_next()
            except Exception:
                processed = False
            if not processed:
                self._stop.wait(self._poll_seconds)
