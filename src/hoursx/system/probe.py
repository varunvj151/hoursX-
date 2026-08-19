"""Kernel and host introspection.

Everything here is read-only and reads the kernel's own interfaces —
``/proc``, ``/sys``, the kernel ring buffer — rather than shelling out where a
file read will do. That keeps the output parseable and avoids depending on
which userland tools happen to be installed.

All functions degrade rather than raise: an unreadable interface yields an
absent field, because a diagnosis that returns partial facts is far more useful
than one that aborts on the first restricted path.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROC = Path("/proc")
SYS = Path("/sys")


def _read_text(path: Path, limit: int = 1_000_000) -> str | None:
    """Read a kernel interface file, returning None when it is unavailable."""
    try:
        with path.open("r", errors="replace") as handle:
            return handle.read(limit)
    except (OSError, PermissionError, UnicodeDecodeError):
        return None


def _read_int(path: Path) -> int | None:
    raw = _read_text(path, 64)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


# ------------------------------------------------------------------- kernel


@dataclass
class KernelFacts:
    kernel_release: str = ""
    kernel_version: str = ""
    architecture: str = ""
    hostname: str = ""
    distribution: str = ""
    uptime_seconds: float | None = None
    boot_command_line: str = ""
    cpu_count: int = 0
    page_size: int = 0
    container: str | None = None
    security_modules: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items() if value not in ("", None, [])}


def _detect_container() -> str | None:
    """Identify the containment context, since it bounds what any change means.

    A sysctl written inside a container may be namespaced (and invisible to the
    host) or rejected outright — the agent should know which world it is in
    before reasoning about the host.
    """
    if Path("/.dockerenv").exists():
        return "docker"
    cgroup = _read_text(PROC / "1" / "cgroup", 8192) or ""
    for marker in ("kubepods", "docker", "containerd", "lxc", "podman"):
        if marker in cgroup:
            return marker
    if (sched := _read_text(PROC / "1" / "sched", 256)) and not sched.startswith(
        ("systemd", "init")
    ):
        return "unknown-container"
    return None


def _detect_distribution() -> str:
    release = _read_text(Path("/etc/os-release"), 8192)
    if not release:
        return ""
    for line in release.splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.partition("=")[2].strip().strip('"')
    return ""


def _security_modules() -> list[str]:
    raw = _read_text(SYS / "kernel" / "security" / "lsm", 512)
    return [module for module in (raw or "").strip().split(",") if module]


def kernel_facts() -> KernelFacts:
    """Snapshot of kernel identity and boot configuration."""
    uname = platform.uname()
    uptime_raw = _read_text(PROC / "uptime", 128)
    uptime = None
    if uptime_raw:
        try:
            uptime = float(uptime_raw.split()[0])
        except (ValueError, IndexError):
            uptime = None
    return KernelFacts(
        kernel_release=uname.release,
        kernel_version=(_read_text(PROC / "version", 1024) or uname.version).strip(),
        architecture=uname.machine,
        hostname=uname.node,
        distribution=_detect_distribution(),
        uptime_seconds=uptime,
        boot_command_line=(_read_text(PROC / "cmdline", 4096) or "").strip(),
        cpu_count=os.cpu_count() or 0,
        page_size=os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 0,
        container=_detect_container(),
        security_modules=_security_modules(),
    )


# ----------------------------------------------------------------- resources


@dataclass
class ResourceSnapshot:
    load_average: tuple[float, float, float] | None = None
    memory_total_kb: int | None = None
    memory_available_kb: int | None = None
    swap_total_kb: int | None = None
    swap_free_kb: int | None = None
    process_count: int = 0
    open_file_descriptors: int | None = None
    file_descriptor_limit: int | None = None
    disks: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items() if value not in ("", None, [])}

    @property
    def memory_used_percent(self) -> float | None:
        if not self.memory_total_kb or self.memory_available_kb is None:
            return None
        used = self.memory_total_kb - self.memory_available_kb
        return round(100.0 * used / self.memory_total_kb, 1)


def _meminfo() -> dict[str, int]:
    raw = _read_text(PROC / "meminfo", 16384)
    if not raw:
        return {}
    values: dict[str, int] = {}
    for line in raw.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0])
    return values


def _disk_usage() -> list[dict[str, Any]]:
    """Usage for real mounted filesystems, skipping virtual ones."""
    mounts = _read_text(PROC / "mounts", 65536) or ""
    seen: set[str] = set()
    disks: list[dict[str, Any]] = []
    virtual = {
        "proc",
        "sysfs",
        "devtmpfs",
        "devpts",
        "tmpfs",
        "cgroup",
        "cgroup2",
        "securityfs",
        "pstore",
        "bpf",
        "debugfs",
        "tracefs",
        "mqueue",
        "hugetlbfs",
        "configfs",
        "fusectl",
        "binfmt_misc",
        "overlay",
        "squashfs",
    }
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mountpoint, fstype = parts[0], parts[1], parts[2]
        if fstype in virtual or mountpoint in seen:
            continue
        seen.add(mountpoint)
        try:
            usage = shutil.disk_usage(mountpoint)
        except (OSError, PermissionError):
            continue
        disks.append(
            {
                "mountpoint": mountpoint,
                "device": device,
                "fstype": fstype,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "used_percent": round(100.0 * usage.used / usage.total, 1) if usage.total else 0.0,
            }
        )
    return disks[:40]


def resource_snapshot() -> ResourceSnapshot:
    """Current CPU load, memory, descriptors, and filesystem usage."""
    memory = _meminfo()
    try:
        load = os.getloadavg()
    except (OSError, AttributeError):
        load = None

    fd_allocated = None
    if raw := _read_text(PROC / "sys" / "fs" / "file-nr", 128):
        parts = raw.split()
        if parts and parts[0].isdigit():
            fd_allocated = int(parts[0])

    return ResourceSnapshot(
        load_average=load,
        memory_total_kb=memory.get("MemTotal"),
        memory_available_kb=memory.get("MemAvailable"),
        swap_total_kb=memory.get("SwapTotal"),
        swap_free_kb=memory.get("SwapFree"),
        process_count=len([p for p in PROC.iterdir() if p.name.isdigit()]) if PROC.is_dir() else 0,
        open_file_descriptors=fd_allocated,
        file_descriptor_limit=_read_int(PROC / "sys" / "fs" / "file-max"),
        disks=_disk_usage(),
    )


# ----------------------------------------------------------------- processes


def list_processes(limit: int = 50, sort_by: str = "rss") -> list[dict[str, Any]]:
    """Process table read straight from ``/proc``, heaviest first.

    ``sort_by`` is ``rss`` (memory) or ``pid``. Reading ``/proc`` directly means
    this works identically whether or not ``ps`` is installed.
    """
    if not PROC.is_dir():
        return []
    processes: list[dict[str, Any]] = []
    page_kb = (os.sysconf("SC_PAGE_SIZE") // 1024) if hasattr(os, "sysconf") else 4

    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        status = _read_text(entry / "status", 8192)
        if status is None:
            continue  # process exited between listing and reading
        fields = {}
        for line in status.splitlines():
            key, _, value = line.partition(":")
            fields[key] = value.strip()
        statm = (_read_text(entry / "statm", 256) or "").split()
        rss_kb = int(statm[1]) * page_kb if len(statm) > 1 and statm[1].isdigit() else 0
        cmdline = (_read_text(entry / "cmdline", 4096) or "").replace("\0", " ").strip()
        processes.append(
            {
                "pid": int(entry.name),
                "name": fields.get("Name", ""),
                "state": fields.get("State", ""),
                "ppid": int(fields.get("PPid", 0) or 0),
                "threads": int(fields.get("Threads", 0) or 0),
                "rss_kb": rss_kb,
                "uid": (fields.get("Uid", "").split() or [""])[0],
                "cmdline": cmdline[:400] or f"[{fields.get('Name', '')}]",
            }
        )

    key = (lambda p: p["pid"]) if sort_by == "pid" else (lambda p: -p["rss_kb"])
    processes.sort(key=key)
    return processes[: max(1, limit)]


# ------------------------------------------------------------------- modules


def loaded_modules(limit: int = 200) -> list[dict[str, Any]]:
    """Loaded kernel modules, parsed from ``/proc/modules`` (read-only)."""
    raw = _read_text(PROC / "modules", 262144)
    if not raw:
        return []
    modules: list[dict[str, Any]] = []
    for line in raw.splitlines()[:limit]:
        parts = line.split()
        if len(parts) < 4:
            continue
        used_by = parts[3].rstrip(",")
        modules.append(
            {
                "name": parts[0],
                "size_bytes": int(parts[1]) if parts[1].isdigit() else 0,
                "use_count": int(parts[2]) if parts[2].isdigit() else 0,
                "used_by": [] if used_by == "-" else used_by.split(","),
            }
        )
    return modules


# -------------------------------------------------------------------- sysctl


_SYSCTL_KEY_RE = re.compile(r"^[a-z0-9_.\-]+$", re.IGNORECASE)


def sysctl_path(key: str) -> Path:
    """Map a dotted sysctl key to its ``/proc/sys`` path, rejecting traversal."""
    if not _SYSCTL_KEY_RE.match(key) or ".." in key:
        raise ValueError(f"invalid sysctl key {key!r}")
    return PROC / "sys" / Path(*key.split("."))


def read_sysctl(key: str) -> str | None:
    """Read one kernel parameter."""
    try:
        return (_read_text(sysctl_path(key), 8192) or "").strip() or None
    except ValueError:
        return None


def read_sysctl_group(prefix: str, limit: int = 200) -> dict[str, str]:
    """Read every parameter under a dotted prefix (e.g. ``net.ipv4.tcp``)."""
    try:
        root = sysctl_path(prefix)
    except ValueError:
        return {}
    if not root.exists():
        return {}
    if root.is_file():
        value = read_sysctl(prefix)
        return {prefix: value} if value is not None else {}

    values: dict[str, str] = {}
    base = PROC / "sys"
    for path in sorted(root.rglob("*")):
        if not path.is_file() or len(values) >= limit:
            continue
        content = _read_text(path, 4096)
        if content is None:
            continue  # write-only or restricted parameter
        values[".".join(path.relative_to(base).parts)] = content.strip()
    return values


# ---------------------------------------------------------------- namespaces


def list_namespaces(pid: int = 1) -> dict[str, str]:
    """Namespace identities for a process — how isolated this context is."""
    namespaces: dict[str, str] = {}
    ns_dir = PROC / str(pid) / "ns"
    try:
        entries = sorted(ns_dir.iterdir())
    except (OSError, PermissionError):
        return namespaces
    for entry in entries:
        try:
            namespaces[entry.name] = os.readlink(entry)
        except (OSError, PermissionError):
            continue
    return namespaces


def cgroup_summary() -> dict[str, Any]:
    """cgroup v2 limits that actually bound this process's resources."""
    summary: dict[str, Any] = {}
    controllers = _read_text(SYS / "fs" / "cgroup" / "cgroup.controllers", 1024)
    if controllers:
        summary["controllers"] = controllers.split()
    for label, filename in (
        ("memory_max", "memory.max"),
        ("memory_current", "memory.current"),
        ("cpu_max", "cpu.max"),
        ("pids_max", "pids.max"),
    ):
        value = _read_text(SYS / "fs" / "cgroup" / filename, 128)
        if value:
            summary[label] = value.strip()
    summary["self_cgroup"] = (_read_text(PROC / "self" / "cgroup", 4096) or "").strip()
    return {key: value for key, value in summary.items() if value}


