#pragma once

#include <stddef.h>
#include <stdint.h>

bool setup_camera_ws_transport();
bool task_setup_camera_ws_transport();
bool camera_ws_transport_ready();
void camera_ws_transport_on_link_down();
void camera_ws_transport_on_link_up();

/** 接管 packed 所有权；链路未就绪时也会释放。只保留最新一帧。 */
bool camera_ws_submit_latest(uint8_t* packed, size_t packed_len);

/** 根据实际发送耗时和覆盖次数自适应得到的抓帧间隔。 */
uint32_t camera_ws_capture_interval_ms();
