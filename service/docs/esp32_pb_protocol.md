# ROM 与服务端通信协议

本文描述当前 ROM 与主服务的实际通信契约，更新于 2026-11-18。启动发现为 **schema 2**，播放协议为 **PB v2**，紧凑媒体头版本为 **1**；这三个版本属于不同层。PB 是项目自定义的播放消息格式。

本文不定义主服务到 ASR、TTS 或外部 Agent 的协议；这些服务由主服务适配，ROM 不直接连接。历史真机数据见文末链接，不能作为本次改动的真机验收证明。

## 1. 云服务器配置与启动发现

配网页、串口配置和编译期默认值只接受一个**完整 HTTP/HTTPS 启动接口地址**，例如：

```text
https://example.com/robot/bootstrap
http://192.168.1.10:9000/api/device/bootstrap
```

编译期默认项为 `DESKBOT_BOOTSTRAP_URL`。输入地址上限 511 个 ASCII 字符，主机为域名或 IPv4、长度小于 64；必须包含非根接口路径。不接受 WS/WSS、用户凭证、查询参数、fragment、空白、反斜线或 `..`。主机、端口和路径按输入使用，ROM 不补 `/api/device/bootstrap`，不移除路径尾部的 `/`。

ROM 在独立 bootstrap 任务中直接请求该地址，仅追加设备参数：

```http
GET /robot/bootstrap?device_id=deskbot_1234
```

服务端内置启动接口是 `GET /api/device/bootstrap`，自定义公网路径由反向代理映射到它。返回 `code/message/data`：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "schema": 2,
    "device_id": "deskbot_1234",
    "realtime": {
      "protocol": "deskbot.pb.v2",
      "url": "wss://voice.example.com:9443/media/audio?region=cn"
    },
    "camera": {
      "enabled": true,
      "url": "wss://camera.example.com:9444/media/jpeg"
    },
    "face_pack": {
      "format": "deskbot-face-v1",
      "version": "0123456789abcdef",
      "url": "https://cdn.example.com/faces/0123456789abcdef",
      "size": 62865,
      "sha256": "64 个小写十六进制字符"
    },
    "audio": {"input_codec": "opus", "output_codec": "opus", "sample_rate": 16000, "channels": 1},
    "capabilities": ["local_face_pack_v1", "pcm_lipsync_v1", "local_idle_v1"]
  }
}
```

地址解析规则：

- 主连接和相机各使用返回的独立 WS/WSS 地址，允许不同主机、端口、路径和查询参数。
- 资源使用返回的 HTTP/HTTPS 地址。ROM 不从主连接地址推算资源或相机地址。
- 也支持根相对地址，例如 `/robot/audio`。它基于**启动接口的 origin**：`https://example.com/custom/bootstrap` 下的 `/robot/audio` 解析成 `wss://example.com/robot/audio`，不会补 `/custom`。
- 相对地址不支持 `audio`、`../audio` 或 `//other-host/audio`。服务端下发地址不得包含 ROM 专用的 `device_id` 参数；服务端配置同时禁止预置 `version` 参数。
- 媒体 URL 保留既有查询参数，ROM 再追加 `device_id` 和 `version`；资源 URL 原样请求。
- HTTPS 启动接口禁止下发明文 WS/HTTP 目标；HTTP 启动接口可以升级到 WSS/HTTPS。HTTP 和 WSS 的 TLS 使用 SDK CA 证书包，不使用跳过证书验证的连接。
- 不根据请求的 `Host`、`X-Forwarded-Host` 等头生成公开地址。

服务端在 `config.yaml` 配置对外地址：

```yaml
server:
  ws_path: /asr_chat
  device_bootstrap:
    realtime_url: wss://voice.example.com:9443/media/audio?region=cn
    camera_url: wss://camera.example.com:9444/media/jpeg
    camera_enabled: true
    face_pack_base_url: https://cdn.example.com/faces
```

`face_pack_base_url` 后由服务器追加 `/<version>`；资源基址不接受查询参数。以上配置用于**公布地址**，不会创建 CDN、代理转发或任意路径路由；这些目标必须已能访问对应服务。省略 `realtime_url` 时使用 `server.ws_path`；其余默认值是 `/camera_uplink`、启用相机、`/api/device/face-pack`。全部支持根相对配置。

