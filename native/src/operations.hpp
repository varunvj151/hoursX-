// The four privileged operations, each executed only after policy.hpp allows it.
#pragma once

#include <string>
#include <string_view>

namespace hoursx {

struct OpResult {
    bool ok = false;
    std::string payload;  // value read, or a human-readable outcome

    static OpResult success(std::string value) { return {true, std::move(value)}; }
    static OpResult failure(std::string why) { return {false, std::move(why)}; }
};

// Reads /proc/sys/<key-with-dots-as-slashes>. The key must already have passed
// is_wellformed_sysctl_key; this refuses again rather than assume it.
OpResult sysctl_get(std::string_view key);

// Writes the parameter and reads it back, so the reply reports what the kernel
// actually accepted rather than what was requested.
OpResult sysctl_set(std::string_view key, std::string_view value);

OpResult send_signal(long pid, int signal_number);

// Runs systemctl with the unit and action as separate argv entries — never a
// shell string, so a unit name can never become a command.
OpResult service_action(std::string_view unit, std::string_view action);

}  // namespace hoursx
