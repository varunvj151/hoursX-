"""The privilege envelope for kernel and host operations.

These are the tests that matter most in this package: they assert the boundary
holds, not that the happy path works.
"""

import os
import signal

import pytest

from hoursx.system.ops import load_kernel_module, send_signal, unload_kernel_module, write_sysctl
from hoursx.system.privileges import (
    OperationClass,
    SystemPolicy,
    UnsafeOperationError,
    classify_service,
    classify_signal,
    classify_sysctl,
)

# --------------------------------------------------------------------- sysctl


@pytest.mark.parametrize(
    "key",
    [
        "vm.swappiness",
        "vm.dirty_ratio",
        "net.core.somaxconn",
        "net.ipv4.tcp_fin_timeout",
        "fs.file-max",
        "kernel.pid_max",
    ],
)
def test_reversible_tunables_are_mutable(key):
    assert classify_sysctl(key)[0] is OperationClass.MUTATE


@pytest.mark.parametrize(
    "key",
    [
        "kernel.core_pattern",  # kernel executes this on crash — root escalation
        "kernel.modules_disabled",  # one-way, needs a reboot to undo
        "kernel.randomize_va_space",  # disabling ASLR removes a core mitigation
        "kernel.sysrq",  # direct kernel commands including reboot
        "kernel.kptr_restrict",  # leaks kernel pointers, defeats KASLR
        "kernel.unprivileged_bpf_disabled",
        "kernel.dmesg_restrict",
        "kernel.ftrace_enabled",
    ],
)
def test_security_critical_sysctls_are_refused(key):
    classification, reason = classify_sysctl(key)
    assert classification is OperationClass.REFUSED
    assert reason  # a refusal must always explain itself


def test_unknown_sysctl_is_refused_by_default():
    """Deny-by-default: an unreviewed key is not silently writable."""
    classification, reason = classify_sysctl("some.invented.parameter")
    assert classification is OperationClass.REFUSED
    assert "allowlist" in reason


def test_operator_allowlist_can_extend_mutable_keys():
    policy = SystemPolicy(
        enabled=True,
        allow_mutations=True,
        extra_sysctl_allowlist=frozenset({"net.ipv6.conf.all.forwarding"}),
    )
    assert policy.classify_sysctl_write("net.ipv6.conf.all.forwarding")[0] is OperationClass.MUTATE


def test_allowlist_cannot_override_a_refusal_for_unrelated_keys():
    policy = SystemPolicy(enabled=True, allow_mutations=True)
    assert policy.classify_sysctl_write("kernel.core_pattern")[0] is OperationClass.REFUSED


# -------------------------------------------------------------------- signals

_SIGKILL = getattr(signal, "SIGKILL", 9)
_SIGHUP = getattr(signal, "SIGHUP", 1)
_SIGUSR1 = getattr(signal, "SIGUSR1", 10)
_SIGCONT = getattr(signal, "SIGCONT", 18)
_SIGTERM = getattr(signal, "SIGTERM", 15)


def test_init_process_is_protected():
    classification, reason = classify_signal(1, _SIGKILL, own_pid=4242)
    assert classification is OperationClass.REFUSED
    assert "halts the host" in reason


def test_kernel_pid_zero_is_protected():
    assert classify_signal(0, _SIGTERM, own_pid=4242)[0] is OperationClass.REFUSED


def test_agent_cannot_signal_itself():
    classification, reason = classify_signal(4242, _SIGTERM, own_pid=4242)
    assert classification is OperationClass.REFUSED
    assert "abandon this run" in reason


def test_process_group_signals_are_refused():
    """Negative pids fan out to a whole group — too broad to approve safely."""
    assert classify_signal(-1, _SIGTERM, own_pid=4242)[0] is OperationClass.REFUSED


@pytest.mark.parametrize(
    "sig", [_SIGTERM, _SIGHUP, _SIGKILL, _SIGUSR1, _SIGCONT]
)
def test_ordinary_signals_to_ordinary_processes_are_gated_not_refused(sig):
    assert classify_signal(9999, sig, own_pid=4242)[0] is OperationClass.MUTATE


def test_unknown_signal_number_is_refused():
    assert classify_signal(9999, 4242, own_pid=1)[0] is OperationClass.REFUSED


# ------------------------------------------------------------------- services


