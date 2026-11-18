#include "cmd.h"
#include "usb_bench.h"
#include "audio_frontend_esp_sr.h"
#include "face_assets.h"
#include "device_bootstrap.h"
#include "display.h"
#include "pb_runtime.h"
#include "logger.h"
#include "mic.h"
#include "speaker.h"
#include "task_trace.h"
#include "utils/nvs_config_utils.h"
#include "utils/utils.h"

/** 解析 cmd 中从首个空格开始的空格分隔整数，写入 out[0..max_n-1]，返回实际解析个数。 */
static int parse_int_args(const String& cmd, int* out, int max_n) {
  int n = 0;
  int i = cmd.indexOf(' ');
  while (n < max_n && i >= 0) {
    if (i + 1 >= (int)cmd.length()) break;
    int j = cmd.indexOf(' ', i + 1);
    String tok = (j < 0) ? cmd.substring(i + 1) : cmd.substring(i + 1, j);
    tok.trim();
    if (tok.length() == 0) break;
    out[n++] = tok.toInt();
    if (j < 0) break;
    i = j;
  }
  return n;
}

void handle_cmd(String cmd) {
  if (Serial.available() > 0 && cmd == "") {
    cmd = Serial.readStringUntil('\n');
    cmd.trim();
  }

  if (!cmd.isEmpty()) {
    /* 纯文本模式：非 { 开头时，直接当 factory 命令处理（便于串口调试）。 */
    if (cmd[0] != '{') {
      executeFactoryCommand(cmd);
      return;
    }

    StaticJsonDocument<1024> doc;
    DeserializationError error = deserializeJson(doc, cmd);

    if (error) {
      log_error("[CMD] JSON parse failed: %s", error.c_str());
      return;
    }

    if (doc["actions"].is<JsonArray>()) {
      JsonArray actions = doc["actions"].as<JsonArray>();
      for (JsonVariant action : actions) {
        String actionCmd = action.as<String>();
        executeCommand(actionCmd);
      }
    }

    if (doc["factory"].is<String>()) {
      String factoryCmd = doc["factory"].as<String>();
      executeFactoryCommand(factoryCmd);
    }
  }
}

/* 调用约定：
 * - head_* 命令异步入队：motor_task 独立执行斜坡，不阻塞命令处理。
 * - 表情/显示动画由 asr_chat 下行 pb 矢量帧驱动，不再支持本地 eye_* / play_animation 等命令。
 * - "delay" 命令保留为调试用，原地阻塞 1s。
 */
void executeCommand(String cmd) {
  if (cmd == "delay") {
    delay(1000);
  } else {
    log_warn("[CMD] unknown action: %s", cmd.c_str());
    return;
  }
  log_info("[CMD] %s", cmd.c_str());
}

