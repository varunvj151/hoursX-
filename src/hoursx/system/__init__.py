"""Kernel and host operations.

This package gives an agent real visibility into — and bounded control over —
the machine it runs on: the kernel ring buffer, loaded modules, sysctl
parameters, cgroups, namespaces, processes, services, and sockets.

The capability is **off by default** (``HOURSX_SYSTEM_OPS``). When enabled it is
governed by :mod:`hoursx.system.privileges`, which sorts every operation into
one of three classes: freely readable, mutation requiring human approval, or
refused outright.
"""

from hoursx.system.privileges import (
    OperationClass,
    SystemPolicy,
    UnsafeOperationError,
    classify_signal,
    classify_sysctl,
)
from hoursx.system.probe import (
    KernelFacts,
    ResourceSnapshot,
    kernel_facts,
    list_namespaces,
    list_processes,
    loaded_modules,
    read_kernel_log,
    read_sysctl,
    resource_snapshot,
)

__all__ = [
    "KernelFacts",
    "OperationClass",
    "ResourceSnapshot",
    "SystemPolicy",
    "UnsafeOperationError",
    "classify_signal",
    "classify_sysctl",
    "kernel_facts",
    "list_namespaces",
    "list_processes",
    "loaded_modules",
    "read_kernel_log",
    "read_sysctl",
    "resource_snapshot",
]
