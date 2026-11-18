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
| `/asr_chat?device_id=` | **生产设备**：语音 + 可选 `camera_frame`；pb 下行（当前只检查 `device_id`，尚无设备凭证校验） |
| `/camera_uplink?device_id=` | 独立 JPEG 上行，不抢占主连接；当前只检查 device_id |
| `/camera_view?device_id=&debug_token=` | 调试：JPEG 预览（控制台签发 debug_token + 设备归属） |
| `/device_pipeline?role=subscriber&device_id=&debug_token=` | 调试：流水线事件（订阅侧 debug_token；生产者仅 `device_id`） |

ROM 只配置完整 HTTP/HTTPS 启动接口（默认 `/api/device/bootstrap`）；对外主连接、相机和资源地址在 `server.device_bootstrap` 中配置，返回给 ROM。见设备协议中的 schema 2 示例。

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

Flecket 保存的 Agent 管理基础人设、工具定义和音色。连接时读取原工具列表，合并当前设备的工具；同名内置工具采用设备端定义，其他上游工具保留。原人设后追加固定机器人规则，人物资料按本轮身份和最新文件版本动态加入回复提示词，音色留空时沿用上游配置。

| 工具 | 执行位置 | 行为 |
|---|---|---|
| `robot_perform` | 服务端编排、ROM 执行 | 预设动作与表情；支持说话前、同时、说话后，以及纯动作 |
| `recognize_face` | 服务端 | 查询自动识别的新鲜画面，可返回多人和注册用的 track_id |
| `recognize_voiceprint` | 服务端 | 查询本轮声音的识别结果，不沿用上一轮姓名 |
| `register_face` | 服务端 | 使用新鲜画面登记姓名；多人时必须指定 face_id |
| `register_voiceprint` | 服务端 | 使用本轮声音样本登记姓名；无本轮样本时拒绝注册 |
| `chat_search` | 服务端 | 按日期、人物、关键词查每日记录，支持完整分页 |
| `chat_read` | 服务端 | 按真实事件 ID 读取原文和前后文 |
| `person_read` | 服务端 | 列出人物或读取指定人物、分块和版本 |
| `person_update` | 服务端 | 按用户发言来源整理或纠正资料，检查版本；临时状态带失效时间 |
| `memory_forget` | 服务端 | 联动遗忘条目、来源及同轮记录，阻止重新整理恢复，重建对话连接 |
| `schedule_task` | 服务端 | 一次性、周期性提醒 CRUD；创建和修改支持相对秒／分钟或五段 cron；列表支持 limit/offset 和 total/has_more |
| `rom_volume` | ROM | get/set/adjust 实际扬声器音量，确认后同步设备设置 |
| `media_list` | 服务端 | 查询本设备上传的音乐与图片 |
| `play_music` | 服务端传输、ROM 播放 | 默认先简短回复再播放；speak=false 直接播放 |
| `show_image` | ROM | 默认先回复再展示；speak=false 直接展示，收到实际绘制回执才报告 displayed |
| `stop_media` | 服务端与 ROM | 停止音乐、取消图片；图片停止没有设备确认时返回 unconfirmed，不报告全部成功 |

工具回传遵循 `function_call_output`。慢工具在独立任务中执行，不阻塞音频和打断接收；call_id 去重，重连或新输入阻止旧轮续写。所有工具可用 `speak=false` 安静执行。未知工具返回错误，不执行任意代码。ROM 工具只在设备声明对应能力时注册。

不再注册 `get_current_time` 和旧 `memory` / `memory_add` / `memory_delete` / `memory_del`，也会移除上游保存的同名定义。北京时间、UTC 和星期在每轮用户回复、工具续答和提醒的 system instructions 中重新生成，避免长连接使用连接建立时的旧时间。

