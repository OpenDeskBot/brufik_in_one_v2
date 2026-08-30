# ASR 外部服务协议 v1

语音识别外部服务的**唯一权威协议规范**。本地引擎（externals）与云端 ASR（豆包等）
统一收敛到本协议：主服务只认一套请求/响应结构，换后端 = 换 provider 或换 URL。

生命周期管理（进程怎么活）见 [external_services.md](./external_services.md)，
引擎进程设计模式见 [external_service_design.md](./external_service_design.md)。
本文只定义**线上协议**（输入/输出格式）。

## 设计原则

1. **协议越薄越好**：只传消费方（chat 流程）真正需要的字段。协议的扩展方向是
   "加可选字段"——现在不背未来"也许有用"的字段，将来按需加回，不破坏现有消费方。
2. **输入自描述优先**：音频用 `Content-Type` 声明容器，采样率/声道优先从容器读取。
3. **输出只含 text**：时间戳等富信息由主服务内部后处理补齐，不走协议（见
   [segments 约定](#segments-主服务内部后处理约定)）。
4. **错误结构统一**：`{"error": {"code", "message"}}`，主服务据此区分"配额耗尽 vs
   音频损坏"做不同降级。

## 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/transcribe` | 语音识别（核心） |
| GET | `/health` | 健康检查（external 服务管理器 healthcheck 用） |

## POST /transcribe 请求

### 音频输入格式

| Content-Type | body | 采样率来源 | 说明 |
|---|---|---|---|
| `application/octet-stream`（默认） | raw PCM **int16 LE、单声道** | `X-Sample-Rate` header（缺省 16000） | 主服务内部形态：设备解码后的 PCM 零转换直发 |
| `audio/wav` | WAV 容器 | 头内自描述 | 第三方对接友好；须为 PCM 编码、单声道 |

规则：

- `X-Sample-Rate` 须为正整数（引擎内部负责重采样到模型所需采样率）
- WAV 时忽略 `X-Sample-Rate`（以头为准）；`channels != 1` 或非 PCM 编码 → 400
- 不支持的 Content-Type → 415
- 音频时长上限 60s（超限 → 413 `audio_too_large`，可选实现）
- 主服务默认发 PCM；WAV 支持是协议能力，不强制主服务使用

### 成功响应（200）

```json
{
  "text": "你好，有什么可以帮你",     // 必填；无有效语音时可为空字符串 ""
  "language": "zh",                  // 可选：识别语言（zh/en/yue/ja/ko/auto 等）
  "confidence": 0.98,                // 可选：0-1 平均置信度
  "elapsed_ms": 120                  // 可选：引擎从收到请求到返回的内部耗时（ms）
}
```

`text` 是唯一必填字段，其余可选且仅供诊断/未来消费，v1 消费方只读 `text`。

### 错误响应（4xx/5xx）

```json
{"error": {"code": "empty_pcm", "message": "empty pcm"}}
```

错误码清单：

| HTTP | code | 场景 |
|------|------|------|
| 400 | `empty_pcm` | body 为空 |
| 400 | `invalid_sample_rate` | X-Sample-Rate 非正整数 |
| 400 | `invalid_wav` | WAV 头解析失败 / 非 PCM 编码 / 非单声道 |
| 413 | `audio_too_large` | 超过时长上限（可选实现） |
| 415 | `unsupported_media_type` | Content-Type 不在上表 |
| 503 | `model_not_ready` | 模型未加载完成 |
| 500 | `transcribe_failed` | 引擎内部推理异常 |

## GET /health

```json
{"ok": true, "model_ready": true}
```

- `ok`：进程存活且可服务；`model_ready`：模型加载完成（`/transcribe` 可 200）
- 服务管理器 healthcheck 仅以 HTTP 200 + 无异常为准（见 service.yaml `healthcheck`）

## 引擎实现规范（服务端）

任何实现本协议的引擎（本地 externals 或云端网关进程）必须满足：

1. 实现 `/transcribe` 与 `/health` 两端点，请求/响应/错误结构与本协议完全一致
2. **推理串行化**：单模型实例非线程安全（ONNX），须加锁保证并发安全
3. `service.yaml` 契约：`type: asr`、`healthcheck.url` 指向 `/health`
4. 配置自治：只读同目录 `config.yaml`（主服务 asr 段快照），不读主服务配置/env
5. 请求侧解析（PCM/WAV → 归一化 PCM + sr）、标准响应/错误构造建议复用
   `deskbot_server.infrastructure.asr.protocol`（dict 级，无 pydantic 依赖）

## 主服务消费侧约定（客户端）

`HttpAsrAdapter`（`infrastructure/asr/http_adapter.py`）是实现本协议的客户端：

- 默认发 PCM（主服务手里就是 PCM），`X-Sample-Rate` 带上真实采样率
- 超时 30s（音频上行 + 推理）
- 错误结构 `{"error": {code, message}}` → 抛 `RuntimeError` 携带 code，上层据此降级
- 成功但缺 `text` 字段 → 抛 `RuntimeError`（响应异常）
- `is_valid_text`（文本过滤）**本地执行**，不随音频上行

### segments：主服务内部后处理约定

协议输出**不包含**时间戳字段。需要句子级时间戳（打断、字幕等未来需求）时，
由主服务内部统一补全，与后端无关：

- `utils/asr_segments.py`：`complete_segments(text, pcm_bytes, sample_rate)` →
  `[{"start_ms", "end_ms", "text"}]`
- 算法：按句末标点切句（复用 `infrastructure/tts/text_split.py` 的纯文本切分）→
  按字符数等分总时长（`total_ms = len(pcm)/2/sr*1000`）→ 末段吃舍入余量，
  保证 `end_ms == total_ms`
- 与 `utils/phoneme_duration.py` 的"文本+总时长→时间轴"是同一算法思路，但
  **不共享代码**：ASR 是句子级、权重等分，比音素级简单，各自独立实现
- 边界：文本空或 total_ms ≤ 0 → `[]`；无句末标点 → 单段 `[{0, total_ms, 全句}]`

## 接入指南

### 云服务（豆包/讯飞等）

云端 API 无本地进程，**不建 externals 目录**，主服务内适配器接入：

1. `infrastructure/asr/<name>_adapter.py`：实现 `AsrPort`（`ports/asr.py`）——
   `transcribe(pcm, sr)` 内部：pcm → 该云所需格式（如 wav，复用 `pcm_to_wav_bytes`）
   → 调云 API → 解析出 text（并归一化 language/confidence 可选字段）
2. `model/settings.py`：`asr.provider` 接受新值 + 新增该云配置段（认证等），
   与 `external_url` 平级
3. `infrastructure/bootstrap.py`：`build_asr_adapter` 加分支
4. `is_valid_text` 复用 `infrastructure/asr/text_filter.py`

可选：认证/限流想集中管理时，可加 `externals/<name>-engine/` 薄网关进程，
实现本协议（此时主服务走 `provider: external` + URL，零适配代码）。

### 本地独立服务（externals）

1. `externals/<name>-engine/`：`server.py`（加载模型 + 实现协议两端点）、
   `service.yaml`（type: asr + 端口 + healthcheck）、`config.yaml`（自治快照）
2. 主服务零代码改动：`asr.provider: external` + `external_url` 指向新引擎
   （`HttpAsrAdapter` 不动）

## 已接入后端注册表

| 后端 | 形态 | 接入方式 | 状态 |
|------|------|----------|------|
| funasr（SenseVoice） | 本地 externals（`externals/funasr`，端口 9102） | `provider: external` | 已实现：PCM/WAV 输入、`{"text", "elapsed_ms"}`、统一错误结构（复用 `infrastructure/asr/protocol.py`） |
| doubao（豆包） | 云 API（火山一句话识别 v1，`openspeech.bytedance.com`） | `provider: doubao`（`infrastructure/asr/doubao_adapter.py`） | 已实现：配置走 env（`DOUBAO_ASR_APP_ID`/`ACCESS_TOKEN`/`CLUSTER`）；直连云 API，不走本协议 HTTP 层，但响应解析为同一 text 结构 |

## 版本与兼容

- 本协议为 v1。**加可选字段向后兼容**；删/改必填字段为破坏性变更，需升版本
- 协议演进记录在此文档；服务端实现与消费端解析以本文为唯一权威
