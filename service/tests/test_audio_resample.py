"""audio_resample 单测：等采样率直通 / 时长 / 直流 / 带内保真 / 带外衰减。"""

from __future__ import annotations

import numpy as np
import pytest

from deskbot_server.utils.audio_resample import resample_pcm16_mono


def _sine_pcm(rate: int, seconds: float, freq: float, amp: float = 0.5) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    x = (amp * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2")
    return x.tobytes()


def _rms_db(pcm: bytes) -> float:
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    return 20 * np.log10(max(np.sqrt(np.mean(x**2)), 1e-9) / 32767)


def test_same_rate_returns_original():
    pcm = _sine_pcm(24000, 0.2, 1000)
    assert resample_pcm16_mono(pcm, 24000, 24000) is pcm


def test_empty_returns_empty():
    assert resample_pcm16_mono(b"", 48000, 24000) == b""


def test_half_length_and_duration():
    pcm = _sine_pcm(48000, 0.5, 1000)
    out = resample_pcm16_mono(pcm, 48000, 24000)
    n_in = len(pcm) // 2
    n_out = len(out) // 2
    # 长度按时长比例取整（±1 样本）
    assert abs(n_out - n_in / 2) <= 1
    ms_in = n_in * 1000 // 48000
    ms_out = n_out * 1000 // 24000
    assert abs(ms_out - ms_in) <= 1


def test_low_freq_preserved_within_0_5db():
    pcm = _sine_pcm(48000, 0.5, 1000)  # 1kHz 远低于 12kHz 新 Nyquist，应无损通过
    out = resample_pcm16_mono(pcm, 48000, 24000)
    assert abs(_rms_db(out) - _rms_db(pcm)) < 0.5


def test_high_freq_attenuated():
    pcm = _sine_pcm(48000, 0.5, 20000)  # 20kHz 在新采样率下应被抗混叠滤除
    out = resample_pcm16_mono(pcm, 48000, 24000)
    assert _rms_db(out) < -30.0


def test_48k_to_16k_three_to_one():
    """豆包/moss 统一下发 16k：3:1 降采样，时长与带内保真。"""
    pcm = _sine_pcm(48000, 0.5, 2000)
    out = resample_pcm16_mono(pcm, 48000, 16000)
    n_in = len(pcm) // 2
    n_out = len(out) // 2
    assert abs(n_out - n_in / 3) <= 1
    assert abs(_rms_db(out) - _rms_db(pcm)) < 0.6
    # 截止 0.45×(16/48)×48k ≈ 7.2kHz，97 抽头过渡带到 ~9.3kHz：
    # 10kHz 应在阻带内（<-35dB），7.5kHz 过渡带内仅需明显衰减
    out_hi = resample_pcm16_mono(_sine_pcm(48000, 0.5, 10000), 48000, 16000)
    assert _rms_db(out_hi) < -35.0
    out_mid = resample_pcm16_mono(_sine_pcm(48000, 0.5, 7500), 48000, 16000)
    assert _rms_db(out_mid) < -10.0


def test_dc_preserved():
    x = (np.full(24000, 8000, dtype="<i2")).tobytes()  # 0.5s @48k，DC=8000
    out = resample_pcm16_mono(x, 48000, 24000)
    y = np.frombuffer(out, dtype="<i2")
    assert abs(float(np.mean(y)) - 8000.0) < 50.0


def test_upsample_rejected():
    with pytest.raises(ValueError):
        resample_pcm16_mono(_sine_pcm(24000, 0.1, 1000), 24000, 48000)
