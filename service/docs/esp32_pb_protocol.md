# ESP32 与 BotServer 通信协议（PB v2，可协商紧凑媒体）

音频与播放控制使用 `ws://<host>:9000/asr_chat?device_id=<id>`；相机使用独立 `/camera_uplink` 连接。
支持 `local_face_pack_v1` 的新固件先通过普通 HTTP 获取启动配置并更新本地资源，然后建立这些连接，见本文末尾“HTTP 启动配置与本地资源”。
基础格式为一个 WebSocket binary message：`u32be(json_len) + JSON UTF-8 + 媒体字节`。双方协商后，连续纯音频与回执可使用下述短二进制格式；首包、动作、表情和结束/取消控制仍使用基础格式。服务端 `ready` 握手保留 TEXT JSON 兼容。
内置表情扩展：声明 `anim_id_v1` 的设备支持独立 `pb_single` 携带 `anim_id`，可附带 `anim_version`；握手中的同名版本字段用于资源一致性检查。声明 `phoneme_id_v1` 的设备还支持 `anim:[{"phoneme_id":"a","ms":120}]`，按音素 ID 加载嘴部关键帧，与表情共用资源版本。制作、烧录及完整调用约束见 [内置表情工作流](../../hardware/FACE_ASSETS.md)。
`next_bin_len` 声明同一 message 尾部媒体长度；不使用 base64。服务端保留旧上行 TEXT+BIN 的兼容解析，新固件不使用。
> **相关文档：** 部署与快速使用见 [README.md](../README.md)；主服务配置见 [SERVER.md](./SERVER.md)。定时任务、长期记忆、控制台管理等为**服务端能力**，不扩展本设备协议字段。

默认 **`asr_chat_device_pb_only: true`**：设备下行只处理 **`pb_*` + PCM**。服务端不再下发 `face_info`、`asr_*`、`llm_text`、TTS/错误类 JSON（如 `playback_done`、`tts_error`、`error`、`asr_rejected` 等）；轮次结果与耗时见 `/device_pipeline` 的 `pipeline_event` 或 Web 调试台。

---

## 0. 鉴权（设备端必读）

### 0.1 适用范围

| 通道 | 路径 | 鉴权 |
|------|------|------|
| 生产主链路 | `/asr_chat` | **`device_id` 必须**（仅此一项）；含语音与 `camera_frame` |
| 摄像头预览 | `/camera_view` | 控制台签发的 **`debug_token`** + 设备归属 |
| 流水线订阅 | `/device_pipeline` 订阅侧 | 控制台签发的 **`debug_token`** + 设备归属 |
| 流水线生产者 | `/device_pipeline` | **`device_id` 必须**（仅此一项） |
| HTTP API | `/api/*` | 有效用户的 Web 会话或 debug token；未认证返回 `401`，访问他人设备返回 `403` |
| 健康检查 | `/health` | 否 |

**设备链路无 API Key、无 PIN**；`debug_token` 由 Web 控制台「调试台」为已登录用户签发（`GET /api/debug/ws_token`）。

### 0.2 设备连接 URL

```
ws://<host>:9000/asr_chat?device_id=deskbot_abc123
```

### 0.3 失败行为（设备 WS）

| 情况 | WebSocket |
|------|-----------|
| 缺少 `device_id` | 关闭码 **1008**，reason `device_id_required` |
| 重复连接 | 同一 `device_id` 只保留最新一条（`keep_only_one_link`） |

### 0.4 Web / 调试鉴权（仅控制台与调试）

Web 控制台（`:9000`）注册登录后，会话 cookie 即 Web / HTTP 鉴权；`/camera_view` 与 `/device_pipeline` 订阅侧使用 `debug_token`（`GET /api/debug/ws_token` 签发，URL 参数名 `debug_token` / `debugtoken` / `ws_token`）。设备主链路不涉及任何 Key。

### 0.5 固件最小接入示例

```c
// 伪代码
const char *url =
  "ws://192.168.1.10:9000/asr_chat"
  "?device_id=deskbot_1cdbd476ab5c";
websocket_connect(url);
```

---

## 1. 传输规则

- 基础格式的每条 binary message 自包含 4 字节大端 JSON 长度、JSON、可选媒体；JSON 最大 64KiB。协商格式以 `44 42 01`（`DB` + 版本 1）开头，与合法基础长度前缀不冲突。
- `audio.next_bin_len`（下行）或根级 `next_bin_len`（上行）必须与尾部媒体字节数一致。
- 一次只写完整 message，服务端串行发送；不跨 message 拼 JSON 和音频。
- 音频、确认共用主连接；固件控制发送队列优先于音频队列。相机独立连接。

### 1.1 能力协商

固件每次连接成功都先发送以下打包消息（包括重连，不能只在首次上电发送）：

```json
{"type":"device_hello","capabilities":["pb_stream_v1","pb_epoch_v1","pb_playback_progress_v1"]}
```

服务端仅对声明 `pb_stream_v1` 的设备启用连续播放，重连时清除旧能力记录。
未声明的设备继续使用 §8.2 旧式十块窗口与完整序列完成确认。

### 1.2 紧凑协议协商与兼容

