"""Embedded engine for CLI and GUI use.

The CLI runs agents **in-process** rather than talking to a server. That is a
deliberate choice: an operator debugging a host at 3am should not have to stand
up PostgreSQL, Redis, and an API just to ask an agent a question. The same
service graph the server uses is assembled locally against SQLite, so behaviour
is identical — only the transport is absent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from sqlalchemy import select

from hoursx.auth.rbac import Role
from hoursx.config import HoursXSettings, get_settings
from hoursx.db.models import AgentProfile, Session, User, Workspace, WorkspaceMember
from hoursx.events import Event, EventType
from hoursx.orchestration import Conductor
from hoursx.services import AppServices, build_services

LOCAL_EMAIL = "local@hoursx.invalid"
LOCAL_WORKSPACE_SLUG = "local"

DEFAULT_AGENT_GRANTS = [
    "fs.*",
    "code.patch",
    "shell.run",
    "git.*",
    "http.fetch",
    "knowledge.*",
    "memory.*",
    "system.*",
    "change.*",
]


@dataclass
class LocalContext:
    services: AppServices
    conductor: Conductor
    user_id: str
    workspace_id: str


async def open_local(settings: HoursXSettings | None = None) -> LocalContext:
    """Build the local engine, provisioning the implicit workspace on first use.

    A single-operator CLI still needs a user and workspace because every domain
    object is tenant-scoped. Rather than force a signup flow, one local identity
    is created on demand and reused.
    """
    settings = settings or get_settings()
    services = build_services(settings)
    await services.db.create_all()

    async with services.db.session() as db:
        user = (
            await db.execute(select(User).where(User.email == LOCAL_EMAIL))
        ).scalar_one_or_none()
        if user is None:
            user = User(
                email=LOCAL_EMAIL,
                display_name="Local Operator",
                # Unusable password hash: this identity is reachable only from
                # the local process, never through the HTTP API.
                password_hash="local-only-no-login",
            )
            db.add(user)
            await db.flush()
        workspace = (
            await db.execute(select(Workspace).where(Workspace.slug == LOCAL_WORKSPACE_SLUG))
        ).scalar_one_or_none()
        if workspace is None:
            workspace = Workspace(name="Local", slug=LOCAL_WORKSPACE_SLUG)
            db.add(workspace)
            await db.flush()
        membership = (
            await db.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == workspace.id,
                    WorkspaceMember.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            db.add(
                WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=Role.OWNER.value)
            )
        user_id, workspace_id = user.id, workspace.id

    return LocalContext(
        services=services,
        conductor=Conductor(services),
        user_id=user_id,
        workspace_id=workspace_id,
    )


async def close_local(context: LocalContext) -> None:
    await context.conductor.wait_for_inline_runs()
    await context.services.db.dispose()


async def ensure_agent(
    context: LocalContext,
    handle: str = "operator",
    *,
    title: str = "Operator Agent",
    instructions: str = "",
    model_alias: str = "deep",
    grants: list[str] | None = None,
) -> str:
    """Return an agent id, creating the profile if it does not exist."""
    async with context.services.db.session() as db:
        profile = (
            await db.execute(
                select(AgentProfile).where(
                    AgentProfile.workspace_id == context.workspace_id,
                    AgentProfile.handle == handle,
                )
            )
        ).scalar_one_or_none()
        if profile is None:
            profile = AgentProfile(
                workspace_id=context.workspace_id,
                handle=handle,
                title=title,
                instructions=instructions
                or "You operate this machine. Diagnose before you change anything.",
                model_alias=model_alias,
                tool_grants=grants or DEFAULT_AGENT_GRANTS,
                can_delegate=True,
            )
            db.add(profile)
            await db.flush()
        return profile.id


async def ensure_session(context: LocalContext, agent_id: str, title: str) -> str:
    async with context.services.db.session() as db:
        session = Session(
            workspace_id=context.workspace_id,
            user_id=context.user_id,
            agent_profile_id=agent_id,
            title=title,
            sandbox_dir="",
        )
        db.add(session)
        await db.flush()
        session.sandbox_dir = f"session-{session.id}"
        await db.flush()
        return session.id


async def stream_run(
    context: LocalContext,
    *,
    session_id: str,
    text: str,
    on_event: Callable[[Event], None] | None = None,
) -> Event | None:
    """Submit a goal and yield control until the run reaches a terminal state.

    Subscribes *before* submitting so no early delta is missed — the run can
    start producing output before ``submit_message`` returns.
    """
    events: asyncio.Queue[Event] = asyncio.Queue()
    stop = asyncio.Event()

    async def pump() -> None:
        async for event in context.services.bus.subscribe(context.workspace_id):
            await events.put(event)
            if stop.is_set():
                return

    pump_task = asyncio.create_task(pump())
    await asyncio.sleep(0)  # let the subscription register before we submit

    run_id = await context.conductor.submit_message(
        session_id=session_id, user_id=context.user_id, text=text
    )

    terminal: Event | None = None
    try:
        while True:
            event = await asyncio.wait_for(events.get(), timeout=900)
            if event.run_id != run_id:
                continue
            if on_event is not None:
                on_event(event)
            if event.type in (EventType.RUN_FINISHED, EventType.RUN_AWAITING_APPROVAL):
                terminal = event
                break
    except (TimeoutError, asyncio.CancelledError):
        terminal = None
    finally:
        stop.set()
        pump_task.cancel()
        await asyncio.gather(pump_task, return_exceptions=True)
        await context.conductor.wait_for_inline_runs()
    return terminal


async def iter_events(context: LocalContext) -> AsyncIterator[Event]:
    """Raw workspace event stream, for the GUI's background listener."""
    async for event in context.services.bus.subscribe(context.workspace_id):
        yield event
