# Brufik

[English](README.md)

**本仓库中的硬件设计部分遵循 [CERN-OHL-S-2.0](mechanical/LICENSE)，软件部分遵循 [GPL-3.0](firmware/LICENSE)。**

**Brufik** 是一款开源桌面机器人：自研 **Deskbot v2 主控板**（ESP32-S3-WROOM-1U-N16R8）+ 屏 + 舵机 + 麦 + 喇叭。语音与画面走本 monorepo 后台 [`../service/`](../service/)。

![Brufik 桌面机器人 — 组装完成实物](mechanical/poster.jpg)

---

## 工具链

| 项 | 说明 |
|------|------|
| 板型 | 自研 **Deskbot v2 主控板**（PCB V2.0 Base，ESP32-S3-WROOM-1U-N16R8） |
| Platform | [pioarduino](https://github.com/pioarduino/platform-espressif32) **55.03.39**（**不要**用官方 PlatformIO `espressif32` 的 Arduino 2.0.17 / IDF 4.4.7） |
| 框架 | Arduino-ESP32 **3.3.9** + ESP-IDF **5.5.4** |
| 宿主机 Python | **≥3.10**（推荐 `cd hardware && python3.11 -m venv .venv && .venv/bin/pip install platformio`） |
| 烧录 | [`./flash_rom.sh`](flash_rom.sh)（优先 `.venv/bin/pio`；串口含 `/dev/tty.usbmodem*`） |

音频走 Arduino **`ESP_I2S`**（PDM 收音 + STD 功放）。相机优先硬件 JPEG，否则 RGB565 + `frame2jpg`（init 后丢弃若干帧做 AWB）。`device_id` 由 `esp_read_mac` 生成（`deskbot_<mac>`）。

---

## 一、开箱即用

1. 编辑 [`firmware/deskbot_config.h`](firmware/deskbot_config.h)：把 `WIFI_DEFAULT_*` 改成你家路由，或把热点改成与宏一致；并设置 `DESKBOT_WS_HOST` / `DESKBOT_WS_PORT`。
2. 上电后连 WiFi，并连接 `ws://…/asr_chat?device_id=…`（**设备链路仅需 device_id，无 API Key / PIN**）。
3. 改家里 WiFi：上电后连接开放热点，SSID 为 **`device_id`**（如 `deskbot_e83dc1faea30`），浏览器打开屏幕地址（通常 **`http://192.168.4.1/`**）按 onboarding 保存。

---

## 二、本地开发者

**需要：** USB、PlatformIO（Python ≥3.10）、串口权限（Linux `dialout`）。

```bash
# 在 monorepo 的 hardware/ 目录
./flash_rom.sh all          # 编译 + 烧录 + 监视
./flash_rom.sh build
./flash_rom.sh upload [端口]
./flash_rom.sh log [端口]
```

| 命令 | 说明 |
|------|------|
| `./flash_rom.sh build` | 编译 |
| `./flash_rom.sh upload [端口]` | 烧录 |
| `./flash_rom.sh log [端口]` | 串口监视 |
| `./flash_rom.sh all [端口]` | 烧录 + 监视 |

后台见 [`../service/`](../service/)。固件 WebSocket：**`/asr_chat`**。摄像头 JPEG 经同一 WS 上行（`camera_frame`）；STA 正常运行后**没有**常驻本机摄像头网页（仅配网 AP 门户）。服务端调试预览：`/camera_view`。

Arduino 3.x 的 WiFi write timeout 补丁优先改 `NetworkClient.cpp`（见 `scripts/`）。

---

## 三、自行组装

### 元器件与采购关键词

| 部件 | 说明 | 搜索关键词（淘宝/嘉立创等） |
|------|------|---------------------------|
| 主控 PCB | **PCB V2.0 Base** 自研主控板：ESP32-S3-WROOM-1U-N16R8 + **板载麦克风** + 功放（NS4168）+ TYPE-C；[BOM](mechanical/BOM_V2.0_Base_PCB_V2.0_Base_2026-09-02.xlsx)、[Gerber](mechanical/小歪_Gerber_PCB_V2.0_Base_2026-09-02.zip) 见仓库 | 嘉立创下单 |
| 屏幕 PCB | **PCB V2.0 LCM** 屏幕转接板，FPC 与主控相连；[BOM](mechanical/BOM_V2.0_LCM_PCB_V2.0_LCM_2026-09-02.xlsx) 见仓库 | 嘉立创下单 |
| 摄像头 | **OV3660**；**异面**（非同面）；广角 **120°**；**40 mm** 长。固件 esp_camera 上电自动识别传感器，OV2640 / OV3660 引脚通用 | `OV3660 摄像头模组`、`ov3660 异面 120度 40mm` |
| 屏幕 | 1.83 寸 SPI，**ST7789** 240×284，圆角，12p 插接、无触摸 | `微雪 1.83寸 LCD`、`Waveshare 1.83 LCD Rev2`、`ST7789 240x284` |
| 舵机 | 一大一小：**9g（俯仰）+ 3.7g（水平）**，均 **JR2.54 插口**；3.7g 工作电压需 **5V**，尺寸 20.2×8.5mm（±0.5mm） | `SG90` / `9g 舵机`；`3.7g 舵机 微型` |
| 喇叭 | **2011** 腔体喇叭，8Ω 1W，1.25 插头 | `喇叭 2011`、`8Ω 2011 扬声器` |
| FPC 天线 | IPEX 1 代接头，线长 5cm，尺寸 28×9mm（≤30×30mm） | `FPC天线 IPEX 1代 5cm` |
| 电源 | **5V ≥1A** 给舵机；主控板 TYPE-C 供电 | `5V 1A 电源`、`USB 5V` |

### 购买材料清单（每套）

| 类别 | 品名 | 规格 | 每套用量 | 说明 |
|------|------|------|----------|------|
| 硬件 | 屏幕 | 1.83英寸TFT液晶屏ST7789小屏240x284显示器LCD彩屏SPI圆角 | 1 | 12p插接 无触摸 |
| 硬件 | 摄像头 | ov3660异面120°广角40mm长 | 1 | — |
| 硬件 | 扬声器 | 2011腔体喇叭8欧1瓦 | 1 | 1.25插头 |
| 硬件 | 9g舵机 | 180°舵机 | 1 | JR2.54插口 |
| 硬件 | 3.7g舵机 | 长宽尺寸20.2*8.5mm | 1 | 1、工作电压必须满足5v；2、长宽20.2*8.5mm（允许±0.5mm公差，一般都是这种规格）；3、JR2.54插口 |
| 硬件 | FPC天线 | FPC天线 IPEX 1代接头 线长5cm 尺寸28*9mm | 1 | 尺寸越大信号越好，但是不得大于30*30mm |
| 硬件 | 屏幕PCB | 详见 PCB sheet | 1 | 即 **PCB V2.0 LCM** |
| 硬件 | 主控PCB | 详见 PCB sheet | 1 | 即 **PCB V2.0 Base** |
| 辅料 | M2*5自攻螺丝 | 十字盘头不锈钢平尾 | 14 | — |
| 辅料 | M2*8自攻螺丝 | 十字盘头不锈钢平尾 | 9 | — |
| 辅料 | M2*12自攻螺丝 | 十字盘头不锈钢平尾 | 2 | — |
| 辅料 | 连接线 | 1.25 6p 150mm 双头同向硅胶线 | 1 | 双头同向，见[示意图](mechanical/连接线_双向同头示意图.png) |

### 组装说明与参考图

- **图文说明书：** [`mechanical/小歪v2.0组装说明书PDF.pdf`](mechanical/小歪v2.0组装说明书PDF.pdf)
- **3D 打印件（整机 STP）：** [`mechanical/小歪机器人v2.0打印件stp.stp`](mechanical/小歪机器人v2.0打印件stp.stp)
- **摄像头卡子（OV3660 固定用）：** [`mechanical/小歪机器人v2.0 ov3660摄像头卡子.stp`](<mechanical/小歪机器人v2.0 ov3660摄像头卡子.stp>)

**主控 PCB（PCB V2.0 Base）：**

![PCB V2.0 Base 主控板](mechanical/PCB_PCB_V2.0_Base_2026-09-02.png)

**屏幕 PCB（PCB V2.0 LCM）：**

![PCB V2.0 LCM 屏幕板](mechanical/PCB_PCB_V2.0_LCM_2026-09-02.png)

### 接线（PCB V2.0 主控板，丝印即 GPIO 号）

> v2.0 主控板丝印直接标 **GPIO 号**；旧 XIAO 图纸上的 IO8 / IO3 指 GPIO，不是丝印 D8 / D3。

| 外设 | 信号 | GPIO | 备注 |
|------|------|------|------|
| **LCD** | MOSI / SCK / CS / DC | **GPIO5 / 4 / 6 / 7** | SPI；RST/BL 硬接 3.3V |
| **舵机 左右 (X)** | PWM | **GPIO16** | 3.7g 小舵机；避开 USB 19/20 与 strapping 引脚 |
| **舵机 上下 (Y)** | PWM | **GPIO15** | 9g 大舵机 |
| **功放 NS4168** | DIN / BCLK / LRC | **GPIO40 / 41 / 42** | I2S → 2011 喇叭；SD=GPIO45 高电平使能 |
| **麦克风** | PDM CLK / DATA | **GPIO1 / GPIO2** | 主控板板载（聆麦 LMD2718T271） |
| **舵机电源** | 5V / GND | 独立 5V≥1A | 与逻辑共地 |

引脚宏见 [`firmware/deskbot_config.h`](firmware/deskbot_config.h)；摄像头 8-bit 并口 + SCCB 引脚见 [`firmware/camera.cpp`](firmware/camera.cpp)。

---

## 许可证

| 范围 | 协议 | 文件 |
|------|------|------|
| 硬件设计（[`mechanical/`](mechanical/)） | CERN-OHL-S-2.0 | [`mechanical/LICENSE`](mechanical/LICENSE) |
| 软件（[`firmware/`](firmware/) 等） | GNU GPL v3.0 | [`firmware/LICENSE`](firmware/LICENSE) |
