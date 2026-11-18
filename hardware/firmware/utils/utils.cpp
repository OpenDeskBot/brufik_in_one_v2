#include "utils.h"
#include "server_endpoint.h"

#include "deskbot_config.h"
#include "logger.h"
#include "nvs_config_utils.h"

#include <FFat.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <stdlib.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_mac.h"
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

namespace {
constexpr size_t kMaxPackedJsonLen = DESKBOT_MAX_PACKED_JSON_LEN;
}

bool parse_packed_frame(uint8_t* data, size_t length, PackedFrame& out) {
  out.bin = nullptr;
  out.bin_len = 0;
  if (data == nullptr || length < 4) {
    log_warn("[UTILS] packed frame too short len=%u", (unsigned)length);
    return false;
  }
  const size_t json_len =
      ((size_t)data[0] << 24) | ((size_t)data[1] << 16) | ((size_t)data[2] << 8) | (size_t)data[3];
  if (json_len == 0 || json_len > kMaxPackedJsonLen || 4 + json_len > length) {
    log_warn("[UTILS] packed json_len invalid %u total=%u", (unsigned)json_len, (unsigned)length);
    return false;
  }
  if (data[4] != '{') {
    log_warn("[UTILS] packed frame json does not start with '{'");
    return false;
  }
  const DeserializationError jerr = deserializeJson(out.doc, data + 4, json_len);
  if (jerr) {
    log_warn("[UTILS] packed deserialize failed len=%u err=%s", (unsigned)json_len, jerr.c_str());
    return false;
  }
  out.bin = data + 4 + json_len;
  out.bin_len = static_cast<int>(length - 4 - json_len);
  return true;
}

uint8_t* new_packed_bin(const char* json, const uint8_t* bin, size_t bin_len, size_t* out_len) {
  if (out_len) {
    *out_len = 0;
  }
  if (!json) {
    return nullptr;
  }
  const size_t json_len = strlen(json);
  if (json_len == 0 || json_len > kMaxPackedJsonLen) {
    log_warn("[UTILS] new_packed_bin bad json_len=%u", (unsigned)json_len);
    return nullptr;
  }
  if (bin_len > 0 && bin == nullptr) {
    return nullptr;
  }
  const size_t total = 4u + json_len + bin_len;
  uint8_t* frame = (uint8_t*)heap_caps_malloc(total, MALLOC_CAP_SPIRAM);
  if (!frame) {
    log_warn("[UTILS] new_packed_bin alloc fail total=%u", (unsigned)total);
    return nullptr;
  }
  frame[0] = (uint8_t)((json_len >> 24) & 0xFFu);
  frame[1] = (uint8_t)((json_len >> 16) & 0xFFu);
  frame[2] = (uint8_t)((json_len >> 8) & 0xFFu);
  frame[3] = (uint8_t)(json_len & 0xFFu);
  memcpy(frame + 4, json, json_len);
  if (bin_len > 0) {
    memcpy(frame + 4 + json_len, bin, bin_len);
  }
  if (out_len) {
    *out_len = total;
  }
  return frame;
}

void setup_FFat() {
  if (!FFat.begin(true)) {
    log_error("[FFAT] begin failed (check partition deskbot_rom_8MB.csv); FS unavailable");
    return;
  }
  log_info("[FFAT] ready");
}

const char* get_device_id() {
  static char s_id[32];
  static uint32_t s_cached_suffix = 0;
  const uint32_t suffix = nvs_get_device_suffix();
  if (s_cached_suffix != suffix) {
    uint8_t mac[6] = {0};
    (void)esp_read_mac(mac, ESP_MAC_WIFI_STA);
    /* 格式：brfk_ + mac(12位hex) + 随机4位数字，后缀存 NVS 保持重启不变 */
    snprintf(s_id, sizeof(s_id), "brfk_%02x%02x%02x%02x%02x%02x%04u", mac[0], mac[1], mac[2], mac[3],
             mac[4], mac[5], suffix);
    s_cached_suffix = suffix;
  }
  return s_id;
}

String json_escape(const String& raw) {
  String out;
  out.reserve(raw.length() + 8);
  for (size_t i = 0; i < raw.length(); ++i) {
    const char c = raw.charAt(i);
    switch (c) {
      case '\\':
        out += "\\\\";
        break;
      case '"':
        out += "\\\"";
        break;
      case '\b':
        out += "\\b";
        break;
      case '\f':
        out += "\\f";
        break;
      case '\n':
        out += "\\n";
        break;
      case '\r':
        out += "\\r";
        break;
      case '\t':
        out += "\\t";
        break;
      default:
        if ((uint8_t)c < 0x20) {
          char buf[7];
          snprintf(buf, sizeof(buf), "\\u%04x", (unsigned)c);
          out += buf;
        } else {
          out += c;
        }
        break;
    }
  }
  return out;
}

bool parse_ws_proto(const char* str, WsProto& out) {
  out = {};
  server_endpoint::Url parsed;
  if (!str || !server_endpoint::parse(str, parsed) ||
      (parsed.scheme != "ws" && parsed.scheme != "wss")) return false;
  out.is_wss = parsed.secure();
  out.port = parsed.port;
  strlcpy(out.host, parsed.host.c_str(), sizeof(out.host));
  strlcpy(out.path, parsed.path.c_str(), sizeof(out.path));
  return true;
}

