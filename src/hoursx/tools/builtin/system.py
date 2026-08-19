"""Kernel and host tools exposed to the agent.

Read tools carry no approval flag — an agent diagnosing an incident should not
need a human for every ``/proc`` read. Mutating tools set
``requires_approval=True``, so the runtime checkpoints the run and waits for a
person before anything on the host changes.

The whole toolset is inert unless the operator enables it; every tool reports
that clearly rather than failing obscurely, so a disabled capability reads as a
configuration choice instead of a bug.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from hoursx.system.ops import manage_service, send_signal, write_sysctl
from hoursx.system.privileges import SystemPolicy, UnsafeOperationError
from hoursx.system.probe import (
    cgroup_summary,
    kernel_facts,
    list_namespaces,
    list_processes,
    listening_sockets,
    loaded_modules,
    read_kernel_log,
    read_sysctl,
    read_sysctl_group,
    resource_snapshot,
)
from hoursx.tools.base import FunctionTool, ToolContext, ToolOutcome, ToolSpec
from hoursx.tools.registry import ToolRegistry


def _policy(ctx: ToolContext) -> SystemPolicy:
    """Resolve the operator's policy from settings; default-off when absent."""
    services = ctx.services
    if services is None:
        return SystemPolicy()
    settings = services.settings
    return SystemPolicy(
        enabled=settings.system_ops_enabled,
        allow_mutations=settings.system_mutations_enabled,
        extra_sysctl_allowlist=frozenset(settings.system_sysctl_allowlist),
        backend=settings.system_backend,
        sysd_socket=settings.sysd_socket,
    )


def _disabled_notice(operation: str) -> ToolOutcome:
    return ToolOutcome.failure(
        f"System operations are disabled on this deployment, so {operation} is unavailable. "
        f"An operator can enable them with HOURSX_SYSTEM_OPS=true."
    )


class NoArgs(BaseModel):
    pass


class ProcessArgs(BaseModel):
    limit: int = Field(default=25, ge=1, le=200, description="How many processes to return")
    sort_by: str = Field(default="rss", pattern="^(rss|pid)$")


class KernelLogArgs(BaseModel):
    lines: int = Field(default=80, ge=1, le=1000)


class SysctlReadArgs(BaseModel):
    key: str = Field(
        description="Dotted sysctl key or prefix, e.g. 'vm.swappiness' or 'net.ipv4.tcp'"
    )


class SysctlWriteArgs(BaseModel):
    key: str = Field(description="Dotted sysctl key, e.g. 'vm.swappiness'")
    value: str = Field(description="New value to write")


class SignalArgs(BaseModel):
    pid: int = Field(description="Target process id")
    signal: int = Field(default=15, description="Signal number (15=TERM, 9=KILL, 1=HUP)")


class ServiceArgs(BaseModel):
    unit: str = Field(description="systemd unit, e.g. 'nginx' or 'nginx.service'")
    action: str = Field(
        default="status",
        pattern="^(status|is-active|is-enabled|show|cat|start|stop|restart|reload|enable|disable)$",
    )


class NamespaceArgs(BaseModel):
    pid: int = Field(default=1, ge=1, description="Process whose namespaces to inspect")


# ---------------------------------------------------------------- read tools


async def _kernel_info(args: NoArgs, ctx: ToolContext) -> ToolOutcome:
    policy = _policy(ctx)
    if not policy.enabled:
        return _disabled_notice("kernel inspection")
    facts = kernel_facts()
    where = f" (inside {facts.container})" if facts.container else ""
    return ToolOutcome.success(
        f"{facts.distribution or 'linux'} kernel {facts.kernel_release} "
        f"on {facts.architecture}{where}",
        **facts.as_dict(),
    )


async def _resources(args: NoArgs, ctx: ToolContext) -> ToolOutcome:
    policy = _policy(ctx)
    if not policy.enabled:
        return _disabled_notice("resource inspection")
    snapshot = resource_snapshot()
    used = snapshot.memory_used_percent
    load = snapshot.load_average[0] if snapshot.load_average else None
    return ToolOutcome.success(
        f"load {load if load is not None else '?'}, "
        f"memory {used if used is not None else '?'}% used, "
        f"{snapshot.process_count} processes",
        **snapshot.as_dict(),
    )


async def _processes(args: ProcessArgs, ctx: ToolContext) -> ToolOutcome:
    policy = _policy(ctx)
    if not policy.enabled:
        return _disabled_notice("process inspection")
    rows = list_processes(limit=args.limit, sort_by=args.sort_by)
    if not rows:
        return ToolOutcome.failure("/proc is not readable on this host")
    return ToolOutcome.success(f"{len(rows)} processes by {args.sort_by}", processes=rows)


async def _modules(args: NoArgs, ctx: ToolContext) -> ToolOutcome:
    policy = _policy(ctx)
    if not policy.enabled:
        return _disabled_notice("module inspection")
    modules = loaded_modules()
    if not modules:
        return ToolOutcome.failure(
            "no loadable modules reported; this kernel may be monolithic, "
            "or /proc/modules is restricted in this container"
        )
    return ToolOutcome.success(f"{len(modules)} kernel modules loaded", modules=modules)


