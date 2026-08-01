"""Audit trail for consequential actions.

Scope is deliberate: membership and role changes, credential lifecycle, approval
decisions, agent configuration, and plugin installs. Ordinary reads are not
audited — logging everything produces a stream nobody reviews, which is worse
than no audit at all because it looks like coverage.

The writer never raises. An audit failure must not roll back the action it was
describing; it is logged loudly instead.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hoursx.db.models import AuditEvent
from hoursx.observability import get_logger, redact

log = get_logger("audit")


class AuditAction(StrEnum):
    MEMBER_ADDED = "member.added"
    MEMBER_ROLE_CHANGED = "member.role_changed"
    MEMBER_REMOVED = "member.removed"
    API_KEY_CREATED = "api_key.created"
    API_KEY_REVOKED = "api_key.revoked"
    AGENT_CREATED = "agent.created"
    AGENT_UPDATED = "agent.updated"
    AGENT_DELETED = "agent.deleted"
    APPROVAL_DECIDED = "approval.decided"
    RUN_CANCELLED = "run.cancelled"
    DOCUMENT_DELETED = "document.deleted"


async def record(
    session: AsyncSession,
    *,
    workspace_id: str,
    actor_user_id: str | None,
    action: AuditAction,
    target_type: str,
    target_id: str,
    **detail: Any,
) -> None:
    """Append one audit event. Secret-bearing detail keys are redacted."""
    try:
        session.add(
            AuditEvent(
                workspace_id=workspace_id,
                actor_user_id=actor_user_id,
                action=action.value,
                target_type=target_type,
                target_id=target_id,
                detail=redact(detail),
            )
        )
        await session.flush()
    except Exception:  # noqa: BLE001 — auditing must never break the audited action
        log.exception("failed to record audit event %s", action.value)


async def recent_events(
    session: AsyncSession, *, workspace_id: str, limit: int = 100
) -> list[AuditEvent]:
    """Most recent audit events for a workspace, newest first."""
    return list(
        (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.workspace_id == workspace_id)
                .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
