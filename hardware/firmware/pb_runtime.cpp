#include "pb_runtime.h"
#include "media_wire.h"

#include <stdlib.h>
#include <string.h>

#include <atomic>
#include "display.h"
#include "rom_tools.h"
#include "head.h"
#include "logger.h"
#include "speaker.h"
#include "utils/utils.h"
#include "ws_transport.h"
#include "face_assets.h"
#include "device_bootstrap.h"
#include "local_idle_policy.h"
#include "audio_frontend_esp_sr.h"
#include "usb_bench.h"

#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

/* ── 常量 ── */

static constexpr int kAckBatchSize = 10;
static constexpr char kAckChunk[] = "pb_chunk";
static constexpr char kAckEnd[] = "pb_end";

/* ── 状态 ── */

static String s_req;
static int s_dispatched_since_ack = 0;
static String s_ack_req;
static int s_ack_idx = 0;
static uint32_t s_ack_epoch = 0;
static bool s_ack_stream = false;
static bool s_ack_local = false;
static LocalIdlePolicy s_idle;
static std::atomic<uint32_t> s_last_remote_ms{0};
static unsigned s_idle_clip=0;
static std::atomic<uint32_t> s_latest_epoch{0};
static std::atomic<uint32_t> s_cancelled_epoch{0};

/* ── 模型队列 ──
 * 入队即解析：队列里直接存 pb_model（媒体已拷贝，模型自包含），
 * 出队直接分发，不再二次解析。 */

static constexpr UBaseType_t kPbModelQDepth = 64;
static constexpr uint32_t kPbRuntimeStack = 24 * 1024;
static constexpr UBaseType_t kPbRuntimePrio = 6; // Credit must not starve behind rendering/mic.
/** 永久等待会使 PB 泵彻底失活；该超时只处理执行器状态损坏等异常。 */
static constexpr uint32_t kExecutorWaitTimeoutMs = 120000;
static constexpr uint32_t kExecutorWaitLogMs = 1000;

static bool s_setup_ok = false;
static TaskHandle_t s_task = nullptr;
static QueueHandle_t s_model_q = nullptr;

/* ── 内部函数 ── */

static unsigned computeMinExecutorSpace() {
  const unsigned spk = SPEAKER_QUEUE_DEPTH - speaker_input_queue_depth();
  const unsigned hd = HEAD_MOTOR_QUEUE_DEPTH - head_motor_input_queue_depth();
  const unsigned disp = DESKBOT_PB_EXECUTOR_QUEUE_DEPTH - display_render_input_queue_depth();
  unsigned m = spk;
  if (hd < m) m = hd;
  if (disp < m) m = disp;
  return m;
}

static bool sendAck(const char* ack_type) {
  if (s_ack_local) return true;
  const unsigned space = computeMinExecutorSpace();
  JsonDocument ack;
  ack["type"] = "pb_ack";
  ack["req"] = s_ack_req;
  ack["ack_type"] = ack_type;
  ack["idx"] = s_ack_idx;
  if (s_ack_epoch) ack["epoch"] = s_ack_epoch;
  ack["buffered_ms"] = speaker_buffered_ms();
  ack["device_ms"] = millis();
  if (!ws_transport_compact_supported()) {
    ack["audio_space"] = SPEAKER_QUEUE_DEPTH - speaker_input_queue_depth();
    ack["head_space"] = HEAD_MOTOR_QUEUE_DEPTH - head_motor_input_queue_depth();
    ack["display_space"] = DESKBOT_PB_EXECUTOR_QUEUE_DEPTH - display_render_input_queue_depth();
  }
  ack["space"] = space;
  if (!ws_transport_enqueue_ack(ack)) {
    log_error("[PB_ACK] enqueue failed type=%s req=%s", ack_type, s_ack_req.c_str());
    return false;
  }
  log_info("[PB_ACK] %s req=%s space=%u", ack_type, s_ack_req.c_str(), space);
  s_dispatched_since_ack = 0;
  return true;
}

static void abortRound() {
  s_idle.open_response=false;
  speaker_abort();
  display_abort();
  head_abort();
  s_req = "";
  speaker_set_task_done_flag();
  head_set_task_done_flag();
  display_set_task_done_flag();
  s_dispatched_since_ack = 0;
  s_ack_req = "";
}


