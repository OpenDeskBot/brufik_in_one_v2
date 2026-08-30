"""ASR 句子级时间戳补全：``text`` + 原始音频 → segments 时间轴。

协议（docs/asr_protocol.md）输出不包含时间戳，需要句子级时间轴（打断、字幕等）
时由主服务内部统一补全，与 ASR 后端无关：按句末标点切句 → 按字符数等分总时长 →
末段吃舍入余量，保证 ``end_ms == total_ms`` 总和守恒。

与 utils/phoneme_duration.py 是同一"文本+总时长→时间轴"算法思路，但**不共享代码**：
本模块是句子级、字符等权，比音素级简单，各自独立实现。
"""

from __future__ import annotations

from deskbot_server.infrastructure.tts.text_split import split_tts_by_punctuation

MIN_SEGMENT_MS = 1  # 防止音频过短/文本过长时出现零宽段


def total_duration_ms(pcm_bytes: bytes, sample_rate: int) -> int:
    """PCM int16 单声道音频总时长（ms）。"""
    return int(len(pcm_bytes) / 2 / max(1, sample_rate) * 1000)


def complete_segments(text: str, pcm_bytes: bytes, sample_rate: int) -> list[dict]:
    """``text`` + 音频 → ``[{"start_ms": int, "end_ms": int, "text": str}]``。

    - 切句：复用 ``split_tts_by_punctuation``（按句末标点；无标点整句单段）
    - 分配：按每句字符数等权分摊总时长，末段吃掉舍入余量（合计 == total_ms）
    - 边界：文本空或音频总时长 <= 0 → ``[]``
    """
    sentences = split_tts_by_punctuation(text)
    total_ms = total_duration_ms(pcm_bytes, sample_rate)
    if not sentences or total_ms <= 0:
        return []

    total_weight = sum(len(s) for s in sentences) or 1
    segments: list[dict] = []
    cursor = 0
    n = len(sentences)
    for i, sentence in enumerate(sentences):
        if i == n - 1:
            end = total_ms
        else:
            end = int(total_ms * sum(len(s) for s in sentences[: i + 1]) / total_weight)
        end = min(total_ms, max(end, cursor + MIN_SEGMENT_MS))
        segments.append({"start_ms": cursor, "end_ms": end, "text": sentence})
        cursor = end
    return segments
