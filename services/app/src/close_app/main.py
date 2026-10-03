"""FastAPI application for the close platform (a Databricks App; locally behind devproxy).

``python -m close_app`` serves ``app_factory`` on ``$DATABRICKS_APP_PORT``. Tests build their own
instance with ``create_app(context=...)``, so importing this module has no side effects.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from close_app import errors, logs, middleware
from close_app.context import AppContext, build_context
from close_app.routers import (
    close,
    configs,
    delegations,
    governance,
    health,
    journals,
    ledger,
    periods,
    posting,
    rates,
    smoke,
)
from close_app.settings import Settings


def create_app(settings: Settings | None = None, *, context: AppContext | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ctx = context or build_context(settings or Settings())  # type: ignore[call-arg]
        app.state.ctx = ctx
        ctx.start_background()
        try:
            yield
        finally:
            ctx.stop_background()

    app = FastAPI(title="close platform", version="0.1.0", lifespan=lifespan)
    middleware.install(app)
    errors.install(app)
    app.include_router(health.router)
    app.include_router(smoke.router)
    app.include_router(configs.router)
    app.include_router(governance.router)
    app.include_router(journals.router)
    app.include_router(periods.router)
    app.include_router(delegations.router)
    app.include_router(posting.router)
    app.include_router(rates.router)
    app.include_router(ledger.router)
    app.include_router(close.router)
    return app


def app_factory() -> FastAPI:
    """uvicorn factory for the deployed app: JSON logs, settings from the environment."""
    logs.configure()
    return create_app()
