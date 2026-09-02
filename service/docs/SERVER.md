# deskbot-server

主服务：VAD → 外部 funasr 服务（独立进程，9102）→ LLM（多轮 tools）→ TTS → pb 下行。环境搭建见 [../README.md](../README.md)。

## 启动

```bash
# 一键启动（含 Web 控制台）
./start.sh
SKIP_SETUP=1 ./start.sh
```

```bash
cp .env.example .env   # 必填 ARK_API_KEY / ARK_MODEL（火山方舟）
```

| 端口 | 服务 |
|------|------|
| **9000** | WebSocket + HTTP API + FastAPI Web 控制台（同一主服务进程，随 start.sh 一并启动） |

---

## Web 控制台（`:9000`）

邮箱注册 / 登录后可用：

| 路径 | 说明 |
|------|------|
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

**ESP32 设备 WS 仅需 `device_id`**；HTTP `/api/*` 走 Web 会话（无 user_id 时匿名放行）。

---

## LLM 与工具

对话采用 **JSON 回复 + `tools` 数组**；有 tools 时服务端执行后再次调用 LLM（最多 8 轮），无 tools 时走 TTS / pb。

| 工具 | 说明 |
|------|------|
| `schedule_task` | cron 定时任务增删改查（北京时间）；用户说「N 分钟后提醒我…」时**必须**调用，禁止口头答应 |
| `set_camera_follow` | 人脸舵机跟随 |
| `capture_camera` | 获取最近相机帧 |
| `register_face` | 注册 / 更新人脸档案 |
| `memory_add` / `memory_delete` | 长期记忆 |
| `session` | 查询对话 session |
| `webfetch` / `websearch` | 联网抓取 / 搜索 |
| `read` / `write` | 读写 `data/{device_id}/tmp/` |

定时任务由后台调度器每分钟轮询，到期后复用创建时的 `session_id` 作为 LLM 上下文并播报提醒。

人设与工具说明：`data/global/llm_system.txt`（全局模板，所有设备共用，不再复制到设备目录）。

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

- **`.env`**：`ARK_API_KEY` / `ARK_MODEL`（火山方舟 LLM，必填；兼容旧版 `LLM_API_KEY` / `DASHSCOPE_API_KEY`）、`DOUBAO_TTS_*`（豆包 TTS）、`DOUBAO_ASR_API_KEY` / `DOUBAO_ASR_RESOURCE_ID` / `DOUBAO_ASR_UID`（豆包 ASR 2.0 全局兜底凭证；设备级配置优先存 `devices.asr_param`）、`ASR_MODEL_DIR`、`DESKBOT_WEB_PUBLIC_HOST`（多网卡时填局域网 IP）、`DESKBOT_WEB_SECRET_KEY`（生产必设）
- **`config.yaml`**：`audio.input_codec`、`llm.model_name`、`tts.provider`（`moss-tts-nano` 默认 / `doubao`）、`server.asr_chat_device_pb_only`、`debug.asr_auto_reply`

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
