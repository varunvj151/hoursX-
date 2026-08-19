// Independent enforcement of the host-operation policy.
//
// This mirrors hoursx/system/privileges.py on purpose. The Python layer
// classifies operations before it asks; this daemon classifies them again
// before it acts, and refuses on its own authority.
//
// The duplication is the point. If the Python process is compromised it can
// send anything down the socket, so a refusal that lives only in the caller is
// not a control. This file is the component that must not be persuaded.
#pragma once

#include <string>
#include <string_view>

namespace hoursx {

enum class Decision {
    Allow,    // read-only, or an approved and permitted mutation
    Refuse,   // no argument makes this acceptable
};

struct Verdict {
    Decision decision;
    std::string reason;  // always populated on Refuse

    static Verdict allow() { return {Decision::Allow, {}}; }
    static Verdict refuse(std::string why) { return {Decision::Refuse, std::move(why)}; }
    bool allowed() const { return decision == Decision::Allow; }
};

// A sysctl key is well-formed if it contains only [A-Za-z0-9._-] and no
// traversal. Enforced before the key is ever turned into a path.
bool is_wellformed_sysctl_key(std::string_view key);

// Reading any readable parameter is permitted; the kernel's own permissions
// are the boundary there.
Verdict classify_sysctl_read(std::string_view key);

// Writing is deny-by-default: only a reviewed prefix set is permitted, and a
// named set of security-critical parameters is refused outright even if some
// future prefix change would otherwise admit them.
Verdict classify_sysctl_write(std::string_view key);

// PID 0 and 1 halt the machine; negative PIDs address a whole process group.
// Only a small set of signals is permitted at all.
Verdict classify_signal(long pid, int signal_number);

// Unit names are validated, inspection is free, mutation is restricted, and
// units carrying operator access cannot be stopped or disabled.
Verdict classify_service(std::string_view unit, std::string_view action);

// True when the action only inspects, so callers can skip mutation checks.
bool is_read_only_service_action(std::string_view action);

}  // namespace hoursx
