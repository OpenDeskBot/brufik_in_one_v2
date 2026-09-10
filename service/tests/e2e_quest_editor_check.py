"""临时 E2E：真浏览器验证剧情编辑器 5 项改造（连线/箭头/无上游行/节点类型改/面板状态改/节点 prompt 多行）。

运行：cd service && PYTHONPATH=src .venv/bin/python tests/e2e_quest_editor_check.py
只读 xiaoy.json 内容，所有写操作发生在临时剧本 e2e_check 上，结束后删除。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

os.environ["DESKBOT_DB_PATH"] = tempfile.mktemp(suffix=".db")
PORT = 9127
BASE = f"http://127.0.0.1:{PORT}"

import uvicorn  # noqa: E402

from deskbot_server.db.init_db import init_database  # noqa: E402
from deskbot_server.web.app import create_app  # noqa: E402

init_database()
app = create_app()


def _serve() -> None:
    cfg = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error", access_log=False)
    uvicorn.Server(cfg).run()


thread = threading.Thread(target=_serve, daemon=True)
thread.start()
for _ in range(100):
    try:
        import urllib.request

        urllib.request.urlopen(BASE + "/login", timeout=0.5)
        break
    except Exception:
        time.sleep(0.1)

from playwright.sync_api import sync_playwright  # noqa: E402

ok = []
fails = []


def check(name: str, cond: bool, extra: str = "") -> None:
    (ok if cond else fails).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))


def main() -> None:
    xiaoy = None
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1500, "height": 900})
        page = ctx.new_page()

        def api2(method: str, path: str, body=None):
            """页面内 fetch（带会话 cookie），返回 (status, json)。"""
            res = page.evaluate(
                """async ({m, p, b}) => {
                     if (m === 'GET') p += (p.includes('?') ? '&' : '?') + '_t=' + Date.now();
                     const r = await fetch(p, { method: m, headers: { 'Content-Type': 'application/json' },
                                              body: (b === undefined || b === null) ? undefined : JSON.stringify(b) });
                     return { status: r.status, body: await r.json().catch(() => ({})) };
                   }""",
                {"m": method, "p": path, "b": body},
            )
            return res["status"], res["body"]

        def rest(path: str, key: str = "playbook"):
            st, body = api2("GET", path)
            return body.get(key, {}) if st < 400 and body.get("ok") else {}

        try:
            # 注册第一个用户（自动 developer）→ 页面表单登录
            page.goto(BASE + "/login", wait_until="networkidle")  # 同源页面后再 fetch 注册
            reg_st = page.evaluate(
                """async ({u}) => {
                     const r = await fetch(u + '/register', { method: 'POST',
                       headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                       body: new URLSearchParams({ email: 'e2e@example.com', password: 'password1234',
                                                  confirm_password: 'password1234' }) });
                     return r.status;
                   }""",
                {"u": BASE},
            )
            check("注册成功(自动登录)", reg_st == 200 or reg_st == 302, f"status={reg_st}")
            page.wait_for_timeout(500)
            auth_st, auth_body = api2("GET", "/api/quest/playbooks")
            check("登录后可访问 quest API", auth_st == 200, str(auth_body)[:120])

            # 把 xiaoy 内容复制成 e2e_check（不污染真实剧本）
            xiaoy_st, xiaoy_body = api2("GET", "/api/quest/playbooks/xiaoy")
            check("xiaoy.json 可读", xiaoy_st == 200, str(xiaoy_body)[:160])
            if xiaoy_st != 200:
                return
            xiaoy = xiaoy_body["playbook"]
            create_st, create_body = api2("POST", "/api/quest/playbooks", {"name": "e2e_check"})
            imp_st, imp_body = api2(
                "POST", "/api/quest/playbooks/e2e_check/import",
                {"name": "e2e_check", "tasks": xiaoy["tasks"]},
            )
            check("e2e 剧本创建", create_st == 200, str(create_body)[:120])
            check("e2e 剧本导入", imp_st == 200, str(imp_body)[:200])
            if not (create_st == 200 and imp_st == 200):
                return

            page.goto(BASE + "/quest", wait_until="networkidle")
            page.wait_for_selector(".qt-node[data-id='g_city']", timeout=8000)
            # 切换到 e2e_check
            page.select_option(".qt-top select", "e2e_check")
            page.wait_for_function("document.querySelectorAll('.qt-node').length === 5")

            # 1) 基线连线数（以页面实际渲染为准）+ 箭头 marker + 线可见（stroke 属性非空）
            paths = page.locator("svg.qt-edges g path")
            n0 = paths.count()
            check("画布连线数>0", n0 > 0, f"count={n0}")
            any_marker = any(("qt-arrow" in (paths.nth(i).get_attribute("marker-end") or "")) for i in range(n0))
            check("连线带箭头 marker", any_marker, f"n={n0}")
            stroke_info = page.evaluate(
                "Array.from(document.querySelectorAll('svg.qt-edges g path')).map(p => "
                "({ s: p.getAttribute('stroke'), d: (p.getAttribute('d') || '').slice(0, 40) }))"
            )
            stroke_ok = all((p.get("s") or "").strip() for p in stroke_info)
            check("连线有可见 stroke", stroke_ok, f"n={n0} detail={str(stroke_info)[:260]}")
            # 2) 从 g_city 后继口拖到 g_task4 → 新连线
            src = page.locator(".qt-node[data-id='g_city'] .qt-port-out")
            src.scroll_into_view_if_needed()
            sb = src.bounding_box()
            tb = page.locator(".qt-node[data-id='g_task4']").bounding_box()
            check("端口/目标可见", sb is not None and tb is not None, f"src={sb} tgt={tb}")
            page.mouse.move(sb["x"] + sb["width"] / 2, sb["y"] + sb["height"] / 2)
            page.mouse.down()
            page.mouse.move(tb["x"] + tb["width"] / 2, tb["y"] + tb["height"] / 2, steps=12)
            ghost = page.locator("svg.qt-edges line")
            check("拖动中出现 ghost 虚线", ghost.count() == 1, f"ghost={ghost.count()}")
            hit = page.evaluate(
                "([x, y]) => { const el = document.elementFromPoint(x, y); "
                "return el ? (el.closest('.qt-node') !== null) : false; }",
                [tb["x"] + tb["width"] / 2, tb["y"] + tb["height"] / 2],
            )
            check("松手点命中目标任务区", hit is True, "")
            page.mouse.up()
            page.wait_for_timeout(800)
            try:
                page.wait_for_function(
                    "document.querySelectorAll('svg.qt-edges g path').length === " + str(n0 + 1), timeout=5000
                )
            except Exception:
                pass
            page.wait_for_timeout(500)
            probe_st, probe_body = api2("GET", "/api/quest/playbooks/e2e_check")
            p_tasks = (probe_body.get("playbook") or {}).get("tasks", []) if probe_st < 400 else []
            t_city = next((t for t in p_tasks if t["id"] == "g_city"), {})
            err_el = page.locator(".qt-top-err")
            err_txt = err_el.inner_text() if err_el.count() else ""
            ids = [t.get("id") for t in p_tasks]
            check(
                "拖线后 g_city 后继含 g_task4",
                "g_task4" in t_city.get("next_task_ids", []) and probe_st == 200,
                f"st={probe_st} ids={ids} g_city={t_city} err={err_txt}",
            )

            # 3) 节点上无「上游」行
            check("节点不再显示上游计数", page.locator(".qt-node-foot").count() == 0)

            # 3b) 点击两次连线：点 g_task5 后继口（进入连线模式）→ 点 g_city 任务
            page.locator(".qt-node[data-id='g_task5'] .qt-port-out").click()
            page.wait_for_timeout(200)
            pulsing = page.locator(".qt-node[data-id='g_task5'] .qt-port-out.qt-linking").count()
            page.locator(".qt-node[data-id='g_city']").click()
            page.wait_for_timeout(600)
            pb2 = rest("/api/quest/playbooks/e2e_check")
            t5b = next((t for t in pb2.get("tasks", []) if t["id"] == "g_task5"), {})
            check("点选连线：源口脉冲提示", pulsing == 1, f"pulse={pulsing}")
            check("点选连线：g_task5 后继含 g_city", "g_city" in t5b.get("next_task_ids", []),
                  str(t5b.get("next_task_ids")))

            # 3c) 节点高度拉伸：底部把手向下拖 60px 并持久化
            h0 = page.locator(".qt-node[data-id='g_city']").bounding_box()["height"]
            grip = page.locator(".qt-node[data-id='g_city'] .qt-resize")
            gb = grip.bounding_box()
            page.mouse.move(gb["x"] + gb["width"] / 2, gb["y"] + gb["height"] / 2)
            page.mouse.down()
            page.mouse.move(gb["x"] + gb["width"] / 2, gb["y"] + gb["height"] / 2 + 60, steps=8)
            page.mouse.up()
            page.wait_for_timeout(600)
            pb3 = rest("/api/quest/playbooks/e2e_check")
            t_city2 = next((t for t in pb3.get("tasks", []) if t["id"] == "g_city"), {})
            check("节点高度拉伸已持久化", (t_city2.get("pos") or {}).get("height", 0) >= h0 + 40,
                  f"h0={h0} h={ (t_city2.get('pos') or {}).get('height') }")

            # 4) 节点上类型控件改类型并持久化（g_task5 daily → long_term → 改回）
            page.locator(".qt-node[data-id='g_task5'] .qt-type-select").select_option("long_term")
            page.wait_for_timeout(400)
            pb = rest("/api/quest/playbooks/e2e_check")
            t5 = next(t for t in pb["tasks"] if t["id"] == "g_task5")
            check("节点类型控件改 long_term 已保存", t5["type"] == "long_term", t5["type"])
            page.locator(".qt-node[data-id='g_task5'] .qt-type-select").select_option("daily")
            page.wait_for_timeout(400)

            # 5) 节点 prompt 主体多行编辑（g_city 输入两行）
            ta = page.locator(".qt-node[data-id='g_city'] textarea.qt-prompt")
            ta.fill("获取用户所在位置\n并且问问他住的小区")
            page.locator(".qt-node[data-id='g_city'] .qt-node-head").click()
            page.wait_for_timeout(400)
            pb = rest("/api/quest/playbooks/e2e_check")
            t_city = next(t for t in pb["tasks"] if t["id"] == "g_city")
            check("节点 prompt 多行已保存", t_city["prompt"] == "获取用户所在位置\n并且问问他住的小区", repr(t_city["prompt"]))

            # 6) 节点状态下拉 与 详情面板状态下拉 双向可用
            page.locator(".qt-node[data-id='g_city'] .qt-state-select").select_option("completed")
            page.wait_for_timeout(300)
            st = rest("/api/quest/playbooks/e2e_check/state", "instances")
            check("节点状态改 completed", st.get("g_city", {}).get("status") == "completed")
            page.locator(".qt-node[data-id='g_city'] .qt-node-head").click()
            page.wait_for_selector(".qt-panel")
            panel_sel = page.locator(".qt-panel .qt-state-select")
            check("详情面板有状态下拉", panel_sel.count() == 1)
            panel_sel.select_option("running")
            page.wait_for_timeout(300)
            st = rest("/api/quest/playbooks/e2e_check/state", "instances")
            check("面板状态改 running", st.get("g_city", {}).get("status") == "running")
            check("顶部提示文案更新", "后继口" in page.locator(".qt-hint").inner_text())
        finally:
            if os.environ.get("KEEP_E2E"):
                print("KEEP_E2E：保留 data/quest/e2e_check.json 供检查")
            else:
                try:
                    api2("DELETE", "/api/quest/playbooks/e2e_check")
                except Exception:
                    pass
            ctx.close()
            browser.close()

    print("----")
    print(f"PASS {len(ok)} / FAIL {len(fails)}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