**生效与失败处理：** ROM 每次启动获取发现结果，成功后本次运行使用该快照；当前没有运行中定时刷新或热切换连接。修改服务端地址后，设备下次启动获取新值，无需重新编译 ROM。启动接口不可达或配置不兼容时，ROM 每 10 秒重新尝试，禁止建立媒体连接，不回退猜测的固定路径。明确的错误配置也不会放行。资源下载或校验失败则保留旧包／内置包，并允许已验证的媒体连接工作。

**升级：** schema 1 的“服务器基址＋固件补路径”契约已移除。旧 NVS 服务器列表保留供编辑，但 WS/WSS 或只有 origin 的旧地址需要重新填写完整 HTTP/HTTPS 启动接口。新 ROM 和返回 schema 2 的服务端应配套升级；旧 ROM 不支持此地址契约。

## 2. 资源与本地表情

`face_pack` 的版本为 16 位小写十六进制，SHA-256 为 64 位，字节数为正且不超过 256KiB。下载后检查字节数、SHA-256、资源格式和引用，写入 FFat 双槽并校验后激活；同一启动接口、同版本缓存可复用。下载、存储或校验失败保留旧资源。版本化 HTTP 资源支持 ETag／304；已被服务端有限缓存淘汰的旧版本返回 404，不会返回冒充旧版本的新数据。

画布为 **284×240**。正常实时消息使用本地表情 ID／关键帧引用，资源不匹配时服务端可以省略音频或动作中的可选表情，独立表情命令则失败。

- 独立动画：`pb_single` 携带 `anim_id`，不能同时带音频、舵机或 `anim[]`。
- 同步表情：`anim:[{"anim_id":"happy","frame":0,"ms":200}]`，`frame` 为从 0 开始的关键帧索引，`ms` 为插值时长。
- 当前口型使用实际输出 PCM 的 RMS 音量，在 ROM 本地控制开闭嘴；普通口播不传音素，也不做 MFCC／元音分类。旧 `voice_key` 被忽略。
- `local_idle_v1` 由 ROM 播放包内待机程序，服务端不再周期下发待机序列。远程指令抢占本地待机，待机不向服务端伪造完成回执。

历史 `phoneme_id` 与部分 inline `elements` 图元解析仍保留，但不作为当前普通语音的生成方式。inline 解析支持 rect、circle、line、ellipse、round_rect 及相应轮廓和 text；旧文档列出的全部绘图别名并非固件契约。素材制作及格式见 [FACE_ASSETS.md](../../hardware/FACE_ASSETS.md)。

## 3. 通道与设备身份

| 通道 | 默认路径 | 用途／当前访问条件 |
|---|---|---|
| HTTP 启动发现 | `/api/device/bootstrap` | 公开，只返回非敏感配置与共享资源信息 |
| HTTP 表情资源 | `/api/device/face-pack/{version}` | 公开共享素材 |
| 主 WebSocket | `/asr_chat` | 双向音频、PB、回执与 ROM 工具；当前只要求 device_id |
| 相机 WebSocket | `/camera_uplink` | 独立 JPEG 上行；当前只要求 device_id |
| Web 画面订阅 | `/camera_view` | 登录用户的 debug token，并校验设备归属 |
| Web 流水线订阅 | `/device_pipeline` | 订阅侧 debug token＋归属；生产侧仍仅要求 device_id |

设备主连接缺少 ID 时关闭码为 1008、reason 为 `device_id_required`。相同 ID 的新主连接会替换旧连接，相机连接不接管主连接。

**当前没有设备凭证校验。** device_id 是公开标识，并不能证明设备身份；TLS 也不解决冒用已知 ID 的问题。设备认证是待实施改进，本文不宣称已经有设备密钥、签名握手或防冒用能力。公开 bootstrap 不下发用户资料、服务密钥或设备凭证。

## 4. 主连接握手与能力

服务端接入后先发送 TEXT JSON：

```json
{"type":"ready","capabilities":["pb_compact_v1","audio_bin_v1","ack_bin_v1"]}
```

ROM 每次连接（含重连）发送基础打包 BIN：

