"""WebSocket event gateway.

Clients connect with a token (query parameter — browsers cannot set WS headers)
and receive every event for their workspace as JSON frames. The socket is
read-mostly; the only inbound frame is ``{"type": "ping"}``.
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from hoursx.auth import TokenError, verify_token
from hoursx.db.models import User, WorkspaceMember

router = APIRouter()


@router.websocket("/ws")
async def event_socket(websocket: WebSocket, token: str = Query(default="")) -> None:
    services = websocket.app.state.services
    try:
        user_id = verify_token(token, secret=services.settings.jwt_secret)
    except TokenError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    async with services.db.session() as db:
        user = await db.get(User, user_id)
        membership = (
            (
                await db.execute(
                    select(WorkspaceMember)
                    .where(WorkspaceMember.user_id == user_id)
                    .order_by(WorkspaceMember.created_at)
                )
            )
            .scalars()
            .first()
        )
    if user is None or not user.is_active or membership is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    workspace_id = membership.workspace_id

    async def pump_events() -> None:
        subscription = services.bus.subscribe(workspace_id)
        try:
            async for event in subscription:
                await websocket.send_text(event.model_dump_json())
        finally:
            await subscription.aclose()

    pump = asyncio.create_task(pump_events())
    try:
        while True:
            frame = await websocket.receive_json()
            if frame.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
