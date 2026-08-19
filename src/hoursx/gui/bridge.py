"""Bridge between Tkinter's event loop and the asyncio engine.

Tkinter is synchronous and single-threaded; the engine is asyncio. Rather than
interleave them (fragile, and it stalls the UI on every await), the engine runs
in its own thread with its own loop, and the two sides communicate through a
thread-safe queue that the UI drains on a timer.

This keeps one rule absolute: **no Tkinter call ever happens off the UI thread.**
Widget mutation from a worker thread is the classic way to make a Tk app crash
intermittently, and it is the failure mode this module exists to prevent.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from hoursx.cli.engine import LocalContext, close_local, open_local
from hoursx.events import Event


@dataclass
class UiMessage:
    """One message destined for the UI thread."""

    kind: str  # "event" | "result" | "error" | "ready"
    payload: Any = None
    token: str = ""  # correlates a "result" with the call that requested it


class EngineBridge:
    """Owns the engine thread and the UI-bound message queue."""

    def __init__(self) -> None:
        self._queue: queue.Queue[UiMessage] = queue.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._context: LocalContext | None = None
        self._pump: asyncio.Task | None = None
        self._ready = threading.Event()
        self._stopping = False

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Boot the engine thread; returns immediately."""
        self._thread = threading.Thread(target=self._run_loop, name="hoursx-engine", daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._bootstrap())
            loop.run_forever()
        finally:
            # Drain before closing. Async generators (the event subscription)
            # and driver threads (aiosqlite) schedule their own cleanup, and
            # closing the loop first makes that cleanup raise on exit.
            with contextlib.suppress(RuntimeError):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def _bootstrap(self) -> None:
        try:
            self._context = await open_local()
            self._pump = asyncio.create_task(self._pump_events())
            self.post(UiMessage("ready", self._context.workspace_id))
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI, not swallowed
            self.post(UiMessage("error", f"engine failed to start: {exc}"))
        finally:
            self._ready.set()

    async def _pump_events(self) -> None:
        """Forward every workspace event to the UI queue."""
        assert self._context is not None
        async for event in self._context.services.bus.subscribe(self._context.workspace_id):
            if self._stopping:
                return
            self.post(UiMessage("event", event))

    def stop(self, *, timeout: float = 5.0) -> None:
        """Shut the engine down cleanly.

        Ordering matters: cancel the event subscription first, then dispose the
        database, and only then stop the loop. Stopping the loop while the
        SQLAlchemy driver is still finishing leaves its worker thread awaiting a
        closed loop, which surfaces as a traceback on exit.
        """
        self._stopping = True
        # A user can close the window while the engine is still booting.
        # Stopping the loop mid-bootstrap aborts run_until_complete, so wait
        # for startup to settle (bounded) before tearing anything down.
        self._ready.wait(timeout=timeout)
        loop, context = self._loop, self._context
        if loop is None or loop.is_closed():
            return

        async def shutdown() -> None:
            if self._pump is not None:
                self._pump.cancel()
                await asyncio.gather(self._pump, return_exceptions=True)
            if context is not None:
                await close_local(context)
            # Let any callbacks the disposal queued run before the loop halts.
            await asyncio.sleep(0)
            loop.stop()

        future = asyncio.run_coroutine_threadsafe(shutdown(), loop)
        # Teardown must never block the exit path, whatever the driver does.
        with contextlib.suppress(Exception):
            future.result(timeout=timeout)
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # ------------------------------------------------------------ messaging

    def post(self, message: UiMessage) -> None:
        self._queue.put(message)

    def drain(self, limit: int = 200) -> list[UiMessage]:
        """Pull pending messages. Called from the UI thread on a timer.

        Bounded per tick so a burst of streaming deltas cannot monopolise the
        UI thread and freeze the window.
        """
        messages: list[UiMessage] = []
        for _ in range(limit):
            try:
                messages.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return messages

    @property
    def context(self) -> LocalContext | None:
        return self._context

    def submit(
        self,
        factory: Callable[[LocalContext], Coroutine[Any, Any, Any]],
        *,
        token: str = "",
    ) -> None:
        """Schedule engine work; the outcome arrives as a queued UI message."""
        loop = self._loop
        if loop is None or self._context is None:
            self.post(UiMessage("error", "engine is not ready yet", token))
            return

        async def wrapper() -> None:
            assert self._context is not None
            try:
                result = await factory(self._context)
                self.post(UiMessage("result", result, token))
            except Exception as exc:  # noqa: BLE001 — the UI shows the failure
                self.post(UiMessage("error", str(exc), token))

        asyncio.run_coroutine_threadsafe(wrapper(), loop)


def describe_event(event: Event) -> str:
    """One-line human rendering of an event for the activity log."""
    payload = event.payload
    match event.type.value:
        case "run.started":
            return f"▶ run started — {payload.get('goal', '')[:80]}"
        case "run.step":
            mark = "✓" if payload.get("ok") else "✗"
            return f"  {mark} {payload.get('tool')} — {payload.get('summary', '')[:100]}"
        case "run.awaiting_approval":
            return f"⏸ approval needed: {payload.get('tool')}"
        case "run.finished":
            status = payload.get("status")
            detail = payload.get("error") or payload.get("answer") or ""
            return f"■ run {status} — {str(detail)[:100]}"
        case "approval.decided":
            return f"● approval {'granted' if payload.get('approved') else 'denied'}"
        case "document.ingested":
            return f"◆ document ingested ({payload.get('chunks', 0)} chunks)"
        case _:
            return f"· {event.type.value}"
