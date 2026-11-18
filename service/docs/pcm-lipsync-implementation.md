# 本地动画 ID 与 PCM 口型实现记录

## 2026-11-18：清理停用的音素口型链路

本轮范围为服务端口型／表情遗留代码与本地制作工具；未修改固件的响度算法、表情播放、舵机执行或线上设备。保留工作区其他已有修改。以下后续记录优先于本文件 2026-11-11 的历史说明。

- 删除未被运行时调用的 `pb/phoneme_anim.py`、`pb/face_bundle.py`、`pb/anim_defaults.py`、`pb/talk_sim.py` 以及配套的整脸合并、展开和缓存辅助函数。
- 豆包、MOSS、macOS TTS 统一使用 `client/tts/audio_segments.py` 按 200 ms 切分 PCM，逐采样保留原音频；移除拼音／英文 G2P 时间轴及其依赖。现有 TTS 端口名称和字段继续兼容，音素字段为空。豆包时间戳改为显式启用，普通合成不请求。
- 删除首页未有服务端生产者的逐音素模拟分支；网页说话状态示意与设备实际音量口型明确区分。旧 `/debug/simulation` 转到 `/debug/devices`，移除逐音素仿真页并重建静态页面包。旧 TTS 合成接口继续提供音频。
- 删除调用已移除 MFCC 命令的 `tools/verify_vowel_lipsync.py`，把仍被媒体协议验证调用的串口记录器保留为 `tools/device_diagnostics.py`。历史 JSON 验收记录保留。
- 表情工作台改为调用当前编辑器编译入口 `compile_editor_document`，与资源生成器共用转换规则；保留手动嘴型素材预览、本地表情引用、旧包格式兼容和 ROM 兜底资源。

验证：22 个相关服务测试文件共 348 项通过、2 项失败。失败项为 `test_app2c_pages.py::test_2c_home_embeds_live_camera_view_under_stage` 的旧 `CAMERA · LIVE` 文案断言，以及 `test_2c_home_integrates_robot_motion_preview` 的旧 `.home-deck-actions` 样式断言；核对 HEAD 后确认对应文案／样式在本轮修改前已不存在，本轮未改相机卡片和该样式。`test_audio_lipsync.py`、`test_face_assets.py`、`test_face_package.py`、`test_face_studio.py`、`test_face_transition.py` 共 14 项通过。新增 `test_tts_audio_segments.py` 验证音频完整性、分片时长、音频下发、无音素标签、豆包时间戳开关及停用配置过滤。修改文件的 Ruff F 检查、首页脚本 Node 语法检查及 `python hardware/tools/face_assets.py --check` 均通过，内容版本保持 `f28d677b234bc5b6`。未烧录、未部署，未执行本轮真机声画验收。

舵机边界：包内 `idle.clips[].servo` 继续承担离线／无交互时的自主待机。交互动作继续由服务端读取 `servo.json` 预设并按场景编排，经 PB 独立 `servo` 字段下发；本轮未改变动作归属。建议交互采用预设动作与上下文参数组合，设备端继续统一限制角度与执行取消。

## 2026-11-11：初次实现记录

日期：2026-11-11。范围：当前仓库的 `hardware/firmware` 与 `service`，依据 `moss-live-demo/docs/esp32-s3-realtime-lipsync.md` 接入无模板 RMS 降级方案。遵守本项目 FreeRTOS 固件规范与 HTTP 服务工程 SOP 的实现需求路径；保留已有未提交实现和调度链路。未改外部参考项目。

## 实现行为

- `PbService.get_scene_pb_seq()` 用根级 `anim_id` 播放本地整段动画。普通口播 `build_pb_wire_pairs()` 用 `anim_id + frame + ms` 引用本地关键帧，不再调用 TTS 音素到嘴形的转换。TTS 仍可返回旧字段用于其他使用方，普通下行忽略这些标签。
- 生成器同时维护 ROM 常量表和服务安装包内的资源清单。清单包含帧数、各帧时长、别名及内容版本；服务发送前检查设备能力和版本。当前版本 `4b21b918083d28d3`。更换素材后须重新生成、烧录 ROM，并更新配套服务；未知 ID 不自动回退为大图元包。
- 新协议兼容独立 ID、随音频的关键帧引用、原有手动图元和 `phoneme_id` 预览。随音频的帧引用只解包选中的本地帧，不反复分配整段动画。
- 显示保留原有表情插值和打断时从已显示姿态继续过渡的行为。嘴形作为独立叠加层，不改变表情插值的起点，也不覆盖眼睛、鼻子、装饰。约每 10 ms 平滑参数；空闲时约 30 FPS 仅传嘴部包围区域，表情变化时仍合成整脸。
- 播放任务对调音量、下混后的 PCM 预计算 RMS。无完整模板时输出 SILENCE/UNKNOWN；UNKNOWN 继续开合。开合门限为 -45/-15 dBFS，时间常数为 35/70 ms。停止、取消时将口型立即归零。
- I2S 使用 IDF 新驱动的三个 160 帧 DMA 缓冲，四个固定待输出块。DMA 回调只发布已计算结果、拷贝下一块、更新短计数；不做 FFT、浮点分析、日志或分配。空 DMA 缓冲实际输出零，不循环旧音频；源 PCM 在交接前复制为固定队列自有数据。
- DMA 完成时才发布嘴形和内容播放位置。64 位源采样计数统一换算时间，双声道下混为一帧，不重复计时。播放欠载时内容位置不随墙钟走，输出静音并收口。取消立即使旧 generation 失效；播放任务停止 DMA、清理固定队列和缓冲，并通过功放 SD 静音。
- PCM 包尾跨包保留，仅整流收尾补齐一个 DMA 周期。Opus 编码末帧填充按声明内容时长裁掉，避免短句累积时钟偏差；显式 `frames=1` 的包也按带长度头的 batch 解码。跳播/源时间不连续会重新建立 generation 与内容位置。
- 保留 PB 优先级、回压、epoch、取消和完成确认；`pb_started`/`pb_progress` 的 `played_ms` 现在来自已完成 DMA。`output_empty_periods` 统计流激活期间没有新块可装入 DMA 的周期，包括启动和正常收尾；不能直接当作异常欠载次数。`lipsync_mode=rms` 明示当前算法。