```json
{"type":"device_hello","capabilities":["pb_stream_v1","pb_epoch_v1","pb_playback_progress_v1","anim_id_v1","anim_frame_id_v1","pcm_lipsync_v1","local_face_pack_v1","local_idle_v1","pb_compact_v1","audio_bin_v1","ack_bin_v1","rom_tools_v1","image_rgb565_v1"],"anim_version":"实际激活的资源版本"}
```

ROM 还声明 `phoneme_id_v1`、`raw_preview_pcm_v1`；后者只有服务端也声明时才启用。当前实时桥接要求 `pb_stream_v1` 和 `pb_playback_progress_v1`，缺失时结束连接；不保证旧 ROM 可以退回旧式对话。

三个紧凑能力独立取双方交集：

| 能力 | 效果 |
|---|---|
| `pb_compact_v1` | 连续链后续 JSON 可以继承重复元数据，进度约 200ms 限频，精简回执 |
| `audio_bin_v1` | Opus 上行、纯音频续包使用 24 字节头 |
| `ack_bin_v1` | 已知非零 epoch 的回执使用 32 字节头 |

未协商紧凑格式时使用完整基础封装；这项回退不等于支持缺少流式能力的旧固件。每次主连接换代清除能力、音频元数据、旧队列和 epoch 映射。

`ready` 当前只表示传输接入，不表示外部对话服务已经可用；上游启动期间的音频会被丢弃，尚无 `conversation_ready` 状态通知。

## 5. 基础消息封装与上行

每条基础 WebSocket binary message 自包含：

```text
u32be(JSON UTF-8 字节数) + JSON UTF-8 + 可选媒体字节
```

JSON 上限 64KiB；ROM 主连接整条入站消息上限 256KiB。多条消息不共享媒体尾部。服务端发送串行化；ROM 的回执／控制发送队列优先于音频队列。`ready` 保留 TEXT，其他设备主通道消息使用 BIN；当前接收循环**不接受 TEXT+独立 BIN 或裸音频兼容接入**。

音频基础上行示例：

```json
{"type":"audio","codec":"opus","sr":16000,"ch":1,"frames":2,"next_bin_len":80,"opus_bytes":80,"raw_bytes":0,"flush":0}
```

尾部紧随 80 字节音频。多帧 Opus 为 `u16be(帧长)+Opus帧` 串联，每帧 20ms；当前 ROM 常规每包 2 帧／40ms。可选 raw preview 为 8kHz 单声道 PCM16，与 Opus 同包，长度为时长×16 字节，仅用于协商支持的预览链路。

ROM 收尾可发送 `audio.flush=1`，包括空媒体包；当前持久对话桥接不把它视为结束上游会话，不承诺触发“ASR→LLM→TTS”的独立轮次。

相机上行（独立连接）为基础封装：

```json
{"type":"camera_frame","codec":"jpeg","next_bin_len":12345,"seq":42}
```

尾部为 JPEG。ROM 使用单个最新帧槽，发送拥塞时从约 2fps 降到 0.25fps，再逐步恢复；没有实现下行 `cam_fps` 控制。

当前主接收循环处理的类型是 `device_hello`、`audio`、`camera_frame`、`pb_ack`、`rom_result`。没有应用层 `ping/pong`、`user_text` 或独立 `flush` 命令处理。ROM 暂留的 `boot_connect` 会被忽略，不承诺开机问候。WebSocket 控制帧 ping/pong 与这些 JSON 消息不同。

## 6. PB 播放下行

| type | 含义 |
|---|---|
| `pb_start` | 连续链首包，idx=0 |
| `pb_chunk` | 续包 |
| `pb_end` | 末包，可只表示结束、不携带媒体 |
| `pb_single` | 独立一包播放／动画／动作 |
| `pb_cancel` | 取消指定请求／epoch |

连续流：`pb_start(0) → pb_chunk(1…) → pb_end(N)`；单包使用 `pb_single`。首包示例：

```json
{"type":"pb_start","pb_ver":2,"req":"talk-1","epoch":42,"stream":true,"idx":0,"pts_ms":0,"chunk_ms":200,"level":1,"sr":16000,"fmt":"s16le","ch":1,"audio":{"next_bin_len":6400},"anim_version":"0123456789abcdef","anim":[{"anim_id":"happy","frame":0,"ms":200}],"servo":[{"xm":1,"ym":2,"x":5,"y":0,"ms":200}]}
```

