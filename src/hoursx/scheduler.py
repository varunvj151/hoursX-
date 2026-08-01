"""DB-backed cron schedules.

A schedule fires a goal at an agent on a 5-field cron cadence (UTC). The worker
calls :func:`fire_due_schedules` once per minute; ``last_fired_at`` provides the
at-most-once-per-minute guard, so overlapping worker ticks cannot double-fire.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from hoursx.db.models import Schedule, Session, utcnow
from hoursx.events import Event, EventType
from hoursx.observability import get_logger
from hoursx.services import AppServices

log = get_logger("scheduler")


def _field_matches(field: str, value: int) -> bool:
    for part in field.split(","):
        if part == "*":
            return True
        if part.startswith("*/"):
            step = int(part[2:])
            if step > 0 and value % step == 0:
                return True
        elif "-" in part:
            low, high = part.split("-", 1)
            if int(low) <= value <= int(high):
                return True
        elif part.isdigit() and int(part) == value:
            return True
    return False


def cron_matches(expression: str, at: datetime) -> bool:
    """True when a 5-field cron expression matches minute *at* (UTC)."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields, got {expression!r}")
    minute, hour, day, month, weekday = fields
    return (
        _field_matches(minute, at.minute)
        and _field_matches(hour, at.hour)
        and _field_matches(day, at.day)
        and _field_matches(month, at.month)
        and _field_matches(weekday, at.isoweekday() % 7)  # cron: 0=Sunday
    )


async def fire_due_schedules(services: AppServices, conductor, now: datetime | None = None) -> int:
    """Fire every enabled schedule matching the current minute; returns count.

    Two phases on purpose: the marking transaction commits before any run is
    submitted, so schedule state and run submission never contend for the same
    write transaction (and a crash mid-submit cannot re-fire the minute).
    """
    now = now or utcnow()
    to_fire: list[tuple[str, str, str, str, str]] = []
    # (schedule_id, workspace_id, session_id, user_id, goal)

    async with services.db.session() as db:
        due = (await db.execute(select(Schedule).where(Schedule.enabled.is_(True)))).scalars().all()
        for schedule in due:
            try:
                if not cron_matches(schedule.cron, now):
                    continue
            except ValueError:
                log.warning("schedule %s has invalid cron %r; skipping", schedule.id, schedule.cron)
                continue
            last = schedule.last_fired_at
            if last and last.replace(second=0, microsecond=0) == now.replace(
                second=0, microsecond=0
            ):
                continue  # already fired this minute
            schedule.last_fired_at = now
            session = Session(
                workspace_id=schedule.workspace_id,
                user_id=schedule.user_id,
                agent_profile_id=schedule.agent_profile_id,
                title=f"Scheduled: {schedule.goal[:60]}",
                sandbox_dir=f"schedule-{schedule.id}",
            )
            db.add(session)
            await db.flush()
            to_fire.append(
                (schedule.id, schedule.workspace_id, session.id, schedule.user_id, schedule.goal)
            )

    for schedule_id, workspace_id, session_id, user_id, goal in to_fire:
        await services.bus.publish(
            Event(
                type=EventType.SCHEDULE_FIRED,
                workspace_id=workspace_id,
                session_id=session_id,
                payload={"schedule_id": schedule_id},
            )
        )
        await conductor.submit_message(session_id=session_id, user_id=user_id, text=goal)
    return len(to_fire)
