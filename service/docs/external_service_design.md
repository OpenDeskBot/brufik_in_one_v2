# 外部服务设计模式与后台交互规范（以 insightface-engine 为例）

> 面向后续「新增/改造外部独立进程服务」任务的实践指南：设计原理、配置自治、
> 后台界面交互要点、落地清单与踩坑记录。
> 框架层面的 manifest 契约/状态机/API 见 [external_services.md](./external_services.md)。

## 一、架构设计原理

**独立进程 + HTTP 契约，生命周期交管理器，业务零耦合。**

```
┌──────────────────────┐  service.yaml（name/type/port/start/healthcheck/auto_start）
│  主服务 deskbot-server │ ── ExternalServiceManager：discover → start/stop/watchdog/自动重启
│ （独立服务管理页）      │
└──────────┬───────────┘
           │ HTTP 127.0.0.1:<port>（确定性契约，见 contract.py CONTRACTS）
┌──────────▼───────────┐
│ insightface-engine    │  server.py + config.yaml（完全自治）
└──────────────────────┘
```

### 1. 复用原代码，零修改换进程（或 fork 独立）

进程内直接 `import deskbot_server` 既有实现，主服务进程内实现保留。
独立进程只是**运行形态**变化，不是重写——切换成本 = 1 个 manifest。
已独立化的服务例外（见下）；新增外部服务若无独立化诉求，仍按本条"复用原代码"执行。

> **fork 独立化路线**（funasr 与 insightface-engine 均走此路线）：
> 服务目录内 `deskbot_server/` 运行子集副本（随仓库提交，各文件头带同步标记与分叉点），
> server.py `sys.path` 指向本服务目录即可让 `import deskbot_server.*` 解析到副本；
> 分叉点须在文件头标注（如 `utils/paths.py` 根目录锚点、`face_embedding.py` 本地模型优先），
> 主服务改动后按 docs/external_services.md 对应节同步副本并更新日期。
>
> - **funasr**（v1.1.0 独立化，v1.2.0 主服务 internal 移除）：独立 venv + 模型副本；
>   主服务已删 funasr.py/model_dir.py，副本为唯一来源
> - **insightface-engine**（v1.1.0 独立化，主服务 internal 保留为回落）：
>   独立 venv + 模型副本；主服务 `camera_face.provider=http`（默认）经 HTTP 调用，
>   失败自动回落进程内池；`provider=internal` 可切回（见 external_services.md）

> **insightface-engine 多 worker 并行**（config.yaml `workers`，默认 min(4, CPU 核数)，`--workers` 可覆盖）：
> uvicorn `--workers` 模式，父进程绑定 socket，每个 worker 是独立进程、各自加载一份
> MediaPipe + InsightFace 模型（内存 ~N 倍），请求按 socket 分发到各 worker 并行推理；
> `asyncio.Lock` 只在单 worker 内串行化，多核部署吞吐 ≈ N 倍。

### 2. 双引擎 + 优雅降级

| 引擎 | 职责 | 要点 |
|---|---|---|
| MediaPipe FaceLandmarker | 检测 + 关键点 | CPU 轻量；非线程安全 → `asyncio.Lock` 串行化推理 |
| InsightFace buffalo_s | 512 维 embedding | 真正"识别"；模型首次自动下载 |

降级链：embedding 不可用 → 回退 landmarks 几何特征 descriptor（`descriptor_kind: embedding|geometry`），
**服务不阻塞、照常响应**。

### 3. 配置完全自治

- 服务只读**同目录 `config.yaml`**（主服务对应配置段的独立快照），**不读主服务 config.yaml，不读 env**
- 双向解耦：改服务配置不影响主服务，改主服务配置不影响服务
- 缺文件/字段/解析失败 → 回退代码内置默认值 + warning 日志，不启动失败
- 配置字段语义与主服务保持一致（insightface-engine 即 `camera_face` 段：置信度/num_faces/帧尺寸/embedding 开关/undistort 块）

### 4. 生命周期与状态机配合