`device_hello.capabilities` 与 `ready.capabilities` 声明以下独立能力，启用交集；每次主连接换代清空全部协商状态、音频元数据缓存和二进制回执的 epoch→req 映射。相机连接不参与协商，也不接管主连接。

| 能力 | 效果 |
|---|---|
| `pb_compact_v1` | 连续链的相同 `pb_ver/level/stream/sr/fmt/ch` 只发首包；去掉设备下行常规 `t_mono`；回执仅保留最小执行器 `space`，播放进度限频至约 200ms，当所有执行器同时结束时省略重复 `pb_audio_end` |
| `audio_bin_v1` | 上行 Opus 与下行纯音频续包使用 24 字节头；不依赖 `pb_compact_v1` |
| `ack_bin_v1` | 有已登记 epoch 的播放/流控回执使用 32 字节格式；不依赖前两项 |

新 ROM 对没有声明这些能力的旧服务端发送完整原 PB；新服务端对旧 ROM 继续发送完整原 PB。未协商的二进制类型不接受。基础 PB 首包 `pb_start` 先登记 req/epoch、优先级、媒体格式与可选音色（`voice_key`）；没有首包、轮次不匹配或序号不连续的二进制续包会被拒绝。流内元数据应保持稳定；改变编码时开启新链。

带 `anim/servo/volume/voice_key` 等可选信息的音频块继续走基础 PB，不拆成另一条异步消息，保持原有顺序和时间轴。`pb_end/pb_cancel/pb_single` 仍是基础 PB。控制路径支持精简后的字段继承；非流式旧窗口序列保留完整字段。

### 1.3 二进制音频头（24 字节）

所有多字节整数为无符号大端；**一个完整 WS message = 一个头 + 一个媒体体**，没有额外 4 字节 JSON 长度前缀。

| 偏移 | 长度 | 字段 |
|---|---:|---|
| 0 | 2 | ASCII `DB` |
| 2 | 1 | 版本 `1` |
| 3 | 1 | 类型：`1` 下行音频续包；`2` 上行 Opus |
| 4 | 4 | 下行 `epoch`（非零）；上行固定 `0` |
| 8 | 4 | 下行 `idx`（与 PB 共用序号）；上行连接内递增包号，从 0 开始，可因丢包跳号 |
| 12 | 4 | `pts_ms`：下行为本链内容时间轴；上行为已送入传输的音频累计时长 |
| 16 | 2 | 本块时长 ms，非零 |
| 18 | 2 | Opus 帧数（每帧 20ms）；下行 PCM 为 0 |
| 20 | 4 | 下行媒体字节数；上行 Opus 部分字节数 |

下行编码、采样率、声道由首包继承，媒体长度必须精确等于 message 尾部长度；媒体最大 `256KiB-24`。下行保留完整 idx/pts/duration，正常接收和播放回执语义不变。上行固定 Opus、16kHz、单声道，每包 1–50 帧，目前固件仍发送 2 帧/40ms；上行消息尾部允许追加已协商的 8kHz 单声道 PCM16 预览，长度必须为本块时长×16 字节。常规 MOSS 模式没有此预览。多帧 Opus 继续使用原有 `u16be(len)+packet` 串联；单帧沿用原解码器约定。

上行 `flush`（含最后音频或空体）仍使用基础 JSON，保留旧 VAD 收尾语义。长连接断开不重放旧 TX 队列。短头不改变 Opus 帧长、音频预缓冲或音画同步算法。

### 1.4 二进制回执（32 字节）

| 偏移 | 长度 | 字段 |
|---|---:|---|
| 0–3 | 4 | `DB`、版本 1、类型 `3` |
| 4 | 4 | `epoch` |
| 8 | 4 | 累积 `idx`；不得超过该 epoch 服务端已发送的最大 idx |
| 12 | 4 | `played_ms`；仅 flags.bit0=1 时有效 |
| 16 | 4 | `buffered_ms`（无此信息时 0） |
| 20 | 4 | `device_ms`，设备毫秒计数 |
| 24 | 2 | 最小执行器可用 `space`（无此信息时 0） |
| 26 | 1 | 回执代码，见下表 |
| 27 | 1 | flags：bit0 表示有效播放进度；其它位必须为 0 |
| 28 | 4 | `queue_wait_ms`；仅 flags.bit0=1 时有效 |

代码按顺序：0=`pb_chunk`、1=`pb_end`、2=`pb_credit`、3=`pb_audio_end`、4=`pb_started`、5=`pb_progress`、6=`pb_cancelled`、7=`pb_error`。

服务端按连接内已登记 epoch 恢复字符串 req（最多保留 64 个轮次），交给相同的流控和 MOSS 回执处理逻辑。未知 epoch、超前 idx、未知版本/类型/flags 或长度不为 32 均丢弃；旧连接回执不能映射到新连接请求。epoch=0 的旧式任务回执继续使用基础 JSON。