// Only the ordered RX producer owns this cache. Teardown invalidates it via
// an atomic generation, without mutating a document from another task.
static std::atomic<uint32_t> s_wire_generation{0};
struct WireMetadata {
  uint32_t generation=0, epoch=0, sr=0, pts=0;
  int idx=0, level=1;
  uint8_t ch=0;
  bool stream=false;
  char req[37]{}, fmt[PB_MODEL_FMT_CAPACITY]{};
};
static WireMetadata s_wire_meta;
static bool parse_frame_to_model(const uint8_t* data, size_t length, pb_model& out,
                                 const char*& err) {
  const uint32_t generation=s_wire_generation.load();
  if (media_wire::magic(data,length)) {
    media_wire::Audio a;
    if (!ws_transport_binary_audio_supported() || !media_wire::audio_decode(data,length,a) ||
        s_wire_meta.generation!=generation || !s_wire_meta.stream ||
        a.epoch!=s_wire_meta.epoch || a.idx!=(uint32_t)s_wire_meta.idx+1 ||
        a.pts<s_wire_meta.pts) { err="invalid binary audio or missing head"; return false; }
    out.type=PB_MODEL_CHUNK; out.epoch=a.epoch; out.idx=a.idx; out.pts_ms=a.pts;
    out.chunk_ms=a.duration; out.stream=true; out.level=s_wire_meta.level;
    out.sr=s_wire_meta.sr; out.ch=s_wire_meta.ch;
    strlcpy(out.req,s_wire_meta.req,sizeof(out.req)); strlcpy(out.fmt,s_wire_meta.fmt,sizeof(out.fmt));
    out.audio=(pb_audio*)heap_caps_calloc(1,sizeof(pb_audio),MALLOC_CAP_SPIRAM);
    if (!out.audio) return false;
    out.audio->bin=(int8_t*)heap_caps_malloc(a.bytes,MALLOC_CAP_SPIRAM);
    if (!out.audio->bin) { pb_model_free(out); return false; }
    memcpy(out.audio->bin,data+media_wire::kAudioHeader,a.bytes);
    out.audio->next_bin_len=a.bytes; out.audio->frames=a.frames; out.audio->sr=out.sr;
    out.audio->ch=out.ch; strlcpy(out.audio->fmt,out.fmt,sizeof(out.audio->fmt));
    out.audio->epoch=a.epoch; out.audio->idx=a.idx; out.audio->pts_ms=a.pts;
    out.audio->duration_ms=a.duration; out.audio->received_ms=millis();
    strlcpy(out.audio->req,out.req,sizeof(out.audio->req));
    s_wire_meta.idx=a.idx; s_wire_meta.pts=a.pts;
    return true;
  }
  PackedFrame frame;
  if (!parse_packed_frame(const_cast<uint8_t*>(data),length,frame)) return false;
  const char* type=frame.doc["type"] | "";
  const bool continuation=strcmp(type,"pb_chunk")==0 || strcmp(type,"pb_end")==0;
  if (ws_transport_compact_supported() && continuation && frame.doc["level"].isNull()) {
    if (s_wire_meta.generation!=generation || !s_wire_meta.stream ||
        (frame.doc["epoch"] | 0u)!=s_wire_meta.epoch ||
        strcmp(frame.doc["req"] | "",s_wire_meta.req)!=0) { err="missing compact head"; return false; }
    frame.doc["level"]=s_wire_meta.level; frame.doc["stream"]=s_wire_meta.stream;
    frame.doc["sr"]=s_wire_meta.sr; frame.doc["ch"]=s_wire_meta.ch;
    frame.doc["fmt"]=s_wire_meta.fmt;
  }
  if (!pb_model_from_json(frame.doc,frame.bin,frame.bin_len,out,err)) return false;
  if (out.epoch && out.epoch>=s_latest_epoch.load() && out.epoch>s_cancelled_epoch.load()) {
    if (pb_model_is_chain_head(out)) {
      s_wire_meta={}; s_wire_meta.generation=generation; s_wire_meta.epoch=out.epoch;
      s_wire_meta.sr=out.sr; s_wire_meta.ch=out.ch; s_wire_meta.stream=out.stream;
      s_wire_meta.level=out.level; strlcpy(s_wire_meta.req,out.req,sizeof(s_wire_meta.req));
      strlcpy(s_wire_meta.fmt,out.fmt,sizeof(s_wire_meta.fmt));
    }
    if (out.epoch==s_wire_meta.epoch) { s_wire_meta.idx=out.idx; s_wire_meta.pts=out.pts_ms; }
  }
  return true;
}

