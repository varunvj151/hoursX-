"""Health, readiness, and operational introspection."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text

from hoursx import __version__
from hoursx.api.deps import Actor, get_services, require
from hoursx.audit import recent_events
from hoursx.auth import Permission
from hoursx.observability import metrics
from hoursx.services import AppServices

router = APIRouter(tags=["admin"])


@router.get("/healthz")
async def healthz() -> dict:
    """Liveness: the process is up."""
    return {"ok": True, "version": __version__}


@router.get("/readyz")
async def readyz(services: AppServices = Depends(get_services)) -> dict:
    """Readiness: every hard dependency answers.

    Reports per-dependency status rather than a bare boolean, so an operator
    seeing a replica drop out of rotation knows which backend caused it.
    """
    checks: dict[str, str] = {}
    try:
        async with services.db.session() as db:
            await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 — readiness reports, never raises
        checks["database"] = f"error: {type(exc).__name__}"

    if services.settings.redis_url:
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(services.settings.redis_url)
            try:
                await client.ping()
                checks["redis"] = "ok"
            finally:
                await client.aclose()
        except Exception as exc:  # noqa: BLE001
            checks["redis"] = f"error: {type(exc).__name__}"

    healthy = all(value == "ok" for value in checks.values())
    if not healthy:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"ok": False, "checks": checks})
    return {"ok": True, "checks": checks}


@router.get("/v1/admin/metrics")
async def get_metrics(
    actor: Actor = Depends(require(Permission.OBSERVE)),
) -> dict:
    return {"counters": metrics.snapshot()}


@router.get("/v1/admin/audit")
async def get_audit_log(
    limit: int = 100,
    actor: Actor = Depends(require(Permission.MEMBERS_MANAGE)),
    services: AppServices = Depends(get_services),
) -> dict:
    """Recent security-relevant actions in this workspace, newest first.

    Gated on member administration rather than plain observation: the trail
    names who did what, which is itself sensitive.
    """
    async with services.db.session() as db:
        events = await recent_events(
            db, workspace_id=actor.workspace.id, limit=min(max(limit, 1), 500)
        )
        return {
            "events": [
                {
                    "id": event.id,
                    "action": event.action,
                    "actor_user_id": event.actor_user_id,
                    "target_type": event.target_type,
                    "target_id": event.target_id,
                    "detail": event.detail,
                    "created_at": event.created_at.isoformat(),
                }
                for event in events
            ]
        }


@router.get("/v1/admin/quota")
async def get_quota(
    actor: Actor = Depends(require(Permission.OBSERVE)),
    services: AppServices = Depends(get_services),
) -> dict:
    """Live quota consumption, so clients can back off before being throttled."""
    from hoursx.quotas import current_usage

    async with services.db.session() as db:
        usage = await current_usage(db, workspace_id=actor.workspace.id)
    settings = services.settings
    return {
        "active_runs": usage.active_runs,
        "runs_last_hour": usage.runs_last_hour,
        "max_concurrent_runs": settings.max_concurrent_runs_per_workspace,
        "max_runs_per_hour": settings.max_runs_per_hour_per_workspace,
    }


@router.get("/v1/admin/tools")
async def list_tools(
    actor: Actor = Depends(require(Permission.OBSERVE)),
    services: AppServices = Depends(get_services),
) -> dict:
    return {"tools": services.registry.names()}
