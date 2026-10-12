"""ASGI entrypoint:  uvicorn company_env.main:app"""
from __future__ import annotations

from fastapi import FastAPI

from . import ui
from . import admin
from . import api
from . import config
from . import db
from . import errors
from .middleware import FaultAndLogMiddleware


def create_app() -> FastAPI:
    db.ensure_db()
    app = FastAPI(title="Simulated Company Environment", version="0.1.0",
                  description="Finance app + document repository used to test an AI task worker.")
    errors.register(app)
    app.add_middleware(FaultAndLogMiddleware)
    app.include_router(api.router)
    app.include_router(ui.router)
    if config.admin_enabled():
        app.include_router(admin.router)

    @app.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok"}

    return app


app = create_app()