/* ── pb_cancel 抢占 ──
 * cancel 帧解析后正常入队（模型队列），但入队前先置 s_pending_cancel 标志；
 * 置标志与入队由 s_cancel_lock 保证原子，任务侧 handle_pb_cancel() 才能
 * 拿到"队列里必有 cancel"的不变量。任务侧出队丢弃 cancel 及其之前的旧链
 * 积压帧（cancel 之后的新链帧保留），随后 abortRound 清执行器。任务主循环
 * 与 drain/space 等待都会调用 handle_pb_cancel，抢占不等当前链播完。 */

static std::atomic<bool> s_pending_cancel{false};
static SemaphoreHandle_t s_cancel_lock = nullptr;

static void finish_cancel(const String& req, uint32_t epoch, int idx) {
  s_ack_local=false;
  const uint32_t cancel_at = millis();
  abortRound();
  while ((speaker_cancel_pending() || head_cancel_pending() || display_cancel_pending()) &&
         millis()-cancel_at < 1000) vTaskDelay(pdMS_TO_TICKS(2));
  if (epoch) {
    s_ack_req = req; s_ack_epoch = epoch; s_ack_idx = idx;
    const bool stopped = !speaker_cancel_pending() && !head_cancel_pending() && !display_cancel_pending();
    sendAck(stopped ? "pb_cancelled" : "pb_error");
    log_warn("[PB_CANCEL] req=%s epoch=%u stopped=%d stop_ms=%u", req.c_str(),
             (unsigned)epoch, stopped, (unsigned)(millis()-cancel_at));
    s_ack_req = "";
  }
}

static bool handle_pb_cancel() {
  if (!s_pending_cancel.load(std::memory_order_acquire)) {
    return false;
  }
  if (!s_cancel_lock || xSemaphoreTake(s_cancel_lock, portMAX_DELAY) != pdTRUE) {
    return false;
  }
  if (!s_pending_cancel.load(std::memory_order_relaxed)) {
    xSemaphoreGive(s_cancel_lock);
    return false;
  }
  String cancelled_req = s_ack_req;
  uint32_t cancelled_epoch = s_ack_epoch;
  const int cancelled_idx = s_ack_idx;
  pb_model item{};
  while (xQueueReceive(s_model_q, &item, 0) == pdTRUE) {
    const bool is_cancel = (item.type == PB_MODEL_CANCEL);
    if (is_cancel) {
      cancelled_req = item.req; cancelled_epoch = item.epoch;
      log_info("[PB] cancel req=%s active_req=%s (drain)", item.req, s_req.c_str());
    }
    pb_model_free(item);
    if (is_cancel) {
      break;  // cancel 已出：其后的新链帧保留
    }
  }
  s_pending_cancel.store(false, std::memory_order_release);
  xSemaphoreGive(s_cancel_lock);
  // The queued cancel marker is consumed exactly once, whichever path won.
  finish_cancel(cancelled_req, cancelled_epoch, cancelled_idx);
  return true;
}

static void observe_local_speech() {
  const bool ready=audio_frontend_ready() && audio_frontend_output_healthy();
  s_idle.observe_speech(millis(),ready,ready && audio_frontend_speech_active());
}

