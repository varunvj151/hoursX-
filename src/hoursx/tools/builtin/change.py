"""Guarded-change tools.

These replace bare mutation with *provisional* mutation: the agent states what
should become true after the change, and the platform holds it to that. A change
that does not produce its stated effect is reverted rather than left behind.

The `verify` argument is the important part of the model-facing contract. Tool
descriptions say plainly that declaring post-conditions makes the change
self-reverting, because a capability the model is not told about does not exist.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from hoursx.remediation.conditions import Condition
from hoursx.remediation.guard import apply_guarded, confirm_change
from hoursx.remediation.ledger import (
    ChangeKind,
    ChangeStatus,
    list_changes,
    load_change,
    revert_change,
)
from hoursx.system.privileges import SystemPolicy, UnsafeOperationError
from hoursx.tools.base import FunctionTool, ToolContext, ToolOutcome, ToolSpec
from hoursx.tools.registry import ToolRegistry


def _policy(ctx: ToolContext) -> SystemPolicy:
    services = ctx.services
    if services is None:
        return SystemPolicy()
    settings = services.settings
    return SystemPolicy(
        enabled=settings.system_ops_enabled,
        allow_mutations=settings.system_mutations_enabled,
        extra_sysctl_allowlist=frozenset(settings.system_sysctl_allowlist),
        backend=settings.system_backend,
        sysd_socket=settings.sysd_socket,
    )


class GuardedSysctlArgs(BaseModel):
    key: str = Field(description="Dotted sysctl key, e.g. 'vm.swappiness'")
    value: str = Field(description="New value to write")
    verify: list[Condition] = Field(
        default_factory=list,
        description=(
            "Post-conditions that must hold after the change. If any fails, the "
            "change is reverted automatically to its previous value."
        ),
    )
    settle_seconds: float = Field(
        default=2.0,
        ge=0,
        le=60,
        description="How long to wait for the change to take effect before verifying",
    )
    revert_after_seconds: float | None = Field(
        default=None,
        description=(
            "Optional dead-man timer: revert unless a human confirms within this "
            "many seconds. Use for changes that could cut off access."
        ),
    )


class GuardedServiceArgs(BaseModel):
    unit: str = Field(description="systemd unit, e.g. 'nginx'")
    action: str = Field(pattern="^(start|stop|restart|reload|enable|disable)$")
    verify: list[Condition] = Field(default_factory=list)
    settle_seconds: float = Field(default=3.0, ge=0, le=60)
    revert_after_seconds: float | None = Field(default=None)


class ChangeListArgs(BaseModel):
    this_run_only: bool = Field(
        default=True, description="Limit to changes made during the current run"
    )


class ChangeIdArgs(BaseModel):
    change_id: str = Field(description="Change id, or a unique prefix of one")


async def _guarded_sysctl(args: GuardedSysctlArgs, ctx: ToolContext) -> ToolOutcome:
    if ctx.services is None:
        return ToolOutcome.failure("Guarded changes are unavailable in this context.")
    try:
        outcome = await apply_guarded(
            ctx.services,
            _policy(ctx),
            workspace_id=ctx.workspace_id,
            run_id=ctx.run_id,
            kind=ChangeKind.SYSCTL,
            target=args.key,
            new_value=args.value,
            conditions=args.verify,
            settle_seconds=args.settle_seconds,
            revert_after_seconds=args.revert_after_seconds,
        )
    except UnsafeOperationError as exc:
        return ToolOutcome.failure(str(exc))
    return ToolOutcome(ok=outcome.ok, summary=outcome.summary, data=outcome.as_payload())


async def _guarded_service(args: GuardedServiceArgs, ctx: ToolContext) -> ToolOutcome:
    if ctx.services is None:
        return ToolOutcome.failure("Guarded changes are unavailable in this context.")
    try:
        outcome = await apply_guarded(
            ctx.services,
            _policy(ctx),
            workspace_id=ctx.workspace_id,
            run_id=ctx.run_id,
            kind=ChangeKind.SERVICE,
            target=args.unit,
            new_value=args.action,
            conditions=args.verify,
            settle_seconds=args.settle_seconds,
            revert_after_seconds=args.revert_after_seconds,
        )
    except UnsafeOperationError as exc:
        return ToolOutcome.failure(str(exc))
    return ToolOutcome(ok=outcome.ok, summary=outcome.summary, data=outcome.as_payload())


async def _change_list(args: ChangeListArgs, ctx: ToolContext) -> ToolOutcome:
    if ctx.services is None:
        return ToolOutcome.failure("The change ledger is unavailable in this context.")
    async with ctx.services.db.session() as db:
        records = await list_changes(
            db,
            workspace_id=ctx.workspace_id,
            run_id=ctx.run_id if args.this_run_only else None,
        )
    if not records:
        return ToolOutcome.success("No host changes recorded.", changes=[])
    return ToolOutcome.success(
        f"{len(records)} recorded change(s)",
        changes=[
            {
                "id": record.id,
                "kind": record.kind,
                "target": record.target,
                "previous": record.previous_value,
                "new": record.new_value,
                "status": record.status,
                "revertible": record.revertible,
                "detail": record.detail,
            }
            for record in records
        ],
    )


async def _resolve(ctx: ToolContext, prefix: str):
    """Accept an id prefix — the model works from the short ids it was shown."""
    async with ctx.services.db.session() as db:
        records = await list_changes(db, workspace_id=ctx.workspace_id, limit=200)
    return next((record for record in records if record.id.startswith(prefix)), None)


async def _change_revert(args: ChangeIdArgs, ctx: ToolContext) -> ToolOutcome:
    if ctx.services is None:
        return ToolOutcome.failure("The change ledger is unavailable in this context.")
    match = await _resolve(ctx, args.change_id)
    if match is None:
        return ToolOutcome.failure(
            f"No change matching {args.change_id!r}. Use change.list to see recorded changes."
        )
    async with ctx.services.db.session() as db:
        record = await load_change(db, change_id=match.id, workspace_id=ctx.workspace_id)
        outcome = await revert_change(db, _policy(ctx), record, reason="requested by agent;")
    return ToolOutcome(
        ok=outcome.ok,
        summary=outcome.summary,
        data={"change_id": match.id, "status": outcome.status.value},
    )


async def _change_confirm(args: ChangeIdArgs, ctx: ToolContext) -> ToolOutcome:
    if ctx.services is None:
        return ToolOutcome.failure("The change ledger is unavailable in this context.")
    match = await _resolve(ctx, args.change_id)
    if match is None:
        return ToolOutcome.failure(f"No change matching {args.change_id!r}.")
    outcome = await confirm_change(
        ctx.services,
        change_id=match.id,
        workspace_id=ctx.workspace_id,
        confirmed_by=f"run:{ctx.run_id}",
    )
    return ToolOutcome(
        ok=outcome.ok,
        summary=outcome.summary,
        data={"change_id": match.id, "status": outcome.status.value},
    )


def register_change_tools(registry: ToolRegistry) -> None:
    registry.register(
        FunctionTool(
            ToolSpec(
                name="change.sysctl",
                description=(
                    "Change a kernel parameter under verification. Declare post-conditions "
                    "in 'verify' and the change reverts itself automatically if they do not "
                    "hold. Prefer this over system.sysctl_set whenever you can state what "
                    "the change should achieve."
                ),
                params_model=GuardedSysctlArgs,
                requires_approval=True,
                timeout_seconds=180,
            ),
            _guarded_sysctl,
        )
    )
    registry.register(
        FunctionTool(
            ToolSpec(
                name="change.service",
                description=(
                    "Start, stop, or reconfigure a service under verification. Declare "
                    "post-conditions in 'verify'; start/stop and enable/disable revert "
                    "automatically when they fail. restart and reload have no inverse."
                ),
                params_model=GuardedServiceArgs,
                requires_approval=True,
                timeout_seconds=180,
            ),
            _guarded_service,
        )
    )
    registry.register(
        FunctionTool(
            ToolSpec(
                name="change.list",
                description="List host changes recorded for this run, with their revert state.",
                params_model=ChangeListArgs,
            ),
            _change_list,
        )
    )
    registry.register(
        FunctionTool(
            ToolSpec(
                name="change.revert",
                description="Undo a recorded change, restoring the value captured before it.",
                params_model=ChangeIdArgs,
                requires_approval=True,
            ),
            _change_revert,
        )
    )
    registry.register(
        FunctionTool(
            ToolSpec(
                name="change.confirm",
                description=(
                    "Keep a change that carries a dead-man timer, cancelling its automatic "
                    "revert. Only do this once you have evidence the change is correct."
                ),
                params_model=ChangeIdArgs,
                requires_approval=True,
            ),
            _change_confirm,
        )
    )


__all__ = ["ChangeStatus", "register_change_tools"]
