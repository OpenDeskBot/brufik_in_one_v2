#pragma once
#include "face_package_data.h"
#include "server_endpoint.h"
#include <cstdint>
#include <cstring>

// Parse the ordinary HTTP response before applying any channel configuration.
// This header is portable: host tests feed it the service's actual response.
namespace bootstrap_config {
enum class Result { ready, invalid_configuration, invalid_manifest };
struct Manifest {
  char version[17]{}, sha256[65]{}, endpoint[server_endpoint::kUrlCapacity]{};
  uint32_t size=0;
};
struct Settings {
  bool compatible=false, camera_enabled=false;
  char stream_endpoint[server_endpoint::kUrlCapacity]{}, camera_endpoint[server_endpoint::kUrlCapacity]{};
  Manifest package;
};
inline bool hex(JsonVariantConst value,size_t count) {
  if (!face_package_data::text(value,count) || value.as<JsonString>().size()!=count) return false;
  for (const char* p=value.as<const char*>();*p;++p)
    if (!(*p>='0'&&*p<='9') && !(*p>='a'&&*p<='f')) return false;
  return true;
}
inline bool endpoint(JsonVariantConst value,const server_endpoint::Url& base,bool websocket,char* out) {
  if (!face_package_data::text(value,server_endpoint::kUrlCapacity-1)) return false;
  std::string resolved;
  if (!server_endpoint::resolve(base,value.as<const char*>(),websocket,resolved)) return false;
  strcpy(out,resolved.c_str());return true;
}
inline Result decode(JsonVariantConst root,const char* device_id,const char* bootstrap_url,Settings& out) {
  out=Settings{};
  server_endpoint::Url base;
  if (!server_endpoint::bootstrap(bootstrap_url,base)) return Result::invalid_configuration;
  auto data=root["data"];
  if (!device_id || !*device_id || !face_package_data::integer(root["code"],0,0) ||
      !data.is<JsonObjectConst>() || !face_package_data::integer(data["schema"],2,2) ||
      !face_package_data::text(data["device_id"],80) || strcmp(data["device_id"],device_id) ||
      strcmp(data["realtime"]["protocol"] | "","deskbot.pb.v2") ||
      !endpoint(data["realtime"]["url"],base,true,out.stream_endpoint)) return Result::invalid_configuration;
  auto camera=data["camera"];
  if (!camera.isNull() && (!camera.is<JsonObjectConst>() ||
      (!camera["enabled"].isNull() && !camera["enabled"].is<bool>()))) return Result::invalid_configuration;
  out.camera_enabled=camera["enabled"] | false;
  if (out.camera_enabled && !endpoint(camera["url"],base,true,out.camera_endpoint)) {
    out.camera_enabled=false;return Result::invalid_configuration;
  }
  // A missing/bad optional resource manifest must not prevent audio from using
  // a verified cached package, but an incompatible channel configuration must.
  out.compatible=true;
  auto pack=data["face_pack"];
  if (strcmp(pack["format"] | "","deskbot-face-v1") || !hex(pack["version"],16) ||
      !hex(pack["sha256"],64) || !face_package_data::integer(pack["size"],1,face_package_data::kMaxBytes) ||
      !endpoint(pack["url"],base,false,out.package.endpoint)) return Result::invalid_manifest;
  strcpy(out.package.version,pack["version"]);
  strcpy(out.package.sha256,pack["sha256"]);
  out.package.size=pack["size"];
  return Result::ready;
}
} // namespace bootstrap_config
