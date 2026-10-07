from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoredFile:
    key: str
    path: Path


def store_bytes(root: Path, extension: str, content: bytes) -> StoredFile:
    resolved_root = root.resolve()
    resolved_root.mkdir(parents=True, exist_ok=True)
    key = f"{uuid.uuid4().hex}{extension}"
    destination = (resolved_root / key).resolve()
    if destination.parent != resolved_root:
        raise ValueError("storage_path_outside_root")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return StoredFile(key=key, path=destination)