## 验证与产物

环境：macOS 主机，PlatformIO 的 `seeed_xiao_esp32s3` 环境，Arduino-ESP32 3.3.9 / ESP-IDF 5.5.4（以项目锁定依赖为准）。运行记录仅支持主机与编译结论。

| 检查 | 实际结果 |
| --- | --- |
| `python3 -m unittest discover -s hardware/tests` | 10 项通过：素材生成、分配失败释放、本地关键帧、表情插值、任意 PCM 分块、静音/小电平/削波、双声道下混、RMS 平滑、实际 DMA 回调代码的输出时序/欠载/取消隔离 |
| 相关服务回归 | 144 项通过；另 3 项旧接口测试失败，见下文 |
| Ruff 检查此次涉及的服务文件和新增测试 | 通过 |
| `python3 hardware/tools/face_assets.py --check` | ROM 与服务清单均一致 |
| 服务 wheel 构建与清单内容检查 | 通过；安装包包含与 ROM 匹配的 `pb/data/face_assets_manifest.json` |
| `hardware/.venv/bin/pio run -d hardware -e seeed_xiao_esp32s3` | 构建成功，产出 `hardware/.pio/build/seeed_xiao_esp32s3/firmware.bin` 和 `firmware.factory.bin` |

最终构建报告静态 RAM 为 85,680 / 327,680 字节（26.1%），Flash 为 2,239,802 / 8,388,608 字节（26.7%）。这是链接器报告，不包括运行时队列、音频解码和 PSRAM 分配，不能替代板上堆/栈检查。构建保留了原有 MCPWM 驱动弃用告警。

相关服务测试覆盖 `test_pcm_lipsync_protocol.py`、`test_builtin_face_ids.py`、`test_pb_wire_format.py`、`test_opus_downlink.py`、`test_pb_continuous.py`、`test_llm_plan.py`、`test_boot_wake.py`、`test_say_tool.py`、`test_llm_stream_tts.py`、`test_chat_streaming_flow.py`、`test_llm_plain_text_final.py`、`test_pb_and_camera.py`、`test_talk_sim.py`、`test_builtin_face_expressions.py`、`test_interaction_feedback.py`。三个失败项位于最后一个文件：

- `test_camera_face_follow_sends_latest_absolute_position` 引用 `camera_servo_follower.get_asr_voice_auto_reply_enabled`，该符号不存在。
- `test_listen_feedback_respects_min_gap` 和 `test_llm_wait_nod_loop_stops_when_done` 引用不存在的 `deskbot_server.auto_reply` 模块。

本次未修改这些旧接口所在实现或旧测试；排除这三个失效引用后，剩余 144 项通过。未宣称整个仓库测试全通过。

## 未执行的板上验收与算法边界

已按下方记录烧录并检查启动；尚未测 ESP32 上 CPU/P99、堆与栈余量、声学音画偏移，也未执行 30 分钟全并发运行。本次不提供虚构的板上结果。原有实测报告属于旧播放实现。

当前只实现 RMS 普通开合，没有安装 MFCC 校准模板，没有 A/I/U/E/O 分类，也没有宣称任意音色免校准识别。更换分类器时可在预计算 `Frame` 和 UI 叠加接口上继续接入。RMS 分析无需为 48 kHz 做频谱降采样。

16 kHz 的 DMA 周期是 10 ms，48 kHz 时约 3.33 ms。当前使用已完成输出块的 RMS，存在一个块级别的自然滞后，加上姿态平滑和实际显示/功放延迟；没有测量前不承诺音画偏移小于 50 ms。

I2S 队列结构已改变。现有 `audio_frontend_esp_sr.cpp` 的 96 ms AEC 参考延迟来自旧缓冲校准，不能作为本实现已校准的证据；该参数没有凭空改成新的“实测值”。烧录后应重新测量参考/麦克风相关延迟，再决定是否调整。

真机复验顺序：