`pb_credit` 只表示接收/排队及可用容量，仍逐块累计确认，避免可变时长块在窗口中等待形成死锁。`pb_progress` 只来自实际 DMA 完成的音频时钟；约 200ms 一次，结束与取消前强制报告最终进度。`pb_started`、`pb_end`、`pb_cancelled`、`pb_error` 不限频。`pb_end` 仍等待音频、表情和舵机全部完成；若音频先完成而动作/表情仍在执行，立即保留 `pb_audio_end`，使回声处理及时获知扬声器已停止。仅在全部执行器都结束时，省略同一时刻重复的 `pb_audio_end`。服务端不能根据 credit、墙钟或已下发字节提前报告已播放。

二进制回执省去常规 lipsync 字符串等诊断信息；这些仍可通过独立设备诊断查询。扩展错误详情/自定义回执继续使用 JSON，不擅自加入未知二进制字段。

### 1.5 真机验收（2026-11-11）

同一段 6.4 秒、80ms 分块的 Opus 音频，附带一帧本地表情和两次小幅舵机动作；麦克风和独立相机链路并发运行。以下统计为应用消息字节，包含音频，未包含 WS/TCP/IP 头。

| 协商模式 | 音频下行总字节 | 其中元数据/头字节 | 上行回执字节 | 回执数量 |
|---|---:|---:|---:|---:|
| 旧 PB | 33,487 | 18,042 | 30,451 | 165 |
| 精简 PB | 26,226 | 10,781 | 15,996 | 110 |
| 二进制音频 + 回执 | 18,004 | 2,559 | 3,520 | 110 |

音频下行总量减少 **46.2%**，回执字节减少 **88.4%**，三种模式的实际 DMA 进度均到达 6400ms，内容断流计数增量均为 0。该比较只执行一次/模式；首次 DMA 回执约 106–131ms，没有证明首音延迟显著改善，也不是声学端到端延迟测量。

最终固件复验：动作尾段比音频晚约 1103ms 完成，分别收到音频完成与整体完成回执；取消确认约 103–105ms，新 epoch 的替换回复完整播放；启用 Tingting 校准模板时本地分析 319 帧、丢帧 0，平均 1.374ms/帧（含抢占墙钟），内容断流 0。5 秒 CPU tick 采样中音素分析约占单核 3%，WS 约 10%，AFE feed 约 77%，麦克风约 45%；双核总容量按 200% 计，CPU 余量有限，该数据不代表长期负载稳定性。相机在本地并发运行，按原配置未向 MOSS 上传画面。

真实 MOSS 闭环使用生成 WAV 作为桥接输入，回复在物理 ROM 上播放约 7.6 秒，收到 86 条 `playback.chunk.completed` 和 1 条 `playback.completed`。常规服务与原 MOSS 配置已恢复。相关自动回归 **159 项通过**，跨 Python/C++ 头部向量、截断/错版本/旧 epoch、独立能力组合、旧协议窗口、连续流、打断及 MOSS 路由均有覆盖。

固件 SHA256：`141e6023c5abb068ddcb5fd6cee109fb8776befba55b697f2e31b38b8a04e03a`。逐项数据及测量限制见 [验收记录](media-wire-validation-2026-11-11.json)。

### 1.6 夜间完善与最终固件

后续修正了调试动作/表情预览及本地回复后续表情的 `append` 参数丢失；动作附带本地表情现在共用 req 和连续 idx，以单个 `pb_end` 完成。设备错误立即终止窗口，具备索引能力的设备拒收落后窗口的 ACK。动作角度中的 NaN/Inf 在下发前拒绝。

ROM 主循环只保留串口、配网维护和诊断，以 1ms 休眠替代空转，实时音频、网络和执行器仍在独立任务运行。CPU 诊断重新启动时清空旧采样，并显示实际观察时长。最终并发采样：主循环约单核 1%、Idle1 约单核 27%；本地音素分析 319 帧、丢帧 0，6.4 秒音频实际播放完整，取消确认约 107ms。真实 MOSS 再次返回完整播放回执。以上为短时采样，AFE 声学处理仍为主要 CPU 消耗，未据此调整降噪或增益。

最终固件 SHA256：`aed99c9fd5f81853b682c360982f5e59197692b7e4523b24a792da23cd86ae01`。本轮相关自动检查 **252 项通过**。原始对比记录保留不改，后续逐项修正、性能数据、复验和保留项见 [夜间交接记录](overnight-polish-2026-11-11.json)。

## 2. 上行（设备 → 服务端）

### 2.1 音频 `audio`

`codec` 与 `config.yaml` → `audio.input_codec` 一致（常见 **opus** @ 16 kHz）。

```json
{ "type": "audio", "codec": "opus", "next_bin_len": 80 }
```

同一打包 message 的媒体尾部为 80 字节 Opus。PCM 示例：`codec":"pcm16"`, `sr`:16000, `ch`:1。

**段结束**（无 binary）：

```json
{ "type": "flush" }
```

触发 ASR → LLM → TTS → pb 下行。

### 2.2 画面 `camera_frame`

```json
{ "type": "camera_frame", "codec": "jpeg", "next_bin_len": 12345, "seq": 42 }
```

同一打包 message 的媒体尾部为 JPEG。下行 pb 可用 **`cam_fps`**（如 `3` = 每秒 3 帧）调节上行帧率；发送侧建议丢旧保新。

### 2.3 其它

