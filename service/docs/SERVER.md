# deskbot-server

主服务仅桥接外部 Flecket / MOSS Live 会话，VAD、ASR、LLM、TTS 统一由上游负责。环境搭建见 [../README.md](../README.md)。

## 启动

```bash
# 一键启动（含 Web 控制台）
./start.sh
SKIP_SETUP=1 ./start.sh
```

```bash
cp .env.example .env   # 配置 FLECKET_API_KEY 或 MOSS_API_KEY
```

| 端口 | 服务 |
|------|------|
| **9000** | WebSocket + HTTP API + FastAPI Web 控制台（同一主服务进程，随 start.sh 一并启动） |

---

## Web 控制台（`:9000`）

邮箱注册 / 登录后可用：

| 路径 | 说明 |
|------|------|
| `/robot-settings` | 机器人配置：模型选择后展开模型、音色、人设等使用设置；不显示服务地址和密钥 |
| `/model-settings` | 开发者选项 → 模型配置：当前设备的 Agent 预设、服务器地址、API Key 及实时会话连接参数，仅开发者可进入 |
| `/app` | 工作台：用量概览 |
| `/app/devices` | 我的设备：绑定、切换 `device_id` |
| `/app/scheduled-tasks` | 定时任务：cron 任务列表（东八区），可删除 |
| `/app/memories` | 记忆：按设备增删改查长期记忆 |
| `/app/face-profiles` | 人脸识别：按设备查看档案 |
| `/app/usage` | 用量看板：按设备统计 |
| `/app/settings` | 账号设置：资料、密码、LLM 模型配置 |
| `/debug/devices` | 调试：在线设备、流水线 |
| `/debug/llm` | 调试：LLM 试聊 |
| `/debug/tts` | 调试：豆包 TTS |
| `/debug/simulation` | 调试：pb 仿真 |

本地联调脚本可读取 `data/.free_api_key`（若存在）；设备链路不涉及任何 Key。

---

## WebSocket（`:9000`）

| 路径 | 说明 |
|------|------|
| `/asr_chat?device_id=` | **生产设备**：语音 + 可选 `camera_frame`；pb 下行（仅 `device_id` 鉴权） |
| `/camera_view?device_id=&debug_token=` | 调试：JPEG 预览（控制台签发 debug_token + 设备归属） |
| `/device_pipeline?role=subscriber&device_id=&debug_token=` | 调试：流水线事件（订阅侧 debug_token；生产者仅 `device_id`） |

`device_id` 别名：`device` / `deviceid` / `id`。协议：[../docs/esp32_pb_protocol.md](../docs/esp32_pb_protocol.md)。

默认 **`asr_chat_device_pb_only: true`**：设备只收 `pb_*` + PCM。

---

## HTTP API（`:9000`）

| 路径 | 说明 |
|------|------|
| `/health` | 健康检查（免 Key） |
| `/api/devices` | 在线设备列表 |

**ESP32 设备 WS 仅需 `device_id`**；HTTP `/api/*` 走有效用户的 Web 会话或 `debug_token`，未认证返回 `401`，访问他人设备返回 `403`。

---

## 外部实时会话与工具

所有设备仅支持 `flecket` 与 `moss-live`。旧 `local` 选择在启动迁移时改为
`flecket`，新设备默认 Flecket；既有外部服务配置、密钥和旧模型参数保留。
本地模型组合已从启动、设备语音入口和模型设置页移除，不再作为失败回退。

Flecket 保存的 Agent 管理基础人设、工具定义和音色。连接时读取原工具列表，合并当前设备的工具；同名内置工具采用设备端定义，其他上游工具保留。原人设后追加机器人规则和当前记忆，音色留空时沿用上游配置。

| 工具 | 执行位置 | 行为 |
|---|---|---|
| `robot_perform` | 服务端编排、ROM 执行 | 预设动作与表情；支持说话前、同时、说话后，以及纯动作 |
| `get_current_time` | 服务端 | 北京时间与 UTC |
| `memory` | 服务端 | 本设备长期记忆 list/get/add/update/delete；写入后更新会话记忆上下文 |
| `schedule_task` | 服务端 | 一次性、周期性提醒 CRUD，支持相对秒／分钟与五段 cron |
| `rom_volume` | ROM | get/set/adjust 实际扬声器音量，确认后同步设备设置 |
| `media_list` | 服务端 | 查询本设备上传的音乐与图片 |
| `play_music` | 服务端传输、ROM 播放 | 默认先简短回复再播放；speak=false 直接播放 |
| `show_image` | ROM | 默认先回复再展示；speak=false 直接展示，收到实际绘制回执才报告 displayed |
| `stop_media` | 服务端与 ROM | 停止音乐、取消图片 |

