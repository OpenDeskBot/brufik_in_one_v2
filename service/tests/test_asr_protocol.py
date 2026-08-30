"""ASR 外部服务协议 v1 单元测试：请求归一化 / 响应构造与解析 / segments 补全。

协议定义见 docs/asr_protocol.md；server 路由级集成测试在 test_asr_external.py。
"""

from __future__ import annotations

import io
import wave

import pytest

from deskbot_server.infrastructure.asr.protocol import (
    ERR_AUDIO_TOO_LARGE,
    ERR_EMPTY_PCM,
    ERR_INVALID_RESPONSE,
    ERR_INVALID_SAMPLE_RATE,
    ERR_INVALID_WAV,
    ERR_UNSUPPORTED_MEDIA,
    AsrProtocolError,
    error_response,
    error_status,
    extract_error,
    ok_response,
    parse_transcribe_request,
    parse_transcribe_response,
)
from deskbot_server.utils.asr_segments import complete_segments, total_duration_ms
from deskbot_server.utils.audio import pcm_to_wav_bytes

PCM_1S = b"\x00\x00" * 16000  # 1s 静音 @16k


def _make_wav(pcm: bytes, sr: int = 16000, channels: int = 1, sampwidth: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sampwidth)
        wav.setframerate(sr)
        wav.writeframes(pcm)
    return buf.getvalue()


# ---------- 请求解析：PCM ----------


def test_parse_pcm_with_header():
    assert parse_transcribe_request(PCM_1S, "application/octet-stream", "16000") == (PCM_1S, 16000)


def test_parse_pcm_default_sample_rate():
    assert parse_transcribe_request(PCM_1S, "application/octet-stream", None) == (PCM_1S, 16000)
    assert parse_transcribe_request(PCM_1S, "application/octet-stream", "") == (PCM_1S, 16000)


def test_parse_pcm_invalid_sample_rate():
    for bad in ("abc", "-1", "0"):
        with pytest.raises(AsrProtocolError) as ei:
            parse_transcribe_request(PCM_1S, "application/octet-stream", bad)
        assert ei.value.code == ERR_INVALID_SAMPLE_RATE
        assert ei.value.http_status == 400


def test_parse_empty_body():
    with pytest.raises(AsrProtocolError) as ei:
        parse_transcribe_request(b"", "application/octet-stream")
    assert ei.value.code == ERR_EMPTY_PCM


def test_parse_unsupported_media_type():
    with pytest.raises(AsrProtocolError) as ei:
        parse_transcribe_request(PCM_1S, "audio/ogg")
    assert ei.value.code == ERR_UNSUPPORTED_MEDIA
    assert ei.value.http_status == 415


def test_parse_audio_too_large():
    with pytest.raises(AsrProtocolError) as ei:
        parse_transcribe_request(b"\x00\x00" * 16000 * 61, "application/octet-stream")
    assert ei.value.code == ERR_AUDIO_TOO_LARGE
    assert ei.value.http_status == 413


def test_parse_content_type_with_params():
    # 带 charset 等参数应忽略
    assert parse_transcribe_request(PCM_1S, "application/octet-stream; charset=binary", "16000")[1] == 16000


# ---------- 请求解析：WAV ----------


def test_parse_wav_self_describing():
    wav = pcm_to_wav_bytes(PCM_1S, 8000)
    assert parse_transcribe_request(wav, "audio/wav", "99999") == (PCM_1S, 8000)


def test_parse_wav_stereo_rejected():
    wav = _make_wav(PCM_1S * 2, channels=2)
    with pytest.raises(AsrProtocolError) as ei:
        parse_transcribe_request(wav, "audio/wav")
    assert ei.value.code == ERR_INVALID_WAV


def test_parse_wav_8bit_rejected():
    wav = _make_wav(b"\x80" * 16000, sampwidth=1)
    with pytest.raises(AsrProtocolError) as ei:
        parse_transcribe_request(wav, "audio/wav")
    assert ei.value.code == ERR_INVALID_WAV


def test_parse_wav_garbage_rejected():
    with pytest.raises(AsrProtocolError) as ei:
        parse_transcribe_request(b"not a wav file at all", "audio/wav")
    assert ei.value.code == ERR_INVALID_WAV


def test_parse_wav_no_data_rejected():
    wav = _make_wav(b"", sr=16000)
    with pytest.raises(AsrProtocolError) as ei:
        parse_transcribe_request(wav, "audio/wav")
    assert ei.value.code == ERR_INVALID_WAV


