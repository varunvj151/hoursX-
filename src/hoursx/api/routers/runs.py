"""Run inspection, live streaming (SSE), and approval decisions."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from hoursx.api.deps import Actor, get_conductor, get_services, require
from hoursx.api.schemas import ApprovalDecision, ApprovalOut, RunOut, RunStepOut
from hoursx.auth import Permission
from hoursx.db.models import ApprovalRequest, Run, RunStep
from hoursx.errors import NotFoundError
from hoursx.orchestration import Conductor
from hoursx.services import AppServices

router = APIRouter(prefix="/v1/runs", tags=["runs"])

_TERMINAL = {"succeeded", "failed", "cancelled"}


def _out(run: Run) -> RunOut:
    return RunOut(
        id=run.id,
        session_id=run.session_id,
        status=run.status,
        goal=run.goal,
        final_answer=run.final_answer,
        error=run.error,
        step_count=run.step_count,
        created_at=run.created_at,
        finished_at=run.finished_at,
        cancel_requested=run.cancel_requested,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
    )


async def _owned_run(services: AppServices, actor: Actor, run_id: str) -> Run:
    async with services.db.session() as db:
        run = await db.get(Run, run_id)
        if run is None or run.workspace_id != actor.workspace.id:
            raise NotFoundError("run not found", run_id=run_id)
        return run


@router.get("/{run_id}", response_model=RunOut)
async def get_run(
    run_id: str,
    actor: Actor = Depends(require(Permission.SESSIONS_USE)),
    services: AppServices = Depends(get_services),
) -> RunOut:
    return _out(await _owned_run(services, actor, run_id))


@router.get("/{run_id}/steps", response_model=list[RunStepOut])
async def get_run_steps(
    run_id: str,
    actor: Actor = Depends(require(Permission.SESSIONS_USE)),
    services: AppServices = Depends(get_services),
) -> list[RunStepOut]:
    await _owned_run(services, actor, run_id)
    async with services.db.session() as db:
        rows = (
            (
                await db.execute(
                    select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.index)
                )
            )
            .scalars()
            .all()
        )
        return [
            RunStepOut(index=row.index, kind=row.kind, detail=row.detail, created_at=row.created_at)
            for row in rows
        ]


@router.post("/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(
    run_id: str,
    actor: Actor = Depends(require(Permission.SESSIONS_USE)),
    conductor: Conductor = Depends(get_conductor),
) -> dict:
    """Request cancellation. Cancellation is cooperative — a running loop stops
    at its next step boundary rather than being killed mid-tool."""
    accepted = await conductor.request_cancel(run_id=run_id, workspace_id=actor.workspace.id)
    return {"ok": True, "cancelling": accepted}


@router.get("/{run_id}/stream")
async def stream_run(
    run_id: str,
    actor: Actor = Depends(require(Permission.SESSIONS_USE)),
    services: AppServices = Depends(get_services),
) -> StreamingResponse:
    """Server-sent events for one run: deltas, steps, and the terminal state.

    Ends when the run finishes. If the run is already terminal, replays just the
    terminal event so late subscribers still get a definitive outcome.
    """
    run = await _owned_run(services, actor, run_id)

    async def event_source():
        if run.status in _TERMINAL:
            yield _sse(
                "run.finished",
                {"status": run.status, "answer": run.final_answer, "error": run.error},
            )
            return
        subscription = services.bus.subscribe(actor.workspace.id)
        try:
            async for event in subscription:
                if event.run_id != run_id:
                    continue
                yield _sse(event.type.value, event.payload)
                if event.type.value == "run.finished":
                    return
        finally:
            await subscription.aclose()

    return StreamingResponse(event_source(), media_type="text/event-stream")


def _sse(event_name: str, payload: dict) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, default=str)}\n\n"


# ------------------------------------------------------------------- approvals

approvals = APIRouter(prefix="/v1/approvals", tags=["approvals"])


@approvals.get("", response_model=list[ApprovalOut])
async def list_pending_approvals(
    actor: Actor = Depends(require(Permission.RUNS_APPROVE)),
    services: AppServices = Depends(get_services),
) -> list[ApprovalOut]:
    async with services.db.session() as db:
        rows = (
            (
                await db.execute(
                    select(ApprovalRequest)
                    .where(
                        ApprovalRequest.workspace_id == actor.workspace.id,
                        ApprovalRequest.status == "pending",
                    )
                    .order_by(ApprovalRequest.created_at)
                )
            )
            .scalars()
            .all()
        )
        return [
            ApprovalOut(
                id=row.id,
                run_id=row.run_id,
                tool_name=row.tool_name,
                arguments=row.arguments,
                reason=row.reason,
                status=row.status,
                created_at=row.created_at,
            )
            for row in rows
        ]


@approvals.post("/{approval_id}/decision", status_code=status.HTTP_202_ACCEPTED)
async def decide(
    approval_id: str,
    body: ApprovalDecision,
    actor: Actor = Depends(require(Permission.RUNS_APPROVE)),
    services: AppServices = Depends(get_services),
    conductor: Conductor = Depends(get_conductor),
) -> dict:
    async with services.db.session() as db:
        request = await db.get(ApprovalRequest, approval_id)
        if request is None or request.workspace_id != actor.workspace.id:
            raise NotFoundError("approval not found", approval_id=approval_id)
    await conductor.decide_approval(
        approval_id=approval_id, decided_by=actor.user.id, approved=body.approved
    )
    return {"ok": True}


async def wait_run_terminal(
    services: AppServices, run_id: str, timeout: float = 30.0
) -> Run | None:
    """Poll helper used by tests/CLI: wait until a run reaches a terminal state."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with services.db.session() as db:
            run = await db.get(Run, run_id)
            if run is not None and run.status in (*_TERMINAL, "awaiting_approval"):
                return run
        await asyncio.sleep(0.05)
    return None
