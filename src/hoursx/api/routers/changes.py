"""Change ledger API: what an agent changed, and how to undo it.

This is the operator's view of the same records the agent writes. It exists so a
human can audit and reverse agent-made host changes without needing the agent,
which matters most in exactly the situation where the agent is the problem.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel

from hoursx.api.deps import Actor, get_services, require
from hoursx.audit import AuditAction, record
from hoursx.auth import Permission
from hoursx.remediation.guard import confirm_change
from hoursx.remediation.ledger import list_changes, load_change, revert_change
from hoursx.services import AppServices
from hoursx.system.privileges import SystemPolicy

router = APIRouter(prefix="/v1/changes", tags=["changes"])


class ChangeOut(BaseModel):
    id: str
    run_id: str | None
    kind: str
    target: str
    previous_value: str | None
    new_value: str
    status: str
    revertible: bool
    conditions: list[dict[str, Any]]
    detail: str
    created_at: datetime
    expires_at: datetime | None
    settled_at: datetime | None


def _policy(services: AppServices) -> SystemPolicy:
    return SystemPolicy(
        enabled=services.settings.system_ops_enabled,
        allow_mutations=services.settings.system_mutations_enabled,
        extra_sysctl_allowlist=frozenset(services.settings.system_sysctl_allowlist),
        backend=services.settings.system_backend,
        sysd_socket=services.settings.sysd_socket,
    )


def _out(record_row: Any) -> ChangeOut:
    return ChangeOut(
        id=record_row.id,
        run_id=record_row.run_id,
        kind=record_row.kind,
        target=record_row.target,
        previous_value=record_row.previous_value,
        new_value=record_row.new_value,
        status=record_row.status,
        revertible=record_row.revertible,
        conditions=record_row.conditions or [],
        detail=record_row.detail,
        created_at=record_row.created_at,
        expires_at=record_row.expires_at,
        settled_at=record_row.settled_at,
    )


@router.get("", response_model=list[ChangeOut])
async def list_workspace_changes(
    run_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    actor: Actor = Depends(require(Permission.OBSERVE)),
    services: AppServices = Depends(get_services),
) -> list[ChangeOut]:
    async with services.db.session() as db:
        records = await list_changes(
            db, workspace_id=actor.workspace.id, run_id=run_id, limit=limit
        )
        return [_out(row) for row in records]


@router.post("/{change_id}/revert", status_code=status.HTTP_202_ACCEPTED)
async def revert(
    change_id: str,
    actor: Actor = Depends(require(Permission.RUNS_APPROVE)),
    services: AppServices = Depends(get_services),
) -> dict:
    """Undo a change using the value captured before it was applied."""
    async with services.db.session() as db:
        record_row = await load_change(db, change_id=change_id, workspace_id=actor.workspace.id)
        outcome = await revert_change(
            db, _policy(services), record_row, reason=f"reverted by {actor.user.email};"
        )
        await record(
            db,
            workspace_id=actor.workspace.id,
            actor_user_id=actor.user.id,
            action=AuditAction.CHANGE_REVERTED,
            target_type="change",
            target_id=change_id,
            ok=outcome.ok,
            target=record_row.target,
        )
    return {"ok": outcome.ok, "status": outcome.status.value, "detail": outcome.summary}


@router.post("/{change_id}/confirm", status_code=status.HTTP_202_ACCEPTED)
async def confirm(
    change_id: str,
    actor: Actor = Depends(require(Permission.RUNS_APPROVE)),
    services: AppServices = Depends(get_services),
) -> dict:
    """Keep a change past its dead-man window."""
    outcome = await confirm_change(
        services,
        change_id=change_id,
        workspace_id=actor.workspace.id,
        confirmed_by=actor.user.email,
    )
    async with services.db.session() as db:
        await record(
            db,
            workspace_id=actor.workspace.id,
            actor_user_id=actor.user.id,
            action=AuditAction.CHANGE_CONFIRMED,
            target_type="change",
            target_id=change_id,
        )
    return {"ok": outcome.ok, "status": outcome.status.value, "detail": outcome.summary}
