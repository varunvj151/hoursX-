"""Sessions and messages: the conversational surface."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy import select

from hoursx.api.deps import Actor, get_conductor, get_services, require
from hoursx.api.schemas import (
    MessageIn,
    MessageOut,
    SessionCreate,
    SessionOut,
    SubmitResponse,
)
from hoursx.auth import Permission
from hoursx.db.models import AgentProfile, Message, Session
from hoursx.errors import NotFoundError
from hoursx.orchestration import Conductor
from hoursx.services import AppServices

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


def _out(session: Session) -> SessionOut:
    return SessionOut(
        id=session.id,
        title=session.title,
        agent_profile_id=session.agent_profile_id,
        created_at=session.created_at,
        archived=session.archived,
    )


async def _owned_session(services: AppServices, actor: Actor, session_id: str) -> Session:
    async with services.db.session() as db:
        session = await db.get(Session, session_id)
        if session is None or session.workspace_id != actor.workspace.id:
            raise NotFoundError("session not found", session_id=session_id)
        return session


@router.get("", response_model=list[SessionOut])
async def list_sessions(
    actor: Actor = Depends(require(Permission.SESSIONS_USE)),
    services: AppServices = Depends(get_services),
) -> list[SessionOut]:
    async with services.db.session() as db:
        rows = (
            (
                await db.execute(
                    select(Session)
                    .where(
                        Session.workspace_id == actor.workspace.id,
                        Session.archived.is_(False),
                    )
                    .order_by(Session.created_at.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        return [_out(row) for row in rows]


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: SessionCreate,
    actor: Actor = Depends(require(Permission.SESSIONS_USE)),
    services: AppServices = Depends(get_services),
) -> SessionOut:
    async with services.db.session() as db:
        profile = await db.get(AgentProfile, body.agent_profile_id)
        if profile is None or profile.workspace_id != actor.workspace.id:
            raise NotFoundError("agent profile not found", agent_profile_id=body.agent_profile_id)
        session = Session(
            workspace_id=actor.workspace.id,
            user_id=actor.user.id,
            agent_profile_id=profile.id,
            title=body.title,
            sandbox_dir="",
        )
        db.add(session)
        await db.flush()
        session.sandbox_dir = f"session-{session.id}"
        await db.flush()
        return _out(session)


@router.get("/{session_id}/messages", response_model=list[MessageOut])
async def list_messages(
    session_id: str,
    actor: Actor = Depends(require(Permission.SESSIONS_USE)),
    services: AppServices = Depends(get_services),
) -> list[MessageOut]:
    await _owned_session(services, actor, session_id)
    async with services.db.session() as db:
        rows = (
            (
                await db.execute(
                    select(Message)
                    .where(Message.session_id == session_id)
                    .order_by(Message.created_at, Message.id)
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )
        return [
            MessageOut(
                id=row.id,
                role=row.role,
                content=row.content,
                run_id=row.run_id,
                created_at=row.created_at,
            )
            for row in rows
        ]


@router.post(
    "/{session_id}/messages",
    response_model=SubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_message(
    session_id: str,
    body: MessageIn,
    actor: Actor = Depends(require(Permission.SESSIONS_USE)),
    services: AppServices = Depends(get_services),
    conductor: Conductor = Depends(get_conductor),
) -> SubmitResponse:
    """Accept a user message; the run executes asynchronously. Subscribe to the
    WebSocket (or poll the run) for progress and the final answer."""
    await _owned_session(services, actor, session_id)
    run_id = await conductor.submit_message(
        session_id=session_id,
        user_id=actor.user.id,
        text=body.text,
        plan_first=body.plan_first,
        idempotency_key=body.idempotency_key,
    )
    return SubmitResponse(run_id=run_id)
