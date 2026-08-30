# deskbot-server

ESP32 桌面机器人后端：设备上传语音（可选摄像头），服务端完成 **VAD → ASR → LLM → TTS**，经 **pb** 协议下发 PCM、屏幕动画与舵机指令。

本目录位于 monorepo [`brufik_in_one/service/`](../)。固件见 [`../hardware/`](../hardware/)。

**License:** [GPL-3.0](LICENSE)

---

## 最快部署（3 步）

**环境：** Ubuntu 22.04 / 24.04（或 macOS / Windows Git Bash）、Python **3.11**、`ffmpeg`、可访问外网（首次下载模型与 pip 包）。

```bash
# 1. 系统依赖（Ubuntu）
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev ffmpeg curl git

# 2. 配置大模型 Key（必填）
cd service   # monorepo 内；独立 clone 时进入对应目录
cp .env.example .env
# 编辑 .env，填写 ARK_API_KEY 与 ARK_MODEL（火山方舟）

# 3. 一键启动（自动建 venv、下载模型、起主服务 + Web 控制台）
chmod +x start.sh
./start.sh
```

首次启动约需数分钟（ASR 模型约 900MB）。**第二次及以后**若 `.venv` 已完整，直接 `./start.sh` 会自动跳过 pip 安装；也可显式：

```bash
SKIP_SETUP=1 ./start.sh
# 或
FAST_START=1 ./start.sh
```

| 服务 | 地址 | 用途 |
|------|------|------|
| **Web 控制台** | `http://<本机IP>:5050/` | 注册登录、设备绑定、定时任务、记忆等 |
| **设备 WebSocket** | `ws://<本机IP>:9000/asr_chat?device_id=<id>` | ESP32 语音对话主链路（仅需 `device_id`，无 API Key / PIN） |

Web / HTTP 接口与调试订阅使用**控制台登录会话**；调试 WebSocket（`/camera_view`、`/device_pipeline` 订阅侧）在「调试台」页面签发 `debug_token`。本地联调脚本可读取 `data/.free_api_key`（若存在）。

---

## 快速使用

### 1. 打开控制台

浏览器访问 `http://<本机IP>:5050/` → **注册**账号 → 登录进入工作台。

### 2. 鉴权方式（Web / HTTP / 调试订阅）

控制台注册登录后，Web / HTTP 接口直接走会话；调试 WebSocket 订阅（`/camera_view`、`/device_pipeline`）在「调试台」页面获取 `debug_token`。设备固件**不需要**任何 Key。

### 3. 绑定设备

**我的设备** 中添加 `device_id`（与固件一致，如 `deskbot_e83dc1faea30`）即可绑定。后续管理定时任务、记忆、人脸档案时需先**选择设备**。

### 4. 连接设备对话

固件连接（与 `hardware/firmware` 一致）：

```
ws://<本机IP>:9000/asr_chat?device_id=<你的device_id>
```

说话后发送 `flush`，服务端识别 → 调用 LLM → TTS → pb 下行播放。

### 5. 无硬件时本地试跑

```bash
source .venv/bin/activate
python tools/test_client.py \
  --ws-url "ws://127.0.0.1:9000/asr_chat?device_id=deskbot_dev" \
  --input-wav demo_16k_mono.wav
```

WAV 须 **16 kHz / mono / s16le**。麦克风实时测试见 [tools/README.md](tools/README.md)。

### 6. 控制台常用功能

| 菜单 | 说明 |
|------|------|
| 工作台 | 用量概览与快捷入口 |
| 我的设备 | 绑定 / 切换 `device_id` |
| 定时任务 | 查看 LLM 创建的 cron 任务（北京时间），可删除 |
| 记忆 | 按设备管理长期记忆（注入 LLM） |
| 人脸识别 | 按设备查看人脸档案 |
| 用量看板 | 按 Key / 设备查看 ASR、人脸、LLM、TTS 字节统计 |
| 调试台 | 设备在线、LLM 试聊、豆包 TTS、流水线等 |

语音对话中，LLM 可通过工具创建定时提醒（`schedule_task`）、读写设备临时文件、联网搜索等，详见 [docs/SERVER.md](docs/SERVER.md)。

---

## 与 ESP32 的交互（概要）

单条 WebSocket：`/asr_chat?device_id=<id>`。上行、下行均采用 **「JSON + 紧随一条 binary」**，长度由 **`next_bin_len`** 声明，**不用 base64**。

```
┌──────── ESP32 ────────┐                    ┌──── deskbot-server ────┐
│ 麦克风 Opus/PCM       │  audio + binary    │ VAD → 外部 funasr 服务（HTTP）→ 文本     │
│ 可选 JPEG             │  camera_frame      │ → 方舟 LLM + tools │
│ flush / pb_ack        │ ─────────────────► │ → 豆包 TTS + 音素口型   │
│                       │                    │ → 组 pb + PCM           │
│ 播放 PCM + 画屏       │  pb_* + binary     │                         │
│ 舵机 / 音量 / 帧率    │ ◄───────────────── │                         │
└───────────────────────┘                    └─────────────────────────┘
```

TTS 使用火山引擎豆包（`tts.provider: doubao`），凭证配置见 `.env` 与调试台「TTS 调试」。

### 上行（设备 → 服务端）

| JSON `type` | binary | 说明 |
|-------------|--------|------|
| `audio` + `next_bin_len` | Opus 或 PCM | 语音流；段结束发 `flush` 触发识别与回复 |
| `camera_frame` + `next_bin_len` | JPEG | 可选；人脸检测与调试预览 |
| `pb_ack` | 无 | 播放缓冲回压 |
| `ping` | 无 | 保活 |

### 下行（服务端 → 设备）

默认 **`asr_chat_device_pb_only: true`**：只处理 **`pb_start` / `pb_chunk` / `pb_end` / `pb_single`**，以及紧随的 **s16le PCM**（24 kHz mono）。

完整字段见 **[docs/esp32_pb_protocol.md](docs/esp32_pb_protocol.md)**。

### 固件要点

1. URL 须带稳定 **`device_id`**（设备链路仅此一项鉴权，无需 API Key / PIN）。
2. JSON 与 binary **严格成对、顺序发送**。
3. 周期性 **`pb_ack`** 做播放回压。
4. 固件栈：Arduino-ESP32 **3.3.9** / IDF **5.5.4**（pioarduino），详见 [`../hardware/README.md`](../hardware/README.md)。

---

## 数据与目录

| 路径 | 说明 |
|------|------|
| `data/opendesk.db` | 用户、设备绑定、定时任务（SQLite） |
| `data/.free_api_key` | 免费体验 Key（勿提交 Git；本地联调脚本可读取） |
| `data/{device_id}/` | 按设备隔离的配置、session、记忆等（**不入 Git**） |
| `data/global/` | 全局共享配置（LLM 人设模板 `llm_system.txt` 等） |

---

## 文档索引

| 文档 | 内容 |
|------|------|
| [docs/api_interfaces.md](docs/api_interfaces.md) | Web 控制台与 deskbot 设备服务接口清单 |
| [docs/esp32_pb_protocol.md](docs/esp32_pb_protocol.md) | ESP32 通信、鉴权、pb 协议 |
| [docs/SERVER.md](docs/SERVER.md) | 主服务 API、配置、LLM 工具 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 代码分层与模块 |
| [tools/README.md](tools/README.md) | 本地联调脚本 |
| [docs/README.md](docs/README.md) | 文档目录 |

[CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md)
