"""Agent profile management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy import select

from hoursx.api.deps import Actor, get_services, require
from hoursx.api.schemas import AgentProfileIn, AgentProfileOut
from hoursx.audit import AuditAction, record
from hoursx.auth import Permission
from hoursx.db.models import AgentProfile
from hoursx.errors import ConflictError, NotFoundError
from hoursx.services import AppServices

router = APIRouter(prefix="/v1/agents", tags=["agents"])


def _out(profile: AgentProfile) -> AgentProfileOut:
    return AgentProfileOut(
        id=profile.id,
        handle=profile.handle,
        title=profile.title,
        instructions=profile.instructions,
        model_alias=profile.model_alias,
        tool_grants=list(profile.tool_grants or []),
        can_delegate=profile.can_delegate,
        max_steps=profile.max_steps,
        created_at=profile.created_at,
    )


@router.get("", response_model=list[AgentProfileOut])
async def list_agents(
    actor: Actor = Depends(require(Permission.SESSIONS_USE)),
    services: AppServices = Depends(get_services),
) -> list[AgentProfileOut]:
    async with services.db.session() as db:
        rows = (
            (
                await db.execute(
                    select(AgentProfile)
                    .where(AgentProfile.workspace_id == actor.workspace.id)
                    .order_by(AgentProfile.created_at)
                )
            )
            .scalars()
            .all()
        )
        return [_out(row) for row in rows]


@router.post("", response_model=AgentProfileOut, status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: AgentProfileIn,
    actor: Actor = Depends(require(Permission.AGENTS_MANAGE)),
    services: AppServices = Depends(get_services),
) -> AgentProfileOut:
    async with services.db.session() as db:
        duplicate = (
            await db.execute(
                select(AgentProfile).where(
                    AgentProfile.workspace_id == actor.workspace.id,
                    AgentProfile.handle == body.handle,
                )
            )
        ).scalar_one_or_none()
        if duplicate is not None:
            raise ConflictError(f"agent handle {body.handle!r} already exists in this workspace")
        profile = AgentProfile(workspace_id=actor.workspace.id, **body.model_dump())
        db.add(profile)
        await db.flush()
        await record(
            db,
            workspace_id=actor.workspace.id,
            actor_user_id=actor.user.id,
            action=AuditAction.AGENT_CREATED,
            target_type="agent_profile",
            target_id=profile.id,
            handle=profile.handle,
        )
        return _out(profile)


@router.put("/{agent_id}", response_model=AgentProfileOut)
async def update_agent(
    agent_id: str,
    body: AgentProfileIn,
    actor: Actor = Depends(require(Permission.AGENTS_MANAGE)),
    services: AppServices = Depends(get_services),
) -> AgentProfileOut:
    async with services.db.session() as db:
        profile = await db.get(AgentProfile, agent_id)
        if profile is None or profile.workspace_id != actor.workspace.id:
            raise NotFoundError("agent not found", agent_id=agent_id)
        for field, value in body.model_dump().items():
            setattr(profile, field, value)
        await db.flush()
        return _out(profile)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: str,
    actor: Actor = Depends(require(Permission.AGENTS_MANAGE)),
    services: AppServices = Depends(get_services),
) -> None:
    async with services.db.session() as db:
        profile = await db.get(AgentProfile, agent_id)
        if profile is None or profile.workspace_id != actor.workspace.id:
            raise NotFoundError("agent not found", agent_id=agent_id)
        await db.delete(profile)