String build_device_ws_path(const WsProto& target) {
  String path(server_endpoint::with_device(target.path, get_device_id()).c_str());
  path += "&version=";
  path += VERSION;
  return path;
}

bool utils_http_get_binary(const char* url, uint8_t** out_buf, size_t* out_len) {
  if (out_buf) {
    *out_buf = nullptr;
  }
  if (out_len) {
    *out_len = 0;
  }
  if (url == nullptr || url[0] == 0 || out_buf == nullptr || out_len == nullptr) {
    log_error("[UTILS] http_get: bad args");
    return false;
  }

  log_info("[UTILS] HTTP GET %s", url);

  HTTPClient http;
  const bool is_https = (strncmp(url, "https://", 8) == 0);
  WiFiClientSecure secure_client;
  bool begin_ok;
  if (is_https) {
    secure_client.setInsecure();
    begin_ok = http.begin(secure_client, url);
  } else {
    begin_ok = http.begin(url);
  }
  if (!begin_ok) {
    log_error("[UTILS] http.begin failed");
    return false;
  }
  http.setTimeout(60000);

  const int code = http.GET();
  if (code != 200) {
    log_error("[UTILS] HTTP %d", code);
    http.end();
    return false;
  }

  const int clen = http.getSize();
  const size_t cap = (clen > 0 ? (size_t)clen : (size_t)(512 * 1024)) + 16;
  uint8_t* buf = (uint8_t*)heap_caps_malloc(cap, MALLOC_CAP_SPIRAM);
  if (!buf) {
    log_error("[UTILS] PSRAM alloc %u failed (free=%u)", (unsigned)cap,
              (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    http.end();
    return false;
  }

  WiFiClient* stream = http.getStreamPtr();
  size_t got = 0;
  unsigned long t0 = millis();
  constexpr unsigned long kReadTotalMs = 60000;
  while (true) {
    if (clen > 0 && got >= (size_t)clen) {
      break;
    }
    const size_t room = cap - 1 - got;
    if (room == 0) {
      break;
    }
    const int n = stream->available();
    if (n > 0) {
      const int r =
          stream->readBytes(reinterpret_cast<char*>(buf + got), (n > (int)room) ? (int)room : n);
      if (r > 0) {
        got += (size_t)r;
        t0 = millis();
        continue;
      }
    }
    if (clen < 0 && !stream->connected() && stream->available() == 0) {
      break;
    }
    if (millis() - t0 > kReadTotalMs) {
      log_error("[UTILS] read timeout got=%u clen=%d", (unsigned)got, clen);
      break;
    }
    delay(5);
  }
  http.end();
  log_info("[UTILS] body read=%uB (clen=%d)", (unsigned)got, clen);

  if (got == 0) {
    heap_caps_free(buf);
    return false;
  }

  *out_buf = buf;
  *out_len = got;
  return true;
}

BaseType_t utils_task_create_pinned(TaskFunction_t fn, const char* name, uint32_t stack_bytes,
                                    void* arg, UBaseType_t prio, TaskHandle_t* out_handle,
                                    BaseType_t core_id, UtilsTaskStack memory) {
  if (!fn || !name || stack_bytes < 1024) {
    return pdFAIL;
  }
  /*
   * Arduino 3.x / IDF 5.5：CONFIG_FREERTOS_TASK_CREATE_ALLOW_EXT_MEM=y，大栈放 PSRAM，
   * 避免 mic/speaker/ws/display 吃光内部 SRAM 后 pb_runtime/camera 创建失败。
   * TCB 仍须在内部 RAM。
   */
  // NVS/FFat operations disable the external-memory cache. Their owner tasks
  // MUST keep their stack in internal RAM, even when PSRAM stacks are enabled.
  const bool internal = memory == UtilsTaskStack::internal;
  const uint32_t caps = (internal ? MALLOC_CAP_INTERNAL : MALLOC_CAP_SPIRAM) | MALLOC_CAP_8BIT;
  StackType_t* stack = static_cast<StackType_t*>(heap_caps_malloc(stack_bytes, caps));
  StaticTask_t* tcb = static_cast<StaticTask_t*>(
      heap_caps_malloc(sizeof(StaticTask_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
  if (stack && tcb) {
    TaskHandle_t handle =
        xTaskCreateStaticPinnedToCore(fn, name, stack_bytes, arg, prio, stack, tcb, core_id);
    if (handle) {
      if (out_handle) {
        *out_handle = handle;
      }
      log_warn("[TASK] %s stack=%uB memory=%s", name, (unsigned)stack_bytes, internal ? "internal" : "PSRAM");
      return pdPASS;
    }
  }
  if (stack) {
    heap_caps_free(stack);
  }
  if (tcb) {
    heap_caps_free(tcb);
  }
  /* 回落动态创建（可能仍走内部堆）。 */
  log_warn("[TASK] %s requested stack unavailable; fallback=internal stack=%uB", name,
           (unsigned)stack_bytes);
  return xTaskCreatePinnedToCore(fn, name, stack_bytes, arg, prio, out_handle, core_id);
}

void* psram_malloc(size_t sz) {
  void* p = heap_caps_malloc(sz, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  return p ? p : malloc(sz);
}

void safe_copy(char* dst, size_t cap, const char* src) {
  strncpy(dst, src, cap);
  dst[cap - 1] = '\0';
}