1. 用配套清单部署服务并烧录上述 ROM，检查 hello 含 `anim_frame_id_v1`、`pcm_lipsync_v1` 及匹配资源版本。
2. 分别播放独立 `curious`、连续 `happy/sad` 与携带音频的帧引用，观察跨表情插值、眼部/装饰连续性及嘴部覆盖；查看下行不含绘图数组。
3. 播放静音、不同电平、不同音色及 16/48 kHz PCM、短 Opus，观察输出期间才开合；反复欠载、恢复、取消与新话轮，确认无尾部旧音频、旧口型或永久延迟积累。
4. 测量音画和 AEC 偏移，在收音、Wi-Fi、屏幕、电机并发下记录欠载、任务栈、堆、CPU 与耗时；持续至少 30 分钟，再评估上线。


## 追加烧录记录（2026-11-11）

已通过 `/dev/tty.usbmodem21301` 将当前 ROM 烧录到 ESP32-S3。PlatformIO upload 成功，写入哈希校验通过，并通过 RTS 重启。串口观察到 Wi-Fi 已连接，设备响应 `face_version`，返回 `4b21b918083d28d3`，与服务资源清单一致；临时 INFO 日志查询后恢复 WARN。捕获的启动日志未出现 panic/回溯。这仅证明烧录、启动和版本查询成功，尚未执行播放口型、音画偏移和 AEC 实测。

固件摘要和烧录证据见 [烧录记录](pcm-lipsync-flash-2026-11-11.json)。


## USB 自动实测与修复（2026-11-11）

已按用户要求实测并烧录修复。新增 `hardware/usb_test.sh` 一键 USB 入口；设备当前保存为 USB 测试模式，重启跳过 Wi-Fi 和摄像头初始化。正常联网恢复命令为 `./hardware/usb_test.sh mode wifi`。使用说明见 [USB 快速实测](../../hardware/USB_AUDIO_TEST.md)，完整数据见 [自动实测报告](usb-audio-bench-2026-11-11.json)。本节更新了前文“尚未实测”的状态；前文保留为开发阶段历史。

实测发现并修复了 USB trace 受 Wi-Fi 门控、默认串口缓冲丢输入、半包 TX 破坏后续确认、诊断录音挤占 ACK、48kHz 文本编码开销导致供数不足，以及嘴部更新仍重画整脸的问题。USB 改为 PB 二进制直传、固定缓冲批量收发、控制确认优先、保留半包写入；回传诊断副本先低通后降为 8kHz，实际播放与 AFE 采样率不变；嘴部改为独立裁剪画布。

最终复核：16/48kHz、取消、26.31 秒连续语音均完成，录音序列缺口/校验错误和 USB TX 丢弃为 0，播放时钟中位比率 1.0，未检出大于 2ms 的供数停顿。源 PCM 与设备 RMS 的 P95 绝对误差均小于 5e-6。有声帧 DMA 完成到推屏的 P95：16kHz 约 19.2ms，48kHz 约 27.5ms；这不是高速摄影得到的可见像素时延。

旧 96ms AEC 参数使参考过晚；按板上噪声扫参，将默认值修正为 32ms。最终相同语音在捕获第 4–8 秒、按原始麦克风电平归一化后，AEC 开启相对关闭的输出链路改善约 13.0dB；前一轮已收敛状态测得约 49.5dB，不能把它作为每次启动的保证。NS/AGC 均仍开启，所以这不是纯 AEC ERLE。真人双讲按用户选择暂缓；电脑近端样本没有被板载麦克风可靠采到，不标记双讲通过。

14 项固件/USB 主机测试通过，最终固件构建、烧录校验通过；一键入口也已实际执行成功。尚未执行光学同步、真人双讲、30 分钟长稳和 Wi-Fi/摄像头/电机全并发验收。

## ROM 口型能力收敛（2026-11-11 后续）

自动口型始终在设备的共用播放／显示路径执行，USB 工具只供音频和采集诊断。本次将动画平滑和话轮状态收拢至 `audio_lipsync::process_animation`，与 `process_audio` 同属 ROM 纯计算模块；显示任务调用该模块，再使用本地或已下载的嘴部素材绘制。音频结束、取消与欠载清除开合状态，新 generation 不继承上个话轮的开口或空闲计时。普通 PCM 静音继续使用 70 ms 收口过渡，表达式图层插值不变。

本次修复前，欠载虽已经输出零开口目标，显示仍会沿用 70 ms 平滑收口；修复后 DMA 明确标记 `underrun`，显示在下一次嘴部刷新时直接归零，并清除该次过渡历史。此变更不改变 PB 下行协议，也不需要服务端提供口型标签。

21 项硬件原生／主机检查通过，新 ROM 编译通过。当前未检测到 ESP32 USB 串口，因此本次产物**未烧录、未执行新版本板上验收**；不能把此前版本的真机数据当成本次版本实测。固件快照和摘要见 [ROM 构建记录](pcm-lipsync-rom-2026-11-11.json)。先前真人双讲及普通摄像头粗测的补充结果见 [USB 实测记录](usb-audio-bench-2026-11-11.json) 的 `continuation`。