@pytest.mark.parametrize("action", ["status", "is-active", "is-enabled", "show", "cat"])
def test_service_inspection_is_read_only(action):
    assert classify_service("nginx", action)[0] is OperationClass.READ


@pytest.mark.parametrize("action", ["start", "stop", "restart", "reload", "enable", "disable"])
def test_service_changes_require_approval(action):
    assert classify_service("nginx", action)[0] is OperationClass.MUTATE


@pytest.mark.parametrize("unit", ["sshd", "ssh", "systemd-journald", "NetworkManager", "dbus"])
def test_stopping_access_critical_services_is_refused(unit):
    """These carry the operator's own route back into the machine."""
    classification, reason = classify_service(unit, "stop")
    assert classification is OperationClass.REFUSED
    assert "unreachable" in reason or "access" in reason


def test_critical_services_can_still_be_inspected():
    assert classify_service("sshd", "status")[0] is OperationClass.READ


def test_critical_service_suffix_is_normalised():
    assert classify_service("sshd.service", "stop")[0] is OperationClass.REFUSED


@pytest.mark.parametrize("unit", ["nginx; rm -rf /", "../../etc/passwd", "unit name", ""])
def test_malformed_unit_names_are_refused(unit):
    assert classify_service(unit, "status")[0] is OperationClass.REFUSED


def test_unsupported_service_action_is_refused():
    assert classify_service("nginx", "mask")[0] is OperationClass.REFUSED


# ------------------------------------------------------------ kernel modules


def test_module_load_is_refused_unconditionally():
    """The one capability approval cannot make recoverable."""
    with pytest.raises(UnsafeOperationError) as caught:
        load_kernel_module("evil.ko")
    assert "kernel space" in str(caught.value)
    assert "no rollback" in str(caught.value)


def test_module_unload_is_refused_unconditionally():
    with pytest.raises(UnsafeOperationError):
        unload_kernel_module("some_module")


def test_module_refusal_names_the_alternative():
    with pytest.raises(UnsafeOperationError) as caught:
        load_kernel_module("x")
    assert "system.modules" in caught.value.alternative


# --------------------------------------------------------------------- policy


def test_system_operations_are_off_by_default():
    policy = SystemPolicy()
    assert not policy.enabled and not policy.allow_mutations


async def test_disabled_policy_blocks_every_mutation():
    policy = SystemPolicy(enabled=False)
    with pytest.raises(UnsafeOperationError) as caught:
        await write_sysctl(policy, "vm.swappiness", "10")
    assert "disabled on this deployment" in str(caught.value)
    assert "HOURSX_SYSTEM_OPS" in str(caught.value)


async def test_read_only_deployment_blocks_writes():
    """Enabling inspection must not implicitly enable modification."""
    policy = SystemPolicy(enabled=True, allow_mutations=False)
    with pytest.raises(UnsafeOperationError) as caught:
        await write_sysctl(policy, "vm.swappiness", "10")
    assert "read-only" in str(caught.value)


async def test_refused_sysctl_is_rejected_even_when_fully_enabled():
    policy = SystemPolicy(enabled=True, allow_mutations=True)
    with pytest.raises(UnsafeOperationError):
        await write_sysctl(policy, "kernel.core_pattern", "|/tmp/payload")


async def test_signal_to_self_refused_even_when_fully_enabled():
    policy = SystemPolicy(enabled=True, allow_mutations=True)
    with pytest.raises(UnsafeOperationError):
        await send_signal(policy, os.getpid(), _SIGTERM)


async def test_signal_to_init_refused_even_when_fully_enabled():
    policy = SystemPolicy(enabled=True, allow_mutations=True)
    with pytest.raises(UnsafeOperationError):
        await send_signal(policy, 1, _SIGKILL)


async def test_signalling_a_dead_process_reports_cleanly(tmp_path):
    """A missing target is a normal outcome, not an exception."""
    policy = SystemPolicy(enabled=True, allow_mutations=True)
    # A pid that is essentially certain not to exist.
    result = await send_signal(policy, 4_000_000, _SIGTERM)
    assert not result.ok
    assert "no process" in result.summary or "permission denied" in result.summary


def test_unsafe_error_carries_operation_and_reason():
    error = UnsafeOperationError("do thing", "because reasons", "try this instead")
    assert error.operation == "do thing"
    assert error.reason == "because reasons"
    assert "try this instead" in str(error)
