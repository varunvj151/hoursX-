"""Per-workspace execution quotas.

Two independent limits, because they stop different failure modes:

- **Concurrency** bounds simultaneously active runs. Without it one workspace
  can occupy every worker slot and starve all others — the noisy-neighbour
  problem, and the main reason a shared deployment falls over.
- **Hourly rate** bounds total starts. This catches a runaway loop (an agent or
  a schedule submitting continuously) that concurrency alone would permit.

Both are enforced against the database rather than an in-memory counter, so the
limit holds across API replicas and survives restarts. Setting either to 0
disables it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hoursx.db.models import Run, utcnow
from hoursx.errors import QuotaExceededError

_ACTIVE_STATUSES = ("queued", "running", "awaiting_approval")


@dataclass(frozen=True)
class QuotaPolicy:
    max_concurrent_runs: int = 8
    max_runs_per_hour: int = 240

    @property
    def concurrency_enabled(self) -> bool:
        return self.max_concurrent_runs > 0

    @property
    def rate_enabled(self) -> bool:
        return self.max_runs_per_hour > 0


@dataclass(frozen=True)
class QuotaUsage:
    active_runs: int
    runs_last_hour: int


async def current_usage(session: AsyncSession, *, workspace_id: str) -> QuotaUsage:
    """Measure a workspace's live consumption."""
    active = (
        await session.execute(
            select(func.count(Run.id)).where(
                Run.workspace_id == workspace_id, Run.status.in_(_ACTIVE_STATUSES)
            )
        )
    ).scalar_one()
    since = utcnow() - timedelta(hours=1)
    recent = (
        await session.execute(
            select(func.count(Run.id)).where(
                Run.workspace_id == workspace_id, Run.created_at >= since
            )
        )
    ).scalar_one()
    return QuotaUsage(active_runs=int(active), runs_last_hour=int(recent))


async def enforce_run_quota(
    session: AsyncSession, *, workspace_id: str, policy: QuotaPolicy
) -> QuotaUsage:
    """Raise :class:`QuotaExceededError` if starting a run would breach a limit.

    The error names the limit and the current value so a client can back off
    intelligently instead of blind-retrying.
    """
    usage = await current_usage(session, workspace_id=workspace_id)
    if policy.concurrency_enabled and usage.active_runs >= policy.max_concurrent_runs:
        raise QuotaExceededError(
            f"workspace already has {usage.active_runs} active runs "
            f"(limit {policy.max_concurrent_runs}); wait for one to finish",
            limit="max_concurrent_runs",
            limit_value=policy.max_concurrent_runs,
            current=usage.active_runs,
        )
    if policy.rate_enabled and usage.runs_last_hour >= policy.max_runs_per_hour:
        raise QuotaExceededError(
            f"workspace started {usage.runs_last_hour} runs in the last hour "
            f"(limit {policy.max_runs_per_hour})",
            limit="max_runs_per_hour",
            limit_value=policy.max_runs_per_hour,
            current=usage.runs_last_hour,
        )
    return usage
