// Tests for the daemon's own policy and parser.
//
// These matter more than most: this code is what still has to hold when the
// Python caller is compromised and sending hostile input deliberately. A
// dependency-free harness keeps the privileged build free of test frameworks.

#include <csignal>
#include <cstdio>
#include <string>

#include "operations.hpp"
#include "policy.hpp"
#include "protocol.hpp"

namespace {

int g_failures = 0;
int g_checks = 0;

void check(bool condition, const char* what) {
    ++g_checks;
    if (!condition) {
        ++g_failures;
        std::printf("  FAIL  %s\n", what);
    }
}

void section(const char* name) { std::printf("\n%s\n", name); }

using namespace hoursx;

void test_sysctl_key_validation() {
    section("sysctl key validation");
    check(is_wellformed_sysctl_key("vm.swappiness"), "accepts an ordinary key");
    check(is_wellformed_sysctl_key("net.ipv4.tcp_fin_timeout"), "accepts underscores");
    check(is_wellformed_sysctl_key("fs.file-max"), "accepts hyphens");
    check(!is_wellformed_sysctl_key(""), "rejects empty");
    check(!is_wellformed_sysctl_key("../../etc/passwd"), "rejects traversal");
    check(!is_wellformed_sysctl_key("vm/../../etc"), "rejects slashes");
    check(!is_wellformed_sysctl_key("vm.swappiness; rm -rf /"), "rejects shell metacharacters");
    check(!is_wellformed_sysctl_key("vm.swap piness"), "rejects spaces");
    check(!is_wellformed_sysctl_key(std::string(300, 'a')), "rejects over-long keys");
}

void test_sysctl_write_policy() {
    section("sysctl write policy");
    check(classify_sysctl_write("vm.swappiness").allowed(), "allows a reviewed tunable");
    check(classify_sysctl_write("net.core.somaxconn").allowed(), "allows a net.core key");
    check(classify_sysctl_write("fs.file-max").allowed(), "allows fs.file-max");

    check(!classify_sysctl_write("kernel.core_pattern").allowed(),
          "refuses kernel.core_pattern");
    check(!classify_sysctl_write("kernel.modules_disabled").allowed(),
          "refuses kernel.modules_disabled");
    check(!classify_sysctl_write("kernel.randomize_va_space").allowed(),
          "refuses disabling ASLR");
    check(!classify_sysctl_write("kernel.sysrq").allowed(), "refuses kernel.sysrq");
    check(!classify_sysctl_write("kernel.kptr_restrict").allowed(),
          "refuses kernel.kptr_restrict");
    check(!classify_sysctl_write("some.invented.key").allowed(),
          "refuses unreviewed keys by default");
    check(!classify_sysctl_write("../../proc/self/environ").allowed(),
          "refuses traversal in a write");

    check(!classify_sysctl_write("kernel.core_pattern").reason.empty(),
          "a refusal always explains itself");
}

void test_signal_policy() {
    section("signal policy");
    check(!classify_signal(1, SIGKILL).allowed(), "refuses signalling init");
    check(!classify_signal(0, SIGTERM).allowed(), "refuses pid 0");
    check(!classify_signal(-1, SIGTERM).allowed(), "refuses process groups");
    check(!classify_signal(4242, 999).allowed(), "refuses unknown signal numbers");
    check(classify_signal(4242, SIGTERM).allowed(), "allows TERM to an ordinary pid");
    check(classify_signal(4242, SIGHUP).allowed(), "allows HUP");
    check(classify_signal(4242, SIGKILL).allowed(), "allows KILL to an ordinary pid");
}

void test_service_policy() {
    section("service policy");
    check(classify_service("nginx", "status").allowed(), "allows inspection");
    check(classify_service("nginx", "restart").allowed(), "allows restarting an ordinary unit");
    check(classify_service("sshd", "status").allowed(), "allows inspecting sshd");

    check(!classify_service("sshd", "stop").allowed(), "refuses stopping sshd");
    check(!classify_service("sshd.service", "stop").allowed(), "normalises the .service suffix");
    check(!classify_service("systemd-journald", "restart").allowed(),
          "refuses restarting journald");
    check(!classify_service("NetworkManager", "disable").allowed(),
          "refuses disabling NetworkManager");
    check(!classify_service("nginx; rm -rf /", "status").allowed(),
          "refuses shell metacharacters in a unit name");
    check(!classify_service("nginx", "mask").allowed(), "refuses unsupported actions");
    check(!classify_service("", "status").allowed(), "refuses an empty unit");

    check(is_read_only_service_action("is-active"), "is-active is read-only");
    check(!is_read_only_service_action("stop"), "stop is not read-only");
}

void test_base64_roundtrip() {
    section("base64");
    const char* samples[] = {"", "a", "ab", "abc", "abcd", "vm.swappiness",
                             "value with spaces and \n newline", "\x01\x02\x03\xff"};
    for (const char* sample : samples) {
        const std::string original(sample);
        const auto decoded = base64_decode(base64_encode(original));
        check(decoded.has_value() && *decoded == original, "round-trips exactly");
    }
    check(!base64_decode("!!!!").has_value(), "rejects an invalid alphabet");
    check(!base64_decode("abc").has_value(), "rejects a bad length");
    check(!base64_decode("a===").has_value(), "rejects malformed padding");
}

void test_request_parsing() {
    section("request parsing");
    const auto ping = parse_request("PING");
    check(ping.has_value() && ping->verb == "PING" && ping->args.empty(), "parses a bare verb");

    const std::string line = "SYSCTL_SET " + base64_encode("vm.swappiness") + " " +
                             base64_encode("10");
    const auto set = parse_request(line);
    check(set.has_value(), "parses a two-argument request");
    check(set && set->args.size() == 2 && set->args[0] == "vm.swappiness" &&
              set->args[1] == "10",
          "decodes both arguments");

    check(!parse_request("").has_value(), "rejects an empty line");
    check(!parse_request("lowercase").has_value(), "rejects a lowercase verb");
    check(!parse_request("BAD;VERB").has_value(), "rejects punctuation in a verb");
    check(!parse_request("SYSCTL_GET !!!not-base64!!!").has_value(),
          "rejects an argument that is not base64");
    check(!parse_request(std::string(kMaxLineBytes + 1, 'A')).has_value(),
          "rejects an over-long line");

    // A value containing a space must survive, because base64 has no spaces.
    const std::string spaced = "SERVICE " + base64_encode("my unit") + " " +
                               base64_encode("status");
    const auto parsed = parse_request(spaced);
    check(parsed && parsed->args[0] == "my unit", "arguments may contain spaces");
}

void test_response_framing() {
    section("response framing");
    const std::string ok = make_ok("done");
    check(ok.rfind("OK ", 0) == 0, "OK responses are prefixed");
    check(ok.back() == '\n', "responses are newline terminated");
    const auto payload = base64_decode(ok.substr(3, ok.size() - 4));
    check(payload.has_value() && *payload == "done", "payload survives framing");

    const std::string err = make_err("nope");
    check(err.rfind("ERR ", 0) == 0, "ERR responses are prefixed");
}

void test_sysctl_read_of_real_parameter() {
    section("live sysctl read");
    const OpResult result = sysctl_get("kernel.ostype");
    // Restricted environments may refuse the read; it must never claim success
    // with an empty payload.
    check(!result.ok || result.payload == "Linux", "reads kernel.ostype or fails cleanly");

    const OpResult bad = sysctl_get("../../etc/passwd");
    check(!bad.ok, "refuses a traversal read");
}

void test_sysctl_set_value_validation() {
    section("sysctl value validation");
    const OpResult newline = sysctl_set("vm.swappiness", "10\nkernel.sysrq=1");
    check(!newline.ok, "refuses a value containing a newline");

    const OpResult empty = sysctl_set("vm.swappiness", "");
    check(!empty.ok, "refuses an empty value");

    const OpResult long_value = sysctl_set("vm.swappiness", std::string(500, '1'));
    check(!long_value.ok, "refuses an over-long value");
}

}  // namespace

int main() {
    std::printf("hoursx-sysd policy tests\n");
    test_sysctl_key_validation();
    test_sysctl_write_policy();
    test_signal_policy();
    test_service_policy();
    test_base64_roundtrip();
    test_request_parsing();
    test_response_framing();
    test_sysctl_read_of_real_parameter();
    test_sysctl_set_value_validation();

    std::printf("\n%d checks, %d failures\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
