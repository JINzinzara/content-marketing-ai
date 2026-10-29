"""설정과 저장소를 초기화하고 화면·인증·외부 연동과 웹앱 연결"""

import asyncio
import threading
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import jobs, analytics
from .text_ai import TextAI
from .config import Settings
from .db import initialize
from .instagram import Instagram, InstagramError
from .image_ai import ImageAI
from .routes import page, router

APP_DIR = Path(__file__).parent


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        size = 0
        limit = 10 * 1024 * 1024
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse(
                {"detail": "올바르지 않은 이미지 크기입니다"}, status_code=400
            )(scope, receive, send)
        if declared > limit:
            return await JSONResponse(
                {"detail": "이미지 용량이 너무 큽니다. 사진은 8MB 이하로 올려주세요"},
                status_code=413,
            )(scope, receive, send)

        async def bounded_receive():
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > limit:
                    raise HTTPException(413, "이미지가 너무 큽니다")
            return message

        await self.app(scope, bounded_receive, send)


def create_app(settings=None, http_client=None):
    """웹앱 객체"""
    cfg = settings or Settings.from_env()
    cfg.prepare()
    initialize(cfg)
    client = http_client or httpx.Client(
        timeout=httpx.Timeout(120, connect=10), follow_redirects=False
    )

    @asynccontextmanager
    async def lifespan(app):
        jobs.recover(cfg)
        tasks = [asyncio.create_task(jobs.run(app, kind)) for kind in ("publishing", "briefing")]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            if http_client is None:
                client.close()

    app = FastAPI(
        title="콘텐츠 관리 서비스",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.analytics_refreshes = {}
    app.state.analytics_lock = threading.Lock()
    app.state.settings = cfg
    app.state.templates = Jinja2Templates(directory=APP_DIR / "templates")
    app.state.templates.env.filters["kst"] = analytics.local_text
    app.state.instagram = Instagram(cfg, client)
    app.state.ai = TextAI(cfg, client)
    app.state.image_ai = ImageAI(cfg, client)
    app.state.image_ai.recover()
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.session_secret,
        same_site="lax",
        https_only=cfg.environment == "production",
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=cfg.allowed_hosts)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if (
            cfg.environment == "local"
            and request.client
            and request.client.host not in {"127.0.0.1", "::1", "testclient"}
        ):
            return JSONResponse(
                {"detail": "로컬 실행은 이 PC에서만 접근할 수 있습니다"},
                status_code=403,
            )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(request, exc):
        if "text/html" not in request.headers.get("accept", ""):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        result = page(
            request,
            "error.html",
            request.session.get("owner_id"),
            message=exc.detail,
            status=exc.status_code,
        )
        result.status_code = exc.status_code
        return result

    @app.exception_handler(InstagramError)
    async def handle_instagram(request, exc):
        return await handle_http(request, HTTPException(502, str(exc)))

    @app.get("/health")
    def health():
        return {"status": "ok"}

    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
    app.mount("/fonts", StaticFiles(directory=APP_DIR / "assets/fonts"), name="fonts")
    app.include_router(router)
    from .daily_routes import router as daily_router
    app.include_router(daily_router)
    return app
