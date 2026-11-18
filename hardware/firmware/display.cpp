#include "display.h"
#include "usb_bench.h"
#include "esp_timer.h"
#include "display_text.h"
#include "face_transition.h"
#include "face_assets.h"
#include "device_bootstrap.h"
#include "pb_model.h"
#include "speaker.h"
#include "rom_tools.h"

#include "logger.h"
#include "utils/utils.h"

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include <atomic>
#include <cstring>
#include "esp_heap_caps.h"

/* ──────────────────────────────────────────────────────────────────────
 * 全局对象 & PSRAM 帧缓冲
 * ──────────────────────────────────────────────────────────────────── */

DeskbotDisplay g_display(DESKBOT_DISPLAY_CS, DESKBOT_DISPLAY_DC, -1);

/** PSRAM canvas：矢量动画先在 PSRAM 绘制，完成后整帧 DMA 推送，消除逐像素撕裂。 */
class PsramCanvas16 : public GFXcanvas16 {
public:
  PsramCanvas16(uint16_t w, uint16_t h) : GFXcanvas16(w, h, /*alloc=*/false) {
    buffer = (uint16_t*)heap_caps_malloc((uint32_t)w * h * 2u, MALLOC_CAP_SPIRAM);
    if (buffer) memset(buffer, 0, (uint32_t)w * h * 2u);
  }
  void set_viewport(uint16_t w,uint16_t h) {
    _width=w; _height=h;
    for (uint16_t y=0;y<h;++y) memset(buffer+y*WIDTH,0,w*2);
  }
  ~PsramCanvas16() { if (buffer) { free(buffer); buffer = nullptr; } }
};

static PsramCanvas16* s_canvas   = nullptr;
static PsramCanvas16* s_mouth_canvas = nullptr;
static int s_draw_origin_x=0, s_draw_origin_y=0;
static Adafruit_GFX*  s_draw_gfx = &g_display;   /* 渲染目标：canvas 或直写面板 */

/* ──────────────────────────────────────────────────────────────────────
 * FreeRTOS 渲染任务
 * ────────────────────────────────────────────────────────────────────── */

