from __future__ import annotations

from threading import Event

from policy_api.ingestion.worker import IngestionWorker, claimable_runs, is_safe_to_resume
from policy_api.models import IngestionStage, IngestionStatus


def test_only_queued_or_pre_persistence_runs_are_safe_to_resume() -> None:
    assert is_safe_to_resume(IngestionStatus.QUEUED, IngestionStage.VALIDATING)
    assert is_safe_to_resume(IngestionStatus.RUNNING, IngestionStage.PARSING)
    assert is_safe_to_resume(IngestionStatus.RUNNING, IngestionStage.EMBEDDING)
    assert not is_safe_to_resume(IngestionStatus.RUNNING, IngestionStage.PERSISTING)
    assert not is_safe_to_resume(IngestionStatus.COMPLETED, IngestionStage.COMPLETED)
    assert not is_safe_to_resume(IngestionStatus.FAILED, IngestionStage.FAILED)


def test_claimable_runs_excludes_unsafe_persisting_work() -> None:
    runs = [
        type("Run", (), {"status": IngestionStatus.QUEUED, "stage": IngestionStage.VALIDATING})(),
        type("Run", (), {"status": IngestionStatus.RUNNING, "stage": IngestionStage.PARSING})(),
        type("Run", (), {"status": IngestionStatus.RUNNING, "stage": IngestionStage.PERSISTING})(),
        type("Run", (), {"status": IngestionStatus.COMPLETED, "stage": IngestionStage.COMPLETED})(),
    ]

    assert claimable_runs(runs) == runs[:2]


def test_background_worker_drains_available_work_and_stops_cleanly() -> None:
    calls = 0
    processed = Event()

    class Executor:
        def process_next(self) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                processed.set()
                return True
            return False

    worker = IngestionWorker(Executor(), poll_seconds=0.01)
    worker.start()
    assert processed.wait(timeout=1)
    worker.stop(timeout=1)

    assert calls >= 1
    assert not worker.is_alive
