#include "ws_transport.h"
#include "media_wire.h"
#include "device_bootstrap.h"
#include "server_tls.h"
#include "usb_bench.h"

#include "deskbot_config.h"
#include "face_assets.h"
#include "logger.h"
#include "mic.h"
#include "pb_runtime.h"
#include "speaker.h"
#include "utils/nvs_config_utils.h"
#include "utils/opus_codec.h"
#include "utils/utils.h"

#include <Arduino.h>
#include <WiFi.h>
#include <atomic>
#include <esp_heap_caps.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <string.h>

static WsProto s_server_target;
static String s_server_path;

namespace {

WebSocketsClient ws_client;
std::atomic<int> ws_state{static_cast<int>(WsState::kDisconnected)};
std::atomic<bool> s_app_ready{false};
std::atomic<bool> s_raw_preview_supported{false};
bool s_server_hello_seen = false;
std::atomic<bool> s_compact_supported{false}, s_binary_audio{false}, s_binary_ack{false};
static std::atomic<uint32_t> s_uplink_idx{0}, s_uplink_pts{0};

static void reset_media_caps() {
  s_compact_supported.store(false); s_binary_audio.store(false); s_binary_ack.store(false);
  s_uplink_idx=0; s_uplink_pts=0;
}
static void accept_cap(const char* name) {
  if (!name) return;
  if (strcmp(name,"raw_preview_pcm_v1")==0) s_raw_preview_supported.store(true);
  if (strcmp(name,"pb_compact_v1")==0) s_compact_supported.store(true);
  if (strcmp(name,"audio_bin_v1")==0) s_binary_audio.store(true);
  if (strcmp(name,"ack_bin_v1")==0) s_binary_ack.store(true);
}

static std::atomic<uint32_t> s_ws_session{0};

/** 入队前已编码为基础 JSON+media 或协商二进制帧；发送只 sendBIN。 */
struct WsTxItem {
  WsTxType type = WsTxType::kState;
  uint8_t* packed = nullptr;
  size_t packed_len = 0;
  uint32_t session = s_ws_session.load();
};

static bool s_setup_ok = false;
static bool s_bootstrap_path_ready = false;
static QueueHandle_t s_tx_q = nullptr;
static QueueHandle_t s_control_q = nullptr;
static TaskHandle_t s_task = nullptr;
static std::atomic<bool> s_disconnect_requested{false};
static bool s_boot_connect_sent = false;
static unsigned long s_connect_attempt_ms = 0;
static unsigned long s_last_reconnect_ms = 0;
static uint8_t s_connect_fail_count = 0;
static constexpr unsigned long RECONNECT_MIN_MS = 500;
static constexpr unsigned long RECONNECT_MAX_MS = 60000;

static constexpr UBaseType_t kTxDepth = 32;
/* WebSockets 收发、ArduinoJson 解析都在 ws_transport 任务。 */
static constexpr uint32_t kTaskStack = 32 * 1024;
/* 与 mic(6) 同级：持续上行时 drain 不能被 mic encode 饿死。 */
static constexpr UBaseType_t kTaskPrio = 6;
static constexpr size_t kMaxRxCopy = 256 * 1024;
static constexpr size_t kMaxTxJson = 16 * 1024;
static constexpr size_t kMaxTxAudioBin = 4 * 1024;

static void* rx_alloc(size_t n) {
  return heap_caps_malloc(n, MALLOC_CAP_SPIRAM);
}

static void ws_tx_free_item(WsTxItem* item) {
  if (!item) {
    return;
  }
  if (item->packed) {
    free(item->packed);
    item->packed = nullptr;
  }
  item->packed_len = 0;
}

static void clear_tx_queue(void) {
  WsTxItem item{};
  while (xQueueReceive(s_tx_q, &item, 0) == pdTRUE) ws_tx_free_item(&item);
  while (xQueueReceive(s_control_q, &item, 0) == pdTRUE) ws_tx_free_item(&item);
}

static bool enqueue_tx(WsTxItem* item) {
  if (!item || !item->packed || item->packed_len == 0) {
    ws_tx_free_item(item);
    return false;
  }
  if (item->session != s_ws_session.load()) { ws_tx_free_item(item); return false; }
  const QueueHandle_t queue = item->type == WsTxType::kState ? s_control_q : s_tx_q;
  if (xQueueSend(queue, item, 0) == pdTRUE) {
    return true;
  }
  log_warn("[WS_TRANSPORT] TX queue full type=%u len=%u", (unsigned)item->type, (unsigned)item->packed_len);
  ws_tx_free_item(item);
  return false;
}

static bool build_tx_item(WsTxType type, const char* json, const uint8_t* bin, size_t bin_len,
                          size_t max_bin, WsTxItem* out) {
  if (!json || !out) {
    return false;
  }
  const size_t n = strlen(json);
  if (n == 0 || n > kMaxTxJson) {
    return false;
  }
  if (bin_len > max_bin || (bin_len > 0 && bin == nullptr)) {
    if (bin_len > max_bin) {
      log_warn("[WS_TRANSPORT] TX bin too large type=%u len=%u max=%u", (unsigned)type,
               (unsigned)bin_len, (unsigned)max_bin);
    }
    return false;
  }
  *out = {};
  out->type = type;
  out->packed = new_packed_bin(json, bin, bin_len, &out->packed_len);
  return out->packed != nullptr;
}

static void task_loop_ws_transport(void* /*arg*/) {
  for (;;) {
    if (s_disconnect_requested.exchange(false, std::memory_order_acq_rel)) {
      ws_client.disconnect();
    }
    ws_transport_ensure_connected();
    ws_client.loop();
    const bool sent = ws_transport_drain_tx();
    /* 空闲让出 CPU，避免同核饿死 WiFi/其它任务；有 TX 或建连中则只 yield。 */
    if (sent || ws_transport_state() == WsState::kConnecting) {
      taskYIELD();
    } else {
      vTaskDelay(pdMS_TO_TICKS(2));
    }
  }
}

}  // namespace