namespace {

static constexpr uint32_t kPbDisplayBudgetMs      = 40;
static constexpr uint8_t  kPbMaxPrimsPerLayer      = 16;
static constexpr UBaseType_t kPbDisplayQueueDepth  = DESKBOT_PB_EXECUTOR_QUEUE_DEPTH;
static constexpr size_t   kPbMaxTextChars           = 128;
static constexpr size_t   kPbMaxAnimSegsPerChunk    = 64;
static constexpr int      kLayerCount               = 6;  /* bg,nose,mouth,eye_l,eye_r,extra */

/* ── PB 图元 & 层存储 ── */

struct StoredPrim {
  pb_anim_element_shape shape;
  uint16_t color;
  int16_t x, y, w, h, r;
  int16_t x1, y1, x2, y2;
  uint8_t text_size;
  char    text[kPbMaxTextChars + 1];
};

struct StoredLayer {
  uint8_t     count;
  StoredPrim  prims[kPbMaxPrimsPerLayer];
};

struct StoredLayerPool {
  StoredLayer prev[kLayerCount];
  StoredLayer curr[kLayerCount];
  StoredLayer mouth_closed, mouth_open, mouth_shown;
  StoredLayer shown[kLayerCount];  // Exact geometry/color of the last pushed canvas.
};

static StoredLayerPool* s_layer_pool = nullptr;
static bool s_have_prev = false;
static bool s_have_shown = false;
static bool s_mouth_override = false;
static bool s_mouth_dirty = false;
static audio_lipsync::AnimationState s_mouth_state;
static uint32_t s_mouth_tick = 0;
static audio_lipsync::Frame s_mouth_frame;
static std::atomic<bool> s_assets_changed{true};
static std::atomic<uint32_t> s_mouth_trace_until{0};
static void layer_fill_from_pb(const pb_anim_element*,size_t,pb_anim_element_layer,StoredLayer*);

static void reload_mouth_assets() {
  for (unsigned i=0;i<2;++i) {
    pb_anim_frame* frames=nullptr;size_t count=0;const char* error=nullptr;
    if (!face_assets_load_phoneme(face_assets_mouth_phoneme(i!=0),100,frames,count,error)) {
      log_error("[DISPLAY] local mouth asset missing: %s",error);continue;
    }
    layer_fill_from_pb(frames[0].elements,frames[0].element_count,pb_anim_element_layer::mouth,
        i?&s_layer_pool->mouth_open:&s_layer_pool->mouth_closed);
    pb_anim_frames_free(frames,count);
  }
}

// UI-only state. No network/audio waits in the display task.
static bool update_mouth() {
  const uint32_t now = millis();
  const auto frame = speaker_mouth_frame();
  s_mouth_frame = frame;
  if (frame.active) s_mouth_override = true;
  const uint32_t elapsed = s_mouth_tick ? now-s_mouth_tick : 10;
  s_mouth_tick = now;
  const float previous = s_mouth_state.open;
  audio_lipsync::process_animation(frame, elapsed, s_mouth_state);
  s_mouth_dirty |= s_mouth_override && (frame.active || previous != s_mouth_state.open);
  return s_mouth_dirty;
}


static bool ensure_stored_layer_pool() {
  if (s_layer_pool) return true;
  s_layer_pool = static_cast<StoredLayerPool*>(
      heap_caps_calloc(1, sizeof(StoredLayerPool), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (!s_layer_pool) {
    log_error("[DISPLAY] PSRAM StoredLayer pool alloc failed bytes=%u", (unsigned)sizeof(StoredLayerPool));
    return false;
  }
  log_info("[DISPLAY] StoredLayer pool in PSRAM bytes=%u", (unsigned)sizeof(StoredLayerPool));
  return true;
}

/* ── 插值工具 ── */

static void pb_vector_interp_resume() {
  if (s_have_shown) {
    memcpy(s_layer_pool->prev, s_layer_pool->shown, sizeof(s_layer_pool->prev));
    s_have_prev = true;
  }
}

/* ── 图元转换 & 绘制 ── */

static bool prim_from_pb_element(const pb_anim_element& in, StoredPrim& p) {
  memset(&p, 0, sizeof(p));
  if (in.shape == pb_anim_element_shape::none) return false;
  p.shape = in.shape;
  p.color = in.color;
  p.x  = (int16_t)in.x;   p.y  = (int16_t)in.y;
  p.w  = (int16_t)in.w;   p.h  = (int16_t)in.h;   p.r = (int16_t)in.r;
  p.x1 = (int16_t)in.x1;  p.y1 = (int16_t)in.y1;
  p.x2 = (int16_t)in.x2;  p.y2 = (int16_t)in.y2;
  if (p.shape == pb_anim_element_shape::text) {
    if (!in.text[0]) return false;
    strncpy(p.text, in.text, kPbMaxTextChars);
    p.text[kPbMaxTextChars] = '\0';
    int tsz = in.text_size;
    if (tsz < 1) tsz = 1;
    if (tsz > 3) tsz = 3;
    p.text_size = (uint8_t)tsz;
  }
  return true;
}

static void layer_fill_from_pb(const pb_anim_element* elements, size_t count,
                               pb_anim_element_layer layer, StoredLayer* out) {
  out->count = 0;
  if (!elements || count == 0) return;
  for (size_t i = 0; i < count; ++i) {
    if (elements[i].layer != layer || out->count >= kPbMaxPrimsPerLayer) continue;
    if (prim_from_pb_element(elements[i], out->prims[out->count])) {
      out->count++;
    }
  }
}

static void stored_from_pb(const pb_anim_element* elements, size_t count, StoredLayer* out) {
  for (int i = 0; i < kLayerCount; ++i) {
    layer_fill_from_pb(elements, count, static_cast<pb_anim_element_layer>(i), &out[i]);
  }
}

static void draw_prim_unshifted(const StoredPrim& p) {
  const uint16_t col = p.color;
  switch (p.shape) {
    case pb_anim_element_shape::rect:
      if (p.w > 0 && p.h > 0) s_draw_gfx->fillRect(p.x, p.y, p.w, p.h, col);
      break;
    case pb_anim_element_shape::rect_outline:
      if (p.w > 0 && p.h > 0) s_draw_gfx->drawRect(p.x, p.y, p.w, p.h, col);
      break;
    case pb_anim_element_shape::circle:
      if (p.r > 0) s_draw_gfx->fillCircle(p.x, p.y, p.r, col);
      break;
    case pb_anim_element_shape::circle_outline:
      if (p.r > 0) s_draw_gfx->drawCircle(p.x, p.y, p.r, col);
      break;
    case pb_anim_element_shape::line:
      s_draw_gfx->drawLine(p.x1, p.y1, p.x2, p.y2, col);
      break;
    case pb_anim_element_shape::ellipse:
      if (p.w > 0 && p.h > 0) s_draw_gfx->drawEllipse(p.x, p.y, p.w, p.h, col);
      break;
    case pb_anim_element_shape::ellipse_fill:
      if (p.w > 0 && p.h > 0) s_draw_gfx->fillEllipse(p.x, p.y, p.w, p.h, col);
      break;
    case pb_anim_element_shape::round_rect:
      if (p.w > 0 && p.h > 0) {
        if (p.r > 0) s_draw_gfx->fillRoundRect(p.x, p.y, p.w, p.h, p.r, col);
        else         s_draw_gfx->fillRect(p.x, p.y, p.w, p.h, col);
      }
      break;
    case pb_anim_element_shape::round_rect_outline:
      if (p.w > 0 && p.h > 0) {
        if (p.r > 0) s_draw_gfx->drawRoundRect(p.x, p.y, p.w, p.h, p.r, col);
        else         s_draw_gfx->drawRect(p.x, p.y, p.w, p.h, col);
      }
      break;
    case pb_anim_element_shape::text:
      if (p.text[0] != '\0') {
        display_text_draw(s_draw_gfx, p.x, p.y, p.text, p.text_size, col);
      }
      break;
    default: break;
  }
}

static void draw_prim(const StoredPrim& p) {
  if (!s_draw_origin_x && !s_draw_origin_y) { draw_prim_unshifted(p); return; }
  StoredPrim local=p;
  local.x-=s_draw_origin_x; local.y-=s_draw_origin_y;
  local.x1-=s_draw_origin_x; local.y1-=s_draw_origin_y;
  local.x2-=s_draw_origin_x; local.y2-=s_draw_origin_y;
  draw_prim_unshifted(local);
}

/* ── 帧间插值绘制 ── */

static void render_mouth() {
  face_transition::layer(&s_layer_pool->mouth_closed, s_layer_pool->mouth_open,
                         s_mouth_state.open, s_layer_pool->mouth_shown);
  for (uint8_t i=0; i<s_layer_pool->mouth_shown.count; ++i)
    draw_prim(s_layer_pool->mouth_shown.prims[i]);
}

static void draw_shown_layers() {
  for (int i=0; i<kLayerCount; ++i) {
    if (i == static_cast<int>(pb_anim_element_layer::mouth) && s_mouth_override) {
      render_mouth();
      continue;
    }
    const auto& layer = s_layer_pool->shown[i];
    for (uint8_t j=0; j<layer.count; ++j) draw_prim(layer.prims[j]);
  }
}

static void draw_layers_sample(const StoredLayer* prev, const StoredLayer* curr, float t) {
  update_mouth();
  for (int i=0; i<kLayerCount; ++i)
    face_transition::layer(prev ? &prev[i] : nullptr, curr[i], t, s_layer_pool->shown[i]);
  // Store the base expression separately: the audio overlay must never become
  // an interpolation origin for eyes, decorations, or the next expression.
  draw_shown_layers();
}

static void report_mouth_push() {
  const bool bench=usb_bench_active();
  const uint32_t until=s_mouth_trace_until.load(std::memory_order_relaxed);
  if ((!bench && (!until || (int32_t)(until-millis())<=0)) || !s_mouth_override) return;
  char sample[640];
  snprintf(sample,sizeof(sample),
    "{\"usb\":\"mouth\",\"us\":%llu,\"audio_us\":%llu,\"gen\":%u,\"epoch\":%u,"
    "\"pos_ms\":%u,\"first_sample\":%llu,\"target\":%.4f,\"shown\":%.4f,\"rms\":%.5f,\"active\":%s,\"underrun\":%s,"
    "\"version\":\"%s\",\"closed\":\"%s\",\"open\":\"%s\",\"mode\":\"rms\"}",
    (unsigned long long)esp_timer_get_time(),(unsigned long long)s_mouth_frame.output_us,
    (unsigned)s_mouth_frame.generation,(unsigned)s_mouth_frame.epoch,
    (unsigned)s_mouth_frame.end_ms,(unsigned long long)s_mouth_frame.first_sample,s_mouth_frame.open,s_mouth_state.open,s_mouth_frame.rms,
    s_mouth_frame.active?"true":"false",s_mouth_frame.underrun?"true":"false",
    face_assets_version(),face_assets_mouth_phoneme(false),face_assets_mouth_phoneme(true));
  if (bench) usb_bench_emit(sample);
  else {
    const size_t length=strlen(sample);
    // USB is configured with zero TX timeout. Skip congested samples.
    if (Serial.availableForWrite()>=(int)(length+1)) {
      sample[length]='\n';Serial.write((const uint8_t*)sample,length+1);
    }
  }
}

/** canvas 整帧推送到物理屏。setAddrWindow+writePixels 批量写入，避免 drawRGBBitmap 逐像素开销。 */
static inline void pb_canvas_push() {
  s_mouth_dirty = false;
  if (s_canvas && s_canvas->getBuffer()) {
    g_display.startWrite();
    g_display.setAddrWindow(DESKBOT_DISPLAY_CANVAS_X0, 0,
                            DESKBOT_PB_COORD_W, DESKBOT_PB_COORD_H);
    g_display.writePixels(s_canvas->getBuffer(),
                          (uint32_t)DESKBOT_PB_COORD_W * DESKBOT_PB_COORD_H);
    g_display.endWrite();
  }
  report_mouth_push();
}

// Audio-only updates redraw the canvas but transfer just the union of mouth
// bounds. Full expression transitions still push the complete composed face.
static void push_mouth_region() {
  auto draw_full=[]() {
    s_draw_gfx->fillScreen(DESKBOT_DISPLAY_COLOR_BLACK);
    draw_shown_layers(); pb_canvas_push();
  };
  if (!s_canvas || !s_canvas->getBuffer() || !s_mouth_canvas || !s_mouth_canvas->getBuffer()) {
    draw_full(); return;
  }
  int left = DESKBOT_PB_COORD_W, top = DESKBOT_PB_COORD_H, right = 0, bottom = 0;
  const StoredLayer* layers[] = {&s_layer_pool->mouth_closed, &s_layer_pool->mouth_open,
      &s_layer_pool->shown[static_cast<int>(pb_anim_element_layer::mouth)]};
  for (const auto* layer : layers) for (unsigned i=0; i<layer->count; ++i) {
    const auto& p = layer->prims[i];
    int x=p.x, y=p.y, w=p.w, h=p.h;
    switch (p.shape) {
      case pb_anim_element_shape::line:
        x = p.x1 < p.x2 ? p.x1 : p.x2; y = p.y1 < p.y2 ? p.y1 : p.y2;
        w = abs(p.x2-p.x1)+1; h = abs(p.y2-p.y1)+1; break;
      case pb_anim_element_shape::ellipse:
      case pb_anim_element_shape::ellipse_fill:
        x-=p.w; y-=p.h; w=2*p.w+1; h=2*p.h+1; break;
      case pb_anim_element_shape::circle:
      case pb_anim_element_shape::circle_outline:
        x-=p.r; y-=p.r; w=h=2*p.r+1; break;
      case pb_anim_element_shape::text:
        // Text metrics depend on the active font; use a complete refresh.
        draw_full(); return;
      default: break;
    }
    if (x-2<left) left=x-2; if (y-2<top) top=y-2;
    if (x+w+2>right) right=x+w+2; if (y+h+2>bottom) bottom=y+h+2;
  }
  left = left<0 ? 0 : left; top = top<0 ? 0 : top;
  right = right>DESKBOT_PB_COORD_W ? DESKBOT_PB_COORD_W : right;
  bottom = bottom>DESKBOT_PB_COORD_H ? DESKBOT_PB_COORD_H : bottom;
  if (right<=left || bottom<=top) return;
  if (right-left>128 || bottom-top>96) { draw_full(); return; }
  // Draw a clipped viewport, preserving every intersecting face layer. Avoid
  // clearing/copying the entire 136 KB PSRAM face for each mouth-only update.
  auto* full_target=s_draw_gfx;
  s_draw_gfx=s_mouth_canvas; s_draw_origin_x=left; s_draw_origin_y=top;
  s_mouth_canvas->set_viewport(right-left,bottom-top);
  draw_shown_layers();
  s_draw_gfx=full_target; s_draw_origin_x=s_draw_origin_y=0;
  g_display.startWrite();
  g_display.setAddrWindow(DESKBOT_DISPLAY_CANVAS_X0+left, top, right-left, bottom-top);
  for (int y=top; y<bottom; ++y)
    g_display.writePixels(s_mouth_canvas->getBuffer()+(y-top)*128, right-left);
  g_display.endWrite();
  s_mouth_dirty = false;
  report_mouth_push();
}

/* ── 队列 & cancel ── */

struct DisplayRequest {
  DisplayJobType type = DISPLAY_JOB_PB_ANIM_FRAMES;
  pb_anim_frame* anim_frames = nullptr;
  size_t anim_frame_count = 0;
  SemaphoreHandle_t notify_sem = nullptr;
  uint32_t epoch = 0, pts_ms = 0;
  uint16_t* image=nullptr;
  uint32_t image_ms=0;
  char command_id[33]{};
  bool image_shown=false;
};
static std::atomic<unsigned> s_pending_images{0};

static std::atomic<bool>   s_need_cancel{false};
static std::atomic<bool>   s_task_done{true};
static bool s_setup_done = false;
static QueueHandle_t       s_queue       = nullptr;
static TaskHandle_t        s_task        = nullptr;
static SemaphoreHandle_t   s_done_sem    = nullptr;
static SemaphoreHandle_t   s_caller_lock = nullptr;

static void display_free_request_anim_frames(DisplayRequest& req) {
  if (req.anim_frames) {
    pb_anim_frames_free(req.anim_frames, req.anim_frame_count);
    req.anim_frames = nullptr;
    req.anim_frame_count = 0;
  }
}

static void free_display_request(DisplayRequest& req) {
  if(req.type==DISPLAY_JOB_IMAGE && req.image){
    if(!req.image_shown) rom_tools_report(req.command_id,false,"cancelled","image_cancelled");
    free(req.image);req.image=nullptr;s_pending_images.fetch_sub(1);
  }
  if (req.type == DISPLAY_JOB_PB_ANIM_FRAMES) display_free_request_anim_frames(req);
  if (req.notify_sem) { xSemaphoreGive(req.notify_sem); req.notify_sem = nullptr; }
}

/**
 * need_cancel==false → false。
 * 否则非阻塞丢弃旧任务，见到 cancel 则清 flag 并 return true。
 * 队列空且未见 cancel → return true，保持 need_cancel。
 */
static bool poll_cancel() {
  if (!s_need_cancel.load(std::memory_order_acquire)) return false;
  DisplayRequest j{};
  while (xQueueReceive(s_queue, &j, 0) == pdTRUE) {
    if (j.type == DISPLAY_JOB_CANCEL) {
      s_need_cancel.store(false, std::memory_order_release);
      pb_vector_interp_resume();
      log_info("[DISPLAY] cancel");
      return true;
    }
    free_display_request(j);
  }
  return true;
}

/** 按 segment_ms 在 prev↔curr 之间插值渲染。@return false 若中途取消。 */
static bool pb_play_layers_interpolated(const StoredLayer* prev, const StoredLayer* curr,
                                        uint32_t segment_ms, uint16_t bg_rgb565) {
  if (poll_cancel()) return false;
  if (segment_ms == 0) {
    s_draw_gfx->fillScreen(bg_rgb565);
    draw_layers_sample(prev, curr, 1.f);
    pb_canvas_push();
    s_have_shown = true;
    return true;
  }

  uint32_t budget = segment_ms;
  if (budget > 300000u) budget = 300000u;

  const uint32_t t0 = millis();
  uint32_t pushes = 0, first_draw_ms = 0, first_push_ms = 0;
  while (true) {
    if (poll_cancel()) return false;
    const uint32_t elapsed = millis() - t0;

    float t = (float)elapsed / (float)budget;
    if (t > 1.f) t = 1.f;

    const uint32_t t_draw0 = millis();
    s_draw_gfx->fillScreen(bg_rgb565);
    draw_layers_sample(prev, curr, t);
    const uint32_t draw_ms = millis() - t_draw0;

    const uint32_t t_push0 = millis();
    pb_canvas_push();
    s_have_shown = true;
    const uint32_t push_ms = millis() - t_push0;
    pushes++;
    if (pushes == 1) { first_draw_ms = draw_ms; first_push_ms = push_ms; }

    if (elapsed >= budget) break;  // Always display the exact endpoint (t=1).
    const uint32_t spent = millis()-t0;
    const uint32_t remain = spent < budget ? budget-spent : 0;
    if (remain > 0 && remain < kPbDisplayBudgetMs) vTaskDelay(pdMS_TO_TICKS(remain));
    else vTaskDelay(pdMS_TO_TICKS(10));
  }

  const uint32_t wall = millis() - t0;
  log_warn("[PB_LAT] display_seg budget=%u wall=%u pushes=%u first_draw_ms=%u first_push_ms=%u",
           (unsigned)budget, (unsigned)wall, (unsigned)pushes,
           (unsigned)first_draw_ms, (unsigned)first_push_ms);
  return true;
}

/* ── 帧序列渲染 ── */

static void pb_render_anim_frames_timed(const pb_anim_frame* frames, size_t frame_count,
                                        uint32_t epoch, uint32_t pts_ms) {
  if (!frames || frame_count == 0) return;
  if (!speaker_mouth_frame().active) s_mouth_override = false;
  uint32_t budget_sum = 0;
  for (size_t i = 0; i < frame_count; ++i)
    budget_sum += frames[i].ms > 0 ? (uint32_t)frames[i].ms : 1u;
  const uint32_t job_t0 = millis();
  log_warn("[PB_LAT] display_job_begin frames=%u budget_sum=%u",
           (unsigned)frame_count, (unsigned)budget_sum);

  size_t seg_idx = 0;
  for (size_t i = 0; i < frame_count; ++i) {
    if (poll_cancel()) return;
    if (seg_idx >= kPbMaxAnimSegsPerChunk) {
      log_warn("[DISPLAY] anim[] truncated at %u", (unsigned)kPbMaxAnimSegsPerChunk);
      break;
    }
    const pb_anim_frame& seg = frames[i];
    uint32_t seg_ms = seg.ms > 0 ? (uint32_t)seg.ms : 1u;

    bool expired = false;
    if (epoch) {
      const uint32_t wait_at = millis();
      int32_t audio_ms = speaker_timeline_ms(epoch);
      while ((audio_ms < 0 || (uint32_t)audio_ms < pts_ms) && millis()-wait_at < 5000) {
        if (poll_cancel()) return;
        if (update_mouth() && s_have_shown) {
          push_mouth_region();
        }
        vTaskDelay(pdMS_TO_TICKS(10));
        audio_ms = speaker_timeline_ms(epoch);
      }
      const uint32_t end_ms = pts_ms + seg_ms;
      pts_ms = end_ms;
      // Skip expired mouth poses; don't replay a backlog after the speech.
      expired = audio_ms >= 0 && (uint32_t)audio_ms >= end_ms;
      if (!expired && audio_ms >= 0) seg_ms = end_ms - (uint32_t)audio_ms;
    }
    if (expired) { ++seg_idx; continue; }  // Unseen poses must not become interpolation origins.
    stored_from_pb(seg.elements, seg.element_count, s_layer_pool->curr);
    const StoredLayer* prev = s_have_prev ? s_layer_pool->prev : nullptr;
    if (!pb_play_layers_interpolated(prev, s_layer_pool->curr, seg_ms,
                                     DESKBOT_DISPLAY_COLOR_BLACK)) return;
    pb_vector_interp_resume();
    seg_idx++;
  }
  log_warn("[PB_LAT] display_job_end frames=%u budget_sum=%u wall=%u",
           (unsigned)frame_count, (unsigned)budget_sum, (unsigned)(millis() - job_t0));
}

static void execute_display_job(DisplayRequest& req) {
  if(req.type==DISPLAY_JOB_IMAGE){
    s_draw_gfx->drawRGBBitmap(0,0,req.image,DESKBOT_DRAW_W,DESKBOT_DRAW_H);
    pb_canvas_push();req.image_shown=true;rom_tools_report(req.command_id,true,"displayed");
    const uint32_t start=millis();
    while(millis()-start<req.image_ms){if(poll_cancel()) break;vTaskDelay(pdMS_TO_TICKS(10));}
    s_draw_gfx->fillScreen(DESKBOT_DISPLAY_COLOR_BLACK);draw_shown_layers();pb_canvas_push();
    free_display_request(req);
  }
  if (req.type == DISPLAY_JOB_PB_ANIM_FRAMES) {
    pb_render_anim_frames_timed(req.anim_frames, req.anim_frame_count, req.epoch, req.pts_ms);
    display_free_request_anim_frames(req);
  }
  if (req.notify_sem) { xSemaphoreGive(req.notify_sem); req.notify_sem = nullptr; }
}

static void task_loop_display(void*) {
  DisplayRequest req{};
  for (;;) {
    if ((device_bootstrap_done() || usb_bench_active()) && s_assets_changed.exchange(false,std::memory_order_acq_rel)) {
      reload_mouth_assets();
      // Audio-only providers need a local base pose before the first mouth update.
      if (!s_have_shown) {
        pb_anim_frame* base=nullptr;size_t count=0;const char* error=nullptr;
        const char* id=face_assets_idle()["clips"][0]["anim_id"] | "idle";
        if (face_assets_load_frame(id,0,1,base,count,error)) {
          pb_render_anim_frames_timed(base,count,0,0);pb_anim_frames_free(base,count);
        }
      }
    }
    (void)poll_cancel();
    if (xQueueReceive(s_queue, &req, pdMS_TO_TICKS(10)) != pdTRUE) {
      static uint32_t s_last_mouth_push = 0;
      if (update_mouth() && s_have_shown && millis()-s_last_mouth_push >= 33) {
        push_mouth_region();
        s_last_mouth_push = millis();
      }
      continue;
    }
    if (req.type == DISPLAY_JOB_CANCEL) {
      /* 空闲时 task 阻塞在 Receive，Cancel 直接出队，不经过 poll_cancel。
       * 若不在此清 flag，s_need_cancel 永久为 true，后续口型/表情全被丢掉。 */
      if (s_need_cancel.exchange(false, std::memory_order_acq_rel)) {
        pb_vector_interp_resume();
        log_info("[DISPLAY] cancel");
      }
      continue;
    }
    if (req.type == DISPLAY_JOB_END_OF_TASK) {
      s_task_done.store(true, std::memory_order_release);
      continue;
    }
    execute_display_job(req);
  }
}

static void display_enqueue_request(DisplayRequest& req, bool wait_done) {
  if (wait_done) {
    xSemaphoreTake(s_caller_lock, portMAX_DELAY);
    xSemaphoreTake(s_done_sem, 0);
    req.notify_sem = s_done_sem;
    xQueueSend(s_queue, &req, portMAX_DELAY);
    xSemaphoreTake(s_done_sem, portMAX_DELAY);
    xSemaphoreGive(s_caller_lock);
    return;
  }
  req.notify_sem = nullptr;
  if (xQueueSend(s_queue, &req, 0) != pdTRUE) {
    DisplayRequest dropped{};
    if (xQueueReceive(s_queue, &dropped, 0) == pdTRUE) free_display_request(dropped);
    if (xQueueSend(s_queue, &req, 0) != pdTRUE) {
      log_warn("[DISPLAY] queue full after drop-oldest; free submit");
      free_display_request(req);
    }
  }
}

}  // namespace

/* ──────────────────────────────────────────────────────────────────────
 * Public API — 初始化 & 任务启动
 * ──────────────────────────────────────────────────────────────────── */

void setup_display() {
  if (s_setup_done) return;
  if (!ensure_stored_layer_pool()) { log_error("[DISPLAY] StoredLayer pool unavailable"); return; }
  if (!s_queue) s_queue = xQueueCreate(kPbDisplayQueueDepth, sizeof(DisplayRequest));
  if (!s_done_sem) s_done_sem = xSemaphoreCreateBinary();
  if (!s_caller_lock) s_caller_lock = xSemaphoreCreateMutex();
  if (!s_queue || !s_done_sem || !s_caller_lock) { log_error("[DISPLAY] queue allocation failed"); return; }
  for (unsigned i=0; i<2; ++i) {
    pb_anim_frame* frames = nullptr; size_t count = 0; const char* error = nullptr;
    if (!face_assets_load_phoneme(face_assets_mouth_phoneme(i!=0), 100, frames, count, error)) {
      log_error("[DISPLAY] local mouth asset missing: %s", error); return;
    }
    layer_fill_from_pb(frames[0].elements, frames[0].element_count,
        pb_anim_element_layer::mouth,
        i ? &s_layer_pool->mouth_open : &s_layer_pool->mouth_closed);
    pb_anim_frames_free(frames, count);
  }
  g_display.setupPanel();
  if (g_display.width() <= 0 || g_display.height() <= 0) {
    log_error("[DISPLAY] panel size invalid w=%d h=%d", (int)g_display.width(), (int)g_display.height());
  }

  s_canvas = new PsramCanvas16(DESKBOT_DRAW_W, DESKBOT_DRAW_H);
  if (s_canvas && s_canvas->getBuffer()) {
    s_draw_gfx = s_canvas;
    log_info("[DISPLAY] PSRAM canvas %dx%d ok (%.0f KB)",
             DESKBOT_DRAW_W, DESKBOT_DRAW_H,
             (float)(DESKBOT_DRAW_W * DESKBOT_DRAW_H * 2) / 1024.f);
  } else {
    log_error("[DISPLAY] PSRAM canvas alloc failed, fallback direct-write (anim may tear)");
    delete s_canvas;
    s_canvas = nullptr;
    s_draw_gfx = &g_display;
  }

  s_mouth_canvas=new PsramCanvas16(128,96);
  g_display.fillScreen(DESKBOT_DISPLAY_COLOR_BLACK);
  log_info("[DISPLAY] ready ST7789 %dx%d off=%d,%d SPI mosi=%d sck=%d cs=%d dc=%d",
           (int)g_display.width(), (int)g_display.height(), DESKBOT_DISPLAY_COL_OFFSET,
           DESKBOT_DISPLAY_ROW_OFFSET, DESKBOT_DISPLAY_MOSI, DESKBOT_DISPLAY_SCK,
           DESKBOT_DISPLAY_CS, DESKBOT_DISPLAY_DC);
  g_display.setTextSize(DESKBOT_DISPLAY_BOOT_TEXT_SIZE);
  g_display.setTextColor(DESKBOT_DISPLAY_COLOR_WHITE, DESKBOT_DISPLAY_COLOR_BLACK);
  s_setup_done = true;
}

void task_setup_display() {
  if (s_task) return;
  if (!s_queue || !s_done_sem || !s_caller_lock || !s_layer_pool) {
    log_error("[DISPLAY] setup_display must succeed before task_setup_display"); return;
  }
  if (!s_task) {
    /* U8g2 drawUTF8(gb2312) 栈较深；10KB 会触发 canary。
     * prio 贴近 speaker(7)：预取可达 1s，过低会被音频饿死导致嘴形滞后。 */
    const BaseType_t rc =
        utils_task_create_pinned(task_loop_display, "display_render", 24 * 1024, nullptr, 6,
                                 &s_task, APP_CPU_NUM);
    if (rc != pdPASS) {
      log_error("[DISPLAY] task create failed rc=%d", (int)rc);
      s_task = nullptr;
    }
  }
}

void display_assets_changed() { s_assets_changed.store(true,std::memory_order_release); }
void display_mouth_trace(uint32_t duration_ms) {
  s_mouth_trace_until.store(duration_ms?millis()+std::min<uint32_t>(duration_ms,60000):0,std::memory_order_relaxed);
}

/* ──────────────────────────────────────────────────────────────────────
 * Public API — 提交 & 控制
 * ──────────────────────────────────────────────────────────────────── */

Adafruit_GFX* display_guide_target_begin(bool clear_black) {
  Adafruit_GFX* target = (s_canvas && s_canvas->getBuffer())
                             ? static_cast<Adafruit_GFX*>(s_canvas)
                             : static_cast<Adafruit_GFX*>(&g_display);
  if (clear_black) target->fillScreen(DESKBOT_DISPLAY_COLOR_BLACK);
  return target;
}

void display_guide_target_end() {
  if (s_canvas && s_canvas->getBuffer()) pb_canvas_push();
}

void display_render_submit_pb_anim_frames_owned(pb_anim_frame* frames, size_t frame_count,
                                                bool wait_done, uint32_t epoch, uint32_t pts_ms) {
  if (!frames || frame_count == 0) {
    if (frames) pb_anim_frames_free(frames, frame_count);
    return;
  }
  s_task_done.store(false, std::memory_order_release);
  DisplayRequest req{};
  req.type = DISPLAY_JOB_PB_ANIM_FRAMES;
  req.anim_frames = frames;
  req.anim_frame_count = frame_count;
  req.epoch = epoch; req.pts_ms = pts_ms;
  display_enqueue_request(req, wait_done);
}

void display_abort() {
  DisplayRequest req{};
  req.type = DISPLAY_JOB_CANCEL;
  s_need_cancel.store(true, std::memory_order_release);
  (void)xQueueSend(s_queue, &req, portMAX_DELAY);
}

void display_render_reset() { display_abort(); }

bool display_task_done() {
  return s_task_done.load(std::memory_order_acquire);
}

void display_signal_task_done() {
  DisplayRequest req{};
  req.type = DISPLAY_JOB_END_OF_TASK;
  (void)xQueueSend(s_queue, &req, portMAX_DELAY);
}

void display_set_task_done_flag() {
  s_task_done.store(true, std::memory_order_release);
}

unsigned display_render_input_queue_depth(void) {
  return (unsigned)uxQueueMessagesWaiting(s_queue);
}

bool display_cancel_pending() { return s_need_cancel.load(std::memory_order_acquire); }

bool display_submit_image_owned(uint16_t* pixels,uint32_t duration_ms,const char* command_id){
  DisplayRequest req{};req.type=DISPLAY_JOB_IMAGE;req.image=pixels;req.image_ms=duration_ms;
  strlcpy(req.command_id,command_id,sizeof(req.command_id));s_pending_images.fetch_add(1);
  if(xQueueSend(s_queue,&req,0)==pdTRUE) return true;
  req.image_shown=true;free_display_request(req);return false;
}
void display_stop_image(){if(s_pending_images.load()) display_abort();}