`speak` 默认 true；只有用户明确要求安静执行时才设 false，同批本项目工具统一保持安静。它只控制口播，不改变工具成功／失败结果。成功的只读查询可通过 `output_modalities=[text]` 继续必要工具步骤，并将安静标记传到后续动作；例如先安静查找歌曲，再安静播放，不能在查完列表时提前结束。终结动作完成后不再续答。自动识别始终复用原有感知服务：查询工具读取最新观察，最多等待 1.2 秒；人脸最多采用 3 秒内画面，声纹仅采用当前对话轮次。注册结果只返回姓名与档案 ID，不向模型返回特征向量或原始音视频。感知引擎未启用、样本不足和多张脸未选定都返回错误。

提醒修改中的相对延迟从修改时重新计时，并转为一次性提醒；cron、delay_minutes、delay_seconds 只能提供一个。重新指定时间可重新启用已完成提醒；正在播报时拒绝修改时间，防止旧播报结果覆盖新计划。

媒体在上传时预处理：图片等比缩放并居中填充至 284×240，保存 PNG 预览和 RGB565 原始像素；音频由 ffmpeg 转为 16kHz 单声道 PCM WAV。播放时音乐经 Opus 编码，通过有界 PB 流发送给设备解码播放；图片通过带二进制像素的 ROM 命令交显示任务绘制。默认等简短回答实际播完再启动媒体，安静模式直接执行。started/displayed 是开始状态，scheduled 只是排期；后台启动失败会记入媒体失败状态。

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
| `memory/chat_YYYYMMDD.txt` | 按北京时间分日的发言、操作结果和播放回执 |
| `memory/person_{name}.txt` | 基本信息、爱好、偏好、近期关注、任务、项目、健康与情绪；同名增加人物编号后缀 |
| `memory/_memory_state.json` | 人物与识别档案映射、版本、处理进度、请求防重和遗忘标记 |
| `memory/.pending.json` | 事务中断恢复文件；正常提交后删除 |
| `tmp/` | LLM `read` / `write` 沙箱目录 |

全局 SQLite：`data/opendesk.db`（用户、设备绑定、`scheduled_tasks`、用量等表）。

## 文件记忆

记忆运行时只读写设备文件。每日记录由服务端自动保存，独立于可能丢帧的网页转写订阅；最终转写保留原始轮次与条目标识，身份晚到时最多等待本轮结果，未知身份不归给镜头中的人。机器人生成文本标记为播放未确认，实际完成／中断另有回执记录；媒体安排成功与设备开始执行分别记录。

每行是可读文字及 `\t# ` 后的机器元数据，事件 ID 可作为人物条目的来源。正文换行和制表符转义，防止伪造记录。通过管理页和工具修订；直接改可读部分不会替代机器元数据中的正文。

人物 ID 不随姓名变化，人脸和声纹档案关联该 ID。更名时更新人物文件名和别名；同名人物独立保存，登记工具可显式指定 `person_id`。本轮说话人可加载有效资料；仅被看见的人默认只加载基本信息。文件缺失、身份未知、过期状态不冒充确定事实，超预算的大条目不会阻断后续小条目。Flecket 在本轮和工具续答前重新读取资料；MOSS Live 经 `session.update` 更新背景，是否影响已经开始生成的本轮响应取决于上游时序。

后台规则保守提取明确的第一人称信息，跳过问题、假设和引述；复杂整理由 Agent 使用带来源的 `person_update` 完成，不另起本地模型管线。简单的同一事实纠正替代旧条目，迟到的旧来源不覆盖较新的修订。同一人尚未回答完的多段语音共同关联到修订来源，避免一句纠正被语音检测拆成两轮后只引用后半句；回答落盘后按对应轮次清除待关联片段，不清除后来到达的发言。来源扩展不改变工具请求的防重指纹。每项提取有请求防重与进度，重启可从每日记录恢复。任务和项目的记录只表示状态，不替代提醒工具的真实调度。

近期关注默认 14 天、任务／项目 30 天、健康 7 天、情绪 1 天后退出当前有效资料；这表示需要复核，不表示事情已完成或疾病已痊愈。健康与情绪保存为用户自述。

