"""Kernel introspection and the tool surface that exposes it."""

from pathlib import Path

import pytest

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
    sysctl_path,
)
from hoursx.tools.base import ToolContext, ToolInvocation
from hoursx.tools.builtin import register_builtin_tools
from hoursx.tools.executor import ToolExecutor
from hoursx.tools.registry import ToolRegistry

LINUX_ONLY = pytest.mark.skipif(
    not Path("/proc").is_dir(), reason="requires a Linux /proc filesystem"
)


# ------------------------------------------------------------------- probing


@LINUX_ONLY
def test_kernel_facts_report_a_real_kernel():
    facts = kernel_facts()
    assert facts.kernel_release
    assert facts.architecture
    assert facts.cpu_count >= 1


@LINUX_ONLY
def test_kernel_facts_serialise_without_empty_fields():
    data = kernel_facts().as_dict()
    assert "kernel_release" in data
    assert all(value not in ("", None, []) for value in data.values())


@LINUX_ONLY
def test_resource_snapshot_reports_memory_and_processes():
    snapshot = resource_snapshot()
    assert snapshot.process_count > 0
    assert snapshot.memory_total_kb and snapshot.memory_total_kb > 0
    assert 0.0 <= (snapshot.memory_used_percent or 0.0) <= 100.0


def test_memory_percent_is_none_without_data():
    from hoursx.system.probe import ResourceSnapshot

    assert ResourceSnapshot().memory_used_percent is None


@LINUX_ONLY
def test_process_listing_includes_this_process():
    import os

    pids = {row["pid"] for row in list_processes(limit=200, sort_by="pid")}
    assert os.getpid() in pids


@LINUX_ONLY
def test_process_listing_respects_its_limit():
    assert len(list_processes(limit=3)) <= 3


@LINUX_ONLY
def test_process_listing_sorts_by_memory_descending():
    rows = list_processes(limit=10, sort_by="rss")
    assert rows == sorted(rows, key=lambda row: -row["rss_kb"])


@LINUX_ONLY
def test_module_listing_is_shaped_correctly():
    # A container may expose no modules; the shape still has to be right.
    for module in loaded_modules(limit=5):
        assert module["name"]
        assert isinstance(module["used_by"], list)


@LINUX_ONLY
def test_sysctl_read_returns_a_known_parameter():
    value = read_sysctl("kernel.ostype")
    assert value in (None, "Linux")  # None if /proc/sys is restricted


@LINUX_ONLY
def test_sysctl_group_read_returns_multiple_keys():
    group = read_sysctl_group("kernel")
    if group:  # restricted environments legitimately return nothing
        assert all(key.startswith("kernel") for key in group)


@pytest.mark.parametrize("key", ["../../etc/passwd", "kernel/../../etc", "bad key", "a;b"])
def test_sysctl_path_rejects_traversal_and_injection(key):
    with pytest.raises(ValueError):
        sysctl_path(key)


def test_sysctl_read_returns_none_for_invalid_key():
    assert read_sysctl("../../etc/passwd") is None


@LINUX_ONLY
def test_namespaces_are_readable_for_self():
    import os

    namespaces = list_namespaces(os.getpid())
    assert "mnt" in namespaces or namespaces == {}


@LINUX_ONLY
async def test_kernel_log_reports_denial_rather_than_pretending_empty():
    """An empty ring buffer and a denied read are different findings."""
    ok, text = await read_kernel_log(5)
    assert isinstance(ok, bool)
    if not ok:
        assert "unavailable" in text


@LINUX_ONLY
async def test_listening_sockets_are_shaped_correctly():
    for socket in await listening_sockets(limit=5):
        assert socket["family"] in ("ipv4", "ipv6")
        assert 0 <= socket["port"] <= 65535


@LINUX_ONLY
def test_cgroup_summary_returns_a_mapping():
    assert isinstance(cgroup_summary(), dict)


# --------------------------------------------------------------------- tools


def _executor() -> ToolExecutor:
    registry = ToolRegistry()
    register_builtin_tools(registry)
    return ToolExecutor(registry)