- `service.yaml` 声明 `healthcheck`（http 探测）与 `startup_grace_s`（慢启动宽限，如模型加载）
- 状态机关键语义：**unhealthy 不杀进程**——进程活着慢慢就绪，恢复自动回 running
- 看门狗：非预期退出指数退避自动重启，连续 5 次 → crash_loop 等人工
- `auto_start: true` 持久化在 `data/services/state.json`（用户可在后台覆盖）

### 5. 进程隔离收益

模型加载不阻塞主服务启动；推理崩溃不影响主服务；内存/日志独立可观测。

## 二、后台界面交互设计要点

后台路径：侧边栏高级分组 → 独立服务管理（`/services`）。页面为
`templates/app2c/services.html` + `blueprints/services_bp.py`，Vue 3 + Jinja2。

### 1. 表格设计

- **列收敛**：名称 / 类型 / 状态 / 端口 / 描述 / 操作。不加「版本」「服务路径」——
  类型已表意、路径是开发者信息；「已安装 x.x.x」做名称格内小字（安装状态是操作前置条件）
- 状态徽章六态（unhealthy/failed/crash_loop 归并「运行出错」红色系），状态副行：
  pid / uptime / 重启次数 / exit code
- 操作按钮**按状态条件渲染**（安装中禁用、运行中才显示停止/重启），杜绝非法操作
- 轮询：状态 2s、日志 1s 增量（since offset），日志面板不整包重刷

### 2. 测试对话框（核心交互）

**两段式确认**：打开只拉 `GET /api/services/{name}/test-info`（契约描述、请求摘要、
可复制 curl，**不发请求**）→ 用户点「执行测试」才 `POST /api/services/{name}/test`。

**fr 类型专属（人脸图片）**：
- 默认测试图（`data/test/face.jpg`）**打开即预览**——`test-info` 响应携带
  `image_base64/image_name/image_path`，不依赖执行测试
- 「选择本地图片」→ 选中**自动重测**（`image_base64` 随 test 请求提交，上限 8MB）
- **预览所有权约定**：默认样本由服务端回传 base64（前端不知道文件内容）；
  用户上传图由前端本地持有 data URL（自带 mime，避免猜 `image/jpeg`）——两端互不覆盖

**asr 类型专属（测试音频）**（同 fr 模式）：
- 默认 `data/test/asr.wav` 打开即可播放（test-info 携带可播放的 WAV 容器 base64）
- 服务端样本归一化：WAV 容器剥头取 int16 mono PCM（立体声取左声道），
  `X-Sample-Rate` 用**真实采样率**（适配器内部重采样到 16k），curl 命令同步更新；
  原始 PCM 按 16kHz 直发；上限 16MB
- 前端音频输入区用独立字段（`testInputAudioUrl*`）——tts 结果区的 `testAudioUrl` 会被
  runTest 重置，不能复用
**vpr 类型专属（测试音频 → JSON body）**（wespeaker-resnet34，同 asr 音频输入区）：
- 归一化与 asr 相同（WAV 剥头取 int16 mono、真实采样率、上限 16MB），但契约请求体是
  JSON——manager `_vpr_sample` 复用 `_asr_sample` 剥头后经
  `resolve_test_spec(body_overrides={"audio_base64", "sample_rate"})` 重建 JSON body
  （curl 的 `-d` 同步）；前端模板 type 分支为 `asr || vpr`，预览/选文件交互复用
**tts 类型专属（测试文本 + 音色）**（输入是文本，非文件）：
- 默认文本（manifest `test` 段的 `form/json.text`，无则契约「测试」）由 test-info 携带，
  打开即回填输入框
- **音色下拉**：`test.voices_file`（相对服务源目录，如 `checkout/assets/demo.jsonl`）按行序
  枚举 demo-N → name；默认 demo_id 取 manifest；切换音色即重测
- 改文本/音色 → 点「执行测试」用当前值发送：`{"text": "...", "demo_id": "..."}` 经
  `resolve_test_spec(body_overrides=...)` 重建 multipart/JSON 请求体，curl 同步
- 重测后音频播放器要重建：重置 `testAudioUrl` 并用响应 `audio_base64`
  （大字段透传）重新赋值——漏掉这段播放器会消失
