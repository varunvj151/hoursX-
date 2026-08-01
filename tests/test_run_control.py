"""Run lifecycle control: atomic claiming, cancellation, idempotency, usage."""

import asyncio

from sqlalchemy import select, update

from hoursx.db.models import ApprovalRequest, Run, RunStep, utcnow
from hoursx.orchestration import Conductor
from hoursx.providers.types import ChatMessage, ChatResult, ChatRole, ChatUsage, ToolCall
from hoursx.tools.executor import ToolExecutor


def _tool_turn(*calls: ToolCall) -> ChatResult:
    return ChatResult(
        message=ChatMessage(role=ChatRole.ASSISTANT, tool_calls=list(calls)),
        finish_reason="tool_calls",
    )


def _final_turn(text: str, usage: ChatUsage | None = None) -> ChatResult:
    return ChatResult(
        message=ChatMessage(role=ChatRole.ASSISTANT, content=text),
        usage=usage or ChatUsage(),
    )


async def _new_run(services, seeded, goal: str = "do the thing", status: str = "queued") -> str:
    async with services.db.session() as db:
        run = Run(
            workspace_id=seeded.workspace_id,
            session_id=seeded.session_id,
            agent_profile_id=seeded.profile_id,
            goal=goal,
            status=status,
        )
        db.add(run)
        await db.flush()
        return run.id


# --------------------------------------------------------------- atomic claim


async def test_only_one_worker_can_claim_a_run(services, runtime, seeded, echo):
    """The core multi-worker guarantee: a duplicated claim would duplicate every
    side effect the run performs."""
    echo._script = [_final_turn("done") for _ in range(4)]
    run_id = await _new_run(services, seeded)

    claims = await asyncio.gather(
        *[runtime._claim(run_id, from_status=("queued",)) for _ in range(4)]
    )
    assert sum(1 for claim in claims if claim is not None) == 1


async def test_claim_refuses_a_run_in_the_wrong_state(services, runtime, seeded):
    run_id = await _new_run(services, seeded, status="succeeded")
    assert await runtime._claim(run_id, from_status=("queued",)) is None


async def test_claim_returns_none_for_unknown_run(services, runtime):
    assert await runtime._claim("does-not-exist", from_status=("queued",)) is None


async def test_claim_marks_the_run_running(services, runtime, seeded):
    run_id = await _new_run(services, seeded)
    snap = await runtime._claim(run_id, from_status=("queued",))
    assert snap is not None
    async with services.db.session() as db:
        run = await db.get(Run, run_id)
    assert run.status == "running"


async def test_claim_fails_run_whose_session_vanished(services, runtime, seeded):
    run_id = await _new_run(services, seeded)
    async with services.db.session() as db:
        await db.execute(update(Run).where(Run.id == run_id).values(session_id="ghost-session"))
    assert await runtime._claim(run_id, from_status=("queued",)) is None
    async with services.db.session() as db:
        run = await db.get(Run, run_id)
    # It must be recorded as failed, not left silently stuck in 'running'.
    assert run.status == "failed" and "no longer exists" in (run.error or "")


async def test_concurrent_execute_runs_produce_one_execution(services, conductor, seeded, echo):
    echo._script = [_final_turn("only once") for _ in range(5)]
    run_id = await _new_run(services, seeded)
    await asyncio.gather(*[conductor.runtime.execute_run(run_id) for _ in range(3)])
    async with services.db.session() as db:
        steps = (await db.execute(select(RunStep).where(RunStep.run_id == run_id))).scalars().all()
    assert len([s for s in steps if s.kind == "model"]) == 1


# -------------------------------------------------------------- cancellation


async def test_cancel_queued_run_settles_immediately(services, conductor, seeded):
    run_id = await _new_run(services, seeded)
    accepted = await conductor.request_cancel(run_id=run_id, workspace_id=seeded.workspace_id)
    assert accepted
    async with services.db.session() as db:
        run = await db.get(Run, run_id)
    assert run.status == "cancelled"
    assert run.finished_at is not None


async def test_cancelled_run_is_not_executed(services, conductor, seeded, echo):
    echo._script = [_final_turn("should never run")]
    run_id = await _new_run(services, seeded)
    await conductor.request_cancel(run_id=run_id, workspace_id=seeded.workspace_id)
    await conductor.runtime.execute_run(run_id)
    async with services.db.session() as db:
        run = await db.get(Run, run_id)
    assert run.status == "cancelled" and run.final_answer is None


