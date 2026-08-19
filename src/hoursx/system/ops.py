"""Host mutations, each behind the privilege envelope.

Every function here classifies before it acts. A ``REFUSED`` classification
raises :class:`UnsafeOperationError` and nothing is attempted; a ``MUTATE``
classification is executed only because the caller already obtained human
approval through the tool executor's gate.
"""

from __future__ import annotations

import asyncio
import os
import signal as signal_module
from dataclasses import dataclass
from typing import Any

from hoursx.observability import get_logger
from hoursx.system.privileges import (
    OperationClass,
    SystemPolicy,
    UnsafeOperationError,
    classify_service,
    classify_signal,
    refuse_module_operation,
)
from hoursx.system.probe import read_sysctl, sysctl_path

log = get_logger("system.ops")


@dataclass
class OpResult:
    ok: bool
    summary: str
    detail: dict[str, Any]


def _helper_for(policy: SystemPolicy):
    """Return a helper client when the operator selected that backend.

    Policy is still evaluated here first. The helper checks again on its own
    authority, so the two are independent rather than layered — neither relies
    on the other having done its job.
    """
    if policy.backend != "helper":
        return None
    from hoursx.system.helper import HelperClient

    return HelperClient(policy.sysd_socket)


async def write_sysctl(policy: SystemPolicy, key: str, value: str) -> OpResult:
    """Set a kernel parameter, recording its previous value for rollback."""
    policy.check_enabled(f"sysctl write {key}")
    policy.require_mutations_allowed(f"sysctl write {key}")

    classification, reason = policy.classify_sysctl_write(key)
    if classification is OperationClass.REFUSED:
        raise UnsafeOperationError(f"sysctl write {key}", reason)

    helper = _helper_for(policy)
    if helper is not None:
        return await helper.sysctl_set(key, value)

    previous = read_sysctl(key)
    try:
        path = sysctl_path(key)
    except ValueError as exc:
        raise UnsafeOperationError(f"sysctl write {key}", str(exc)) from exc
    if not path.exists():
        return OpResult(False, f"sysctl {key} does not exist on this kernel", {})

    try:
        path.write_text(value)
    except PermissionError:
        return OpResult(
            False,
            f"permission denied writing {key}; the process lacks CAP_SYS_ADMIN "
            f"or the parameter is read-only in this namespace",
            {"key": key, "previous": previous},
        )
    except OSError as exc:
        return OpResult(False, f"failed to write {key}: {exc}", {"key": key})

    current = read_sysctl(key)
    log.warning("sysctl changed", extra={"hoursx": {"key": key, "from": previous, "to": current}})
    return OpResult(
        True,
        f"{key}: {previous} -> {current}",
        # previous is returned so an operator (or the agent) can revert exactly.
        {"key": key, "previous": previous, "current": current},
    )


async def send_signal(policy: SystemPolicy, pid: int, signal_number: int) -> OpResult:
    """Signal a process, refusing protected targets."""
    policy.check_enabled(f"signal pid {pid}")
    policy.require_mutations_allowed(f"signal pid {pid}")

    classification, reason = classify_signal(pid, signal_number, own_pid=os.getpid())
    if classification is OperationClass.REFUSED:
        raise UnsafeOperationError(f"signal pid {pid}", reason)

    helper = _helper_for(policy)
    if helper is not None:
        return await helper.send_signal(pid, signal_number)

    try:
        os.kill(pid, signal_number)
    except (ProcessLookupError, OSError):
        return OpResult(False, f"no process with pid {pid}", {"pid": pid})
    except PermissionError:
        return OpResult(False, f"permission denied signalling pid {pid}", {"pid": pid})
    try:
        name = signal_module.Signals(signal_number).name
    except (ValueError, AttributeError):
        name = f"SIG_{signal_number}"
    return OpResult(True, f"sent {name} to pid {pid}", {"pid": pid, "signal": name})


async def manage_service(policy: SystemPolicy, unit: str, action: str) -> OpResult:
    """Run a systemd unit action, refusing to sever operator access."""
    policy.check_enabled(f"service {action} {unit}")

    classification, reason = classify_service(unit, action)
    if classification is OperationClass.REFUSED:
        raise UnsafeOperationError(f"service {action} {unit}", reason)
    if classification is OperationClass.MUTATE:
        policy.require_mutations_allowed(f"service {action} {unit}")

    helper = _helper_for(policy)
    if helper is not None:
        return await helper.service(unit, action)

    process = await asyncio.create_subprocess_exec(
        "systemctl",
        action,
        unit,
        "--no-pager",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
    except TimeoutError:
        process.kill()
        await process.wait()
        return OpResult(False, f"systemctl {action} {unit} timed out", {"unit": unit})
    except FileNotFoundError:
        return OpResult(
            False,
            "systemctl is not available on this host (not a systemd system, or a container)",
            {"unit": unit},
        )

    output = stdout.decode(errors="replace")[-16000:]
    exit_code = process.returncode or 0
    # `is-active` and friends use exit status as the answer, so a nonzero code
    # is information, not failure.
    informational = action in {"is-active", "is-enabled"}
    return OpResult(
        ok=exit_code == 0 or informational,
        summary=f"systemctl {action} {unit} -> exit {exit_code}",
        detail={"unit": unit, "action": action, "exit_code": exit_code, "output": output},
    )


def load_kernel_module(*_args: Any, **_kwargs: Any) -> OpResult:
    """Always refuses. Present so the boundary is explicit in the codebase
    rather than an unstated absence."""
    raise refuse_module_operation("kernel module load")


def unload_kernel_module(*_args: Any, **_kwargs: Any) -> OpResult:
    """Always refuses; see :func:`load_kernel_module`."""
    raise refuse_module_operation("kernel module unload")