- **踩坑（交互一致性）**：tts 的 select/input 不能挂 `@change` 自动重测——换音色/失焦即触发，
  且整体替换 `testResult` 会丢掉 test-info 的 `voices`（音色下拉消失），
  `testDone` 提前置位还会闪现「✗ 失败：未知错误」。统一走「执行测试」按钮：
  runTest 按类型条件携带 body，响应用 `Object.assign` 合并（保留 voices 等元信息）
- 类型专属样本通用模式 = `_<type>_sample(input_b64, fallback)` 静态方法 +
  `test_info`/`test_service` 分支 + `resolve_test_spec(headers_override=...)` 动态请求头
  / `body_overrides=...` 动态请求体（curl 同步）

**复制交互**：curl 与响应都收敛为 **h4 行内 📋 图标**（`title` 提示用途），
点击复制、变 ✓ 反馈 1.5s。响应复制所见即所得（复制展示体）。
通用实现：`copyText(text, flagKey)`（Clipboard API + textarea 降级）。

**结果呈现**：`✓ 通过（HTTP 200 · 41ms）`——`elapsed_ms` 由后端计时
（请求发出→响应读完，HTTP 错误也计）；失败分层提示：
不可达 → HTTP 状态 → 200 但缺期望字段（列出字段名）。

**大字段透传**：响应大字段（如 tts `audio_base64` >1000 字符）顶层透传 + 内嵌播放器，
展示体替换占位说明——避免截断成残缺 base64。

**防抖与防闪现**：`testBusy` 防重入；`testDone` 在请求返回后才置位，
避免结果区闪现「✗ 失败：未知错误」。

### 3. 生效机制

Jinja2 模板每次请求重读磁盘 → **前端改动刷新即生效**；后端
（manager.py / services_bp.py / contract.py）需重启主服务。

## 三、新增同类服务落地清单

1. **建目录** `externals/<name>/`：`service.yaml` + `server.py` + `config.yaml`（若需配置自治）
2. **service.yaml**：name/version/type/port、start.command+workdir（相对 service root）、
   healthcheck（含 startup_grace_s）、auto_start；install 幂等脚本在 service root 执行
3. **注册契约**：`contract.py` 的 `CONTRACTS` 加条目（method/path/headers/body/expect）+ 内置样本；
   `SERVICE_TYPES` 自动继承
4. **管理器零改动**：discover/install/start/stop/watchdog/test 全部按 manifest 泛化驱动
5. **后端测试**：`tests/test_external_contract.py` 的 fake HTTP server 加端点，
   覆盖 test_info / test_service / 缺字段 / 不可达 / 非法输入
6. **前端自动适配**：表格与测试按钮按 `type` 通用渲染，无需改页面；
   仅在需要类型专属交互（如 fr 图片）时加 `<template v-if="testResult.type === 'fr'">`
7. **同步文档**：`external_services.md` 契约表与 API 表、本文件、docs/README.md 索引
8. **改名注意**：改服务名（如 face-engine → insightface-engine）时同步
   目录 / service.yaml / server.py 内名字 / `state.json` key（保留 auto_start）/ 数据日志目录

## 四、踩坑与约定（决策记录）

| 坑 / 决策 | 结论 |
|---|---|
| 默认图只在 test 响应里回，打开对话框误显「无默认样本」 | 默认样本元信息放 **test-info**，打开即预览 |
| 服务端回传用户上传图会丢 mime | 默认样本服务端回、用户上传图前端持有 |
| 大字段塞展示体会截断成残缺 base64 | 顶层透传 + 播放器，body 占位符 |
| 200 但缺期望字段判"通过"会误报 | `expect` 字段校验，缺失列名提示 |
| 模型加载慢被 healthcheck 误杀 | unhealthy 不杀进程 + `startup_grace_s` 宽限 |
| 用户上传图无大小限制会撑爆请求 | `image_base64` 上限 8MB，非法 base64 明确报错 |
| 响应无耗时，难判断服务性能 | `elapsed_ms` 由 `_probe_contract` 计时返回 |
| 配置读主服务造成双向耦合 | 服务自带 `config.yaml` 快照，完全自治 |
