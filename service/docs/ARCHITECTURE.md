# deskbot-server 架构（概要）

分层：**ws / web**（协议入口）→ **application**（用例）→ **core**（端口 Protocol）← **infrastructure**（ASR / LLM / TTS 实现）。**pb**、**vision** 不依赖 ws。

## 主链路（语音对话）

```
/asr_chat
  → VAD + 外部 funasr 服务（FunAsrAdapter，9102）
  → run_chat_turn
      → ensure_active_session（按 device 加载历史）
      → complete_llm_with_tool_loop（多轮 tools，最多 8 轮）
      → TTS（豆包 doubao）→ 音素口型
      → pb.wire 组包
      → DownlinkPort.send_pb_wire
```

摄像头：`camera_frame` + JPEG → `CameraFaceService` → 调试订阅 / 人脸跟踪 / 舵机跟随。

## 定时任务

```
ScheduledTaskScheduler（60s 轮询）
  → claim_due_tasks（SQLite scheduled_tasks，东八区 cron）
  → run_chat_turn（reuse_session_id，强制 TTS 提醒）
  → finish_scheduled_task
```

任务由 LLM `schedule_task` 工具创建；**不由服务端从用户话术正则推断**。

## 模块一览

| 目录 / 模块 | 职责 |
|-------------|------|
| `src/deskbot_server/ws/` | WebSocket 会话与流水线（`/asr_chat`、`/device_pipeline`、`/camera_view`）、pb 下行队列、鉴权门禁（device_id / debug_token） |
| `src/deskbot_server/web/` | FastAPI 控制台（`auth_bp`/`app_bp` 设备/任务/记忆/人脸，`app2c_bp` 2C 页面，`debug_bp` 调试） |
| `src/deskbot_server/controller/` | HTTP/WS 路由与鉴权装饰器（`require_device_ws` / `require_api_auth` / debug_token） |
| `src/deskbot_server/db/` | SQLAlchemy 模型（User、Device、ScheduledTask、DeviceUsage、DeviceProfileFace、DeviceMemory、DeviceSession 等） |
| `src/deskbot_server/service/user_service.py` | 注册/登录、设备绑定解绑、用户与设备列表 |
| `src/deskbot_server/service/live_service.py` | 在线设备 live 状态（wander/sleep/gaze），对话时暂停 |
| `src/deskbot_server/service/application/chat_flow.py` | 单轮对话编排（LLM + TTS + pb） |
| `src/deskbot_server/service/application/llm_tool_loop.py` | LLM 多轮 tool-call |
| `src/deskbot_server/service/application/llm_tool_runner.py` | 执行 `tools` 指令 |
| `src/deskbot_server/service/application/scheduled_task_scheduler.py` | 到期任务调度 |
| `src/deskbot_server/dao/device_session_mapper.py` | 对话 session（SQLite `device_session` 表） |
| `src/deskbot_server/utils/device_data.py` | 按设备解析 `data/{device_id}/` 数据目录；共享配置在 `data/global/` |
| `src/deskbot_server/pb/` | 表情、口型、舵机、wire 组包 |
| `src/deskbot_server/vision/` | 人脸检测、embedding、跟随 |
| `src/deskbot_server/infrastructure/` | ASR / LLM / TTS 适配实现 |

## WebSocket

| 路径 | 生产必需 |
|------|----------|
| `/asr_chat?device_id=` | 是（`device_id` 必须，仅此一项鉴权） |
| `/camera_view`、`/device_pipeline` | 否（调试；Web 侧 debug token + 设备归属） |

单进程 asyncio；ASR 推理在外部 funasr 独立进程（funasr 完全独立化，v1.2.0 起主服务不再加载 FunASR）。重 CPU 走 `asyncio.to_thread`；人脸推理由 `max_concurrent_face_infer` 限流。

## 装配（`main.py`）

```
init_database()
build_chat_service()
DeviceWsService + LiveService + CameraFaceService + ScheduledTaskScheduler（60s 轮询）
uvicorn FastAPI (:9000)
```

Web 控制台默认由同进程 `mount_web` 挂载（`start.sh` 也可另起）。

## 测试

```bash
source .venv/bin/activate
pytest tests/ -q
```
