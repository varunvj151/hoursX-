"""Background jobs (arq worker).

arq over Celery: it is asyncio-native — job functions are ordinary coroutines
sharing the same service graph as the API — and it rides the Redis instance the
event bus already requires. The worker builds one :class:`AppServices` at
startup and reuses it for every job.

Jobs are deliberately thin: load ids, call the same runtime/engine code the
inline backend uses. There is no logic that exists only in the queue path.
"""

from __future__ import annotations

from typing import Any

from hoursx.config import HoursXSettings, get_settings
from hoursx.observability import configure_logging, get_logger

log = get_logger("jobs")


async def enqueue_job(settings: HoursXSettings, name: str, *args: Any) -> None:
    """Enqueue a job onto the arq queue (requires ``HOURSX_REDIS_URL``)."""
    from arq import create_pool
    from arq.connections import RedisSettings

    if not settings.redis_url:
        raise RuntimeError("task_backend=arq requires HOURSX_REDIS_URL")
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await pool.enqueue_job(name, *args)
    finally:
        await pool.aclose()


# ------------------------------------------------------------------- job bodies


async def execute_run_job(ctx: dict, run_id: str) -> None:
    await ctx["conductor"].runtime.execute_run(run_id)


async def resume_run_job(ctx: dict, run_id: str, approval_id: str, approved: bool) -> None:
    await ctx["conductor"].runtime.resume_run(run_id, approval_id=approval_id, approved=approved)


async def ingest_document_job(ctx: dict, document_id: str, text: str) -> None:
    from hoursx.api.ingest import ingest_and_announce

    await ingest_and_announce(ctx["services"], document_id, text)


async def recover_orphaned_runs_job(ctx: dict) -> None:
    from hoursx.recovery import requeue_orphaned_runs

    requeued = await requeue_orphaned_runs(ctx["services"])
    for run_id in requeued:
        await enqueue_job(ctx["services"].settings, "execute_run_job", run_id)


async def revert_expired_changes_job(ctx: dict) -> None:
    from hoursx.remediation.ledger import revert_expired_changes

    reverted = await revert_expired_changes(ctx["services"])
    if reverted:
        log.warning("reverted %d unconfirmed host changes", len(reverted))


async def dispatch_channel_replies_job(ctx: dict) -> None:
    """Safety net for the API-side dispatch loop.

    The API process normally settles replies within seconds. This exists for the
    case where the replica holding that loop died with obligations outstanding:
    a late answer is recoverable, a lost one is not.
    """
    from hoursx.channels.dispatch import ChannelDispatcher

    services = ctx["services"]
    if not len(services.channels):
        return
    sent = await ChannelDispatcher(services, services.channels).sweep_once()
    if sent:
        log.info("dispatched %d channel replies", sent)


async def fire_schedules_job(ctx: dict) -> None:
    from hoursx.scheduler import fire_due_schedules

    fired = await fire_due_schedules(ctx["services"], ctx["conductor"])
    if fired:
        log.info("fired %d schedules", fired)


# ---------------------------------------------------------------- worker setup


async def _startup(ctx: dict) -> None:
    from hoursx.orchestration import Conductor
    from hoursx.services import build_services

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    services = build_services(settings)
    await services.db.create_all()
    ctx["services"] = services
    ctx["conductor"] = Conductor(services)
    log.info("worker ready")


async def _shutdown(ctx: dict) -> None:
    if services := ctx.get("services"):
        await services.db.dispose()


def worker_settings_class() -> type:
    """Build the arq ``WorkerSettings`` (deferred so importing this module never
    requires Redis to be reachable)."""
    from arq import cron
    from arq.connections import RedisSettings

    settings = get_settings()
    if not settings.redis_url:
        raise RuntimeError("the worker requires HOURSX_REDIS_URL")

    class WorkerSettings:
        functions = [
            execute_run_job,
            resume_run_job,
            ingest_document_job,
            recover_orphaned_runs_job,
            revert_expired_changes_job,
            dispatch_channel_replies_job,
        ]
        cron_jobs = [
            cron(fire_schedules_job, minute=set(range(60))),
            cron(dispatch_channel_replies_job, minute=set(range(60))),
            # Dead-man sweep: unconfirmed changes must not outlive their window.
            cron(revert_expired_changes_job, minute=set(range(60))),
            # Sweep for runs abandoned by crashed workers every 5 minutes.
            cron(recover_orphaned_runs_job, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        ]
        on_startup = _startup
        on_shutdown = _shutdown
        redis_settings = RedisSettings.from_dsn(settings.redis_url)
        max_jobs = 20
        job_timeout = 3600

    return WorkerSettings