工具回传遵循 `function_call_output`。慢工具在独立任务中执行，不阻塞音频和打断接收；call_id 去重，重连或新输入阻止旧轮续写。所有工具可用 `speak=false` 安静执行。未知工具返回错误，不执行任意代码。ROM 工具只在设备声明对应能力时注册。

`robot_perform` 参数：`gesture`、`expression`（当前设备枚举，none 表示不使用）、`duration_ms`（200～3000）、`timing`（before_speech/with_speech/after_speech）、可选 `speak`。一轮最多一个表现计划；说话同时执行的轨迹和音频共用 PB/epoch，必要时补静音尾段维持设备时间轴，回传上游的已播放语音位置不包含补齐静音。纯动作等待 PB 完成回执；scheduled 只是已安排。

新的外部提醒调度器默认每 5 秒轮询，由 `scheduler.external_poll_interval_sec` 调整（1～60）。只在一个实例启用，`DESKBOT_SCHEDULER_ENABLED=0` 可关闭。提醒通过连接中的 Flecket 发起新回复，真正播完才完成；用户交互、离线、打断时重试，默认超过 5 分钟未能播报的提醒按现有过期规则记为失败。重启会恢复未完成的 running 任务。不会调用旧 ChatService 或本地 TTS。剧情／社交主动对话仍停用。

媒体页面 `/my/media`，API：`GET/POST /api/device-media`、`GET /api/device-media/{id}/file`、`DELETE /api/device-media/{id}`、`POST /api/device-media/control`。均校验当前用户的设备归属。资源按设备隔离，Agent 使用 id 访问，不能传任意本地路径或下载 URL。上传上限 20 MB，音乐保留前 3 分钟，最多 100 个资源；ffmpeg 仅启用受限本地媒体格式。

MOSS Live 继续提供直接语音桥接，以上 Agent 工具和主动提醒目前接在 Flecket。旧本地 TTS、场景口播入口 `/api/device_tts`、`/api/scene_playbook/run` 返回 410；直接舵机、表情、PB 播放接口继续可用。

---

## 设备数据（按 `device_id` 隔离）

运行时数据在 `data/{device_id}/`（**不入 Git**），共享配置在 `data/global/`：

| 文件 / 目录 | 说明 |
|-------------|------|
| `data/global/llm_system.txt` | 全局 LLM 人设模板 |
| `user_memory.json` | 长期记忆 |
| `face_profiles.json` | 人脸档案 |
| `session/` | 对话 session（10 分钟无对话开新 session） |
| `tmp/` | LLM `read` / `write` 沙箱目录 |

全局 SQLite：`data/opendesk.db`（用户、设备绑定、`scheduled_tasks`、用量等表）。

---

## 配置

- **`.env`**：`FLECKET_API_KEY` / `MOSS_API_KEY`，以及 Web 会话配置。
- **`config.yaml`**：`conversation.flecket` 配置默认地址和 Agent 清单；设备保存的地址优先。音频和 PB 设置用于媒体传输。
- **设备配置**：`conversation_provider` / `conversation_param` 保存外部服务选择和连接参数，API 不回显密钥。
- 主服务启动不再下载 Silero VAD 模型，也不依赖进程内 ONNX Runtime。

架构概要：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 联调

```bash
source .venv/bin/activate
python tools/test_client.py \
  --ws-url "ws://127.0.0.1:9000/asr_chat?device_id=deskbot_dev" \
  --input-wav demo_16k_mono.wav
```

更多脚本见 [tools/README.md](tools/README.md)。

```bash
source .venv/bin/activate
pytest tests/ -q
```

## 感知服务管理

`/perception-services` 与 `/api/perception-services` 为开发者入口，仅允许人脸 `insightface-engine` 和声纹 `wespeaker-resnet34`。`POST /api/perception-services/{name}/{operation}` 支持 install/start/stop/restart/auto-start；后者参数为布尔型 enabled。原 `/api/services` 和本地语音编排继续停用。

两个独立进程由应用启动时的限定管理器拉起并监控，自动启动选择保存在 `data/services/state.json`，应用退出时先关闭实时会话再停止感知进程。声纹首次安装需在页面安装；已有独立环境可经部署检查后复用。健康检查要求 model_ready，且人脸 embedding_ready 为真；进程启动或端口可访问不代表模型就绪。

Realtime 摄像头分支同时启动限频人脸分析，保留即时网页预览；Flecket/MOSS 上传 PCM 同时交给有界声纹采样器。识别快照供实验台和人物注册使用。本次不自动将识别姓名注入 Flecket 人设，也不替换上游 VAD、ASR、LLM 或 TTS。
