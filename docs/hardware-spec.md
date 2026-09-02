# Deskbot 硬件规格文档

> 产品名称：Deskbot（内部）/ Brufik（公开仓库）
> 固件版本：0.0.5

---

## 1. 主控平台

| 项目 | 规格 |
|------|------|
| 开发板 | **Seeed XIAO ESP32S3 Sense** → 自研 **Deskbot v2 主控板（PCB V2.0 Base，ESP32-S3-WROOM-1U-N16R8）**（引脚以 [`firmware/deskbot_config.h`](../hardware/firmware/deskbot_config.h) 为准；以下引脚均为 **PCB V2.0（Base）** 实际 GPIO，丝印即 GPIO 号，早期 XIAO 参考设计已废弃） |
| MCU | ESP32-S3（Xtensa 双核 LX7） |
| 框架 | Arduino-ESP32 3.3.9 + ESP-IDF 5.5.4 |
| PlatformIO 平台 | pioarduino 55.03.9 |
| PlatformIO 环境名 | `seeed_xiao_esp32s3` |
| Flash 容量 | 8MB |
| PSRAM | 已启用（CONFIG_SPIRAM=y） |
| WiFi | 2.4GHz 802.11 b/g/n（板载） |
| 串口波特率 | 115200（USB CDC） |

### Flash 分区表

| 分区 | 类型 | 偏移 | 大小 |
|------|------|------|------|
| nvs | data/nvs | 0x9000 | 20KB |
| otadata | data/ota | 0xe000 | 8KB |
| app0 | app/factory | 0x10000 | ~7MB |
| ffat | data/fat | 0x700000 | 1MB |

---

## 2. 显示屏

| 项目 | 规格 |
|------|------|
| 型号 | **Waveshare 1.83" LCD Rev2** |
| 驱动 IC | ST7789P（Adafruit_ST7789 驱动） |
| 原始分辨率 | 240 × 284 像素 |
| 显示分辨率 | **284 × 240** 像素（硬件旋转 3，横屏模式） |
| 色深 | RGB565（16-bit） |
| 接口 | SPI Mode 3，80MHz |
| 行偏移 | 36（DESKBOT_DISPLAY_ROW_OFFSET） |
| 列偏移 | 0（DESKBOT_DISPLAY_COL_OFFSET） |
| 亮度 | 最大（寄存器 0x51=255，0x53=0x2C） |
| RST/BL | 硬接 3.3V，不由 MCU 控制 |
| 顶部安全区 | 4 像素 |
| 画布内存 | PSRAM 分配，~133KB |
| 字体 | WenQuanYi 12px 位图字体（GB2312 子集，4400+ 字形） |
| 渲染层 | 6 层矢量动画（bg、nose、mouth、eye_l、eye_r、extra） |

### SPI 引脚分配

| 信号 | GPIO（v2 板） | 备注 |
|------|--------------|------|
| MOSI | 5 | — |
| SCK | 4 | — |
| CS | 6 | — |
| DC | 7 | — |
| RST | -- | 硬接 3.3V |
| BL | -- | 硬接 3.3V |

---

## 3. 摄像头

| 项目 | 规格 |
|------|------|
| 传感器 | **OV2640 / OV3660**（esp_camera 上电自动识别；v2.0 结构件配 **OV3660** 模组，引脚通用） |
| 镜头 | v2.0：**异面**，120° 广角，**40mm** 长度（旧 XIAO 版为同面 25mm） |
| 接口 | 8-bit 并行 + SCCB（I2C）控制 |
| XCLK 频率 | 10MHz（20MHz 会导致 DMA 损坏/绿屏） |
| 帧尺寸 | VGA 640×480（曾用 QVGA 320×240，画质差；OV3660 驱动上限远高于此） |
| 像素格式 | 传感器内 JPEG（硬件 JPEG） |
| JPEG 质量 | 8（0-63，越低越好） |
| 最大 JPEG 大小 | 256KB/帧（VGA+质量 8 最坏场景约 180KB） |
| 传感器锐化 | +2（-3..3 边缘增强，0=默认）；denoise 关闭（抹细节） |
| 帧缓冲 | 2 个缓冲区，位于 PSRAM |
| 抓取模式 | CAMERA_GRAB_WHEN_EMPTY |
| 默认帧率 | 1 FPS（1000ms 间隔，可服务端动态调整） |
| 预热 | 初始化后丢弃 30 帧（AWB/AGC 收敛） |
| 任务栈 | 16KB，优先级 3，绑定 APP_CPU_NUM |

