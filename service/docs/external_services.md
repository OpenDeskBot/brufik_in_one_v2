# 外部进程服务管理

deskbot-server 内置一个轻量**外部进程服务管理器**：以独立进程 + 独立 venv 运行的服务
（如 MOSS-TTS-Nano），由主服务统一管理安装 / 启动 / 停止 / 状态 / 默认启动 / 日志，
并在管理后台（高级 → 外部服务）提供操作界面。

设计原则：**管理框架只做生命周期，不碰业务**。新增一个服务 = 在 `service/externals/<name>/`
放一个 `service.yaml`（manifest 契约）+ 一个幂等的 install 脚本，框架零改动。

## 目录与运行状态

```
service/
  externals/<name>/          # 外部服务源码目录（不入库 .venv / checkout）
    service.yaml             # manifest 契约（必填）
    install.sh               # 幂等安装脚本（manifest install 引用）
  data/services/             # 运行时状态（gitignore）
    <name>/<name>.log        # stdout/stderr 合并日志（超 20MB 轮转 .1）
    <name>/<name>.pid        # pid 文件（孤儿进程回收依据）
    state.json               # 持久化：installed_version / auto_start 覆盖值
```

## Manifest 契约

```yaml
name: moss-tts-nano           # 唯一名：小写字母/数字/-/_（建议用具体框架名，如 funasr/moss-tts-nano）
version: "2026.4.10"          # 展示用版本
description: ...              # 后台展示描述
install:                      # 安装命令（service root 下执行，须幂等；可多条）
  - bash externals/moss-tts-nano/install.sh
uninstall:                    # 可选；无则只清状态与数据目录（同样在 service root 执行）
  - rm -rf externals/moss-tts-nano/.venv externals/moss-tts-nano/checkout
start:
  command:                    # 启动命令（相对 workdir 或绝对路径）
    - .venv/bin/moss-tts-nano
    - serve
    - --backend
    - onnx
    - --host
    - 127.0.0.1
    - --port
    - "9101"
  workdir: ./externals/moss-tts-nano
  # 附加环境变量（合并进主服务环境）；moss-tts-nano 示例：国内网络走 hf-mirror 镜像，
  # HF_HUB_DISABLE_XET=1 禁用 huggingface_hub 的 xet 存储（镜像不支持会 401）
  env:
    HF_ENDPOINT: "https://hf-mirror.com"
    HF_HUB_DISABLE_XET: "1"
healthcheck:                  # 可省略；省略时进程存活即视为健康
  type: http                  # http | tcp
  url: http://127.0.0.1:9101/health
  interval_s: 5
  startup_grace_s: 900        # 首次启动宽限（模型下载等慢启动）；超时仍失败只标记 unhealthy，不杀进程
  timeout_s: 3
  max_failures: 3             # 连续失败超过该次数 → unhealthy（恢复后自动回 running）
auto_start: false             # 主服务启动时自动拉起；用户可在后台覆盖并持久化
```

## 服务类型契约（manifest `type`）

每个服务声明 `type`（七选一）与 `port`（可省略，从 healthcheck url 推导），
**输入/输出格式是确定性契约**，管理后台「测试」按钮按契约发标准样本请求并校验响应字段：

| type | 端点 | 请求 | 成功响应（校验字段） |
|------|------|------|---------------------|
| asr | POST `/transcribe` | body=PCM int16 LE，header `X-Sample-Rate` | `{"text": str}` |
| tts | POST `/synthesize` | JSON `{"text": str}` | `{"audio_base64": str, "sample_rate": int}` |
| llm | POST `/chat` | JSON `{"messages": [{role, content}]}` | `{"text": str}` |
| vlm | POST `/chat` | JSON `{"messages": [...], "image_base64": str}` | `{"text": str}` |
| fr | POST `/detect` | body=JPEG bytes，header `Content-Type: image/jpeg` | `{"faces": [...]}` |

