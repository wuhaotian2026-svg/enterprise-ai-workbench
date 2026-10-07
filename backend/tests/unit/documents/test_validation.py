from __future__ import annotations

import io
import zipfile

import pytest

from policy_api.documents.validation import UploadValidationError, validate_upload


def docx_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("filename", "content_type", "content"),
    [
        ("policy.pdf", "application/pdf", b"%PDF-1.7\nbody"),
        ("policy.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", docx_bytes()),
        ("policy.txt", "text/plain", "休假制度".encode()),
    ],
)
def test_validate_upload_accepts_supported_matching_files(filename: str, content_type: str, content: bytes) -> None:
    result = validate_upload(filename, content_type, content, max_bytes=1024)
    assert result.extension in {".pdf", ".docx", ".txt"}
    assert len(result.sha256) == 64


@pytest.mark.parametrize("filename", ["policy.pdf.exe", "../policy.pdf", "..\\policy.pdf"])
def test_validate_upload_rejects_unsafe_or_double_extension_names(filename: str) -> None:
    with pytest.raises(UploadValidationError, match="unsafe_filename"):
        validate_upload(filename, "application/pdf", b"%PDF-1.7\nbody", max_bytes=1024)


def test_validate_upload_rejects_mime_or_signature_mismatch() -> None:
    with pytest.raises(UploadValidationError, match="content_mismatch"):
        validate_upload("policy.pdf", "application/pdf", b"not-a-pdf", max_bytes=1024)


@pytest.mark.parametrize(("content", "code"), [(b"", "empty_file"), (b"12345", "file_too_large")])
def test_validate_upload_rejects_empty_and_oversized_files(content: bytes, code: str) -> None:
    with pytest.raises(UploadValidationError, match=code):
        validate_upload("policy.txt", "text/plain", content, max_bytes=4)
