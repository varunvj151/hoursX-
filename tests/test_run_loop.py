"""The agent run loop end to end against the scripted Echo provider:
tool dispatch, approval pause/resume, delegation, and failure recording."""

from sqlalchemy import select

from hoursx.db.models import AgentProfile, Message, Run, RunStep
from hoursx.orchestration import Conductor
from hoursx.providers.types import ChatMessage, ChatResult, ChatRole, ToolCall


def _tool_turn(*calls: ToolCall) -> ChatResult:
    return ChatResult(
        message=ChatMessage(role=ChatRole.ASSISTANT, tool_calls=list(calls)),
        finish_reason="tool_calls",
    )


def _final_turn(text: str) -> ChatResult:
    return ChatResult(message=ChatMessage(role=ChatRole.ASSISTANT, content=text))


async def _submit_and_wait(services, conductor, seeded, text: str) -> Run:
    run_id = await conductor.submit_message(
        session_id=seeded.session_id, user_id=seeded.user_id, text=text
    )
    await conductor.wait_for_inline_runs()
    async with services.db.session() as db:
        run = await db.get(Run, run_id)
        assert run is not None
        return run


async def test_plain_answer_persists_message_and_finishes(services, conductor, seeded, echo):
    run = await _submit_and_wait(services, conductor, seeded, "hello agent")
    assert run.status == "succeeded"
    assert run.final_answer == "echo: hello agent"
    async with services.db.session() as db:
        messages = (
            (await db.execute(select(Message).where(Message.session_id == seeded.session_id)))
            .scalars()
            .all()
        )
    roles = [m.role for m in messages]
    assert roles == ["user", "assistant"]


async def test_tool_call_roundtrip_writes_file(services, conductor, seeded, echo):
    echo._script = [
        _tool_turn(
            ToolCall(
                id="c1",
                name="fs.write",
                arguments={"path": "out.txt", "content": "from the agent"},
            )
        ),
        _final_turn("wrote the file"),
    ]
    run = await _submit_and_wait(services, conductor, seeded, "write a file")
    assert run.status == "succeeded" and run.final_answer == "wrote the file"
    sandbox = services.sandbox_root() / "session-test"
    assert (sandbox / "out.txt").read_text() == "from the agent"
    async with services.db.session() as db:
        kinds = [
            step.kind
            for step in (
                await db.execute(
                    select(RunStep).where(RunStep.run_id == run.id).order_by(RunStep.index)
                )
            )
            .scalars()
            .all()
        ]
    assert kinds == ["model", "tool", "model"]


async def test_tool_failure_feeds_guidance_not_crash(services, conductor, seeded, echo):
    echo._script = [
        _tool_turn(ToolCall(id="c1", name="fs.read", arguments={"path": "missing.txt"})),
        _final_turn("file was missing"),
    ]
    run = await _submit_and_wait(services, conductor, seeded, "read a file")
    assert run.status == "succeeded"  # the failed tool is guidance, not a crash


async def test_step_limit_records_failure(services, conductor, seeded, echo):
    echo._script = [
        _tool_turn(ToolCall(id=f"c{i}", name="fs.list", arguments={"path": "."})) for i in range(5)
    ]
    async with services.db.session() as db:
        profile = await db.get(AgentProfile, seeded.profile_id)
        profile.max_steps = 3
        await db.flush()
    run = await _submit_and_wait(services, conductor, seeded, "loop forever")
    assert run.status == "failed" and "step limit" in (run.error or "")


async def test_ungranted_tool_is_reported_to_model(services, conductor, seeded, echo):
    echo._script = [
        _tool_turn(ToolCall(id="c1", name="shell.run", arguments={"command": "id"})),
        _final_turn("ok"),
    ]
    async with services.db.session() as db:
        profile = await db.get(AgentProfile, seeded.profile_id)
        profile.tool_grants = ["fs.*"]  # shell revoked
        await db.flush()
    run = await _submit_and_wait(services, conductor, seeded, "try shell")
    assert run.status == "succeeded"
    async with services.db.session() as db:
        steps = (await db.execute(select(RunStep).where(RunStep.run_id == run.id))).scalars().all()
    assert any(step.kind == "tool" for step in steps)


