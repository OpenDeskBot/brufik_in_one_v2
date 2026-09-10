#include "camera_ws_transport.h"

#include "deskbot_config.h"
#include "logger.h"
#include "utils/utils.h"
#include "ws_transport.h"

#include <Arduino.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <atomic>
#include <esp_heap_caps.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

namespace {

enum class LinkState : uint8_t { kDown, kConnecting, kUp };

struct Frame {
  uint8_t* data = nullptr;
  size_t len = 0;
};

WebSocketsClient s_client;
String s_path;
std::atomic<LinkState> s_state{LinkState::kDown};
std::atomic<bool> s_disconnect_requested{false};
std::atomic<uint32_t> s_overwrites{0};
std::atomic<uint8_t> s_rate_level{0};
portMUX_TYPE s_latest_mux = portMUX_INITIALIZER_UNLOCKED;
Frame s_latest;
TaskHandle_t s_task = nullptr;
bool s_setup_ok = false;
uint32_t s_connect_started_ms = 0;
uint32_t s_last_attempt_ms = 0;
uint8_t s_fail_count = 0;

constexpr uint32_t kIntervalsMs[] = {500, 1000, 2000, 4000};
constexpr uint8_t kMaxRateLevel = sizeof(kIntervalsMs) / sizeof(kIntervalsMs[0]) - 1;
constexpr uint32_t kReconnectMinMs = 500;
constexpr uint32_t kReconnectMaxMs = 60000;
constexpr uint32_t kTaskStack = 8 * 1024;
constexpr UBaseType_t kTaskPrio = 3;

uint32_t reconnect_interval_ms() {
  uint32_t interval = kReconnectMinMs << s_fail_count;
  return interval > kReconnectMaxMs ? kReconnectMaxMs : interval;
}

Frame take_latest() {
  portENTER_CRITICAL(&s_latest_mux);
  Frame frame = s_latest;
  s_latest = {};
  portEXIT_CRITICAL(&s_latest_mux);
  return frame;
}

void clear_latest() {
  Frame frame = take_latest();
  free(frame.data);
}

void set_rate_level(uint8_t level, const char* reason, uint32_t send_ms) {
  if (level > kMaxRateLevel) {
    level = kMaxRateLevel;
  }
  const uint8_t old = s_rate_level.exchange(level, std::memory_order_acq_rel);
  if (old != level) {
    log_warn("[CAMERA_RATE] interval %u->%ums reason=%s send_ms=%u overwrites=%u",
             (unsigned)kIntervalsMs[old], (unsigned)kIntervalsMs[level], reason,
             (unsigned)send_ms, (unsigned)s_overwrites.load(std::memory_order_relaxed));
  }
}

void adapt_rate(bool ok, uint32_t send_ms, uint32_t& seen_overwrites, uint32_t& ewma_ms,
                uint8_t& good_sends, uint32_t& last_change_ms) {
  ewma_ms = ewma_ms == 0 ? send_ms : (ewma_ms * 7u + send_ms) / 8u;
  const uint32_t overwrites = s_overwrites.load(std::memory_order_relaxed);
  const bool overwritten = overwrites != seen_overwrites;
  seen_overwrites = overwrites;
  const uint8_t level = s_rate_level.load(std::memory_order_relaxed);
  const uint32_t interval = kIntervalsMs[level];
  const bool heap_low = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT) < 12 * 1024;
  const bool congested = !ok || overwritten || heap_low || send_ms * 10u >= interval * 7u;

  if (congested) {
    const uint8_t step = (!ok || send_ms >= interval * 2u) ? 2 : 1;
    const char* reason = !ok          ? "send_fail"
                         : overwritten ? "latest_overwrite"
                         : heap_low    ? "internal_heap_low"
                                       : "slow_send";
    set_rate_level(level + step, reason, send_ms);
    good_sends = 0;
    last_change_ms = millis();
    return;
  }

  if (level > 0 && ewma_ms * 4u < interval && ++good_sends >= 24 &&
      millis() - last_change_ms >= 15000u) {
    set_rate_level(level - 1, "stable", send_ms);
    good_sends = 0;
    last_change_ms = millis();
  } else if (ewma_ms * 4u >= interval) {
    good_sends = 0;
  }
}

void ensure_connected() {
  if (WiFi.status() != WL_CONNECTED || server_ws_proto.host[0] == '\0' || s_path.isEmpty()) {
    s_state.store(LinkState::kDown, std::memory_order_release);
    s_connect_started_ms = 0;
    return;
  }
  LinkState state = s_state.load(std::memory_order_acquire);
  if (state == LinkState::kUp) {
    return;
  }
  const uint32_t now = millis();
  if (state == LinkState::kConnecting && s_connect_started_ms != 0 &&
      now - s_connect_started_ms < DESKBOT_WS_CONNECT_TIMEOUT_MS) {
    return;
  }
  if (state == LinkState::kConnecting) {
    s_state.store(LinkState::kDown, std::memory_order_release);
    s_connect_started_ms = 0;
  }

  const uint32_t backoff = reconnect_interval_ms();
  if (s_last_attempt_ms != 0 && now - s_last_attempt_ms < backoff) {
    return;
  }
  s_last_attempt_ms = now;
  if (s_fail_count < 8) {
    ++s_fail_count;
  }
  s_client.disconnect();
  s_client.setReconnectInterval(backoff);
  if (server_ws_proto.is_wss) {
    s_client.beginSSL(server_ws_proto.host, server_ws_proto.port, s_path.c_str());
  } else {
    s_client.begin(server_ws_proto.host, server_ws_proto.port, s_path.c_str());
  }
  s_state.store(LinkState::kConnecting, std::memory_order_release);
  s_connect_started_ms = millis();
  log_warn("[CAMERA_WS] connect %s://%s:%u%s backoff=%ums",
           server_ws_proto.is_wss ? "wss" : "ws", server_ws_proto.host,
           (unsigned)server_ws_proto.port, s_path.c_str(), (unsigned)backoff);
}