# ---------- 响应构造 ----------


def test_ok_response_minimal():
    assert ok_response("你好") == {"text": "你好"}


def test_ok_response_optional_fields_only_when_present():
    assert ok_response("x", language="zh", elapsed_ms=5) == {
        "text": "x",
        "language": "zh",
        "elapsed_ms": 5,
    }
    assert ok_response("x", language=None) == {"text": "x"}


def test_error_response_and_status():
    payload = error_response(ERR_EMPTY_PCM, "empty pcm")
    assert payload == {"error": {"code": "empty_pcm", "message": "empty pcm"}}
    assert error_status(ERR_UNSUPPORTED_MEDIA) == 415
    assert error_status("unknown_code") == 500


# ---------- 响应解析（客户端） ----------


def test_parse_response_ok():
    assert parse_transcribe_response({"text": "你好"}) == {"text": "你好"}


def test_parse_response_optional_fields_passthrough():
    out = parse_transcribe_response({"text": "x", "language": "zh", "confidence": 0.9, "elapsed_ms": 3})
    assert out == {"text": "x", "language": "zh", "confidence": 0.9, "elapsed_ms": 3}


def test_parse_response_missing_text_rejected():
    for payload in ({"nope": 1}, None, "text", []):
        with pytest.raises(AsrProtocolError) as ei:
            parse_transcribe_response(payload)  # type: ignore[arg-type]
        assert ei.value.code == ERR_INVALID_RESPONSE


def test_parse_response_text_not_str_rejected():
    with pytest.raises(AsrProtocolError) as ei:
        parse_transcribe_response({"text": 123})
    assert ei.value.code == ERR_INVALID_RESPONSE


def test_extract_error():
    assert extract_error({"error": {"code": "empty_pcm", "message": "x"}}) == ("empty_pcm", "x")
    assert extract_error({"text": "ok"}) is None
    assert extract_error({"error": "not a dict"}) is None


# ---------- segments 补全 ----------


def test_complete_segments_even_split():
    segs = complete_segments("你好。世界！", PCM_1S, 16000)
    assert segs == [
        {"start_ms": 0, "end_ms": 500, "text": "你好。"},
        {"start_ms": 500, "end_ms": 1000, "text": "世界！"},
    ]


def test_complete_segments_single_sentence():
    segs = complete_segments("没有标点的一句话", PCM_1S, 16000)
    assert segs == [{"start_ms": 0, "end_ms": 1000, "text": "没有标点的一句话"}]


def test_complete_segments_proportional_by_chars():
    segs = complete_segments("一二三四五。六七八九十。", PCM_1S, 16000)
    assert len(segs) == 2
    assert segs[0] == {"start_ms": 0, "end_ms": 500, "text": "一二三四五。"}
    assert segs[1] == {"start_ms": 500, "end_ms": 1000, "text": "六七八九十。"}


def test_complete_segments_sum_conserved_last_eats_remainder():
    # 1000ms 音频：0.3333s 等分会有舍入，末段吃掉余量保证 end == total
    pcm = b"\x00\x00" * 8000  # 500ms @16k
    segs = complete_segments("甲。乙。丙。", pcm, 16000)
    assert len(segs) == 3
    assert segs[-1]["end_ms"] == total_duration_ms(pcm, 16000)
    for a, b in zip(segs, segs[1:]):
        assert a["end_ms"] == b["start_ms"]


def test_complete_segments_non_zero_width_when_text_long():
    # 文本比音频长：每段至少 1ms，总和守恒
    pcm = b"\x00\x00" * 800  # 50ms @16k
    segs = complete_segments("一。二。三。四。五。", pcm, 16000)
    assert len(segs) == 5
    assert all(s["end_ms"] > s["start_ms"] for s in segs)
    assert segs[-1]["end_ms"] == 50


def test_complete_segments_empty_edges():
    assert complete_segments("", PCM_1S, 16000) == []
    assert complete_segments("你好", b"", 16000) == []


def test_total_duration_ms():
    assert total_duration_ms(b"\x00\x00" * 16000, 16000) == 1000
    assert total_duration_ms(b"", 16000) == 0
    assert total_duration_ms(b"\x00\x00" * 10, 0) >= 0  # 非法采样率不炸（兜底 sr=1）
