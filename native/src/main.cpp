// hoursx-sysd — the privileged half of HoursX host control.
//
// Holds the capability so the Python agent does not have to. Accepts requests
// on a group-restricted Unix socket, classifies each one independently of the
// caller, executes the four permitted operations, and logs every privileged
// action to syslog before replying.

#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <string_view>

#include <grp.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <syslog.h>
#include <unistd.h>

#include "operations.hpp"
#include "policy.hpp"
#include "protocol.hpp"

namespace {

constexpr std::string_view kVersion = "hoursx-sysd 0.1.0";
volatile sig_atomic_t g_stop = 0;

void on_terminate(int) { g_stop = 1; }

struct Options {
    std::string socket_path = "/run/hoursx/sysd.sock";
    std::string group_name = "hoursx";
    bool foreground = true;
};

[[noreturn]] void usage(int code) {
    std::fprintf(stderr,
                 "usage: hoursx-sysd [--socket PATH] [--group NAME]\n\n"
                 "  --socket PATH   Unix socket to listen on\n"
                 "                  (default /run/hoursx/sysd.sock)\n"
                 "  --group NAME    group granted access to the socket\n"
                 "                  (default hoursx)\n"
                 "  --version       print version and exit\n");
    std::exit(code);
}

Options parse_options(int argc, char** argv) {
    Options options;
    for (int i = 1; i < argc; ++i) {
        const std::string_view arg = argv[i];
        if (arg == "--socket" && i + 1 < argc) {
            options.socket_path = argv[++i];
        } else if (arg == "--group" && i + 1 < argc) {
            options.group_name = argv[++i];
        } else if (arg == "--version") {
            std::printf("%s\n", std::string(kVersion).c_str());
            std::exit(0);
        } else if (arg == "--help" || arg == "-h") {
            usage(0);
        } else {
            std::fprintf(stderr, "unknown argument: %.*s\n",
                         static_cast<int>(arg.size()), arg.data());
            usage(2);
        }
    }
    return options;
}

// Creates the listening socket owned by root:<group> with mode 0660, so access
// is governed by ordinary Unix group membership rather than anything invented
// here. Returns -1 on failure, having already logged the reason.
int create_listener(const Options& options) {
    if (options.socket_path.size() >= sizeof(sockaddr_un::sun_path)) {
        syslog(LOG_ERR, "socket path is too long");
        return -1;
    }
    ::unlink(options.socket_path.c_str());  // a stale socket must not block startup

    const int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        syslog(LOG_ERR, "socket() failed: %s", std::strerror(errno));
        return -1;
    }

    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    std::snprintf(address.sun_path, sizeof(address.sun_path), "%s",
                  options.socket_path.c_str());

    // Bind under a restrictive umask so the socket is never briefly world
    // writable between bind() and chmod().
    const mode_t previous_umask = ::umask(0177);
    const int bound = ::bind(fd, reinterpret_cast<sockaddr*>(&address), sizeof(address));
    ::umask(previous_umask);
    if (bound != 0) {
        syslog(LOG_ERR, "bind(%s) failed: %s", options.socket_path.c_str(),
               std::strerror(errno));
        ::close(fd);
        return -1;
    }

    const group* grp = ::getgrnam(options.group_name.c_str());
    if (grp == nullptr) {
        syslog(LOG_ERR, "group '%s' does not exist; refusing to start",
               options.group_name.c_str());
        ::close(fd);
        ::unlink(options.socket_path.c_str());
        return -1;
    }
    if (::chown(options.socket_path.c_str(), 0, grp->gr_gid) != 0) {
        syslog(LOG_ERR, "chown of socket failed: %s", std::strerror(errno));
        ::close(fd);
        return -1;
    }
    if (::chmod(options.socket_path.c_str(), 0660) != 0) {
        syslog(LOG_ERR, "chmod of socket failed: %s", std::strerror(errno));
        ::close(fd);
        return -1;
    }
    if (::listen(fd, 16) != 0) {
        syslog(LOG_ERR, "listen() failed: %s", std::strerror(errno));
        ::close(fd);
        return -1;
    }
    return fd;
}

}  // namespace