bool setup_ws_transport(void) {
  if (s_setup_ok) {
    log_info("[WS_TRANSPORT] setup already ok");
    return true;
  }

  s_tx_q = xQueueCreate(kTxDepth, sizeof(WsTxItem));
  s_control_q = xQueueCreate(16, sizeof(WsTxItem));
  if (!s_tx_q || !s_control_q) return false;
  ws_client.onEvent([](WStype_t type, uint8_t* payload, size_t length) {
    if (type == WStype_CONNECTED) {
      reset_media_caps();
      s_raw_preview_supported.store(false, std::memory_order_release);
      s_server_hello_seen = false;
      ws_state.store(static_cast<int>(WsState::kConnected), std::memory_order_release);
      s_connect_fail_count = 0;
      s_last_reconnect_ms = 0;
      s_app_ready.store(true, std::memory_order_release);
      s_connect_attempt_ms = 0;
      char hello[512];
      snprintf(hello, sizeof(hello),
               "{\"type\":\"device_hello\",\"capabilities\":[\"pb_stream_v1\",\"pb_epoch_v1\",\"pb_playback_progress_v1\",\"raw_preview_pcm_v1\",\"anim_id_v1\",\"phoneme_id_v1\",\"anim_frame_id_v1\",\"pcm_lipsync_v1\",\"local_face_pack_v1\",\"local_idle_v1\",\"pb_compact_v1\",\"audio_bin_v1\",\"ack_bin_v1\",\"rom_tools_v1\",\"image_rgb565_v1\"],\"anim_version\":\"%s\"}",
               face_assets_version());
      (void)ws_transport_enqueue_state(hello);
      mic_set_ws_state(kMicWsOk);
      (void)opus_codec_decode_init();
      if (!s_boot_connect_sent) {
        if (ws_transport_enqueue_state("{\"type\":\"boot_connect\"}")) {
          s_boot_connect_sent = true;
          log_info("[WS_TRANSPORT] connected → boot_connect enqueued (first power-on)");
        } else {
          log_warn("[WS_TRANSPORT] connected → boot_connect enqueue failed");
        }
      }
      log_info("[WS_TRANSPORT] connected");
      return;
    }
    if (type == WStype_DISCONNECTED) {
      reset_media_caps();
      s_raw_preview_supported.store(false, std::memory_order_release);
      ws_state.store(static_cast<int>(WsState::kDisconnected), std::memory_order_release);
      s_app_ready.store(false, std::memory_order_release);
      s_connect_attempt_ms = 0;
      mic_set_ws_state(kMicWsError);
      log_warn("[WS_TRANSPORT] disconnected");
      return;
    }
    if (type == WStype_TEXT && length < 512) {
      JsonDocument hello;
      if (deserializeJson(hello, payload, length) == DeserializationError::Ok &&
          strcmp(hello["type"] | "", "ready") == 0) {
        s_server_hello_seen = true;
        for (JsonVariant cap : hello["capabilities"].as<JsonArray>()) {
          const char* name = cap.as<const char*>();
          accept_cap(name);
        }
        log_warn("[WS_TRANSPORT] server ready raw_preview=%d compact=%d audio_bin=%d ack_bin=%d",
                 (int)s_raw_preview_supported.load(), (int)s_compact_supported.load(),
                 (int)s_binary_audio.load(), (int)s_binary_ack.load());
        return;
      }
    }
    if (type == WStype_BIN) {
      if (payload == nullptr || length == 0) {
        return;
      }
      if (!s_server_hello_seen) {
        s_server_hello_seen = true;
        PackedFrame hello;
        if (parse_packed_frame(payload, length, hello) &&
            strcmp(hello.doc["type"] | "", "ready") == 0) {
          for (JsonVariant cap : hello.doc["capabilities"].as<JsonArray>()) {
            const char* name = cap.as<const char*>();
            accept_cap(name);
          }
          return;
        }
      }
      if (length > kMaxRxCopy) {
        log_warn("[WS_TRANSPORT] RX drop oversized len=%u", (unsigned)length);
        return;
      }
      uint8_t* data = (uint8_t*)rx_alloc(length + 1);
      if (!data) {
        log_warn("[WS_TRANSPORT] RX alloc fail len=%u", (unsigned)length);
        return;
      }
      memcpy(data, payload, length);
      data[length] = '\0';
      static uint32_t s_last_rx_hand_ms = 0;
      const uint32_t now = millis();
      const uint32_t gap = (s_last_rx_hand_ms == 0) ? 0 : (now - s_last_rx_hand_ms);
      s_last_rx_hand_ms = now;
      log_warn("[PB_LAT] rx_to_pb len=%u gap_ms=%u", (unsigned)length, (unsigned)gap);
      if (!pb_runtime_enqueue_frame(data, length)) {
        free(data);
        log_warn("[PB_LAT] rx_to_pb DROP (pb not ready or frame_q full) len=%u",
                 (unsigned)length);
      }
      return;
    }
    log_warn("[WS_TRANSPORT] ignore non-BIN type=%d len=%u", (int)type, (unsigned)length);
  });
  s_setup_ok = true;
  log_info("[WS_TRANSPORT] setup ok TX depth=%u", (unsigned)kTxDepth);
  return true;
}

