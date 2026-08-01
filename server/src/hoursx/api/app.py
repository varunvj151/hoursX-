"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from hoursx import __version__
from hoursx.api.routers import (
    admin,
    agents,
    auth,
    knowledge,
    plugins,
    runs,
    schedules,
    sessions,
    ws,
)
from hoursx.config import get_settings
from hoursx.observability import configure_logging, new_request_id, request_id_var
from hoursx.orchestration import Conductor
from hoursx.services import AppServices, build_services


def create_app(services: AppServices | None = None) -> FastAPI:
    """Build the API app. Tests pass a pre-built service graph; production
    builds one from the environment."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal services
        if services is None:
            settings = get_settings()
            configure_logging(settings.log_level, settings.log_json)
            services = build_services(settings)
        await services.db.create_all()
        await services.start()
        app.state.services = services
        app.state.conductor = Conductor(services)
        yield
        await app.state.conductor.wait_for_inline_runs()
        await services.stop()
        await services.db.dispose()

    app = FastAPI(
        title="HoursX API",
        version=__version__,
        lifespan=lifespan,
        description="Autonomous AI agent platform.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # deployments front this with their own origin policy
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def correlation_id(request: Request, call_next):
        rid = new_request_id()
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["x-request-id"] = rid
        return response

    app.include_router(auth.router)
    app.include_router(agents.router)
    app.include_router(sessions.router)
    app.include_router(runs.router)
    app.include_router(runs.approvals)
    app.include_router(knowledge.router)
    app.include_router(schedules.router)
    app.include_router(plugins.router)
    app.include_router(admin.router)
    app.include_router(ws.router)
    return app
