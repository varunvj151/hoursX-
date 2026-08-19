"""CLI parsing, rendering, the embedded engine, and the GUI bridge."""

import argparse

import pytest

from hoursx.cli import render
from hoursx.cli.engine import ensure_agent, ensure_session, open_local, stream_run
from hoursx.cli.main import build_parser
from hoursx.events import Event, EventType
from hoursx.gui.bridge import UiMessage, describe_event
from hoursx.providers.types import ChatMessage, ChatResult, ChatRole, ToolCall

# ------------------------------------------------------------------- parsing


def test_parser_exposes_every_command_group():
    parser = build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert {
        "serve",
        "worker",
        "db-init",
        "create-user",
        "agent",
        "run",
        "approvals",
        "knowledge",
        "system",
        "chat",
        "sessions",
        "doctor",
        "gui",
    } <= set(action.choices)


def test_agent_run_parses_goal_and_options():
    args = build_parser().parse_args(["agent", "run", "fix the disk", "--agent", "sre", "--quiet"])
    assert args.goal == "fix the disk"
    assert args.agent == "sre" and args.quiet is True


def test_agent_run_defaults_to_the_operator_agent():
    assert build_parser().parse_args(["agent", "run", "hello"]).agent == "operator"


def test_system_probe_accepts_verbose_flag():
    assert build_parser().parse_args(["system", "probe", "-v"]).verbose is True


def test_system_processes_rejects_an_unknown_sort_key():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["system", "processes", "--sort", "banana"])


def test_run_subcommands_take_an_id():
    for command in ("show", "cancel"):
        args = build_parser().parse_args(["run", command, "abc123"])
        assert args.run_id == "abc123"


def test_approvals_decisions_parse():
    assert build_parser().parse_args(["approvals", "approve", "a1"]).approval_id == "a1"
    assert build_parser().parse_args(["approvals", "deny", "a1"]).approval_id == "a1"


def test_server_commands_are_marked_synchronous():
    """They block on uvicorn/arq and must not be wrapped in asyncio.run."""
    for command in ("serve", "worker", "db-init"):
        assert build_parser().parse_args([command]).needs_async is False


def test_agent_commands_are_asynchronous_by_default():
    args = build_parser().parse_args(["agent", "list"])
    assert getattr(args, "needs_async", True) is True


def test_missing_subcommand_exits():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


# ----------------------------------------------------------------- rendering


def test_table_renders_headers_and_rows():
    output = render.table([{"id": "a1", "status": "succeeded"}], ["id", "status"])
    assert "ID" in output and "STATUS" in output and "a1" in output


def test_table_reports_emptiness_rather_than_a_blank():
    assert "none" in render.table([], ["id"]).lower()


def test_table_fits_the_requested_width():
    rows = [{"id": "x" * 10, "goal": "y" * 300}]
    for line in render.table(rows, ["id", "goal"], max_width=60).splitlines():
        assert len(line) <= 62  # allows for the inter-column gap


def test_truncate_marks_elision():
    assert render.truncate("abcdefghij", 5).endswith("…")
    assert render.truncate("abc", 10) == "abc"


def test_truncate_flattens_newlines():
    assert "\n" not in render.truncate("a\nb\nc", 20)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "0B"), (1023, "1023B"), (1024, "1.0KiB"), (1024**2, "1.0MiB"), (1024**3, "1.0GiB")],
)
def test_bytes_human_scales(value, expected):
    assert render.bytes_human(value) == expected


def test_bytes_human_handles_missing_values():
    assert render.bytes_human(None) == "?"


def test_bar_is_clamped_to_its_width():
    for fraction in (-1.0, 0.0, 0.5, 1.0, 2.0):
        assert len(render.bar(fraction, 10).replace("\033", "")) >= 10


def test_colour_is_suppressed_when_not_a_tty():
    class NotATty:
        def isatty(self) -> bool:
            return False

    assert render.paint("x", "red", stream=NotATty()) == "x"


