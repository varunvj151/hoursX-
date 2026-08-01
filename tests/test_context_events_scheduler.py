"""Context budgeting, event bus fanout, cron matching, and plugin SDK."""

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from hoursx.events import Event, EventBus, EventType
from hoursx.prompts import ContextBuilder, estimate_tokens, render_system_prompt
from hoursx.providers.types import ChatMessage, ChatRole
from hoursx.scheduler import cron_matches
from hoursx.sdk.discovery import discover_plugins
from hoursx.sdk.manifest import PluginManifest, PluginPermission, PluginTool
from hoursx.tools.base import FunctionTool, ToolOutcome, ToolSpec

# ------------------------------------------------------------------- context


def test_context_keeps_newest_history_under_budget():
    old = ChatMessage(role=ChatRole.USER, content="old " * 400)
    newer = ChatMessage(role=ChatRole.ASSISTANT, content="newer answer")
    newest = ChatMessage(role=ChatRole.USER, content="newest question")
    builder = ContextBuilder(
        token_budget=estimate_tokens(newer.content + newest.content) + 40,
        system_prompt="sys",
        history=[old, newer, newest],
    )
    messages = builder.build()
    contents = [m.content for m in messages]
    assert contents[0] == "sys"
    assert "newest question" in contents[-1]
    assert all("old" not in c for c in contents)


def test_context_never_drops_the_latest_message():
    huge = ChatMessage(role=ChatRole.USER, content="x" * 100_000)
    builder = ContextBuilder(token_budget=50, history=[huge])
    assert builder.build()[-1].content == huge.content


def test_context_bounds_aux_sections():
    builder = ContextBuilder(
        token_budget=10_000,
        aux_section_budget=30,
        system_prompt="sys",
        memory_notes=["note " + str(i) * 50 for i in range(50)],
    )
    [system] = [m for m in builder.build() if m.role == ChatRole.SYSTEM]
    assert estimate_tokens(system.content) < 200


def test_system_prompt_includes_identity_and_instructions():
    prompt = render_system_prompt("Researcher", "Cite sources.")
    assert "Researcher" in prompt and "Cite sources." in prompt


# -------------------------------------------------------------------- events


async def test_event_bus_delivers_to_workspace_subscribers():
    bus = EventBus()
    received: list[Event] = []

    async def consume():
        async for event in bus.subscribe("w1"):
            received.append(event)
            break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    await bus.publish(Event(type=EventType.RUN_STARTED, workspace_id="w1"))
    await asyncio.wait_for(task, timeout=1)
    assert received[0].type == EventType.RUN_STARTED


async def test_event_bus_isolates_workspaces():
    bus = EventBus()
    got_other = False

    async def consume():
        nonlocal got_other
        async for _ in bus.subscribe("w2"):
            got_other = True

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    await bus.publish(Event(type=EventType.RUN_STARTED, workspace_id="w1"))
    await asyncio.sleep(0.05)
    task.cancel()
    assert not got_other


# ----------------------------------------------------------------------- cron


@pytest.mark.parametrize(
    ("expr", "when", "matches"),
    [
        ("* * * * *", datetime(2026, 7, 31, 12, 30), True),
        ("30 12 * * *", datetime(2026, 7, 31, 12, 30), True),
        ("31 12 * * *", datetime(2026, 7, 31, 12, 30), False),
        ("*/15 * * * *", datetime(2026, 7, 31, 12, 30), True),
        ("*/15 * * * *", datetime(2026, 7, 31, 12, 31), False),
        ("0 9-17 * * *", datetime(2026, 7, 31, 13, 0), True),
        ("0 9-17 * * *", datetime(2026, 7, 31, 20, 0), False),
        # 2026-07-31 is a Friday (cron weekday 5).
        ("0 12 * * 5", datetime(2026, 7, 31, 12, 0), True),
        ("0 12 * * 0", datetime(2026, 7, 31, 12, 0), False),
    ],
)
def test_cron_matching(expr, when, matches):
    assert cron_matches(expr, when) is matches


def test_cron_rejects_wrong_field_count():
    with pytest.raises(ValueError):
        cron_matches("* * *", datetime.now())


# ------------------------------------------------------------------ plugin sdk


class _PArgs(BaseModel):
    pass


def _plugin_tool(name: str = "weather.today") -> FunctionTool:
    async def run(args: _PArgs, ctx) -> ToolOutcome:
        return ToolOutcome.success("sunny")

    return FunctionTool(ToolSpec(name=name, description="", params_model=_PArgs), run)


def test_manifest_validates_and_gates_by_permission():
    manifest = PluginManifest(
        name="weather",
        version="1.0.0",
        summary="Weather tools",
        tools=[PluginTool(tool=_plugin_tool(), needs=[PluginPermission.NETWORK])],
    )
    assert manifest.granted_tools({PluginPermission.NETWORK})
    assert manifest.granted_tools(set()) == []


def test_manifest_rejects_unnamespaced_tools():
    with pytest.raises(ValueError):
        PluginManifest(
            name="weather",
            version="1.0.0",
            summary="x",
            tools=[PluginTool(tool=_plugin_tool("today"))],
        )


def test_local_plugin_discovery_isolates_failures(tmp_path: Path):
    (tmp_path / "good.py").write_text(
        "from hoursx.sdk.manifest import PluginManifest\n"
        "def manifest():\n"
        "    return PluginManifest(name='good-plugin', version='0.1.0', summary='ok')\n"
    )
    (tmp_path / "broken.py").write_text("raise RuntimeError('bad plugin')\n")
    report = discover_plugins(tmp_path)
    assert [m.name for m in report.loaded] == ["good-plugin"]
    assert "local:broken.py" in report.failed