void task_loop(void*) {
  uint32_t seen_overwrites = 0;
  uint32_t ewma_ms = 0;
  uint32_t last_change_ms = 0;
  uint8_t good_sends = 0;

  for (;;) {
    if (s_disconnect_requested.exchange(false, std::memory_order_acq_rel)) {
      s_client.disconnect();
      s_state.store(LinkState::kDown, std::memory_order_release);
      clear_latest();
    }
    ensure_connected();
    s_client.loop();

    if (!camera_ws_transport_ready()) {
      clear_latest();
      ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(10));
      continue;
    }

    Frame frame = take_latest();
    if (!frame.data) {
      ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(2));
      continue;
    }

    const uint32_t started = millis();
    const bool ok = s_client.sendBIN(frame.data, frame.len);
    const uint32_t send_ms = millis() - started;
    free(frame.data);
    adapt_rate(ok, send_ms, seen_overwrites, ewma_ms, good_sends, last_change_ms);
    if (!ok) {
      s_client.disconnect();
      s_state.store(LinkState::kDown, std::memory_order_release);
      s_connect_started_ms = 0;
    }
    if (!ok || send_ms >= 250u) {
      log_warn("[CAMERA_WS] sent=%u len=%u send_ms=%u interval=%u pending_overwrites=%u",
               ok ? 1u : 0u, (unsigned)frame.len, (unsigned)send_ms,
               (unsigned)camera_ws_capture_interval_ms(),
               (unsigned)s_overwrites.load(std::memory_order_relaxed));
    }
  }
}

}  // namespace

bool setup_camera_ws_transport() {
  if (s_setup_ok) {
    return true;
  }
  if (server_ws_proto.host[0] == '\0') {
    log_warn("[CAMERA_WS] no configured server");
    return false;
  }
  s_path = build_device_ws_path(server_ws_proto, "/camera_uplink");
  s_client.onEvent([](WStype_t type, uint8_t*, size_t) {
    if (type == WStype_CONNECTED) {
      s_state.store(LinkState::kUp, std::memory_order_release);
      s_connect_started_ms = 0;
      s_last_attempt_ms = 0;
      s_fail_count = 0;
      log_warn("[CAMERA_WS] connected");
    } else if (type == WStype_DISCONNECTED) {
      s_state.store(LinkState::kDown, std::memory_order_release);
      s_connect_started_ms = 0;
      log_warn("[CAMERA_WS] disconnected");
    }
  });
  s_setup_ok = !s_path.isEmpty();
  return s_setup_ok;
}

bool task_setup_camera_ws_transport() {
  if (!s_setup_ok || s_task) {
    return s_setup_ok;
  }
  const BaseType_t rc = utils_task_create_pinned(task_loop, "camera_ws", kTaskStack, nullptr,
                                                 kTaskPrio, &s_task, APP_CPU_NUM);
  if (rc != pdPASS) {
    s_task = nullptr;
    log_error("[CAMERA_WS] task create failed rc=%d", (int)rc);
    return false;
  }
  return true;
}

bool camera_ws_transport_ready() {
  return s_state.load(std::memory_order_acquire) == LinkState::kUp;
}

void camera_ws_transport_on_link_down() {
  s_state.store(LinkState::kDown, std::memory_order_release);
  s_disconnect_requested.store(true, std::memory_order_release);
  if (s_task) {
    xTaskNotifyGive(s_task);
  }
}

void camera_ws_transport_on_link_up() {
  s_state.store(LinkState::kDown, std::memory_order_release);
  s_last_attempt_ms = 0;
  if (s_task) {
    xTaskNotifyGive(s_task);
  }
}

bool camera_ws_submit_latest(uint8_t* packed, size_t packed_len) {
  if (!packed || packed_len == 0 || !camera_ws_transport_ready()) {
    free(packed);
    return false;
  }

  portENTER_CRITICAL(&s_latest_mux);
  Frame old = s_latest;
  s_latest = {packed, packed_len};
  portEXIT_CRITICAL(&s_latest_mux);
  if (old.data) {
    free(old.data);
    s_overwrites.fetch_add(1, std::memory_order_relaxed);
  }
  if (s_task) {
    xTaskNotifyGive(s_task);
  }
  return true;
}

uint32_t camera_ws_capture_interval_ms() {
  return kIntervalsMs[s_rate_level.load(std::memory_order_relaxed)];
}
