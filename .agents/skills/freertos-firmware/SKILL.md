---
name: freertos-firmware
description: >-
  Deskbot ESP32-S3 固件 FreeRTOS 开发规范：setup_xxx / task_setup_xxx / task_loop_xxx
  三件套、init 契约（业务函数不重复判空）、s_ 静态变量、执行器 xQueue+cancel 模式、
  utils_task_create_pinned、boot 阶段顺序。在新增/修改 hardware/firmware 模块、
  FreeRTOS 任务、队列、并发或启动流程时使用。
---

# FreeRTOS 固件开发规范

适用于 `hardware/firmware/`（Arduino + ESP-IDF FreeRTOS）。

## 模块分层

| 层级 | 文件 | 职责 |
|------|------|------|
| 入口 | `deskbot_rom.ino` | 只编排 `setup()`；`loop()` 仅 cmd / wifi / 诊断 |
| 配置 | `deskbot_config.h` | 引脚、网络、调参宏 |
| 硬件/执行器 | `xxx.{h,cpp}` | 一模块一对文件；`.h` 薄 API |
| 领域/传输 | `pb_model`、`pb_runtime`、`ws_transport` | 协议与调度 |
| 工具 | `utils/utils.h` | 打包帧、HTTP、任务创建 |

**禁止**在 `loop()` / `setup()` 做阻塞重活（Opus、JPEG、长渲染、HTTP）。

## 三件套（硬性）

凡含 **xQueue 和/或 xTask** 的模块，`.h` 暴露 `setup_xxx` / `task_setup_xxx`；`.cpp` 实现 **`task_loop_xxx`**（原 `xxx_task` 统一改名，仅 FreeRTOS 入口，不对外调用）：

| 函数 | 职责 | 幂等 |
|------|------|------|
| `setup_xxx()` | 硬件、**xQueue/Semaphore**、静态资源；**在此完成全部非 task 初始化** | 是 |
| `task_setup_xxx()` | **仅** `utils_task_create_pinned(task_loop_xxx, ...)` | 是（`if (s_task) return`） |
| `task_loop_xxx(void* arg)` | 任务主循环；`.cpp` 内 `static` 或匿名 namespace | — |

**Init 契约**：`setup_xxx` + `task_setup_xxx` 成功后，模块内其它 API **不得**再判 `s_queue` / `s_task` / `s_setup_ok` 是否就绪（调用方保证 boot 顺序）。仅 `task_setup_xxx` 可校验前置 `setup_xxx` 是否成功。

Boot 顺序由 `deskbot_rom.ino` 保证；业务层直接调 enqueue / abort / depth，不写防御性 `if (!s_queue) return`。

## 静态变量

- 文件内静态变量 **一律 `s_` 前缀**：`s_task`、`s_queue`、`s_need_cancel`
- 常量：`static constexpr kCamelCase` 或 `DESKBOT_*`（config）
- 跨任务标志：`std::atomic` + `s_` 前缀，如 `s_state_ws`

## 执行器：xQueue + 队尾 cancel

speaker / display / head 统一模式：

1. `std::atomic<bool> s_need_cancel`
2. `xxx_abort()`：`s_need_cancel=true` + 队尾 `Cancel` job
3. `poll_cancel()`（仅 task 内）：见 `speaker.cpp`
4. 长循环分块调用 `poll_cancel()`

## 任务创建

```cpp
void task_setup_xxx(void) {
  if (s_task) {
    return;
  }
  const BaseType_t rc = utils_task_create_pinned(
      task_loop_xxx, "xxx", kXxxTaskStack, nullptr, kXxxTaskPrio, &s_task, APP_CPU_NUM);
  if (rc != pdPASS) {
    log_error("[XXX] task create failed");
  }
}
```

栈大小（字节）写在 `.cpp` 顶部并注释原因；用 `log_stack_heap_tick()` 观察 high-water。

## 命名

| 类别 | 规则 |
|------|------|
| 函数 | `snake_case` |
| 任务循环 | `task_loop_<模块名>` |
| 宏 | `DESKBOT_*` |
| 日志 | `[MODULE]` |

## Boot 顺序（不可乱）

1. 相机探针 → `camera_deinit()` → I2S / WiFi
2. `setup_pb_runtime` 先于 `setup_ws_transport`
3. `task_setup_display` / `task_setup_head` 先于 `task_setup_pb_runtime`
4. WiFi 后 `setup_camera()` + `mic_restart_pdm()`
5. `head_servo_boot_attach()` 在相机之后

## 新模块模板

### xxx.h

```cpp
#pragma once

bool setup_xxx(void);
void task_setup_xxx(void);
void xxx_abort(void);
unsigned xxx_input_queue_depth(void);
```

### xxx.cpp（骨架）

```cpp
#include "xxx.h"
#include "logger.h"
#include "utils/utils.h"

#include <atomic>
#include "freertos/queue.h"
#include "freertos/task.h"

namespace {

struct Job { /* ... */ };

constexpr uint32_t kXxxTaskStack = 8 * 1024;

QueueHandle_t s_queue = nullptr;
TaskHandle_t s_task = nullptr;
std::atomic<bool> s_need_cancel{false};

static void task_loop_xxx(void* arg);
static bool poll_cancel();

static void task_loop_xxx(void* /*arg*/) {
  for (;;) { /* poll_cancel + xQueueReceive + work */ }
}

}  // namespace

bool setup_xxx(void) {
  if (!s_queue) {
    s_queue = xQueueCreate(DESKBOT_PB_EXECUTOR_QUEUE_DEPTH, sizeof(Job));
  }
  return s_queue != nullptr;
}

void task_setup_xxx(void) {
  if (s_task) {
    return;
  }
  utils_task_create_pinned(task_loop_xxx, "xxx", kXxxTaskStack, nullptr, 3, &s_task, APP_CPU_NUM);
}

void xxx_abort(void) {
  s_need_cancel.store(true, std::memory_order_release);
  Job j{};
  xQueueSend(s_queue, &j, 0);
}

unsigned xxx_input_queue_depth(void) {
  return (unsigned)uxQueueMessagesWaiting(s_queue);
}
```

## 检查清单

- [ ] 含 task/queue 的模块是否有 `setup_` / `task_setup_` / `task_loop_` 三件套？
- [ ] 队列是否在 `setup_xxx` 创建，而非 `task_setup_xxx`？
- [ ] 业务 API 是否已去掉 `if (!s_queue)` / `if (!s_task)`？
- [ ] 静态变量是否均为 `s_` 前缀？
- [ ] abort / poll_cancel 是否与 speaker 语义一致？

## 反模式

- `xxx_task` 命名（应改为 `task_loop_xxx`）
- 在 enqueue/abort/depth 里重复检查 init
- 在 `task_setup_xxx` 里创建 queue（应放在 `setup_xxx`）
- 裸 `xTaskCreate`、loop 里阻塞重活、无 `_owned` 跨 task 传堆指针
