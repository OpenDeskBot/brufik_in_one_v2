#include "usb_bench.h"
#include "audio_frontend_esp_sr.h"
#include "cmd.h"
#include "face_assets.h"
#include "logger.h"
#include "mic.h"
#include "pb_runtime.h"
#include "speaker.h"
#include "utils/utils.h"
#include <Arduino.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <atomic>
#include <esp_heap_caps.h>
#include <freertos/queue.h>
#include <mbedtls/base64.h>

namespace {
constexpr size_t kLineCapacity = 32768;
constexpr size_t kTxCapacity = 1400;
// JSON/base64 decoding runs off loopTask; PB remains the playback owner.
constexpr uint32_t kUsbTaskStack = 12 * 1024;
struct Tx { uint16_t length; uint8_t bytes[kTxCapacity]; };
struct Control { uint16_t length; uint8_t bytes[512]; };
static bool s_active = false;
static char* s_line = nullptr;
static uint8_t* s_binary = nullptr;
static size_t s_binary_length=0, s_binary_received=0;
constexpr size_t kBinaryCapacity=65536;
static size_t s_length = 0;
static bool s_discard_line = false;
static QueueHandle_t s_tx_queue = nullptr, s_control_queue = nullptr;
static TaskHandle_t s_task = nullptr;
static std::atomic<uint32_t> s_tx_drops{0};

static void status() {
  char json[384];
  snprintf(json, sizeof(json),
      "{\"usb\":\"ready\",\"mode\":\"usb\",\"usb_protocol\":2,\"version\":\"%s\",\"device_ms\":%u,"
      "\"afe_ready\":%s,\"afe_healthy\":%s,\"aec_enabled\":%s,\"aec_delay_ms\":%u,\"tx_drops\":%u}",
      face_assets_version(), (unsigned)millis(), audio_frontend_ready()?"true":"false",
      audio_frontend_output_healthy()?"true":"false",
      audio_frontend_bench_aec_enabled()?"true":"false",
      (unsigned)audio_frontend_reference_delay_ms(), (unsigned)s_tx_drops.load());
  usb_bench_emit(json);
}

static void process_binary() {
  if (s_binary_length<4) { usb_bench_emit("{\"usb\":\"error\",\"reason\":\"short_packet\"}"); return; }
  const uint32_t json_length=(uint32_t(s_binary[0])<<24)|(uint32_t(s_binary[1])<<16)|
      (uint32_t(s_binary[2])<<8)|s_binary[3];
  JsonDocument header;
  if (json_length>s_binary_length-4 || deserializeJson(header,reinterpret_cast<const char*>(s_binary+4),json_length)) {
    usb_bench_emit("{\"usb\":\"error\",\"reason\":\"invalid_packet\"}"); return;
  }
  const int idx=header["idx"]|0;
  auto* owned=static_cast<uint8_t*>(heap_caps_malloc(s_binary_length,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
  if (!owned) { usb_bench_emit("{\"usb\":\"error\",\"reason\":\"no_memory\"}"); return; }
  memcpy(owned,s_binary,s_binary_length);
  const bool accepted=pb_runtime_enqueue_frame(owned,s_binary_length);
  if (!accepted) free(owned);
  char result[128];
  snprintf(result,sizeof(result),"{\"usb\":\"queued\",\"idx\":%d,\"accepted\":%s}",idx,accepted?"true":"false");
  usb_bench_emit(result);
}

static void process_line() {
  if (!s_length) return;
  s_line[s_length] = '\0';
  if (s_line[0] != '{') {
    if (!strcmp(s_line, "usb_status")) status();
    else executeFactoryCommand(String(s_line));
    return;
  }
  JsonDocument doc;
  if (deserializeJson(doc, s_line)) { usb_bench_emit("{\"usb\":\"error\",\"reason\":\"invalid_json\"}"); return; }
  if (!doc["pb"].is<JsonObject>()) { usb_bench_emit("{\"usb\":\"error\",\"reason\":\"missing_pb\"}"); return; }
  const char* b64 = doc["b64"] | "";
  const size_t encoded = strlen(b64);
  const size_t capacity = encoded / 4 * 3 + 3;
  auto* pcm = static_cast<uint8_t*>(heap_caps_malloc(capacity, MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
  if (!pcm) { usb_bench_emit("{\"usb\":\"error\",\"reason\":\"no_memory\"}"); return; }
  size_t actual = 0;
  const int rc = encoded ? mbedtls_base64_decode(pcm,capacity,&actual,
      reinterpret_cast<const unsigned char*>(b64),encoded) : 0;
  if (rc) { heap_caps_free(pcm); usb_bench_emit("{\"usb\":\"error\",\"reason\":\"invalid_base64\"}"); return; }
  String json; serializeJson(doc["pb"], json);
  size_t length = 0;
  auto* packed = new_packed_bin(json.c_str(), pcm, actual, &length);
  heap_caps_free(pcm);
  const bool accepted = packed && pb_runtime_enqueue_frame(packed,length);
  if (packed && !accepted) free(packed);
  char result[192];
  snprintf(result,sizeof(result),"{\"usb\":\"queued\",\"idx\":%d,\"accepted\":%s}",
      doc["pb"]["idx"]|0, accepted?"true":"false");
  usb_bench_emit(result);
}

static void task_loop_usb_bench(void*) {
  status();
  Tx tx;
  size_t tx_offset=0;
  bool sending=false;
  for (;;) {
    // Bounded work per turn; TX must drain even during a large incoming block.
    uint8_t incoming[2048];
    const int available=Serial.available();
    const size_t count=available>0?Serial.read(incoming,available>2048?2048:available):0;
    for (size_t i=0; i<count; ++i) {
      if (s_binary_length) {
        size_t take=count-i;
        if (take>s_binary_length-s_binary_received) take=s_binary_length-s_binary_received;
        memcpy(s_binary+s_binary_received,incoming+i,take);
        s_binary_received+=take; i+=take-1;
        if (s_binary_received==s_binary_length) {
          process_binary(); s_binary_length=s_binary_received=0;
        }
        continue;
      }
      const int value=incoming[i];
      if (s_length>=8 && !memcmp(s_line,"DBPB0001",8)) {
        s_line[s_length++]=static_cast<char>(value);
        if (s_length==12) {
          const auto* size=reinterpret_cast<const uint8_t*>(s_line+8);
          const uint32_t length=(uint32_t(size[0])<<24)|(uint32_t(size[1])<<16)|(uint32_t(size[2])<<8)|size[3];
          s_length=0;
          if (length>=4 && length<=kBinaryCapacity) s_binary_length=length;
          else usb_bench_emit("{\"usb\":\"error\",\"reason\":\"invalid_binary_length\"}");
        }
        continue;
      }
      if (value=='\r') continue;
      if (value=='\n') {
        if (!s_discard_line) process_line();
        else usb_bench_emit("{\"usb\":\"error\",\"reason\":\"line_too_long\"}");
        s_length=0; s_discard_line=false;
      } else if (!s_discard_line) {
        if (s_length+1<kLineCapacity) s_line[s_length++]=static_cast<char>(value);
        else s_discard_line=true;
      }
    }
    // Preserve partial USB writes. Dropping a half-written binary packet also
    // corrupts the following JSON ACK; only whole queued records may be lost.
    for (unsigned i=0; i<8; ++i) {
      if (!sending) {
        Control control;
        if (xQueueReceive(s_control_queue,&control,0)==pdTRUE) {
          tx.length=control.length; memcpy(tx.bytes,control.bytes,control.length);
        } else if (xQueueReceive(s_tx_queue,&tx,0)!=pdTRUE) break;
        sending=true; tx_offset=0;
      }
      tx_offset+=Serial.write(tx.bytes+tx_offset,tx.length-tx_offset);
      if (tx_offset<tx.length) break;
      sending=false;
    }
    vTaskDelay(pdMS_TO_TICKS(1));
  }
}
}  // namespace

bool setup_usb_bench() {
  Preferences prefs;
  if (prefs.begin("usbbench",true)) { s_active=prefs.getBool("enabled",false); prefs.end(); }
  if (!s_active) return true;
  Serial.setRxBufferSize(16384);
  Serial.setTxBufferSize(8192);
  if (!s_binary) s_binary=static_cast<uint8_t*>(heap_caps_malloc(kBinaryCapacity,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
  if (!s_line) s_line=static_cast<char*>(heap_caps_malloc(kLineCapacity,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
  if (!s_tx_queue) s_tx_queue=xQueueCreate(8,sizeof(Tx));
  if (!s_control_queue) s_control_queue=xQueueCreate(12,sizeof(Control));
  return s_line && s_binary && s_tx_queue && s_control_queue;
}
void task_setup_usb_bench() {
  if (!s_active || s_task) return;
  if (!s_line || !s_binary || !s_tx_queue || !s_control_queue) { log_error("[USB] setup_usb_bench failed"); return; }
  utils_task_create_pinned(task_loop_usb_bench,"usb_bench",kUsbTaskStack,nullptr,7,&s_task,
                           APP_CPU_NUM,UtilsTaskStack::internal);
}
bool usb_bench_active() { return s_active; }
void usb_bench_set_mode(bool enabled) {
  Preferences prefs;
  if (!prefs.begin("usbbench",false)) return;
  const bool saved=prefs.putBool("enabled",enabled)>0;
  prefs.end();
  if (saved) { delay(30); ESP.restart(); }
}
bool usb_bench_write(const void* bytes,size_t length) {
  if (!s_active) return false;
  if (!length || length>kTxCapacity) { s_tx_drops.fetch_add(1); return false; }
  Tx tx; tx.length=length; memcpy(tx.bytes,bytes,length);
  if (xQueueSend(s_tx_queue,&tx,0)==pdTRUE) return true;
  s_tx_drops.fetch_add(1); return false;
}
void usb_bench_emit(const char* json) {
  if (!s_active) return;
  const size_t length=strlen(json);
  if (length+1<=512 && !strstr(json,"\"mouth\"") && !strstr(json,"\"aec_stats\"")) {
    Control control; control.length=length+1;
    memcpy(control.bytes,json,length); control.bytes[length]='\n';
    if (xQueueSend(s_control_queue,&control,0)!=pdTRUE) s_tx_drops.fetch_add(1);
    return;
  }
  Tx tx;
  if (length+1>kTxCapacity) { s_tx_drops.fetch_add(1); return; }
  tx.length=length+1; memcpy(tx.bytes,json,length); tx.bytes[length]='\n';
  if (xQueueSend(s_tx_queue,&tx,0)!=pdTRUE) s_tx_drops.fetch_add(1);
}
uint32_t usb_bench_tx_drops() { return s_tx_drops.load(); }
