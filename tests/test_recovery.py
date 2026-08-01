"""Orphaned-run recovery: the liveness counterpart to single-winner claiming."""

from datetime import timedelta

from sqlalchemy import update

from hoursx.db.models import Run, utcnow
from hoursx.recovery import requeue_orphaned_runs


async def _run(services, seeded, *, status: str, heartbeat_age: timedelta | None) -> str:
    async with services.db.session() as db:
        run = Run(
            workspace_id=seeded.workspace_id,
            session_id=seeded.session_id,
            agent_profile_id=seeded.profile_id,
            goal="work",
            status=status,
        )
        db.add(run)
        await db.flush()
        run_id = run.id
        if heartbeat_age is not None:
            await db.execute(
                update(Run).where(Run.id == run_id).values(heartbeat_at=utcnow() - heartbeat_age)
            )
        return run_id


async def test_stale_running_run_is_requeued(services, seeded):
    run_id = await _run(services, seeded, status="running", heartbeat_age=timedelta(hours=2))
    assert await requeue_orphaned_runs(services) == [run_id]
    async with services.db.session() as db:
        run = await db.get(Run, run_id)
    assert run.status == "queued" and run.heartbeat_at is None


async def test_fresh_running_run_is_left_alone(services, seeded):
    """Re-queuing live work is far worse than a late recovery."""
    run_id = await _run(services, seeded, status="running", heartbeat_age=timedelta(seconds=5))
    assert await requeue_orphaned_runs(services) == []
    async with services.db.session() as db:
        assert (await db.get(Run, run_id)).status == "running"


async def test_approval_parked_run_is_never_reclaimed(services, seeded):
    """It waits on a human, not a worker — days of silence are legitimate."""
    run_id = await _run(
        services, seeded, status="awaiting_approval", heartbeat_age=timedelta(days=3)
    )
    assert await requeue_orphaned_runs(services) == []
    async with services.db.session() as db:
        assert (await db.get(Run, run_id)).status == "awaiting_approval"


async def test_terminal_runs_are_ignored(services, seeded):
    for status in ("succeeded", "failed", "cancelled"):
        await _run(services, seeded, status=status, heartbeat_age=timedelta(hours=5))
    assert await requeue_orphaned_runs(services) == []


async def test_run_without_heartbeat_is_not_touched(services, seeded):
    """A queued run has never been claimed; it is not an orphan."""
    await _run(services, seeded, status="queued", heartbeat_age=None)
    assert await requeue_orphaned_runs(services) == []


async def test_threshold_is_configurable(services, seeded):
    run_id = await _run(services, seeded, status="running", heartbeat_age=timedelta(minutes=2))
    assert await requeue_orphaned_runs(services, stale_after=timedelta(hours=1)) == []
    assert await requeue_orphaned_runs(services, stale_after=timedelta(minutes=1)) == [run_id]


async def test_recovery_is_idempotent(services, seeded):
    run_id = await _run(services, seeded, status="running", heartbeat_age=timedelta(hours=2))
    first = await requeue_orphaned_runs(services)
    second = await requeue_orphaned_runs(services)
    assert first == [run_id] and second == []


async def test_recovered_run_can_be_executed_again(services, conductor, seeded, echo):
    from hoursx.providers.types import ChatMessage, ChatResult, ChatRole

    echo._script = [ChatResult(message=ChatMessage(role=ChatRole.ASSISTANT, content="recovered"))]
    run_id = await _run(services, seeded, status="running", heartbeat_age=timedelta(hours=2))
    await requeue_orphaned_runs(services)
    await conductor.runtime.execute_run(run_id)
    async with services.db.session() as db:
        run = await db.get(Run, run_id)
    assert run.status == "succeeded" and run.final_answer == "recovered"


async def test_running_run_receives_heartbeats(services, conductor, seeded, echo):
    from hoursx.providers.types import ChatMessage, ChatResult, ChatRole, ToolCall

    echo._script = [
        ChatResult(
            message=ChatMessage(
                role=ChatRole.ASSISTANT,
                tool_calls=[ToolCall(id="c1", name="fs.list", arguments={"path": "."})],
            ),
            finish_reason="tool_calls",
        ),
        ChatResult(message=ChatMessage(role=ChatRole.ASSISTANT, content="done")),
    ]
    run_id = await conductor.submit_message(
        session_id=seeded.session_id, user_id=seeded.user_id, text="beat"
    )
    await conductor.wait_for_inline_runs()
    async with services.db.session() as db:
        run = await db.get(Run, run_id)
    # Cleared on completion so a finished run is never mistaken for an orphan.
    assert run.status == "succeeded" and run.heartbeat_at is None
