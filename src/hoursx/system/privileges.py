"""The privilege envelope for host and kernel operations.

Every system operation falls into exactly one class:

- ``READ`` — observation only. Safe to run unattended; this is the bulk of what
  a diagnosing agent actually needs.
- ``MUTATE`` — changes live host state, but is reversible and scoped. Requires
  human approval before it executes.
- ``REFUSED`` — no approval gate makes it acceptable, because the failure mode
  is unrecoverable, unbounded, or destroys the very control plane that would
  let an operator intervene.

The refusal list is deliberately short and specific. A blanket ban would push
operators toward giving the agent raw ``shell.run`` as root, which is strictly
worse: it removes classification, approval, and audit in one step. Naming the
genuinely unrecoverable operations and permitting the rest — under approval —
is what keeps the safe path also the convenient one.
"""

from __future__ import annotations

import re
import signal as signal_module
from dataclasses import dataclass, field
from enum import StrEnum


class OperationClass(StrEnum):
    READ = "read"
    MUTATE = "mutate"
    REFUSED = "refused"


class UnsafeOperationError(Exception):
    """Raised for a REFUSED operation. Carries the reason and the alternative."""

    def __init__(self, operation: str, reason: str, alternative: str = "") -> None:
        message = f"refused: {operation} — {reason}"
        if alternative:
            message += f". {alternative}"
        super().__init__(message)
        self.operation = operation
        self.reason = reason
        self.alternative = alternative


# --------------------------------------------------------------------- sysctl

# Tunables an operator would plausibly ask an agent to adjust while diagnosing:
# networking backlogs, connection tracking, VM pressure, file handles.
_SYSCTL_MUTABLE_PREFIXES = (
    "net.core.",
    "net.ipv4.tcp_",
    "net.ipv4.ip_local_port_range",
    "net.netfilter.nf_conntrack_max",
    "vm.swappiness",
    "vm.dirty_ratio",
    "vm.dirty_background_ratio",
    "vm.vfs_cache_pressure",
    "vm.max_map_count",
    "fs.file-max",
    "fs.inotify.",
    "kernel.pid_max",
)

# Writing these disables the protections that make everything else survivable,
# or hands over arbitrary kernel-mode execution.
_SYSCTL_REFUSED = {
    "kernel.modules_disabled": (
        "one-way switch that cannot be undone without a reboot",
        "Adjust module policy through your boot configuration instead.",
    ),
    "kernel.kptr_restrict": (
        "relaxing it leaks kernel pointers to userspace and defeats KASLR",
        "",
    ),
    "kernel.dmesg_restrict": ("weakens kernel log access control", ""),
    "kernel.unprivileged_bpf_disabled": (
        "re-enabling unprivileged BPF is a documented privilege-escalation surface",
        "",
    ),
    "kernel.core_pattern": (
        "it is executed by the kernel on crash and is a known root-escalation vector",
        "",
    ),
    "kernel.sysrq": ("exposes direct kernel commands including immediate reboot", ""),
    "kernel.randomize_va_space": ("disabling ASLR removes a core exploit mitigation", ""),
    "kernel.ftrace_enabled": ("kernel tracing control is not agent-appropriate", ""),
}


def classify_sysctl(key: str) -> tuple[OperationClass, str]:
    """Classify a sysctl write. Returns the class and an explanation."""
    normalized = key.strip()
    if normalized in _SYSCTL_REFUSED:
        reason, alternative = _SYSCTL_REFUSED[normalized]
        return OperationClass.REFUSED, reason if not alternative else f"{reason}. {alternative}"
    if normalized.startswith(_SYSCTL_MUTABLE_PREFIXES):
        return OperationClass.MUTATE, "reversible tunable; requires approval"
    return (
        OperationClass.REFUSED,
        f"{normalized!r} is outside the reviewed tunable set; "
        f"add it to the allowlist deliberately if your deployment needs it",
    )


# -------------------------------------------------------------------- signals

# Standard POSIX signals mapped to integer values for cross-platform compatibility:
_KNOWN_SIGNALS: dict[int, str] = {
    1: "SIGHUP",
    2: "SIGINT",
    9: "SIGKILL",
    10: "SIGUSR1",
    12: "SIGUSR2",
    15: "SIGTERM",
    18: "SIGCONT",
    19: "SIGSTOP",
}
for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGUSR1", "SIGUSR2", "SIGKILL", "SIGSTOP", "SIGCONT"):
    val = getattr(signal_module, name, None)
    if val is not None and isinstance(val, int):
        _KNOWN_SIGNALS[int(val)] = name

# Signals an operator legitimately sends while managing a misbehaving process.
_SIGNALS_ALLOWED: set[int] = set(_KNOWN_SIGNALS.keys())

# Killing init tears down the machine; killing the agent's own process group
# aborts the run mid-flight and strands whatever it was doing.
_PROTECTED_PIDS = {0, 1}