| `type` | 说明 |
|--------|------|
| `ping` | 服务端回 `pong` |
| `pb_ack` | 播放回压，见 §8 |
| `user_text` | 调试：跳过 ASR |

**示例顺序**

```
JSON audio + next_bin_len → BIN opus
JSON camera_frame → BIN jpeg
JSON flush
```

---

## 3. 下行 pb 概述

| 项目 | 约定 |
|------|------|
| 版本 | `pb_ver: 2`（wire v2.1） |
| 音频 | mono **opus**（默认，`config.yaml` → `audio.output_codec`）或 **s16le**，**sr = 16000**（首包声明，统一下发采样率） |
| 画布 | **284 × 240**，原点左上 |
| 单包 | 硬件上限 `chunk_ms ≤ 10000`；服务端合并默认 ≤ **500**（`PB_CHUNK_MS_MAX`） |
| 口播默认 | `level = 1`，`action = "replace"` |

一条 pb JSON 可含 **`anim[]`**（表情）、**`servo[]`**（舵机）、**`audio`**（PCM 长度）。口播由服务端按音素组帧后合并为多片 `pb_start` → `pb_chunk*` → `pb_end`，或单片 **`pb_single`**。

**全局规则（固件必实现）**

| 编号 | 规则 |
|------|------|
| R0 | `pb_start`/`pb_chunk`/`pb_end`/`pb_single` 中 `audio`/`servo`/`anim` 至少一项 |
| R1 | 队列决策仅 **`pb_start`**、**`pb_single`**；同 `req` 的 `pb_chunk`/`pb_end` 只续传 |
| R2 | `audio.next_bin_len > 0` → 下一条为等长 audio binary（opus batch 或 s16le PCM） |
| R3 | 协议错位 → 丢弃该 `req` 剩余片，清队列 |
| R4 | 同 `req` 内 `idx` 从 0 严格递增 |
| R5 | 有 `anim[]` 时 `sum(anim[i].ms) == chunk_ms` |
| R6 | PCM 字节数 `== (chunk_ms * sr // 1000) * 2` |

24 kHz：`chunk_ms=113` → 5424 字节；`chunk_ms=1921` → 92112 字节。固件 WS RX 缓冲须能容纳单帧 binary（默认可至约 480000 字节 / 10s）。

---

## 4. 下行消息类型

| `type` | 用途 |
|--------|------|
| `pb_start` | 链首包（`idx=0`），触发队列；含音频时带 `sr`/`fmt`/`ch` |
| `pb_chunk` | 链中间包 |
| `pb_end` | 链末包 |
| `pb_single` | 整轮仅一条（idle、单段舵机、或口播仅一包） |
| `pb_cancel` | 中止 `req` |

多片：`pb_start`(0) → `pb_chunk`(1…N-2) → `pb_end`(N-1)。单片只发 **`pb_single`**，禁止无 `pb_start` 单发 `pb_end`。

### 4.1 公共字段

| 字段 | 说明 |
|------|------|
| `req` | 序列 ID（16 位 hex 常见） |
| `idx` | 分片序号，从 0 递增 |
| `chunk_ms` | 本片时长（ms） |
| `level` | 优先级 0–3：0 idle，1 口播，2 紧急，3 调试 |
| `action` | `replace`（默认）\| `append` \| `default` |
| `volume` | 0–100，可选；同 `req` 后续 PCM 按此音量 |
| `cam_fps` | >0 时设置 JPEG 上行目标帧率 |

忽略以 `_` 开头的键。

### 4.2 时序示例

```
→ JSON pb_start  audio.next_bin_len=N  chunk_ms=T  anim[…]
→ BINARY N 字节 PCM
→ JSON pb_chunk …
→ BINARY …
→ JSON pb_end …
→ BINARY …
```

---

## 5. 动画 `anim[]`

`anim` **必须是数组**。每项：

| 字段 | 说明 |
|------|------|
| `elements` | 图层容器（见下表） |
| `ms` | 该段子动画时长（≥1） |
| `phoneme` | 可选，音素符号（调试） |

### 5.1 `elements` 图层

| 键 | 说明 |
|----|------|
| `bg` | 背景（最先绘制） |
| `nose` | 鼻 |
| `mouth` | 口型 |
| `eye_l` / `eye_r` | 左/右眼 |
| `extra` | 装饰（腮红、文字等） |

### 5.2 片内时间轴

```
t = 0
for k in 0 .. anim.length-1:
  在 [t, t + anim[k].ms) 绘制 anim[k].elements
  t += anim[k].ms
```

有 PCM 时子动画切换与采样边界对齐。

### 5.3 绘制顺序

**`bg` → `nose` → `mouth` → `eye_l` → `eye_r` → `extra`**

### 5.4 图元与颜色

每个图元须有 **`shape`**（比较前转小写、做别名归一化）。wire 上颜色为 **`c`（RGB565 整数）**；配置 JSON 可写 `#RGB` / 命名色，服务端转换；缺省 **65535（白）**。

```
R5 = (R8 >> 3) & 0x1F;  G6 = (G8 >> 2) & 0x3F;  B5 = (B8 >> 3) & 0x1F
c  = (R5 << 11) | (G6 << 5) | B5
```

未知 `shape`：**跳过**该图元，不判整包失败。

