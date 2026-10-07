from __future__ import annotations

from pathlib import Path

from policy_api.documents.storage import store_bytes


def test_store_bytes_uses_generated_key_and_stays_inside_root(tmp_path: Path) -> None:
    stored = store_bytes(tmp_path, ".txt", b"policy")

    assert stored.path.parent == tmp_path.resolve()
    assert stored.path.name != "policy.txt"
    assert stored.path.suffix == ".txt"
    assert stored.path.read_bytes() == b"policy"


def test_store_bytes_atomically_replaces_temporary_file(tmp_path: Path) -> None:
    stored = store_bytes(tmp_path, ".pdf", b"%PDF-1.7")

    assert stored.path.exists()
    assert not list(tmp_path.glob("*.tmp"))
