"""外部服务管理器：状态机、看门狗、自动重启、install、state.json 持久化。

状态机:
    NOT_INSTALLED → INSTALLING → STOPPED → STARTING → RUNNING
                                 ↘ FAILED（启动即崩，等用户）
    RUNNING → UNHEALTHY（healthcheck 连续失败；不杀进程——首次拉模型等
              慢启动场景进程活着慢慢就绪；恢复后自动回 RUNNING）
    RUNNING/UNHEALTHY 进程非预期退出 → 自动重启（指数退避），连续
    MAX_RESTARTS 次 → CRASH_LOOP（停止自愈，等用户干预）
    STARTING 宽限期内退出 → FAILED（配置/依赖问题，自动重启无意义）

并发：每个服务一把 asyncio.Lock，install/start/stop 与看门狗重启互斥。
进程跟随主服务生命周期；主服务被 SIGKILL 遗留的孤儿进程由 pid 文件
检测并在下次 start 时回收。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from deskbot_server.infrastructure.external.process_supervisor import ExternalProcess, SpawnConfig, pid_alive
from deskbot_server.service.external.contract import resolve_test_spec
from deskbot_server.service.external.manifest import HealthCheckConfig, ServiceManifest, discover_manifests

logger = logging.getLogger("deskbot-server")

SERVICE_ROOT = Path(__file__).resolve().parents[4]  # service/
EXTERNALS_DIR = SERVICE_ROOT / "externals"
DATA_DIR = SERVICE_ROOT / "data" / "services"
STATE_FILE = DATA_DIR / "state.json"

WATCHDOG_INTERVAL_S = 1.0
MAX_RESTARTS = 5
MAX_RESTART_BACKOFF_S = 60.0
MAX_LOG_SIZE = 20 * 1024 * 1024  # 日志超 20MB 轮转到 .1

# fr 契约测试默认样本：后台测试按钮优先用 data/test/face.jpg（用户可选本地图覆盖）
DEFAULT_FACE_TEST_IMAGE = SERVICE_ROOT / "data" / "test" / "face.jpg"
MAX_FACE_TEST_IMAGE_BYTES = 8 * 1024 * 1024  # 用户上传测试图上限 8MB

# asr 契约测试默认样本：data/test/asr.wav（用户可选本地音频覆盖）
DEFAULT_ASR_TEST_AUDIO = SERVICE_ROOT / "data" / "test" / "asr.wav"
MAX_ASR_TEST_AUDIO_BYTES = 16 * 1024 * 1024  # 用户上传测试音频上限 16MB


class ServiceState(Enum):
    NOT_INSTALLED = "not_installed"
    INSTALLING = "installing"
    STOPPED = "stopped"
    STARTING = "starting"
    RESTARTING = "restarting"
    RUNNING = "running"
    UNHEALTHY = "unhealthy"
    STOPPING = "stopping"
    FAILED = "failed"
    CRASH_LOOP = "crash_loop"


@dataclass
class ServiceSnapshot:
    """API 快照（进程内可变状态由 _ServiceHandle 维护）。"""

    name: str
    state: ServiceState
    installed: bool
    installed_version: str | None
    auto_start: bool
    description: str
    version: str
    service_type: str = ""
    port: int | None = None
    service_path: str = ""
    pid: int | None = None
    uptime_s: float | None = None
    healthy: bool | None = None
    restarts: int = 0
    exit_code: int | None = None
    last_error: str | None = None
    log_size: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "installed": self.installed,
            "installed_version": self.installed_version,
            "auto_start": self.auto_start,
            "description": self.description,
            "version": self.version,
            "service_type": self.service_type,
            "port": self.port,
            "service_path": self.service_path,
            "pid": self.pid,
            "uptime_s": round(self.uptime_s) if self.uptime_s is not None else None,
            "healthy": self.healthy,
            "restarts": self.restarts,
            "exit_code": self.exit_code,
            "last_error": self.last_error,
            "log_size": self.log_size,
        }


class ServiceError(RuntimeError):
    pass


@dataclass
class _ServiceHandle:
    manifest: ServiceManifest
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    proc: ExternalProcess | None = None

    state: ServiceState = ServiceState.NOT_INSTALLED
    desired: bool = False  # 用户期望运行（影响自动重启）
    start_ts: float | None = None
    restart_count: int = 0
    fail_count: int = 0  # healthcheck 连续失败次数
    last_check_ts: float = 0.0
    restart_after: float | None = None  # backoff 到期时间点（watchdog 触发重启）
    last_error: str | None = None
    exit_code: int | None = None

    def log_path(self, data_dir: Path) -> Path:
        return data_dir / self.manifest.name / f"{self.manifest.name}.log"

    def pid_path(self, data_dir: Path) -> Path:
        return data_dir / self.manifest.name / f"{self.manifest.name}.pid"


class ExternalServiceManager:
    """外部服务管理器（单例，由 bootstrap 装配并随主服务生命周期启停）。"""

    def __init__(self, externals_dir: Path | None = None, data_dir: Path | None = None) -> None:
        self.externals_dir = Path(externals_dir or EXTERNALS_DIR)
        self.data_dir = Path(data_dir or DATA_DIR)
        self.state_file = self.data_dir / "state.json"
        self._handles: dict[str, _ServiceHandle] = {}
        self._state: dict[str, dict] = {}  # {name: {installed_version, auto_start}}
        self._watchdog_task: asyncio.Task | None = None
        self._load_state()

    # ------------------------------------------------------------------ 装配

    def discover(self) -> None:
        """（重新）扫描 externals 目录，注册 manifest。运行期重扫用于热加服务。"""
        self._handles = {
            m.name: _ServiceHandle(manifest=m) for m in discover_manifests(self.externals_dir)
        }
        for name, handle in self._handles.items():
            persisted = self._state.get(name) or {}
            handle.manifest.auto_start = bool(persisted.get("auto_start", handle.manifest.auto_start))
            # 恢复持久化事实：已安装（state.json 有 installed_version）→ 未启动；
            # 否则重启后会误显示"未安装"（installed 是持久事实，state 是运行时状态）
            if "installed_version" in persisted:
                handle.state = ServiceState.STOPPED

    def start_watchdog(self) -> None:
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.get_running_loop().create_task(self._watchdog_loop())
            logger.info("[external] watchdog started (%d manifests)", len(self._handles))

    async def shutdown(self) -> None:
        """停看门狗 + 停止全部运行中服务（主服务退出路径）。"""
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task
            self._watchdog_task = None
        for name in list(self._handles):
            try:
                await self.stop(name)
            except Exception:
                logger.exception("[external] shutdown stop %s failed", name)

    async def apply_auto_start(self) -> None:
        """主服务启动时拉起所有 auto_start 服务（不阻塞启动完成）。"""
        for handle in self._handles.values():
            if not handle.manifest.auto_start:
                continue
            try:
                await self.start(handle.manifest.name)
            except ServiceError as exc:
                logger.error("[external] auto_start %s 失败: %s", handle.manifest.name, exc)

    # ------------------------------------------------------------------ 状态

    def _load_state(self) -> None:
        if not self.state_file.is_file():
            self._state = {}
            return
        try:
            raw = __import__("json").loads(self.state_file.read_text(encoding="utf-8"))
            self._state = raw.get("services", {}) if isinstance(raw, dict) else {}
        except Exception:
            logger.exception("[external] state.json 解析失败，按空状态处理")
            self._state = {}

    def _save_state(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        import json

        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"services": self._state}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_file)

    def _persisted(self, name: str) -> dict:
        return self._state.setdefault(name, {})

    def snapshot(self, name: str) -> ServiceSnapshot | None:
        handle = self._handles.get(name)
        if handle is None:
            return None
        persisted = self._persisted(name)
        healthy = None
        if handle.proc is not None and handle.proc.is_running():
            if handle.manifest.healthcheck is None:
                healthy = True
            elif handle.state in (ServiceState.RUNNING, ServiceState.STARTING, ServiceState.UNHEALTHY):
                healthy = handle.state != ServiceState.UNHEALTHY
        try:
            service_path = handle.manifest.source_dir.relative_to(SERVICE_ROOT).as_posix()
        except ValueError:
            service_path = str(handle.manifest.source_dir)
        return ServiceSnapshot(
            name=name,
            state=handle.state,
            installed="installed_version" in persisted,
            installed_version=persisted.get("installed_version"),
            auto_start=bool(persisted.get("auto_start", handle.manifest.auto_start)),
            description=handle.manifest.description,
            version=handle.manifest.version,
            service_type=handle.manifest.service_type,
            port=handle.manifest.port,
            service_path=service_path,
            pid=handle.proc.pid if handle.proc else None,
            uptime_s=(time.monotonic() - handle.start_ts) if handle.start_ts else None,
            healthy=healthy,
            restarts=handle.restart_count,
            exit_code=handle.exit_code,
            last_error=handle.last_error,
            log_size=self._log_size(name),
        )

    def status_all(self) -> list[ServiceSnapshot]:
        return [s for s in (self.snapshot(n) for n in self._handles) if s is not None]

    def names(self) -> list[str]:
        return sorted(self._handles)

    # ------------------------------------------------------------------ 操作

    async def install(self, name: str) -> ServiceSnapshot:
        handle = self._require(name)
        async with handle.lock:
            if handle.state in (ServiceState.INSTALLING, ServiceState.STARTING, ServiceState.RESTARTING):
                raise ServiceError(f"{name} 正在安装/启动，无法重复安装")
            commands = handle.manifest.install
            if not commands:
                # 无 install 段视为"无需安装"（如依赖系统已有）
                self._mark_installed(handle)
                return self.snapshot(name)  # type: ignore[return-value]
            self._set_state(handle, ServiceState.INSTALLING, error=None)
            log_path = self._prepare_log(handle)
            try:
                for cmd in commands:
                    logger.info("[external] %s install: %s", name, cmd)
                    log_fd = os.open(str(log_path), os.O_CREAT | os.O_WRONLY | os.O_APPEND)
                    try:
                        proc = await asyncio.create_subprocess_shell(
                            cmd,
                            cwd=str(SERVICE_ROOT),
                            stdout=log_fd,
                            stderr=subprocess.STDOUT,
                        )
                    finally:
                        os.close(log_fd)
                    await proc.wait()
                    if proc.returncode != 0:
                        raise ServiceError(f"install 命令失败 (exit={proc.returncode}): {cmd}")
                self._mark_installed(handle)
                self._set_state(handle, ServiceState.STOPPED)
            except ServiceError:
                self._set_state(handle, ServiceState.FAILED)
                raise
            except Exception as exc:
                self._set_state(handle, ServiceState.FAILED, error=str(exc))
                raise ServiceError(f"install 异常: {exc}") from exc
            return self.snapshot(name)  # type: ignore[return-value]

    def _mark_installed(self, handle: _ServiceHandle) -> None:
        persisted = self._persisted(handle.manifest.name)
        persisted["installed_version"] = handle.manifest.version
        self._save_state()

    async def uninstall(self, name: str) -> ServiceSnapshot:
        """卸载：停止（若运行）→ 执行 manifest uninstall 命令（可选，失败仅告警）
        → 删除数据目录（日志/pid）→ 清安装状态（回到 not_installed）。"""
        handle = self._require(name)
        async with handle.lock:
            handle.desired = False
            handle.restart_after = None
            if handle.proc is not None and handle.proc.is_running():
                self._set_state(handle, ServiceState.STOPPING)
                await handle.proc.terminate()
                handle.proc.drop()
                handle.proc = None
            log_path = self._prepare_log(handle)
            for cmd in handle.manifest.uninstall:
                logger.info("[external] %s uninstall: %s", name, cmd)
                log_fd = os.open(str(log_path), os.O_CREAT | os.O_WRONLY | os.O_APPEND)
                try:
                    proc = await asyncio.create_subprocess_shell(
                        cmd,
                        cwd=str(SERVICE_ROOT),
                        stdout=log_fd,
                        stderr=subprocess.STDOUT,
                    )
                finally:
                    os.close(log_fd)
                await proc.wait()
                if proc.returncode != 0:
                    # 清理命令失败不阻塞卸载：状态照清，日志留证据
                    logger.error("[external] %s uninstall 命令失败 (exit=%s): %s", name, proc.returncode, cmd)
            with contextlib.suppress(OSError):
                shutil.rmtree(handle.log_path(self.data_dir).parent)
            self._state.pop(name, None)
            self._save_state()
            self._set_state(handle, ServiceState.NOT_INSTALLED)
            return self.snapshot(name)  # type: ignore[return-value]

    async def start(self, name: str) -> ServiceSnapshot:
        handle = self._require(name)
        async with handle.lock:
            if handle.proc is not None and handle.proc.is_running():
                return self.snapshot(name)  # type: ignore[return-value] 已在运行
            if handle.state == ServiceState.INSTALLING:
                raise ServiceError(f"{name} 正在安装中")
            self._ensure_installed(handle)
            self._reap_orphan(handle)
            handle.desired = True
            handle.restart_count = 0
            handle.fail_count = 0
            handle.last_error = None
            handle.restart_after = None
            await self._spawn_locked(handle)
            return self.snapshot(name)  # type: ignore[return-value]

    async def stop(self, name: str) -> ServiceSnapshot:
        handle = self._require(name)
        async with handle.lock:
            handle.desired = False
            handle.restart_after = None
            if handle.proc is None or not handle.proc.is_running():
                self._set_state(handle, ServiceState.STOPPED)
                return self.snapshot(name)  # type: ignore[return-value]
            self._set_state(handle, ServiceState.STOPPING)
            await handle.proc.terminate()
            handle.proc.drop()
            handle.proc = None
            self._set_state(handle, ServiceState.STOPPED)
            return self.snapshot(name)  # type: ignore[return-value]

    async def restart(self, name: str) -> ServiceSnapshot:
        await self.stop(name)
        return await self.start(name)

    def set_auto_start(self, name: str, enabled: bool) -> ServiceSnapshot:
        """持久化 auto_start 覆盖值（manifest 默认值 + 用户配置）。"""
        handle = self._require(name)
        self._persisted(name)["auto_start"] = bool(enabled)
        handle.manifest.auto_start = bool(enabled)
        self._save_state()
        return self.snapshot(name)  # type: ignore[return-value]

    # ------------------------------------------------------------------ 契约测试

    async def test_service(
        self,
        name: str,
        image_base64: str | None = None,
        audio_base64: str | None = None,
        text: str | None = None,
        demo_id: str | None = None,
    ) -> dict:
        """按测试规格（manifest test 覆盖或类型契约）发标准样本请求并校验响应
        （不要求服务已安装/运行）。

        fr：image_base64 传用户选的测试图片（base64 编码的 JPEG/PNG bytes）；
        不传则用 data/test/face.jpg（存在时），否则回退规格内置 1x1 样本。
        asr：audio_base64 传用户选的测试音频（WAV 容器或原始 PCM int16 LE）；
        不传则用 data/test/asr.wav（存在时），否则回退规格内置静音样本。
        tts：text 传用户自定义合成文本、demo_id 传音色；不传用 manifest test 或契约内置值。
        默认样本命中时响应携带 *base64/*name/*path 供前端预览。
        """
        handle = self._require(name)
        spec = resolve_test_spec(handle.manifest.service_type, handle.manifest.port, handle.manifest.test)
        payload = {
            "service": name,
            "type": handle.manifest.service_type,
            "contract": spec["description"],
            "request": {"method": spec["method"], "url": spec["url"], "headers": spec["headers"]},
            "curl": spec["curl"],
            "ok": False,
        }
        body = spec["body"]
        if handle.manifest.service_type == "fr":
            body, sample = self._fr_sample(image_base64, fallback=spec["body"])
            if sample:
                payload.update(sample)
        elif handle.manifest.service_type == "tts" and (text is not None or demo_id is not None):
            overrides: dict = {}
            if text is not None:
                overrides["text"] = text
            if demo_id is not None:
                overrides["demo_id"] = demo_id
            spec = resolve_test_spec(
                handle.manifest.service_type,
                handle.manifest.port,
                handle.manifest.test,
                body_overrides=overrides,
            )
            payload["request"] = {"method": spec["method"], "url": spec["url"], "headers": spec["headers"]}
            payload["curl"] = spec["curl"]
            body = spec["body"]
        elif handle.manifest.service_type == "asr":
            body, sample_rate, sample = self._asr_sample(audio_base64, fallback=spec["body"])
            if sample_rate:
                # 重新解析规格：X-Sample-Rate 用样本真实采样率，curl 命令同步
                spec = resolve_test_spec(
                    handle.manifest.service_type,
                    handle.manifest.port,
                    handle.manifest.test,
                    headers_override={"X-Sample-Rate": str(sample_rate)},
                )
                payload["request"] = {"method": spec["method"], "url": spec["url"], "headers": spec["headers"]}
                payload["curl"] = spec["curl"]
            if sample:
                payload.update(sample)
        elif handle.manifest.service_type == "vpr":
            b64, sample_rate, sample = self._vpr_sample(audio_base64)
            if b64:
                # 重新解析规格：JSON body 的 audio_base64/sample_rate 用样本真实值，curl 同步
                spec = resolve_test_spec(
                    handle.manifest.service_type,
                    handle.manifest.port,
                    handle.manifest.test,
                    body_overrides={"audio_base64": b64, "sample_rate": sample_rate},
                )
                payload["request"] = {"method": spec["method"], "url": spec["url"], "headers": spec["headers"]}
                payload["curl"] = spec["curl"]
                body = spec["body"]
            if sample:
                payload.update(sample)
        try:
            result = await asyncio.to_thread(
                self._probe_contract,
                spec["url"],
                spec["method"],
                spec["headers"],
                body,
                spec["expect"],
            )
        except RuntimeError as exc:
            payload["error"] = str(exc)
            return payload
        payload.update(result)
        return payload

    async def test_info(self, name: str) -> dict:
        """测试契约元信息（不发起请求）：契约描述 / 请求 / 可复制的 curl 命令。
        管理后台「测试」对话框打开时展示，用户确认后再执行测试。
        fr/asr：附带默认测试样本（face.jpg / asr.wav 存在时）供前端打开即预览。"""
        handle = self._require(name)
        spec = resolve_test_spec(handle.manifest.service_type, handle.manifest.port, handle.manifest.test)
        info = {
            "service": name,
            "type": handle.manifest.service_type,
            "contract": spec["description"],
            "request": {
                "method": spec["method"],
                "url": spec["url"],
                "headers": spec["headers"],
                "body": spec["body_desc"],
            },
            "curl": spec["curl"],
            "curl_hint": spec["curl_hint"],
        }
        if handle.manifest.service_type == "fr":
            _, sample = self._fr_sample(None, fallback=spec["body"])
            if sample:
                info.update(sample)
        elif handle.manifest.service_type == "asr":
            _, sample_rate, sample = self._asr_sample(None, fallback=spec["body"])
            if sample_rate:
                # 请求头/curl 用默认样本真实采样率（如 asr.wav 48kHz）
                spec = resolve_test_spec(
                    handle.manifest.service_type,
                    handle.manifest.port,
                    handle.manifest.test,
                    headers_override={"X-Sample-Rate": str(sample_rate)},
                )
                info["request"] = {
                    "method": spec["method"],
                    "url": spec["url"],
                    "headers": spec["headers"],
                    "body": spec["body_desc"],
                }
                info["curl"] = spec["curl"]
            if sample:
                info.update(sample)
        elif handle.manifest.service_type == "vpr":
            b64, sample_rate, sample = self._vpr_sample(None)
            if b64:
                # 请求体/curl 用默认样本真实采样率（如 asr.wav 48kHz）
                spec = resolve_test_spec(
                    handle.manifest.service_type,
                    handle.manifest.port,
                    handle.manifest.test,
                    body_overrides={"audio_base64": b64, "sample_rate": sample_rate},
                )
                info["request"] = {
                    "method": spec["method"],
                    "url": spec["url"],
                    "headers": spec["headers"],
                    "body": spec["body_desc"],
                }
                info["curl"] = spec["curl"]
            if sample:
                info.update(sample)
        elif handle.manifest.service_type == "tts":
            info["text"] = self._default_tts_text(handle.manifest)
            voices, default_id = self._tts_voices(handle.manifest)
            if voices:
                info["voices"] = voices
                info["demo_id"] = default_id
        return info

    @staticmethod
    def _default_tts_text(manifest) -> str:
        """tts 默认测试文本：manifest test 覆盖段的 text → 契约内置「测试」。"""
        test = manifest.test
        if test is not None:
            if test.form_body and "text" in test.form_body:
                return str(test.form_body["text"])
            if test.json_body and "text" in test.json_body:
                return str(test.json_body["text"])
        return "测试"

    def _tts_voices(self, manifest) -> tuple[list[dict], str]:
        """枚举 tts 音色：manifest test.voices_file（demo.jsonl 逐行 JSON 的 name，行序=demo-N）。

        相对服务源目录（如 externals/moss-tts-nano/checkout/assets/demo.jsonl → checkout/assets/demo.jsonl）。
        返回 (voices, default_id)；文件缺失/解析失败返回 ([], "")。
        """
        test = manifest.test
        if test is None or not test.voices_file:
            return [], ""
        path = manifest.source_dir / test.voices_file
        if not path.is_file():
            logger.warning("[external] %s 音色文件缺失: %s", manifest.name, path)
            return [], ""
        voices: list[dict] = []
        try:
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    name = str(__import__("json").loads(line).get("name") or f"demo-{n}")
                except Exception:
                    name = f"demo-{n}"
                voices.append({"id": f"demo-{n}", "name": name})
        except OSError:
            logger.exception("[external] %s 音色文件读取失败: %s", manifest.name, path)
            return [], ""
        default_id = ""
        if test.form_body and test.form_body.get("demo_id"):
            default_id = str(test.form_body["demo_id"])
        elif test.json_body and test.json_body.get("demo_id"):
            default_id = str(test.json_body["demo_id"])
        elif voices:
            default_id = voices[0]["id"]
        return voices, default_id

    @staticmethod
    def _fr_sample(image_base64: str | None, fallback: bytes) -> tuple[bytes, dict | None]:
        """fr 测试样本解析：用户上传 → data/test/face.jpg → 契约内置 1x1 图。

        返回 (body, sample_meta)；sample_meta 仅默认样本时携带（用户上传的图前端自己持有预览）。
        """
        import base64 as _b64

        if image_base64:
            try:
                raw = _b64.b64decode(image_base64)
            except (ValueError, TypeError) as exc:
                raise ServiceError(f"image_base64 非法: {exc}") from exc
            if len(raw) > MAX_FACE_TEST_IMAGE_BYTES:
                raise ServiceError(f"图片过大（>{MAX_FACE_TEST_IMAGE_BYTES // (1024 * 1024)}MB）")
            return raw, None
        if DEFAULT_FACE_TEST_IMAGE.is_file():
            raw = DEFAULT_FACE_TEST_IMAGE.read_bytes()
            return raw, {
                "image_base64": _b64.b64encode(raw).decode(),
                "image_name": DEFAULT_FACE_TEST_IMAGE.name,
                "image_path": DEFAULT_FACE_TEST_IMAGE.relative_to(SERVICE_ROOT).as_posix(),
            }
        return fallback, None

    @staticmethod
    def _asr_sample(audio_base64: str | None, fallback: bytes) -> tuple[bytes, int, dict | None]:
        """asr 测试样本解析：用户上传 → data/test/asr.wav → 契约内置静音 PCM。

        WAV 容器剥头取 int16 PCM：立体声取左声道（契约 int16 mono PCM），
        采样率用真实值（适配器内部重采样到 16k）；原始 PCM 视为 16kHz 直接透传。
        返回 (pcm, sample_rate, sample_meta)；sample_meta 仅默认样本时携带
        （audio_base64=可播放的 WAV 容器、audio_name/audio_path/sample_rate）。
        """
        import array as _array
        import base64 as _b64
        import io as _io
        import wave as _wave

        raw: bytes | None = None
        if audio_base64:
            try:
                raw = _b64.b64decode(audio_base64)
            except (ValueError, TypeError) as exc:
                raise ServiceError(f"audio_base64 非法: {exc}") from exc
            if len(raw) > MAX_ASR_TEST_AUDIO_BYTES:
                raise ServiceError(f"音频过大（>{MAX_ASR_TEST_AUDIO_BYTES // (1024 * 1024)}MB）")
        elif DEFAULT_ASR_TEST_AUDIO.is_file():
            raw = DEFAULT_ASR_TEST_AUDIO.read_bytes()
        if raw is None:
            return fallback, 16000, None
        if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
            try:
                with _wave.open(_io.BytesIO(raw), "rb") as w:
                    channels, sample_rate = w.getnchannels(), w.getframerate()
                    sampwidth = w.getsampwidth()
                    frames = w.readframes(w.getnframes())
            except Exception as exc:
                raise ServiceError(f"WAV 解析失败: {exc}") from exc
            if not frames:
                raise ServiceError("WAV 音频内容为空")
            if sampwidth == 1:
                frames = bytes(b for byte in frames for b in (byte, 0))  # 8bit → 16bit
            elif sampwidth != 2:
                raise ServiceError(f"不支持的 WAV 位深: {sampwidth * 8}bit（仅 8/16bit）")
            if channels > 1:  # 立体声/多声道 → 取左声道（int16 交错样本 [::channels]）
                pcm = _array.array("h", frames)[0::channels].tobytes()
            else:
                pcm = frames
            meta = None if audio_base64 else {
                "audio_base64": _b64.b64encode(raw).decode(),
                "audio_name": DEFAULT_ASR_TEST_AUDIO.name,
                "audio_path": DEFAULT_ASR_TEST_AUDIO.relative_to(SERVICE_ROOT).as_posix(),
                "sample_rate": sample_rate,
            }
            return pcm, sample_rate, meta
        return raw, 16000, None  # 非 WAV 容器：视为原始 PCM int16，契约默认 16kHz

    @classmethod
    def _vpr_sample(cls, audio_base64: str | None) -> tuple[str | None, int, dict | None]:
        """vpr 测试样本解析：用户上传 → data/test/asr.wav → 无样本回退契约内置静音。

        与 _asr_sample 相同的归一化（WAV 剥头取 int16 mono PCM、真实采样率），
        但契约请求体是 JSON——返回 (audio_base64, sample_rate, sample_meta) 供
        resolve_test_spec(body_overrides=...) 重建 JSON body（curl 同步）。
        无样本时返回 (None, 16000, None)，沿用契约内置样本。
        """
        import base64 as _b64

        if not audio_base64 and not DEFAULT_ASR_TEST_AUDIO.is_file():
            return None, 16000, None
        pcm, sample_rate, meta = cls._asr_sample(audio_base64, fallback=b"")
        return _b64.b64encode(pcm).decode(), sample_rate, meta

    @staticmethod
    def _probe_contract(url: str, method: str, headers: dict, body: bytes, expect: list[str]) -> dict:
        """发契约样本请求；返回 {ok, status, elapsed_ms, body, error, missing, [大字段透传]}。

        elapsed_ms：请求发出到响应体读完的毫秒耗时（HTTP 错误也计入）。
        大字段（如 tts 的 audio_base64，动辄几百 KB）单独透传到顶层供前端使用
        （播放器），展示用 body 里替换为占位说明，避免截断成残缺 base64。
        """
        import json as _json
        import urllib.error

        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                status = resp.status
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
        except urllib.error.URLError as exc:
            raise RuntimeError(f"服务不可达: {exc.reason}") from exc
        elapsed_ms = int((time.monotonic() - start) * 1000)
        body_text = raw.decode("utf-8", errors="replace")
        ok = 200 <= status < 300
        missing: list[str] = []
        extra: dict[str, str] = {}
        if ok and body_text:
            try:
                data = _json.loads(body_text)
                if isinstance(data, dict):
                    missing = [k for k in expect if k not in data]
                    for key in ("audio_base64",):
                        val = data.get(key)
                        if isinstance(val, str) and len(val) > 1000:
                            extra[key] = val
                            size_kb = len(val) * 3 // 4 // 1024
                            data[key] = f"<{key}: {len(val)} 字符 ≈ {size_kb}KB，见上方播放器>"
                            body_text = _json.dumps(data, ensure_ascii=False)
            except ValueError:
                missing = list(expect)  # 非 JSON：格式不符
        error = None if ok else f"HTTP {status}"
        if ok and missing:
            error = f"响应缺少期望字段: {', '.join(missing)}"
        return {
            "ok": ok and not missing,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "body": body_text[:20000],
            "error": error,
            "missing": missing,
            **extra,
        }

    # ------------------------------------------------------------------ 日志

    def log_snapshot(self, name: str, since: int = 0) -> dict:
        """读日志文件 from-offset 新增内容；超 20MB 先轮转。"""
        handle = self._require(name)
        path = self._prepare_log(handle, rotate=False)
        if not path.is_file():
            return {"log": "", "next_offset": 0, "size": 0}
        size = path.stat().st_size
        if since >= size:
            return {"log": "", "next_offset": size, "size": size}
        with open(path, "rb") as f:
            f.seek(since)
            data = f.read(size - since)
        return {"log": data.decode("utf-8", errors="replace"), "next_offset": size, "size": size}

    # ------------------------------------------------------------------ 内部

    def _require(self, name: str) -> _ServiceHandle:
        handle = self._handles.get(name)
        if handle is None:
            raise ServiceError(f"未知服务: {name}")
        return handle

    def _ensure_installed(self, handle: _ServiceHandle) -> None:
        if "installed_version" not in self._persisted(handle.manifest.name):
            raise ServiceError(f"{handle.manifest.name} 尚未安装，请先 install")

    def _prepare_log(self, handle: _ServiceHandle, rotate: bool = True) -> Path:
        path = handle.log_path(self.data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        if rotate and path.is_file() and path.stat().st_size > MAX_LOG_SIZE:
            backup = path.with_suffix(".log.1")
            try:
                backup.unlink(missing_ok=True)
                path.rename(backup)
                logger.info("[external] %s 日志轮转 -> %s", handle.manifest.name, backup.name)
            except OSError:
                logger.exception("[external] %s 日志轮转失败", handle.manifest.name)
        return path

    def _log_size(self, name: str) -> int:
        handle = self._handles.get(name)
        if handle is None:
            return 0
        path = handle.log_path(self.data_dir)
        return path.stat().st_size if path.is_file() else 0

    def _reap_orphan(self, handle: _ServiceHandle) -> None:
        """回收主服务异常退出遗留的孤儿进程（pid 文件指向存活进程）。"""
        pid_path = handle.pid_path(self.data_dir)
        if not pid_path.is_file():
            return
        try:
            pid = int(pid_path.read_text().strip())
        except (OSError, ValueError):
            pid = 0
        if pid and pid_alive(pid):
            logger.warning("[external] %s 回收遗留进程 pid=%s", handle.manifest.name, pid)
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and pid_alive(pid):
                time.sleep(0.2)
            if pid_alive(pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            pid_path.unlink(missing_ok=True)

    def _set_state(
        self,
        handle: _ServiceHandle,
        state: ServiceState,
        *,
        error: str | None = None,
        exit_code: int | None = None,
    ) -> None:
        handle.state = state
        if error is not None:
            handle.last_error = error
        if exit_code is not None:
            handle.exit_code = exit_code
        if state in (ServiceState.RUNNING, ServiceState.UNHEALTHY, ServiceState.STARTING):
            handle.start_ts = handle.start_ts or time.monotonic()
        else:
            handle.start_ts = None
        logger.info("[external] %s -> %s (desired=%s)", handle.manifest.name, state.value, handle.desired)

    async def _spawn_locked(self, handle: _ServiceHandle) -> None:
        """调用方须持有 handle.lock。"""
        handle.proc = None
        manifest = handle.manifest
        workdir = manifest.resolve_workdir(SERVICE_ROOT)
        if not workdir.is_dir():
            self._set_state(handle, ServiceState.FAILED, error=f"workdir 不存在: {workdir}")
            raise ServiceError(f"{manifest.name} workdir 不存在: {workdir}")
        self._set_state(handle, ServiceState.STARTING)
        cfg = SpawnConfig(
            command=manifest.command,
            workdir=workdir,
            log_path=self._prepare_log(handle),
            pid_path=handle.pid_path(self.data_dir),
            env=manifest.env,
        )
        proc = ExternalProcess(cfg)
        try:
            await proc.spawn()
        except Exception as exc:
            self._set_state(handle, ServiceState.FAILED, error=f"spawn 失败: {exc}")
            raise ServiceError(f"{manifest.name} 启动失败: {exc}") from exc
        handle.proc = proc
        handle.start_ts = time.monotonic()
        handle.exit_code = None
        # 若进程立即退出（spawn 后瞬死），交给 watchdog 归入 FAILED
        logger.info("[external] %s started pid=%s", manifest.name, proc.pid)

    async def _watchdog_loop(self) -> None:
        logger.info("[external] watchdog loop start")
        try:
            while True:
                for handle in list(self._handles.values()):
                    try:
                        await self._tick(handle)
                    except Exception:
                        logger.exception("[external] tick %s failed", handle.manifest.name)
                await asyncio.sleep(WATCHDOG_INTERVAL_S)
        except asyncio.CancelledError:
            logger.info("[external] watchdog loop cancelled")
            raise

    async def _tick(self, handle: _ServiceHandle) -> None:
        async with handle.lock:
            if handle.proc is not None and not handle.proc.is_running():
                await self._on_exit_locked(handle)  # 进程退出 → 状态机判定
            if handle.proc is None:
                # 重启调度：期望运行 + backoff 到点
                if handle.desired and handle.restart_after is not None and time.monotonic() >= handle.restart_after:
                    await self._restart_locked(handle)
                return
            if handle.state == ServiceState.STOPPING:
                return  # terminate 进行中，不干预
            await self._healthcheck_locked(handle)

    async def _on_exit_locked(self, handle: _ServiceHandle) -> None:
        exit_code = handle.proc.returncode
        handle.proc.drop()
        handle.proc = None  # drop 只清内部 process，句柄置 None 供 _tick 判定
        handle.exit_code = exit_code
        if handle.state in (ServiceState.STOPPING,):
            self._set_state(handle, ServiceState.STOPPED)
            return
        if handle.state == ServiceState.STARTING:
            # 启动宽限期内退出 = 启动失败（配置/依赖问题），不自动重启
            self._set_state(handle, ServiceState.FAILED, error=f"进程启动后立即退出 (exit={exit_code})", exit_code=exit_code)
            return
        if handle.state in (ServiceState.RUNNING, ServiceState.UNHEALTHY, ServiceState.RESTARTING):
            if not handle.desired:
                self._set_state(handle, ServiceState.STOPPED, exit_code=exit_code)
                return
            handle.restart_count += 1
            if handle.restart_count > MAX_RESTARTS:
                self._set_state(
                    handle,
                    ServiceState.CRASH_LOOP,
                    error=f"连续 {handle.restart_count} 次崩溃，停止自动重启 (exit={exit_code})",
                    exit_code=exit_code,
                )
                return
            backoff = 0.0 if handle.restart_count == 1 else min(2 ** (handle.restart_count - 2), MAX_RESTART_BACKOFF_S)
            self._set_state(handle, ServiceState.RESTARTING, error=f"进程退出 (exit={exit_code})，{backoff:.0f}s 后重启", exit_code=exit_code)
            handle.restart_after = time.monotonic() + backoff
            return
        # 其余状态（FAILED/CRASH_LOOP/NOT_INSTALLED）退出 → 归 stopped
        self._set_state(handle, ServiceState.STOPPED, exit_code=exit_code)

    async def _restart_locked(self, handle: _ServiceHandle) -> None:
        handle.restart_after = None
        handle.fail_count = 0
        handle.last_error = None
        await self._spawn_locked(handle)

    async def _healthcheck_locked(self, handle: _ServiceHandle) -> None:
        manifest = handle.manifest
        hc = manifest.healthcheck
        if hc is None:
            if handle.state == ServiceState.STARTING:
                self._set_state(handle, ServiceState.RUNNING)
            return
        now = time.monotonic()
        if now - handle.last_check_ts < hc.interval_s:
            return
        handle.last_check_ts = now
        try:
            ok = await asyncio.wait_for(self._check(hc), timeout=hc.timeout_s)
        except Exception:
            ok = False
        if ok:
            handle.fail_count = 0
            if handle.state in (ServiceState.UNHEALTHY, ServiceState.STARTING):
                self._set_state(handle, ServiceState.RUNNING)
            return
        handle.fail_count += 1
        if handle.state == ServiceState.STARTING:
            if handle.start_ts is not None and now - handle.start_ts >= hc.startup_grace_s:
                self._set_state(handle, ServiceState.UNHEALTHY, error="启动宽限期后健康检查仍失败")
            return  # 宽限期内不计数升级
        if handle.fail_count > hc.max_failures and handle.state != ServiceState.UNHEALTHY:
            self._set_state(handle, ServiceState.UNHEALTHY, error="健康检查连续失败")

    async def _check(self, hc: HealthCheckConfig) -> bool:
        if hc.type == "tcp":
            _, writer = await asyncio.open_connection("127.0.0.1", hc.port)
            writer.close()
            await writer.wait_closed()
            return True
        try:
            await asyncio.to_thread(self._http_get, hc.url, hc.timeout_s)
            return True
        except Exception:
            return False

    @staticmethod
    def _http_get(url: str, timeout_s: float) -> None:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")
