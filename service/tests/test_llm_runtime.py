from __future__ import annotations

import json

import pytest


class _FakeHttpResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._body


def test_build_chat_model_keeps_openai_compatible_model_id():
    from deskbot_server.infrastructure.llm.runtime import build_chat_model

    assert build_chat_model("openai", "ep-202607020001") == "ep-202607020001"
    assert build_chat_model("openai", "openai/ep-202607020001") == "ep-202607020001"


def test_build_chat_model_preserves_real_org_prefixes():
    """前缀只在属于当前协议时才剥：``Qwen/Qwen3-8B`` 的 ``Qwen`` 是 org 名，不是协议别名。

    早先的实现不看协议、只要前缀命中 LEGACY_MODEL_PREFIXES 就剥（``qwen`` 恰在其中），
    会把硅基流动的模型 ID 变成 ``Qwen3-8B`` 发给云端 → 400。
    """
    from deskbot_server.infrastructure.llm.runtime import build_chat_model

    assert build_chat_model("openai", "Qwen/Qwen3-8B") == "Qwen/Qwen3-8B"
    assert build_chat_model("openai", "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B") == "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B"
    # 协议自己的别名照旧剥
    assert build_chat_model("ark", "ark/ep-1") == "ep-1"
    assert build_chat_model("ark_responses", "ark_responses/ep-1") == "ep-1"
    assert build_chat_model("ark", "volcano/ep-1") == "ep-1"  # _normalized_protocol 归一别名
    # 别的协议的前缀不再越界剥（新语义）
    assert build_chat_model("openai", "dashscope/qwen-max") == "dashscope/qwen-max"
    # 已知残留：前缀恰好等于协议名时仍剥（有意保留的向后兼容）
    assert build_chat_model("openai", "openai/gpt-oss-20b") == "gpt-oss-20b"


def test_cloud_provider_of_base_url_matches_host_suffix_only():
    """host 后缀匹配：``evil-siliconflow.cn`` / ``api.siliconflow.cn.evil.com`` 不算硅基流动。"""
    from deskbot_server.infrastructure.llm.runtime import cloud_provider_of_base_url as f

    assert f("https://api.siliconflow.cn/v1") == "siliconflow"
    assert f("https://api.siliconflow.cn/v1/") == "siliconflow"
    assert f("https://ark.cn-beijing.volces.com/api/v3") == "ark"
    assert f("https://evil-siliconflow.cn/v1") is None
    assert f("https://api.siliconflow.cn.evil.com/v1") is None
    assert f("http://127.0.0.1:9105/v1") is None
    assert f("") is None


def test_resolve_system_llm_config_reads_api_key_env(monkeypatch):
    """系统默认可经 ``llm.api_key_env`` **指名**服务端环境变量取密钥（只认变量名）。"""
    from deskbot_server.infrastructure.llm.runtime import SILICONFLOW_BASE_URL, resolve_system_llm_config

    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-sf-test")
    monkeypatch.setattr(
        "deskbot_server.infrastructure.llm.runtime.load_config",
        lambda: {
            "llm": {
                "protocol": "openai",
                "base_url": SILICONFLOW_BASE_URL,
                "model_name": "Qwen/Qwen3-8B",
                "api_key_env": "SILICONFLOW_API_KEY",
                "context_window": 32768,
            }
        },
    )

    cfg = resolve_system_llm_config()

    assert cfg.api_key == "sk-sf-test"
    assert cfg.protocol == "openai"
    assert cfg.api_base == SILICONFLOW_BASE_URL
    assert cfg.model == "Qwen/Qwen3-8B"  # org 前缀必须保留
    assert cfg.context_window == 32768
    assert cfg.extra_body == {"enable_thinking": False}


def test_resolve_system_llm_config_api_key_env_never_inline_secret(monkeypatch):
    """``api_key_env`` 只接受环境变量名；写明文密钥进去取不到值（config.yaml 进 git）。"""
    from deskbot_server.infrastructure.llm.runtime import resolve_system_llm_config

    monkeypatch.setattr(
        "deskbot_server.infrastructure.llm.runtime.load_config",
        lambda: {"llm": {"protocol": "openai", "model_name": "m", "api_key_env": "sk-inline-secret"}},
    )

    assert resolve_system_llm_config().api_key == ""