bool task_setup_ws_transport(void) {
  if (!s_setup_ok) {
    log_error("[WS_TRANSPORT] task_setup skipped (setup not ok)");
    return false;
  }
  if (s_task) {
    return true;
  }
  BaseType_t rc = utils_task_create_pinned(task_loop_ws_transport, "ws_transport", kTaskStack, nullptr,
                                           kTaskPrio, &s_task, APP_CPU_NUM);
  if (rc != pdPASS) {
    log_error("[WS_TRANSPORT] task create failed rc=%d (internal free=%u)", (int)rc,
              (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    s_task = nullptr;
    return false;
  }
  log_info("[WS_TRANSPORT] task OK stack=%u prio=%u", (unsigned)kTaskStack, (unsigned)kTaskPrio);
  return true;
}

WsState ws_transport_state(void) {
  return static_cast<WsState>(ws_state.load(std::memory_order_acquire));
}

bool ws_transport_ok(void) {
  return ws_transport_state() == WsState::kConnected;
}

bool ws_transport_raw_preview_supported(void) {
  return s_raw_preview_supported.load(std::memory_order_acquire);
}

bool ws_transport_ready(void) {
  return s_app_ready.load(std::memory_order_acquire);
}

void ws_transport_ensure_connected(void) {
  if (!device_bootstrap_stream_enabled()) return;
  if (!s_bootstrap_path_ready) {
    if (!parse_ws_proto(device_bootstrap_stream_url(),s_server_target)) return;
    s_server_path=build_device_ws_path(s_server_target);
    s_bootstrap_path_ready=true;
  }
  if (WiFi.status() != WL_CONNECTED) {
    ws_state.store(static_cast<int>(WsState::kDisconnected), std::memory_order_release);
    s_connect_attempt_ms = 0;
    return;
  }
  const WsState st = ws_transport_state();
  if (st == WsState::kConnected) {
    return;
  }
  if (s_server_target.host[0] == '\0' || s_server_path.length() == 0) {
    s_connect_attempt_ms = 0;
    return;
  }
  /* connecting：只泵 loop，避免每轮 disconnect+begin 打断连接。 */
  if (st == WsState::kConnecting) {
    if (s_connect_attempt_ms != 0 &&
        (millis() - s_connect_attempt_ms) < (unsigned long)DESKBOT_WS_CONNECT_TIMEOUT_MS) {
      return;
    }
    /* 超时：回到 disconnected，下一轮重新 begin。 */
    ws_state.store(static_cast<int>(WsState::kDisconnected), std::memory_order_release);
    s_connect_attempt_ms = 0;
  }

  /* 指数退避：500ms → 1s → 2s → 4s → 8s → 16s → 32s → 60s */
  unsigned long interval = RECONNECT_MIN_MS << s_connect_fail_count;
  if (interval > RECONNECT_MAX_MS) {
    interval = RECONNECT_MAX_MS;
  }
  const unsigned long now = millis();
  if (s_last_reconnect_ms != 0 && (now - s_last_reconnect_ms) < interval) {
    return; /* 还在退避窗口内，等下一轮 */
  }
  s_last_reconnect_ms = now;
  if (s_connect_fail_count < 8) {
    s_connect_fail_count++;
  }

  ws_client.disconnect();
  ws_transport_new_session();
  log_warn("[WS_TRANSPORT] connect %s://%s:%u%s (retry #%u, backoff %lus)",
           s_server_target.is_wss ? "wss" : "ws",
           s_server_target.host, (unsigned)s_server_target.port, s_server_path.c_str(),
           (unsigned)s_connect_fail_count, (unsigned)(interval / 1000));
  /*
   * 必须在 begin 前设好 interval。库 loop() 里用
   * (millis() - _lastConnectionFail) < _reconnectInterval 抑制重连；
   * begin() 会把 _lastConnectionFail 置 0，若 interval 设成「几天」则
   * (millis()-0) 一直小于 interval，TCP 永远不会发起。
   */
  ws_client.setReconnectInterval(interval);
  if (s_server_target.is_wss) {
    server_begin_wss(ws_client,s_server_target,s_server_path.c_str());
  } else {
    ws_client.begin(s_server_target.host, s_server_target.port, s_server_path.c_str());
  }
  ws_state.store(static_cast<int>(WsState::kConnecting), std::memory_order_release);
  s_connect_attempt_ms = millis();
}

bool ws_transport_send(const uint8_t* data, size_t len) {
  if (ws_transport_state() != WsState::kConnected) {
    return false;
  }
  if (!data || len == 0) {
    return false;
  }
  if (ws_client.sendBIN(data, len)) {
    return true;
  }
  ws_state.store(static_cast<int>(WsState::kDisconnected), std::memory_order_release);
  log_warn("[WS_TRANSPORT] sendBIN fail → disconnected len=%u", (unsigned)len);
  return false;
}

void ws_transport_on_link_down(const char* why) {
  log_warn("[WS_TRANSPORT] wifi down (%s)", why ? why : "?");
  s_disconnect_requested.store(true, std::memory_order_release);
  ws_state.store(static_cast<int>(WsState::kDisconnected), std::memory_order_release);
  s_app_ready.store(false, std::memory_order_release);
  s_connect_attempt_ms = 0;
  mic_set_ws_state(kMicWsError);
}

void ws_transport_on_link_up(void) {
  log_info("[WS_TRANSPORT] wifi up");
  ws_state.store(static_cast<int>(WsState::kDisconnected), std::memory_order_release);
  s_connect_attempt_ms = 0;
}

void ws_transport_new_session(void) {
  s_ws_session++;
  reset_media_caps();
  clear_tx_queue();
  s_app_ready.store(false, std::memory_order_release);
  mic_set_ws_state(kMicWsError);
  pb_runtime_abort_session();
  log_info("[WS_TRANSPORT] new session=%u (PB runtime abort requested)",
           (unsigned)s_ws_session.load());
}

bool ws_transport_enqueue_state(const char* json) {
  if (usb_bench_active()) { usb_bench_emit(json); return true; }
  WsTxItem item{};
  if (!build_tx_item(WsTxType::kState, json, nullptr, 0, 0, &item)) {
    return false;
  }
  return enqueue_tx(&item);
}

bool ws_transport_enqueue_audio(const char* json, const uint8_t* bin, size_t bin_len) {
  if (!ws_transport_ok() || !ws_transport_ready()) {
    return false;
  }
  WsTxItem item{};
  if (!build_tx_item(WsTxType::kAudio, json, bin, bin_len, kMaxTxAudioBin, &item)) {
    return false;
  }
  return enqueue_tx(&item);
}

bool ws_transport_enqueue_opus(const uint8_t* bin, size_t bin_len, uint16_t frames,
                               size_t opus_bytes, bool flush) {
  const uint32_t session=s_ws_session.load();
  if (!ws_transport_ok() || !ws_transport_ready()) return false;
  if (s_binary_audio.load() && !flush && bin && frames>0 && frames<=50 &&
      opus_bytes>0 && opus_bytes<=bin_len && bin_len<=kMaxTxAudioBin &&
      (bin_len==opus_bytes || bin_len-opus_bytes==frames*320u)) {
    WsTxItem item; item.session=session; item.type=WsTxType::kAudio;
    item.packed_len=media_wire::kAudioHeader+bin_len;
    item.packed=(uint8_t*)malloc(item.packed_len);
    if (!item.packed) return false;
    media_wire::Audio a; a.idx=s_uplink_idx.fetch_add(1);
    a.pts=s_uplink_pts.fetch_add(frames*20); a.frames=frames;
    a.duration=frames*20; a.bytes=opus_bytes;
    media_wire::audio_encode(item.packed,2,a);
    memcpy(item.packed+media_wire::kAudioHeader,bin,bin_len);
    return enqueue_tx(&item);
  }
  char hdr[256];
  snprintf(hdr,sizeof(hdr),
      "{\"type\":\"audio\",\"codec\":\"opus\",\"next_bin_len\":%u,\"sr\":16000,\"ch\":1,"
      "\"frames\":%u,\"flush\":%u,\"opus_bytes\":%u,\"raw_bytes\":%u,\"raw_sr\":8000}",
      (unsigned)bin_len,(unsigned)frames,flush ? 1u : 0u,(unsigned)opus_bytes,
      (unsigned)(bin_len-opus_bytes));
  return ws_transport_enqueue_audio(hdr,bin,bin_len);
}

bool ws_transport_drain_tx(void) {
  if (!ws_transport_ok()) {
    clear_tx_queue();
    return false;
  }

  WsTxItem item{};
  const bool control = xQueueReceive(s_control_q, &item, 0) == pdTRUE;
  if (control || xQueueReceive(s_tx_q, &item, 0) == pdTRUE) {
    if (item.session != s_ws_session.load()) { ws_tx_free_item(&item); return true; }
    const uint32_t t0 = millis();
    const size_t plen = item.packed_len;
    const bool ok = ws_transport_send(item.packed, plen);
    const uint32_t send_ms = millis() - t0;
    if (!ok) {
      log_warn("[WS_TRANSPORT] sendBIN fail type=%u len=%u send_ms=%u", (unsigned)item.type,
               (unsigned)plen, (unsigned)send_ms);
      if (xQueueSendToFront(control ? s_control_q : s_tx_q, &item, 0) != pdTRUE) {
        ws_tx_free_item(&item);
      }
      return false;
    }
    ws_tx_free_item(&item);
    return true;
  }
  return false;
}

bool ws_transport_compact_supported(void) { return s_compact_supported.load(); }
bool ws_transport_binary_audio_supported(void) { return s_binary_audio.load(); }
bool ws_transport_enqueue_ack(const JsonDocument& ack) {
  const uint32_t session=s_ws_session.load();
  const int code=media_wire::ack_code(ack["ack_type"] | "");
  const uint32_t epoch=ack["epoch"] | 0u;
  if (s_binary_ack.load() && epoch && code>=0 && ack["error"].isNull() && !usb_bench_active()) {
    WsTxItem item; item.session=session; item.packed_len=media_wire::kAckHeader;
    item.packed=(uint8_t*)malloc(item.packed_len);
    if (!item.packed) return false;
    media_wire::ack_encode(item.packed,epoch,ack["idx"] | 0u,ack["played_ms"] | 0u,
        ack["buffered_ms"] | 0u,ack["device_ms"] | 0u,ack["space"] | 0,
        code,!ack["played_ms"].isNull(),ack["queue_wait_ms"] | 0u);
    return enqueue_tx(&item);
  }
  String json; serializeJson(ack,json);
  return ws_transport_enqueue_state(json.c_str());
}
