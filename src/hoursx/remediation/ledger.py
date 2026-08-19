"""The change ledger: what was changed, and how to put it back.

Every guarded mutation writes a row here *before* its outcome is known, so a
change never exists on the host without a stored path back. Revert reads that
row rather than recomputing an inverse, because reconstructing prior state after
the fact is exactly where rollback tooling usually gets it wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hoursx.db.models import ChangeRecord, utcnow
from hoursx.errors import NotFoundError
from hoursx.observability import get_logger, metrics
from hoursx.system.privileges import SystemPolicy, UnsafeOperationError

log = get_logger("remediation.ledger")


class ChangeKind(StrEnum):
    SYSCTL = "sysctl"
    SERVICE = "service"


class ChangeStatus(StrEnum):
    APPLIED = "applied"  # on the host, not yet verified
    VERIFIED = "verified"  # post-conditions held
    CONFIRMED = "confirmed"  # a human kept it, expiry cleared
    REVERTED = "reverted"  # undone
    REVERT_FAILED = "revert_failed"
    UNREVERTIBLE = "unrevertible"  # applied, but has no inverse


# systemd actions and their inverses. restart/reload are absent on purpose:
# they have no meaningful undo, and pretending otherwise would be a lie the
# operator only discovers during an incident.
_SERVICE_INVERSE = {
    "start": "stop",
    "stop": "start",
    "enable": "disable",
    "disable": "enable",
}


@dataclass
class RevertOutcome:
    ok: bool
    summary: str
    status: ChangeStatus


async def record_change(
    session: AsyncSession,
    *,
    workspace_id: str,
    run_id: str | None,
    kind: ChangeKind,
    target: str,
    previous_value: str | None,
    new_value: str,
    conditions: list[dict] | None = None,
    revert_after_seconds: float | None = None,
) -> ChangeRecord:
    """Write the ledger row for a change that is about to be applied."""
    revertible = True
    status = ChangeStatus.APPLIED
    if kind is ChangeKind.SERVICE and new_value not in _SERVICE_INVERSE:
        revertible = False
        status = ChangeStatus.UNREVERTIBLE

    record = ChangeRecord(
        workspace_id=workspace_id,
        run_id=run_id,
        kind=kind.value,
        target=target,
        previous_value=previous_value,
        new_value=new_value,
        revertible=revertible,
        status=status.value,
        conditions=conditions or [],
        expires_at=(
            utcnow() + timedelta(seconds=revert_after_seconds)
            if revert_after_seconds and revertible
            else None
        ),
    )
    session.add(record)
    await session.flush()
    metrics.incr("changes.applied")
    return record


async def revert_change(
    session: AsyncSession,
    policy: SystemPolicy,
    record: ChangeRecord,
    *,
    reason: str = "",
) -> RevertOutcome:
    """Undo one change using its recorded prior state."""
    if record.status in (ChangeStatus.REVERTED.value, ChangeStatus.CONFIRMED.value):
        return RevertOutcome(
            True, f"change {record.id[:12]} needs no revert", ChangeStatus(record.status)
        )
    if not record.revertible:
        return RevertOutcome(
            False,
            f"{record.kind} '{record.new_value}' on {record.target} has no inverse; "
            f"restore it deliberately",
            ChangeStatus.UNREVERTIBLE,
        )

    try:
        ok, summary = await _apply_inverse(policy, record)
    except UnsafeOperationError as exc:
        ok, summary = False, str(exc)

    status = ChangeStatus.REVERTED if ok else ChangeStatus.REVERT_FAILED
    await session.execute(
        update(ChangeRecord)
        .where(ChangeRecord.id == record.id)
        .values(
            status=status.value,
            expires_at=None,
            detail=f"{reason} {summary}".strip(),
            settled_at=utcnow(),
        )
    )
    metrics.incr("changes.reverted" if ok else "changes.revert_failed")
    if not ok:
        # A failed revert leaves the host in a state nobody chose; it must be
        # loud, because the automated safety net has just been shown not to hold.
        log.error(
            "revert failed",
            extra={"hoursx": {"change_id": record.id, "target": record.target}},
        )
    return RevertOutcome(ok, summary, status)


async def _apply_inverse(policy: SystemPolicy, record: ChangeRecord) -> tuple[bool, str]:
    from hoursx.system.ops import manage_service, write_sysctl

    if record.kind == ChangeKind.SYSCTL.value:
        if record.previous_value is None:
            return False, f"no prior value recorded for {record.target}"
        result = await write_sysctl(policy, record.target, record.previous_value)
        return result.ok, result.summary

    if record.kind == ChangeKind.SERVICE.value:
        inverse = _SERVICE_INVERSE.get(record.new_value)
        if inverse is None:
            return False, f"no inverse for service action {record.new_value!r}"
        result = await manage_service(policy, record.target, inverse)
        return result.ok, result.summary

    return False, f"unknown change kind {record.kind!r}"


async def load_change(session: AsyncSession, *, change_id: str, workspace_id: str) -> ChangeRecord:
    record = await session.get(ChangeRecord, change_id)
    if record is None or record.workspace_id != workspace_id:
        raise NotFoundError("change not found", change_id=change_id)
    return record


async def list_changes(
    session: AsyncSession,
    *,
    workspace_id: str,
    run_id: str | None = None,
    limit: int = 50,
) -> list[ChangeRecord]:
    statement = select(ChangeRecord).where(ChangeRecord.workspace_id == workspace_id)
    if run_id:
        statement = statement.where(ChangeRecord.run_id == run_id)
    rows = await session.execute(statement.order_by(ChangeRecord.created_at.desc()).limit(limit))
    return list(rows.scalars().all())


async def revert_expired_changes(services, policy: SystemPolicy | None = None) -> list[str]:
    """Revert changes whose confirmation window elapsed.

    The dead-man switch. A network or firewall change that locks the operator
    out also prevents them from reverting it — so the revert has to be armed
    *before* the change, and fire on silence rather than on request.
    """
    policy = policy or SystemPolicy(
        enabled=services.settings.system_ops_enabled,
        allow_mutations=services.settings.system_mutations_enabled,
        backend=services.settings.system_backend,
        sysd_socket=services.settings.sysd_socket,
    )
    reverted: list[str] = []
    now = utcnow()

    async with services.db.session() as db:
        due = (
            (
                await db.execute(
                    select(ChangeRecord).where(
                        ChangeRecord.status == ChangeStatus.APPLIED.value,
                        ChangeRecord.expires_at.is_not(None),
                        ChangeRecord.expires_at < now,
                    )
                )
            )
            .scalars()
            .all()
        )
        for record in due:
            outcome = await revert_change(db, policy, record, reason="confirmation window elapsed;")
            if outcome.ok:
                reverted.append(record.id)

    if reverted:
        log.warning("reverted %d unconfirmed changes: %s", len(reverted), reverted)
    return reverted
