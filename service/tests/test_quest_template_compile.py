"""剧本编辑器模板回归测试：用项目 vendor 的 Vue（3.4.38）实际编译 questApp 模板。

背景：Vue 3.4.x 编译器对 ``:class="{qt-id-dup: ...}"`` 这类带连字符的对象键
会生成非法 JS（``{qt-id-dup: ...}`` 被解析成减法），导致 mount 时编译崩溃、
页面全空。此测试确保模板里不再出现这类构造（要求 node 可用，否则跳过）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
VENDOR_VUE = _SRC / "deskbot_server/web/static/vendor/vue.global.prod.min.js"
QUEST_TEMPLATE = _SRC / "deskbot_server/web/templates/quests.html"

_COMPILE_NODE_SCRIPT = r"""
const fs = require('fs');
// vendor 是 IIFE（var Vue = ...），追加导出后写成临时模块再 require
const src = fs.readFileSync(process.argv[1], 'utf-8');
const modPath = process.argv[3];
fs.writeFileSync(modPath, src + '\n;module.exports = Vue;');
const Vue = require(modPath);
const tpl = fs.readFileSync(process.argv[2], 'utf-8');
try {
  Vue.compile(tpl, { delimiters: ['[[', ']]'] });
  console.log('OK');
} catch (e) {
  console.error('COMPILE FAIL:', e.message);
  process.exit(1);
}
"""


def _extract_questapp(template_text: str) -> str:
    s = template_text.index('<div id="questApp"')
    e = template_text.index("{% block scripts %}")
    tpl = template_text[s:e]
    tpl = re.sub(r"^\s*<div id=\"questApp\"[^>]*>", "", tpl)
    tpl = re.sub(r"\s*</div>\s*$", "", tpl)
    return tpl


def _node_available() -> bool:
    return shutil.which("node") is not None


@pytest.mark.skipif(not _node_available(), reason="需要 node 执行 Vue 编译检查")
def test_quest_template_compiles_with_vendored_vue():
    assert VENDOR_VUE.is_file(), f"vendor Vue 缺失: {VENDOR_VUE}"
    tpl = _extract_questapp(QUEST_TEMPLATE.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        tpl_path = Path(tmp) / "questapp.html"
        tpl_path.write_text(tpl, encoding="utf-8")
        mod_path = Path(tmp) / "vue_vendor_mod.js"
        proc = subprocess.run(
            ["node", "-e", _COMPILE_NODE_SCRIPT, str(VENDOR_VUE), str(tpl_path), str(mod_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert proc.returncode == 0, f"模板在 vendor Vue 下编译失败:\n{proc.stderr}"


def test_quest_template_no_hyphenated_class_object_keys():
    """防回归：:class 对象键不能带连字符（Vue 3.4 编译器 bug）。"""
    tpl = QUEST_TEMPLATE.read_text(encoding="utf-8")
    # 只匹配未加引号的连字符键（`{qt-id-dup: ...}` 编译崩溃；`{ 'qt-id-dup': ... }` 合法）
    bad = re.findall(r":class=\"\{[^\"']*[a-z]+-[a-z]+\s*:", tpl)
    assert not bad, f"发现未加引号的连字符 :class 对象键（会编译崩溃）: {bad}"


def test_quest_template_arg_calls_defined_in_methods():
    """防回归：模板里带参调用的函数必须在 methods 里。

    Vue 的 computed 是无参缓存 getter，`incomingSum(t)` 这类带参调用
    若误放进 computed，运行时报 ``incomingSum is not a function``。
    """
    src = QUEST_TEMPLATE.read_text(encoding="utf-8")
    tpl = _extract_questapp(src)
    methods_block = re.search(r"\n  methods: \{(.*?)\n  \},\n", src, re.S)
    assert methods_block, "未找到 methods 块"
    defined = set(
        re.findall(r"^\s{4}(?:async\s+)?([a-zA-Z_]\w*)\s*\([^)]*\)\s*\{", methods_block.group(1), re.M)
    )
    # 只取 Vue 表达式：[[ ... ]] 插值与 :/@/v- 绑定属性值（排除中文文案误报）
    exprs = re.findall(r"\[\[ ([^\]]+?) \]\]", tpl)
    exprs += re.findall(r'[@:][a-zA-Z0-9_.-]*="([^"]*)"', tpl)
    calls: set[str] = set()
    for expr in exprs:
        # 排除属性链（Math.round / .splice）与 $event 等
        calls |= set(re.findall(r"(?<![\w.$])([a-zA-Z_]\w*)\(", expr))
    globals_ok = {"Math", "JSON", "String", "Number", "Boolean", "Object", "Array", "Date", "parseInt", "parseFloat", "isNaN"}
    missing = sorted(c for c in calls if c not in defined and c not in globals_ok)
    assert not missing, f"模板带参调用了未定义/不在 methods 里的函数（computed 不能带参）: {missing}"
