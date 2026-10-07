from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import Request
from fastapi.responses import JSONResponse


class FixedWindowRateLimiter:
    def __init__(self, window_seconds: float = 60) -> None:
        self.window_seconds = window_seconds; self._events: dict[str, deque[float]] = defaultdict(deque); self._lock = Lock()

    def check(self, key: str, limit: int, now: float | None = None) -> int | None:
        current = time.monotonic() if now is None else now
        with self._lock:
            events = self._events[key]
            while events and current - events[0] >= self.window_seconds: events.popleft()
            if len(events) >= limit: return max(1, math.ceil(self.window_seconds - (current - events[0])))
            events.append(current); return None


def limited_response(request: Request, retry_after: int) -> JSONResponse:
    return JSONResponse(status_code=429, headers={"Retry-After": str(retry_after)}, content={
        "code":"rate_limited", "message":"Too many requests.", "request_id":request.state.request_id})


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"