namespace hoursx {

// Handles one request. Exposed for the test binary, which drives it directly
// rather than going through a socket.
std::string handle_request(std::string_view line) {
    const auto request = parse_request(line);
    if (!request) {
        return make_err("malformed request");
    }
    const std::string& verb = request->verb;
    const auto& args = request->args;

    if (verb == "PING") {
        return make_ok(std::string(kVersion));
    }

    if (verb == "SYSCTL_GET") {
        if (args.size() != 1) {
            return make_err("SYSCTL_GET takes one argument");
        }
        const Verdict verdict = classify_sysctl_read(args[0]);
        if (!verdict.allowed()) {
            return make_err(verdict.reason);
        }
        const OpResult result = sysctl_get(args[0]);
        return result.ok ? make_ok(result.payload) : make_err(result.payload);
    }

    if (verb == "SYSCTL_SET") {
        if (args.size() != 2) {
            return make_err("SYSCTL_SET takes two arguments");
        }
        const Verdict verdict = classify_sysctl_write(args[0]);
        if (!verdict.allowed()) {
            syslog(LOG_WARNING, "refused sysctl write %s: %s", args[0].c_str(),
                   verdict.reason.c_str());
            return make_err(verdict.reason);
        }
        const OpResult result = sysctl_set(args[0], args[1]);
        syslog(LOG_NOTICE, "sysctl_set %s=%s -> %s (%s)", args[0].c_str(), args[1].c_str(),
               result.ok ? "ok" : "failed", result.payload.c_str());
        return result.ok ? make_ok(result.payload) : make_err(result.payload);
    }

    if (verb == "SIGNAL") {
        if (args.size() != 2) {
            return make_err("SIGNAL takes two arguments");
        }
        char* end = nullptr;
        const long pid = std::strtol(args[0].c_str(), &end, 10);
        if (end == args[0].c_str() || *end != '\0') {
            return make_err("pid is not a number");
        }
        const long signal_number = std::strtol(args[1].c_str(), &end, 10);
        if (end == args[1].c_str() || *end != '\0' || signal_number <= 0 ||
            signal_number > 64) {
            return make_err("signal is not a valid number");
        }
        const Verdict verdict = classify_signal(pid, static_cast<int>(signal_number));
        if (!verdict.allowed()) {
            syslog(LOG_WARNING, "refused signal %ld to pid %ld: %s", signal_number, pid,
                   verdict.reason.c_str());
            return make_err(verdict.reason);
        }
        const OpResult result = send_signal(pid, static_cast<int>(signal_number));
        syslog(LOG_NOTICE, "signal %ld to pid %ld -> %s", signal_number, pid,
               result.ok ? "ok" : result.payload.c_str());
        return result.ok ? make_ok(result.payload) : make_err(result.payload);
    }

    if (verb == "SERVICE") {
        if (args.size() != 2) {
            return make_err("SERVICE takes two arguments");
        }
        const Verdict verdict = classify_service(args[0], args[1]);
        if (!verdict.allowed()) {
            syslog(LOG_WARNING, "refused service %s %s: %s", args[1].c_str(),
                   args[0].c_str(), verdict.reason.c_str());
            return make_err(verdict.reason);
        }
        const OpResult result = service_action(args[0], args[1]);
        if (!is_read_only_service_action(args[1])) {
            syslog(LOG_NOTICE, "service %s %s -> %s", args[1].c_str(), args[0].c_str(),
                   result.ok ? "ok" : "failed");
        }
        return result.ok ? make_ok(result.payload) : make_err(result.payload);
    }

    return make_err("unknown verb");
}

}  // namespace hoursx

namespace {

// Serves one connection: read lines, answer each, stop on EOF or a bad line.
void serve_connection(int client_fd) {
    std::string buffer;
    char chunk[4096];

    while (!g_stop) {
        const ssize_t count = ::read(client_fd, chunk, sizeof(chunk));
        if (count <= 0) {
            return;
        }
        buffer.append(chunk, static_cast<std::size_t>(count));
        if (buffer.size() > hoursx::kMaxLineBytes) {
            const std::string reply = hoursx::make_err("request too large");
            (void)::write(client_fd, reply.data(), reply.size());
            return;
        }

        std::size_t newline;
        while ((newline = buffer.find('\n')) != std::string::npos) {
            const std::string line = buffer.substr(0, newline);
            buffer.erase(0, newline + 1);
            const std::string reply = hoursx::handle_request(line);
            if (::write(client_fd, reply.data(), reply.size()) < 0) {
                return;
            }
        }
    }
}

}  // namespace

int main(int argc, char** argv) {
    const Options options = parse_options(argc, argv);

    ::openlog("hoursx-sysd", LOG_PID | LOG_CONS, LOG_DAEMON);
    // A dead client must never take the daemon down with SIGPIPE.
    ::signal(SIGPIPE, SIG_IGN);
    ::signal(SIGTERM, on_terminate);
    ::signal(SIGINT, on_terminate);

    if (::geteuid() != 0) {
        syslog(LOG_WARNING,
               "not running as root; privileged operations will fail with EPERM");
    }

    const int listener = create_listener(options);
    if (listener < 0) {
        ::closelog();
        return 1;
    }
    syslog(LOG_NOTICE, "listening on %s for group %s", options.socket_path.c_str(),
           options.group_name.c_str());

    while (!g_stop) {
        const int client = ::accept(listener, nullptr, nullptr);
        if (client < 0) {
            if (errno == EINTR) {
                continue;
            }
            syslog(LOG_ERR, "accept() failed: %s", std::strerror(errno));
            break;
        }
        // Connections are served one at a time. Host mutations are rare and
        // serialising them removes a whole class of concurrency bug from the
        // component that holds privilege.
        serve_connection(client);
        ::close(client);
    }

    ::close(listener);
    ::unlink(options.socket_path.c_str());
    syslog(LOG_NOTICE, "stopped");
    ::closelog();
    return 0;
}