### 摄像头引脚分配

| 信号 | GPIO |
|------|------|
| XCLK | 14 |
| PCLK | 48 |
| VSYNC | 11 |
| HREF | 12 |
| SIOD (SDA) | 9 |
| SIOC (SCL) | 10 |
| Y2 (D0) | 39 |
| Y3 (D1) | 18 |
| Y4 (D2) | 8 |
| Y5 (D3) | 17 |
| Y6 (D4) | 38 |
| Y7 (D5) | 47 |
| Y8 (D6) | 21 |
| Y9 (D7) | 13 |
| PWDN | -1（未使用） |
| RESET | -1（未使用） |

> 引脚以 `firmware/camera.cpp` 顶部宏为准（PCB V2.0 Base 实际布线）；上表原为 v1/XIAO 转接板旧值，勿再参考。

### 摄像头服务端配置

| 参数 | 默认值 | 范围 |
|------|--------|------|
| frame_width | 640 | 160-640 |
| frame_height | 480 | 120-480 |
| horizontal_fov_deg | 120 | 30-170 |
| eye_yaw_range_deg | 50 | 10-90 |
| frontal_angle_threshold_deg | 15 | 1-90 |
| min_face_detection_confidence | 0.15 | - |
| num_faces | 5 | 1-10 |
| face_track_max_dist_px | 90 | 16-240 |
| face_track_max_lost_frames | 18 | 1-60 |
| face_embedding_enabled | true | bool |
| identity_similarity_threshold | 0.40 | 0.25-0.99 |

---

## 4. 舵机

| 项目 | X 轴（水平/左右） | Y 轴（垂直/上下） |
|------|-------------------|-------------------|
| 类型 | 3.7g 微型舵机（5V，20.2×8.5mm，JR2.54） | SG90 / 9g 舵机（JR2.54） |
| GPIO | 16 | 15 |
| 角度范围 | 0° - 180° | 70° - 110°（限位行程） |
| 中心位置 | 90° | 90° |
| 脉冲范围 | 1000μs - 2000μs | 1000μs - 2000μs |
| PWM 频率 | 50Hz（20ms 周期） | 50Hz（20ms 周期） |

- 驱动库：ESP32Servo（基于 MCPWM）
- 运动控制：时间预算线性插值，每 tick 更新
- 命令模式：绝对(0)、相对(1)、保持(2)
- 命令队列深度：50
- 任务栈：8KB，优先级 3，绑定 APP_CPU_NUM

### 舵机服务端配置

| 参数 | 默认值 |
|------|--------|
| xMin | 0 |
| xMax | 180 |
| yMin | 70 |
| yMax | 110 |
| xReverse | 0 |
| yReverse | 0 |
| 视角模式 | viewer（观看者视角） |

---

## 5. 音频

### 扬声器（播放）

| 项目 | 规格 |
|------|------|
| 功放芯片 | **NS4168**（I2S 输入 D 类功放，v2 主控板板载，替代原 MAX98357A） |
| 扬声器 | 2011 型，8Ω 1W 腔体喇叭（1.25 插头） |
| I2S 总线 | I2S_NUM_1，STD 模式 |
| 采样率 | **16000Hz 统一下发**（moss 引擎 48k 由服务端降采样；豆包直连 16k；设备端"整块解码→播放"结构下 24k/48k 会进入解码预算边缘带） |
| 位深 | 16-bit |
| 声道 | 单声道（支持 WAV 立体声回放） |
| DMA 缓冲 | 6 × 240 帧（ESP_I2S 驱动固定，≈5.8KB；解码突发需小于其覆盖时长） |
| 默认音量 | 85%（范围 0-100） |
| 编解码 | PCM S16LE、Opus（下行解码） |
| 可听阈值 | mean_abs × volume ≥ 16 |
| 任务栈 | 28KB，优先级 7，绑定 APP_CPU_NUM |

