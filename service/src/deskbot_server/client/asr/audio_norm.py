"""ASR 测试音频归一化：任意 WAV / raw PCM → 单声道 16-bit PCM（重采样 16k）。

语义对齐 external/manager.py::_asr_sample（8bit 扩展 / 多声道取左声道 / 非 WAV
视为 16k raw PCM），并在此基础上统一重采样到 16k——funasr pytorch 路径不内部
重采样，豆包一句话识别只支持 8k/16k，客户端归一化后两个 provider 都能直接使用。

纯函数、无副作用，供 robot_capability 的 ASR 测试使用。
"""

from __future__ import annotations

import array
import io
import wave

import numpy as np

MAX_TEST_AUDIO_BYTES = 16 * 1024 * 1024  # 对齐 external/manager.MAX_ASR_TEST_AUDIO_BYTES
DEFAULT_PCM_SAMPLE_RATE = 16000  # 非 WAV 容器的 raw PCM 视为 16k（契约默认）
MAX_AUDIO_SECONDS = 60.0  # funasr 协议与豆包一句话识别共用上限


def parse_audio_pcm(raw: bytes) -> tuple[bytes, int]:
    """解析测试音频 → (pcm int16 LE mono, sample_rate)。

    - RIFF/WAVE 容器：wave 模块解析；8bit 扩展为 16bit、多声道取左声道
      （int16 交错样本 ``[::channels]``），保留真实采样率
    - 非 WAV 容器：视为 raw PCM int16 LE，采样率 16000

    Raises:
        ValueError: 空 / 超大 / 解析失败 / 非 PCM 编码 / 不支持位深 / 无音频数据。
    """
    if not raw:
        raise ValueError("音频内容为空")
    if len(raw) > MAX_TEST_AUDIO_BYTES:
        raise ValueError(f"音频过大（>{MAX_TEST_AUDIO_BYTES // (1024 * 1024)}MB）")

    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(raw), "rb") as wav:
                channels = wav.getnchannels()
                sample_rate = wav.getframerate()
                sampwidth = wav.getsampwidth()
                comptype = wav.getcomptype()
                frames = wav.readframes(wav.getnframes())
        except Exception as exc:
            raise ValueError(f"WAV 解析失败: {exc}") from exc
        if not frames:
            raise ValueError("WAV 音频内容为空")
        if comptype != "NONE":
            raise ValueError(f"WAV 非 PCM 编码: {comptype}")
        if sampwidth == 1:
            frames = bytes(b for byte in frames for b in (byte, 0))  # 8bit → 16bit
        elif sampwidth != 2:
            raise ValueError(f"不支持的 WAV 位深: {sampwidth * 8}bit（仅 8/16bit）")
        # 立体声/多声道 → 取左声道（int16 交错样本 [::channels]）
        pcm = array.array("h", frames)[0::channels].tobytes() if channels > 1 else frames
        return pcm, sample_rate

    return raw, DEFAULT_PCM_SAMPLE_RATE  # 非 WAV 容器：raw PCM 透传


def resample_pcm(pcm: bytes, from_sr: int, to_sr: int = DEFAULT_PCM_SAMPLE_RATE) -> bytes:
    """重采样到 16k（默认），保持 int16 LE 单声道。

    - ``from_sr == to_sr``：原样返回
    - 整数比降采样（如 48k→16k 为 3:1）：先按比例均值低通再抽样（廉价抗混叠）
    - 其余：``np.interp`` 线性插值

    Raises:
        ValueError: 采样率非法 / pcm 字节数非 2 的倍数。
    """
    if from_sr <= 0 or to_sr <= 0:
        raise ValueError(f"非法采样率: {from_sr} → {to_sr}")
    if from_sr == to_sr:
        return pcm
    if len(pcm) % 2:
        raise ValueError("PCM 字节数非 2 的倍数")
    n_in = len(pcm) // 2
    if n_in == 0:
        raise ValueError("PCM 音频为空")
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)

    ratio = to_sr / from_sr
    n_out = max(1, round(n_in * ratio))
    if from_sr > to_sr and from_sr % to_sr == 0:
        # 整数比降采样：先均值低通（抗混叠），再抽样
        factor = from_sr // to_sr
        pad = (-n_in) % factor
        if pad:
            samples = np.concatenate([samples, np.zeros(pad, dtype=np.float32)])
        pooled = samples.reshape(-1, factor).mean(axis=1)
        out = pooled
    elif from_sr < to_sr and to_sr % from_sr == 0:
        # 整数比升采样：零阶保持（每样本重复）
        factor = to_sr // from_sr
        out = np.repeat(samples, factor)
    else:
        # 非整数比：线性插值
        out = np.interp(np.linspace(0, n_in - 1, n_out), np.arange(n_in), samples)

    out = np.clip(np.rint(out), -32768, 32767).astype(np.int16)
    return out.tobytes()


def normalize_test_audio(raw: bytes) -> tuple[bytes, int]:
    """测试入口：解析 + 重采样 → (pcm int16 LE mono 16k, 16000)。

    Raises:
        ValueError: 解析 / 重采样失败；>60s 音频。
    """
    pcm, sample_rate = parse_audio_pcm(raw)
    pcm = resample_pcm(pcm, sample_rate)
    duration_s = len(pcm) / 2 / DEFAULT_PCM_SAMPLE_RATE
    if duration_s > MAX_AUDIO_SECONDS:
        raise ValueError(f"音频超时: {duration_s:.1f}s > {MAX_AUDIO_SECONDS:.0f}s")
    return pcm, DEFAULT_PCM_SAMPLE_RATE
