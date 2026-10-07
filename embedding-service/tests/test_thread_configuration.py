from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from policy_embeddings import app as embedding_app


async def _captured_threads(raw_value: str | None) -> int:
    captured: list[int] = []
    original_embedder = embedding_app.LocalEmbedder
    original_value = os.environ.get("LOCAL_MODEL_THREADS")

    class FakeEmbedder:
        def __init__(self, _model_path, *, threads: int) -> None:
            captured.append(threads)

    try:
        embedding_app.LocalEmbedder = FakeEmbedder
        if raw_value is None:
            os.environ.pop("LOCAL_MODEL_THREADS", None)
        else:
            os.environ["LOCAL_MODEL_THREADS"] = raw_value
        application = SimpleNamespace(state=SimpleNamespace())
        async with embedding_app.lifespan(application):
            pass
    finally:
        embedding_app.LocalEmbedder = original_embedder
        if original_value is None:
            os.environ.pop("LOCAL_MODEL_THREADS", None)
        else:
            os.environ["LOCAL_MODEL_THREADS"] = original_value
    return captured[0]


def main() -> None:
    assert asyncio.run(_captured_threads(None)) == 0
    assert asyncio.run(_captured_threads("0")) == 0
    assert asyncio.run(_captured_threads("4")) == 4
    assert asyncio.run(_captured_threads("99")) == 20
    assert asyncio.run(_captured_threads("-3")) == 0


if __name__ == "__main__":
    main()
