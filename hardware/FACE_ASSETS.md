# 内置表情：制作、预览、生成与烧录

## 本地讲话抢占自主待机（2026-11-18）

ROM 的 PB 任务直接读取 ESP-SR 已去抖的语音活动状态，不消费 USB 诊断事件队列，也不等待服务器回复。选择待机片段前、分发前以及片段执行等待期间都会检查：检测到用户讲话即取消本地待机的执行器工作，保留远程 PB 队列和轮次。头部保持中断附近的姿态；本轮没有增加强制回中、提示音或固定聆听表情。

用户讲话期间禁止启动新待机；最后一次观察到讲话后，重新等待包内 `after_ms`（当前 15 秒），避免在短暂停顿中立即恢复小动作。现有 VAD 的语音确认和防噪声规则保持不变，因此“本地检测到讲话就停”不表示物理开口到停止为零延时。VAD 未就绪或处理输出不健康时暂停自主待机，避免执行无法对讲话让位的动作。

此规则只针对本地自主待机。远程口播、音乐、独立动作、图片等仍采用现有调度／打断规则；它不修改 Flecket 的 `allow_barge_in` 默认值，不把本地 VAD 自动升级为播放中双讲检测，也不会撤销服务器工具的副作用。

验证：`test_local_idle_speech.py`、`test_face_package.py`、`test_audio_lipsync.py`、`test_rom_control.py` 共 9 项通过；其中直接编译实际 PB 待机等待分支，覆盖讲话停止、检测故障停止和远程任务不被误停。固件环境 `seeed_xiao_esp32s3` 编译通过；未刷机、未测真实开口到物理停止的延迟。

表情的唯一源文件是 `service/data/global/deskbot-face.json`。`emotions` 存动画关键帧，`phonemes` 存嘴部造型素材，`lipsync` 定义本地音频分析结果与口型素材的映射，`idle` 定义空闲动画和舵机动作。设备启动先通过 HTTP 获取配置并更新本地资源包，再建立实时连接；编译进固件的资源保留作首次启动和下载失败的后备。

常用流程：**编辑 JSON → 网页预览／校验保存 → ROM 重启时自动检查并下载 → 按 ID 播放**。更新素材、口型映射及待机行为不再需要重新烧录。只有修改固件解释器或更新离线后备资源时才编译烧录。

## 日常使用

在仓库根目录启动工作台（Python 3.10+，仅标准库）：

```bash
python3 hardware/tools/face_studio.py
```

打开 <http://127.0.0.1:8975/>。选表情、调整当前表情数据，点击“应用到预览”，满意后点击“保存并生成资源”。页面也支持新建表情、导入完整表情库、下载草稿、与已保存版本对照。在“资源类型”中选择“口型音素”，可编辑和预览 44 个口型；保存同时更新两类资源。

当前编辑器以 JSON 参数编辑为主，还不是拖拽绘图工具。`ms` 是向这一帧过渡的时间，单位毫秒；连续两帧姿态相同可以形成停留。图元用同一顺序、同一种 shape，有助于平滑插值。显示尺寸是 284 × 240，`c` 是 RGB565 整数颜色。浏览器抗锯齿、字体与设备像素绘制有所不同，定稿仍需真机观察。

保存会检查 ID/别名唯一性、帧数、时间、固件支持的图元和每层图元数量，并自动生成资源。源文件变化冲突会阻止覆盖，旧文件备份保存在 `hardware/.pio/face-backups/`。备份是本机临时保护；版本管理仍应提交源 JSON 和生成资源。

保存后编译或烧录：

```bash
# 只编译，自动重新检查并生成表情资源
./hardware/flash_rom.sh build

# 编译并烧录；将串口替换为实际设备串口
./hardware/flash_rom.sh upload /dev/tty.usbmodemXXXX
```

不需要先手动导出，PlatformIO 的编译前钩子会自动执行。无效表情会让编译中止，避免误烧旧资源。不要手改 `firmware/generated/face_assets.inc`，下次生成会覆盖它。`service/tools/refine_face_expressions.py` 是最初制作本轮表情的脚本，不是编译步骤；重新运行会覆盖网页编辑结果。

烧录后在串口监视器中验证：

```text
face_version
face curious
face got_it
face sleep
face waiting
```

`waiting` 是 `thinking` 的兼容别名。每条命令播放一轮，沿用 PB 优先级、取消和完成回报机制；工作台上的“循环”仅影响网页预览。

## 生成产物和版本

```bash
python3 hardware/tools/face_assets.py
python3 hardware/tools/face_assets.py --check
```

生成两个文件：

- `hardware/firmware/generated/face_assets.inc`：C++ 常量表，由 `face_assets.cpp` 引入；重复完整姿态会去重。
- `hardware/firmware/generated/face_assets_manifest.json`：资源内容版本、ID、别名、帧数和总时长。服务端部署时应带上与固件配套的清单。

