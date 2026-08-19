# Kernel and Host Operations

HoursX agents can inspect and, under human approval, adjust the machine they run
on. This document states exactly what that means, where the boundary is, and why
it is drawn there.

**The capability is off by default.** A fresh install grants no host access at
all. Enabling it is a deliberate operator decision, made twice: once for reading,
once for writing.

```bash
HOURSX_SYSTEM_OPS=true            # kernel and host inspection
HOURSX_SYSTEM_MUTATIONS=true      # allow approved changes (requires the above)
HOURSX_SYSTEM_SYSCTL_ALLOWLIST='["net.ipv6.conf.all.forwarding"]'
```

## The three operation classes

Every host operation is classified before it runs.

| Class | Meaning | Gate |
| --- | --- | --- |
| **READ** | Observation only | None — an agent diagnosing an incident should not need a human for every `/proc` read |
| **MUTATE** | Changes live state, reversibly and in scope | Human approval, per call |
| **REFUSED** | No approval makes it acceptable | Always denied, with a stated reason |

The refusal list is deliberately short and specific. A blanket ban would push
operators toward handing the agent raw `shell.run` as root — which is strictly
worse, because it discards classification, approval, and audit in one move.
Naming the genuinely unrecoverable operations and permitting the rest under
approval is what keeps the safe path also the convenient one.

## What agents can read

| Tool | Reads |
| --- | --- |
| `system.kernel` | Release, version, architecture, distribution, boot cmdline, LSMs, container context |
| `system.resources` | Load average, memory, swap, file descriptors, per-filesystem usage |
| `system.processes` | Process table from `/proc`, sorted by RSS or pid |
| `system.modules` | Loaded kernel modules and their dependents |
| `system.kernel_log` | Kernel ring buffer (`/dev/kmsg`, falling back to `dmesg`) |
| `system.sysctl` | Any readable kernel parameter, by key or prefix |
| `system.namespaces` | Namespace identities and cgroup v2 limits |
| `system.sockets` | TCP sockets in `LISTEN`, parsed from `/proc/net/tcp` |

These read the kernel's own interfaces rather than shelling out, so output is
parseable and does not depend on which userland tools happen to be installed.

Two details worth knowing:

- **Container context is reported deliberately.** A sysctl written inside a
  container may be namespaced or rejected outright, so the agent is told which
  world it is in before it reasons about "the host".
- **A denied read is distinguished from an empty result.** `kernel.dmesg_restrict`
  produces "unavailable", never a silent empty string — an empty ring buffer is a
  finding, a denied read is not, and conflating them produces confident wrong
  conclusions.

## What agents can change, with approval

| Tool | Changes | Guard rails |
| --- | --- | --- |
| `system.sysctl_set` | Kernel tunables | Reviewed allowlist only; the previous value is returned for exact rollback |
| `system.signal` | Sends a signal to a process | pid 0/1 and the agent's own process refused; process groups refused |
| `system.service` | systemd unit lifecycle | Units carrying operator access refused for stop/disable/restart |

Mutating tools set `requires_approval=True`, so the runtime checkpoints the run,
persists an approval request, and waits. The operator sees the tool name and its
exact arguments before deciding. Approval is per-call — approving one
`sysctl_set` does not grant a second.

### Sysctl allowlist

Mutable by default: `net.core.*`, `net.ipv4.tcp_*`, `net.ipv4.ip_local_port_range`,
`net.netfilter.nf_conntrack_max`, `vm.swappiness`, `vm.dirty_ratio`,
`vm.dirty_background_ratio`, `vm.vfs_cache_pressure`, `vm.max_map_count`,
`fs.file-max`, `fs.inotify.*`, `kernel.pid_max`.

Anything not listed is refused unless an operator adds it to
`HOURSX_SYSTEM_SYSCTL_ALLOWLIST`. Deny-by-default is the point: an unreviewed
parameter is not silently writable.

## What is refused outright

**Loading or unloading kernel modules.** This is the one capability the platform
declines rather than gates, and the reasoning is worth being explicit about:
kernel code has no sandbox, no rollback, and no error boundary. A bad argument
does not raise an exception — it panics the host or corrupts a filesystem.
Approval does not help, because an operator who approves `insmod` cannot
un-panic a kernel. Module *inventory* is readable through `system.modules`;
changing modules belongs in configuration management with a human at the console.

**Security-critical sysctls**, because writing them disables the protections
that make everything else survivable:

| Parameter | Why |
| --- | --- |
| `kernel.core_pattern` | Executed by the kernel on crash — a known root-escalation vector |
| `kernel.modules_disabled` | One-way switch; cannot be undone without a reboot |
| `kernel.randomize_va_space` | Disabling ASLR removes a core exploit mitigation |
| `kernel.sysrq` | Exposes direct kernel commands including immediate reboot |
| `kernel.kptr_restrict` | Leaks kernel pointers to userspace, defeats KASLR |
| `kernel.unprivileged_bpf_disabled` | Documented privilege-escalation surface |
| `kernel.dmesg_restrict` | Weakens kernel log access control |
| `kernel.ftrace_enabled` | Kernel tracing control is not agent-appropriate |

**Signals to pid 0 and 1**, which halt the machine; **signals to the agent's own
process**, which would abandon the run mid-flight; and **negative pids**, which
fan out to an entire process group.

**Stopping, disabling, or restarting** `sshd`, `ssh`, `systemd-journald`,
`systemd-logind`, `dbus`, `networking`, `systemd-networkd`, or `NetworkManager`
— these carry the operator's own route back into the machine. They remain
fully inspectable.

## Privilege in practice

The tools do not acquire privilege; they inherit the process's. Running the
worker as an unprivileged user means `sysctl_set` returns a clear
permission-denied outcome rather than succeeding. That is the intended default.

If you deliberately run with `CAP_SYS_ADMIN`, the classification above is what
stands between a model's suggestion and your kernel. Treat
`HOURSX_SYSTEM_MUTATIONS=true` on a privileged process as the highest-trust
configuration HoursX supports, and pair it with the audit log
(`GET /v1/admin/audit`), which records every approval decision and who made it.

## Verified changes

Prefer `change.sysctl` and `change.service` over the bare `system.*` mutations
wherever the agent can state what the change should achieve. Those tools apply
the same privilege checks, then hold the change to its declared post-conditions
and revert it automatically if they do not hold. See
[guarded change](guarded-change.md).

## Using it

From the terminal, without a server:

```bash
hoursx system probe -v                          # kernel, memory, disks, modules
hoursx system processes --sort rss --limit 20
hoursx system kernel-log --lines 100

hoursx agent run "why is memory pressure climbing on this host?"
hoursx approvals list
hoursx approvals approve <id>
```

From the desktop application, the **System** tab shows the same facts, and the
**Approvals** tab is where paused tool calls surface with their arguments.

## Testing the boundary

The privilege envelope is covered by tests that assert refusals hold even when
every flag is enabled and approval has been granted — because a boundary that is
only tested on the happy path is not a boundary:

```bash
pytest tests/test_system_privileges.py -q
```
