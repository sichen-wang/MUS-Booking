"""网页服务的组装：模板、静态文件、中间件、路由，以及随服务启停的后台任务。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException

from ..engine import Clock, Engine
from ..store import Store
from .actions import Connect
from .security import Limiter, SameOrigin, base

HERE = Path(__file__).parent
STATIC = HERE / "static"


@dataclass
class Services:
    store: Store
    engine: Engine
    clock: Clock
    connect: Connect
    preview: bool = False


def _versions() -> dict[str, str]:
    """静态文件的内容摘要，拼在地址后面，内容变了浏览器就会重新下载。"""
    return {
        path.relative_to(STATIC).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()[:10]
        for path in STATIC.rglob("*")
        if path.is_file()
    }


def _helpers(request: Request) -> dict[str, object]:
    root = base(request)
    versions: dict[str, str] = request.app.state.versions

    def url(path: str) -> str:
        return f"{root}{path}"

    def static(name: str) -> str:
        return f"{root}/static/{name}?v={versions.get(name, '0')}"

    return {"u": url, "static": static}


templates = Jinja2Templates(directory=HERE / "templates", context_processors=[_helpers])
templates.env.trim_blocks = True
templates.env.lstrip_blocks = True


def render(request: Request, name: str, status: int = 200, **context: object) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context, status_code=status)


def go(request: Request, path: str) -> Response:
    """表单提交之后跳转。由 HTMX 发起的请求让它在页面内跳转，免得整页闪一下。"""
    target = f"{base(request)}{path}"
    if request.headers.get("hx-request"):
        return Response(headers={"HX-Location": json.dumps({"path": target, "target": "body"})})
    return RedirectResponse(target, status_code=303)


def services(request: Request) -> Services:
    return request.app.state.services


def create_app(svc: Services, *, run_engine: bool = True) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(svc.engine.run()) if run_engine else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = svc
    app.state.versions = _versions()
    app.state.login_limit = Limiter(attempts=8, window=900)
    app.state.account_limit = Limiter(attempts=5, window=900)
    app.add_middleware(SameOrigin)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    from . import admin, pages

    app.include_router(pages.router)
    app.include_router(admin.router)

    @app.exception_handler(pages.SignedOut)
    async def signed_out(request: Request, _: Exception) -> Response:
        return go(request, "/")

    @app.exception_handler(HTTPException)
    async def not_found(request: Request, error: HTTPException) -> Response:
        if error.status_code == 404:
            return render(request, "missing.html", status=404)
        return Response(status_code=error.status_code)

    return app