static bool make_local_idle(pb_model& model) {
  if (usb_bench_active() || !device_bootstrap_done()) return false;
  observe_local_speech();
  auto idle=face_assets_idle();
  s_idle.last_command=s_last_remote_ms.load(std::memory_order_acquire);
  const bool busy=!speaker_task_done() || !head_task_done() || !display_task_done() ||
      uxQueueMessagesWaiting(s_model_q)>0;
  const uint32_t now=millis();
  if (!s_idle.due(now,idle["after_ms"] | 15000u,idle["repeat_ms"] | 12000u,busy)) return false;
  auto clips=idle["clips"].as<JsonArrayConst>();
  if (!clips.size()) return false;
  auto clip=clips[s_idle_clip++%clips.size()];
  const char* error=nullptr;uint32_t duration=0;
  if (!face_assets_load(clip["anim_id"],model.anim,model.anim_count,duration,error)) return false;
  auto steps=clip["servo"].as<JsonArrayConst>();
  if (steps.size()) {
    model.servo=(pb_servo_frame*)psram_malloc(steps.size()*sizeof(pb_servo_frame));
    if (!model.servo) {pb_model_free(model);return false;}
    for (JsonObjectConst step:steps) {
      auto& dst=model.servo[model.servo_count++];
      dst.xm=step["xm"];dst.ym=step["ym"];dst.x=step["x"];dst.y=step["y"];dst.ms=step["ms"];
    }
  }
  model.local_idle=true;model.type=PB_MODEL_SINGLE;model.chunk_ms=duration;model.level=0;
  strlcpy(model.req,"local-idle",sizeof(model.req));
  s_idle.last_idle=now;
  return true;
}

