from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict
from transformers import AutoTokenizer


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    input: str | list[str]
    encoding_format: str = "float"


class LocalEmbedder:
    def __init__(self, model_path: Path, *, threads: int) -> None:
        onnx_path = model_path / "onnx" / "model.onnx"
        if not onnx_path.is_file():
            raise RuntimeError("local_model_missing")
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_path / "onnx", local_files_only=True
        )
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(onnx_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.input_names = {item.name for item in self.session.get_inputs()}

    def embed(self, texts: list[str]) -> list[list[float]]:
        tokens = self.tokenizer(
            texts,
            max_length=512,
            padding=True,
            truncation=True,
            return_tensors="np",
        )
        inputs = {
            name: np.asarray(value, dtype=np.int64)
            for name, value in tokens.items()
            if name in self.input_names
        }
        if "token_type_ids" in self.input_names and "token_type_ids" not in inputs:
            inputs["token_type_ids"] = np.zeros_like(inputs["input_ids"], dtype=np.int64)
        hidden = self.session.run(None, inputs)[0]
        mask = np.asarray(tokens["attention_mask"], dtype=np.float32)[..., None]
        pooled = (hidden * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        normalized = pooled / np.clip(norms, 1e-12, None)
        return normalized.astype(np.float32).tolist()


@asynccontextmanager
async def lifespan(application: FastAPI):
    model_path = Path(os.environ.get("LOCAL_MODEL_PATH", "/models/multilingual-e5-small"))
    threads = max(0, min(int(os.environ.get("LOCAL_MODEL_THREADS", "0")), 20))
    application.state.embedder = LocalEmbedder(model_path, threads=threads)
    yield


app = FastAPI(title="Local policy embeddings", docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/embeddings")
def embeddings(payload: EmbeddingRequest) -> dict[str, Any]:
    expected_model = os.environ.get("LOCAL_MODEL_ID", "intfloat/multilingual-e5-small")
    if payload.model != expected_model:
        raise HTTPException(status_code=400, detail="unsupported_model")
    if payload.encoding_format != "float":
        raise HTTPException(status_code=400, detail="unsupported_encoding_format")
    texts = [payload.input] if isinstance(payload.input, str) else payload.input
    if not texts or len(texts) > 64 or any(not text.strip() for text in texts):
        raise HTTPException(status_code=400, detail="invalid_input")
    vectors = app.state.embedder.embed(texts)
    return {
        "object": "list",
        "model": expected_model,
        "data": [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in enumerate(vectors)
        ],
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }
