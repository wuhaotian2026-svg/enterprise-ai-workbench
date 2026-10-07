from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path


MIME_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
}


class UploadValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedUpload:
    extension: str
    mime_type: str
    sha256: str
    size: int


def _is_docx(content: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
            return "[Content_Types].xml" in names and "word/document.xml" in names
    except zipfile.BadZipFile:
        return False


def validate_upload(filename: str, content_type: str, content: bytes, *, max_bytes: int) -> ValidatedUpload:
    if Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise UploadValidationError("unsafe_filename")
    if not re.fullmatch(r"[^.]+\.(pdf|docx|txt)", filename, flags=re.IGNORECASE):
        raise UploadValidationError("unsafe_filename")
    if not content:
        raise UploadValidationError("empty_file")
    if len(content) > max_bytes:
        raise UploadValidationError("file_too_large")
    extension = Path(filename).suffix.lower()
    expected_mime = MIME_TYPES[extension]
    signature_matches = (
        (extension == ".pdf" and content.startswith(b"%PDF-"))
        or (extension == ".docx" and _is_docx(content))
        or (extension == ".txt" and b"\x00" not in content)
    )
    if content_type != expected_mime or not signature_matches:
        raise UploadValidationError("content_mismatch")
    return ValidatedUpload(extension, expected_mime, hashlib.sha256(content).hexdigest(), len(content))
