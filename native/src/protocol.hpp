// The wire grammar: line-delimited, space-separated, base64-encoded fields.
//
//   Request:   VERB <b64-arg> [<b64-arg>...]
//   Response:  OK <b64-payload>  |  ERR <b64-reason>
//
// Base64 rather than JSON is a security choice. It removes quoting, escaping,
// and injection concerns entirely, and keeps the parser short enough to audit
// in one sitting — which matters in the one component that holds privilege.
#pragma once

#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace hoursx {

std::string base64_encode(std::string_view input);

// Strict: rejects invalid alphabet, bad padding, and wrong length. A privileged
// parser should refuse anything it does not fully understand.
std::optional<std::string> base64_decode(std::string_view input);

struct Request {
    std::string verb;
    std::vector<std::string> args;
};

// Parses one line. Returns nullopt when the line is malformed, over-long, or
// contains a field that is not valid base64.
std::optional<Request> parse_request(std::string_view line);

std::string make_ok(std::string_view payload);
std::string make_err(std::string_view reason);

// Longest line the daemon will accept, to bound memory from a hostile caller.
inline constexpr std::size_t kMaxLineBytes = 64 * 1024;
inline constexpr std::size_t kMaxArgs = 8;

}  // namespace hoursx
