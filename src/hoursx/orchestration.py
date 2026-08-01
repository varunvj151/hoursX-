"""Conductor: the orchestrator that turns user intent into runs.

The conductor owns run creation and dispatch. Execution happens either inline
(background task in this process — dev default) or on the arq queue (production).
It also owns approval decisions, closing the loop the runtime opened.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from hoursx.agents import AgentRuntime
from hoursx.db.models import ApprovalRequest, Message, Run, Session, utcnow
from hoursx.errors import NotFoundError
from hoursx.events import Event, EventType
from hoursx.observability import get_logger
from hoursx.planning import Planner
from hoursx.quotas import QuotaPolicy, enforce_run_quota
from hoursx.services import AppServices

log = get_logger("orchestration")


class Conductor:
    def __init__(self, services: AppServices) -> None:
        self._services = services
        self.runtime = AgentRuntime(services)
        self._planner = Planner(services.router)
        self._inline_tasks: set[asyncio.Task] = set()

    # ----------------------------------------------------------------- submit

    async def submit_message(
        self,
        *,
        session_id: str,
        user_id: str,
        text: str,
        plan_first: bool = False,
        idempotency_key: str | None = None,
    ) -> str:
        """Persist the user's message, create a run, and dispatch it.

        Returns the run id immediately; progress flows through the event bus.
        Supplying ``idempotency_key`` makes a retried submission return the
        original run instead of starting a duplicate — networks retry, and a
        duplicated agent run means duplicated side effects and spend.
        """
        async with self._services.db.session() as db:
            chat_session = await db.get(Session, session_id)
            if chat_session is None:
                raise NotFoundError(f"session {session_id} not found", session_id=session_id)
            workspace_id = chat_session.workspace_id
            agent_profile_id = chat_session.agent_profile_id

            if idempotency_key:
                existing = (
                    (
                        await db.execute(
                            select(Run).where(
                                Run.workspace_id == workspace_id,
                                Run.idempotency_key == idempotency_key,
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if existing is not None:
                    return existing.id

            await enforce_run_quota(db, workspace_id=workspace_id, policy=self._quota_policy())

        goal = text
        if plan_first:
            plan = await self._planner.plan(text)
            goal = f"{text}\n\nWork through this plan:\n{plan.as_checklist()}"

        async with self._services.db.session() as db:
            db.add(Message(session_id=session_id, role="user", content=text))
            run = Run(
                workspace_id=workspace_id,
                session_id=session_id,
                agent_profile_id=agent_profile_id,
                goal=goal,
                status="queued",
                idempotency_key=idempotency_key,
            )
            db.add(run)
            await db.flush()
            run_id = run.id

        await self._dispatch(run_id)
        return run_id

    def _quota_policy(self) -> QuotaPolicy:
        settings = self._services.settings
        return QuotaPolicy(
            max_concurrent_runs=settings.max_concurrent_runs_per_workspace,
            max_runs_per_hour=settings.max_runs_per_hour_per_workspace,
        )

    async def request_cancel(self, *, run_id: str, workspace_id: str) -> bool:
        """Flag a run for cancellation; the loop observes it at the next step.

        Returns False when the run already reached a terminal state — cancelling
        finished work is a no-op, not an error.
        """
        async with self._services.db.session() as db:
            run = await db.get(Run, run_id)
            if run is None or run.workspace_id != workspace_id:
                raise NotFoundError(f"run {run_id} not found", run_id=run_id)
            if run.status in ("succeeded", "failed", "cancelled"):
                return False
            run.cancel_requested = True
            # A run parked on approval has no loop polling the flag, so settle
            # it here rather than leaving it waiting forever.
            if run.status in ("queued", "awaiting_approval"):
                run.status = "cancelled"
                run.error = "cancelled by operator"
                run.checkpoint = None
                run.finished_at = utcnow()
                terminal = True
            else:
                terminal = False
            await db.flush()

        if terminal:
            await self._services.bus.publish(
                Event(
                    type=EventType.RUN_FINISHED,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    payload={
                        "status": "cancelled",
                        "answer": None,
                        "error": "cancelled by operator",
                    },
                )
            )
        return True

    async def _dispatch(self, run_id: str) -> None:
        if self._services.settings.task_backend == "arq":
            from hoursx.jobs import enqueue_job

            await enqueue_job(self._services.settings, "execute_run_job", run_id)
            return
        task = asyncio.create_task(self._execute_logged(run_id))
        # Keep a strong ref until done; asyncio only weakly references tasks.
        self._inline_tasks.add(task)
        task.add_done_callback(self._inline_tasks.discard)

    async def _execute_logged(self, run_id: str) -> None:
        try:
            await self.runtime.execute_run(run_id)
        except Exception:  # noqa: BLE001 — dispatch boundary; runtime already records
            log.exception("inline run %s failed at dispatch boundary", run_id)

    async def wait_for_inline_runs(self) -> None:
        """Test/shutdown helper: drain in-flight inline executions."""
        if self._inline_tasks:
            await asyncio.gather(*list(self._inline_tasks), return_exceptions=True)

    # -------------------------------------------------------------- approvals

    async def decide_approval(self, *, approval_id: str, decided_by: str, approved: bool) -> None:
        """Record a human decision and resume the parked run."""
        async with self._services.db.session() as db:
            request = await db.get(ApprovalRequest, approval_id)
            if request is None:
                raise NotFoundError(f"approval {approval_id} not found", approval_id=approval_id)
            if request.status != "pending":
                return  # idempotent: a second decision is a no-op
            request.status = "approved" if approved else "denied"
            request.decided_by = decided_by
            request.decided_at = utcnow()
            await db.flush()
            workspace_id, run_id = request.workspace_id, request.run_id

        await self._services.bus.publish(
            Event(
                type=EventType.APPROVAL_DECIDED,
                workspace_id=workspace_id,
                run_id=run_id,
                payload={"approval_id": approval_id, "approved": approved},
            )
        )

        if self._services.settings.task_backend == "arq":
            from hoursx.jobs import enqueue_job

            await enqueue_job(
                self._services.settings, "resume_run_job", run_id, approval_id, approved
            )
            return
        task = asyncio.create_task(
            self.runtime.resume_run(run_id, approval_id=approval_id, approved=approved)
        )
        self._inline_tasks.add(task)
        task.add_done_callback(self._inline_tasks.discard)
