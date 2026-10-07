from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class DocumentResponse(BaseModel):
    id: UUID
    display_name: str
    mime_type: str
    status: str
    is_enabled: bool
    error_code: str | None
    updated_at: datetime
    chunk_count: int


class IngestionRunResponse(BaseModel):
    id: UUID
    stage: str
    status: str
    error_code: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class DocumentChunkResponse(BaseModel):
    id: UUID
    sequence: int
    heading_path: str | None
    page_number: int | None
    location: str | None
    text: str
