#pragma once
#include <Arduino.h>

bool setup_device_bootstrap();
bool task_setup_device_bootstrap();
bool device_bootstrap_done();
bool device_bootstrap_stream_enabled();
bool device_bootstrap_camera_enabled();
const char* device_bootstrap_stream_url();
const char* device_bootstrap_camera_url();
String device_bootstrap_status_json();
