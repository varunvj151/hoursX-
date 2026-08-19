#include "policy.hpp"

#include <algorithm>
#include <array>
#include <csignal>
#include <string>
#include <string_view>

namespace hoursx {
namespace {

// Reversible tunables an operator would plausibly ask an agent to adjust while
// diagnosing: network backlogs, connection tracking, VM pressure, file handles.
constexpr std::array<std::string_view, 13> kWritablePrefixes{
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
    "net.ipv4.ip_forward",
};

// Writing any of these disables a protection that makes everything else
// survivable, or hands over kernel-mode execution outright.
struct RefusedKey {
    std::string_view key;
    std::string_view reason;
};

constexpr std::array<RefusedKey, 8> kRefusedKeys{{
    {"kernel.core_pattern",
     "executed by the kernel on crash; a known root-escalation vector"},
    {"kernel.modules_disabled",
     "one-way switch that cannot be undone without a reboot"},
    {"kernel.randomize_va_space", "disabling ASLR removes a core exploit mitigation"},
    {"kernel.sysrq", "exposes direct kernel commands including immediate reboot"},
    {"kernel.kptr_restrict", "leaks kernel pointers to userspace and defeats KASLR"},
    {"kernel.unprivileged_bpf_disabled",
     "re-enabling unprivileged BPF is a documented privilege-escalation surface"},
    {"kernel.dmesg_restrict", "weakens kernel log access control"},
    {"kernel.ftrace_enabled", "kernel tracing control is not agent-appropriate"},
}};

// Signals an operator legitimately sends while managing a misbehaving process.
constexpr std::array<int, 8> kAllowedSignals{
    SIGTERM, SIGINT, SIGHUP, SIGUSR1, SIGUSR2, SIGKILL, SIGSTOP, SIGCONT,
};

// Stopping any of these removes the operator's own route back into the machine.
constexpr std::array<std::string_view, 8> kCriticalUnits{
    "sshd", "ssh", "systemd-journald", "systemd-logind",
    "dbus", "networking", "systemd-networkd", "NetworkManager",
};

constexpr std::array<std::string_view, 5> kReadActions{
    "status", "is-active", "is-enabled", "show", "cat",
};

constexpr std::array<std::string_view, 6> kMutateActions{
    "start", "stop", "restart", "reload", "enable", "disable",
};

bool starts_with(std::string_view value, std::string_view prefix) {
    return value.size() >= prefix.size() && value.compare(0, prefix.size(), prefix) == 0;
}

std::string_view strip_service_suffix(std::string_view unit) {
    constexpr std::string_view suffix = ".service";
    if (unit.size() > suffix.size() && unit.compare(unit.size() - suffix.size(),
                                                    suffix.size(), suffix) == 0) {
        return unit.substr(0, unit.size() - suffix.size());
    }
    return unit;
}

template <typename Container, typename Value>
bool contains(const Container& items, const Value& needle) {
    return std::find(items.begin(), items.end(), needle) != items.end();
}

}  // namespace

bool is_wellformed_sysctl_key(std::string_view key) {
    if (key.empty() || key.size() > 256) {
        return false;
    }
    // Reject traversal before the key is ever joined onto /proc/sys.
    if (key.find("..") != std::string_view::npos || key.find('/') != std::string_view::npos) {
        return false;
    }
    return std::all_of(key.begin(), key.end(), [](unsigned char c) {
        return std::isalnum(c) != 0 || c == '.' || c == '_' || c == '-';
    });
}

Verdict classify_sysctl_read(std::string_view key) {
    if (!is_wellformed_sysctl_key(key)) {
        return Verdict::refuse("malformed sysctl key");
    }
    return Verdict::allow();
}

Verdict classify_sysctl_write(std::string_view key) {
    if (!is_wellformed_sysctl_key(key)) {
        return Verdict::refuse("malformed sysctl key");
    }
    // The refusal list is checked first so that no prefix rule, present or
    // future, can accidentally admit a security-critical parameter.
    for (const auto& entry : kRefusedKeys) {
        if (key == entry.key) {
            return Verdict::refuse(std::string(entry.reason));
        }
    }
    for (const auto& prefix : kWritablePrefixes) {
        if (starts_with(key, prefix)) {
            return Verdict::allow();
        }
    }
    return Verdict::refuse("outside the reviewed writable parameter set");
}

Verdict classify_signal(long pid, int signal_number) {
    if (pid == 0 || pid == 1) {
        return Verdict::refuse("pid 0 and 1 are the kernel and init; signalling them halts the host");
    }
    if (pid < 0) {
        return Verdict::refuse("negative pids address a process group, which is too broad");
    }
    if (!contains(kAllowedSignals, signal_number)) {
        return Verdict::refuse("signal is not in the permitted set");
    }
    return Verdict::allow();
}

bool is_read_only_service_action(std::string_view action) {
    return contains(kReadActions, action);
}

Verdict classify_service(std::string_view unit, std::string_view action) {
    if (unit.empty() || unit.size() > 128) {
        return Verdict::refuse("invalid unit name");
    }
    const bool wellformed = std::all_of(unit.begin(), unit.end(), [](unsigned char c) {
        return std::isalnum(c) != 0 || c == '@' || c == ':' || c == '.' || c == '_' || c == '-';
    });
    if (!wellformed) {
        return Verdict::refuse("invalid unit name");
    }
    if (is_read_only_service_action(action)) {
        return Verdict::allow();
    }
    if (!contains(kMutateActions, action)) {
        return Verdict::refuse("unsupported service action");
    }
    const std::string_view base = strip_service_suffix(unit);
    const bool severing = action == "stop" || action == "disable" || action == "restart";
    if (severing && contains(kCriticalUnits, base)) {
        return Verdict::refuse(
            "this unit carries operator access to the host; stopping it could make the "
            "machine unreachable");
    }
    return Verdict::allow();
}

}  // namespace hoursx