void executeFactoryCommand(String cmd) {
  if (cmd.startsWith("ws_url ")) {
    String url = cmd.substring(7);
    url.trim();
    server_endpoint::Url parsed;
    if (!server_endpoint::bootstrap(url.c_str(), parsed)) {
      log_warn("[CMD] invalid server URL");
      return;
    }
    const char* active = nvs_ws_get_active_id();
    bool saved = false;
    if (strcmp(active, "builtin") != 0) saved = nvs_ws_update_custom(active, url.c_str());
    else {
      char id[8];
      saved = nvs_ws_add_custom(url.c_str(), id, sizeof(id)) && nvs_ws_set_active_id(id);
    }
    if (saved) {
      log_warn("[CMD] server URL updated; restarting");
      ESP.restart();
    } else log_warn("[CMD] server URL save failed");
  } else if (cmd.startsWith("usb_mode ")) {
    int enabled[1];
    if (parse_int_args(cmd,enabled,1)==1) usb_bench_set_mode(enabled[0]!=0);
  } else if (cmd.startsWith("aec_enable ")) {
    int enabled[1];
    const bool ok=parse_int_args(cmd,enabled,1)==1 && audio_frontend_bench_set_aec_enabled(enabled[0]!=0);
    usb_bench_emit(ok?"{\"usb\":\"aec_enable\",\"accepted\":true}":"{\"usb\":\"aec_enable\",\"accepted\":false}");
  } else if (cmd.startsWith("aec_delay ")) {
    int ms[1];
    const bool ok=parse_int_args(cmd,ms,1)==1 && ms[0]>=0 &&
        audio_frontend_set_reference_delay_ms(ms[0]);
    char reply[128];
    snprintf(reply,sizeof(reply),"{\"usb\":\"aec_delay\",\"accepted\":%s,\"ms\":%u}",
        ok?"true":"false",(unsigned)audio_frontend_reference_delay_ms());
    usb_bench_emit(reply);
  } else if (cmd == "mic_trace_stop") {
    mic_trace_stop();
  } else if (cmd.startsWith("mic_trace ")) {
    int durations[1];
    if (parse_int_args(cmd, durations, 1) == 1) mic_trace_start((uint32_t)max(1000, durations[0]));
  } else if (cmd.startsWith("log ")) {
    int levels[1];
    if (parse_int_args(cmd, levels, 1) == 1) {
      log_set_level((uint8_t)constrain(levels[0], LOG_LEVEL_NONE, LOG_LEVEL_TRACE));
      log_warn("[CMD] log level=%u", (unsigned)global_log_level);
    }
  } else if (cmd == "resource_status") {
    Serial.println(device_bootstrap_status_json());
  } else if (cmd == "lipsync_status") {
    Serial.println("{\"type\":\"lipsync_status\",\"mode\":\"rms\",\"classifier\":\"rms-v1\"}");
  } else if (cmd == "audio_status") {
    Serial.println(speaker_status_json());
  } else if (cmd == "cpu_stats") {
    if(setup_cpu_runtime_stats())task_setup_cpu_runtime_stats();
  } else if (cmd == "cpu_stats_stop") {
    cpu_runtime_stats_stop();
  } else if (cmd.startsWith("mouth_trace ")) {
    int durations[1];
    if (parse_int_args(cmd,durations,1)==1 && durations[0]>=0)
      display_mouth_trace((uint32_t)durations[0]);
  } else if (cmd == "continue_boot") {
    // Idempotent after the provisioning phase has already completed.
  } else if (cmd == "face_version") {
    log_info("[FACE] version=%s", face_assets_version());
  } else if (cmd.startsWith("face ")) {
    String id = cmd.substring(5);
    id.trim();
    JsonDocument doc;
    doc["type"] = "pb_single";
    doc["req"] = String("face-") + String(millis());
    doc["anim_id"] = id;
    String json;
    serializeJson(doc, json);
    size_t packed_len = 0;
    uint8_t* packed = new_packed_bin(json.c_str(), nullptr, 0, &packed_len);
    if (!packed) { log_warn("[FACE] command allocation failed"); return; }
    if (!pb_runtime_enqueue_frame(packed, packed_len)) {
      free(packed);
      log_warn("[FACE] rejected id=%s", id.c_str());
      return;
    }
  } else if (cmd == "reboot" || cmd == "restart") {
    log_info("[CMD] Rebooting device...");
    ESP.restart();
  } else if (cmd == "reset_wifi") {
    wifi_provision_reset();
  } else if (cmd == "chat") {
    /* 主 loop 已持续泵 pb；mic 自治上行，无需再切会话。 */
    log_info("[CMD] chat: already running (serviceLoop + mic autonomous)");
  } else if (cmd == "task") {
    log_task_dump();
  } else if (cmd.startsWith("play_url")) {
    // {"factory":"play_url <url>"} —— 拉取 URL 指向的 WAV 并走 I2S 播放。
    // 典型用法：上位机合成 WAV、提供临时 URL，再经串口下发本命令由设备拉取播放。
    int firstSpaceIndex = cmd.indexOf(' ');
    if (firstSpaceIndex <= 0) {
      log_warn("[CMD] play_url: empty url");
      return;
    }
    String url = cmd.substring(firstSpaceIndex + 1);
    url.trim();
    if (url.isEmpty()) {
      log_warn("[CMD] play_url: empty url");
      return;
    }
    log_info("[CMD] play_url: %s", url.c_str());
    speaker_play_url(url.c_str());
  } else if (cmd.startsWith("asr_chat")) {
    /* mic_task 自治上行；无需再跑语音轮次。 */
    log_info("[CMD] asr_chat: mic uplink is autonomous (no voice round)");
  } else {
    log_warn("[CMD] Unknown factory command: %s", cmd.c_str());
    return;
  }
  log_info("[CMD] %s", cmd.c_str());
}
