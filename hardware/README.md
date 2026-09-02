# Brufik

[中文](README_zh.md) | English

**Hardware in this repository is under [CERN-OHL-S-2.0](mechanical/LICENSE); software is under [GPL-3.0](firmware/LICENSE).**

**Brufik** is an open-source deskbot built on a custom **Deskbot v2 mainboard** (ESP32-S3-WROOM-1U-N16R8). Backend lives in this monorepo: [`../service/`](../service/).

![Brufik deskbot — assembled unit](mechanical/poster.jpg)

---

## Toolchain

| Item | Value |
|------|--------|
| Board | Custom **Deskbot v2 mainboard** (PCB V2.0 Base, ESP32-S3-WROOM-1U-N16R8) |
| Platform | [pioarduino](https://github.com/pioarduino/platform-espressif32) **55.03.39** (do **not** use stock PlatformIO `espressif32` Arduino 2.0.17 / IDF 4.4.7) |
| Framework | Arduino-ESP32 **3.3.9** + ESP-IDF **5.5.4** |
| Host Python | **≥3.10** (recommend `python3.11 -m venv hardware/.venv` then `pip install platformio`) |
| Flash | [`./flash_rom.sh`](flash_rom.sh) (prefers `.venv/bin/pio`; ports include `/dev/tty.usbmodem*`) |

Audio uses Arduino **`ESP_I2S`** (PDM RX mic + STD TX amp). Camera uplink prefers hardware JPEG, else RGB565 + `frame2jpg` (AWB warmup frames after init). `device_id` comes from `esp_read_mac` (`deskbot_<mac>`).

---

## 1. Out of the box

1. Edit [`firmware/deskbot_config.h`](firmware/deskbot_config.h): set `WIFI_DEFAULT_*` to your AP (or set the AP to match those macros), and set `DESKBOT_WS_HOST` / `DESKBOT_WS_PORT` to your backend.
2. Power on — the device joins WiFi and opens `ws://…/asr_chat?device_id=…` (**device_id only**; no API key or PIN on the device link).
3. To change WiFi later: power on, join the open soft-AP whose SSID is the **`device_id`** (e.g. `deskbot_e83dc1faea30`), then open the URL on the screen (usually **`http://192.168.4.1/`**).

---

## 2. Developers

```bash
# from hardware/
./flash_rom.sh all          # build + upload + monitor
./flash_rom.sh build
./flash_rom.sh upload [port]
./flash_rom.sh log [port]
```

Backend: [`../service/`](../service/). Firmware WebSocket: **`/asr_chat`**. Camera frames go over the same WS (`camera_frame`); there is **no** always-on local camera HTTP page after STA is up (provisioning portal only during AP mode). Debug JPEG preview is on the server: `/camera_view`.

WiFi write-timeout patches (Arduino 3.x) target `NetworkClient.cpp` (fallback `WiFiClient.cpp`); see `scripts/`.

---

## 3. DIY assembly

| Part | Notes | Search terms |
|------|-------|----------------|
| Main PCB | **PCB V2.0 Base** — custom mainboard: ESP32-S3-WROOM-1U-N16R8, **onboard mic** + NS4168 amp + USB-C. [BOM](mechanical/BOM_V2.0_Base_PCB_V2.0_Base_2026-09-02.xlsx), [Gerber](mechanical/小歪_Gerber_PCB_V2.0_Base_2026-09-02.zip) in repo | JLC |
| LCD PCB | **PCB V2.0 LCM** — screen carrier board, FPC to mainboard. [BOM](mechanical/BOM_V2.0_LCM_PCB_V2.0_LCM_2026-09-02.xlsx) in repo | JLC |
| Camera | **OV3660**, **off-axis**, **120°** wide, **40 mm** long. Firmware `esp_camera` auto-detects the sensor; OV2640/OV3660 share the pinout | `OV3660 camera off-axis 120° 40mm` |
| LCD | 1.83" SPI **ST7789** 240×284, rounded corners, 12-pin socket, no touch | Waveshare 1.83 LCD Rev2 |
| Servos | **Large + small**: **9g (tilt) + 3.7g (pan)**, both **JR2.54**; 3.7g needs **5V**, 20.2×8.5 mm (±0.5 mm) | `SG90` / `9g servo`, `3.7g micro servo` |
| Amp | I2S (onboard NS4168) | — |
| Speaker | **2011** type, 8Ω 1W, 1.25 plug | `2011 speaker`, `8Ω 2011` |
| FPC antenna | IPEX 1st-gen connector, 5 cm cable, 28×9 mm (≤30×30 mm) | `FPC antenna IPEX 1 5cm` |
| Power | **5V ≥1A** for servos; USB-C on mainboard | — |

### BOM for ordering (per kit)

| Category | Item | Spec | Qty | Notes |
|----------|------|------|-----|-------|
| Hardware | Screen | 1.83" TFT LCD ST7789 240×284 SPI rounded corners | 1 | 12-pin socket, no touch |
| Hardware | Camera | OV3660 off-axis 120° wide-angle, 40 mm long | 1 | — |
| Hardware | Speaker | 2011 chamber, 8Ω 1W | 1 | 1.25 plug |
| Hardware | 9g servo | 180° servo | 1 | JR2.54 socket |
| Hardware | 3.7g servo | 20.2 × 8.5 mm | 1 | 1) Must run on **5V**; 2) 20.2×8.5 mm (±0.5 mm tolerance, standard size); 3) JR2.54 socket |
| Hardware | FPC antenna | IPEX gen-1 connector, 5 cm cable, 28×9 mm | 1 | Larger gives better signal, but ≤ 30×30 mm |
| Hardware | LCD PCB | See PCB sheet | 1 | i.e. **PCB V2.0 LCM** |
| Hardware | Main PCB | See PCB sheet | 1 | i.e. **PCB V2.0 Base** |
| Fasteners | M2×5 self-tapping screw | Cross pan-head, stainless, flat tip | 14 | — |
| Fasteners | M2×8 self-tapping screw | Cross pan-head, stainless, flat tip | 9 | — |
| Fasteners | M2×12 self-tapping screw | Cross pan-head, stainless, flat tip | 2 | — |
| Fasteners | Cable | 1.25 6-pin 150 mm, dual-head same-direction silicone | 1 | Same-direction, see [diagram](mechanical/连接线_双向同头示意图.png) |