def test_resolve_system_llm_config_api_key_env_missing(monkeypatch):
    """变量名合法但环境未设置 → 空 key，不抛（由 _validate_api_key 在真正调用时报错）。"""
    from deskbot_server.infrastructure.llm.runtime import resolve_system_llm_config

    monkeypatch.delenv("NO_SUCH_ENV_XYZ", raising=False)
    monkeypatch.setattr(
        "deskbot_server.infrastructure.llm.runtime.load_config",
        lambda: {"llm": {"protocol": "openai", "model_name": "m", "api_key_env": "NO_SUCH_ENV_XYZ"}},
    )

    assert resolve_system_llm_config().api_key == ""


def test_siliconflow_extra_body_only_for_thinking_capable_models():
    """R1 蒸馏款的思考被 chat template 固化，下发 enable_thinking 无效（实测仍有 reasoning token）。"""
    from deskbot_server.infrastructure.llm.runtime import siliconflow_extra_body

    assert siliconflow_extra_body("Qwen/Qwen3-8B") == {"enable_thinking": False}
    assert siliconflow_extra_body("deepseek-ai/DeepSeek-R1-0528-Qwen3-8B") is None
    assert siliconflow_extra_body("") is None


def test_build_completion_payload_merges_extra_body_with_reserved_key_guard():
    from deskbot_server.infrastructure.llm.runtime import ResolvedLlmConfig, _build_completion_payload

    def cfg(**kw):
        base = {
            "model": "Qwen/Qwen3-8B",
            "api_key": "k",
            "api_base": "https://api.siliconflow.cn/v1",
            "protocol": "openai",
            "source": "test",
            "display_name": "t",
        }
        return ResolvedLlmConfig(**{**base, **kw})

    msgs = [{"role": "user", "content": "hi"}]
    body = _build_completion_payload(
        msgs, cfg(extra_body={"enable_thinking": False}), temperature=0.7, json_mode=False, stream=False
    )
    assert body["enable_thinking"] is False
    assert body["model"] == "Qwen/Qwen3-8B"

    # 保留键不可被 extra_body 覆盖
    body = _build_completion_payload(
        msgs, cfg(extra_body={"model": "hacked", "messages": []}), temperature=0.7, json_mode=False, stream=False
    )
    assert body["model"] == "Qwen/Qwen3-8B"
    assert body["messages"] == msgs

    # extra_body 为 None 时不合并（保持既有请求体形状）
    body = _build_completion_payload(msgs, cfg(), temperature=0.7, json_mode=False, stream=False)
    assert body == {"model": "Qwen/Qwen3-8B", "messages": msgs, "temperature": 0.7, "stream": False}

    # ark_responses 分支不合并（它有自己的 thinking 字段）
    body = _build_completion_payload(
        msgs,
        cfg(protocol="ark_responses", extra_body={"enable_thinking": False}),
        temperature=0.7,
        json_mode=False,
        stream=False,
    )
    assert "enable_thinking" not in body
    assert body["thinking"] == {"type": "disabled"}


def test_api_key_error_message_names_siliconflow_env():
    from deskbot_server.infrastructure.llm.runtime import (
        SILICONFLOW_API_KEY_ENV,
        ResolvedLlmConfig,
        api_key_error_message,
    )

    sf = ResolvedLlmConfig(
        model="Qwen/Qwen3-8B",
        api_key="",
        api_base="https://api.siliconflow.cn/v1",
        protocol="openai",
        source="system",
        display_name="t",
    )
    msg = api_key_error_message(sf)
    assert msg and SILICONFLOW_API_KEY_ENV in msg
    assert ".env" in msg

    # 有 key → 无需报错
    assert api_key_error_message(ResolvedLlmConfig(**{**sf.__dict__, "api_key": "sk-x"})) is None

    # 本地引擎恒不报错
    local = ResolvedLlmConfig(
        model="m", api_key="", api_base="http://127.0.0.1:9105/v1", protocol="openai", source="device", display_name="t"
    )
    assert api_key_error_message(local) is None