def _ctx(tmp_path: Path, services=None) -> ToolContext:
    return ToolContext(
        workspace_id="w", session_id="s", run_id="r", sandbox_dir=tmp_path, services=services
    )


def _call(name: str, **arguments) -> ToolInvocation:
    return ToolInvocation(call_id="c", tool_name=name, arguments=arguments)


def test_system_tools_are_registered():
    registry = ToolRegistry()
    register_builtin_tools(registry)
    names = set(registry.names())
    assert {"system.kernel", "system.processes", "system.modules", "system.sysctl"} <= names
    assert {"system.sysctl_set", "system.signal", "system.service"} <= names


def test_mutating_system_tools_require_approval():
    registry = ToolRegistry()
    register_builtin_tools(registry)
    for name in ("system.sysctl_set", "system.signal", "system.service"):
        assert registry.get(name).spec.requires_approval, f"{name} must be gated"


def test_read_system_tools_do_not_require_approval():
    registry = ToolRegistry()
    register_builtin_tools(registry)
    for name in ("system.kernel", "system.processes", "system.sysctl", "system.modules"):
        assert not registry.get(name).spec.requires_approval


async def test_system_tools_report_clearly_when_disabled(tmp_path, services):
    """A disabled capability must read as a configuration choice, not a bug."""
    services.settings.system_ops_enabled = False
    outcome = await _executor().execute(
        _call("system.kernel"), _ctx(tmp_path, services), grants=["system.*"]
    )
    assert not outcome.ok
    assert "HOURSX_SYSTEM_OPS" in outcome.summary


@LINUX_ONLY
async def test_kernel_tool_returns_facts_when_enabled(tmp_path, services):
    services.settings.system_ops_enabled = True
    outcome = await _executor().execute(
        _call("system.kernel"), _ctx(tmp_path, services), grants=["system.*"]
    )
    assert outcome.ok
    assert outcome.data["kernel_release"]


@LINUX_ONLY
async def test_processes_tool_returns_rows_when_enabled(tmp_path, services):
    services.settings.system_ops_enabled = True
    outcome = await _executor().execute(
        _call("system.processes", limit=5), _ctx(tmp_path, services), grants=["system.*"]
    )
    assert outcome.ok and len(outcome.data["processes"]) <= 5


@LINUX_ONLY
async def test_resources_tool_returns_a_snapshot(tmp_path, services):
    services.settings.system_ops_enabled = True
    outcome = await _executor().execute(
        _call("system.resources"), _ctx(tmp_path, services), grants=["system.*"]
    )
    assert outcome.ok and outcome.data["process_count"] > 0


async def test_sysctl_set_refuses_a_dangerous_key_through_the_tool(tmp_path, services):
    services.settings.system_ops_enabled = True
    services.settings.system_mutations_enabled = True
    outcome = await _executor().execute(
        _call("system.sysctl_set", key="kernel.core_pattern", value="|/tmp/x"),
        _ctx(tmp_path, services),
        grants=["system.*"],
        approved=True,  # even with approval, refusal stands
    )
    assert not outcome.ok
    assert "refused" in outcome.summary


async def test_signal_tool_refuses_init_even_when_approved(tmp_path, services):
    services.settings.system_ops_enabled = True
    services.settings.system_mutations_enabled = True
    outcome = await _executor().execute(
        _call("system.signal", pid=1, signal=9),
        _ctx(tmp_path, services),
        grants=["system.*"],
        approved=True,
    )
    assert not outcome.ok and "refused" in outcome.summary


async def test_service_tool_refuses_stopping_sshd(tmp_path, services):
    services.settings.system_ops_enabled = True
    services.settings.system_mutations_enabled = True
    outcome = await _executor().execute(
        _call("system.service", unit="sshd", action="stop"),
        _ctx(tmp_path, services),
        grants=["system.*"],
        approved=True,
    )
    assert not outcome.ok and "refused" in outcome.summary


async def test_system_tools_are_invisible_without_a_grant(tmp_path, services):
    services.settings.system_ops_enabled = True
    outcome = await _executor().execute(
        _call("system.kernel"), _ctx(tmp_path, services), grants=["fs.*"]
    )
    assert not outcome.ok and "not available" in outcome.summary
