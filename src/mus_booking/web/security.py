"""浏览器会话、同源检查和尝试次数限制。"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

COOKIE = "mb_session"
COOKIE_AGE = 365 * 24 * 3600


def base(request: Request) -> str:
    return request.scope.get("root_path", "").rstrip("/")


def set_session(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE,
        token,
        max_age=COOKIE_AGE,
        path=f"{base(request)}/",
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )


def clear_session(request: Request, response: Response) -> None:
    response.delete_cookie(COOKIE, path=f"{base(request)}/")


class SameOrigin:
    """拒绝来自别的网站的表单提交。会话 Cookie 本身也设了 SameSite=Lax，这是第二道。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD", "OPTIONS"):
            headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
            source = headers.get("origin") or headers.get("referer") or ""
            if not source or urlsplit(source).netloc != headers.get("host", ""):
                await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class Limiter:
    """在一段时间内最多允许若干次尝试。只在内存里计数，服务重启就清零，对这个规模足够。"""

    def __init__(self, attempts: int, window: float) -> None:
        self.attempts = attempts
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, *keys: str) -> bool:
        now = time.monotonic()
        for key in keys:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.attempts:
                return False
        for key in keys:
            self._hits[key].append(now)
        return True
