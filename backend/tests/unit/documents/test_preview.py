from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from policy_api.documents.preview import DOCX_MIME, PDF_MIME, PreviewError, build_preview


def converter(output: bytes = b"%PDF-1.7\npreview"):
    calls: list[list[str]] = []

    def run(args: list[str], **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(args)
        source = Path(args[-1])
        outdir = Path(args[args.index("--outdir") + 1])
        (outdir / f"{source.stem}.pdf").write_bytes(output)
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    return calls, run


def test_pdf_preview_returns_the_contained_original(tmp_path: Path) -> None:
    source = tmp_path / "policy.pdf"
    source.write_bytes(b"%PDF-1.7\noriginal")

    result = build_preview(source, tmp_path, uuid4(), "a" * 64, PDF_MIME)

    assert result.path == source
    assert result.media_type == PDF_MIME
    assert result.display_name == "policy.pdf"


def test_docx_preview_converts_validates_and_reuses_sha_cache(tmp_path: Path) -> None:
    source = tmp_path / "policy.docx"
    source.write_bytes(b"PK\x03\x04docx")
    document_id = uuid4()
    calls, run = converter()

    first = build_preview(source, tmp_path, document_id, "b" * 64, DOCX_MIME, runner=run)
    second = build_preview(source, tmp_path, document_id, "b" * 64, DOCX_MIME, runner=run)

    assert first == second
    assert first.path.read_bytes().startswith(b"%PDF-")
    assert first.path.name == f"{document_id}-{'b' * 64}.pdf"
    assert first.display_name == "policy-preview.pdf"
    assert len(calls) == 1
    assert calls[0][0] == "soffice"
    assert "--headless" in calls[0]
    assert not any(";" in argument for argument in calls[0])


def test_docx_preview_uses_a_new_cache_key_for_a_new_source_hash(tmp_path: Path) -> None:
    source = tmp_path / "policy.docx"
    source.write_bytes(b"PK\x03\x04docx")
    document_id = uuid4()
    calls, run = converter()

    old = build_preview(source, tmp_path, document_id, "c" * 64, DOCX_MIME, runner=run)
    new = build_preview(source, tmp_path, document_id, "d" * 64, DOCX_MIME, runner=run)

    assert old.path != new.path
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("mime_type", "code"),
    [("text/plain", "preview_not_supported"), ("application/octet-stream", "preview_not_supported")],
)
def test_preview_rejects_unsupported_types(tmp_path: Path, mime_type: str, code: str) -> None:
    source = tmp_path / "policy.txt"
    source.write_text("policy", encoding="utf-8")

    with pytest.raises(PreviewError, match=code):
        build_preview(source, tmp_path, uuid4(), "e" * 64, mime_type)


def test_preview_rejects_missing_or_outside_root_sources(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.docx"
    outside.write_bytes(b"PK\x03\x04docx")

    with pytest.raises(PreviewError, match="document_not_found"):
        build_preview(outside, tmp_path, uuid4(), "f" * 64, DOCX_MIME)
    with pytest.raises(PreviewError, match="document_not_found"):
        build_preview(tmp_path / "missing.docx", tmp_path, uuid4(), "f" * 64, DOCX_MIME)


def test_preview_maps_timeout_converter_failure_and_invalid_pdf(tmp_path: Path) -> None:
    source = tmp_path / "policy.docx"
    source.write_bytes(b"PK\x03\x04docx")

    def timeout(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired("soffice", 30)

    def failure(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.CalledProcessError(1, "soffice")

    with pytest.raises(PreviewError, match="preview_timeout"):
        build_preview(source, tmp_path, uuid4(), "1" * 64, DOCX_MIME, runner=timeout)
    with pytest.raises(PreviewError, match="preview_failed"):
        build_preview(source, tmp_path, uuid4(), "2" * 64, DOCX_MIME, runner=failure)

    _calls, invalid = converter(b"not-a-pdf")
    with pytest.raises(PreviewError, match="preview_failed"):
        build_preview(source, tmp_path, uuid4(), "3" * 64, DOCX_MIME, runner=invalid)
