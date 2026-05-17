"""FastAPI entry point."""
from __future__ import annotations

import logging

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.config import get_settings
from app.routes.analyze import router as analyze_router
from app.routes.content import router as content_router
from app.routes.exports import router as exports_router
from app.routes.news import router as news_router
from app.routes.performance import router as performance_router
from app.routes.results import router as results_router
from app.routes.runs import router as runs_router

# ----- structured logging -----
logging.basicConfig(level=logging.INFO, format="%(message)s")
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="SEO Intel POC",
        version=__version__,
        description="Crawl a website, detect SEO issues, discover entities, research keywords.",
    )

    # CORS — POC: allow Next.js dev server.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "version": __version__, "env": settings.app_env}

    @app.get("/api/sources/status")
    async def sources_status() -> dict:
        return {
            "dataforseo":    bool(settings.dataforseo_login and settings.dataforseo_password),
            "openai":        bool(settings.openai_api_key),
            "apify":         bool(settings.apify_token),
            "apify_enabled": settings.apify_enabled,
            "google":        bool(settings.google_client_id and settings.google_client_secret),
            "supabase":      bool(settings.supabase_url and settings.supabase_service_role_key),
        }

    app.include_router(analyze_router)
    app.include_router(content_router)
    app.include_router(news_router)
    app.include_router(performance_router)
    app.include_router(runs_router)
    app.include_router(results_router)
    app.include_router(exports_router)

    return app


app = create_app()