版本取规范化播放数据的 SHA-256 前 16 位。调整坐标、颜色、时间、ID 或别名会改变版本；只调整 JSON 缩进或展示标题不会改变。字符串 ID 不受数组排序影响，建议长期保持 `curious` 等 ID 稳定；改名时保留旧名为别名。

目前 15 个表情、44 个口型音素，共 154 帧。此次构建中的图元、索引、帧、版本常量表共 40,729 字节，已检查位于 `.flash.rodata`；这个数字不包含 ID 字符串和播放器代码。运行时只将选中的动画解包到 PSRAM，播放完成或取消后沿用现有模型释放，不复制整个库到 RAM。

v1 限制：每表情 1–64 帧，每帧至少 40 ms，每表情总长不超过 10 秒，每层最多 16 个图元。表情的五个可见图层必须明确提供（音素至少提供 mouth，播放时只加载 mouth），隐藏装饰可用黑色或零尺寸占位，避免上个表情残留。编译器支持的图元与 `pb_model.h` 对齐。

## 只发 ID 的下行协议

最小 JSON：

```json
{"type":"pb_single","req":"face-001","anim_id":"curious"}
```

`req` 是本次播放的请求标识，须非空且最多 36 字节。ID 是表情内容的标识，两者用途不同。设备会查本地资源，补出帧和时长。实际仍按原 PB 协议发送 **WebSocket binary message**：四字节大端 JSON 长度 + UTF-8 JSON，不能直接发送为 WebSocket TEXT。

```python
import json
payload = {"type": "pb_single", "req": "face-001", "anim_id": "curious"}
raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
packet = len(raw).to_bytes(4, "big") + raw
# 在已有、获授权的设备下行连接中发送 packet；不要另开连接抢占设备。
```

可选 `anim_version` 应来自配套清单，设备会拒绝不匹配版本。`idx` 如提供只能为 0；`chunk_ms` 如提供必须等于清单总时长。v1 不允许与 `anim`、`servo`、`audio`、媒体 binary 或 `stream=true` 混用。未知 ID、混用字段、版本不匹配会被解析器拒绝并记录日志，当前解析失败路径不保证发送错误 ACK。

设备的 `device_hello` 会声明 `anim_id_v1` 并携带 `anim_version`。服务端已保留此能力和版本，并在发送 ID 前检查兼容性。旧设备或版本不匹配时不会发送 ID，不自动回退为另一套动画。

现有服务代码可这样创建一个独立表情请求，沿用 `DeviceWsService.send` 队列：

```python
import uuid
from deskbot_server.model.pb_seq import PbBlock, PbSeq, PbType

# manifest 是随本次固件部署的 face_assets_manifest.json 内容。
asset = next(a for a in manifest["assets"] if a["id"] == "curious")
req = uuid.uuid4().hex
seq = PbSeq(req=req, entries=(PbBlock(
    type=PbType.SINGLE, req=req,
    anim_id=asset["id"], anim_version=manifest["version"],
    chunk_ms=asset["duration_ms"],
),))
accepted = await device_ws.send(device_id, seq)
```

这里补齐了模型和发送能力，但没有把所有旧业务场景自动改写为 ID 请求。调用方应使用上述入口；原有 TTS 生成路径仍可走原帧数据；调用方也可以使用下面的音素 ID 格式。当前显示队列按顺序执行；音素更新时保留屏幕当前眼神与装饰，不代表基础动画和嘴型有两个独立并行的时间轴。

## 内置口型与插值规则

资源清单的 `phonemes` 包含音素名称、别名、帧数、默认总时长。支持 `sil`、`a`、`ai` 等，以及 `AA`、`IY`、`üan` 等原有别名（区分大小写）。复合音素保留全部子帧，并按权重缩放到本次语音指定时长。

设备声明 `phoneme_id_v1` 后，`anim` 可以省去绘图数组，只给音素与时长：

```json
{"type":"pb_single","req":"mouth-001","idx":0,"chunk_ms":300,"anim":[{"phoneme_id":"a","ms":120},{"phoneme_id":"ai","ms":180}]}
```

同样使用现有 binary 封包；可附带根级 `anim_version` 校验整个表情/音素库版本。服务模型对应 `PbAnim(phoneme_id="a", ms=120)`。`phoneme_id` 与该帧 `elements` 互斥；它允许放进携带音频的 `pb_start/chunk/end` 序列，沿用 `epoch/pts_ms` 时钟。旧的 `phoneme` 字段仍仅是内联帧的调试标记，不会自动触发查表。

音素只替换嘴部图层，不使用素材中的固定眼睛位置覆盖当前眼神。每个音素时长需至少等于子帧数且不超过 30,000 ms；展开后的每个 PB 包最多 64 帧，超限会拒绝，调用方应拆包。口型切换和表情切换复用同一套插值。

