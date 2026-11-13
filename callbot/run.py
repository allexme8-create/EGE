#!/usr/bin/env python3
"""Точка входа: python run.py [--host 0.0.0.0] [--port 8000]."""
from __future__ import annotations

import argparse
import base64
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app import config
from app.api import router
from app.asr import ASRService
from app.db import Database
from app.engine import CallEngine
from app.settings import SettingsStore
from app.tts import TTSService


def create_app() -> FastAPI:
    config.ensure_dirs()
    db = Database(config.DB_PATH)
    db.init_schema()
    settings = SettingsStore(db)
    asr = ASRService(settings)
    tts = TTSService(settings, config.PROMPTS_DIR)
    engine = CallEngine(db, settings, asr, tts)
    engine.recover_stale()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        engine.resume_running_campaigns()
        yield
        engine.shutdown()
        db.close()

    app = FastAPI(title="Обзвон+", version=config.APP_VERSION, docs_url="/api/docs",
                  openapi_url="/api/openapi.json", lifespan=lifespan)
    app.state.db = db
    app.state.settings = settings
    app.state.asr = asr
    app.state.tts = tts
    app.state.engine = engine

    if config.AUTH_USER and config.AUTH_PASSWORD:
        expected = "Basic " + base64.b64encode(
            f"{config.AUTH_USER}:{config.AUTH_PASSWORD}".encode("utf-8")).decode("ascii")

        class BasicAuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                if request.url.path == "/api/health":
                    return await call_next(request)
                if secrets.compare_digest(request.headers.get("authorization", ""), expected):
                    return await call_next(request)
                return Response(status_code=401, content="Требуется авторизация",
                                headers={"WWW-Authenticate": 'Basic realm="callbot"'})

        app.add_middleware(BasicAuthMiddleware)

    app.include_router(router, prefix="/api")
    app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "app" / "static")),
              name="static")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = (Path(__file__).resolve().parent / "app" / "static" / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    return app


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Сервис обзвона клиентов")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run("run:app" if args.reload else app, host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