写入按设备串行，跨进程文件锁保护单次文件事务；人物文件原子替换，恢复日志先落盘，读取前完成未提交事务。设备整体清空与文件操作在本项目单进程部署中互斥。保存结果与在线会话通知分离；通知失败不会把已经保存的资料报告成未保存。

遗忘清理人物条目、被替代版本、对应原始记录及可关联的同轮回复，保留不含正文的排除标记。人物资料注入和历史工具读取会登记来源，后续回答继承当前连接已读过的来源；遗忘递归清理这些回答和播放记录，即使后续轮次的身份未知也不会留下可检索的副本。来源关联按回答保守处理，可能连同该回答的其他内容一起清理，但不据此删除后续独立的用户发言。旧会话和后台任务使用的记忆代次立即失效；客户端停止旧播放、清除人物上下文并请求重连，不向新会话重放旧记录。新版本之前没有来源关联的记录无法自动推断全部引用关系，可显式选择记录或遗忘整个人物。遗忘人物资料不等于撤销人脸／声纹登记；日志备份和上游服务自己的持久历史需要遵循各自的删除策略。

启动执行一次性旧数据迁移：SQLite `device_memory` 和旧 `user_memory.json` 导入“未归属”的人物文件，文件提交成功后删除旧来源，不双写，也不猜人物归属。迁移中断可重复；`device_memory` 表仅保留旧结构兼容用途。升级前备份数据库和设备目录，回退旧服务时应使用同一批备份，不能仅回退代码。

管理页 `/my/memories` 提供人物分块、来源追溯、完整分页、日期和关键词搜索、纠正和遗忘。沿用 `/app/api/memories` CRUD，新增 `/app/api/memory/chat`、`/chat/{event_id}`、`/people`、`/people/{person_id}` 和 `/forget`，均检查登录及设备归属。Agent 工具只接受对象 ID，不接受任意文件路径。

本地验证：`pytest -q tests/test_file_memory.py`；浏览器验证运行 `python tools/run_file_memory_browser.py`，需要 Node、Playwright 和 Chrome，可通过 `PLAYWRIGHT_MODULE` / `CHROME_PATH` 指定安装路径。测试使用临时数据和本机端口，不连接真实设备。验证记录见 [file-memory-2026-11-18.json](file-memory-2026-11-18.json)。

服务器真实 Agent 验证使用 `PYTHONPATH=src .venv/bin/python tools/verify_file_memory_agent.py --audio /path/to/synthetic-wavs --report /tmp/file-memory-agent.json`。需提前准备 `enroll/remember/episode/load/recall/correct/forget/after_forget.wav` 八份单声道 16kHz PCM16 合成语音，并在具有既有 Agent、ASR 和声纹服务配置的服务目录执行。脚本创建隔离测试账号和设备，使用真实模型、实际记忆工具和正式 HTTP 登录接口；模拟 ROM 播放回执，不使用真实麦克风、摄像头或执行器。声纹依赖就绪后才开始，默认清理测试数据；`--keep-fixture` 仅供诊断，之后必须清理。该脚本通过独立校验进程调用部署代码，不覆盖设备 WebSocket 接入及物理播放验收。

2026-11-18 已部署并通过上述真实 Agent 联调：每日记录、声纹识别后的跨连接资料加载、历史检索、分段语音纠正、遗忘及重连后不再恢复。正式 HTTP 登录、读取和设备归属检查通过；5 条旧记忆迁移保留，7 组联调账号与设备已清理。部署备份、最终文件摘要、模型对话及修复记录见 [file-memory-deployment-2026-11-18.json](file-memory-deployment-2026-11-18.json)。

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

Realtime 摄像头分支同时启动限频人脸分析，保留即时网页预览；Flecket/MOSS 上传 PCM 同时交给有界声纹采样器。识别快照供实验台和人物注册使用；Flecket 每轮通过有时效的系统事件获得识别结果，也可用识别工具查询，注册工具负责登记新人物。识别姓名不写入永久人设，不替换上游 VAD、ASR、LLM 或 TTS。
