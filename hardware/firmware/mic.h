#pragma once

#include <Arduino.h>
#include <stdint.h>
#include "deskbot_config.h"

#define PDM_MIC_CLK DESKBOT_PDM_MIC_CLK
#define PDM_MIC_DATA DESKBOT_PDM_MIC_DATA

/* 麦克风上行：
 * - setup_mic：I2S0 + OpusEncoder
 * - mic_set_ws_state：原子写门控（AEC 已做回声消除，不再与 speaker 互斥）
 * - ws_ok：enhance → send_to_ws(pcm, false) 满 5 帧发送
 * - ≥30s：send_to_ws(nullptr, true)，hdr 带 "flush":1（不足 5 帧也发） */

static constexpr size_t kMicFrameSamples = 320; /* 20ms @ 16kHz */

struct MicFrame {
  int16_t pcm[kMicFrameSamples];
};

enum MicWsState : int8_t {
  kMicWsError = 0,
  kMicWsOk = 1,
};

bool setup_mic();
/** 相机 GDMA 重 init 后必须再调：否则 I2S0 PDM 常读空/挂死，表现为不收音。 */
bool mic_restart_pdm();
void task_setup_mic();

void mic_set_ws_state(MicWsState s);

/** 本段已成功入 WS TX 的 PCM 样点数（调试/日志）。 */
uint32_t mic_uplink_samples_sent(void);

/** ws_ok。 */
bool mic_capture_allowed(void);

void enhance_voice(int16_t* data, size_t length);
void enhance_voice_reset(void);

