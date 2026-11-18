#include "device_bootstrap.h"
#include "face_package.h"
#include "face_package_data.h"
#include "face_cache_policy.h"
#include "face_assets.h"
#include "bootstrap_config.h"
#include "deskbot_config.h"
#include "logger.h"
#include "boot_guide.h"
#include "display.h"
#include "utils/nvs_config_utils.h"
#include "utils/utils.h"
#include <FFat.h>
#include <Preferences.h>
#include <WiFi.h>
#include <atomic>
#include <algorithm>
#include <esp_crt_bundle.h>
#include <esp_http_client.h>
#include <mbedtls/sha256.h>
#include <time.h>
#include <cerrno>
#include <cstdio>
#include <unistd.h>

namespace {
// JSON validation needs a larger stack; package bytes/document live in PSRAM.
constexpr uint32_t kTaskStack=16*1024;
TaskHandle_t s_task=nullptr;
std::atomic<bool> s_done{false};
bool s_stream_enabled=false,s_camera_enabled=false;
String s_stream_url,s_camera_url;
String s_origin;
const char* s_source="compiled";
const char* s_result="pending";
int s_active_slot=-1;
constexpr const char* kSlots[]={"/face0.bin","/face1.bin"};

String sha256(const uint8_t* bytes,size_t n) {
  uint8_t hash[32];mbedtls_sha256(bytes,n,hash,0);char hex[65];
  for (unsigned i=0;i<32;++i) snprintf(hex+i*2,3,"%02x",hash[i]);
  return String(hex);
}
struct Buffer {
  uint8_t* data=nullptr;size_t size=0;
  ~Buffer(){free(data);}
};
bool read_file(const char* path,Buffer& bytes) {
  File f=FFat.open(path,"r");
  if (!f || !f.size() || f.size()>face_package_data::kMaxBytes) return false;
  bytes.size=f.size();bytes.data=(uint8_t*)psram_malloc(bytes.size);
  return bytes.data && f.read(bytes.data,bytes.size)==bytes.size;
}
bool get_http(const String& url,size_t max,Buffer& out) {
  esp_http_client_config_t config{};
  config.url=url.c_str();config.timeout_ms=4000;config.disable_auto_redirect=true;
  config.crt_bundle_attach=esp_crt_bundle_attach;
  auto client=esp_http_client_init(&config);
  if (!client) return false;
  bool ok=false;
  do {
    if (esp_http_client_open(client,0)!=ESP_OK) break;
    const auto length=esp_http_client_fetch_headers(client);
    if (esp_http_client_get_status_code(client)!=200 || length>(int64_t)max) break;
    out.data=(uint8_t*)psram_malloc(max+1);
    if (!out.data) break;
    const uint32_t deadline=millis()+12000;
    int n=0;
    while (out.size<max && (int32_t)(deadline-millis())>0) {
      n=esp_http_client_read(client,(char*)out.data+out.size,std::min<size_t>(2048,max-out.size));
      if (n<0) break;
      if (n==0) {if (esp_http_client_is_complete_data_received(client)) break;vTaskDelay(1);continue;}
      out.size+=n;
    }
    ok=n>=0 && out.size && esp_http_client_is_complete_data_received(client);
    if (ok) out.data[out.size]=0;
  } while(false);
  esp_http_client_close(client);esp_http_client_cleanup(client);
  return ok;
}
void sync_clock_if_needed(bool secure) {
  if (!secure || time(nullptr)>=1700000000) return;
  configTime(0,0,"ntp.aliyun.com","pool.ntp.org","time.cloudflare.com");
  const uint32_t until=millis()+4000;
  while (time(nullptr)<1700000000 && (int32_t)(until-millis())>0) vTaskDelay(pdMS_TO_TICKS(50));
}
struct FaceCacheStore {
  Preferences& prefs;
  const Buffer* incoming=nullptr;
  const char* version="";
  const char* digest="";
  bool load_if_valid(unsigned slot) {
    JsonDocument meta;
    if (deserializeJson(meta,prefs.getString(slot?"s1":"s0","")) ||
        s_origin!=String(meta["origin"] | "")) return false;
    Buffer bytes;
    return read_file(kSlots[slot],bytes) && sha256(bytes.data,bytes.size)==String(meta["sha256"] | "") &&
        face_package_install(bytes.data,bytes.size,meta["version"] | "");
  }
  bool write_temporary() {
    log_warn("[BOOTSTRAP] store: writing temporary file");
    // Use stdio here to retain the original write/open error and explicitly
    // sync the contents before readback and publication of the inactive slot.
    FILE* f=fopen("/ffat/face.tmp","wb");
    if (!f) {
      const int error=errno;
      log_error("[BOOTSTRAP] temporary open failed errno=%d (%s), used=%u total=%u internal=%u",
          error,strerror(error),(unsigned)FFat.usedBytes(),(unsigned)FFat.totalBytes(),
          (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
      return false;
    }
    const bool written=fwrite(incoming->data,1,incoming->size,f)==incoming->size;
    const bool flushed=fflush(f)==0;
    const bool synced=fsync(fileno(f))==0;
    const bool closed=fclose(f)==0;
    return written && flushed && synced && closed;
  }
  bool verify_temporary() {
    log_warn("[BOOTSTRAP] store: verifying temporary file");
    Buffer bytes;
    return read_file("/face.tmp",bytes) && bytes.size==incoming->size && sha256(bytes.data,bytes.size)==String(digest);
  }
  bool replace_slot(unsigned slot) {
    log_warn("[BOOTSTRAP] store: publishing slot=%u",slot);
    FFat.remove(kSlots[slot]);
    return FFat.rename("/face.tmp",kSlots[slot]);
  }
  bool write_metadata(unsigned slot) {
    log_warn("[BOOTSTRAP] store: writing slot metadata=%u",slot);
    JsonDocument meta;meta["origin"]=s_origin;meta["sha256"]=digest;meta["version"]=version;
    String record;serializeJson(meta,record);
    return prefs.putString(slot?"s1":"s0",record)!=0;
  }
  bool select_slot(unsigned slot) {log_warn("[BOOTSTRAP] store: selecting slot=%u",slot);return prefs.putUChar("slot",slot)!=0;}
};
void sync_once() {
  char configured[server_endpoint::kUrlCapacity];
  deskbot_ws_format_active_url(configured,sizeof(configured));
  server_endpoint::Url target;
  if (!server_endpoint::bootstrap(configured,target)) {
    s_result="invalid_address";
    log_error("[BOOTSTRAP] configure a complete HTTP(S) discovery endpoint");
    return;
  }
  // Scope durable resources to the exact configured discovery endpoint.
  s_origin=configured;
  Preferences prefs;const bool store=prefs.begin("face_packs",false);
  FaceCacheStore cache{prefs};
  int active=store?face_cache_policy::recover(cache,prefs.getUChar("slot",0)):-1;
  if (active>=0) {
    s_source="cache";s_active_slot=active;
    log_warn("[BOOTSTRAP] loaded cached pack=%s slot=%d",face_assets_version(),active);
  }
  sync_clock_if_needed(target.secure());
  Buffer config;
  if (!get_http(String(server_endpoint::with_device(configured,get_device_id()).c_str()),16*1024,config)) {
    s_result="http_unavailable";
    log_warn("[BOOTSTRAP] discovery unavailable; media disabled, keeping pack %s",face_assets_version());
    if(store)prefs.end();return;
  }
  JsonDocument doc;
  if (deserializeJson(doc,config.data,config.size)) {
    s_stream_enabled=false;s_camera_enabled=false;s_result="invalid_config";if(store)prefs.end();return;
  }
  bootstrap_config::Settings discovered;
  const auto result=bootstrap_config::decode(doc.as<JsonVariantConst>(),get_device_id(),configured,discovered);
  s_stream_enabled=discovered.compatible;
  s_camera_enabled=discovered.compatible && discovered.camera_enabled;
  if (result==bootstrap_config::Result::invalid_configuration) {
    s_result="invalid_config";log_error("[BOOTSTRAP] incompatible server configuration");
    if(store)prefs.end();return;
  }
  s_stream_url=discovered.stream_endpoint;s_camera_url=discovered.camera_endpoint;
  // An HTTP discovery server may advertise secure media or a secure CDN too.
  sync_clock_if_needed(s_stream_url.startsWith("wss://") ||
      s_camera_url.startsWith("wss://") || String(discovered.package.endpoint).startsWith("https://"));
  if (result==bootstrap_config::Result::invalid_manifest) {s_result="invalid_manifest";if(store)prefs.end();return;}
  const auto& pack=discovered.package;
  const char* version=pack.version;
  const char* digest=pack.sha256;
  if (active>=0 && !strcmp(face_assets_version(),version)) {
    s_result="current";log_warn("[BOOTSTRAP] pack current; download skipped");if(store)prefs.end();return;
  }
  Buffer bytes;
  if (!store || !get_http(String(pack.endpoint),face_package_data::kMaxBytes,bytes) || bytes.size!=pack.size ||
      sha256(bytes.data,bytes.size)!=String(digest)) {
    s_result="download_failed";log_warn("[BOOTSTRAP] pack download/integrity failed; keeping previous resources");
    if(store)prefs.end();return;
  }
  log_warn("[BOOTSTRAP] downloaded %u bytes; validating render program",(unsigned)bytes.size);
  // Validate before touching either durable slot; never replace live resources on write failure.
  if (!face_package_validate(bytes.data,bytes.size,version)) {
    s_result="invalid_package";log_warn("[BOOTSTRAP] rejected invalid pack");prefs.end();return;
  }
  log_warn("[BOOTSTRAP] render program valid; committing package");
  cache.incoming=&bytes;cache.version=version;cache.digest=digest;
  const bool committed=face_cache_policy::commit(cache,active);
  FFat.remove("/face.tmp");prefs.end();
  if (committed && face_package_install(bytes.data,bytes.size,version)) {
    s_source="downloaded";s_result="updated";s_active_slot=active==0?1:0;
    log_warn("[BOOTSTRAP] active face pack=%s (%u bytes)",face_assets_version(),(unsigned)bytes.size);
  } else {s_result="storage_failed";log_warn("[BOOTSTRAP] storage/activation failed; keeping previous resources");}
}
static void task_loop_device_bootstrap(void*) {
  // Discovery must succeed before either media connection starts. Network or
  // configuration failures retry without inventing fallback service paths.
  for (;;) {
    if (WiFi.status()==WL_CONNECTED) sync_once();
    if (s_stream_enabled) break;
    vTaskDelay(pdMS_TO_TICKS(10000));
  }
  display_assets_changed();
  s_done.store(true,std::memory_order_release);
  vTaskSuspend(nullptr);
}
}
bool setup_device_bootstrap(){return true;}
bool task_setup_device_bootstrap() {
  if(s_task)return true;
  return utils_task_create_pinned(task_loop_device_bootstrap,"bootstrap",kTaskStack,nullptr,3,&s_task,
                                  APP_CPU_NUM,UtilsTaskStack::internal)==pdPASS;
}
bool device_bootstrap_done(){return s_done.load(std::memory_order_acquire);}
bool device_bootstrap_stream_enabled(){return device_bootstrap_done()&&s_stream_enabled;}
bool device_bootstrap_camera_enabled(){return device_bootstrap_done()&&s_camera_enabled;}
const char* device_bootstrap_stream_url(){return s_stream_url.c_str();}
const char* device_bootstrap_camera_url(){return s_camera_url.c_str();}
String device_bootstrap_status_json() {
  JsonDocument doc;doc["type"]="resource_status";doc["ready"]=device_bootstrap_done();
  if (device_bootstrap_done()) {
    doc["source"]=s_source;doc["result"]=s_result;doc["slot"]=s_active_slot;
    doc["version"]=face_assets_version();doc["stream_enabled"]=s_stream_enabled;doc["camera_enabled"]=s_camera_enabled;
  }
  String json;serializeJson(doc,json);return json;
}
