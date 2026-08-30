"""外部服务类型契约：六类服务的确定性 HTTP 输入/输出格式。

新增服务必须在 manifest 声明 type（见 ServiceType），并实现对应契约端点。
测试按钮（POST /api/services/{name}/test）按本契约构造标准样本请求并校验响应；
manifest 可加 test 覆盖段（见 manifest.TestSpec）声明真实测试端点（如 tts-engine
的 /api/generate multipart 接口），resolve_test_spec() 合并二者并生成可复制的
curl 命令（管理后台「测试」对话框展示，终端直接粘贴即可测试）。
"""

from __future__ import annotations

import base64
import json
import uuid

# 1 秒 16kHz 单声道静音 PCM（int16 LE）——asr / vpr 测试样本
SILENCE_PCM = b"\x00\x00" * 16000
# 1x1 白色 PNG——vlm 测试样本
ONE_PX_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
# 1x1 JPEG——fr 测试样本
ONE_PX_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AVN//2Q=="
)

# 各类型的确定性契约：method / path / headers / body / 期望响应字段
CONTRACTS: dict[str, dict] = {
    "asr": {
        "description": "语音识别：POST /transcribe，body=PCM int16 LE，header X-Sample-Rate",
        "method": "POST",
        "path": "/transcribe",
        "headers": {"Content-Type": "application/octet-stream", "X-Sample-Rate": "16000"},
        "body": SILENCE_PCM,
        "expect": ["text"],
    },
    "tts": {
        "description": "语音合成：POST /synthesize，JSON {text}",
        "method": "POST",
        "path": "/synthesize",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"text": "测试"}).encode("utf-8"),
        "expect": ["audio_base64", "sample_rate"],
    },
    "llm": {
        "description": "文本对话：POST /chat，JSON {messages}（含多轮 tool-call 上下文与 tools 声明）",
        "method": "POST",
        "path": "/chat",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {
                "messages": [
                    {"role": "system", "content": "你是桌面助手，必要时调用工具获取时间/日期信息。"},
                    {"role": "user", "content": "现在几点了？"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_time",
                                "type": "function",
                                "function": {"name": "get_current_time", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_time", "content": '{"time": "14:30"}'},
                    # needle 为英文模型：最后一条问句用英文，测试按钮才能演示工具调用
                    {"role": "user", "content": "Thanks. What day of the week is it today?"},
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_current_time",
                            "description": "获取当前本地时间",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "get_current_date",
                            "description": "获取当前本地日期",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weekday",
                            "description": "获取当前星期（英文）",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    },
                ],
            }
        ).encode("utf-8"),
        "expect": ["text"],
    },
    "vlm": {
        "description": "多模态对话：POST /chat，JSON {messages, image_base64}",
        "method": "POST",
        "path": "/chat",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {"messages": [{"role": "user", "content": "描述这张图片"}], "image_base64": base64.b64encode(ONE_PX_PNG).decode()}
        ).encode("utf-8"),
        "expect": ["text"],
    },
    "fr": {
        "description": "人脸识别：POST /detect，body=JPEG，header Content-Type: image/jpeg",
        "method": "POST",
        "path": "/detect",
        "headers": {"Content-Type": "image/jpeg"},
        "body": ONE_PX_JPEG,
        "expect": ["faces"],
    },
    "vpr": {
        "description": "声纹：POST /voiceprint，JSON {audio_base64, sample_rate}（PCM int16 或 WAV 容器）",
        "method": "POST",
        "path": "/voiceprint",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {"audio_base64": base64.b64encode(SILENCE_PCM).decode(), "sample_rate": 16000}
        ).encode("utf-8"),
        "expect": ["embedding"],
    },
}

