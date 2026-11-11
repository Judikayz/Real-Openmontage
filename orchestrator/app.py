"""App factory. Mounted standalone or inside Backlot."""
from __future__ import annotations

from fastapi import FastAPI

from .config import Config
from .db import DB
from .routes import router
from .storage import build_storage


def attach(app: FastAPI, cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config()
    app.state.cfg = cfg
    app.state.db = DB(cfg.db_path)
    app.state.storage = build_storage(cfg)
    app.include_router(router)
    return app


def create_app(cfg: Config | None = None) -> FastAPI:
    return attach(FastAPI(title="Orchestrator", docs_url=None, redoc_url=None), cfg)