### 5.5 `shape` 对照表

| 主 `shape` | 别名（等价） | 必填字段 |
|------------|--------------|----------|
| `rect` | `fill_rect`, `fillRect` | `x`,`y`,`w`,`h` |
| `rect_outline` | `draw_rect`, `drawRect` | 同上 |
| `circle` | `fill_circle`, `fillCircle` | `x`,`y`,`r` |
| `circle_outline` | `draw_circle`, `drawCircle` | 同上 |
| `line` | `drawLine` | `x1`,`y1`,`x2`,`y2` |
| `pixel` | `point`, `drawPixel` | `x`,`y` |
| `hline` | `h_line`, `drawFastHLine` | `x`,`y`,`w` |
| `vline` | `v_line`, `drawFastVLine` | `x`,`y`,`h` |
| `ellipse` / `ellipse_fill` | `drawEllipse` / `fillEllipse` | `x`,`y` + (`rw`,`rh` 或 `w`,`h` 作半轴) |
| `triangle` / `triangle_fill` | `drawTriangle` / `fillTriangle` | `x0,y0,x1,y1,x2,y2` 或第一点 `x,y` |
| `round_rect` / `round_rect_outline` | `fillRoundRect` / `drawRoundRect` | `x`,`y`,`w`,`h`, `radius` 或 `r` |
| `rotated_rect_outline` / `rotated_rect_fill` | — | `x`,`y`,`w`,`h`,`angle`（**中心**坐标） |
| `text` | — | `x`,`y`,`text`,`size`,`c` |

三角形勿与 `rect` 的 `w`,`h` 混淆。

---

## 6. 音频与舵机

### 6.1 PCM

```json
"audio": { "next_bin_len": 92112 }
```

| `sr`/`fmt`/`ch` 以本 `req` **首条含音频**的包为准（当前 16000 / opus 或 s16le / 1）。

### 6.2 舵机 `servo[]`

```json
"servo": [
  { "xm": 1, "ym": 1, "x": 0, "y": 30, "ms": 380 }
]
```

| `xm`/`ym` | 0 绝对；1 相对增量；2 本轴保持 |
|-----------|------------------------------|

与 `anim[]` **并行**调度；无舵机时省略 `servo` 键。

---

## 7. 优先级队列

收到 **`pb_start` / `pb_single`** 时按 `level`（0–3）与 `action` 决策：

| 条件 | 行为 |
|------|------|
| `level` > `queue_level` | 清空队列，立即执行 |
| `level` == `queue_level` 且 `replace` | 清空后执行 |
| `append` | 追加队尾 |
| `default` | 队列中更高优先级序列 >1 条则丢弃，否则同 append |

---

## 8. 回压、连续播放与取消

### 8.1 协商后的连续播放

一轮回答只有一个 `req` 与正整数 `epoch`。所有短句追加到同一条链：
`pb_start(idx=0)` → `pb_chunk(idx=1...)` → 唯一 `pb_end`（可不带音频）。
短句边界不发送 END/SINGLE，不关闭 I2S，也不等待表情、舵机播完。
每块包含 `stream:true`、`epoch`、`pts_ms`（相对本轮音频的毫秒位置）；`idx` 全轮递增。
保留原有 `audio`、`servo[]`、`anim[]`、`volume`。音频格式仍支持 16kHz mono s16le/Opus。

固件每块分发后发送累计确认；服务端首块立即发出，随后在最多 4 块、约 1000ms 的有界窗口内预取，不逐块停等。服务端核对 `req + epoch + idx` 才继续：

```json
{"type":"pb_ack","ack_type":"pb_credit","req":"r","epoch":7,"idx":3,
 "buffered_ms":700,"audio_space":47,"head_space":49,"display_space":48}
```

- `pb_credit`：准许下一块；音频预取受约 1000ms 预算和队列容量约束，表情队列不扣除音频额度。
- `pb_started`：首块 PCM 对应的 DMA 周期已实际完成输出，带 `device_ms`、`queue_wait_ms`、`played_ms`；不等同于声学录音测量。
- `pb_progress`：采样已完成输出的 DMA 内容进度；`played_ms` 在欠载时保持，不用墙钟追赶。它不包括功放的声学延迟。
- `pb_audio_end`：整轮音频执行器完成；不代表舵机/表情完成。
- `pb_end`：整轮音频、舵机、表情全部完成，服务端才能报告整轮播放成功。
- `pb_error`：队列或音频执行失败，当前流失败并取消，不能报告成功。

服务端生产队列有界；取消、断线和设备失败均解除生产者等待。
表情按已完成 DMA 的内容位置等待/追赶，舵机按同一位置启动并保留动作轨迹。语音口型由本地实际输出的 PCM 驱动，独立于表情关键帧。

### 8.2 未协商设备的兼容行为

每十块等待 `pb_chunk`（执行器空间）；最后等待 `pb_end`（三路全部完成）。
服务端窗口超时至少为声明窗口时长加宽限，不固定假定任何窗口三秒内可完成。

### 8.3 取消与轮次隔离

```json
{"type":"pb_cancel","req":"r","epoch":7}
```