async def test_cancel_is_observed_between_steps(services, conductor, seeded, echo):
    """A run mid-flight stops at the next boundary rather than being killed."""
    echo._script = [
        _tool_turn(ToolCall(id="c1", name="fs.list", arguments={"path": "."})),
        _final_turn("kept going"),
    ]
    run_id = await _new_run(services, seeded)
    async with services.db.session() as db:
        await db.execute(update(Run).where(Run.id == run_id).values(cancel_requested=True))
    await conductor.runtime.execute_run(run_id)
    async with services.db.session() as db:
        run = await db.get(Run, run_id)
    assert run.status == "cancelled"
    assert run.error == "cancelled by operator"


async def test_cancel_terminal_run_is_a_noop(services, conductor, seeded):
    run_id = await _new_run(services, seeded, status="succeeded")
    assert not await conductor.request_cancel(run_id=run_id, workspace_id=seeded.workspace_id)


async def test_cancel_releases_a_run_parked_on_approval(services, seeded, echo):
    """An approval-parked run has no loop polling the flag; cancelling must not
    leave it waiting forever."""
    services.executor = ToolExecutor(services.registry, force_approval=frozenset({"shell.run"}))
    conductor = Conductor(services)
    echo._script = [
        _tool_turn(ToolCall(id="c1", name="shell.run", arguments={"command": "echo hi"})),
        _final_turn("unreachable"),
    ]
    run_id = await conductor.submit_message(
        session_id=seeded.session_id, user_id=seeded.user_id, text="risky"
    )
    await conductor.wait_for_inline_runs()
    async with services.db.session() as db:
        assert (await db.get(Run, run_id)).status == "awaiting_approval"

    await conductor.request_cancel(run_id=run_id, workspace_id=seeded.workspace_id)
    async with services.db.session() as db:
        run = await db.get(Run, run_id)
    assert run.status == "cancelled" and run.checkpoint is None


async def test_cancel_emits_a_terminal_event(services, conductor, seeded):
    from hoursx.events import EventType

    seen: list[str] = []

    async def consume():
        async for event in services.bus.subscribe(seeded.workspace_id):
            seen.append(event.type.value)
            if event.type == EventType.RUN_FINISHED:
                return

    run_id = await _new_run(services, seeded)
    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    await conductor.request_cancel(run_id=run_id, workspace_id=seeded.workspace_id)
    await asyncio.wait_for(task, timeout=2)
    assert "run.finished" in seen


async def test_cancel_unknown_run_raises_not_found(services, conductor, seeded):
    from hoursx.errors import NotFoundError

    try:
        await conductor.request_cancel(run_id="nope", workspace_id=seeded.workspace_id)
    except NotFoundError:
        return
    raise AssertionError("expected NotFoundError")


async def test_cancel_across_workspaces_is_not_found(services, conductor, seeded):
    from hoursx.errors import NotFoundError

    run_id = await _new_run(services, seeded)
    try:
        await conductor.request_cancel(run_id=run_id, workspace_id="intruder")
    except NotFoundError:
        return
    raise AssertionError("expected NotFoundError")


# --------------------------------------------------------------- idempotency


async def test_same_idempotency_key_returns_the_original_run(services, conductor, seeded, echo):
    echo._script = [_final_turn("first"), _final_turn("second")]
    first = await conductor.submit_message(
        session_id=seeded.session_id, user_id=seeded.user_id, text="hi", idempotency_key="k-1"
    )
    second = await conductor.submit_message(
        session_id=seeded.session_id, user_id=seeded.user_id, text="hi", idempotency_key="k-1"
    )
    await conductor.wait_for_inline_runs()
    assert first == second


async def test_different_keys_create_distinct_runs(services, conductor, seeded, echo):
    echo._script = [_final_turn("a"), _final_turn("b")]
    first = await conductor.submit_message(
        session_id=seeded.session_id, user_id=seeded.user_id, text="hi", idempotency_key="k-1"
    )
    second = await conductor.submit_message(
        session_id=seeded.session_id, user_id=seeded.user_id, text="hi", idempotency_key="k-2"
    )
    await conductor.wait_for_inline_runs()
    assert first != second


async def test_no_key_never_deduplicates(services, conductor, seeded, echo):
    echo._script = [_final_turn("a"), _final_turn("b")]
    first = await conductor.submit_message(
        session_id=seeded.session_id, user_id=seeded.user_id, text="hi"
    )
    second = await conductor.submit_message(
        session_id=seeded.session_id, user_id=seeded.user_id, text="hi"
    )
    await conductor.wait_for_inline_runs()
    assert first != second