def test_resolve_system_llm_config_ignores_env_keys(monkeypatch):
    """系统默认 LLM 不读任何环境变量密钥（密钥仅设备级 llm_param）；只取 config.yaml llm 段。"""
    from deskbot_server.infrastructure.llm.runtime import resolve_system_llm_config

    monkeypatch.setenv("ARK_API_KEY", "ark-test-key")
    monkeypatch.setenv("LLM_API_KEY", "llm-test-key")
    monkeypatch.setattr(
        "deskbot_server.infrastructure.llm.runtime.load_config",
        lambda: {"llm": {"protocol": "ark_responses", "model_name": "ep-202607020001"}},
    )

    cfg = resolve_system_llm_config()

    assert cfg.api_key == ""  # 系统默认恒无密钥
    assert cfg.api_base == "https://ark.cn-beijing.volces.com/api/v3"
    assert cfg.model == "ep-202607020001"
    assert cfg.source == "system"


def test_chat_completion_stream_invokes_tts_extractor(monkeypatch):
    from deskbot_server.infrastructure.llm.runtime import ResolvedLlmConfig, chat_acompletion

    seen_deltas: list[str] = []

    def fake_stream(messages, cfg, *, temperature, json_mode, on_delta=None, timeout=60):
        assert json_mode is True
        chunks = ['{"tts":"', "你好", '"}']
        for c in chunks:
            seen_deltas.append(c)
            if on_delta is not None:
                on_delta(c)
        return "".join(chunks), {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}

    monkeypatch.setattr("deskbot_server.infrastructure.llm.runtime._request_chat_completion_stream", fake_stream)
    cfg = ResolvedLlmConfig(
        model="qwen-flash",
        api_key="test-key",
        api_base="https://dashscope.example/v1",
        protocol="dashscope",
        source="test",
        display_name="test",
    )
    tts_seen: list[str] = []

    async def _run():
        async def on_tts(text: str) -> None:
            tts_seen.append(text)

        content, meta = await chat_acompletion([{"role": "user", "content": "hi"}], config=cfg, on_tts_ready=on_tts)
        return content, meta

    import asyncio

    content, meta = asyncio.run(_run())
    assert content == '{"tts":"你好"}'
    assert tts_seen == ["你好"]
    assert meta["usage"]["total_tokens"] == 3