- 相同类型图元：位置、尺寸、圆角和 RGB565 颜色线性插值。
- 图元类型、文字内容或字号不同：旧图形前半段淡出，新图形后半段淡入，避免硬切。
- 多余图元淡出；增量消息未指定的图层沿用上一个可见状态。
- 每次绘制保存实际显示姿态。取消或打断保留该姿态，新动画从这里开始，而不是从旧动画的目标帧开始。
- 动画结束明确推送 `t=1`，过期未显示的语音帧不会改变下一次插值的起点。
- 过渡占用目标第一帧的 `ms`，不会额外添加一段固定过渡时间。网页也按此规则预览；真实屏幕刷新和字体仍需烧录后观察。

## 更新策略

HTTP 资源更新已实现，普通实时消息不传图元库或口型。配网页只接受带完整接口路径的 HTTP/HTTPS 启动地址。固件直接请求该地址（只追加 device_id），读取 schema 2 的完整流地址、相机地址及资源包地址／版本／大小／SHA-256；不再补固定路径。各目标可以使用独立域名、端口，根相对地址只基于启动接口 origin。发现失败时重试，不猜测媒体端点。HTTP 请求在独立 bootstrap 任务执行，完成前禁止语音和相机长连接。

资源包格式为 `deskbot-face-v1`，包含去重的图元指令、帧表、表情和音素索引、口型映射及待机程序。这是由 ROM 中既有解释器执行的有界声明式渲染程序，不是下载后动态编译／执行 C++。当前包约 63KB，设备单包上限 256KiB。服务端与固件共同限制表大小、图元坐标、帧数、时长和动作参数。

包保存在现有 FFat 分区的双槽文件 `/face0.bin`、`/face1.bin` 中（内容为 JSON）。启动时按完整启动接口地址加载并校验缓存；新版本下载完整、SHA-256 和格式校验通过后，写临时文件 `/face.tmp`、同步并读回验证、替换非活动槽、保存该槽元数据，最后切换 NVS 活动槽。失败时保留旧槽或内置资源；缓存损坏时尝试另一个同源槽。不会因换服务器而沿用另一服务器的下载包。重连不会重复下载，下一次启动重新检查版本。

写入临时文件后会再次读回并校验 SHA-256，再执行槽切换。HTTPS 启动前先做有界时钟同步，HTTPS 与 WSS 均使用 SDK 的 CA 证书包。串口 `resource_status` 返回本次启动的资源来源（compiled/cache/downloaded）、检查结果、槽号、版本和通道启用状态；可区分首次更新与重启后跳过下载。该诊断不会采集音频或修改资源。

以下属于旧 schema 1 的历史结果，不能作为当前 schema 2 的验收证明。2026-11-11 真机验证：首次下载激活、同版本重启跳过下载、新版本切换双槽、服务器关闭时缓存启动及自主待机均通过。把 `open_phoneme` 临时改为 `i` 后，普通联网模式的实际音频／屏幕诊断确认使用新包映射；验证后已恢复原素材。纯音频、表情 ID、动作、音频附带表情帧引用和动作、取消后新音频均有设备完成回执，用户确认声音和口型正常。最新相关服务回归 114 项、原生／USB 测试 21 项通过，固件编译通过。验收记录见 `service/docs/device-bootstrap-http-2026-11-11.json`。

固件实测修复包括：执行 Flash 写入的任务使用内部 RAM 栈；I2S 中断使用内联的 32 位原子读取，避免写 Flash 时调用位于 Flash 的辅助函数。双槽提交的故障注入测试不等于 FAT 文件系统的整卷掉电事务保证；本轮未在写入中执行硬断电。

串口 `continue_boot` 可在开机配网阶段执行与网页“继续启动”相同的操作，避免接入热点的未知终端使倒计时持续暂停；只接受这一条早期命令，不提前调用依赖媒体队列的普通命令。`mouth_trace 10000` 在普通联网模式输出 10 秒口型标量诊断，包含实际包版本和开闭口型 ID；上限 60 秒，`mouth_trace 0` 关闭，不记录音频，USB 拥塞时丢弃诊断样本。

当前统一使用 `rms-v1`；`closed_phoneme` 和 `open_phoneme` 指向包内闭嘴／张嘴资源（默认 `sil`、`a`），按实际输出 PCM 音量驱动开合。无需音色模板、元音识别或 `voice_key`，更换 TTS 音色无需额外配置。旧缓存包中的 MFCC 元数据会被忽略，仅沿用开闭嘴资源。纯音频服务可直接使用本地基础姿态和口型资源。