未播放音频时，新语音达到 VAD 人声起点确认阈值即取消旧生成，无须等到整句识别。
播放期间及结束后 450ms，Silero 起点仅标记疑似插话，先等待 ASR 核对：与当前口播文本相同或高度重合的片段不触发取消、不进入对话历史；明确的停止/纠正命令和其它有效输入在识别后取消旧生成与播放。四字以内的含糊重叠片段会保守过滤，正常问候、报时及明确命令放行。这样会让播放期间的插话取消等待切句及识别，不能再宣称所有插话均在起点取消。
本地 SenseVoice 另有滚动插话识别：播放期间每隔至少 300ms 对最近最多 900ms 的人声做一次有界识别，每连接最多一个在途请求，HTTP 超时 600ms。识别到“停一下 / 别说 / 等一下”等明确指令，且并非机器人正在说的原文时，立即取消，不等待完整切句；完整 ASR 保留原句继续生成新回答。滚动检查不依赖新的 VAD 起点，避免回声让 VAD 长时间保持讲话时漏掉用户插话。预览结果必须仍属于同一连接、输入版本及播放请求，迟到结果不能取消新回答；断连会取消并回收预览任务。不支持预览的 ASR 后端沿用完整识别路径。
回声窗口由物理 `pb_started` 开启，`pb_audio_end`/`pb_cancelled`/`pb_error` 收尾；迟到的旧轮确认不能关闭新轮保护。正常 AEC 输出继续全双工上行；AFE 输出不健康时才退回原始麦克风，并在扬声器播放及声学尾音期间将无 AEC 的原始上行置零。AFE 任务持续收音以自动恢复。
每轮连续流使用递增 epoch；设备丢弃旧 epoch 的数据和取消包，已取消 epoch 的后续媒体也不能复活。
重连时清空设备会话和服务端能力缓存。取消走执行器队尾标记，保留标记之后的新任务。
`pb_cancelled` 在三路取消标记均处理后上报；收到取消请求与执行器停止明确区分。
服务端用输入修订号拒绝插话之前尚未完成的 ASR 结果。

### 8.4 原始插话收音旁路（`raw_preview_pcm_v1`）

服务端在连接的 `ready.capabilities` 中声明 `raw_preview_pcm_v1`。固件收到后才在播放期间和短暂尾音窗口给上行音频附加原始麦克风旁路；未协商时二进制仍只有原来的 Opus batch。现有 `ready` 文本控制消息保留兼容，媒体/PB 仍使用打包 BIN。

```json
{"type":"audio","codec":"opus","sr":16000,"ch":1,"frames":2,"next_bin_len":680,"opus_bytes":40,"raw_bytes":640,"raw_sr":8000}
```

同帧媒体 = 原始 Opus batch（`opus_bytes`）+ 8kHz 单声道 s16le 原始收音（`raw_bytes`，最多640字节）。服务端严格校验长度、偶数字节和采样率，先分离旁路，再将原封不动的 Opus 交给普通切句。原始旁路只用于滚动停止指令识别，不直接触发普通聊天，避免重新引入自问自答。

真实双轨录音证明：用户说的“停一下”在原始音轨可识别，在 AEC 后音轨会变成其它词或丢失。确认停止后取消旧生成/播放、递增输入版本、以最近原始片段重新起一段 VAD，并短暂使用后续原始旁路收音，保留“现在几点”等后半句；新问题被 ASR 接受后恢复普通 AEC 路径。只有停止指令时保持静默等待下一句，不提前丢弃续听状态。已确认插话、明确机器人指令和当前30秒对话窗口优先放行，不串行等待声纹和人脸；未明确寻址的空闲输入仍做环境闲聊过滤。播放期间模糊的短问候按回声保守处理，明确停止/唤醒与完整报时问题保留。

设备回声参考补偿由真机校准改为96ms，默认使用 FD 高性能模式、较强 NLP 和9dB AGC。校准音偏差从约74ms降至4ms，但该改善不能替代真实人声插话验证。耗时相关性搜索只在串口 `log 3` 诊断时运行。显式串口 `mic_trace <毫秒>` 可采集最多60秒的原始/上传双轨，默认关闭；USB输出不阻塞音频，主机按魔数、序号与校验和恢复记录。

## 9. 表情配置（服务端生成 `anim[]`）

口播时服务端从 **`face_bundle`** JSON（`tts.pb_face_bundle_json` 或 `DESKBOT_PB_FACE_BUNDLE_JSON`）按音素查表组帧；保存文件后 **mtime 热重载**。示例：`data/global/deskbot-face.json`（`phonemes` + `emotions`）。

| 顶层键 | 说明 |
|--------|------|
| `mouth_by_phoneme` | 音素 → 口型 `{ elements[], offset? }` |
| `mouth_by_phoneme_groups` | 共享条：`states[]` + `elements` + `offset` |
| `eye_l` / `eye_r` | `default` / `open` / `close` 图元数组 |
| `nose` | `default` 图元数组 |
| `extra` | 任意态名 → 图元数组；`metadata.extra_state` 选态 |
| `metadata.blink` | `open_ms` / `close_ms` 控制眨眼相位 |

**offset**：口型 `offset (dx,dy)` 仅平移 **鼻、眼、extra** 坐标；**嘴不动**。未知音素用 `"_"` 或内置默认。

