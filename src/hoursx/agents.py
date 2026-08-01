"""Agent runtime: the model ⇄ tools loop that drives every run.

State machine per run::

    queued -> running -> succeeded | failed | cancelled
                 ^  \
                 |   -> awaiting_approval   (tool gate hit; transcript checkpointed)
                 \\______/                   (human decision resumes the loop)

Every transition is persisted and emitted on the event bus; a run can never end
without a recorded outcome.

Transaction discipline: the loop never holds a database transaction across a
model call or tool execution. It works from an immutable snapshot and opens a
short write session per persistence step — so tools (and delegated child runs)
are free to use the database themselves without deadlocking the parent.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update

from hoursx.db.models import (
    AgentProfile,
    ApprovalRequest,
    Message,
    Run,
    RunStep,
    Session,
    utcnow,
)
from hoursx.events import Event, EventType
from hoursx.observability import get_logger, metrics
from hoursx.prompts import ContextBuilder, render_system_prompt
from hoursx.providers.types import ChatMessage, ChatRequest, ChatRole, ChatUsage, ToolCall
from hoursx.services import AppServices
from hoursx.tools.base import ToolContext, ToolInvocation
from hoursx.tools.executor import ApprovalPending

log = get_logger("agents.runtime")

_MAX_DELEGATION_DEPTH = 2


@dataclass(frozen=True)
class ProfileSnap:
    id: str
    title: str
    instructions: str
    model_alias: str
    tool_grants: tuple[str, ...]
    can_delegate: bool
    max_steps: int | None


@dataclass
class RunSnap:
    """Detached snapshot of the rows the loop needs. Mutable fields
    (``step_count``, ``step_seq``) are loop-local and written back explicitly."""

    run_id: str
    workspace_id: str
    session_id: str
    parent_run_id: str | None
    goal: str
    sandbox_dir: str
    profile: ProfileSnap
    step_count: int
    step_seq: int  # monotonic RunStep ordering within the run
    input_tokens: int = 0
    output_tokens: int = 0


class AgentRuntime:
    def __init__(self, services: AppServices) -> None:
        self._services = services
        # Accounting for the step record written immediately after each turn.
        self._last_turn_ms = 0
        self._last_turn_usage = ChatUsage()

    # ------------------------------------------------------------------ public

    async def execute_run(self, run_id: str, *, _depth: int = 0) -> None:
        """Drive a queued run to a terminal state or an approval pause.

        Claims strictly from ``queued``. A run already marked ``running`` belongs
        to another worker, and re-claiming it would duplicate every side effect
        it has performed. Runs orphaned by a crashed worker are returned to
        ``queued`` by :func:`hoursx.recovery.requeue_orphaned_runs`, never by an
        opportunistic re-claim here.
        """
        snap = await self._claim(run_id, from_status=("queued",))
        if snap is None:
            return
        await self._emit(snap, EventType.RUN_STARTED, {"goal": snap.goal})
        transcript = await self._build_transcript(snap)
        await self._loop(snap, transcript, pending=[], depth=_depth)

    async def resume_run(self, run_id: str, *, approval_id: str, approved: bool) -> None:
        """Continue a run parked on an approval decision."""
        async with self._services.db.session() as db:
            run = await db.get(Run, run_id)
            if run is None or run.status != "awaiting_approval" or not run.checkpoint:
                return
            checkpoint = dict(run.checkpoint)
        snap = await self._claim(run_id, from_status=("awaiting_approval",))
        if snap is None:
            return

        transcript = [ChatMessage.model_validate(m) for m in checkpoint["messages"]]
        pending = [ToolCall.model_validate(c) for c in checkpoint["pending_calls"]]
        if not pending:
            return
        gated, rest = pending[0], pending[1:]

        if approved:
            outcome_msg = await self._run_tool(snap, gated, approved=True)
        else:
            outcome_msg = _tool_message(
                gated, "The human operator denied this action. Choose another approach."
            )
            await self._emit(
                snap,
                EventType.RUN_STEP,
                {"tool": gated.name, "ok": False, "summary": "denied by operator"},
            )
        transcript.append(outcome_msg)
        await self._record_step(snap, "tool", {"call": gated.model_dump(), "approved": approved})
        await self._loop(snap, transcript, pending=rest, depth=0)

    # ------------------------------------------------------------------- loop

    async def _loop(
        self,
        snap: RunSnap,
        transcript: list[ChatMessage],
        *,
        pending: list[ToolCall],
        depth: int,
    ) -> None:
        max_steps = snap.profile.max_steps or self._services.settings.max_run_steps
        try:
            # Finish tool calls left over from a resumed model turn first.
            remaining = list(pending)
            while remaining:
                call = remaining.pop(0)
                try:
                    transcript.append(await self._run_tool(snap, call, depth=depth))
                except ApprovalPending as gate:
                    await self._park_for_approval(snap, transcript, [call, *remaining], gate)
                    return
                await self._record_step(snap, "tool", {"call": call.model_dump()})

            while snap.step_count < max_steps:
                # Cancellation is cooperative and checked at step boundaries:
                # interrupting mid-tool could leave a side effect half-applied.
                if await self._cancel_requested(snap):
                    await self._finish(snap, "cancelled", error="cancelled by operator")
                    return
                result = await self._model_turn(snap, transcript)
                snap.step_count += 1
                transcript.append(result.message)
                await self._record_step(
                    snap,
                    "model",
                    {
                        "text": result.message.content[:2000],
                        "tool_calls": [c.model_dump() for c in result.message.tool_calls],
                    },
                )

                if not result.message.tool_calls:
                    await self._finish(snap, "succeeded", answer=result.message.content)
                    return

                calls = list(result.message.tool_calls)
                while calls:
                    call = calls.pop(0)
                    try:
                        transcript.append(await self._run_tool(snap, call, depth=depth))
                    except ApprovalPending as gate:
                        await self._park_for_approval(snap, transcript, [call, *calls], gate)
                        return
                    await self._record_step(snap, "tool", {"call": call.model_dump()})

            await self._finish(
                snap,
                "failed",
                error=f"step limit reached ({max_steps}) before a final answer",
            )
        except Exception as exc:  # noqa: BLE001 — a run must always end recorded
            log.exception("run %s crashed", snap.run_id)
            await self._finish(snap, "failed", error=f"runtime error: {exc}")

    async def _model_turn(self, snap: RunSnap, transcript: list[ChatMessage]):
        """One streamed model call; text deltas are fanned out as events."""
        services = self._services
        request = ChatRequest(
            model="unset",  # router substitutes the resolved model name
            messages=transcript,
            tools=services.registry.descriptors(list(snap.profile.tool_grants)),
        )
        started = time.monotonic()
        result = None
        try:
            stream = services.router.stream(snap.profile.model_alias, request)
            async for delta in stream:
                if delta.kind == "text" and delta.text:
                    await self._emit(snap, EventType.RUN_DELTA, {"text": delta.text})
                elif delta.kind == "done":
                    result = delta.result
        except Exception:
            services.router.note_stream_outcome(snap.profile.model_alias, ok=False)
            raise
        if result is None:
            services.router.note_stream_outcome(snap.profile.model_alias, ok=False)
            raise RuntimeError("model stream ended without a result")
        services.router.note_stream_outcome(snap.profile.model_alias, ok=True)
        snap.input_tokens += result.usage.input_tokens
        snap.output_tokens += result.usage.output_tokens
        self._last_turn_ms = int((time.monotonic() - started) * 1000)
        self._last_turn_usage = result.usage
        metrics.incr("runs.model_turns")
        return result

    async def _run_tool(
        self, snap: RunSnap, call: ToolCall, *, approved: bool = False, depth: int = 0
    ) -> ChatMessage:
        services = self._services
        ctx = ToolContext(
            workspace_id=snap.workspace_id,
            session_id=snap.session_id,
            run_id=snap.run_id,
            sandbox_dir=services.sandbox_root() / snap.sandbox_dir,
            services=services,
            delegate=self._delegate_fn(snap, depth) if snap.profile.can_delegate else None,
        )
        ctx.sandbox_dir.mkdir(parents=True, exist_ok=True)
        invocation = ToolInvocation(call_id=call.id, tool_name=call.name, arguments=call.arguments)
        outcome = await services.executor.execute(
            invocation, ctx, grants=list(snap.profile.tool_grants), approved=approved
        )
        await self._emit(
            snap,
            EventType.RUN_STEP,
            {"tool": call.name, "ok": outcome.ok, "summary": outcome.summary},
        )
        payload = outcome.summary
        if outcome.data:
            payload += "\n" + _compact_json(outcome.data)
        return _tool_message(call, payload)

    # -------------------------------------------------------------- delegation

    def _delegate_fn(self, snap: RunSnap, depth: int):
        async def delegate(agent_handle: str, goal: str) -> str:
            if depth >= _MAX_DELEGATION_DEPTH:
                return "Delegation depth limit reached; solve the remainder yourself."
            async with self._services.db.session() as db:
                profile = (
                    await db.execute(
                        select(AgentProfile).where(
                            AgentProfile.workspace_id == snap.workspace_id,
                            AgentProfile.handle == agent_handle,
                        )
                    )
                ).scalar_one_or_none()
                if profile is None:
                    raise LookupError(agent_handle)
                child = Run(
                    workspace_id=snap.workspace_id,
                    session_id=snap.session_id,
                    agent_profile_id=profile.id,
                    parent_run_id=snap.run_id,
                    goal=goal,
                    status="queued",
                )
                db.add(child)
                await db.flush()
                child_id = child.id
            await self.execute_run(child_id, _depth=depth + 1)
            async with self._services.db.session() as db:
                finished = await db.get(Run, child_id)
                if finished is None or finished.status != "succeeded":
                    return (
                        f"Delegated agent ended with status "
                        f"{finished.status if finished else 'unknown'}: "
                        f"{(finished.error if finished else None) or 'no answer'}"
                    )
                return finished.final_answer or "(no answer text)"

        return delegate

    # ----------------------------------------------------------------- helpers

    async def _claim(self, run_id: str, *, from_status: tuple[str, ...]) -> RunSnap | None:
        """Atomically claim the run and snapshot what the loop needs.

        The status transition is a compare-and-swap: ``UPDATE … WHERE id = ? AND
        status IN (…)``. Exactly one caller can win, so two workers pulling the
        same job — or a resume racing a retry — cannot both execute the run and
        duplicate its side effects. A read-then-write would allow precisely that.
        """
        async with self._services.db.session() as db:
            claimed = await db.execute(
                update(Run)
                .where(Run.id == run_id, Run.status.in_(from_status))
                .values(status="running", checkpoint=None, heartbeat_at=utcnow())
            )
            if claimed.rowcount != 1:
                return None  # another worker won the claim, or the state moved on

            run = await db.get(Run, run_id)
            assert run is not None  # the CAS above proved it exists
            await db.refresh(run)
            session = await db.get(Session, run.session_id)
            profile = await db.get(AgentProfile, run.agent_profile_id)
            if session is None or profile is None:
                run.status = "failed"
                run.error = "session or agent profile no longer exists"
                run.finished_at = utcnow()
                return None
            step_seq = (
                await db.execute(select(func.count(RunStep.id)).where(RunStep.run_id == run_id))
            ).scalar_one()
            return RunSnap(
                run_id=run.id,
                workspace_id=run.workspace_id,
                session_id=run.session_id,
                parent_run_id=run.parent_run_id,
                goal=run.goal,
                sandbox_dir=session.sandbox_dir,
                profile=ProfileSnap(
                    id=profile.id,
                    title=profile.title,
                    instructions=profile.instructions,
                    model_alias=profile.model_alias,
                    tool_grants=tuple(profile.tool_grants or []),
                    can_delegate=profile.can_delegate,
                    max_steps=profile.max_steps,
                ),
                step_count=run.step_count,
                step_seq=step_seq,
            )

    async def _cancel_requested(self, snap: RunSnap) -> bool:
        """Re-read the cancellation flag; the API sets it on another connection."""
        async with self._services.db.session() as db:
            flag = (
                await db.execute(select(Run.cancel_requested).where(Run.id == snap.run_id))
            ).scalar_one_or_none()
        return bool(flag)

    async def _build_transcript(self, snap: RunSnap) -> list[ChatMessage]:
        services = self._services
        async with services.db.session() as db:
            recalled = await services.memory.recall(
                db,
                workspace_id=snap.workspace_id,
                query=snap.goal,
                agent_profile_id=snap.profile.id,
            )
            is_child = snap.parent_run_id is not None
            # Child runs get a clean transcript: their goal is self-contained and
            # parent history would leak unrelated context.
            history = (
                [] if is_child else await services.memory.conversation_history(db, snap.session_id)
            )
        builder = ContextBuilder(
            token_budget=services.settings.context_token_budget,
            system_prompt=render_system_prompt(snap.profile.title, snap.profile.instructions),
            memory_notes=[hit.text for hit in recalled],
            history=history,
        )
        transcript = builder.build()
        if not transcript or transcript[-1].role != ChatRole.USER:
            transcript.append(ChatMessage(role=ChatRole.USER, content=snap.goal))
        return transcript

    async def _park_for_approval(
        self,
        snap: RunSnap,
        transcript: list[ChatMessage],
        pending: list[ToolCall],
        gate: ApprovalPending,
    ) -> None:
        async with self._services.db.session() as db:
            request = ApprovalRequest(
                workspace_id=snap.workspace_id,
                run_id=snap.run_id,
                tool_name=gate.invocation.tool_name,
                arguments=gate.invocation.arguments,
                reason=gate.reason,
            )
            db.add(request)
            await db.flush()
            approval_id = request.id
            await db.execute(
                update(Run)
                .where(Run.id == snap.run_id)
                .values(
                    status="awaiting_approval",
                    step_count=snap.step_count,
                    checkpoint={
                        "messages": [m.model_dump() for m in transcript],
                        "pending_calls": [c.model_dump() for c in pending],
                    },
                )
            )
        metrics.incr("runs.approval_pauses")
        await self._emit(
            snap,
            EventType.RUN_AWAITING_APPROVAL,
            {
                "approval_id": approval_id,
                "tool": gate.invocation.tool_name,
                "arguments": gate.invocation.arguments,
                "reason": gate.reason,
            },
        )

    async def _finish(
        self,
        snap: RunSnap,
        status: str,
        *,
        answer: str | None = None,
        error: str | None = None,
    ) -> None:
        async with self._services.db.session() as db:
            await db.execute(
                update(Run)
                .where(Run.id == snap.run_id)
                .values(
                    status=status,
                    final_answer=answer,
                    error=error,
                    checkpoint=None,
                    step_count=snap.step_count,
                    input_tokens=snap.input_tokens,
                    output_tokens=snap.output_tokens,
                    heartbeat_at=None,
                    finished_at=utcnow(),
                )
            )
            if answer is not None and snap.parent_run_id is None:
                db.add(
                    Message(
                        session_id=snap.session_id,
                        role="assistant",
                        content=answer,
                        run_id=snap.run_id,
                    )
                )
        metrics.incr(f"runs.{status}")
        await self._emit(
            snap,
            EventType.RUN_FINISHED,
            {"status": status, "answer": answer, "error": error},
        )

    async def _record_step(self, snap: RunSnap, kind: str, detail: dict) -> None:
        usage = self._last_turn_usage if kind == "model" else ChatUsage()
        async with self._services.db.session() as db:
            # Each recorded step doubles as a liveness beat, so recovery can
            # tell a slow-but-alive run from an abandoned one.
            await db.execute(update(Run).where(Run.id == snap.run_id).values(heartbeat_at=utcnow()))
            db.add(
                RunStep(
                    run_id=snap.run_id,
                    index=snap.step_seq,
                    kind=kind,
                    detail=detail,
                    duration_ms=self._last_turn_ms if kind == "model" else 0,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                )
            )
        snap.step_seq += 1

    async def _emit(self, snap: RunSnap, type_: EventType, payload: dict) -> None:
        await self._services.bus.publish(
            Event(
                type=type_,
                workspace_id=snap.workspace_id,
                session_id=snap.session_id,
                run_id=snap.run_id,
                payload=payload,
            )
        )


def _tool_message(call: ToolCall, content: str) -> ChatMessage:
    return ChatMessage(role=ChatRole.TOOL, content=content, tool_call_id=call.id)


def _compact_json(data: dict) -> str:
    import json

    text = json.dumps(data, ensure_ascii=False, default=str)
    return text if len(text) <= 8000 else text[:8000] + "…(truncated)"


def new_call_id() -> str:
    return "call_" + uuid.uuid4().hex[:12]
