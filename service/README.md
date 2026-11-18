# deskbot-server

ESP32 桌面机器人后端：设备上传语音（可选摄像头），外部 Flecket / MOSS Live 服务完成 **VAD、ASR、LLM、TTS**，主服务负责媒体桥接、设备与工具执行，经 **pb** 协议下发 PCM、屏幕动画与舵机指令。

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
# 配置 FLECKET_API_KEY（或 MOSS_API_KEY），在「开发者选项 → 模型配置」设置外部 Agent。

# 3. 一键启动（自动建 venv、起主服务 + Web 控制台）
chmod +x start.sh
./start.sh
```

主服务不下载或加载语音模型，不再装配本地 ASR → LLM → TTS。所有设备直接连接外部 Flecket 或 MOSS Live 会话；上游服务的部署与模型管理由各自服务负责。

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

在 ROM 配网页填写完整启动接口：

```
http://<本机IP>:9000/api/device/bootstrap
```

ROM 读取接口返回的资源、主 WebSocket 和相机地址。服务端在 `server.device_bootstrap` 配置下发的地址，支持独立域名／端口；修改后设备下次启动获取，无需重刷 ROM。当前持久对话由外部 Agent 管理，不通过独立 flush 消息触发识别和回复。完整格式与升级要求见 [设备协议](docs/esp32_pb_protocol.md)。

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
| 记忆 | 每日交流与操作记录、按人物分块的有来源资料、搜索、纠正及联动遗忘 |
| 人脸识别 | 按设备查看人脸档案 |
| 用量看板 | 按 Key / 设备查看 ASR、人脸、LLM、TTS 字节统计 |
| 调试台 | 设备在线、LLM 试聊、豆包 TTS、流水线等 |

Flecket 实时 Agent 已接入动作与表情、人脸／声纹注册与识别查询、设备记忆、定时提醒、ROM 音量、音乐播放和图片展示。当前时间按每轮动态加入系统上下文，不再提供时间工具。声音与人设保留上游配置，工具按当前设备能力动态合并。媒体可在「媒体库」上传，详见 [docs/SERVER.md](docs/SERVER.md)。

记忆保存在 `data/{device_id}/memory/`：`chat_YYYYMMDD.txt` 自动记录每日发言与操作，`person_{name}.txt` 保存有来源的人物资料。Agent 使用 `chat_search`、`chat_read`、`person_read`、`person_update`、`memory_forget`；人物资料按本轮识别结果加载，不再把最新 30 条混合记忆固定塞入会话。旧 SQLite／JSON 记忆启动时迁入“未归属”，详见 [文件记忆](docs/SERVER.md#文件记忆)。

---

## 与 ESP32 的交互（概要）

单条 WebSocket：`/asr_chat?device_id=<id>`。上行、下行均采用 **「JSON + 紧随一条 binary」**，长度由 **`next_bin_len`** 声明，**不用 base64**。

```
ESP32 麦克风 / 摄像头 → deskbot-server 媒体桥接 → 外部 Flecket / MOSS Live
ESP32 播放 / 屏幕 / 舵机 ← PB / Opus 下行 ← 上游实时语音结果
```

语音识别、对话生成与语音合成统一由外部实时服务完成；主服务不安装、启停或回退到本地模型组合。

### 上行（设备 → 服务端）

| JSON `type` | binary | 说明 |
|-------------|--------|------|
| `audio` + `next_bin_len` | Opus 或 PCM | 16kHz 单声道语音流；持久会话不以 flush 终止 |
| `camera_frame` + `next_bin_len` | JPEG | 可选；人脸检测与调试预览 |
| `pb_ack` | 无 | 播放缓冲回压 |
| `device_hello` / `rom_result` | 无 | 能力握手／设备工具结果 |

### 下行（服务端 → 设备）

主连接下发 **`pb_start` / `pb_chunk` / `pb_end` / `pb_single` / `pb_cancel`** 和 ROM 工具命令；音频为 16kHz 单声道 Opus 或 s16le，与 JSON 打包在同一条 binary message 中。纯音频续包和执行回执可协商紧凑格式。

完整字段见 **[docs/esp32_pb_protocol.md](docs/esp32_pb_protocol.md)**。

### 固件要点

1. 主／相机连接 URL 由启动接口下发，ROM 追加稳定 **`device_id`**。当前该标识尚无设备凭证校验，不等同于身份认证。
2. 基础消息使用 **4字节 JSON 长度＋JSON＋媒体的单条 binary message**；不使用 TEXT＋独立 BIN。
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

`GET /health` 统一返回 `{"status":"ok"}`。所有 HTTP 响应带 `X-Request-ID`。多实例部署时，外部实时服务负责对话生成；新的提醒调度器只向外部 Agent 发起播报，不装配本地语音模型。

### 外部 Agent 实时语音

「机器人设置 → 对话方式」选择 **外部 Agent（flecket / Realtime）**，
从「已保存的 Agent」选择预设，或填写其他兼容服务地址及 API Key，
点击「保存对话方式」，设备自动重连。人设、音色留空时继承上游 Agent 配置，
上游工具定义也会保留；连接时合并当前设备的工具清单和使用规则。
`robot_perform` 使用设备的动作／表情枚举，服务端工具与 ROM 工具均绑定当前设备；
未知工具返回明确错误。
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
提供直接语音会话。Flecket 模式另提供机器人业务工具；MOSS Live 保留直接语音桥接。

本地模型组合已从启动、设备连接和设置入口移除。旧设备保存的 `local` 模式在下次启动时迁移为 `flecket`，既有外部服务地址、密钥及旧模型参数保留。缺少外部服务配置时会明确连接失败，不回退本地模型。
旧本地对话调度不再启动。提醒由新的外部 Agent 调度器处理，默认每 5 秒轮询；设备忙碌、离线或用户打断时延后重试，设备确认播完才记为完成。剧情／社交主动对话保持停用。
`/api/device_tts` 和 `/api/scene_playbook/run` 返回 410，原独立 ASR / LLM / TTS 配置接口已移除。
协议及播放回归使用内存传输替身；真实服务与设备联调需运行环境网络权限。

### 媒体与 ROM 工具

「媒体库」支持按设备上传图片、音乐，预览、删除、播放／停止和音量控制。图片会等比放大或缩小到 284×240 屏幕边界，居中完整展示；宽高比不一致时保留黑边，不裁切、不拉伸。适配在上传时完成，已入库图片需要在服务更新后重新上传才能应用新规则，无需重刷 ROM。音乐由 ffmpeg 预处理为单声道 16kHz，再用现有 Opus/PB 链路流式播放。每个上传最多 20 MB，音乐保留前 3 分钟，每台设备最多 100 个资源。当前提供用户上传的资料库，不提供在线曲库检索。

ROM 新增 `rom_tools_v1` 和 `image_rgb565_v1` 能力。音量工具等待实际扬声器设置回执，图片等待显示任务确认已绘制；旧 ROM 不注册这些工具，需刷入新版固件。普通音乐播放沿用 `pb_stream_v1`。默认播报回声保护同样适用于音乐；未开启播放时插话时，可在媒体库手动停止。

### 人脸与声纹感知服务

开发者菜单「感知服务」（`/perception-services`）仅管理 `insightface-engine`（9103）与 `wespeaker-resnet34`（9104），提供安装、启动、停止、重启和自动启动。进程、模型和依赖仍在各自 `externals/` 独立环境中；不恢复本地 ASR/LLM/TTS 编排，也不提供通用命令执行入口。服务状态会校验模型就绪字段，而不仅是 HTTP 端口。

摄像头预览与限频人脸分析并行，识别结果写入原有人物快照；跟随仅在当前设备明确开启时发送低优先级舵机指令。声纹从实时上传 PCM 中采样短语音片段，安静约 600ms 后识别，连续说话最多取 8 秒；每设备最多一次在途识别，不保存原始录音。机器人播报／音乐期间和尾音窗口暂停采样，避免把扬声器当作用户。语音片段筛选不是本地 ASR 或对话流程，特征推理仍调用独立声纹服务。

Flecket 每轮回复携带结构化人脸／声纹系统事件，只引用当前设备、本轮且未过期的识别结果；定时提醒通过同一格式携带任务、计划时间和触发时间。动态数据放入该次回复的 system instructions，不写成用户发言，也不积累旧识别上下文。格式、时序及验证范围见 [动态系统事件](docs/system-events.md)。
