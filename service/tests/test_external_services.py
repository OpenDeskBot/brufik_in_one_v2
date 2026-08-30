"""外部进程服务管理器测试：manifest 解析 / 生命周期 / 自动重启 / healthcheck。

用临时目录里一个 fake python HTTP 服务充当"外部服务"，不依赖真实 MOSS-TTS-Nano。
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
import time
from pathlib import Path

import pytest

from deskbot_server.service.external.manager import ExternalServiceManager, ServiceError, ServiceState
from deskbot_server.service.external.manifest import ManifestError, ServiceManifest, discover_manifests

SERVICE_ROOT = Path(__file__).resolve().parents[1]  # service/
REAL_MANIFEST = SERVICE_ROOT / "externals" / "tts-engine" / "service.yaml"

FAKE_SERVICE_PY = textwrap.dedent(
    """
    import http.server
    import os
    import sys
    import threading
    import time

    PORT = int(sys.argv[1])
    MODE = os.environ.get("FAKE_MODE", "ok")
    CRASH_AFTER = float(os.environ.get("FAKE_CRASH_AFTER", "3"))

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    if MODE == "exit_now":
        sys.exit(1)
    if MODE == "crash_after":
        time.sleep(CRASH_AFTER)
        sys.exit(7)
    while True:
        sys.stderr.write("fake-server-heartbeat\\n")
        sys.stderr.flush()
        time.sleep(3600)
    """
)


def _write_fake_service(
    tmp: Path,
    name: str,
    *,
    health_port: int,
    mode: str = "ok",
    install_cmd: str = "true",
    env_extra: str = "",
) -> Path:
    """构造临时外部服务目录：server.py + service.yaml。"""
    svc_dir = tmp / "externals" / name
    svc_dir.mkdir(parents=True, exist_ok=True)
    (svc_dir / "server.py").write_text(FAKE_SERVICE_PY)
    (svc_dir / "service.yaml").write_text(
        textwrap.dedent(
            f"""
            name: {name}
            version: test-1.0
            type: asr
            port: {health_port}
            description: fake service for tests
            install:
              - "{install_cmd}"
            start:
              command:
                - {sys.executable}
                - server.py
                - "{health_port}"
              workdir: {svc_dir}
              env: {{ FAKE_MODE: "{mode}"{env_extra} }}
            healthcheck:
              type: http
              url: http://127.0.0.1:{health_port}/health
              interval_s: 0.3
              startup_grace_s: 2.0
              timeout_s: 2.0
              max_failures: 2
            """
        )
    )
    return svc_dir


def _make_manager(tmp: Path) -> ExternalServiceManager:
    mgr = ExternalServiceManager(externals_dir=tmp / "externals", data_dir=tmp / "data")
    mgr.discover()
    return mgr


async def _wait_state(mgr: ExternalServiceManager, name: str, state: ServiceState, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = mgr.snapshot(name)
        if snap is not None and snap.state == state:
            return snap
        await asyncio.sleep(0.1)
    raise AssertionError(f"{name} 未在 {timeout}s 内进入 {state.value}: {mgr.snapshot(name)}")


# ------------------------------------------------------------------ manifest


def test_manifest_load_real():
    """真实 tts-engine manifest 能解析，且契约字段符合预期。"""
    m = ServiceManifest.load(REAL_MANIFEST)
    assert m.name == "moss-tts-nano"
    assert m.install == ["bash externals/tts-engine/install.sh"]
    assert m.uninstall and "rm -rf" in m.uninstall[0]
    assert m.command[0].endswith("moss-tts-nano")
    assert m.command[1] == "serve" and "--backend" in m.command and "onnx" in m.command
    assert m.healthcheck is not None
    assert m.healthcheck.type == "http"
    assert m.healthcheck.url == "http://127.0.0.1:9101/health"
    assert m.resolve_workdir(SERVICE_ROOT) == SERVICE_ROOT / "externals" / "tts-engine"
    assert m.test is not None and m.test.voices_file == "checkout/assets/demo.jsonl"


def test_manifest_invalid(tmp_path):
    bad = tmp_path / "service.yaml"
    bad.write_text("name: 'Bad Name'\nstart:\n  command: [x]\n")
    with pytest.raises(ManifestError):
        ServiceManifest.load(bad)

    bad.write_text("name: ok\nstart:\n  command: echo hi\n")
    with pytest.raises(ManifestError):
        ServiceManifest.load(bad)  # command 非数组


def test_discover_sorted(tmp_path):
    _write_fake_service(tmp_path, "beta", health_port=9191)
    _write_fake_service(tmp_path, "alpha", health_port=9192)
    ms = discover_manifests(tmp_path / "externals")
    assert [m.name for m in ms] == ["alpha", "beta"]
    # 损坏的 manifest 被跳过
    (tmp_path / "externals" / "broken").mkdir()
    (tmp_path / "externals" / "broken" / "service.yaml").write_text("name: 'x y'\n")
    ms = discover_manifests(tmp_path / "externals")
    assert all(m.name != "broken" for m in ms)


# ------------------------------------------------------------------ 生命周期


def test_lifecycle(tmp_path):
    async def run():
        _write_fake_service(tmp_path, "fake", health_port=9193)
        mgr = _make_manager(tmp_path)
        mgr.start_watchdog()
        try:
            # 未安装 → start 拒绝
            with pytest.raises(ServiceError):
                await mgr.start("fake")
            assert mgr.snapshot("fake").state == ServiceState.NOT_INSTALLED
            # 安装
            snap = await mgr.install("fake")
            assert snap.state == ServiceState.STOPPED and snap.installed
            assert snap.installed_version == "test-1.0"
            # 启动 → healthcheck 通过 → running
            snap = await mgr.start("fake")
            assert snap.state in (ServiceState.STARTING, ServiceState.RUNNING)
            snap = await _wait_state(mgr, "fake", ServiceState.RUNNING)
            assert snap.healthy is True and snap.pid
            # 停止
            snap = await mgr.stop("fake")
            assert snap.state == ServiceState.STOPPED
            assert snap.pid is None
            # 幂等 stop
            snap = await mgr.stop("fake")
            assert snap.state == ServiceState.STOPPED
        finally:
            await mgr.shutdown()

    asyncio.run(run())


def test_startup_crash_fails(tmp_path):
    """启动即崩 → FAILED，不自动重启。"""
    async def run():
        _write_fake_service(tmp_path, "fake", health_port=9194, mode="exit_now")
        mgr = _make_manager(tmp_path)
        mgr.start_watchdog()
        try:
            await mgr.install("fake")
            await mgr.start("fake")
            snap = await _wait_state(mgr, "fake", ServiceState.FAILED)
            assert snap.restarts == 0  # 启动失败不算崩溃重启
            assert snap.exit_code == 1
        finally:
            await mgr.shutdown()

    asyncio.run(run())


def test_crash_auto_restart(tmp_path):
    """运行中崩溃 → 自动重启（首次立即）→ 恢复 running，restarts 计数。"""
    async def run():
        _write_fake_service(tmp_path, "fake", health_port=9195, mode="crash_after", env_extra=', FAKE_CRASH_AFTER: "3"')
        mgr = _make_manager(tmp_path)
        mgr.start_watchdog()
        try:
            await mgr.install("fake")
            await mgr.start("fake")
            snap = await _wait_state(mgr, "fake", ServiceState.RUNNING)
            assert snap.pid
            # 首次崩溃（3s 后）→ 自动重启（backoff 0，RESTARTING 瞬态不可观测）
            # → 再次 running 且 restarts==1；该窗口持续到第二次崩溃（+3s）前
            deadline = time.monotonic() + 15
            snap = None
            while time.monotonic() < deadline:
                snap = mgr.snapshot("fake")
                if snap and snap.state == ServiceState.RUNNING and snap.restarts == 1:
                    break
                await asyncio.sleep(0.1)
            assert snap is not None and snap.restarts == 1, f"崩溃未自动重启: {snap}"
            assert snap.pid and snap.pid != 0
        finally:
            await mgr.shutdown()

    asyncio.run(run())


def test_healthcheck_unhealthy_keeps_process(tmp_path):
    """healthcheck 连续失败 → unhealthy；进程不杀（首次拉模型等慢启动场景）。"""
    async def run():
        _write_fake_service(tmp_path, "fake", health_port=9196)
        mgr = _make_manager(tmp_path)
        mgr.start_watchdog()
        try:
            await mgr.install("fake")
            # 改指向不通的端口，制造 healthcheck 失败
            handle = mgr._handles["fake"]
            handle.manifest.healthcheck.url = "http://127.0.0.1:9199/health"  # type: ignore[union-attr]
            await mgr.start("fake")
            snap = await _wait_state(mgr, "fake", ServiceState.UNHEALTHY, timeout=10)
            assert snap.pid  # 进程仍存活
            # 恢复探测地址 → 自动回 running
            handle.manifest.healthcheck.url = "http://127.0.0.1:9196/health"  # type: ignore[union-attr]
            snap = await _wait_state(mgr, "fake", ServiceState.RUNNING, timeout=8)
            assert snap.healthy is True
        finally:
            await mgr.shutdown()

    asyncio.run(run())


# ------------------------------------------------------------------ 安装与持久化


def test_install_failure(tmp_path):
    async def run():
        _write_fake_service(tmp_path, "fake", health_port=9197, install_cmd="false")
        mgr = _make_manager(tmp_path)
        try:
            with pytest.raises(ServiceError):
                await mgr.install("fake")
            snap = mgr.snapshot("fake")
            assert snap.state == ServiceState.FAILED
            assert not snap.installed
        finally:
            await mgr.shutdown()

    asyncio.run(run())


def test_discover_restores_installed_state(tmp_path):
    """重启后 discover 恢复持久化安装状态：已安装 → stopped（未启动），而非 not_installed。"""
    async def run():
        _write_fake_service(tmp_path, "fake", health_port=9198)
        mgr = _make_manager(tmp_path)
        mgr.start_watchdog()
        try:
            await mgr.install("fake")
            assert mgr.snapshot("fake").state == ServiceState.STOPPED
            # 模拟主服务重启：新 manager 实例 + 重新 discover
            mgr2 = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
            mgr2.discover()
            snap = mgr2.snapshot("fake")
            assert snap.installed is True
            assert snap.state == ServiceState.STOPPED, "已安装服务重启后应为未启动而非未安装"
            # 且可直接启动
            mgr2.start_watchdog()
            await mgr2.start("fake")
            await _wait_state(mgr2, "fake", ServiceState.RUNNING)
        finally:
            await mgr.shutdown()
            await mgr2.shutdown()

    asyncio.run(run())


def test_uninstall(tmp_path):
    """卸载：停止运行中进程 + 执行 uninstall 命令 + 清数据目录与状态。"""
    async def run():
        _write_fake_service(tmp_path, "fake", health_port=9198, install_cmd="", env_extra="")
        svc_dir = tmp_path / "externals" / "fake"
        marker = svc_dir / "installed.marker"
        # install/uninstall 命令在 service root 执行，用绝对路径标记
        yaml_path = svc_dir / "service.yaml"
        text = yaml_path.read_text()
        text = text.replace('install:\n  - ""', f'install:\n  - touch "{marker}"')
        text = text.replace("start:\n", f'uninstall:\n  - rm -f "{marker}"\nstart:\n')
        yaml_path.write_text(text)
        mgr = _make_manager(tmp_path)
        mgr.start_watchdog()
        try:
            await mgr.install("fake")
            assert (svc_dir / "installed.marker").exists()
            await mgr.start("fake")
            await _wait_state(mgr, "fake", ServiceState.RUNNING)
            # 卸载
            snap = await mgr.uninstall("fake")
            assert snap.state == ServiceState.NOT_INSTALLED
            assert not snap.installed
            # uninstall 命令已执行（marker 删除）
            assert not (svc_dir / "installed.marker").exists()
            # 数据目录（日志/pid）已清理
            assert not (tmp_path / "data" / "fake").exists()
            # 安装状态已清：start 应拒绝
            with pytest.raises(ServiceError):
                await mgr.start("fake")
        finally:
            await mgr.shutdown()

    asyncio.run(run())


def test_uninstall_running_stops_process(tmp_path):
    """卸载会先停掉运行中的进程。"""
    async def run():
        _write_fake_service(tmp_path, "fake", health_port=9199)
        mgr = _make_manager(tmp_path)
        mgr.start_watchdog()
        try:
            await mgr.install("fake")
            await mgr.start("fake")
            snap = await _wait_state(mgr, "fake", ServiceState.RUNNING)
            pid = snap.pid
            assert pid
            await mgr.uninstall("fake")
            import time as _t
            _t.sleep(0.5)
            from deskbot_server.infrastructure.external.process_supervisor import pid_alive
            assert not pid_alive(pid), f"卸载后进程 {pid} 仍存活"
        finally:
            await mgr.shutdown()

    asyncio.run(run())


def test_auto_start_persist(tmp_path):
    _write_fake_service(tmp_path, "fake", health_port=9198)
    mgr = _make_manager(tmp_path)
    assert mgr.snapshot("fake").auto_start is False
    snap = mgr.set_auto_start("fake", True)
    assert snap.auto_start is True
    # state.json 持久化（data_dir 下），新实例读回
    assert (tmp_path / "data" / "state.json").is_file()
    mgr2 = _make_manager(tmp_path)
    assert mgr2.snapshot("fake").auto_start is True


def test_log_snapshot(tmp_path):
    async def run():
        _write_fake_service(tmp_path, "fake", health_port=9199)
        mgr = _make_manager(tmp_path)
        mgr.start_watchdog()
        try:
            await mgr.install("fake")
            await mgr.start("fake")
            await _wait_state(mgr, "fake", ServiceState.RUNNING)
            d = mgr.log_snapshot("fake", since=0)
            assert d["log"]  # fake server 启动日志（http.server 输出）
            assert d["size"] > 0
            # since 增量读
            d2 = mgr.log_snapshot("fake", since=d["next_offset"])
            assert d2["size"] == d["size"]
        finally:
            await mgr.shutdown()

    asyncio.run(run())