static void task_loop_pb_runtime(void* /*arg*/) {
  constexpr TickType_t kIdleWaitTicks = pdMS_TO_TICKS(2);

  for (;;) {
    /* pb_cancel 抢占：先出队处理（丢弃旧链积压帧 + abortRound）。 */
    if (handle_pb_cancel()) {
      continue;
    }
    // A cancelled executor may still be retiring an old END marker. Do not
    // publish new work until every owner has crossed its cancel boundary.
    if (speaker_cancel_pending() || head_cancel_pending() || display_cancel_pending()) {
      vTaskDelay(kIdleWaitTicks);
      continue;
    }

    /* 入队时已解析，出队直接分发。 */
    pb_model incoming{};
    if (xQueueReceive(s_model_q, &incoming, kIdleWaitTicks) != pdTRUE) {
      if (!make_local_idle(incoming)) continue;
    }

    // A cancel may wake xQueueReceive after the top-of-loop flag check.
    // Clear its pending flag here: leaving it set would drain the NEW chain
    // on the next iteration looking for an already-consumed marker.
    if (incoming.type == PB_MODEL_CANCEL) {
      xSemaphoreTake(s_cancel_lock, portMAX_DELAY);
      s_pending_cancel.store(false, std::memory_order_release);
      xSemaphoreGive(s_cancel_lock);
      finish_cancel(incoming.req, incoming.epoch, s_ack_idx);
      pb_model_free(incoming);
      continue;
    }

    if (incoming.epoch && (incoming.epoch < s_latest_epoch.load() ||
                           incoming.epoch <= s_cancelled_epoch.load())) {
      pb_model_free(incoming);
      continue;
    }
    const int incoming_type = incoming.type;
    const bool local_idle=incoming.local_idle;
    s_ack_local=local_idle;
    if (!local_idle && incoming.type==PB_MODEL_START) s_idle.open_response=true;
    if (local_idle) {
      // Close the race between choosing an idle clip and publishing its tracks.
      observe_local_speech();
      if (s_idle.stop_for_attention() || uxQueueMessagesWaiting(s_model_q)>0) {
        pb_model_free(incoming);continue;
      }
    }
    s_ack_req = incoming.req; s_ack_idx = incoming.idx;
    s_ack_epoch = incoming.epoch; s_ack_stream = incoming.stream;
    if (incoming.volume >= 0) speaker_set_volume(incoming.volume);

    /* ── dispatch (was dispatchModel) ── */
    log_info("[PB] dispatch req=%s type=%s idx=%d level=%d anim=%u servo=%u audio=%d",
             incoming.req, pb_model_type_name(incoming.type), incoming.idx, incoming.level,
             (unsigned)incoming.anim_count, (unsigned)incoming.servo_count,
             incoming.audio ? incoming.audio->next_bin_len : 0);

    if (pb_model_is_chain_head(incoming)) {
      s_req = incoming.req;
      s_dispatched_since_ack = 0;
    }

    if (incoming.anim && incoming.anim_count > 0) {
      display_render_submit_pb_anim_frames_owned(incoming.anim, incoming.anim_count, false,
                                                       incoming.stream ? incoming.epoch : 0, incoming.pts_ms);
      incoming.anim = nullptr;
      incoming.anim_count = 0;
    }
    if (incoming.servo && incoming.servo_count > 0) {
      head_submit_pb_servo_chunk_owned(incoming.servo, incoming.servo_count,
                                         incoming.stream ? incoming.epoch : 0, incoming.pts_ms);
      incoming.servo = nullptr;
      incoming.servo_count = 0;
    }
    bool audio_failed = false;
    if (incoming.audio && incoming.audio->bin && incoming.audio->next_bin_len > 0) {
      /* speaker_submit_pb_audio_owned 为 owned 语义：成功入队或入队失败均会
       * 释放 audio（失败时在函数内 free 后返回 false）。因此无论成败都必须
       * 置空 incoming.audio，否则下方 pb_model_free 会二次释放 → 堆损坏。 */
      if (!speaker_submit_pb_audio_owned(incoming.audio)) {
        log_warn("[PB] audio dispatch failed req=%s idx=%d (dropped)", incoming.req,
                 incoming.idx);
        audio_failed = true;
      }
      incoming.audio = nullptr;
    }

    s_ack_req = incoming.req;
    ++s_dispatched_since_ack;

    if (incoming_type == PB_MODEL_END || incoming_type == PB_MODEL_SINGLE) {
      log_info("[PB] complete req=%s idx=%d type=%s", incoming.req, incoming.idx,
               pb_model_type_name(incoming.type));
      speaker_stream_pcm16_end(incoming.ch > 0 ? incoming.ch : 1);
    }

    pb_model_free(incoming);
    if (audio_failed) {
      sendAck("pb_error");
      abortRound();
      continue;
    }

    bool preempted = false;
    if (s_ack_stream) {
      // Bound AUDIO ahead in milliseconds. Slow expressions cannot withhold
      // audio credits. Their executor catches up against the audio timeline.
      const uint32_t wait_started = millis();
      while (speaker_buffered_ms() > 1000 || speaker_input_queue_depth() >= SPEAKER_QUEUE_DEPTH-3) {
        if (handle_pb_cancel()) { preempted = true; break; }
        if (millis()-wait_started > 3000) {
          sendAck("pb_error"); abortRound(); preempted = true; break;
        }
        vTaskDelay(pdMS_TO_TICKS(2));
      }
      if (!preempted) sendAck("pb_credit");
    }
    if (!s_ack_stream && s_dispatched_since_ack >= kAckBatchSize) {
      const uint32_t wait_started = millis();
      uint32_t last_wait_log = wait_started;
      while (computeMinExecutorSpace() < (unsigned)kAckBatchSize) {
        if (handle_pb_cancel()) {
          preempted = true;
          break;
        }
        const uint32_t now = millis();
        if (now - last_wait_log >= kExecutorWaitLogMs) {
          log_warn("[PB_WAIT_SPACE] req=%s elapsed=%u space=%u need=%u", s_ack_req.c_str(),
                   (unsigned)(now - wait_started), computeMinExecutorSpace(),
                   (unsigned)kAckBatchSize);
          last_wait_log = now;
        }
        if (now - wait_started >= kExecutorWaitTimeoutMs) {
          log_error("[PB_WAIT_SPACE] timeout req=%s space=%u", s_ack_req.c_str(),
                    computeMinExecutorSpace());
          abortRound();
          preempted = true;
          break;
        }
        vTaskDelay(pdMS_TO_TICKS(5));
      }
      if (!preempted) {
        sendAck(kAckChunk);
      }
    }

    if (!preempted && (incoming_type == PB_MODEL_END || incoming_type == PB_MODEL_SINGLE)) {
      speaker_signal_task_done();
      head_signal_task_done();
      display_signal_task_done();
      bool audio_end_sent = false;
      const uint32_t wait_started = millis();
      uint32_t last_wait_log = wait_started;
      while (!(speaker_task_done() && head_task_done() && display_task_done())) {
        if (local_idle) {
          observe_local_speech();
          if (s_idle.stop_for_attention()) {
            // Local voice detection yields before any server round trip. Keep
            // remote PB and its epochs intact: only retire this local idle work.
            log_info("[PB] local idle stopped: %s",s_idle.user_speaking?"user speech":"VAD unavailable");
            finish_cancel(String(),0,0);preempted=true;break;
          }
        }
        if (local_idle && uxQueueMessagesWaiting(s_model_q)>0) {
          // Interrupt only local executor work; preserve the incoming remote queue.
          log_info("[PB] remote command preempts local idle");
          finish_cancel(String(),0,0);preempted=true;break;
        }
        if (handle_pb_cancel()) {
          preempted = true;
          break;
        }
        if (s_ack_stream && speaker_task_done() && !audio_end_sent) {
          sendAck("pb_audio_end"); audio_end_sent = true;
        }
        const uint32_t now = millis();
        if (now - last_wait_log >= kExecutorWaitLogMs) {
          log_warn("[PB_WAIT_END] req=%s elapsed=%u speaker=%d head=%d display=%d "
                   "speaker_q=%u head_q=%u display_q=%u",
                   s_ack_req.c_str(), (unsigned)(now - wait_started), speaker_task_done(),
                   head_task_done(), display_task_done(), speaker_input_queue_depth(),
                   head_motor_input_queue_depth(), display_render_input_queue_depth());
          last_wait_log = now;
        }
        if (now - wait_started >= kExecutorWaitTimeoutMs) {
          log_error("[PB_WAIT_END] timeout req=%s speaker=%d head=%d display=%d",
                    s_ack_req.c_str(), speaker_task_done(), head_task_done(),
                    display_task_done());
          abortRound();
          preempted = true;
          break;
        }
        vTaskDelay(pdMS_TO_TICKS(5));
      }
      if (!preempted) {
        if (s_ack_stream && !audio_end_sent && !ws_transport_compact_supported()) sendAck("pb_audio_end");
        sendAck(kAckEnd);
      }
      if (!local_idle) s_idle.open_response=false;
    }
  }
}