固件只需解析 wire 上的 `anim[].elements`；编辑表情见仓库 `data/`。

---

## 10. 与旧版差异

| 项目 | 旧版 | 当前 |
|------|------|------|
| `anim` | 单对象 `{elements}` | **数组** `[{elements, ms, phoneme?}, …]` |
| `servo` | 单对象 | **数组** |
| 根级 `phoneme` | 有 | **无**（在 `anim[i].phoneme`） |
| 音频长度 | `audio.next_bin: 1` | **`audio.next_bin_len`** 字节数 |
| 颜色 | `color` 字符串 | wire **`c` RGB565** |

---

## 11. 固件实现清单

1. 仅连 `/asr_chat?device_id=`（无 PIN / API Key）；JSON/binary 状态机。
2. 上行成对发送；下行处理 `pb_*`，按 `audio.next_bin_len` 收 PCM。
3. `anim[]` 按 `ms` 切换；绘制顺序 §5.3；R6 校验 PCM 长度。
4. `pb_start`/`pb_single` 入队（§7）；周期性 `pb_ack`。
5. 未知键、`_` 前缀键忽略；未知 `shape` 跳过。

## 12. 真机回归

`tools/verify_device_playback.py` 是本机显式运行的诊断入口：先正常停止占用 9000 端口的本项目后台，再以服务 venv 启动该脚本，传 `--device-id` 与 `--output`。脚本启动普通后台，并仅对指定设备执行一次验证，不增加测试 HTTP 接口。验证后后台继续运行；常规部署入口仍是 `python -m deskbot_server`。

验证包含同固件下旧式三段播放与连续播放的配对对照、三路执行完成、取消确认、旧轮迟到数据/取消包隔离、多次取消、真实模型与 TTS 输出。固定音频故意附带较长表情以检查跨路等待，结果不能当作任意真实对话的加速比例。`pb_started`/`pb_progress` 是 I2S 提交及网络确认，麦克风到实际声学首音另需现场对话/录音验收。

固件 USB 调试输出使用零等待发送：调试读取端慢或未读取时允许丢日志，不能让日志背压阻塞音频、表情或确认。


## 13. 本地动画与 PCM 口型（2026-11-11）

普通口播不再生成或下发 TTS 音素口型。服务端下发本地表情 ID 与关键帧索引，嘴部由设备的播放 PCM 驱动。独立动画继续使用根级 `anim_id`，播放整段本地动画；携带音频时使用 `anim[]` 内的帧引用：

```json
{"type":"pb_start","req":"talk-1","idx":0,"epoch":42,"stream":true,"pts_ms":0,"chunk_ms":200,"anim_version":"与固件配套的资源版本","anim":[{"anim_id":"happy","frame":0,"ms":200}],"sr":16000,"ch":1,"fmt":"s16le","audio":{"next_bin_len":6400}}
```

`frame` 是从 0 开始的本地关键帧索引，`ms` 是到达该姿态的插值时间（1–30000 ms），不是重播整段动画。嵌套 `anim_id` 与 `elements`、`phoneme_id` 互斥；未知 ID、越界帧与错误版本均拒绝。传输仍是四字节大端 JSON 长度 + JSON + 音频的单条 WebSocket binary message。

设备新增能力 `anim_frame_id_v1`、`pcm_lipsync_v1`。服务端检查帧引用能力与资源版本，不自动退回大图元包。`pb/data/face_assets_manifest.json` 随服务发布，构建钩子与 ROM 素材同时生成它；修改表情后重新生成、编译、烧录，并部署配套服务清单。

固件先按实际音量缩放并下混 PCM，再按 160 个源采样帧计算 RMS，预先保存普通嘴部开合目标。三个固定 DMA 缓冲及四个待输出块拥有 PCM 副本；DMA 回调只移交预计算结果与拷贝下一块，不做浮点计算、分配或日志。只有已完成输出的块才发布口型；欠载清零输出和口型目标，取消让旧 generation 立即失效并停止 DMA、清理待播放数据。包尾不足 160 帧会跨包保留，只有整流结束才补齐不足一个 DMA 周期的尾部。

首版是无模板的 RMS 降级模式：静音为 SILENCE，非静音为 UNKNOWN 并继续开合；未实现 A/I/U/E/O 的 MFCC 校准匹配，不把普通开合宣称为元音识别。RMS 不做频谱抽样，因此无需为 48 kHz 单独降采样。16 kHz 时一个 DMA 周期为 10 ms，48 kHz 时约 3.33 ms；嘴形结果最多以该周期粒度滞后 I2S 输出，屏幕刷新与功放还会增加可见/声学延迟，尚待真机测量。

UI 约每 10 ms 平滑嘴部参数，空闲时约 30 FPS 只传输嘴部区域，表情过渡时合成整脸；眼睛、鼻子、装饰和表情插值不被音频覆盖。开口/收口时间常数为 35/70 ms，停止与取消立即将开合归零。门限在 `deskbot_config.h` 的 `DESKBOT_LIPSYNC_SILENCE_DB`（默认 -45 dBFS）与 `DESKBOT_LIPSYNC_FULL_DB`（默认 -15 dBFS）调整。

