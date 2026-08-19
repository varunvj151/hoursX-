"""Guarded change orchestration.

The sequence, and why it is in this order:

1. **Read prior state** — before touching anything, so the revert path exists
   even if the apply half-succeeds.
2. **Ledger the change** — persisted before application, so a crash between
   apply and record cannot strand an unrecorded mutation.
3. **Apply.**
4. **Settle** — kernel tunables and services do not take effect instantly;
   verifying immediately would measure the old state and revert a good change.
5. **Verify** — evaluate the declared post-conditions.
6. **Revert on failure** — including when verification itself could not run.
   A change whose effect cannot be confirmed is not a change worth keeping.

Nothing here decides *whether* a change is permitted; that is
:mod:`hoursx.system.privileges`, which has already run by the time a guard is
invoked.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from hoursx.db.models import ChangeRecord, utcnow
from hoursx.events import Event, EventType
from hoursx.observability import get_logger, metrics
from hoursx.remediation.conditions import Condition, ConditionResult, all_met, evaluate_conditions
from hoursx.remediation.ledger import (
    ChangeKind,
    ChangeStatus,
    record_change,
    revert_change,
)
from hoursx.system.privileges import SystemPolicy, UnsafeOperationError

log = get_logger("remediation.guard")

# Verification runs inside a tool call, so the settle window has to stay well
# inside the tool timeout or the guard would be killed mid-verify.
MAX_SETTLE_SECONDS = 60.0
MAX_REVERT_AFTER_SECONDS = 3600.0


@dataclass
class GuardOutcome:
    ok: bool
    summary: str
    change_id: str | None = None
    status: ChangeStatus = ChangeStatus.APPLIED
    results: list[ConditionResult] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def as_payload(self) -> dict:
        return {
            **self.detail,
            "change_id": self.change_id,
            "status": self.status.value,
            "verification": [
                {
                    "condition": result.condition.describe(),
                    "met": result.met,
                    "observed": result.observed,
                    "detail": result.detail,
                }
                for result in self.results
            ],
        }


async def apply_guarded(
    services,
    policy: SystemPolicy,
    *,
    workspace_id: str,
    run_id: str | None,
    kind: ChangeKind,
    target: str,
    new_value: str,
    conditions: list[Condition] | None = None,
    settle_seconds: float = 0.0,
    revert_after_seconds: float | None = None,
) -> GuardOutcome:
    """Apply a change under verification, reverting it if it does not hold."""
    conditions = conditions or []
    settle = max(0.0, min(settle_seconds, MAX_SETTLE_SECONDS))
    revert_after = (
        max(1.0, min(revert_after_seconds, MAX_REVERT_AFTER_SECONDS))
        if revert_after_seconds
        else None
    )

    previous = await _read_prior_state(policy, kind, target)

    async with services.db.session() as db:
        record = await record_change(
            db,
            workspace_id=workspace_id,
            run_id=run_id,
            kind=kind,
            target=target,
            previous_value=previous,
            new_value=new_value,
            conditions=[condition.model_dump(mode="json") for condition in conditions],
            revert_after_seconds=revert_after,
        )
        change_id = record.id
        revertible = record.revertible

    try:
        applied_ok, apply_summary = await _apply(policy, kind, target, new_value)
    except UnsafeOperationError as exc:
        await _settle(services, change_id, ChangeStatus.REVERTED, detail=str(exc))
        return GuardOutcome(False, str(exc), change_id, ChangeStatus.REVERTED)

    if not applied_ok:
        # Nothing changed, so there is nothing to undo — close the row rather
        # than leaving a phantom "applied" change in the ledger.
        await _settle(services, change_id, ChangeStatus.REVERTED, detail=apply_summary)
        return GuardOutcome(False, apply_summary, change_id, ChangeStatus.REVERTED)

    await _emit(
        services,
        workspace_id,
        run_id,
        EventType.CHANGE_APPLIED,
        {"change_id": change_id, "kind": kind.value, "target": target, "value": new_value},
    )

    if not conditions:
        detail = apply_summary
        if revert_after:
            detail += (
                f" — reverts automatically in {revert_after:.0f}s unless confirmed "
                f"(change.confirm {change_id[:12]})"
            )
        return GuardOutcome(
            True,
            detail,
            change_id,
            ChangeStatus.APPLIED,
            detail={"previous": previous, "applied": apply_summary, "expires_in": revert_after},
        )

    if settle:
        await asyncio.sleep(settle)

    results = await evaluate_conditions(conditions)
    if all_met(results):
        await _settle(services, change_id, ChangeStatus.VERIFIED, detail="post-conditions held")
        metrics.incr("changes.verified")
        await _emit(
            services,
            workspace_id,
            run_id,
            EventType.CHANGE_VERIFIED,
            {"change_id": change_id, "target": target},
        )
        held = "; ".join(result.describe() for result in results)
        summary = f"{apply_summary} — verified ({held})"
        if revert_after:
            summary += f"; still reverts in {revert_after:.0f}s unless confirmed"
        return GuardOutcome(
            True,
            summary,
            change_id,
            ChangeStatus.VERIFIED,
            results,
            {"previous": previous, "applied": apply_summary},
        )

    failed = "; ".join(result.describe() for result in results if not result.met)
    if not revertible:
        await _settle(
            services, change_id, ChangeStatus.UNREVERTIBLE, detail=f"unverified: {failed}"
        )
        return GuardOutcome(
            False,
            f"{apply_summary} — post-conditions did NOT hold ({failed}), and this action "
            f"has no automatic inverse. Restore it deliberately.",
            change_id,
            ChangeStatus.UNREVERTIBLE,
            results,
        )

    async with services.db.session() as db:
        record = await db.get(ChangeRecord, change_id)
        assert record is not None
        revert = await revert_change(db, policy, record, reason="post-conditions failed;")

    await _emit(
        services,
        workspace_id,
        run_id,
        EventType.CHANGE_REVERTED,
        {"change_id": change_id, "target": target, "ok": revert.ok, "reason": failed},
    )
    if revert.ok:
        return GuardOutcome(
            False,
            f"Post-conditions did not hold ({failed}). The change was reverted to "
            f"{previous!r}. Try a different value or investigate further.",
            change_id,
            ChangeStatus.REVERTED,
            results,
        )
    return GuardOutcome(
        False,
        f"Post-conditions did not hold ({failed}) AND the revert failed "
        f"({revert.summary}). {target} may be left at {new_value!r} — check it.",
        change_id,
        ChangeStatus.REVERT_FAILED,
        results,
    )


async def confirm_change(
    services, *, change_id: str, workspace_id: str, confirmed_by: str
) -> GuardOutcome:
    """Keep a change past its confirmation window, clearing the dead-man timer."""
    from hoursx.remediation.ledger import load_change

    async with services.db.session() as db:
        record = await load_change(db, change_id=change_id, workspace_id=workspace_id)
        if record.status in (ChangeStatus.REVERTED.value, ChangeStatus.REVERT_FAILED.value):
            return GuardOutcome(
                False,
                f"change {change_id[:12]} was already reverted; nothing to confirm",
                change_id,
                ChangeStatus(record.status),
            )
        record.status = ChangeStatus.CONFIRMED.value
        record.expires_at = None
        record.settled_at = utcnow()
        record.detail = f"confirmed by {confirmed_by}"
        target = record.target

    metrics.incr("changes.confirmed")
    return GuardOutcome(
        True, f"{target} confirmed; it will not be reverted", change_id, ChangeStatus.CONFIRMED
    )


# ------------------------------------------------------------------- internals


async def _read_prior_state(policy: SystemPolicy, kind: ChangeKind, target: str) -> str | None:
    if kind is ChangeKind.SYSCTL:
        from hoursx.system.probe import read_sysctl

        return read_sysctl(target)
    if kind is ChangeKind.SERVICE:
        from hoursx.system.ops import manage_service

        try:
            result = await manage_service(policy, target, "is-active")
        except UnsafeOperationError:
            return None
        return result.detail.get("output", "").strip() or None
    return None


async def _apply(
    policy: SystemPolicy, kind: ChangeKind, target: str, new_value: str
) -> tuple[bool, str]:
    from hoursx.system.ops import manage_service, write_sysctl

    if kind is ChangeKind.SYSCTL:
        result = await write_sysctl(policy, target, new_value)
    else:
        result = await manage_service(policy, target, new_value)
    return result.ok, result.summary


async def _settle(services, change_id: str, status: ChangeStatus, *, detail: str) -> None:
    from sqlalchemy import update

    async with services.db.session() as db:
        await db.execute(
            update(ChangeRecord)
            .where(ChangeRecord.id == change_id)
            .values(status=status.value, detail=detail, settled_at=utcnow(), expires_at=None)
        )


async def _emit(services, workspace_id: str, run_id: str | None, type_: EventType, payload: dict):
    await services.bus.publish(
        Event(type=type_, workspace_id=workspace_id, run_id=run_id, payload=payload)
    )
