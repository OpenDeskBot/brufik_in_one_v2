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

# 2. 准备配置（云端模型按需配置密钥）
cd service   # monorepo 内；独立 clone 时进入对应目录
cp .env.example .env
# 默认可使用独立本地模型服务；选择云端模型时，按 provider 配置相应密钥。
# 可选：ARK_API_KEY 与 ARK_MODEL（火山方舟，仅当设备在「机器人配置 → 大脑 LLM」中选 ark 时需要）

# 3. 一键启动（自动建 venv、下载 VAD 模型、起主服务 + Web 控制台）
chmod +x start.sh
./start.sh
```

主服务本身只下载 Silero VAD 模型（约 2.3MB，语音切句用）；**ASR / TTS / 声纹 / 本地 LLM 模型均由各独立外部服务安装时自备**（独立 venv + 服务目录内模型，不再进主服务）——启动后在控制台菜单「外部服务」（高级 → 外部服务）安装并启动所需服务即可，详见 [docs/external_services.md](docs/external_services.md)。

**第二次及以后**若 `.venv` 已完整，直接 `./start.sh` 会自动跳过 pip 安装；也可显式：

```bash
SKIP_SETUP=1 ./start.sh
# 或
FAST_START=1 ./start.sh
```

| 服务 | 地址 | 用途 |
|------|------|------|
| **Web 控制台** | `http://<本机IP>:9000/` | 注册登录、设备绑定、定时任务、记忆等 |
| **设备 WebSocket** | `ws://<本机IP>:9000/asr_chat?device_id=<id>` | ESP32 语音对话主链路（仅需 `device_id`，无 API Key / PIN） |

Web / HTTP 接口与调试订阅使用**控制台登录会话**；调试 WebSocket（`/camera_view`、`/device_pipeline` 订阅侧）在「调试台」页面签发 `debug_token`。本地联调脚本可读取 `data/.free_api_key`（若存在）。

---

## 快速使用

### 1. 打开控制台

浏览器访问 `http://<本机IP>:9000/` → **注册**账号 → 登录进入工作台。

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

外部 Agent 模式由上游完成 ASR、LLM 和 TTS。兼容的本地模型组合仍可使用外部 MOSS-TTS-Nano 或豆包端点；端点由各自服务运维，后台不再负责安装和启停。

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


## SOP 工程约定与静态控制台

服务端已接入显式路由/权限声明、启动校验、统一错误与请求统计、Service 事务、直接 SQL DAO 和调度生命周期。详细目录及兼容约定见 [架构](docs/ARCHITECTURE.md)，实际验收与尚存旧测试问题见 [SOP 实施记录](docs/SOP_COMPLIANCE.md)。

控制台使用静态资源和 JSON API；原页面 URL 保留。修改 `src/deskbot_server/public/{site,admin}/views/` 后运行：

```bash
python tools/build_public.py
```

生成的 `public/shared/views.json` 随代码发布。Python 服务不执行页面模板、不依赖 Node；页面内容测试需要 Node 22。可选真实浏览器回归：

```bash
npm ci
# Chrome/Chromium 路径按本机设置；测试会自行启动临时服务与临时数据库。
CHROME_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" npm run test:browser
```

`GET /health` 统一返回 `{"status":"ok"}`。所有 HTTP 响应带 `X-Request-ID`。多实例部署时，只在唯一调度实例启用提醒调度，其余设置 `DESKBOT_SCHEDULER_ENABLED=0`。正常停止会等待在途提醒结束。

### 外部 Agent 实时语音

「机器人设置 → 对话方式」选择 **外部 Agent（flecket / Realtime）**，
从「已保存的 Agent」选择预设，或填写其他兼容服务地址及 API Key，
点击「保存对话方式」，设备自动重连。人设、音色留空时继承上游 Agent 配置，
上游工具定义也会保留；已接入该 Agent 的 `get_current_time` 客户端工具，
其他未接入工具返回明确错误，不执行任意工具。
`config.yaml` 的 `conversation.flecket` 配置服务默认地址和 Agent 清单。
已加入相邻 flecket 项目的三个 Agent，默认「豆包llm_tts」
（`agent_265823d9ffd34dd7`）。当前保存的地址仍为 `127.0.0.1:9110`；
部署到云端后修改主机名或设置 `FLECKET_URL`（例如
`wss://voice.example/v1/realtime?model=agent_265823d9ffd34dd7`）。
设备已保存的地址优先于服务默认地址，切换部署时也需更新该设备的地址。
也可在服务端 `.env` 设置 `FLECKET_API_KEY`，后台密钥框留空。
MOSS Live 与 flecket 的配置分别保留，API 不回显密钥。
本地独立服务管理页面及 `/api/services` 全部接口已移除；主服务启动时不再
发现、安装、拉起或守护本地模型进程，也不再依赖本地服务状态进行预热。

每个设备连接对应一条持久 Flecket Realtime WebSocket：设备 Opus 解码成
16kHz PCM16，通过 `input_audio_buffer.append` 持续上传；flecket 执行
VAD、ASR、LLM、TTS，并返回 48kHz PCM16，服务端连续重采样为 16kHz Opus 下发。
无需启动本地模型组合中的 ASR、LLM 和 TTS 服务。

默认在播报期间及播完后 450ms 回传静音，防止扬声器回声形成自我打断循环；
此时无法在播报期间插话。设备回声消除稳定后，可在后台开启「允许播放时插话」。
开启后，语音开始立即取消旧播放，并以设备已确认的累计播放位置发送
`conversation.item.truncate`。`response.done` 只表示生成结束，
播放完成依照设备回执报告 `flecket.playback`。可选摄像头仅在新语音开始时
发送最近一帧；图片能力取决于 flecket 上游模型。该接入与 MOSS Live 一样
提供直接语音会话，不注册本地机器人业务工具；已接入上游 Agent 的查询时间工具。

本地模型组合的系统默认 LLM 仍使用原有硅基流动配置，flecket 不作为 LLM 候选。
协议及播放回归使用内存传输替身；真实服务与设备联调需运行环境网络权限。