def classify_signal(pid: int, signal_number: int, *, own_pid: int) -> tuple[OperationClass, str]:
    """Classify sending a signal to a process."""
    if pid in _PROTECTED_PIDS:
        return (
            OperationClass.REFUSED,
            f"pid {pid} is the init/kernel process; signalling it halts the host",
        )
    if pid == own_pid:
        return (
            OperationClass.REFUSED,
            "that is the agent's own process; terminating it would abandon this run",
        )
    if pid < 0:
        return (
            OperationClass.REFUSED,
            "negative pids signal an entire process group, which is too broad to approve safely",
        )
    sig_int = int(signal_number)
    sig_name = _KNOWN_SIGNALS.get(sig_int)
    if sig_name is None:
        try:
            resolved = signal_module.Signals(sig_int)
            sig_name = resolved.name
        except (ValueError, AttributeError):
            return OperationClass.REFUSED, f"unknown signal number {signal_number}"

    if sig_int not in _SIGNALS_ALLOWED:
        return OperationClass.REFUSED, f"{sig_name} is not in the permitted signal set"
    return OperationClass.MUTATE, f"sending {sig_name} to pid {pid} requires approval"


# ------------------------------------------------------------- kernel modules

_MODULE_REFUSAL = (
    "loading or unloading kernel modules executes arbitrary code in kernel space, "
    "where there is no sandbox, no rollback, and a fault panics the host"
)
_MODULE_ALTERNATIVE = (
    "Module inventory is readable via system.modules; if a module must change, "
    "do it through your configuration management with a human at the console."
)


def refuse_module_operation(operation: str) -> UnsafeOperationError:
    """Kernel module load/unload is refused unconditionally.

    This is the one place the platform declines a capability outright rather
    than gating it, because approval does not make it recoverable: an operator
    approving ``insmod`` cannot un-panic a kernel.
    """
    return UnsafeOperationError(operation, _MODULE_REFUSAL, _MODULE_ALTERNATIVE)


# ------------------------------------------------------------------- services

_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9@:._\\-]{1,128}$")

# Stopping these removes the operator's own ability to reach or fix the machine.
_CRITICAL_SERVICES = {
    "sshd",
    "ssh",
    "systemd-journald",
    "systemd-logind",
    "dbus",
    "networking",
    "systemd-networkd",
    "NetworkManager",
}

_SERVICE_READ_ACTIONS = {"status", "is-active", "is-enabled", "show", "cat"}
_SERVICE_MUTATE_ACTIONS = {"start", "stop", "restart", "reload", "enable", "disable"}


def classify_service(unit: str, action: str) -> tuple[OperationClass, str]:
    """Classify a service-manager action."""
    if not _SERVICE_NAME_RE.match(unit):
        return OperationClass.REFUSED, f"invalid unit name {unit!r}"
    if action in _SERVICE_READ_ACTIONS:
        return OperationClass.READ, "inspection only"
    if action not in _SERVICE_MUTATE_ACTIONS:
        return OperationClass.REFUSED, f"unsupported service action {action!r}"

    base = unit.removesuffix(".service")
    if base in _CRITICAL_SERVICES and action in {"stop", "disable", "restart"}:
        return (
            OperationClass.REFUSED,
            f"{base} carries the operator's own access to this host; "
            f"stopping it could make the machine unreachable",
        )
    return OperationClass.MUTATE, f"{action} on {unit} requires approval"


# --------------------------------------------------------------------- policy


@dataclass(frozen=True)
class SystemPolicy:
    """Operator-controlled envelope for the whole system toolset.

    ``enabled`` is false by default: deep host access is a deliberate opt-in,
    not something a fresh install grants silently.
    """

    enabled: bool = False
    allow_mutations: bool = False
    extra_sysctl_allowlist: frozenset[str] = field(default_factory=frozenset)
    # "direct" or "helper"; see hoursx.system.helper for why the latter exists.
    backend: str = "direct"
    sysd_socket: str = "/run/hoursx/sysd.sock"

    def check_enabled(self, operation: str) -> None:
        if not self.enabled:
            raise UnsafeOperationError(
                operation,
                "system operations are disabled on this deployment",
                "Set HOURSX_SYSTEM_OPS=true to enable them.",
            )

    def classify_sysctl_write(self, key: str) -> tuple[OperationClass, str]:
        if key in self.extra_sysctl_allowlist:
            return OperationClass.MUTATE, "explicitly allowlisted by the operator"
        return classify_sysctl(key)

    def require_mutations_allowed(self, operation: str) -> None:
        if not self.allow_mutations:
            raise UnsafeOperationError(
                operation,
                "this deployment permits read-only system access",
                "Set HOURSX_SYSTEM_MUTATIONS=true to allow approved changes.",
            )
