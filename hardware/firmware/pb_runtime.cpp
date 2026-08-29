#include "pb_runtime.h"

#include <stdlib.h>
#include <string.h>

#include <atomic>
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

/* ── pb_cancel 抢占 ──
 * cancel 帧解析后正常入队（模型队列），但入队前先置 s_pending_cancel 标志；
 * 置标志与入队由 s_cancel_lock 保证原子，任务侧 handle_pb_cancel() 才能
 * 拿到"队列里必有 cancel"的不变量。任务侧出队丢弃 cancel 及其之前的旧链
 * 积压帧（cancel 之后的新链帧保留），随后 abortRound 清执行器。任务主循环
 * 与 drain/space 等待都会调用 handle_pb_cancel，抢占不等当前链播完。 */

static std::atomic<bool> s_pending_cancel{false};
static SemaphoreHandle_t s_cancel_lock = nullptr;

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
  pb_model item{};
  while (xQueueReceive(s_model_q, &item, 0) == pdTRUE) {
    const bool is_cancel = (item.type == PB_MODEL_CANCEL);
    if (is_cancel) {
      log_info("[PB] cancel req=%s active_req=%s (drain)", item.req, s_req.c_str());
    }
    pb_model_free(item);
    if (is_cancel) {
      break;  // cancel 已出：其后的新链帧保留
    }
  }
  s_pending_cancel.store(false, std::memory_order_release);
  xSemaphoreGive(s_cancel_lock);
  /* abortRound 的执行器 abort 是阻塞入队（portMAX_DELAY），放锁后再调。 */
  abortRound();
  return true;
}

static void task_loop_pb_runtime(void* /*arg*/) {
  constexpr TickType_t kIdleWaitTicks = pdMS_TO_TICKS(2);

  for (;;) {
    /* pb_cancel 抢占：先出队处理（丢弃旧链积压帧 + abortRound）。 */
    if (handle_pb_cancel()) {
      continue;
    }

    /* 入队时已解析，出队直接分发。 */
    pb_model incoming{};
    if (xQueueReceive(s_model_q, &incoming, kIdleWaitTicks) != pdTRUE) {
      continue;
    }

    /* 防御：队列中残留的 cancel（如连续两个 cancel）直接消费。 */
    if (incoming.type == PB_MODEL_CANCEL) {
      log_info("[PB] cancel req=%s active_req=%s (queue)", incoming.req, s_req.c_str());
      if (incoming.req[0] == '\0' || s_req.isEmpty() || s_req.equals(incoming.req)) {
        abortRound();
      }
      pb_model_free(incoming);
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
        if (handle_pb_cancel()) {
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
        if (handle_pb_cancel()) {
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

/* 模型入队；满则丢最旧。失败时内部已释放 model。 */
static bool enqueue_model(pb_model& model) {
  if (xQueueSend(s_model_q, &model, 0) != pdTRUE) {
    pb_model dropped{};
    if (xQueueReceive(s_model_q, &dropped, 0) == pdTRUE) {
      pb_model_free(dropped);
    }
    if (xQueueSend(s_model_q, &model, 0) != pdTRUE) {
      log_warn("[PB_RUNTIME] model queue full after drop; free new");
      pb_model_free(model);
      return false;
    }
  }
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

  /* pb_cancel：先置标志再入队（锁内原子，保证任务侧能看到"队列里必有
   * cancel"），由 handle_pb_cancel 出队丢弃 cancel 及旧链积压帧。 */
  if (model.type == PB_MODEL_CANCEL) {
    if (s_cancel_lock && xSemaphoreTake(s_cancel_lock, portMAX_DELAY) == pdTRUE) {
      s_pending_cancel.store(true, std::memory_order_release);
      (void)enqueue_model(model);
      xSemaphoreGive(s_cancel_lock);
    } else {
      s_pending_cancel.store(true, std::memory_order_release);
      (void)enqueue_model(model);
    }
    return true;
  }

  (void)enqueue_model(model);
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
