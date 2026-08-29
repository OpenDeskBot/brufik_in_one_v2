#include "pb_runtime.h"

#include <stdlib.h>
#include <string.h>
#include "display.h"
#include "head.h"
#include "logger.h"
#include "speaker.h"
#include "utils/utils.h"
#include "ws_transport.h"

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

/* ── 模型队列 ──
 * 入队即解析：队列里直接存 pb_model（媒体已拷贝，模型自包含），
 * 出队直接分发，不再二次解析。 */

static constexpr UBaseType_t kPbModelQDepth = 64;
static constexpr uint32_t kPbRuntimeStack = 24 * 1024;
static constexpr UBaseType_t kPbRuntimePrio = 5;

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

static void sendAck(const char* ack_type) {
  const unsigned space = computeMinExecutorSpace();
  JsonDocument ack;
  ack["type"] = "pb_ack";
  ack["req"] = s_ack_req;
  ack["ack_type"] = ack_type;
  ack["space"] = space;
  String msg;
  if (serializeJson(ack, msg) == 0) {
    log_warn("[PB] pb_ack serialize failed");
    return;
  }
  ws_transport_enqueue_state(msg.c_str());
  log_info("[PB_ACK] %s req=%s space=%u", ack_type, s_ack_req.c_str(), space);
  s_dispatched_since_ack = 0;
}