async def test_idempotency_does_not_duplicate_the_user_message(services, conductor, seeded, echo):
    from hoursx.db.models import Message

    echo._script = [_final_turn("once")]
    for _ in range(3):
        await conductor.submit_message(
            session_id=seeded.session_id,
            user_id=seeded.user_id,
            text="only once please",
            idempotency_key="dedupe",
        )
    await conductor.wait_for_inline_runs()
    async with services.db.session() as db:
        messages = (
            (
                await db.execute(
                    select(Message).where(
                        Message.session_id == seeded.session_id, Message.role == "user"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(messages) == 1


# ---------------------------------------------------------------- accounting


async def test_run_accumulates_token_usage(services, conductor, seeded, echo):
    echo._script = [_final_turn("counted", ChatUsage(input_tokens=120, output_tokens=45))]
    run_id = await conductor.submit_message(
        session_id=seeded.session_id, user_id=seeded.user_id, text="count me"
    )
    await conductor.wait_for_inline_runs()
    async with services.db.session() as db:
        run = await db.get(Run, run_id)
    assert run.input_tokens == 120 and run.output_tokens == 45


async def test_usage_sums_across_multiple_turns(services, conductor, seeded, echo):
    echo._script = [
        ChatResult(
            message=ChatMessage(
                role=ChatRole.ASSISTANT,
                tool_calls=[ToolCall(id="c1", name="fs.list", arguments={"path": "."})],
            ),
            usage=ChatUsage(input_tokens=100, output_tokens=10),
            finish_reason="tool_calls",
        ),
        _final_turn("done", ChatUsage(input_tokens=150, output_tokens=20)),
    ]
    run_id = await conductor.submit_message(
        session_id=seeded.session_id, user_id=seeded.user_id, text="multi"
    )
    await conductor.wait_for_inline_runs()
    async with services.db.session() as db:
        run = await db.get(Run, run_id)
    assert run.input_tokens == 250 and run.output_tokens == 30


async def test_model_steps_carry_timing_and_usage(services, conductor, seeded, echo):
    echo._script = [_final_turn("timed", ChatUsage(input_tokens=7, output_tokens=3))]
    run_id = await conductor.submit_message(
        session_id=seeded.session_id, user_id=seeded.user_id, text="time me"
    )
    await conductor.wait_for_inline_runs()
    async with services.db.session() as db:
        step = (
            (
                await db.execute(
                    select(RunStep).where(RunStep.run_id == run_id, RunStep.kind == "model")
                )
            )
            .scalars()
            .first()
        )
    assert step.input_tokens == 7 and step.output_tokens == 3
    assert step.duration_ms >= 0


async def test_quota_blocks_submission_when_saturated(services, conductor, seeded, echo):
    from hoursx.errors import QuotaExceededError

    services.settings.max_concurrent_runs_per_workspace = 1
    async with services.db.session() as db:
        db.add(
            Run(
                workspace_id=seeded.workspace_id,
                session_id=seeded.session_id,
                agent_profile_id=seeded.profile_id,
                goal="occupying the slot",
                status="running",
            )
        )
    try:
        await conductor.submit_message(
            session_id=seeded.session_id, user_id=seeded.user_id, text="blocked"
        )
    except QuotaExceededError as exc:
        assert exc.context["limit"] == "max_concurrent_runs"
        return
    raise AssertionError("expected QuotaExceededError")


async def test_approval_request_survives_cancel_as_history(services, seeded, echo):
    """Cancelling settles the run but must not erase the approval record."""
    services.executor = ToolExecutor(services.registry, force_approval=frozenset({"shell.run"}))
    conductor = Conductor(services)
    echo._script = [
        _tool_turn(ToolCall(id="c1", name="shell.run", arguments={"command": "ls"})),
        _final_turn("x"),
    ]
    run_id = await conductor.submit_message(
        session_id=seeded.session_id, user_id=seeded.user_id, text="gate me"
    )
    await conductor.wait_for_inline_runs()
    await conductor.request_cancel(run_id=run_id, workspace_id=seeded.workspace_id)
    async with services.db.session() as db:
        approvals = (
            (await db.execute(select(ApprovalRequest).where(ApprovalRequest.run_id == run_id)))
            .scalars()
            .all()
        )
    assert len(approvals) == 1


async def test_finished_run_records_completion_timestamp(services, conductor, seeded, echo):
    echo._script = [_final_turn("done")]
    before = utcnow()
    run_id = await conductor.submit_message(
        session_id=seeded.session_id, user_id=seeded.user_id, text="stamp"
    )
    await conductor.wait_for_inline_runs()
    async with services.db.session() as db:
        run = await db.get(Run, run_id)
    assert run.finished_at is not None and run.finished_at >= before