### 麦克风（录音）

| 项目 | 规格 |
|------|------|
| 类型 | 板载 PDM 麦克风（v2 主控板板载，聆麦 LMD2718T271；原 Sense 扩展板方案已废弃） |
| I2S 总线 | I2S_NUM_0，PDM_RX 模式 |
| 采样率 | 16000Hz |
| 位深 | 16-bit |
| 声道 | 单声道 |
| 帧大小 | 320 样本 = 20ms @ 16kHz |
| 编码 | Opus，VOIP 模式，复杂度 0，码率 24kbps |
| 语音增强 | 高通滤波器（α=0.969）+ 5× 增益 |
| VAD | 能量门限 + EMA，触发比 1.3×，底噪阈值 140 |
| 上行批量 | 5 个 Opus 帧/WS 消息 |
| 最大连续上行 | 30 秒/段 |
| 任务栈 | 28KB，优先级 6，绑定 APP_CPU_NUM |

### 音频引脚分配

**扬声器 I2S（v2 板）：**

| 信号 | GPIO | 备注 |
|------|------|------|
| DIN | 40 | — |
| BCLK | 41 | — |
| LRC | 42 | — |
| SD (=AMP_CTRL) | 45 | 高电平使能（strapping 引脚，boot 后才可拉高） |
| GAIN | NC | 未连接 |

**麦克风 PDM（v2 板）：**

| 信号 | GPIO |
|------|------|
| CLK | 1 |
| DATA | 2 |

### VAD（语音活动检测）配置

| 参数 | 值 |
|------|-----|
| mode | 3 |
| frame_ms | 30 |
| min_speech_ms | 250 |
| max_silence_ms | 500 |
| pre_speech_ms | 300 |
| silero_threshold | 0.5 |
| silero_threshold_low | 0.2 |

---

## 6. XIAO ESP32S3 Pad-GPIO 映射（仅历史参考）

> 旧 XIAO 转接板方案使用，**已废弃**；v2 主控板丝印直接标 GPIO 号。

```
D0  = GPIO1    D1  = GPIO2    D2  = GPIO3    D3  = GPIO4
D4  = GPIO5    D5  = GPIO6    D6  = GPIO43   D7  = GPIO44
D8  = GPIO7    D9  = GPIO8    D10 = GPIO9
```

---

## 7. 电源要求

| 模块 | 电压 | 备注 |
|------|------|------|
| MCU + 显示屏 + 摄像头 | 3.3V | v2 主控板板载稳压，TYPE-C 5V 供电 |
| 舵机 | **5V ≥ 1A** | 独立供电，与逻辑共地 |
| NS4168 功放 | 5V（TYPE-C VBUS） | v2 主控板板载（D 类功放，替代原 MAX98357A） |
| USB-C 接口 | 5V | 主控 PCB V2.0（Base）上的 TYPE-C 16P（LCSC C2765186），CC1/CC2 5.1k 下拉电阻 |

**建议总供电：5V ≥ 1A**（舵机为主要电流消耗）

---

## 8. 通信协议总览

| 协议 | 用途 | 详情 |
|------|------|------|
| SPI | 显示屏驱动 | Mode 3，80MHz |
| I2S (STD) | 扬声器输出 | I2S_NUM_1，16kHz 16-bit 单声道 |
| I2S (PDM_RX) | 麦克风采集 | I2S_NUM_0，16kHz 16-bit PDM |
| SCCB/I2C | 摄像头控制 | SIOD=GPIO9, SIOC=GPIO10 |
| 8-bit 并行 | 摄像头数据 | D0-D7 + VSYNC/HREF/PCLK |
| PWM (MCPWM) | 舵机控制 | 50Hz, 1000-2000μs 脉冲 |
| WiFi | 网络连接 | STA 模式（正常）/ AP 模式（配网） |
| WebSocket | 云端通信 | ws:// 或 wss://，路径 /asr_chat |
| HTTP | 配网门户 | AP 模式下 WebServer 192.168.4.1 |
| USB CDC | 串口调试 | 115200 baud |

