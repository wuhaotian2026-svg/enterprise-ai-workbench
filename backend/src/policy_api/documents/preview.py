from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import UUID, uuid4


PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class PreviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreviewResult:
    path: Path
    media_type: str
    display_name: str


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


def _valid_pdf(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(5) == b"%PDF-"
    except OSError:
        return False


def _contained_source(source: Path, upload_root: Path) -> Path:
    root = upload_root.resolve()
    resolved = source.resolve()
    if resolved.parent != root or not resolved.is_file():
        raise PreviewError("document_not_found")
    return resolved


def build_preview(
    source: Path,
    upload_root: Path,
    document_id: UUID,
    sha256: str,
    mime_type: str,
    *,
    runner: Runner = subprocess.run,
    timeout_seconds: float = 30,
) -> PreviewResult:
    source = _contained_source(source, upload_root)
    if mime_type == PDF_MIME:
        if not _valid_pdf(source):
            raise PreviewError("preview_failed")
        return PreviewResult(source, PDF_MIME, source.name)
    if mime_type != DOCX_MIME:
        raise PreviewError("preview_not_supported")

    preview_root = upload_root.resolve() / "previews"
    preview_root.mkdir(parents=True, exist_ok=True)
    cached = preview_root / f"{document_id}-{sha256}.pdf"
    if _valid_pdf(cached):
        return PreviewResult(cached, PDF_MIME, f"{source.stem}-preview.pdf")

    work = preview_root / f".work-{uuid4().hex}"
    output = work / "output"
    profile = work / "profile"
    output.mkdir(parents=True)
    profile.mkdir(parents=True)
    arguments = [
        "soffice",
        f"-env:UserInstallation={profile.as_uri()}",
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output),
        str(source),
    ]
    try:
        runner(arguments, check=True, timeout=timeout_seconds, capture_output=True)
    except subprocess.TimeoutExpired as exc:
        raise PreviewError("preview_timeout") from exc
    except (subprocess.CalledProcessError, OSError) as exc:
        raise PreviewError("preview_failed") from exc

    converted = output / f"{source.stem}.pdf"
    if not _valid_pdf(converted):
        raise PreviewError("preview_failed")
    converted.replace(cached)
    return PreviewResult(cached, PDF_MIME, f"{source.stem}-preview.pdf")