### Assembly guide & reference photos

- **Step-by-step manual:** [`mechanical/小歪v2.0组装说明书PDF.pdf`](mechanical/小歪v2.0组装说明书PDF.pdf)
- **3D-printed parts (STEP):** [`mechanical/小歪机器人v2.0打印件stp.stp`](mechanical/小歪机器人v2.0打印件stp.stp)
- **Camera clip (OV3660 mount):** [`mechanical/小歪机器人v2.0 ov3660摄像头卡子.stp`](<mechanical/小歪机器人v2.0 ov3660摄像头卡子.stp>)

**Main PCB (PCB V2.0 Base):**

![PCB V2.0 Base mainboard](mechanical/PCB_PCB_V2.0_Base_2026-09-02.png)

**LCD PCB (PCB V2.0 LCM):**

![PCB V2.0 LCM screen board](mechanical/PCB_PCB_V2.0_LCM_2026-09-02.png)

### Wiring (PCB V2.0, silkscreen = GPIO numbers)

> The v2.0 mainboard silk labels **GPIO numbers** directly; on old XIAO drawings, IO8 / IO3 mean GPIO, not pads D8 / D3.

| Device | Signals | GPIO | Notes |
|--------|---------|------|-------|
| LCD SPI | MOSI/SCK/CS/DC | **GPIO5 / 4 / 6 / 7** | RST/BL tied to 3.3V |
| Servo X (pan) | PWM | **GPIO16** | 3.7g small; avoids USB 19/20 & strapping pins |
| Servo Y (tilt) | PWM | **GPIO15** | 9g large |
| Amp NS4168 | DIN/BCLK/LRC | **GPIO40 / 41 / 42** | I2S → 2011 speaker; SD=GPIO45 enable-high |
| Mic | PDM CLK/DATA | **GPIO1 / GPIO2** | Onboard (LinkMems LMD2718T271) |
| Servo power | 5V / GND | Separate 5V ≥1A | Common ground with logic |

Pin macros: [`firmware/deskbot_config.h`](firmware/deskbot_config.h); camera 8-bit parallel + SCCB pins: [`firmware/camera.cpp`](firmware/camera.cpp).

---

## License

| Scope | License | File |
|-------|---------|------|
| Hardware ([`mechanical/`](mechanical/)) | CERN-OHL-S-2.0 | [`mechanical/LICENSE`](mechanical/LICENSE) |
| Software ([`firmware/`](firmware/) etc.) | GNU GPL v3.0 | [`firmware/LICENSE`](firmware/LICENSE) |
