# 本地调试工具

在 `.venv` 激活后运行。

- **模拟设备主链路**（与固件一致）：URL 只需带 `device_id`（设备链路仅此一项鉴权，无 API Key / PIN）。
- **调试订阅**（`/camera_view`、`/device_pipeline` 订阅侧）：在 Web 控制台「调试台」登录后签发 `debug_token`，URL 带 `debug_token=...`（兼容参数名 `debugtoken` / `ws_token`）。
- **HTTP / 工具兜底**：`data/.free_api_key`（若存在）仅供本地联调脚本读取。

```bash
source .venv/bin/activate

# 推送 wav 测 /asr_chat 全链路
python tools/test_client.py \
  --ws-url "ws://127.0.0.1:9000/asr_chat?device_id=deskbot_dev" \
  --input-wav demo_16k_mono.wav

# 本机麦克风
python tools/live_mic_client.py \
  --ws-url "ws://127.0.0.1:9000/asr_chat?device_id=deskbot_dev"

# 推图片测 camera_frame
python tools/camera_test_client.py \
  --ws-url "ws://127.0.0.1:9000/asr_chat?device_id=deskbot_dev" \
  --image-dir ./samples

# 设备-服务端网络连通性 / 并发 / PB 延迟（须真实设备在线）
python tools/network_connectivity_test.py \
  --device-id deskbot_e8f60a8cf9b0 \
  --base-url http://127.0.0.1:9000 \
  --concurrent-sec 20
```

WAV 须 **16 kHz / mono / s16le**。