「测试」对话框对 fr 展示测试图片：打开即默认显示 `data/test/face.jpg`（test-info 携带，结果回
`image_base64/image_name/image_path` 供前端预览），也可选择本地图片（`POST
/api/services/{name}/test` 带 JSON body `{"image_base64": "..."}`，上限 8MB）；
默认样本缺失时回退契约内置 1x1 JPEG。

「测试」对话框对 asr 展示测试音频：打开即默认播放 `data/test/asr.wav`（test-info 携带，
结果回 `audio_base64/audio_name/audio_path/sample_rate` 供前端预览），也可选择本地音频
（`{"audio_base64": "..."}`，上限 16MB）。样本支持 WAV 容器（自动剥头取 int16 mono PCM，
立体声取左声道，`X-Sample-Rate` 用真实采样率，curl 同步）与原始 PCM（按 16kHz 直发）；
默认样本缺失时回退契约内置静音 PCM。

「测试」对话框对 tts 展示测试文本与音色：打开即回填默认文本（manifest `test` 段的
`form/json.text`，无则契约内置「测试」）与音色下拉（`test.voices_file` 指向服务目录内
的 demo.jsonl，逐行 JSON 的 `name` 按行序映射 demo-N；默认 demo_id 取 manifest
`form/json.demo_id`）；`{"text": "...", "demo_id": "..."}` 可覆盖，multipart/JSON
body 与 curl 同步重建），改文本/音色后点「执行测试」用当前值发送。
| vpr | POST `/voiceprint` | JSON `{"audio_base64": str, "sample_rate": int?}` | `{"embedding": [...], "dim": int}` |

「测试」对话框对 vpr 展示测试音频（同 asr 模式，音频输入区复用）：默认
`data/test/asr.wav`（test-info 携带，打开即可播放），也可选择本地音频
（`{"audio_base64": "..."}`，上限 16MB）。样本支持 WAV 容器（剥头取 int16 mono PCM，
`sample_rate` 用真实采样率，curl 同步）与原始 PCM（按 16kHz）；默认样本缺失时回退
契约内置静音 PCM。vpr-engine 另提供 `POST /compare`（两个音频 embedding 余弦相似度，
传 `threshold` 返回 `match`），供后续声纹识别玩法使用。

契约定义与样本在 `service/external/contract.py`；测试接口 `POST /api/services/{name}/test`
返回请求摘要 + 状态码 + 响应时间（elapsed_ms）+ 响应体 + 缺失字段（200 但缺期望字段判失败）。

### manifest `test` 覆盖段（可选）

现成服务（如 moss-tts-nano）通常没有实现通用契约端点（`/synthesize` 等），可在
service.yaml 加 `test` 段声明真实测试端点，测试按钮与 curl 命令按它发送：

```yaml
test:
  path: /api/generate        # 真实端点；默认用契约 path
  method: POST
  headers: {X-Extra: '1'}    # 可选，合并进契约 headers
  form:                      # multipart/form-data（二选一；curl 生成 -F 参数）
    text: "你好，这是语音合成测试。"
    demo_id: "demo-1"
  # json: {text: "..."}      # 或 JSON body（curl 生成 -d 参数）
  expect: [audio_base64]     # 可选，默认用契约 expect
```

- `json` 与 `form` 互斥；都不写则用契约内置样本（二进制契约 curl 用
  `--data-binary @<文件>` 占位并给替换提示）
- 「测试」对话框打开时先调 `GET /api/services/{name}/test-info` 拉取契约描述、
  请求与**可复制 curl 命令**（点「执行测试」才真正发请求），curl 直接粘贴终端
  即可复测，无需登录鉴权

## 状态机

```
NOT_INSTALLED → INSTALLING → STOPPED → STARTING → RUNNING
                                 ↘ FAILED（启动即崩，等用户）
RUNNING → UNHEALTHY（healthcheck 连续失败；不杀进程，恢复后自动回 RUNNING）
RUNNING/UNHEALTHY 非预期退出 → RESTARTING（指数退避 0/1/2/4/…/60s）
连续 5 次崩溃 → CRASH_LOOP（停止自愈，等用户干预）
STARTING 宽限期内退出 → FAILED（配置/依赖问题，自动重启无意义）
```