static void abortRound() {
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


/* 打包帧 → pb_model（入队时解析一次；媒体已拷贝进模型，原始缓冲可立即释放）。 */
static bool parse_frame_to_model(const uint8_t* data, size_t length, pb_model& out,
                                 const char*& err) {
  PackedFrame frame;
  if (!parse_packed_frame(const_cast<uint8_t*>(data), length, frame)) {
    return false;
  }
  const size_t media_len = frame.bin_len > 0 ? static_cast<size_t>(frame.bin_len) : 0;
  return pb_model_from_json(frame.doc, frame.bin, media_len, out, err);
}

/* ── pb_cancel 直接通知（不走模型队列）──
 * cancel 在入队解析后被识别：立即清空模型队列（丢弃旧链积压帧）并直接通知
 * pb_runtime 任务；任务在下一循环周期（≤2ms）内处理，drain/space 等待
 * 也会被打断。避免 cancel 排在旧链帧之后、等执行器播完才生效。 */

static volatile bool s_pending_cancel = false;
static char s_pending_cancel_req[37];
static portMUX_TYPE s_cancel_mux = portMUX_INITIALIZER_UNLOCKED;

static void notify_pending_cancel(const char* req) {
  portENTER_CRITICAL(&s_cancel_mux);
  strncpy(s_pending_cancel_req, req, sizeof(s_pending_cancel_req) - 1);
  s_pending_cancel_req[sizeof(s_pending_cancel_req) - 1] = '\0';
  s_pending_cancel = true;
  portEXIT_CRITICAL(&s_cancel_mux);
}

/* 任务侧取出待处理 cancel；返回 true 表示有（req 已拷出并清除标志）。 */
static bool take_pending_cancel(char* req_out, size_t req_sz) {
  portENTER_CRITICAL(&s_cancel_mux);
  const bool has = s_pending_cancel;
  if (has) {
    strncpy(req_out, s_pending_cancel_req, req_sz - 1);
    req_out[req_sz - 1] = '\0';
    s_pending_cancel = false;
  }
  portEXIT_CRITICAL(&s_cancel_mux);
  return has;
}

/* 等待循环里只读标志：有 pending cancel 立即打断等待（处理在循环顶部统一做）。 */
static bool cancel_pending() {
  return s_pending_cancel;
}

static void task_loop_pb_runtime(void* /*arg*/) {
  constexpr TickType_t kIdleWaitTicks = pdMS_TO_TICKS(2);

  for (;;) {
    /* pb_cancel 直接通知路径（模型队列已由入队侧清空，这里统一处理抢占）。 */
    char cancel_req[37];
    if (take_pending_cancel(cancel_req, sizeof(cancel_req))) {
      log_info("[PB] cancel req=%s active_req=%s (direct)", cancel_req, s_req.c_str());
      if (cancel_req[0] == '\0' || s_req.isEmpty() || s_req.equals(cancel_req)) {
        abortRound();
      }
      continue;
    }

    /* 入队时已解析，出队直接分发。 */
    pb_model incoming{};
    if (xQueueReceive(s_model_q, &incoming, kIdleWaitTicks) != pdTRUE) {
      continue;
    }

    const int incoming_type = incoming.type;

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
      display_render_submit_pb_anim_frames_owned(incoming.anim, incoming.anim_count);
      incoming.anim = nullptr;
      incoming.anim_count = 0;
    }
    if (incoming.servo && incoming.servo_count > 0) {
      head_submit_pb_servo_chunk_owned(incoming.servo, incoming.servo_count);
      incoming.servo = nullptr;
      incoming.servo_count = 0;
    }
    if (incoming.audio && incoming.audio->bin && incoming.audio->next_bin_len > 0) {
      if (!speaker_submit_pb_audio_owned(incoming.audio)) {
        log_warn("[PB] audio dispatch failed req=%s idx=%d", incoming.req, incoming.idx);
      } else {
        incoming.audio = nullptr;
      }
    }

    s_ack_req = incoming.req;
    ++s_dispatched_since_ack;

    if (incoming_type == PB_MODEL_END || incoming_type == PB_MODEL_SINGLE) {
      log_info("[PB] complete req=%s idx=%d type=%s", incoming.req, incoming.idx,
               pb_model_type_name(incoming.type));
      speaker_stream_pcm16_end(incoming.ch > 0 ? incoming.ch : 1);
    }

    pb_model_free(incoming);

    bool preempted = false;
    if (s_dispatched_since_ack >= kAckBatchSize) {
      while (computeMinExecutorSpace() < (unsigned)kAckBatchSize) {
        if (cancel_pending()) {
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
      while (!(speaker_task_done() && head_task_done() && display_task_done())) {
        if (cancel_pending()) {
          preempted = true;
          break;
        }
        vTaskDelay(pdMS_TO_TICKS(5));
      }
      if (!preempted) {
        sendAck(kAckEnd);
      }
    }
  }
}

/* ── 公开接口 ── */

bool setup_pb_runtime(void) {
  if (!s_model_q) {
    s_model_q = xQueueCreate(kPbModelQDepth, sizeof(pb_model));
    if (!s_model_q) {
      log_error("[PB_RUNTIME] model queue create failed");
      s_setup_ok = false;
      return false;
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
  /* 入队即解析：队列里直接存 pb_model，出队不再二次解析。
   * 解析成功后原始缓冲立即释放（媒体已拷贝进模型）。 */
  pb_model model{};
  const char* err = nullptr;
  if (!parse_frame_to_model(data, length, model, err)) {
    log_warn("[PB] model parse rejected: %s", err ? err : "packed");
    return false;  // 调用方（ws_transport）负责 free(data)
  }
  free(data);

  /* pb_cancel 不走模型队列：清空旧链积压帧并直接通知 pb_runtime 任务，
   * 抢占不等当前链播完。 */
  if (model.type == PB_MODEL_CANCEL) {
    pb_runtime_discard_rx_queue();
    notify_pending_cancel(model.req);
    pb_model_free(model);
    return true;
  }

  if (xQueueSend(s_model_q, &model, 0) != pdTRUE) {
    pb_model dropped{};
    if (xQueueReceive(s_model_q, &dropped, 0) == pdTRUE) {
      pb_model_free(dropped);
    }
    if (xQueueSend(s_model_q, &model, 0) != pdTRUE) {
      log_warn("[PB_RUNTIME] model queue full after drop; free new len=%u", (unsigned)length);
      pb_model_free(model);
      return true;  // 已自行释放，调用方无需再 free
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