### WebSocket 协议细节

- 端点：`ws://<host>:<port>/asr_chat?device_id=<mac>`（仅 `device_id` 鉴权，无 API Key / PIN）
- 最大消息大小：1MB
- TCP 超时：15s，写超时：5s
- 线格式：`u32be(json_len) + json_utf8 + optional_binary`
- 服务端端口：9000
- Ping 间隔：120s，Ping 超时：300s

---

## 9. 音频处理（全双工）

- 音频前端集成 **ESP-SR AFE**（`audio_frontend_esp_sr.cpp`）：AEC 回声消除 / NS 降噪 / AGC / VAD
- **全双工**：已移除 speaker/mic 半双工互斥，播放时麦克风照常采集，机器人可以边听边说、随时被打断
- 上行统一 16kHz（`SAMPLE_RATE`），下行 TTS 音频同样统一 16kHz（config.yaml `tts.sample_rate`）；切句在服务端 Silero VAD

---

## 10. 关键注意事项

### ⚡ 电源
1. **舵机必须独立 5V 供电（≥1A）**，不可直接从 XIAO 板取电，否则会导致 MCU 复位或工作不稳定
2. 舵机电源与逻辑电源必须**共地**
3. USB-C 接口需确保 CC1/CC2 5.1k 下拉电阻焊接正确，否则无法被主机识别

### 🔌 引脚冲突
1. 舵机避开 USB 19/20 与 strapping 引脚：当前固件 X（左右）→ **GPIO16**（3.7g 小舵机）、Y（上下）→ **GPIO15**（9g 大舵机）
2. **PCB V2.0（Base）** 已按上述 GPIO 直接布线，按丝印焊接即可；早期 XIAO 转接板方案（舵机曾连到 D6/D7 UART0）已废弃
3. 板载 LED 引脚与音频/摄像头冲突，**不可用**

### 📷 摄像头
1. XCLK 必须为 **10MHz**，20MHz 会导致 ESP32-S3 上 DMA 损坏（绿屏）
2. 初始化后需丢弃 **30 帧**预热画面（AWB/AGC 收敛）
3. 120° 广角镜头会产生桶形畸变，服务端可配置去畸变（默认关闭）

### 🔊 音频
1. 音频前端含 **AEC / NS / AGC / VAD**（ESP-SR AFE），全双工采集，播放时不静音麦克风
2. AGC 压缩增益默认 12dB（`DESKBOT_AFE_AGC_COMPRESSION_GAIN_DB`），避免 AEC 残留抬过 VAD 阈值
3. Opus 编码复杂度设为 0（最低），优先保证实时性

### 🖥️ 显示屏
1. RST 和 BL 引脚**硬接 3.3V**，固件无法控制复位和背光
2. 横屏旋转后 X 起始位置需偏移 -18（DESKBOT_DISPLAY_ROT3_XSTART_ADJ）
3. 顶部有 4px 安全区，绘制内容应避开

### 📡 通信
1. WebSocket 最大帧 1MB，单帧 JPEG 最大 32KB
2. 下行 PB 帧最大 64KB JSON + 320KB 二进制（10s @ 16kHz PCM，统一下发采样率）
3. PB 帧间需 50ms 间隔，PB 块间需 150ms 间隔
4. WiFi 默认 SSID：`deskbot_wifi`，密码：`hello2026`

### 🔧 调试
1. USB CDC 串口波特率 115200
2. 配网模式：AP 热点，WebServer 地址 192.168.4.1
3. 固件支持命令行交互（通过串口）