`idle.after_ms` 是无远程指令后的首次等待，`repeat_ms` 是两次本地待机开始的最小间隔，`clips` 依次循环。每项包含 `anim_id` 和可选内容的 `servo` 数组。默认等待 15 秒，间隔 12 秒，交替播放 idle 和带轻微转头的 curious。进行中的远程音频／动作不会被待机抢占；新远程命令到来时取消本地待机，等各执行器消费取消边界后再交付新命令，保留新命令及其完成标志。待机不向服务端伪造 PB 完成回执。服务端对声明 `local_idle_v1` 的设备停止旧版定时待机推送；主动对话和人脸交互仍可下发正常远程指令。

## 验证记录

### 音量口型（2026-11-12）

已移除 MFCC 元音识别任务、音色模板匹配、五类元音混合、共享 PCM 分析缓冲和校准工具。联网播放和 USB 播放均按最终输出 PCM 的 RMS 音量，在闭嘴与张嘴素材间平滑插值；静音收口，停止、取消和欠载闭嘴。

串口 `lipsync_status` 固定报告 `mode=rms`；`audio_status` 和 `mouth_trace` 保留。`lipsync_mode`、`lipsync_profile` 命令已移除。设备仅声明 `pcm_lipsync_v1` 口型能力，不再声明 `mfcc_vowel_v1`。旧资源包可继续加载，音色和元音字段不影响口型。

服务端资源打包器同样统一输出 `rms-v1` 和开闭嘴两个资源引用；旧元音模板在导出时丢弃，不参与资源版本计算。普通批量／流式口播不再查询音色模板或自动生成 `voice_key`。

此前元音试验的历史验收记录保留在 `service/docs/vowel-lipsync-physical-2026-11-11.json`。

2026-11-10：按 FreeRTOS 固件规范及 HTTP 服务工程 v0.4-draft 的字段扩展路径实施。没有新增生产 HTTP 路由、数据库表或任务。

```bash
python3 -m unittest discover -s hardware/tests -p 'test_face_*.py' -v
cd service
.venv/bin/python -m pytest tests/test_builtin_face_ids.py tests/test_pb_ack_window.py tests/test_pb_wire_format.py tests/test_builtin_face_expressions.py -q
```

- 8 项工具/原生 C++ 测试通过：确定性、逐图元数据一致、非法数据、过期产物、全部 ID/别名加载及逐次分配失败释放、网页保存/冲突/来源检查。
- 38 项服务端相关测试通过：ID 序列化、能力/版本门控、原 PB 和表情回归。
- `./hardware/flash_rom.sh build` 通过；浏览器点击保存生成成功。
- 尚未烧录设备，未进行真机显示、断电重启、取消播放及服务端到设备的实机联调。


## PCM 自动口型

当前普通口播使用 `anim:[{"anim_id":"happy","frame":0,"ms":200}]` 引用本地表情关键帧，完全不发送 TTS 音素口型或绘图数组。`frame` 从 0 开始，`ms` 为向该帧过渡的时长；表情继续插值，音频嘴部独立叠加，结束/打断闭嘴。上面的 `phoneme_id` 保留给手动预览和旧协议兼容，普通口播不再调用它。

自动口型是 ROM 的共用能力，正常联网与 USB 模式均使用 `speaker.cpp` → `audio_lipsync.h` → `display.cpp`：先按实际音量缩放并下混播放 PCM，由 `process_audio` 计算 RMS；只有相应 DMA 块完成输出后，显示任务才通过 `process_animation` 更新开合，在当前资源包指定的闭嘴／张嘴素材之间插值。服务端只需发送音频和可选表情 ID，不承担口型计算或插值。音量门限位于 `deskbot_config.h`，默认 -45/-15 dBFS；开口／收口时间常数为 35/70 ms。

正常 PCM 静音逐渐收口；停止、取消及实际播放欠载则清除过渡状态，在下一次嘴部刷新时闭嘴。新播放 generation 从闭嘴状态重新开始，空闲墙钟时间和旧话轮嘴形不会带入新话轮。USB 的 `mouth.underrun` 仅用于观察这条共用 ROM 路径，主机测试脚本不驱动口型。当前统一为 RMS 音量开合模式，无需校准或配置音色。

生成器现在还输出 `service/src/deskbot_server/pb/data/face_assets_manifest.json`，随服务安装包发布；包含每帧时长，和 ROM 使用同一内容版本。重新制作素材后应同时更新设备固件与服务清单。具体协议、RMS 模式边界、回归结果和真机验收步骤见 [PCM 口型实现](../service/docs/esp32_pb_protocol.md#2-资源与本地表情)。


## 不连接 Wi-Fi 的真机测试

运行 `./hardware/usb_test.sh` 即可通过 USB 发送实际音频和动画 ID，回收口型、时序和 AEC 诊断。模式切换、真实语音对照与指标边界见 [USB 快速实测](USB_AUDIO_TEST.md)。