音频与可选动画、动作的媒体仍在**同一条消息**内，不拆成异步消息。音频为单声道 16kHz，支持 Opus 或 s16le。`audio.next_bin_len` 声明音频尾部字节数，Opus 使用 `audio.frames`；发送方应确保尾部精确等长，PCM 字节数为 `chunk_ms × sr / 1000 × 2`。

`req` 长度为 1～36 字符；`epoch` 标识本连接内的轮次，连续流必须非零。`idx` 逐块递增；`pts_ms` 是轮内内容时间轴，不能直接和服务端／设备的墙钟比较。`chunk_ms` 为本块时长。带音频的首包声明 `sr/fmt/ch`，协商紧凑模式后续包可继承。更换媒体格式应开启新链。

`volume` 为可选的 0～100 音量；`level` 为 0～3。`replace/append/default` 是**服务端序列调度策略**，由服务端排队、发出取消实现；ROM 当前没有实现独立的 action 优先级调度，不能靠在任意 JSON 包里加 action 来抢占。

舵机字段 `xm/ym`：0 绝对、1 相对、2 保持；`x/y` 为角度，`ms` 为动作时长。动画最多 64 个时间片、舵机最多 32 段；动画内容与音频可以独立存在，纯音频不要求附带 anim。

## 7. 紧凑音频格式（24 字节）

所有整数为无符号大端。一个完整 WS message＝头＋媒体，无额外 JSON 长度前缀。

| 偏移 | 字节 | 字段 |
|---|---:|---|
| 0 | 2 | ASCII `DB` |
| 2 | 1 | 版本 1 |
| 3 | 1 | 类型：1 下行纯音频续包，2 上行 Opus |
| 4 | 4 | epoch；上行为 0，下行非零 |
| 8 | 4 | idx；下行共用 PB 序号，上行连接内递增且允许跳号 |
| 12 | 4 | pts_ms |
| 16 | 2 | 时长 ms，非零 |
| 18 | 2 | Opus 帧数；PCM 为 0 |
| 20 | 4 | 下行媒体字节数／上行 Opus 部分字节数 |

下行只能用于已经登记首包的纯音频 `pb_chunk`，编码由首包继承，需匹配 epoch、连续 idx 和非倒退时间戳，媒体尾部必须精确等长。带表情、舵机、音量等额外控制的包仍走基础格式。`pb_end/pb_single/pb_cancel` 不使用此头。

上行固定 Opus 16kHz 单声道，每包 1～50 帧，时长＝frames×20ms；允许尾部追加已协商的 raw preview，必须正好为时长×16 字节。媒体上限 `256KiB−24`；常规 ROM 上行队列另限每包媒体 4KiB。flush 使用基础格式。

## 8. 执行回执、流控与取消

基础回执示例：

```json
{"type":"pb_ack","ack_type":"pb_credit","req":"talk-1","epoch":42,"idx":3,"buffered_ms":320,"device_ms":12345,"space":10}
```

| ack_type | 语义 |
|---|---|
| `pb_chunk` | 非连续旧窗口流控确认；服务端仍保留此播放辅助路径 |
| `pb_credit` | 累计接收／排队流控确认，不证明已播放 |
| `pb_started` | 首个对应 DMA 周期已完成输出 |
| `pb_progress` | 实际 DMA 输出进度，带 played_ms |
| `pb_audio_end` | 音频执行结束，动画／动作可能仍在继续 |
| `pb_end` | 音频、动画、舵机均结束 |
| `pb_cancelled` | 执行器已处理取消边界 |
| `pb_error` | 执行失败 |

服务端连续流首块立即发出，随后控制在最多 4 块、累计约 1000ms 的在途窗口中；累计 credit 释放窗口。结束包可绕过普通窗口容量限制。ROM 根据音频缓存／执行队列发送 credit，不以动作尾段阻塞音频接收。

`pb_progress` 在紧凑模式约每 200ms 报告，结束／取消强制报告最终进度；服务端不能用 credit 或下发字节推算“播完”。音频与全部执行器同刻结束时，紧凑模式可省略重复的 `pb_audio_end`。这些进度不是麦克风到扬声器的声学端到端测量。

