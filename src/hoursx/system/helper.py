"""Client for ``hoursx-sysd``, the privileged helper daemon.

Selecting this backend moves every privileged operation out of the Python
process and into a small C++ daemon reached over a Unix socket. The agent then
runs with no capabilities at all, which is the point: the process that embeds a
language model, makes network calls, and executes plugin code should not be the
process holding ``CAP_SYS_ADMIN``.

The daemon re-checks the policy itself, so this client is a transport rather
than a control. Nothing here is load-bearing for security — if it were, a
compromise of this process would defeat it.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass

from hoursx.observability import get_logger
from hoursx.system.ops import OpResult

log = get_logger("system.helper")

DEFAULT_SOCKET = "/run/hoursx/sysd.sock"
_TIMEOUT_SECONDS = 90.0
_MAX_REPLY_BYTES = 1 << 20


class HelperUnavailableError(Exception):
    """The daemon could not be reached.

    Distinct from a refusal: unreachable means the operator has not deployed or
    started the helper, which is a configuration problem with a clear fix, not a
    policy decision.
    """


@dataclass(frozen=True)
class HelperClient:
    socket_path: str = DEFAULT_SOCKET

    async def _call(self, verb: str, *args: str) -> tuple[bool, str]:
        """Send one request and return (ok, payload)."""
        encoded = " ".join([verb, *(base64.b64encode(arg.encode()).decode() for arg in args)])
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.socket_path), timeout=10
            )
        except (TimeoutError, OSError) as exc:
            raise HelperUnavailableError(
                f"cannot reach hoursx-sysd at {self.socket_path}: {exc}. "
                f"Start the daemon, or set HOURSX_SYSTEM_BACKEND=direct."
            ) from exc

        try:
            writer.write(encoded.encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=_TIMEOUT_SECONDS)
        except (TimeoutError, OSError) as exc:
            raise HelperUnavailableError(f"hoursx-sysd did not respond: {exc}") from exc
        finally:
            writer.close()
            # The daemon closes its side after each connection; a failure to
            # await that is not worth propagating over a completed call.
            with _suppress_connection_teardown():
                await writer.wait_closed()

        if not line or len(line) > _MAX_REPLY_BYTES:
            raise HelperUnavailableError("hoursx-sysd returned an unusable reply")

        status, _, payload_b64 = line.decode(errors="replace").strip().partition(" ")
        try:
            payload = base64.b64decode(payload_b64).decode(errors="replace")
        except (ValueError, TypeError):
            raise HelperUnavailableError("hoursx-sysd returned a malformed reply") from None
        return status == "OK", payload

    async def ping(self) -> str:
        """Return the daemon's version string, or raise if it is unreachable."""
        ok, payload = await self._call("PING")
        if not ok:
            raise HelperUnavailableError(payload)
        return payload

    async def sysctl_get(self, key: str) -> str | None:
        ok, payload = await self._call("SYSCTL_GET", key)
        return payload if ok else None

    async def sysctl_set(self, key: str, value: str) -> OpResult:
        ok, payload = await self._call("SYSCTL_SET", key, value)
        if ok:
            log.info("helper applied sysctl", extra={"hoursx": {"key": key}})
            return OpResult(True, f"{key}: {payload}", {"key": key, "detail": payload})
        return OpResult(False, payload, {"key": key})

    async def send_signal(self, pid: int, signal_number: int) -> OpResult:
        ok, payload = await self._call("SIGNAL", str(pid), str(signal_number))
        return OpResult(ok, payload, {"pid": pid, "signal": signal_number})

    async def service(self, unit: str, action: str) -> OpResult:
        ok, payload = await self._call("SERVICE", unit, action)
        exit_code = _parse_exit_code(payload)
        return OpResult(
            ok,
            f"systemctl {action} {unit} -> exit {exit_code}" if exit_code is not None else payload,
            {"unit": unit, "action": action, "exit_code": exit_code, "output": payload},
        )


def _parse_exit_code(payload: str) -> int | None:
    first, _, _ = payload.partition("\n")
    if first.startswith("exit "):
        try:
            return int(first[5:].strip())
        except ValueError:
            return None
    return None


class _suppress_connection_teardown:
    """Swallow teardown errors on an already-completed call."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, (OSError, BrokenPipeError))


async def probe_helper(socket_path: str = DEFAULT_SOCKET) -> tuple[bool, str]:
    """Check whether the daemon is reachable. Used by ``hoursx doctor``."""
    try:
        version = await HelperClient(socket_path).ping()
    except HelperUnavailableError as exc:
        return False, str(exc)
    return True, version