/* ── 公开接口 ── */

bool setup_pb_runtime(void) {
  s_last_remote_ms.store(millis());
  if (!s_model_q) {
    s_model_q = xQueueCreate(kPbModelQDepth, sizeof(pb_model));
    if (!s_model_q) {
      log_error("[PB_RUNTIME] model queue create failed");
      s_setup_ok = false;
      return false;
    }
  }
  if (!s_cancel_lock) {
    s_cancel_lock = xSemaphoreCreateMutex();
    if (!s_cancel_lock) {
      log_warn("[PB_RUNTIME] cancel lock create failed");
    }
  }
  s_req.reserve(37);
  s_ack_req.reserve(37);
  s_setup_ok = true;
  log_info("[PB_RUNTIME] setup ok model_q=%u", (unsigned)kPbModelQDepth);
  return true;
}

bool task_setup_pb_runtime(void) {
  if (!s_setup_ok) {
    log_error("[PB_RUNTIME] task_setup skipped (setup not ok)");
    return false;
  }
  if (s_task) {
    return true;
  }
  BaseType_t rc = utils_task_create_pinned(task_loop_pb_runtime, "pb_runtime", kPbRuntimeStack, nullptr,
                                           kPbRuntimePrio, &s_task, APP_CPU_NUM);
  if (rc != pdPASS) {
    log_error("[PB_RUNTIME] task create failed rc=%d (internal free=%u)", (int)rc,
              (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    s_task = nullptr;
    return false;
  }
  log_info("[PB_RUNTIME] task OK stack=%u prio=%u", (unsigned)kPbRuntimeStack,
           (unsigned)kPbRuntimePrio);
  return true;
}

bool pb_runtime_enqueue_frame(uint8_t* data, size_t length) {
  if(!media_wire::magic(data,length) && rom_tools_handle_frame(data,length)){free(data);return true;}
  /* 入队即解析：队列里直接存 pb_model，出队不再二次解析。
   * 解析成功后原始缓冲立即释放（媒体已拷贝进模型）。 */
  pb_model model{};
  const char* err = nullptr;
  if (!parse_frame_to_model(data, length, model, err)) {
    log_warn("[PB] model parse rejected: %s", err ? err : "packed");
    return false;  // 调用方（ws_transport）负责 free(data)
  }
  s_last_remote_ms.store(millis(),std::memory_order_release);
  free(data);
  if (model.epoch) {
    const uint32_t latest = s_latest_epoch.load();
    if (model.epoch < latest || (model.type != PB_MODEL_CANCEL && model.epoch <= s_cancelled_epoch.load())) {
      log_warn("[PB_EPOCH] drop stale req=%s epoch=%u latest=%u", model.req, (unsigned)model.epoch, (unsigned)latest);
      pb_model_free(model); return true;
    }
    if (model.epoch > latest) {
      if (!pb_model_is_chain_head(model)) { pb_model_free(model); return true; }
      s_latest_epoch.store(model.epoch);
    }
    if (model.type == PB_MODEL_CANCEL) s_cancelled_epoch.store(model.epoch);
  }

  /* pb_cancel：先置标志再入队（锁内原子，保证任务侧能看到"队列里必有
   * cancel"），由 handle_pb_cancel 出队丢弃 cancel 及旧链积压帧。 */
  if (model.type == PB_MODEL_CANCEL) {
    if (s_cancel_lock && xSemaphoreTake(s_cancel_lock, portMAX_DELAY) == pdTRUE) {
      s_pending_cancel.store(true, std::memory_order_release);
      /* 入队；满则丢最旧（与置标志在锁内原子）。 */
      if (xQueueSend(s_model_q, &model, 0) != pdTRUE) {
        pb_model dropped{};
        if (xQueueReceive(s_model_q, &dropped, 0) == pdTRUE) {
          pb_model_free(dropped);
        }
        if (xQueueSend(s_model_q, &model, 0) != pdTRUE) {
          log_warn("[PB_RUNTIME] model queue full after drop; free new");
          pb_model_free(model);
        }
      }
      xSemaphoreGive(s_cancel_lock);
      return true;
    }
    /* 锁不可用（创建失败）：仅置标志，与入队不原子（极端回退）。 */
    s_pending_cancel.store(true, std::memory_order_release);
  }

  if (model.stream && uxQueueSpacesAvailable(s_model_q) == 0) {
    JsonDocument error;
    error["type"] = "pb_ack"; error["ack_type"] = "pb_error";
    error["req"] = model.req; error["epoch"] = model.epoch; error["idx"] = model.idx;
    error["error"] = "model_queue_full";
    String msg; serializeJson(error, msg); ws_transport_enqueue_state(msg.c_str());
    pb_model_free(model);
    log_error("[PB_STREAM] model queue full");
    return true;
  }
  /* 入队；满则丢最旧（失败时内部已释放 model）。 */
  if (xQueueSend(s_model_q, &model, 0) != pdTRUE) {
    pb_model dropped{};
    if (xQueueReceive(s_model_q, &dropped, 0) == pdTRUE) {
      pb_model_free(dropped);
    }
    if (xQueueSend(s_model_q, &model, 0) != pdTRUE) {
      log_warn("[PB_RUNTIME] model queue full after drop; free new");
      pb_model_free(model);
    }
  }
  log_info("[PB_ENQ] pb_q_in len=%u q=%u", (unsigned)length,
           (unsigned)uxQueueMessagesWaiting(s_model_q));
  return true;
}

void pb_runtime_discard_rx_queue(void) {
  if (!s_model_q) {
    return;
  }
  pb_model item{};
  while (xQueueReceive(s_model_q, &item, 0) == pdTRUE) {
    pb_model_free(item);
  }
}

void pb_runtime_abort_session(void) {
  s_wire_generation.fetch_add(1);
  s_latest_epoch.store(0);
  s_cancelled_epoch.store(0);
  // Use a real boundary marker even for connection teardown. A bare flag
  // could otherwise drain the new session looking for a nonexistent cancel.
  pb_model cancel{};
  cancel.type = PB_MODEL_CANCEL;
  strlcpy(cancel.req, "session", sizeof(cancel.req));
  xSemaphoreTake(s_cancel_lock, portMAX_DELAY);
  pb_runtime_discard_rx_queue();
  (void)xQueueSend(s_model_q, &cancel, 0);
  s_pending_cancel.store(true, std::memory_order_release);
  xSemaphoreGive(s_cancel_lock);
  log_warn("[PB_RUNTIME] abort session requested");
}
