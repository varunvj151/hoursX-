# hoursx-sysd — privileged system helper

A small C++ daemon that performs privileged host operations on behalf of the
Python agent, so that **the agent itself never needs privilege**.

## Why this exists

Without it, giving HoursX the ability to tune a kernel parameter means running
the whole Python process with `CAP_SYS_ADMIN` — a process that also embeds a
language model, makes outbound network calls, executes plugin code, and runs
shell commands on the model's behalf. That is a very large amount of surface to
hold a capability.

`hoursx-sysd` inverts that. The daemon holds the capability; the agent runs as
an unprivileged user and asks over a Unix socket. The privileged surface shrinks
from a large Python application to roughly six hundred lines of C++ that does
nothing but parse a fixed grammar and execute four operations.

This is the same reasoning behind `sudo`, `polkit`, and every well-behaved
setuid helper: privilege belongs in the smallest reviewable component, not in
the largest one.

## The daemon does not trust its caller

The Python layer already classifies every operation into read, approved
mutation, or refusal. The daemon **re-implements that classification
independently** and refuses on its own authority.

That duplication is deliberate. If the Python process is compromised — through
prompt injection, a malicious plugin, or a dependency — it can send whatever it
likes down the socket. The daemon is the component that must not be persuaded.
A refusal that only exists in the caller is not a control.

Concretely: an attacker with full control of the Python process still cannot
write `kernel.core_pattern`, signal PID 1, or stop `sshd` through this daemon.

## Protocol

Line-delimited, over `AF_UNIX` `SOCK_STREAM`. Fields are space-separated and
base64-encoded.

```
Request:   VERB <b64-arg> [<b64-arg>...]
Response:  OK <b64-payload>
           ERR <b64-reason>
```

Base64 rather than JSON is a security choice, not a stylistic one. It removes
quoting, escaping, and injection concerns entirely, and the parser is short
enough to read in one sitting — which matters a great deal in the one component
that holds privilege.

| Verb | Arguments | Effect |
| --- | --- | --- |
| `PING` | — | Liveness and version |
| `SYSCTL_GET` | key | Read a kernel parameter |
| `SYSCTL_SET` | key, value | Write an allowlisted parameter |
| `SIGNAL` | pid, signal number | Signal a non-protected process |
| `SERVICE` | unit, action | systemd unit action |

## Building

```bash
cmake -S native -B native/build -DCMAKE_BUILD_TYPE=Release
cmake --build native/build
```

Produces `native/build/hoursx-sysd`. No third-party dependencies; C++20 and the
POSIX system interfaces only.

## Running

```bash
sudo ./hoursx-sysd --socket /run/hoursx/sysd.sock --group hoursx
```

The socket is created `0660`, owned by root and the named group. Only members of
that group can connect, so access is controlled by ordinary Unix group
membership rather than anything the daemon invents.

Enable it from the Python side with:

```bash
HOURSX_SYSTEM_BACKEND=helper
HOURSX_SYSD_SOCKET=/run/hoursx/sysd.sock
```

With `HOURSX_SYSTEM_BACKEND=direct` (the default) the Python process performs
the operations itself and needs the privilege accordingly.

## Deployment shape

```
hoursx-sysd    root, CAP_SYS_ADMIN + CAP_KILL      ~600 lines of C++
     ^
     | AF_UNIX, group-restricted
     |
hoursx worker  uid hoursx, no capabilities         the whole agent
```

Every privileged operation is written to syslog with its arguments and outcome
before the reply is sent, so the audit record survives even if the caller
discards the response.
