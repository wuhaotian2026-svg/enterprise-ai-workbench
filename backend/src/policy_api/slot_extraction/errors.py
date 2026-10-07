from __future__ import annotations


class SlotExtractionError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        slot_extraction_calls: int = 0,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.slot_extraction_calls = slot_extraction_calls
        super().__init__(code)


class SourceQuoteNotFound(SlotExtractionError):
    def __init__(self) -> None:
        super().__init__("source_quote_not_found")


class SourceQuoteAmbiguous(SlotExtractionError):
    def __init__(self) -> None:
        super().__init__("source_quote_ambiguous")
