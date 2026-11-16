"""启动隔离测试服务并执行真实浏览器验收；不加载模型，不接入硬件。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="deskbot-sop-browser-") as folder:
        env = dict(
            os.environ,
            DESKBOT_DB_PATH=f"{folder}/test.db",
            DESKBOT_WEB_SECRET_KEY="browser-test-secret",
            PYTHONPATH=f"{root / 'src'}{os.pathsep}{root}",
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        with (Path(folder) / "server.log").open("w+") as log:
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "tests.sop_browser_app:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=root,
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                for _ in range(100):
                    try:
                        with urllib.request.urlopen(url + "/health", timeout=1):
                            break
                    except OSError:
                        if server.poll() is not None:
                            raise RuntimeError("isolated test server did not start") from None
                        time.sleep(0.1)
                else:
                    raise RuntimeError("isolated test server startup timed out")
                env["DESKBOT_BROWSER_URL"] = url
                result = subprocess.run(
                    ["node", str(root / "tools/check_browser.cjs")], cwd=root, env=env, timeout=90, check=False
                )
                if result.returncode:
                    return result.returncode
                subprocess.run([sys.executable, "-m", "tests.ux_browser_seed"], cwd=root, env=env, check=True)
                subprocess.run(["node", str(root / "tools/check_interactions.cjs")], cwd=root, env=env, timeout=90, check=True)
                server.terminate()
                server.wait(timeout=10)
                server = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "tests.sop_browser_app:create_app",
                        "--factory",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ],
                    cwd=root,
                    env=env,
                    stdout=log,
                    stderr=log,
                )
                for _ in range(100):
                    try:
                        with urllib.request.urlopen(url + "/health", timeout=1) as response:
                            assert json.load(response) == {"status": "ok"}
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("isolated test server restart timed out")
                body = urllib.parse.urlencode({"email": "browser-sop@example.com", "password": "browser-password-123"})
                request = urllib.request.Request(
                    url + "/login",
                    data=body.encode(),
                    headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    assert json.load(response)["redirect"] == "/home"
                    assert "deskbot_session=" in response.headers.get("Set-Cookie", "")
                print("PASS graceful stop, restart and persisted account login")
                return 0
            finally:
                server.terminate()
                server.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
