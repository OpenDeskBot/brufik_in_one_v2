"""mono s16le PCM 降采样（windowed-sinc 抗混叠；统一下发采样率用）。

设备端 opus 是"整块解码→播放"（见 hardware firmware speaker.cpp），解码
突发必须小于 I2S DMA 环覆盖才不会在块边界欠载：采样率越高，解码量线性
变大而环覆盖时长反比变小。本模块把各 provider 引擎输出的 PCM 在服务端
统一降采样到 ``settings.tts.sample_rate``（默认 16k，与豆包一致，解码
预算余量最大；24k/48k 会进入边缘带出现偶发断点）。

只支持降采样（dst <= src）；等采样率直接返回原字节。
实现为 Kaiser 窗 sinc 低通（抗混叠）+ 线性插值取点，无第三方依赖（numpy）。
"""

from __future__ import annotations

import numpy as np

_LOWPASS_TAPS = 97  # 窗长：48k→16k 等降采样下过渡带/阻带足够，成本 ~百ms 级/10s 音频
_KAISER_BETA = 8.6  # 阻带 ~ -70dB


def _design_lowpass(cutoff: float) -> np.ndarray:
    """Kaiser 窗 sinc 低通（cutoff 为输入采样率归一化频率，0..0.5）。"""
    n = np.arange(_LOWPASS_TAPS) - (_LOWPASS_TAPS - 1) / 2.0
    h = np.sinc(2.0 * cutoff * n) * np.kaiser(_LOWPASS_TAPS, _KAISER_BETA)
    h /= h.sum()
    return h.astype(np.float32)


def resample_pcm16_mono(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """mono s16le PCM 由 ``src_rate`` 降采样到 ``dst_rate``。

    - ``dst_rate == src_rate``：原样返回（零拷贝）。
    - ``dst_rate > src_rate``：不支持（抛 ValueError）。
    - 输出长度按时长比例取整，总时长与输入偏差 < 1ms。
    """
    if not pcm:
        return b""
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError(f"invalid rates: src={src_rate} dst={dst_rate}")
    if dst_rate == src_rate:
        return pcm
    if dst_rate > src_rate:
        raise ValueError(f"resample_pcm16_mono 仅支持降采样: {src_rate} -> {dst_rate}")

    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    n = x.size
    if n == 0:
        return b""
    ratio = dst_rate / float(src_rate)

    # 抗混叠低通：截止取 0.45 × 目标 Nyquist（归一化到输入采样率）
    cutoff = 0.45 * ratio
    h = _design_lowpass(cutoff)
    if n < _LOWPASS_TAPS:  # 极短包：边缘反射填充避免截断
        xf = np.convolve(np.pad(x, _LOWPASS_TAPS // 2, mode="reflect"), h, mode="same")
        xf = xf[_LOWPASS_TAPS // 2 : _LOWPASS_TAPS // 2 + n]
    else:
        xf = np.convolve(x, h, mode="same")

    # 在滤波后的连续信号上按目标网格取点（带内已被低通限带，线性插值足够）
    out_n = max(1, int(round(n * ratio)))
    pos = np.arange(out_n, dtype=np.float64) * (n / float(out_n))
    y = np.interp(pos, np.arange(n, dtype=np.float64), xf)

    clipped = np.clip(np.rint(y), -32768.0, 32767.0).astype("<i2")
    return clipped.tobytes()
