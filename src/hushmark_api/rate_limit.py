from collections import OrderedDict
from collections.abc import Callable
from math import ceil
from threading import Lock
from time import monotonic

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_MAX_CLIENTS = 10_000
_WINDOW_SECONDS = 60


class RateLimitMiddleware:
    def __init__(
        self, app: ASGIApp, max_requests: int, clock: Callable[[], float] = monotonic
    ) -> None:
        if max_requests <= 0:
            raise ValueError("RATE_LIMIT_PER_MINUTE must be positive when enabled")
        self.app = app
        self.max_requests = max_requests
        self.clock = clock
        self._clients: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._lock = Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not scope["path"].startswith("/v1/")
        ):
            await self.app(scope, receive, send)
            return

        address = scope.get("client")
        client = address[0] if address else "unknown"
        now = self.clock()
        with self._lock:
            while self._clients:
                started, _ = next(iter(self._clients.values()))
                if now - started < _WINDOW_SECONDS:
                    break
                self._clients.popitem(last=False)
            started, count = self._clients.get(client, (now, 0))
            retry_after = 0
            if count >= self.max_requests:
                retry_after = max(1, ceil(started + _WINDOW_SECONDS - now))
            else:
                self._clients[client] = (started, count + 1)
                if len(self._clients) > _MAX_CLIENTS:
                    self._clients.popitem(last=False)

        if retry_after:
            response = JSONResponse(
                status_code=429,
                content={
                    "code": "rate_limited",
                    "message": "Too many requests. Please wait a minute and try again.",
                },
                headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