def _shell_quote(value: str) -> str:
    """POSIX 单引号包裹（值内单引号用 '"'"' 转义），保证 curl 可原样粘贴。"""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def resolve_test_spec(
    service_type: str,
    port: int,
    test=None,
    headers_override: dict | None = None,
    body_overrides: dict | None = None,
) -> dict:
    """合并 manifest test 覆盖与类型契约，返回完整测试规格。

    返回 {method, path, url, headers, body, body_desc, expect, description, curl, curl_hint}。
    - body: 请求体 bytes（json → JSON；form → multipart/form-data；否则契约内置样本）
    - curl: 可直接在终端执行的 curl 命令（json → -d；form → -F；二进制 → --data-binary @文件）
    - curl_hint: 二进制契约的占位文件提示（如「将 face.jpg 替换为你的图片」）
    - headers_override: 动态覆盖请求头（如 asr 测试样本的真实采样率 X-Sample-Rate），
      curl 命令随之同步
    - body_overrides: 覆盖请求体字段（如 tts 测试文本 {"text": "..."}），json/form/
      契约 JSON body 均支持，curl 命令随之同步
    """
    contract = CONTRACTS[service_type]
    method = (test.method if test and test.method else contract["method"]).upper()
    path = test.path if test and test.path else contract["path"]
    headers = dict(contract["headers"])
    expect = list(contract["expect"]) if not (test and test.expect) else list(test.expect)
    if test and test.headers:
        headers.update(test.headers)
    if headers_override:
        headers.update(headers_override)

    description = contract["description"]
    body: bytes = contract["body"]
    body_desc = ""
    curl_args: list[str] = []
    curl_hint = ""

    if test and (test.path or test.method or test.json_body is not None or test.form_body):
        # 覆盖了真实端点：描述按实际请求重新生成（契约描述已不准确）
        kind = service_type.upper()
        if test.json_body is not None:
            fmt = "JSON"
        elif test.form_body:
            fmt = "multipart form"
        else:
            fmt = "契约样本"
        description = f"{kind}：{test.method or 'POST'} {test.path or contract['path']}，{fmt}"

    if test and test.json_body is not None:
        payload = dict(test.json_body)
        if body_overrides:
            payload.update(body_overrides)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
        body_desc = json.dumps(payload, ensure_ascii=False)
        curl_args.append(f"-H {_shell_quote('Content-Type: application/json')}")
        curl_args.append(f"-d {_shell_quote(body_desc)}")
    elif test and test.form_body:
        form = dict(test.form_body)
        if body_overrides:
            form.update(body_overrides)
        boundary = uuid.uuid4().hex
        parts = []
        for key, value in form.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n')
        body = ("".join(parts) + f"--{boundary}--\r\n").encode("utf-8")
        # 覆盖契约默认 Content-Type（如 tts 契约的 application/json），否则 multipart body 会被当 JSON 解析
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        body_desc = "form " + ", ".join(f"{k}={v}" for k, v in form.items())
        for key, value in form.items():
            curl_args.append(f"-F {_shell_quote(f'{key}={value}')}")
    else:
        # 契约内置样本：二进制（asr PCM / fr JPEG）→ curl 用占位文件；JSON → 可字段覆盖
        ctype = headers.get("Content-Type", "")
        if ctype == "application/json":
            if body_overrides:
                payload = json.loads(body.decode("utf-8"))
                payload.update(body_overrides)
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            body_desc = body.decode("utf-8", errors="replace")
            curl_args.append(f"-H {_shell_quote('Content-Type: application/json')}")
            curl_args.append(f"-d {_shell_quote(body_desc)}")
        elif ctype == "image/jpeg":
            curl_hint = "将占位文件 face.jpg 替换为你自己的图片路径"
            curl_args.append(f"-H {_shell_quote('Content-Type: image/jpeg')}")
            curl_args.append("--data-binary @face.jpg")
        elif ctype == "application/octet-stream":
            curl_hint = "将占位文件 audio.pcm 替换为你自己的音频路径（PCM 16kHz 单声道 int16）"
            curl_args.append(f"-H {_shell_quote('Content-Type: application/octet-stream')}")
            curl_args.append("--data-binary @audio.pcm")
        body_desc = f"binary {len(body)} bytes"

    # 其余 headers（跳过 Content-Type——-d/-F 自动设置，boundary 每次不同不能粘贴）
    for key, value in headers.items():
        if key.lower() == "content-type":
            continue
        curl_args.append(f"-H {_shell_quote(f'{key}: {value}')}")

    url = f"http://127.0.0.1:{port}{path}"
    curl = "curl -sS -X {} {}{}".format(method, _shell_quote(url), " " + " ".join(curl_args) if curl_args else "")
    return {
        "method": method,
        "path": path,
        "url": url,
        "headers": headers,
        "body": body,
        "body_desc": body_desc,
        "expect": expect,
        "description": description,
        "curl": curl,
        "curl_hint": curl_hint,
    }


SERVICE_TYPES = tuple(CONTRACTS.keys())
