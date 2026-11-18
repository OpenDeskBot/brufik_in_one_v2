#pragma once
#include <stddef.h>
#include <stdint.h>

// Explicit, persisted local test mode. Default is the normal Wi-Fi boot path.
bool setup_usb_bench();
void task_setup_usb_bench();
bool usb_bench_active();
void usb_bench_set_mode(bool enabled);
bool usb_bench_write(const void* bytes, size_t length);
void usb_bench_emit(const char* json);
uint32_t usb_bench_tx_drops();
