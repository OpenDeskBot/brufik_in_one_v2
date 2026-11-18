#pragma once

#include <cstdint>
#include <string>

// Shared by provisioning, discovery and host tests. URLs are complete endpoints,
// never prefixes to which firmware appends a service path.
namespace server_endpoint {
constexpr size_t kUrlCapacity = 512;
struct Url {
  std::string scheme, host, authority, path;
  uint16_t port = 0;
  bool secure() const { return scheme == "https" || scheme == "wss"; }
};

inline bool parse(const std::string& raw, Url& out) {
  out = {};
  if (raw.empty() || raw.size() >= kUrlCapacity) return false;
  for (unsigned char c : raw)
    if (c <= 32 || c >= 127 || c == '\\' || c == '#') return false;
  const auto split = raw.find("://");
  if (split == std::string::npos) return false;
  out.scheme = raw.substr(0, split);
  if (out.scheme != "http" && out.scheme != "https" && out.scheme != "ws" && out.scheme != "wss") return false;
  const auto slash = raw.find('/', split + 3);
  // A non-root path is required: a bare server origin is not a bootstrap API.
  if (slash == std::string::npos) return false;
  out.authority = raw.substr(split + 3, slash - split - 3);
  out.path = raw.substr(slash);
  if (out.authority.empty() || out.authority.find_first_of("@?%") != std::string::npos ||
      out.path == "/" || out.path[1] == '?' || out.path[1] == '/' ||
      out.path.find("..") != std::string::npos) return false;
  const auto colon = out.authority.find(':');
  out.host = out.authority.substr(0, colon);
  if (out.host.empty() || out.host.size() >= 64) return false;
  for (char c : out.host)
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || c == '.' || c == '-')) return false;
  out.port = out.secure() ? 443 : 80;
  if (colon != std::string::npos) {
    const auto digits = out.authority.substr(colon + 1);
    if (digits.empty() || digits.size() > 5) return false;
    unsigned port = 0;
    for (char c : digits) {
      if (c < '0' || c > '9') return false;
      port = port * 10 + c - '0';
    }
    if (!port || port > 65535) return false;
    out.port = static_cast<uint16_t>(port);
  }
  return true;
}

inline bool bootstrap(const char* raw, Url& out) {
  return raw && parse(raw, out) && (out.scheme == "http" || out.scheme == "https") &&
      out.path.find('?') == std::string::npos;
}

inline std::string with_device(const std::string& endpoint, const char* device_id) {
  return endpoint + (endpoint.find('?') == std::string::npos ? "?" : "&") + "device_id=" + device_id;
}

// Root-relative references use the bootstrap origin, per ordinary URL semantics.
// Absolute URLs may move media/resources to another host or port. An HTTPS
// discovery endpoint cannot direct the ROM to unencrypted HTTP/WS endpoints.
inline bool resolve(const Url& base, const char* value, bool websocket, std::string& result) {
  if (!value || !*value) return false;
  const std::string ref(value);
  const std::string scheme = websocket ? (base.secure() ? "wss" : "ws") : base.scheme;
  result = ref[0] == '/' ? scheme + "://" + base.authority + ref : ref;
  Url target;
  return parse(result, target) &&
      (websocket ? (target.scheme == "ws" || target.scheme == "wss") :
                   (target.scheme == "http" || target.scheme == "https")) &&
      (!base.secure() || target.secure()) &&
      target.path.find("device_id=") == std::string::npos;
}
}  // namespace server_endpoint
