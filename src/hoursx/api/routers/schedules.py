"""Schedule management (recurring agent goals)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from hoursx.api.deps import Actor, get_services, require
from hoursx.api.schemas import ScheduleIn, ScheduleOut
from hoursx.auth import Permission
from hoursx.db.models import AgentProfile, Schedule, utcnow
from hoursx.scheduler import cron_matches
from hoursx.services import AppServices

router = APIRouter(prefix="/v1/schedules", tags=["schedules"])


def _out(schedule: Schedule) -> ScheduleOut:
    return ScheduleOut(
        id=schedule.id,
        agent_profile_id=schedule.agent_profile_id,
        cron=schedule.cron,
        goal=schedule.goal,
        enabled=schedule.enabled,
        last_fired_at=schedule.last_fired_at,
        created_at=schedule.created_at,
    )


@router.get("", response_model=list[ScheduleOut])
async def list_schedules(
    actor: Actor = Depends(require(Permission.SCHEDULES_MANAGE)),
    services: AppServices = Depends(get_services),
) -> list[ScheduleOut]:
    async with services.db.session() as db:
        rows = (
            (
                await db.execute(
                    select(Schedule)
                    .where(Schedule.workspace_id == actor.workspace.id)
                    .order_by(Schedule.created_at)
                )
            )
            .scalars()
            .all()
        )
        return [_out(row) for row in rows]


@router.post("", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    body: ScheduleIn,
    actor: Actor = Depends(require(Permission.SCHEDULES_MANAGE)),
    services: AppServices = Depends(get_services),
) -> ScheduleOut:
    try:
        cron_matches(body.cron, utcnow())  # validate the expression up front
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    async with services.db.session() as db:
        profile = await db.get(AgentProfile, body.agent_profile_id)
        if profile is None or profile.workspace_id != actor.workspace.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "agent profile not found")
        schedule = Schedule(
            workspace_id=actor.workspace.id, user_id=actor.user.id, **body.model_dump()
        )
        db.add(schedule)
        await db.flush()
        return _out(schedule)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    schedule_id: str,
    actor: Actor = Depends(require(Permission.SCHEDULES_MANAGE)),
    services: AppServices = Depends(get_services),
) -> None:
    async with services.db.session() as db:
        schedule = await db.get(Schedule, schedule_id)
        if schedule is None or schedule.workspace_id != actor.workspace.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "schedule not found")
        await db.delete(schedule)
