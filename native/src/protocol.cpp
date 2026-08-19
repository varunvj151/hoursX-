#include "protocol.hpp"

#include <array>
#include <cstdint>

namespace hoursx {
namespace {

constexpr std::string_view kAlphabet =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

// Reverse table built once; 0xFF marks a character outside the alphabet.
std::array<std::uint8_t, 256> make_reverse_table() {
    std::array<std::uint8_t, 256> table{};
    table.fill(0xFF);
    for (std::uint8_t i = 0; i < kAlphabet.size(); ++i) {
        table[static_cast<unsigned char>(kAlphabet[i])] = i;
    }
    return table;
}

const std::array<std::uint8_t, 256>& reverse_table() {
    static const std::array<std::uint8_t, 256> table = make_reverse_table();
    return table;
}

}  // namespace

std::string base64_encode(std::string_view input) {
    std::string out;
    out.reserve(((input.size() + 2) / 3) * 4);
    std::size_t i = 0;
    while (i + 2 < input.size()) {
        const std::uint32_t block = (static_cast<unsigned char>(input[i]) << 16) |
                                    (static_cast<unsigned char>(input[i + 1]) << 8) |
                                    static_cast<unsigned char>(input[i + 2]);
        out += kAlphabet[(block >> 18) & 0x3F];
        out += kAlphabet[(block >> 12) & 0x3F];
        out += kAlphabet[(block >> 6) & 0x3F];
        out += kAlphabet[block & 0x3F];
        i += 3;
    }
    if (i + 1 == input.size()) {
        const std::uint32_t block = static_cast<unsigned char>(input[i]) << 16;
        out += kAlphabet[(block >> 18) & 0x3F];
        out += kAlphabet[(block >> 12) & 0x3F];
        out += "==";
    } else if (i + 2 == input.size()) {
        const std::uint32_t block = (static_cast<unsigned char>(input[i]) << 16) |
                                    (static_cast<unsigned char>(input[i + 1]) << 8);
        out += kAlphabet[(block >> 18) & 0x3F];
        out += kAlphabet[(block >> 12) & 0x3F];
        out += kAlphabet[(block >> 6) & 0x3F];
        out += '=';
    }
    return out;
}

std::optional<std::string> base64_decode(std::string_view input) {
    if (input.size() % 4 != 0) {
        return std::nullopt;
    }
    if (input.empty()) {
        return std::string{};
    }
    const auto& table = reverse_table();

    std::size_t padding = 0;
    if (input[input.size() - 1] == '=') {
        ++padding;
    }
    if (input.size() >= 2 && input[input.size() - 2] == '=') {
        ++padding;
    }

    std::string out;
    out.reserve((input.size() / 4) * 3);
    for (std::size_t i = 0; i < input.size(); i += 4) {
        std::uint32_t block = 0;
        for (std::size_t j = 0; j < 4; ++j) {
            const char c = input[i + j];
            if (c == '=') {
                // Padding is only legal in the final quantum's tail.
                if (i + 4 != input.size() || j < 2) {
                    return std::nullopt;
                }
                block <<= 6;
                continue;
            }
            const std::uint8_t value = table[static_cast<unsigned char>(c)];
            if (value == 0xFF) {
                return std::nullopt;
            }
            block = (block << 6) | value;
        }
        out += static_cast<char>((block >> 16) & 0xFF);
        out += static_cast<char>((block >> 8) & 0xFF);
        out += static_cast<char>(block & 0xFF);
    }
    out.resize(out.size() - padding);
    return out;
}

std::optional<Request> parse_request(std::string_view line) {
    if (line.empty() || line.size() > kMaxLineBytes) {
        return std::nullopt;
    }
    Request request;
    std::size_t start = 0;
    std::size_t fields = 0;

    while (start <= line.size()) {
        const std::size_t space = line.find(' ', start);
        const std::string_view token =
            line.substr(start, space == std::string_view::npos ? std::string_view::npos
                                                               : space - start);
        if (fields == 0) {
            if (token.empty() || token.size() > 32) {
                return std::nullopt;
            }
            // The verb is plain ASCII so the log line stays readable; anything
            // outside a strict alphabet is rejected rather than sanitised.
            for (const char c : token) {
                if ((c < 'A' || c > 'Z') && c != '_') {
                    return std::nullopt;
                }
            }
            request.verb = std::string(token);
        } else {
            if (request.args.size() >= kMaxArgs) {
                return std::nullopt;
            }
            auto decoded = base64_decode(token);
            if (!decoded) {
                return std::nullopt;
            }
            request.args.push_back(std::move(*decoded));
        }
        ++fields;
        if (space == std::string_view::npos) {
            break;
        }
        start = space + 1;
    }
    return request;
}

std::string make_ok(std::string_view payload) {
    return "OK " + base64_encode(payload) + "\n";
}

std::string make_err(std::string_view reason) {
    return "ERR " + base64_encode(reason) + "\n";
}

}  // namespace hoursx
