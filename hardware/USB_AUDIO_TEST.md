# USB 音频、口型和 AEC 快速实测

适用于本仓库 ESP32-S3 ROM。当前设备已经烧录支持 USB 二进制测试的固件，并启用 USB 模式。USB 模式在约 3.3 秒内启动，跳过 Wi-Fi 配网、连接服务器和摄像头初始化；使用同一套 PB 解析、扬声器、表情插值、PCM 口型和 ESP-SR AEC。

## 日常入口

在仓库根目录插上 USB 后执行：

```bash
# 检查设备；自动选择唯一 ESP32 USB 串口
./hardware/usb_test.sh status

# 默认约 7 秒静音/音量阶跃，自动保存报告
./hardware/usb_test.sh

# 48kHz 播放、取消、AEC 噪声测试
./hardware/usb_test.sh run --rate 48000
./hardware/usb_test.sh run --cancel-at 2
./hardware/usb_test.sh run --kind noise --delay 32

# 中文语音引导的真人双讲（macOS，默认音量请显式调到本次实测值）
./hardware/usb_test.sh run --kind double-talk --volume 50
```

脚本使用已有的 `service/.venv/bin/python`，依赖 numpy 和 pyserial。多个设备时用 `./hardware/usb_test.sh --port /dev/tty.usbmodem21301 run` 显式选择。

切换模式：

```bash
# 从正常固件切到 USB 模式，保存选择并重启
./hardware/usb_test.sh mode usb

# 测试完恢复正常 Wi-Fi / 服务端功能，保存选择并重启
./hardware/usb_test.sh mode wifi
```

首次从联网模式切换时需要等当前启动流程能够处理命令；此后重启直接进 USB 测试。模式仅存于 NVS 的 `usbbench/enabled`，不清除 Wi-Fi 凭证。USB 模式期间不启动联网、摄像头或服务端对话；需要对话时恢复 `mode wifi`。

如果更换或回退固件，仍沿用 `./hardware/flash_rom.sh upload <串口>`。USB 工具要求握手 `usb_protocol=2`；资源版本也要与本地清单匹配。

## 用真实语音对照 AEC

输入文件要求单声道、16000 Hz、PCM16 WAV。使用相同文件、相同音量比较开关，测试期间保持环境安静：

```bash
./hardware/usb_test.sh run --wav /path/to/test.wav --aec off
./hardware/usb_test.sh run --wav /path/to/test.wav --aec on
```

`--volume` 默认 35（0–100），`--delay` 默认 32 ms。参考延迟只在空闲时修改；AEC 开关仅为 USB 诊断，在 AFE feed 任务帧边界生效。恢复联网或重启后 AEC 默认开启。自动采集结束会停止 trace，保留实际音频处理状态；使用 `--aec off` 后，可以再运行一次默认测试恢复开启。

`--rate 48000` 将 16kHz 测试源按采样保持扩展到 48kHz，用来检验真实 48kHz 播放节奏、时间轴和口型，不评估高频音质。日常测试不需要 TTS、ASR 或服务器。

## 报告与判读

默认输出到 `hardware/.pio/usb-bench/manual/<时间>/`，可用 `--output <目录>` 指定：

- `source.wav`：实际发送的测试源。
- `raw.wav`：板载麦克风诊断副本。
- `processed.wav`：实际 AFE/NS/AGC 路径的输出副本。
- `events.json`：PB 确认、DMA/嘴形/推屏时间、AEC 统计。
- `metrics.json`：统计结果与最终设备状态。

诊断回传为 8kHz 双路 PCM，先经过 32 抽头低通再抽取；32 抽头滤波群延迟约 0.97 ms。播放和 ESP-SR 输入/处理采样率没有因回传改变。旧 `DBAUD001` 全速 trace 仍可解析，USB 使用 `DBAUD002` 低带宽版本。

先确认 `trace_integrity_ok=true`，即采集非空、`trace_sequence_gaps=0`、`trace_bad_checksums=0` 和 `tx_drops_delta=0`。`final_status.tx_drops` 是本次开机以来的累计值；电脑没有打开串口读取时也可能增加，不能因此否定后续完整的采集。报告保存 `tx_drops_start` 和本次增量，计数器按 32 位回绕计算。`afe_processed_fraction` 表示采集中真正采用处理输出的比例，不能只看 AFE 初始化成功。

中断或播放失败时，工具会尽力取消当前请求、停止 trace，并保存 `failed-events.json`、日志及已收到的音频；设备断开时仍需要重新连接才能确认其最终状态。

