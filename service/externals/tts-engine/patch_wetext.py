#!/usr/bin/env python3
"""MOSS-TTS-Nano WeTextProcessing 降级 patch（幂等，可重复执行）。

用法: python patch_wetext.py <app_py> <text_normalization_pipeline_py>

背景: WeTextProcessing 依赖 pynini，PyPI 无 macOS arm64 wheel 且源码编译需要
openfst；装不上时 MOSS 的 warmup 会硬失败导致 /api/generate 全部 500。
本脚本把两处硬检查改为降级（warmup 只 warning；归一化走 robust fallback）。
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "deskbot patch"

APP_OLD = (
    "                normalization_snapshot = self.text_normalizer_manager.ensure_ready()\n"
    "                if normalization_snapshot.failed:\n"
    "                    raise RuntimeError(normalization_snapshot.error or normalization_snapshot.message)"
)
APP_NEW = (
    "                normalization_snapshot = self.text_normalizer_manager.ensure_ready()\n"
    "                if normalization_snapshot.failed:\n"
    f"                    # [{MARKER}] WeTextProcessing/pynini 在部分平台装不上，\n"
    "                    # warmup 降级不阻断（generate 时用 robust fallback 归一化）\n"
    '                    logging.getLogger("deskbot-external-tts").warning(\n'
    '                        "WeTextProcessing unavailable, text normalization degraded: %s",\n'
    "                        normalization_snapshot.error or normalization_snapshot.message,\n"
    "                    )"
)
NORM_OLD = (
    "    if enable_wetext:\n"
    "        if text_normalizer_manager is None:\n"
    '            raise RuntimeError("WeTextProcessing manager is unavailable.")'
)
NORM_NEW = (
    "    if enable_wetext and (text_normalizer_manager is None or not text_normalizer_manager.snapshot().ready):\n"
    f"        # [{MARKER}] WeTextProcessing 不可用时降级为 robust fallback 归一化\n"
    '        logging.getLogger("deskbot-external-tts").warning(\n'
    '            "WeTextProcessing unavailable, using robust fallback normalization"\n'
    "        )\n"
    "        enable_wetext = False\n"
    "    if enable_wetext:\n"
    "        if text_normalizer_manager is None:\n"
    '            raise RuntimeError("WeTextProcessing manager is unavailable.")'
)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: patch_wetext.py <app_py> <text_normalization_pipeline_py>")
    app_py, norm_py = Path(sys.argv[1]), Path(sys.argv[2])

    src = app_py.read_text(encoding="utf-8")
    if MARKER in src:
        print(f"[patch] skip (already applied): {app_py.name}")
    elif APP_OLD in src:
        app_py.write_text(src.replace(APP_OLD, APP_NEW), encoding="utf-8")
        print(f"[patch] applied: {app_py.name} (warmup degradation)")
    else:
        raise SystemExit(f"[patch] FAILED: warmup target not found in {app_py}")

    src = norm_py.read_text(encoding="utf-8")
    if MARKER in src:
        print(f"[patch] skip (already applied): {norm_py.name}")
    elif NORM_OLD in src:
        norm_py.write_text(src.replace(NORM_OLD, NORM_NEW), encoding="utf-8")
        print(f"[patch] applied: {norm_py.name} (wetext degradation)")
    else:
        raise SystemExit(f"[patch] FAILED: wetext target not found in {norm_py}")


if __name__ == "__main__":
    main()
