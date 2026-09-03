from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_developer: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    devices: Mapped[list[Device]] = relationship(back_populates="owner")


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("device_id", name="uq_devices_device_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    access_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    volume: Mapped[int] = mapped_column(Integer, default=80, nullable=False)
    fps: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    auto_reply: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    servo_mode: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    live_mode: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 设备级 ASR provider：funasr（默认）/ doubao；空/NULL 按 funasr 处理
    asr_provider: Mapped[str] = mapped_column(String(32), default="funasr", server_default="funasr", nullable=False)
    # 设备级 ASR 参数（JSON：{"funasr": {"url"}, "doubao": {"api_key", ...}}），NULL=未配置
    asr_param: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 设备级 TTS provider：moss-tts-nano（默认）/ doubao；空/NULL 按 moss-tts-nano 处理
    tts_provider: Mapped[str] = mapped_column(String(32), default="moss-tts-nano", server_default="moss-tts-nano", nullable=False)
    # 设备级 TTS 参数（JSON：{"moss": {"demo_id"}, "doubao": {"api_key", ...}}），NULL=未配置
    tts_param: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 设备级 LLM provider：空=未配置（回落系统默认 config.yaml llm 段）；
    # minicpm / qwen = 本地固定端点；ark = 云端（密钥/模型在该设备 llm_param["ark"]）
    llm_provider: Mapped[str] = mapped_column(String(64), default="", server_default="", nullable=False)
    # 设备级 LLM 参数（JSON：{"ark": {api_key, model_name, base_url}, "context_window": N,
    # "native_tools": bool}；context_window 影响历史 token 预算），NULL=未配置
    llm_param: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 设备绑定的剧情剧本名（对应 data/quest/{quest_id}.json）；空/NULL=未绑定
    quest_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    owner: Mapped[User] = relationship(back_populates="devices")


class ScheduledTask(Base):
    """设备 cron 定时任务：由 LLM tools 创建，调度器按 next_run_at 触发。"""

    __tablename__ = "scheduled_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    cron_expr: Mapped[str] = mapped_column(String(128), nullable=False, default="* * * * *")
    task_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="once")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeviceUsage(Base):
    """设备每日用量统计。"""

    __tablename__ = "device_usage"
    __table_args__ = (UniqueConstraint("date", "device_id", name="uq_device_usage_date_device"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    asr: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    llm: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cv: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    tts: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class DeviceProfileFace(Base):
    """设备已注册人脸档案（原 face_profiles.json 迁入）。"""

    __tablename__ = "device_profile_face"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    descriptor: Mapped[str] = mapped_column(Text, nullable=False)
    descriptor_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="embedding")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class DeviceProfileVoice(Base):
    """设备已注册声纹档案（WeSpeaker 256 维 embedding，独立于 device_profile_face）。"""

    __tablename__ = "device_profile_voice"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    descriptor: Mapped[str] = mapped_column(Text, nullable=False)
    descriptor_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="voice")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class DeviceMemory(Base):
    """设备长期记忆（原 user_memory.json 迁入），注入 LLM system prompt。"""

    __tablename__ = "device_memory"
    __table_args__ = (
        UniqueConstraint("device_id", "title", name="uq_device_memory_device_title"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    parent: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeviceSession(Base):
    """设备对话 Session 头。"""

    __tablename__ = "device_session"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(64), nullable=False, default="新对话")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class DeviceSessionMessage(Base):
    """Session 内单条消息。"""

    __tablename__ = "device_session_message"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("device_session.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class QuestInstance(Base):
    """剧本任务实例：每设备每任务的运行状态、分数与结果。

    定义（goal/activation_score/on_success 等）存于剧本 JSON 文件，
    本表只存运行态：状态机 not_started → running → success/failed。
    """

    __tablename__ = "quest_instance"
    __table_args__ = (
        UniqueConstraint("device_id", "playbook", "task_id", name="uq_quest_instance_dev_play_task"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    playbook: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="not_started", index=True)
    current_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    strategy_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
