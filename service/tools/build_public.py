"""打包静态页面源码；不执行模板、不读取配置或数据库。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

PUBLIC = Path(__file__).resolve().parents[1] / "src/deskbot_server/public"


def build():
    views = {}
    for section in ("site", "admin"):
        root = PUBLIC / section / "views"
        for path in sorted(root.rglob("*.html")):
            source = path.read_text(encoding="utf-8")
            source = re.sub(r"{%\s*with\s+(.+?)\s*%}", r"{% set \1 %}", source)
            source = re.sub(r"{%\s*endwith\s*%}", "", source)
            source = source.replace("user.created_at.strftime('%Y-%m-%d %H:%M')", "(user.created_at|displaydate)")
            views[str(path.relative_to(root))] = source
    serialized = json.dumps(views, ensure_ascii=False)
    (PUBLIC / "shared/views.json").write_text(serialized, encoding="utf-8")
    # A new loader URL also updates clients that cached an older loader without
    # the no-store fetch option. Tie it to both the page bundle and loader code.
    loader = (PUBLIC / "shared/page-loader.js").read_text(encoding="utf-8")
    runtime = (PUBLIC / "shared/ui-runtime.js").read_text(encoding="utf-8")
    version = hashlib.sha256((serialized + loader + runtime).encode()).hexdigest()[:12]
    for section in ("site", "admin"):
        shell = PUBLIC / section / "index.html"
        source = shell.read_text(encoding="utf-8")
        source = re.sub(r"/static/page-loader\.js(?:\?v=[a-f0-9]+)?",
                        f"/static/page-loader.js?v={version}", source)
        source = re.sub(r"/static/ui-runtime\.js(?:\?v=[a-f0-9]+)?",
                        f"/static/ui-runtime.js?v={version}", source)
        shell.write_text(source, encoding="utf-8")
    print(f"Built {len(views)} browser views")


if __name__ == "__main__":
    build()