本次代码、主机测试、编译与待执行板上验收见 [实现记录](pcm-lipsync-implementation.md)。此前第 12 节及历史 JSON 内的实测仅属于旧固件，不能作为新 DMA 播放实现的真机证据。


### USB 离线复验

当前固件支持 `./hardware/usb_test.sh`，通过 `DBPB0001` + u32be 包长直接发送相同 PB packed bytes。USB 模式跳过 Wi-Fi 和摄像头启动，保留 PB 队列、插值、PCM 口型和 AEC。参考延迟已经物理扫参修正为 32ms；诊断录音经 FIR 降为 8kHz，实际处理和播放不变。实测值及未验证边界见 [USB 结果](usb-audio-bench-2026-11-11.json)，操作见 [USB 快速实测](../../hardware/USB_AUDIO_TEST.md)。

## HTTP 启动配置与本地资源（2026-11-11）

普通 HTTP 是控制／资源平面，WebSocket 是实时媒体平面。配网页保存 HTTP(S) 或兼容的 WS(S) 服务器基址，可附路径前缀。ROM 每次启动请求 `GET <base>/api/device/bootstrap?device_id=<id>`，在请求完成前不发起媒体连接。返回 `code/message/data`；公开内容仅包括非敏感连接信息与共享素材，不包含凭证、用户信息或服务密钥。

```json
{"code":0,"message":"ok","data":{"schema":1,"device_id":"rom-id","realtime":{"protocol":"deskbot.pb.v2","url":"/asr_chat"},"camera":{"enabled":true,"url":"/camera_uplink"},"face_pack":{"format":"deskbot-face-v1","version":"<16 hex>","url":"/api/device/face-pack/<version>","size":62865,"sha256":"<64 hex>"}}}
```

这里的 url 均作为已配置服务器／路径前缀下的端点拼接；不根据 Host 或转发请求头生成上游地址。资源 GET 返回确定性的 JSON 图元程序，带 ETag 与 immutable 缓存头；If-None-Match 匹配返回 304。旧版本只在进程有限缓存中保留，失效返回 404，客户端需重新获取 bootstrap，不可把最新版字节冒充旧版 URL。

ROM 校验下载字节数、SHA-256、格式和资源引用，写入 FFat 双槽后再激活。下次启动加载同源缓存，仅版本变化时重新下载；未联网成功、下载中断、非法素材或写入失败均保留旧包／内置后备。HTTP bootstrap 不可达时兼容旧服务器路径；收到明确的不兼容配置时不盲目连接。详细资源结构、口型映射和待机程序见 `hardware/FACE_ASSETS.md`。

实时 PB 保留 `req/epoch/idx/pts_ms`、累计回压、取消隔离与执行完成语义。音频、舵机和表情都可独立存在；显式 `anims=[]` 的口播不携带 anim、口型或图元。指定表情时只引用已下载的本地 ID／帧号。新固件握手增加 `local_face_pack_v1` 与 `local_idle_v1`，携带实际激活包的 anim_version。

声明 `local_idle_v1` 后，服务端不再定时补发旧版待机序列，空闲由 ROM 包内的 `idle` 程序处理。新指令抢占本地待机时保留协议输入队列，先等待扬声器／显示／舵机处理取消边界，再派发新指令，避免旧 END 标志提前确认新动作。

2026-11-12 起统一使用音量口型。ROM 不再声明 `mfcc_vowel_v1`，不做元音识别或音色模板匹配；普通批量与流式语音组帧不再解析 TTS 音色，也不再生成 `voice_key`。协议模型仍可读写旧消息中的可选 `voice_key`，新 ROM 忽略该元数据。服务端只发送音频和可选表情 ID／帧引用，嘴巴张合由实际播放 PCM 的 RMS 音量决定。

完整表情 `anim_id` 仍是独立的 `pb_single`；要与音频或动作同时发送表情，应使用 `anim:[{anim_id,frame,ms}]` 帧引用。可选表情比音频短时，组帧中的空姿态只表示保持现有画面的时长，不包含口型图元；整包没有表情时省略 `anim`。无效播放参数会使该序列返回 dropped，不会被当作 completed，也不会终止后续播放队列。

资源打包器统一输出 `lipsync={classifier:"rms-v1",closed_phoneme:"sil",open_phoneme:"a"}`（开闭嘴资源可自定义）。旧文档中的 MFCC 配置在导出时转换成 RMS 并丢弃音色模板、元音映射和分类参数；这些旧字段不再影响资源内容版本。口型完全由实际播放 PCM 音量与开闭嘴资源驱动。普通口播不发送 TTS 音素。新固件若因资源更新失败而仍使用旧包，服务端会省略版本不匹配的可选表情，继续下发音频／舵机，不退回在线传输图元。独立表情命令仍检查版本和 ID。

本地待机与远程指令由同一个 ROM 调度器仲裁；无下行指令到期后按资源包播放动画／动作，远程轮次未结束时不启动。新远程指令立即终止待机执行，既不丢弃新队列内容，也不向网络报告虚假的播放完成。服务端生成结束、音频执行结束与表情／舵机全部完成继续分别处理。