- 进程跟随主服务生命周期：主服务正常退出会统一 `SIGTERM → 5s → SIGKILL` 回收；
  主服务被 SIGKILL 遗留的孤儿进程在下次 `start` 时由 pid 文件检测并回收
- 每次操作（install/start/stop/restart/autostart）与看门狗重启通过每服务一把
  asyncio.Lock 串行化，天然防竞态

## 管理后台与 API

后台路径：侧边栏高级分组 → 独立服务管理（`/services`），需登录。表格展示
服务名称/类型/状态/端口/描述/操作；状态六态：未安装 / 安装中 / 未启动 / 启动中 /
停止中 / 运行中 / 运行出错（unhealthy/failed/crash_loop 均归"运行出错"）。
操作：安装 / 卸载 / 启动 / 停止 / 重启 / 默认启动开关 / 查看日志。
日志面板 1s 轮询实时增量，安装与运行输出在同一个日志文件。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/services` | 全部服务状态（state/pid/uptime/healthy/restarts/last_error/log_size） |
| POST | `/api/services/{name}/install` | 幂等安装（输出进服务日志） |
| POST | `/api/services/{name}/uninstall` | 卸载：停进程 → 执行 manifest uninstall 命令（可选，失败仅告警）→ 清数据目录与安装状态 |
| POST | `/api/services/{name}/start` | 启动（未安装会 409） |
| POST | `/api/services/{name}/stop` | 停止 |
| POST | `/api/services/{name}/restart` | 重启 |
| GET | `/api/services/{name}/test-info` | 测试契约元信息（契约描述/请求/可复制 curl），不发请求 |
| POST | `/api/services/{name}/test` | 契约测试（manifest `test` 覆盖或类型契约）；fr 可带 `{"image_base64": "..."}` 指定测试图片 |
| PUT | `/api/services/{name}/autostart` | 改默认启动（`{"enabled": true}`，持久化到 state.json） |
| GET | `/api/services/{name}/logs?since=N` | 日志增量读（since=上次 next_offset） |

错误响应统一 `{"ok": false, "error": "..."}`；冲突（安装中/启动中）返回 409。

## 代码结构

```
deskbot_server/
  infrastructure/external/process_supervisor.py   # 进程原语：spawn/pid/信号/日志 fd 重定向
  service/external/manifest.py                    # service.yaml 解析与 externals 扫描
  service/external/manager.py                     # 状态机、看门狗、自动重启、state.json
  web/blueprints/services_bp.py                   # 页面 + REST API
  web/templates/app2c/services.html               # 管理页面（Vue3 + 轮询）