async def _kernel_log(args: KernelLogArgs, ctx: ToolContext) -> ToolOutcome:
    policy = _policy(ctx)
    if not policy.enabled:
        return _disabled_notice("kernel log access")
    ok, text = await read_kernel_log(args.lines)
    if not ok:
        # Distinguishing "denied" from "empty" matters: an empty ring buffer is
        # a finding, a denied read is not.
        return ToolOutcome.failure(text)
    return ToolOutcome.success(f"last {args.lines} kernel log lines", log=text)


async def _sysctl_read(args: SysctlReadArgs, ctx: ToolContext) -> ToolOutcome:
    policy = _policy(ctx)
    if not policy.enabled:
        return _disabled_notice("sysctl inspection")
    exact = read_sysctl(args.key)
    if exact is not None:
        return ToolOutcome.success(f"{args.key} = {exact}", values={args.key: exact})
    group = read_sysctl_group(args.key)
    if not group:
        return ToolOutcome.failure(
            f"no readable sysctl at {args.key!r}; check the key or try a shorter prefix"
        )
    return ToolOutcome.success(f"{len(group)} parameters under {args.key}", values=group)


async def _namespaces(args: NamespaceArgs, ctx: ToolContext) -> ToolOutcome:
    policy = _policy(ctx)
    if not policy.enabled:
        return _disabled_notice("namespace inspection")
    namespaces = list_namespaces(args.pid)
    if not namespaces:
        return ToolOutcome.failure(f"namespaces for pid {args.pid} are not readable")
    return ToolOutcome.success(
        f"{len(namespaces)} namespaces for pid {args.pid}",
        namespaces=namespaces,
        cgroups=cgroup_summary(),
    )


async def _sockets(args: NoArgs, ctx: ToolContext) -> ToolOutcome:
    policy = _policy(ctx)
    if not policy.enabled:
        return _disabled_notice("socket inspection")
    sockets = await listening_sockets()
    return ToolOutcome.success(f"{len(sockets)} listening TCP sockets", sockets=sockets)


# -------------------------------------------------------------- mutate tools


async def _sysctl_write(args: SysctlWriteArgs, ctx: ToolContext) -> ToolOutcome:
    try:
        result = await write_sysctl(_policy(ctx), args.key, args.value)
    except UnsafeOperationError as exc:
        return ToolOutcome.failure(str(exc))
    return ToolOutcome(ok=result.ok, summary=result.summary, data=result.detail)


async def _signal(args: SignalArgs, ctx: ToolContext) -> ToolOutcome:
    try:
        result = await send_signal(_policy(ctx), args.pid, args.signal)
    except UnsafeOperationError as exc:
        return ToolOutcome.failure(str(exc))
    return ToolOutcome(ok=result.ok, summary=result.summary, data=result.detail)


async def _service(args: ServiceArgs, ctx: ToolContext) -> ToolOutcome:
    try:
        result = await manage_service(_policy(ctx), args.unit, args.action)
    except UnsafeOperationError as exc:
        return ToolOutcome.failure(str(exc))
    return ToolOutcome(ok=result.ok, summary=result.summary, data=result.detail)


def register_system_tools(registry: ToolRegistry) -> None:
    """Register kernel/host tools. Registration is unconditional; the policy
    decides at call time, so ``/v1/admin/tools`` always shows the full surface
    and an operator can see what enabling the capability would grant."""
    read_tools = [
        (
            "system.kernel",
            "Kernel release, distribution, boot cmdline, and container context.",
            NoArgs,
            _kernel_info,
        ),
        (
            "system.resources",
            "Load average, memory, file descriptors, and disk usage.",
            NoArgs,
            _resources,
        ),
        (
            "system.processes",
            "Process table read from /proc, sorted by memory or pid.",
            ProcessArgs,
            _processes,
        ),
        (
            "system.modules",
            "Loaded kernel modules and their dependents (read-only).",
            NoArgs,
            _modules,
        ),
        ("system.kernel_log", "Recent kernel ring-buffer messages.", KernelLogArgs, _kernel_log),
        ("system.sysctl", "Read kernel parameters by key or prefix.", SysctlReadArgs, _sysctl_read),
        (
            "system.namespaces",
            "Namespace and cgroup context for a process.",
            NamespaceArgs,
            _namespaces,
        ),
        ("system.sockets", "TCP sockets currently in the LISTEN state.", NoArgs, _sockets),
    ]
    for name, description, model, fn in read_tools:
        registry.register(FunctionTool(ToolSpec(name, description, model), fn))

    registry.register(
        FunctionTool(
            ToolSpec(
                name="system.sysctl_set",
                description=(
                    "Change a kernel parameter. Reversible tunables only; the previous "
                    "value is returned so the change can be rolled back."
                ),
                params_model=SysctlWriteArgs,
                requires_approval=True,
            ),
            _sysctl_write,
        )
    )
    registry.register(
        FunctionTool(
            ToolSpec(
                name="system.signal",
                description="Send a signal to a process (TERM, HUP, KILL, STOP, CONT).",
                params_model=SignalArgs,
                requires_approval=True,
            ),
            _signal,
        )
    )
    registry.register(
        FunctionTool(
            ToolSpec(
                name="system.service",
                description=(
                    "Inspect or control a systemd unit. Status checks are immediate; "
                    "start/stop/restart require approval."
                ),
                params_model=ServiceArgs,
                requires_approval=True,
                timeout_seconds=90,
            ),
            _service,
        )
    )
