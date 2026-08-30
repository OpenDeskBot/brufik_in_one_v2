"""外部服务 manifest 契约：service.yaml 解析与 externals 目录扫描。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from deskbot_server.service.external.contract import SERVICE_TYPES

logger = logging.getLogger("deskbot-server")

MANIFEST_FILENAME = "service.yaml"
NAME_RE_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789-_"

_PORT_RE = re.compile(r":(\d+)(?:/|$)")
_TCP_PORT_RE = re.compile(r"port\s*[=:]\s*(\d+)", re.IGNORECASE)


class ManifestError(ValueError):
    pass


@dataclass
class TestSpec:
    """可选测试契约覆盖：声明服务真实测试端点（默认用 contract.py 通用契约）。

    json 与 form 二选一：json → application/json 请求体；form → multipart/form-data
    （curl 生成 -F 参数，终端可直接复制测试）。
    """

    path: str = ""
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    json_body: dict | None = None
    form_body: dict | None = None
    expect: list[str] = field(default_factory=list)
    voices_file: str = ""  # tts 音色枚举文件（相对服务源目录，如 checkout/assets/demo.jsonl）

    @classmethod
    def from_dict(cls, raw: dict) -> TestSpec:
        if not isinstance(raw, dict):
            raise ManifestError("test 段须为 mapping")
        method = str(raw.get("method") or "POST").upper()
        if method not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
            raise ManifestError(f"test.method 仅支持 GET/POST/PUT/DELETE/PATCH，got {method!r}")
        path = str(raw.get("path") or "").strip()
        if path and not path.startswith("/"):
            raise ManifestError(f"test.path 须以 / 开头: {path!r}")
        headers = raw.get("headers") or {}
        if not isinstance(headers, dict):
            raise ManifestError("test.headers 须为 mapping")
        json_body = raw.get("json")
        form_body = raw.get("form")
        if json_body is not None and form_body is not None:
            raise ManifestError("test.json 与 test.form 只能二选一")
        if json_body is not None and not isinstance(json_body, dict):
            raise ManifestError("test.json 须为 mapping")
        if form_body is not None and not isinstance(form_body, dict):
            raise ManifestError("test.form 须为 mapping")
        expect = raw.get("expect") or []
        if not isinstance(expect, list) or not all(isinstance(e, str) for e in expect):
            raise ManifestError("test.expect 须为字符串数组")
        return cls(
            path=path,
            method=method,
            headers={str(k): str(v) for k, v in headers.items()},
            json_body=dict(json_body) if json_body is not None else None,
            form_body={str(k): str(v) for k, v in form_body.items()} if form_body else None,
            expect=[str(e) for e in expect],
            voices_file=str(raw.get("voices_file") or "").strip(),
        )


@dataclass
class HealthCheckConfig:
    type: str  # "http" | "tcp"
    url: str = ""  # http 探测地址
    port: int = 0  # tcp 探测端口
    interval_s: float = 5.0  # 探测间隔
    startup_grace_s: float = 30.0  # 启动宽限期（首次拉模型等慢启动场景）
    timeout_s: float = 3.0
    max_failures: int = 3  # 连续失败超过该次数进入 unhealthy

    @classmethod
    def from_dict(cls, raw: dict) -> HealthCheckConfig:
        htype = str(raw.get("type", "http"))
        if htype not in ("http", "tcp"):
            raise ManifestError(f"healthcheck.type 仅支持 http/tcp，got {htype!r}")
        return cls(
            type=htype,
            url=str(raw.get("url") or ""),
            port=int(raw.get("port") or 0),
            interval_s=float(raw.get("interval_s", 5.0)),
            startup_grace_s=float(raw.get("startup_grace_s", 30.0)),
            timeout_s=float(raw.get("timeout_s", 3.0)),
            max_failures=int(raw.get("max_failures", 3)),
        )


@dataclass
class ServiceManifest:
    name: str
    version: str
    description: str
    service_type: str  # asr/tts/llm/vlm/fr/vpr（契约见 contract.py）
    port: int  # 服务监听端口（展示与测试用）
    install: list[str]  # 安装命令（在 service root 下执行，须幂等）
    uninstall: list[str]  # 卸载命令（可选；无则只清状态与数据目录）
    command: list[str]  # 启动命令（含解释器路径）
    workdir: str  # 相对 service root
    env: dict[str, str] = field(default_factory=dict)
    healthcheck: HealthCheckConfig | None = None
    test: TestSpec | None = None  # 可选测试端点覆盖（默认用 contract.py 通用契约）
    auto_start: bool = False  # 默认值；用户可在后台覆盖并持久化
    source_dir: Path = field(default_factory=Path)

    def resolve_workdir(self, service_root: Path) -> Path:
        p = Path(self.workdir)
        return p if p.is_absolute() else (service_root / p).resolve()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "auto_start": self.auto_start,
        }

    @classmethod
    def load(cls, path: Path) -> ServiceManifest:
        """解析单个 service.yaml；缺失/非法字段抛 ManifestError。"""
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ManifestError(f"无法读取 manifest {path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise ManifestError(f"manifest 非法 YAML {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ManifestError(f"manifest 顶层须为 mapping: {path}")

        name = str(raw.get("name") or "").strip()
        if not name or not all(c in NAME_RE_CHARS for c in name):
            raise ManifestError(f"manifest name 非法（仅小写字母/数字/-/_）: {name!r}")

        start = raw.get("start") or {}
        if not isinstance(start, dict):
            raise ManifestError(f"manifest 缺少 start 段: {path}")
        command = start.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
            raise ManifestError(f"manifest start.command 须为非空字符串数组: {path}")

        install = raw.get("install")
        if install is None:
            install = []
        if not isinstance(install, list) or not all(isinstance(c, str) for c in install):
            raise ManifestError(f"manifest install 须为字符串数组: {path}")

        uninstall = raw.get("uninstall")
        if uninstall is None:
            uninstall = []
        if not isinstance(uninstall, list) or not all(isinstance(c, str) for c in uninstall):
            raise ManifestError(f"manifest uninstall 须为字符串数组: {path}")

        env = start.get("env") or {}
        if not isinstance(env, dict):
            raise ManifestError(f"manifest start.env 须为 mapping: {path}")

        hc = None
        if raw.get("healthcheck"):
            if not isinstance(raw["healthcheck"], dict):
                raise ManifestError(f"manifest healthcheck 须为 mapping: {path}")
            hc = HealthCheckConfig.from_dict(raw["healthcheck"])

        test = None
        if raw.get("test"):
            test = TestSpec.from_dict(raw["test"])

        service_type = str(raw.get("type") or "").strip().lower()
        if service_type not in SERVICE_TYPES:
            raise ManifestError(
                f"manifest type 须为 {'/'.join(SERVICE_TYPES)} 之一，got {service_type!r}: {path}"
            )
        port = _resolve_port(raw, hc)
        if port is None:
            raise ManifestError(
                f"manifest 无法确定端口：请在 manifest 顶层写 port，或 healthcheck 用 "
                f"http://127.0.0.1:<port>/... / tcp port=<port>: {path}"
            )

        return cls(
            name=name,
            version=str(raw.get("version") or "").strip(),
            description=str(raw.get("description") or "").strip(),
            service_type=service_type,
            port=port,
            install=[str(c) for c in install],
            uninstall=[str(c) for c in uninstall],
            command=[str(c) for c in command],
            workdir=str(start.get("workdir") or "."),
            env={str(k): str(v) for k, v in env.items()},
            healthcheck=hc,
            test=test,
            auto_start=bool(raw.get("auto_start", False)),
            source_dir=path.parent,
        )


def _resolve_port(raw: dict, hc: HealthCheckConfig | None) -> int | None:
    """端口解析顺序：manifest 顶层 port → healthcheck http url → healthcheck tcp port。"""
    explicit = raw.get("port")
    if explicit is not None:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            raise ManifestError(f"manifest port 非法: {explicit!r}") from None
    if hc is not None and hc.type == "http" and hc.url:
        m = _PORT_RE.search(hc.url)
        if m:
            return int(m.group(1))
    if hc is not None and hc.type == "tcp":
        m = _TCP_PORT_RE.search(str(hc.port))
        if m:
            return int(m.group(1))
        if hc.port:
            return hc.port
    return None


def discover_manifests(externals_dir: Path) -> list[ServiceManifest]:
    """扫描 externals/*/service.yaml，返回按名称排序的 manifest 列表。"""
    manifests: list[ServiceManifest] = []
    if not externals_dir.is_dir():
        return manifests
    for child in sorted(externals_dir.iterdir()):
        manifest_path = child / MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        try:
            manifests.append(ServiceManifest.load(manifest_path))
        except ManifestError as exc:
            logger.error("[external] 跳过非法 manifest %s: %s", manifest_path, exc)
    return manifests
