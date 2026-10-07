from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class ErrorResponse:
    code: str
    message: str
    request_id: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)
