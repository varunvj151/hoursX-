"""Declarative post-conditions over host state.

A condition is data, never code. The agent picks a probe from a closed set,
names a target, and states a comparison — so a post-condition can be persisted,
shown to a human before approval, and re-evaluated later without ever handing
model output to an evaluator.

Deliberately no expression language. ``eval`` on model-authored strings inside
a component that also holds host privileges is exactly the shape of bug this
whole subsystem exists to prevent.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from hoursx.system.probe import (
    list_processes,
    listening_sockets,
    read_sysctl,
    resource_snapshot,
)


class ProbeKind(StrEnum):
    """What a condition can observe. Closed set, by design."""

    MEMORY_USED_PERCENT = "memory_used_percent"
    LOAD_1M = "load_1m"
    LOAD_5M = "load_5m"
    PROCESS_COUNT = "process_count"
    OPEN_FILE_DESCRIPTORS = "open_file_descriptors"
    DISK_USED_PERCENT = "disk_used_percent"  # target = mountpoint
    DISK_FREE_BYTES = "disk_free_bytes"  # target = mountpoint
    SYSCTL_VALUE = "sysctl_value"  # target = dotted sysctl key
    PORT_LISTENING = "port_listening"  # target = port number
    PROCESS_RUNNING = "process_running"  # target = process name
    SERVICE_ACTIVE = "service_active"  # target = systemd unit


class Operator(StrEnum):
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    EQ = "eq"
    NE = "ne"


_NEEDS_TARGET = {
    ProbeKind.DISK_USED_PERCENT,
    ProbeKind.DISK_FREE_BYTES,
    ProbeKind.SYSCTL_VALUE,
    ProbeKind.PORT_LISTENING,
    ProbeKind.PROCESS_RUNNING,
    ProbeKind.SERVICE_ACTIVE,
}

_SYMBOLS = {
    Operator.LT: "<",
    Operator.LTE: "<=",
    Operator.GT: ">",
    Operator.GTE: ">=",
    Operator.EQ: "==",
    Operator.NE: "!=",
}


class Condition(BaseModel):
    """One assertion about host state after a change."""

    probe: ProbeKind
    operator: Operator
    value: float | str | bool = Field(description="Value to compare the observation against")
    target: str = Field(default="", description="Mountpoint, sysctl key, port, unit, or process")

    def describe(self) -> str:
        subject = f"{self.probe.value}({self.target})" if self.target else self.probe.value
        return f"{subject} {_SYMBOLS[self.operator]} {self.value}"


@dataclass
class ConditionResult:
    condition: Condition
    met: bool
    observed: Any
    detail: str = ""

    def describe(self) -> str:
        verdict = "held" if self.met else "FAILED"
        base = f"{self.condition.describe()} -> {verdict} (observed {self.observed!r})"
        return f"{base}; {self.detail}" if self.detail else base


class _Unobservable(Exception):
    """The probe could not read the value it needs."""


# ----------------------------------------------------------------- observation


async def _observe(condition: Condition) -> Any:
    probe, target = condition.probe, condition.target

    if probe in _NEEDS_TARGET and not target:
        raise _Unobservable(f"{probe.value} requires a target")

    if probe is ProbeKind.SYSCTL_VALUE:
        value = read_sysctl(target)
        if value is None:
            raise _Unobservable(f"sysctl {target} is not readable")
        return value

    if probe is ProbeKind.PORT_LISTENING:
        if not target.isdigit():
            raise _Unobservable(f"{target!r} is not a port number")
        wanted = int(target)
        return any(socket["port"] == wanted for socket in await listening_sockets(limit=500))

    if probe is ProbeKind.PROCESS_RUNNING:
        needle = target.lower()
        return any(
            needle in row["name"].lower() or needle in row["cmdline"].lower()
            for row in list_processes(limit=200, sort_by="pid")
        )

    if probe is ProbeKind.SERVICE_ACTIVE:
        return await _service_is_active(target)

    snapshot = resource_snapshot()

    if probe is ProbeKind.MEMORY_USED_PERCENT:
        used = snapshot.memory_used_percent
        if used is None:
            raise _Unobservable("memory usage is not readable")
        return used
    if probe in (ProbeKind.LOAD_1M, ProbeKind.LOAD_5M):
        if snapshot.load_average is None:
            raise _Unobservable("load average is not readable")
        return snapshot.load_average[0 if probe is ProbeKind.LOAD_1M else 1]
    if probe is ProbeKind.PROCESS_COUNT:
        return snapshot.process_count
    if probe is ProbeKind.OPEN_FILE_DESCRIPTORS:
        if snapshot.open_file_descriptors is None:
            raise _Unobservable("file descriptor count is not readable")
        return snapshot.open_file_descriptors

    disk = next((d for d in snapshot.disks if d["mountpoint"] == target), None)
    if disk is None:
        raise _Unobservable(f"no mounted filesystem at {target}")
    return disk["used_percent" if probe is ProbeKind.DISK_USED_PERCENT else "free_bytes"]


async def _service_is_active(unit: str) -> bool:
    import asyncio

    if shutil.which("systemctl") is None:
        raise _Unobservable("systemctl is not available on this host")
    process = await asyncio.create_subprocess_exec(
        "systemctl",
        "is-active",
        unit,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await asyncio.wait_for(process.communicate(), timeout=20)
    return stdout.decode(errors="replace").strip() == "active"


# ------------------------------------------------------------------ comparison


def _compare(observed: Any, operator: Operator, expected: Any) -> bool:
    """Compare, coercing to a common type where it is unambiguous.

    Kernel interfaces return strings ("10", "active"), while a model naturally
    writes numbers and booleans. Coercing here means a correct condition is not
    rejected over a type mismatch the author could not reasonably anticipate.
    """
    if operator in (Operator.EQ, Operator.NE):
        equal = _loosely_equal(observed, expected)
        return equal if operator is Operator.EQ else not equal

    left, right = _as_number(observed), _as_number(expected)
    if left is None or right is None:
        # Ordering comparisons are meaningless on non-numeric values; treating
        # that as "unmet" would silently pass a nonsense condition.
        raise _Unobservable(
            f"cannot order-compare {observed!r} against {expected!r}; use eq/ne instead"
        )
    match operator:
        case Operator.LT:
            return left < right
        case Operator.LTE:
            return left <= right
        case Operator.GT:
            return left > right
        case Operator.GTE:
            return left >= right
    return False


def _loosely_equal(observed: Any, expected: Any) -> bool:
    if isinstance(observed, bool) or isinstance(expected, bool):
        return _as_bool(observed) == _as_bool(expected)
    left, right = _as_number(observed), _as_number(expected)
    if left is not None and right is not None:
        return left == right
    return str(observed).strip() == str(expected).strip()


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "active", "on"}


# ------------------------------------------------------------------ evaluation


async def evaluate_conditions(conditions: list[Condition]) -> list[ConditionResult]:
    """Evaluate every condition, never raising.

    An unobservable probe yields ``met=False`` with an explanation rather than an
    exception. That is the safe direction: a change whose effect cannot be
    confirmed is treated as unverified, which triggers revert — better than
    assuming success because the check itself broke.
    """
    results: list[ConditionResult] = []
    for condition in conditions:
        try:
            observed = await _observe(condition)
        except _Unobservable as exc:
            results.append(
                ConditionResult(condition=condition, met=False, observed=None, detail=str(exc))
            )
            continue
        except Exception as exc:  # noqa: BLE001 — a probe fault must not abort the sweep
            results.append(
                ConditionResult(
                    condition=condition, met=False, observed=None, detail=f"probe error: {exc}"
                )
            )
            continue

        try:
            met = _compare(observed, condition.operator, condition.value)
            detail = ""
        except _Unobservable as exc:
            met, detail = False, str(exc)
        results.append(
            ConditionResult(condition=condition, met=met, observed=observed, detail=detail)
        )
    return results


def all_met(results: list[ConditionResult]) -> bool:
    return all(result.met for result in results)