取消包为 `{"type":"pb_cancel","req":"talk-1","epoch":42}`。ROM 优先处理取消，丢弃旧链积压，保留取消标记之后的新链；等待各执行器跨过取消边界后确认。旧 epoch 与已取消轮次的后续媒体不能复活。重连丢弃旧 TX 队列、旧 PB 状态，不重放旧语音。

已协商 `ack_bin_v1`、epoch 非零且无扩展错误详情时可使用 32 字节回执：

| 偏移 | 字节 | 字段 |
|---|---:|---|
| 0～3 | 4 | `DB`、版本1、类型3 |
| 4 | 4 | epoch |
| 8 | 4 | 累计 idx |
| 12 | 4 | played_ms，仅 flags.bit0 有效时解释 |
| 16 | 4 | buffered_ms，无信息为0 |
| 20 | 4 | device_ms |
| 24 | 2 | 最小执行器可用 space |
| 26 | 1 | 回执代码：按上表从0到7 |
| 27 | 1 | flags，bit0 表示有效进度，其余位必须为0 |
| 28 | 4 | queue_wait_ms，仅 bit0 有效时解释 |

服务端连接内最多保留 64 个 epoch→req 映射；未知 epoch、超前 idx、未知代码／版本／flags、非32字节消息丢弃。扩展错误详情、无 epoch 的回执使用基础 JSON。计数器为32位毫秒值，会回绕，不跨设备比较。

## 9. ROM 工具命令

需要 `rom_tools_v1`；图片另需 `image_rgb565_v1`。使用主连接基础封装，与 PB 共用串行发送队列：

```json
{"type":"rom_command","id":"0123456789abcdef0123456789abcdef","op":"volume","arguments":{"action":"adjust","value":10},"next_bin_len":0}
```

| op | 参数／媒体 | 成功回执 |
|---|---|---|
| `volume` | action=get/set/adjust；set 0～100、adjust −100～100 | status=completed，返回实际 volume |
| `image` | width=284、height=240、duration_ms=1000～30000；尾部精确136320字节 RGB565 小端像素 | 绘制后 status=displayed |
| `stop_image` | 无媒体 | status=completed |

回执 `{"type":"rom_result","id":"同请求","ok":true,"status":"completed","volume":60}`；失败 `ok=false` 并带 error。请求 ID 为32位十六进制，服务端只接受正在等待的 ID，最多8个并行等待；ROM 缓存近期8个请求，重复同 ID／同内容不重复执行相对音量调整。图片由显示任务执行；displayed 只证明已显示，不证明展示时长已经结束。该通道不改变 PB 的 epoch 或音频元数据。

## 10. 当前边界与后续整改

以下是已知实现限制，不是已完成能力：

1. 设备接入尚无独立身份凭证；必须在认证成功后才允许抢占主连接，且相机／生产者也需认证。
2. 普通 JSON 路径未和紧凑路径统一强制校验完整序列、pb_ver、精确媒体长度；解析失败通常只记录日志，服务端可能等待超时。发送方仍应遵守本文的序列和长度约束。
3. ready 尚未区分传输就绪与对话上游就绪；启动音频可能丢失。
4. 发现配置是本次启动快照，尚无运行中自动刷新与热切换。

[媒体格式历史验收](media-wire-validation-2026-11-11.json)、[播放及取消历史记录](device-playback-verification-2026-11-10.json)、[旧 HTTP 启动验收](device-bootstrap-http-2026-11-11.json)均属于各自时间点的固件，不证明 schema 2 已完成真机验证。USB 测试复用 PB 基础封装，见 [USB_AUDIO_TEST.md](../../hardware/USB_AUDIO_TEST.md)。

本次 schema 2 的编译、172 项相关测试及本机 HTTP 启停验证见 [2026-11-18 实施记录](device-discovery-2026-11-18.json)；尚未烧录真机或部署云端。

后续已于同日完成云端部署及 USB 烧录，真机确认下载并启用新版资源、媒体和相机连接；验证范围及延迟修复见 [发布记录](rom-latency-release-2026-11-18.json)。