`reported_rms_absolute_error_p95` 将实际输出嘴形 RMS 与源 PCM 在相同采样位置、相同 Q15 音量下比较；不使用 TTS 音素。`output_clock_ms_per_content_ms` 使用去重输出点的间隔中位数，`content_wall_stalls_ms` 独立报告供数停顿，不能把停顿误称为采样时钟漂移。

`voiced_dma_to_panel_push_ms` 是有声帧的 DMA 完成到屏幕数据推送完成的间隔，包含 MCU 调度与绘制。`microphone_envelope_vs_panel_push` 对比板载麦克风捕获的声音包络和推送的嘴部开合；正 lag 表示麦克风包络在画面之后。麦克风时间是 PDM 读完成时刻，屏幕时间是推送完成时刻，因此这是可重复的板载音画代理测量，不能替代高速摄像机/光电传感器的真正可见像素—声学测量。

AEC 对照应比较相同声音稳定播放段的处理输出，并归一化原始麦克风电平。输出还包含 NS、AGC 和线性增益，不能把简单 `raw_rms/processed_rms` 当成纯 AEC ERLE；也不能只因输出变小就宣称双讲人声被保留。

`raw_mean_pcm` / `processed_mean_pcm` 帮助发现直流偏置；`raw_speech_band_rms` / `processed_speech_band_rms` 单独计算 120–3400 Hz 的 RMS，排除直流及慢漂移。完整频段 RMS、语音频段能量都不能单独证明人声可懂。

## 真人双讲复测

执行 `run --kind double-talk --volume 50` 后，离麦克风约 20 厘米，每次听到“开始”就反复说“一二三四五六七八九十，今天天气很好。”第一轮是 8 秒安静说话，第二轮是 8 秒噪声播放与说话同时进行，最后机器人会说“测试结束”。提示在本机通过 macOS 自带 `say` 的 Tingting 中文语音和 `afconvert` 生成；没有生成有效音频时工具报错，不继续静默测试。该功能不需要联网或启动服务；其他系统可用 `--wav` 输入预制引导音频。

报告额外保存 `phases.json`，记录测试源中各阶段的准确起止时间。麦克风文件包含采集前后留白，AFE 输出还有处理延迟；截取阶段时先用实际播放参考与原始麦克风对齐，再校准处理输出延迟，不能把源文件秒数直接当成麦克风文件秒数。双讲应以听辨或本机识别确认内容为准；仅完成播放不算通过。

## 2026-11-11 补充实测

- 真人按固定句子完成两轮后，本机 SenseVoice 从安静和双讲的处理后音频中均识别出数字序列及“今天天气很好”。本次双讲条件下人声得以保留，输出没有削波，1583 帧采集无丢帧、校验错误或新增 USB 丢弃。它不代表所有距离、音量和房间条件均通过。此前没有实际说话的一轮作废。
- 电脑摄像头 30 fps 同时拍屏和录音，直接提取可见嘴部高宽比与声音包络。按录制音频包的真实 PTS 对齐，相关峰约为画面落后声音 51 ms；10 个开闭口边缘的局部中点估计中位数约 45 ms。普通摄像头一帧约 33 ms，未标定摄像头和麦克风固有延迟，因此结果是粗测，不是精确到毫秒的验收值。
- 电脑录音包累计有约 1.60 秒时间空洞，单次最大约 11.4 ms。分析保留每个包的真实时间位置，没有将缺失时段拼接压缩。USB 设备侧采集完整；不能把电脑采集空洞当成设备供数停顿。
- 主机回归检查 17 项通过；中文引导音源的两段 8 秒窗口另做了实际生成检查。当前保持 USB 模式、AEC 开启、参考延迟 32 ms。

## USB 数据与任务边界

下行格式是 `DBPB0001` + 四字节大端包长 + 原有 PB packed bytes（四字节大端 JSON 长度 + JSON + PCM），最大 64 KiB。控制命令仍可用换行文本。直接复用 `pb_runtime_enqueue_frame`，保留 epoch、队列回压、取消和完成确认。

USB 接收任务批量读数据，使用固定 PSRAM 接收缓冲，不在 loop 中解码音频；确认消息走独立优先队列。TX 保留未完成的 USB 写入，不能丢弃半个二进制包。麦克风采集与显示任务只非阻塞提交诊断数据，不等待电脑读取。

本次实测结果、修复前后对照和边界见 [USB 真机结果](../service/docs/usb-audio-bench-2026-11-11.json)。原始 WAV/事件保存在本机 `.pio/usb-bench/`。
