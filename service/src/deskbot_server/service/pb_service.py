"""PbService：PbSeq 构建服务（单例）。

提供两个核心功能：
1. 按场景名获取 PbSeq —— 供 LiveService 等直接入队发送。
2. 通过 LLM + TTS 结果组装 PbSeq —— 供 chat_flow 等直接入队发送。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from typing import Any

from deskbot_server.dao.face_expr_scenes_store import find_design_scene_by_name, load_face_expr_scenes_file
from deskbot_server.model.pb_seq import PbAction, PbAnim, PbBlock, PbSeq, PbType
from deskbot_server.pb.shapes import PB_ACTION_REPLACE, PB_LEVEL_IDLE, PB_LEVEL_TASK
from deskbot_server.pb.wire import build_pb_wire_pairs
from deskbot_server.utils.singleton import SingletonMeta

logger = logging.getLogger("deskbot-server")


class PbService(metaclass=SingletonMeta):
    """构建播放序列；资源经 DAO 查询，合成和网络发送由调用方负责。"""

    # ------------------------------------------------------------------
    # 功能1：按场景名获取 PbSeq
    # ------------------------------------------------------------------

    def build_servo_pb_seq(self, head: PbBlock, *, level: int, action: PbAction, scene_name: str = "") -> PbSeq:
        """Compose one command with a single request and one completion barrier.

        The initial display hold aligns the optional scene after the servo.
        Render primitives stay in the downloaded package, just as with speech.
        An unavailable optional scene does not discard the servo command.
        """
        from deskbot_server.dao.face_asset_catalog import find_face_asset

        local = find_face_asset(scene_name) if scene_name else None
        if local is None:
            return PbSeq(req=head.req, entries=(head,), level=level, action=action)
        asset, version = local
        frames = asset["frame_ms"]
        entries = [replace(head, type=PbType.START, idx=0, anim=(PbAnim(ms=head.chunk_ms),))]
        for i, duration in enumerate(frames):
            entries.append(
                PbBlock(
                    type=PbType.END if i == len(frames) - 1 else PbType.CHUNK,
                    req=head.req,
                    idx=i + 1,
                    chunk_ms=duration,
                    anim=(PbAnim(anim_id=asset["id"], frame=i, ms=duration),),
                    anim_version=version,
                )
            )
        return PbSeq(req=head.req, entries=tuple(entries), level=level, action=action)

    def get_scene_pb_seq(
        self,
        scene_name: str,
        *,
        device_id: str | None = None,
        level: int = PB_LEVEL_IDLE,
        action: PbAction = PbAction.REPLACE,
    ) -> PbSeq | None:
        """从 ``deskbot-face.json`` 加载指定场景，返回可直接入队的 ``PbSeq``。

        Args:
            scene_name: 场景名（不区分大小写）。
            device_id: 设备 ID（用于加载设备特定配置）。
            level: 序列优先级（默认 idle=0）。
            action: 队列调度策略（默认 REPLACE）。

        Returns:
            场景 PbSeq；场景不存在或无有效帧时返回 ``None``。
        """
        rows = load_face_expr_scenes_file(seed_if_missing=True, device_id=device_id) or []
        ent = find_design_scene_by_name(rows, scene_name)
        if ent is None:
            logger.debug("[pb_service] 场景 %r 未找到 device_id=%s", scene_name, device_id)
            return None

        from deskbot_server.dao.face_asset_catalog import find_face_asset

        local = find_face_asset(scene_name)
        if local is None:
            raise ValueError(f"animation {scene_name!r} is not compiled into the firmware")
        asset, version = local
        req_id = uuid.uuid4().hex[:16]
        return PbSeq(
            req=req_id,
            level=level,
            action=action,
            entries=(
                PbBlock(
                    type=PbType.SINGLE,
                    req=req_id,
                    anim_id=asset["id"],
                    anim_version=version,
                    chunk_ms=asset["duration_ms"],
                ),
            ),
        )

    # ------------------------------------------------------------------
    # 功能2：通过 LLM + TTS 结果组装 PbSeq
    # ------------------------------------------------------------------

    def build_pb_seq_from_tts(
        self,
        segs: list[dict[str, Any]],
        tts_cfg: dict[str, Any],
        *,
        moves: list[dict[str, Any]] | None = None,
        anims: list[dict[str, Any]] | None = None,
        sample_rate: int,
        request_id: str | None = None,
        device_id: str | None = None,
        level: int = PB_LEVEL_TASK,
        action: str = PB_ACTION_REPLACE,
        volume: int | None = None,
        leading_move_steps: int = 0,
        random_servo_cfg: dict[str, Any] | None = None,
        servo_plan: list[dict[str, Any]] | None = None,
    ) -> PbSeq | None:
        """将 TTS 音频分片 + LLM moves/anims 组装为可直接入队的 ``PbSeq``。

        Args:
            segs: TTS 音频分片列表（含 ``pcm`` / ``ms``；忽略音素标签）。
            tts_cfg: TTS 配置（含 ``sample_rate`` 等）。
            moves: LLM 返回的舵机动作列表。
            anims: LLM 返回的表情动画列表。
            sample_rate: 音频采样率。
            request_id: 请求 ID（不传则自动生成）。
            device_id: 设备 ID。
            level: 序列优先级（默认 task=1）。
            action: 队列调度策略（默认 replace）。
            volume: 音量 0-100。
            leading_move_steps: 前置舵机步数。
            random_servo_cfg: 随机舵机配置。
            servo_plan: 舵机计划（与 moves 互斥）。

        Returns:
            组装好的 PbSeq；无有效分片时返回 ``None``。
        """
        if not segs:
            return None

        pairs, pb_req, n_pb, sr_pb = build_pb_wire_pairs(
            segs,
            tts_cfg,
            servo_plan=servo_plan,
            moves=moves,
            anims=anims,
            sample_rate=sample_rate,
            request_id=request_id,
            random_servo_cfg=random_servo_cfg,
            volume=volume,
            device_id=device_id,
            action=action,
            leading_move_steps=leading_move_steps,
        )
        if not pairs:
            return None
        # 从 wire 帧中读取实际 level（build_pb_wire_pairs 可能写入了不同值）
        task_level = int(pairs[0][0].get("level") or level) if pairs else level
        pb_seq = PbSeq.from_wire_pairs(pairs, level=task_level)
        logger.info(
            "[pb_service] tts assembled req=%s level=%d blocks=%d sr=%d",
            pb_seq.req,
            pb_seq.level,
            pb_seq.block_count,
            sr_pb,
        )
        return pb_seq