async def test_approval_pause_and_approved_resume(services, seeded, echo):
    from hoursx.db.models import ApprovalRequest
    from hoursx.tools.executor import ToolExecutor

    # Operator policy: shell.run needs a human.
    services.executor = ToolExecutor(services.registry, force_approval=frozenset({"shell.run"}))
    conductor = Conductor(services)
    echo._script = [
        _tool_turn(ToolCall(id="c1", name="shell.run", arguments={"command": "echo approved-run"})),
        _final_turn("command done"),
    ]
    run_id = await conductor.submit_message(
        session_id=seeded.session_id, user_id=seeded.user_id, text="run something risky"
    )
    await conductor.wait_for_inline_runs()

    async with services.db.session() as db:
        run = await db.get(Run, run_id)
        assert run.status == "awaiting_approval"
        assert run.checkpoint is not None
        approval = (
            (await db.execute(select(ApprovalRequest).where(ApprovalRequest.run_id == run_id)))
            .scalars()
            .one()
        )
        assert approval.tool_name == "shell.run" and approval.status == "pending"

    await conductor.decide_approval(
        approval_id=approval.id, decided_by=seeded.user_id, approved=True
    )
    await conductor.wait_for_inline_runs()
    async with services.db.session() as db:
        run = await db.get(Run, run_id)
        assert run.status == "succeeded" and run.final_answer == "command done"
        assert run.checkpoint is None


async def test_denied_approval_lets_agent_continue(services, seeded, echo):
    from hoursx.db.models import ApprovalRequest
    from hoursx.tools.executor import ToolExecutor

    services.executor = ToolExecutor(services.registry, force_approval=frozenset({"shell.run"}))
    conductor = Conductor(services)
    echo._script = [
        _tool_turn(ToolCall(id="c1", name="shell.run", arguments={"command": "echo hi"})),
        _final_turn("respecting the denial"),
    ]
    run_id = await conductor.submit_message(
        session_id=seeded.session_id, user_id=seeded.user_id, text="risky"
    )
    await conductor.wait_for_inline_runs()
    async with services.db.session() as db:
        approval = (
            (await db.execute(select(ApprovalRequest).where(ApprovalRequest.run_id == run_id)))
            .scalars()
            .one()
        )
    await conductor.decide_approval(
        approval_id=approval.id, decided_by=seeded.user_id, approved=False
    )
    await conductor.wait_for_inline_runs()
    async with services.db.session() as db:
        run = await db.get(Run, run_id)
    assert run.status == "succeeded" and run.final_answer == "respecting the denial"


async def test_delegation_runs_child_and_returns_answer(services, conductor, seeded, echo):
    async with services.db.session() as db:
        parent = await db.get(AgentProfile, seeded.profile_id)
        parent.can_delegate = True
        db.add(
            AgentProfile(
                workspace_id=seeded.workspace_id,
                handle="researcher",
                title="Researcher",
                model_alias="deep",
                tool_grants=["fs.*"],
            )
        )
        await db.flush()
    echo._script = [
        # Parent asks for delegation; child then answers; parent concludes.
        _tool_turn(
            ToolCall(
                id="c1",
                name="agent.delegate",
                arguments={"agent": "researcher", "goal": "research the topic deeply"},
            )
        ),
        _final_turn("research result: 42"),
        _final_turn("the researcher found: 42"),
    ]
    run = await _submit_and_wait(services, conductor, seeded, "delegate this")
    assert run.status == "succeeded"
    assert run.final_answer == "the researcher found: 42"
    async with services.db.session() as db:
        child = (await db.execute(select(Run).where(Run.parent_run_id == run.id))).scalars().one()
    assert child.status == "succeeded" and child.final_answer == "research result: 42"


async def test_events_emitted_over_bus(services, conductor, seeded, echo):
    import asyncio

    from hoursx.events import EventType

    seen: list[str] = []

    async def consume():
        async for event in services.bus.subscribe(seeded.workspace_id):
            seen.append(event.type.value)
            if event.type == EventType.RUN_FINISHED:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    await _submit_and_wait(services, conductor, seeded, "emit events")
    await asyncio.wait_for(task, timeout=2)
    assert "run.started" in seen and "run.delta" in seen and "run.finished" in seen
