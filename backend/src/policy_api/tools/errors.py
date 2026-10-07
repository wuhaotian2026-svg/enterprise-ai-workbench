from __future__ import annotations

from collections.abc import Mapping


class ToolError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        self.code = code
        self.metadata = dict(metadata or {})
        super().__init__(code)