# ---------------------------------------------------------------- kernel log


async def read_kernel_log(lines: int = 80) -> tuple[bool, str]:
    """Tail the kernel ring buffer.

    Prefers ``/dev/kmsg`` (no external tool) and falls back to ``dmesg``. Access
    is commonly restricted (``kernel.dmesg_restrict``) or unavailable inside a
    container, so the boolean says whether the read actually succeeded — a
    silent empty string would read as "the kernel logged nothing", which is a
    very different and misleading conclusion.
    """
    kmsg = Path("/dev/kmsg")
    if kmsg.exists() and os.access(kmsg, os.R_OK):
        collected: list[str] = []
        try:
            # Non-blocking: /dev/kmsg blocks at EOF waiting for new records.
            fd = os.open(kmsg, os.O_RDONLY | os.O_NONBLOCK)
            try:
                while len(collected) < lines:
                    try:
                        chunk = os.read(fd, 8192)
                    except BlockingIOError:
                        break
                    if not chunk:
                        break
                    collected.append(chunk.decode(errors="replace").strip())
            finally:
                os.close(fd)
        except OSError:
            collected = []
        if collected:
            return True, "\n".join(collected[-lines:])

    if shutil.which("dmesg") is None:
        return False, "kernel log unavailable: /dev/kmsg not readable and dmesg not installed"
    try:
        process = await asyncio.create_subprocess_exec(
            "dmesg",
            "--ctime",
            "--color=never",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
    except (TimeoutError, OSError) as exc:
        return False, f"kernel log unavailable: {exc}"
    if process.returncode != 0:
        return False, (
            "kernel log unavailable: dmesg was denied "
            "(kernel.dmesg_restrict is likely set, or this is an unprivileged container)"
        )
    text = stdout.decode(errors="replace")
    return True, "\n".join(text.splitlines()[-lines:])


# ------------------------------------------------------------------- sockets


async def listening_sockets(limit: int = 100) -> list[dict[str, Any]]:
    """Listening TCP sockets parsed from ``/proc/net/tcp``.

    Hex-encoded kernel format rather than ``ss`` output: no tool dependency and
    a stable format across distributions.
    """
    sockets: list[dict[str, Any]] = []
    for proc_file, family in ((PROC / "net" / "tcp", 4), (PROC / "net" / "tcp6", 6)):
        raw = _read_text(proc_file, 262144)
        if not raw:
            continue
        for line in raw.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 4 or parts[3] != "0A":  # 0A == TCP_LISTEN
                continue
            local = parts[1]
            address, _, port_hex = local.partition(":")
            try:
                port = int(port_hex, 16)
            except ValueError:
                continue
            sockets.append(
                {
                    "family": f"ipv{family}",
                    "port": port,
                    "address_hex": address,
                    "inode": parts[9] if len(parts) > 9 else "",
                }
            )
            if len(sockets) >= limit:
                return sockets
    return sockets