def test_no_color_env_disables_colour(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert not render.colour_enabled()


def test_key_values_aligns_and_handles_lists():
    output = render.key_values({"a": 1, "long_key": ["x", "y"]})
    assert "long_key" in output and "x, y" in output


def test_key_values_reports_emptiness():
    assert "empty" in render.key_values({}).lower()


# -------------------------------------------------------------------- engine


async def test_local_engine_provisions_its_workspace(settings):
    context = await open_local(settings)
    try:
        assert context.user_id and context.workspace_id
    finally:
        await context.services.db.dispose()


async def test_local_engine_is_idempotent_across_opens(settings):
    first = await open_local(settings)
    first_ids = (first.user_id, first.workspace_id)
    await first.services.db.dispose()

    second = await open_local(settings)
    try:
        assert (second.user_id, second.workspace_id) == first_ids
    finally:
        await second.services.db.dispose()


async def test_ensure_agent_reuses_an_existing_handle(settings):
    context = await open_local(settings)
    try:
        first = await ensure_agent(context, "operator")
        second = await ensure_agent(context, "operator")
        assert first == second
    finally:
        await context.services.db.dispose()


async def test_ensure_agent_grants_system_tools_by_default(settings):
    from sqlalchemy import select

    from hoursx.db.models import AgentProfile

    context = await open_local(settings)
    try:
        await ensure_agent(context, "operator")
        async with context.services.db.session() as db:
            profile = (
                await db.execute(select(AgentProfile).where(AgentProfile.handle == "operator"))
            ).scalar_one()
        assert "system.*" in profile.tool_grants
    finally:
        await context.services.db.dispose()


async def test_stream_run_reaches_a_terminal_event(settings, echo):
    from hoursx.providers.router import ModelRouter, _HashEmbedProvider
    from hoursx.services import build_services

    echo._script = [ChatResult(message=ChatMessage(role=ChatRole.ASSISTANT, content="done"))]
    router = ModelRouter(
        {"echo": echo, "hash": _HashEmbedProvider()}, aliases=settings.model_aliases
    )
    services = build_services(settings, router=router)
    await services.db.create_all()

    from hoursx.cli.engine import LocalContext
    from hoursx.orchestration import Conductor

    base = await open_local(settings)
    context = LocalContext(
        services=services,
        conductor=Conductor(services),
        user_id=base.user_id,
        workspace_id=base.workspace_id,
    )
    await base.services.db.dispose()
    try:
        agent_id = await ensure_agent(context, "operator")
        session_id = await ensure_session(context, agent_id, "test")
        seen: list[Event] = []
        terminal = await stream_run(
            context, session_id=session_id, text="hello", on_event=seen.append
        )
        assert terminal is not None
        assert terminal.type is EventType.RUN_FINISHED
        assert terminal.payload["status"] == "succeeded"
        assert any(event.type is EventType.RUN_DELTA for event in seen)
    finally:
        await services.db.dispose()


# ---------------------------------------------------------------- gui bridge


@pytest.mark.parametrize(
    ("event_type", "payload", "marker"),
    [
        (EventType.RUN_STARTED, {"goal": "do it"}, "▶"),
        (EventType.RUN_STEP, {"tool": "fs.read", "ok": True}, "✓"),
        (EventType.RUN_STEP, {"tool": "fs.read", "ok": False}, "✗"),
        (EventType.RUN_AWAITING_APPROVAL, {"tool": "system.signal"}, "⏸"),
        (EventType.RUN_FINISHED, {"status": "succeeded"}, "■"),
        (EventType.APPROVAL_DECIDED, {"approved": True}, "●"),
        (EventType.DOCUMENT_INGESTED, {"chunks": 3}, "◆"),
    ],
)
def test_event_descriptions_are_distinct_and_marked(event_type, payload, marker):
    line = describe_event(Event(type=event_type, workspace_id="w", payload=payload))
    assert marker in line


def test_event_description_includes_the_tool_name():
    line = describe_event(
        Event(
            type=EventType.RUN_STEP,
            workspace_id="w",
            payload={"tool": "system.sysctl_set", "ok": True, "summary": "changed"},
        )
    )
    assert "system.sysctl_set" in line


def test_ui_message_defaults_are_inert():
    message = UiMessage("ready")
    assert message.payload is None and message.token == ""


def test_bridge_drain_is_bounded():
    """A burst of deltas must not monopolise the UI thread."""
    from hoursx.gui.bridge import EngineBridge

    bridge = EngineBridge()
    for index in range(500):
        bridge.post(UiMessage("event", index))
    assert len(bridge.drain(limit=100)) == 100
    assert len(bridge.drain(limit=1000)) == 400


def test_bridge_reports_when_engine_is_not_ready():
    from hoursx.gui.bridge import EngineBridge

    bridge = EngineBridge()
    bridge.submit(lambda _context: None, token="t")  # type: ignore[arg-type]
    messages = bridge.drain()
    assert messages and messages[0].kind == "error"
    assert messages[0].token == "t"


def test_bridge_stop_before_start_is_safe():
    from hoursx.gui.bridge import EngineBridge

    EngineBridge().stop(timeout=0.1)  # must not raise


def test_tool_call_round_trips_through_a_result():
    """Guards the shape the CLI relies on when rendering step traces."""
    result = ChatResult(
        message=ChatMessage(
            role=ChatRole.ASSISTANT,
            tool_calls=[ToolCall(id="c1", name="system.kernel", arguments={})],
        ),
        finish_reason="tool_calls",
    )
    assert result.message.tool_calls[0].name == "system.kernel"