```

装配：`main.build_runtime` 创建 manager 并 `discover()`；`main()` 启动看门狗并异步拉起
auto_start 服务；FastAPI lifespan 关闭时 `shutdown()` 优雅回收全部子进程。

单个服务的设计模式（配置自治、后台测试对话框交互规范、新增服务落地清单与踩坑）见
[external_service_design.md](./external_service_design.md)。

## insightface-engine：InsightFace 人脸检测与识别（独立化，v1.1.0）

`externals/insightface-engine/` 以独立进程提供 MediaPipe 检测 + InsightFace 512 维
embedding：**完全自包含**——独立 venv（`.venv`，`install.sh` 幂等）、服务目录内模型副本
（`models/mediapipe/face_landmarker.task` 3.6M + `models/buffalo_s/w600k_mbf.onnx` 13M；
install 双路径：主服务 `models/` 或 `~/.insightface` 缓存有源则 copy，无源则下载）、
`deskbot_server/` 运行子集副本（随仓库提交，各文件头带同步标记；分叉点：`utils/paths.py`
根目录、`vision/face_embedding.py` 本地模型优先），**运行期零依赖主服务**；**配置完全自治**——
只读同目录 `config.yaml`（含 `workers` 字段，默认 min(4, CPU 核数)，uvicorn 多 worker
并行推理，每 worker 独立加载一份模型，内存 ~N 倍）。

- 契约 fr：`POST /detect`（body=JPEG bytes）→ `{"faces": [{landmarks, embedding,
  descriptor_kind, image_w, image_h}, ...]}`；`POST /embedding`（JSON `jpeg_base64` +
  `landmarks`）→ `{"embedding", "descriptor_kind"}`；`GET /health`；端口 9103
- **主服务集成**：`camera_face.provider=http`（config.yaml 默认）→ `FrHttpClient`
  （`infrastructure/face/fr_http_client.py`，镜像 funasr_adapter：urllib + to_thread）
  调 `/detect`；调用失败（不可达/超时/响应异常）自动回落进程内池并告警
  （首次 warning 之后 debug）；`provider=internal` 可手动切回进程内多进程池
- 主服务进程内实现（`CameraFaceService` + ProcessPoolExecutor）保留作回落/对照，
  依赖不卸载（与 funasr 的"internal 移除"不同——主服务自身还在用同一套 vision 代码）

## vpr-engine：WeSpeaker ResNet34 声纹识别

`externals/vpr-engine/` 以独立进程提供 WeSpeaker ResNet34 speaker embedding
（256 维，CN-Celeb 训练，适合中文语音），**独立 venv**（主服务 venv 无 wespeaker 依赖），
**配置完全自治**——只读同目录 `config.yaml`（model_dir/device/min_audio_seconds 等），
不读主服务 config.yaml/env。

- 端点：`GET /health`；`POST /voiceprint` → `{"embedding": [...], "dim": 256,
  "elapsed_ms": ...}`；`POST /compare`（声纹比对）→ `{"similarity": ..., "match": bool?}`；
  端口 9104。音频输入为 WAV 容器（自描述）或原始 PCM int16 LE + `sample_rate`（默认
  16kHz）；服务端重采样到 16k 后提取；错误统一 `{"error": {"code", "message"}}`
- 安装：`install.sh` 幂等（独立 venv + pip 装 wespeaker + hf-mirror 下载 CN-Celeb
  ResNet34 模型 ~100MB，已装跳过）；模型换 VoxCeleb 版本改 `config.yaml` 的
  `model_dir` 并手动下载对应 repo 即可
- 注意：torch 推理串行化由 server 内锁保证；`/compare` 的 `match` 判定阈值由调用方
  业务层决定（同人典型相似度 ~0.7+，跨人 <0.5，仅供参考）

## moss-tts-nano 已知适配（install.sh 自动处理）

- **模型下载**：首次启动经 hf-mirror 镜像下载约 740MB ONNX 模型（`HF_ENDPOINT` +
  `HF_HUB_DISABLE_XET` 已写入 manifest env）；海外环境可去掉镜像
- **WeTextProcessing/pynini**：PyPI 无 macOS arm64 wheel（编译需 openfst），装不上时
  install.sh 会幂等 patch MOSS 源码两处（warmup 硬检查 → 降级 warning；归一化走
  robust fallback），合成功能照常可用，仅中文文本归一化精度略降
- **verify**：`/health` 200；`POST /api/generate`（multipart `text` + `demo_id`）
  返回 JSON 含 `audio_base64`（48kHz 双声道 wav）

## funasr：FunASR 独立服务（主服务 internal 已移除，v1.2.0）

`externals/funasr/` 以独立进程提供 FunASR SenseVoice 识别：**完全自包含**——
独立 venv（`.venv`，`install.sh` 幂等）、服务目录内模型副本（`models/SenseVoiceSmall`，
约 2G；install 双路径：本机 `service/models/` 有源则 copy，无源则从 modelscope 下载 +
本地导出量化 ONNX）、`deskbot_server/` 运行子集副本（随仓库提交），**运行期零依赖主服务**
（venv/模型/源码均自备）；**配置完全自治**——只读同目录 `config.yaml`，不读主服务 config.yaml/env。

**主服务进程内 FunASR 已移除**（`src/deskbot_server/infrastructure/asr/funasr.py`、
`model_dir.py` 及 venv 依赖 funasr/funasr-onnx/torch 等全部删除）。

- 端点：`GET /health`；`POST /transcribe` → `{"text": "...", "elapsed_ms": ...}`；
  端口 9102。请求/响应遵循 ASR 外部服务协议 v1（PCM + `X-Sample-Rate` 或 WAV 容器
  自描述；错误统一 `{"error": {code, message}}`），完整规范见
  [asr_protocol.md](./asr_protocol.md)
- **ASR 为设备级路由**：每个设备在 device 表 `asr_provider` 列配置（`funasr` 默认 /
  `doubao`），查不到默认 funasr（`infrastructure/asr/resolve.py::resolve_asr_adapter`
  每次调用动态解析，无状态 adapter）；`config.yaml` 的 asr 段仅保留
  `external_url` / `text_filter` 基础设施参数。funasr 时装配 `FunAsrAdapter`
  （`infrastructure/asr/funasr_adapter.py`），`is_valid_text` 文本过滤保持本地执行；
  funasr 不可达时 `transcribe` 抛 `RuntimeError`（上层已有 LLM 降级路径）
- 安装/卸载由独立服务管理后台执行（install.sh 幂等：venv 自愈 + 模型双路径跳过 +
  warmup 预验）；ONNX 推理串行化由 server 内锁保证

### funasr 代码副本同步

`externals/funasr/deskbot_server/` 是 **funasr 服务自身的运行时**（13 个文件，
全部 stdlib+numpy；3 个分叉文件：`utils/paths.py`（PROJECT_ROOT 指向服务根）、
`model/__init__.py`、`utils/__init__.py`）。**主服务已删除 funasr.py/model_dir.py，
本副本是这些代码的唯一来源**（不再存在"从主服务同步"的方向；主服务其余共享文件如
`protocol.py`/`settings.py` 改动后仍按需手动同步）：

1. 主服务 `src/deskbot_server/` 相关文件改动后，按各文件头「来源」标记手动 `cp` 覆盖
   （注意「分叉点」差异需手工重放：paths.py 的 parents[2]、两个 `__init__.py` 精简版）
2. 更新副本文件头的 `synced:` 日期；`git log --diff-filter=M -- externals/funasr/deskbot_server/`
   可快速发现是否漂移

install.sh **不会**在安装期从主服务源码生成副本（避免覆盖分叉文件、保持安装离线）。

## 接入业务（示例：TTS 与 ASR 云端 provider）

外部服务只解决"进程怎么活"，业务接入走既有 port/adapter 模式：
`TtsPort`（`ports/tts.py`）保持不变，`MossTtsAdapter`（`infrastructure/tts/moss_adapter.py`）
调用 moss-tts-nano 的 `/api/generate`（multipart，text + demo_id），口型音素由
`utils/phoneme_duration.py` 按文本+总时长补充；在 `bootstrap` 装配时按 `config.yaml` 的
`tts.provider` 切换。默认 `provider: moss-tts-nano`（独立进程）；`doubao`（火山云端，
`infrastructure/tts/doubao_phoneme.py`）为备选。

ASR 同构：设备级两态——`funasr`（独立进程，协议见
[asr_protocol.md](./asr_protocol.md)）/ `doubao`（火山云端一句话识别，
`infrastructure/asr/doubao_adapter.py`，配置走 env `DOUBAO_ASR_*`）。进程内
FunASR 已移除（v1.2.0）；provider 存 device 表（v1.3.0 起），非 config.yaml。
云端 provider 不建 externals 目录（无本地进程），直连云 API。

外部服务只解决"进程怎么活"，业务接入走既有 port/adapter 模式：
`TtsPort`（`ports/tts.py`）保持不变，`MossTtsAdapter`（`infrastructure/tts/moss_adapter.py`）
调用 moss-tts-nano 的 `/api/generate`（multipart，text + demo_id），口型音素由
`utils/phoneme_duration.py` 按文本+总时长补充；在 `bootstrap` 装配时按 `config.yaml` 的
`tts.provider` 切换。默认 `provider: moss-tts-nano`（独立进程）；`doubao`（火山云端，
`infrastructure/tts/doubao_phoneme.py`）为备选。