def test_chat_completion_posts_to_openai_compatible_endpoint(monkeypatch):
    from deskbot_server.infrastructure.llm.runtime import ResolvedLlmConfig, chat_completion

    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        seen["headers"] = dict(req.headers)
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeHttpResponse(
            {
                "choices": [{"message": {"content": '{"tts":"你好"}'}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
            }
        )

    monkeypatch.setattr("deskbot_server.infrastructure.llm.runtime.urllib.request.urlopen", fake_urlopen)
    cfg = ResolvedLlmConfig(
        model="openai/ep-202607020001",
        api_key="ark-test-key",
        api_base="https://ark.cn-beijing.volces.com/api/v3",
        protocol="openai",
        source="test",
        display_name="火山方舟",
    )

    content, meta = chat_completion([{"role": "user", "content": "你好"}], config=cfg, json_mode=True, temperature=0.2)

    assert seen["url"] == "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer ark-test-key"
    assert seen["headers"]["Content-type"] == "application/json"
    assert seen["body"] == {
        "model": "ep-202607020001",
        "messages": [{"role": "user", "content": "你好"}],
        "temperature": 0.2,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    assert content == '{"tts":"你好"}'
    assert meta["model"] == "ep-202607020001"
    assert meta["usage"] == {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}


def test_missing_key_message_points_to_device_config():
    from deskbot_server.infrastructure.llm.runtime import ResolvedLlmConfig, chat_completion

    cfg = ResolvedLlmConfig(
        model="ep-202607020001",
        api_key="",
        api_base="https://ark.cn-beijing.volces.com/api/v3",
        protocol="openai",
        source="test",
        display_name="火山方舟",
    )

    with pytest.raises(ValueError) as exc:
        chat_completion([{"role": "user", "content": "hi"}], config=cfg)

    msg = str(exc.value)
    assert "ARK_API_KEY" not in msg
    assert "VOLCENGINE_API_KEY" not in msg
    assert "该设备 LLM 配置" in msg  # 云端密钥指向设备级配置，不再提示环境变量
    assert "pip install" not in msg.lower()


def test_ark_responses_completion_posts_to_responses_endpoint(monkeypatch):
    from deskbot_server.infrastructure.llm.runtime import ResolvedLlmConfig, chat_completion

    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeHttpResponse(
            {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": '{"tts":"你好"}'}],
                    }
                ],
                "usage": {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
            }
        )

    monkeypatch.setattr("deskbot_server.infrastructure.llm.runtime.urllib.request.urlopen", fake_urlopen)
    cfg = ResolvedLlmConfig(
        model="ep-20260708093928-299x5",
        api_key="ark-test-key",
        api_base="https://ark.cn-beijing.volces.com/api/v3",
        protocol="ark_responses",
        source="test",
        display_name="DeepSeek v4 Flash",
    )

    content, meta = chat_completion(
        [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "你好"}],
        config=cfg,
        json_mode=True,
        temperature=0.2,
    )

    assert seen["url"] == "https://ark.cn-beijing.volces.com/api/v3/responses"
    assert seen["body"]["model"] == "ep-20260708093928-299x5"
    assert seen["body"]["stream"] is False
    assert seen["body"]["thinking"] == {"type": "disabled"}
    assert seen["body"]["text"] == {"format": {"type": "json_object"}}
    assert seen["body"]["input"] == [
        {"role": "system", "content": [{"type": "input_text", "text": "你是助手"}]},
        {"role": "user", "content": [{"type": "input_text", "text": "你好"}]},
    ]
    assert content == '{"tts":"你好"}'
    assert meta["usage"] == {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}


def test_ark_responses_stream_parses_output_text_delta(monkeypatch):
    from deskbot_server.infrastructure.llm.runtime import ResolvedLlmConfig, chat_acompletion

    class _FakeStreamResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size: int = 4096) -> bytes:
            if getattr(self, "_done", False):
                return b""
            self._done = True
            return (
                b"event: response.output_text.delta\n"
                b'data: {"type":"response.output_text.delta","delta":"{\\"tts\\":\\""}\n\n'
                b"event: response.output_text.delta\n"
                b'data: {"type":"response.output_text.delta","delta":"\\u4f60\\u597d"}\n\n'
                b"event: response.output_text.delta\n"
                b'data: {"type":"response.output_text.delta","delta":"\\"}"}\n\n'
                b"event: response.completed\n"
                b'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3}}}\n\n'
            )

    def fake_urlopen(req, timeout):
        return _FakeStreamResponse()

    monkeypatch.setattr("deskbot_server.infrastructure.llm.runtime.urllib.request.urlopen", fake_urlopen)
    cfg = ResolvedLlmConfig(
        model="ep-20260708093928-299x5",
        api_key="ark-test-key",
        api_base="https://ark.cn-beijing.volces.com/api/v3",
        protocol="ark_responses",
        source="test",
        display_name="DeepSeek v4 Flash",
    )

    import asyncio

    content, meta = asyncio.run(chat_acompletion([{"role": "user", "content": "hi"}], config=cfg, stream=True))

    assert content == '{"tts":"你好"}'
    assert meta["usage"] == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}


def test_resolve_first_token_timeout_disables_ark_responses_default(monkeypatch):
    from deskbot_server.infrastructure.llm.runtime import LLM_FIRST_TOKEN_TIMEOUT_SECONDS, resolve_first_token_timeout

    monkeypatch.delenv("LLM_FIRST_TOKEN_TIMEOUT", raising=False)
    assert resolve_first_token_timeout("ark_responses") == 0.0
    assert resolve_first_token_timeout("openai") == LLM_FIRST_TOKEN_TIMEOUT_SECONDS


def test_resolve_first_token_timeout_honors_env(monkeypatch):
    from deskbot_server.infrastructure.llm.runtime import resolve_first_token_timeout

    monkeypatch.setenv("LLM_FIRST_TOKEN_TIMEOUT", "20")
    assert resolve_first_token_timeout("ark_responses") == 20.0


def test_wrap_plain_text_llm_answer():
    from deskbot_server.infrastructure.llm.openai_compat import _wrap_plain_text_llm_answer

    wrapped = _wrap_plain_text_llm_answer("明天是7月16号，星期四。")
    assert wrapped is not None
    assert '"tts": "明天是7月16号，星期四。"' in wrapped
    assert _wrap_plain_text_llm_answer('{"tts":"已有 JSON"}') is None
