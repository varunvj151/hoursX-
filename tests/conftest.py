"""Shared test fixtures.

Every test gets an isolated service graph: file-backed SQLite in a temp dir, an
in-process event bus, a scriptable Echo model, and a sandbox root inside the
temp dir. Nothing touches the network or global state.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from hoursx.agents import AgentRuntime
from hoursx.api import create_app
from hoursx.config import HoursXSettings
from hoursx.db.models import AgentProfile, Session, User, Workspace, WorkspaceMember
from hoursx.events import EventBus
from hoursx.orchestration import Conductor
from hoursx.providers.echo import EchoProvider
from hoursx.providers.router import ModelRouter, _HashEmbedProvider
from hoursx.services import AppServices, build_services


@pytest.fixture
def settings(tmp_path) -> HoursXSettings:
    return HoursXSettings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.sqlite3",
        workspace_root=str(tmp_path / "workspaces"),
        plugin_dir=str(tmp_path / "plugins"),
        jwt_secret="test-secret",
        task_backend="inline",
        # Inert until a channel is registered; named here so channel tests can
        # route without rebuilding the whole service graph.
        channel_workspace_slug="test-ws",
        channel_agent_handle="assistant",
        model_aliases={
            "fast": "echo/any",
            "deep": "echo/any",
            "embed": "hash/hash-embed-256",
        },
    )


@pytest.fixture
def echo() -> EchoProvider:
    return EchoProvider()


@pytest.fixture
async def services(settings, echo) -> AsyncIterator[AppServices]:
    router = ModelRouter(
        {"echo": echo, "hash": _HashEmbedProvider()},
        aliases=settings.model_aliases,
    )
    graph = build_services(settings, router=router, bus=EventBus())
    await graph.db.create_all()
    yield graph
    await graph.db.dispose()


@pytest.fixture
def runtime(services) -> AgentRuntime:
    return AgentRuntime(services)


@pytest.fixture
def conductor(services) -> Conductor:
    return Conductor(services)


class Seeded:
    """Ids of a seeded workspace/user/agent/session graph."""

    def __init__(self, **ids: str) -> None:
        self.__dict__.update(ids)

    workspace_id: str
    user_id: str
    profile_id: str
    session_id: str


@pytest.fixture
async def seeded(services) -> Seeded:
    """A workspace with one user (owner), one agent profile, one session."""
    async with services.db.session() as db:
        user = User(email="op@hoursx.example.com", display_name="Op", password_hash="x")
        db.add(user)
        await db.flush()
        workspace = Workspace(name="Test WS", slug="test-ws")
        db.add(workspace)
        await db.flush()
        db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner"))
        profile = AgentProfile(
            workspace_id=workspace.id,
            handle="assistant",
            title="Assistant",
            instructions="Be helpful.",
            model_alias="deep",
            tool_grants=["fs.*", "memory.*", "knowledge.*", "shell.run", "agent.delegate"],
        )
        db.add(profile)
        await db.flush()
        session = Session(
            workspace_id=workspace.id,
            user_id=user.id,
            agent_profile_id=profile.id,
            sandbox_dir="session-test",
        )
        db.add(session)
        await db.flush()
        return Seeded(
            workspace_id=workspace.id,
            user_id=user.id,
            profile_id=profile.id,
            session_id=session.id,
        )


@pytest.fixture
async def api_client(services) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client bound to a fully-wired app instance (lifespan included)."""
    app = create_app(services)
    # Lifespan is run explicitly: ASGITransport does not trigger it.
    async with (
        httpx.ASGITransport(app=app) as transport,
        _lifespan(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        yield client


def _lifespan(app):
    import contextlib

    @contextlib.asynccontextmanager
    async def run():
        async with app.router.lifespan_context(app):
            yield

    return run()
