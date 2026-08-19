#include "operations.hpp"

#include <cerrno>
#include <csignal>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include "policy.hpp"

namespace hoursx {
namespace {

constexpr std::size_t kMaxReadBytes = 64 * 1024;
constexpr std::size_t kMaxOutputBytes = 16 * 1024;

std::string sysctl_path(std::string_view key) {
    std::string path = "/proc/sys/";
    path.reserve(path.size() + key.size());
    for (const char c : key) {
        path += (c == '.') ? '/' : c;
    }
    return path;
}

std::string trim(std::string value) {
    const auto first = value.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) {
        return {};
    }
    const auto last = value.find_last_not_of(" \t\r\n");
    return value.substr(first, last - first + 1);
}

}  // namespace

OpResult sysctl_get(std::string_view key) {
    // Re-validate: this function must be safe even if called directly.
    if (!is_wellformed_sysctl_key(key)) {
        return OpResult::failure("malformed sysctl key");
    }
    std::ifstream file(sysctl_path(key));
    if (!file.is_open()) {
        return OpResult::failure("parameter is not readable");
    }
    std::string content;
    content.resize(kMaxReadBytes);
    file.read(content.data(), static_cast<std::streamsize>(kMaxReadBytes));
    content.resize(static_cast<std::size_t>(file.gcount()));
    return OpResult::success(trim(std::move(content)));
}

OpResult sysctl_set(std::string_view key, std::string_view value) {
    if (!is_wellformed_sysctl_key(key)) {
        return OpResult::failure("malformed sysctl key");
    }
    // Values are written verbatim to a kernel interface, so anything with a
    // newline or control character is rejected rather than sanitised.
    if (value.empty() || value.size() > 256) {
        return OpResult::failure("value is empty or too long");
    }
    for (const char raw : value) {
        const auto c = static_cast<unsigned char>(raw);
        if (c < 0x20 || c == 0x7F) {
            return OpResult::failure("value contains control characters");
        }
    }

    const OpResult previous = sysctl_get(key);
    if (!previous.ok) {
        return OpResult::failure("parameter does not exist on this kernel");
    }

    std::ofstream file(sysctl_path(key));
    if (!file.is_open()) {
        return OpResult::failure("permission denied writing parameter");
    }
    file << value;
    file.flush();
    if (!file.good()) {
        return OpResult::failure("kernel rejected the value");
    }
    file.close();

    const OpResult current = sysctl_get(key);
    std::ostringstream out;
    out << previous.payload << " -> " << (current.ok ? current.payload : std::string("?"));
    return OpResult::success(out.str());
}

OpResult send_signal(long pid, int signal_number) {
    const Verdict verdict = classify_signal(pid, signal_number);
    if (!verdict.allowed()) {
        return OpResult::failure(verdict.reason);
    }
    if (::kill(static_cast<pid_t>(pid), signal_number) != 0) {
        if (errno == ESRCH) {
            return OpResult::failure("no such process");
        }
        if (errno == EPERM) {
            return OpResult::failure("permission denied signalling process");
        }
        return OpResult::failure(std::string("kill failed: ") + std::strerror(errno));
    }
    return OpResult::success("signal delivered");
}

OpResult service_action(std::string_view unit, std::string_view action) {
    int pipe_fds[2];
    if (::pipe(pipe_fds) != 0) {
        return OpResult::failure("could not create pipe");
    }

    const pid_t child = ::fork();
    if (child < 0) {
        ::close(pipe_fds[0]);
        ::close(pipe_fds[1]);
        return OpResult::failure("could not fork");
    }

    if (child == 0) {
        // Child: redirect both streams to the pipe and exec systemctl directly.
        // execvp with separate argv entries means a unit name can never be
        // interpreted as a command, however it is spelled.
        ::close(pipe_fds[0]);
        ::dup2(pipe_fds[1], STDOUT_FILENO);
        ::dup2(pipe_fds[1], STDERR_FILENO);
        ::close(pipe_fds[1]);

        const std::string action_str(action);
        const std::string unit_str(unit);
        const char* argv[] = {"systemctl", action_str.c_str(), unit_str.c_str(),
                              "--no-pager", nullptr};
        ::execvp("systemctl", const_cast<char* const*>(argv));
        ::_exit(127);  // exec failed
    }

    ::close(pipe_fds[1]);
    std::string output;
    char buffer[4096];
    ssize_t count;
    while ((count = ::read(pipe_fds[0], buffer, sizeof(buffer))) > 0) {
        if (output.size() < kMaxOutputBytes) {
            output.append(buffer, static_cast<std::size_t>(count));
        }
    }
    ::close(pipe_fds[0]);

    int status = 0;
    ::waitpid(child, &status, 0);
    const int exit_code = WIFEXITED(status) ? WEXITSTATUS(status) : -1;

    if (exit_code == 127) {
        return OpResult::failure("systemctl is not available on this host");
    }
    if (output.size() > kMaxOutputBytes) {
        output.resize(kMaxOutputBytes);
    }

    std::ostringstream out;
    out << "exit " << exit_code << "\n" << output;
    // `is-active` and friends answer through the exit status, so a nonzero code
    // there is information rather than failure. The caller interprets it.
    return {exit_code == 0 || is_read_only_service_action(action), out.str()};
}

}  // namespace hoursx
