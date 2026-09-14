"""按统一速率预算改写 servo.json 的 preset 时长（幂等）。

背景
----
舵机动作「慢」不在固件——`hardware/firmware/head.cpp` 严格按每步的 ``ms`` 线性
插值，``ms`` 就是唯一调速手段。慢的原因全在 ``data/servo.json`` 的数值里：
``shake_head`` 一次 60° 摆动写成了 1000ms，``look_*`` 单步 500ms。

为什么需要脚本
--------------
``servo.json`` 是**设备级优先**的（``utils/device_data.resolve_json_path`` 在给了
device_id 时返回 ``data/{device_id}/servo.json``，且它不在 ``SHARED_CONFIG_NAMES``
里）。所以只改 ``data/servo.json`` 对正在跑的设备**零效果**，必须逐份改。手改必然
漂移，这里按 preset id 统一改写。

速率预算（按总行程算）
----------------------
常规 ~200°/s、fast ~300°/s、slow ~110°/s。硬件上限约 300–400°/s，且单步下限
受 ``dao/servo_config_store.normalize_servo_step`` 钳制（ms ∈ [50, 10000]）。

用法（在 service/ 目录）
-----------------------
    PYTHONPATH=src .venv/bin/python scripts/apply_servo_timings.py           # 预演
    PYTHONPATH=src .venv/bin/python scripts/apply_servo_timings.py --apply   # 落盘
    PYTHONPATH=src .venv/bin/python scripts/apply_servo_timings.py --apply data/brfk_xxx/servo.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# preset id -> 每步 ms（步数必须与文件里一致，否则跳过并告警，不动结构）
TIMINGS: dict[str, list[int]] = {
    "center": [320],
    "look_left": [350],
    "look_right": [350],
    # y 轴行程只有 70–110（40°），20° 的移动给 180ms ≈ 110°/s
    "look_up": [180],
    "look_down": [180],
    "look_upper_left": [340],
    "look_upper_right": [340],
    "look_lower_left": [340],
    "look_lower_right": [340],
    # 3 步保持 3 步：tests/test_llm_plan.py 断言 len(steps)==3
    "nod_head": [220, 240, 220],
    "nod_head_fast": [140, 155, 140, 140, 155, 140],
    "nod_head_slow": [350, 400, 350],
    "shake_head": [450, 450, 450],
    "shake_head_fast": [250, 330, 250, 250, 330, 250],
    "shake_head_slow": [600, 800, 600],
    "go_play": [150, 150, 150, 150, 150, 150, 150, 100],
    # clockwise / counterclockwise / dj 不动：单步已是 200–240°/s，再压会丢步
}

# 新增 preset（不存在才追加；已存在不动）。坐标：x=90 正前 / x=30 右 / x=150 左，
# y=70 抬头到底 / y=110 低头到底。
NEW_PRESETS: list[dict] = [
    {
        "id": "tilt_curious",
        "label": "好奇歪头",
        "desc": "侧头微仰看着对方，表达好奇与兴趣",
        "steps": [
            {"x": 70, "y": 78, "xm": 0, "ym": 0, "ms": 400},
            {"x": 70, "y": 78, "xm": 0, "ym": 0, "ms": 500},
        ],
    },
    {
        "id": "perk_up",
        "label": "一激灵",
        "desc": "猛地抬头再回落，表达被吸引注意或惊讶",
        "steps": [
            {"x": 90, "y": 70, "xm": 0, "ym": 0, "ms": 180},
            {"x": 90, "y": 90, "xm": 0, "ym": 0, "ms": 220},
        ],
    },
    {
        "id": "double_take",
        "label": "回头再看一眼",
        "desc": "先撇向一侧再转向另一侧，像回头看第二眼",
        "steps": [
            {"x": 40, "y": 90, "xm": 0, "ym": 0, "ms": 240},
            {"x": 140, "y": 90, "xm": 0, "ym": 0, "ms": 400},
            {"x": 90, "y": 90, "xm": 0, "ym": 0, "ms": 240},
        ],
    },
]


def default_targets() -> list[Path]:
    """模板 + 各设备副本（设备级优先，模板对它无效，必须逐份改）。"""
    targets = [ROOT / "data" / "servo.json"]
    targets += sorted(p for p in (ROOT / "data").glob("*/servo.json") if p.parent.name != "global")
    return targets


def apply_file(path: Path, *, dry_run: bool) -> tuple[int, int, list[str]]:
    """返回 (改写 preset 数, 新增 preset 数, 告警列表)。"""
    notes: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return 0, 0, [f"读取失败: {exc}"]

    presets = data.get("presets")
    if not isinstance(presets, list):
        return 0, 0, ["无 presets 数组，跳过"]

    changed = 0
    for preset in presets:
        if not isinstance(preset, dict):
            continue
        want = TIMINGS.get(str(preset.get("id") or ""))
        steps = preset.get("steps")
        if not want or not isinstance(steps, list):
            continue
        if len(steps) != len(want):
            notes.append(f"{preset['id']}: 步数 {len(steps)} != 预期 {len(want)}，跳过（不改结构）")
            continue
        if [s.get("ms") for s in steps] == want:
            continue
        for step, ms in zip(steps, want, strict=True):
            step["ms"] = ms
        changed += 1
        notes.append(f"{preset['id']}: -> {sum(want)}ms")

    existing = {str(p.get("id") or "") for p in presets if isinstance(p, dict)}
    added = 0
    for new in NEW_PRESETS:
        if new["id"] in existing:
            continue
        presets.append(json.loads(json.dumps(new)))  # 深拷贝，避免共享引用
        added += 1
        notes.append(f"+ 新增 {new['id']}（{sum(s['ms'] for s in new['steps'])}ms）")

    if not dry_run and (changed or added):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed, added, notes


def main() -> int:
    ap = argparse.ArgumentParser(description="按统一速率预算改写 servo.json（幂等）")
    ap.add_argument("--apply", action="store_true", help="真正写盘（缺省只预演）")
    ap.add_argument("paths", nargs="*", help="目标 servo.json（缺省：模板 + 各设备副本）")
    args = ap.parse_args()

    targets = [Path(p) for p in args.paths] if args.paths else default_targets()
    total_changed = total_added = 0
    for path in targets:
        if not path.is_file():
            print(f"[skip] 不存在: {path}")
            continue
        changed, added, notes = apply_file(path, dry_run=not args.apply)
        total_changed += changed
        total_added += added
        tag = "改写" if changed or added else "已是目标值"
        print(f"\n{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}  [{tag}]")
        for n in notes:
            print(f"    {n}")

    print(f"\n合计：改写 {total_changed} 个 preset，新增 {total_added} 个。")
    if not args.apply:
        print("预演结束。确认无误后加 --apply 落盘。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
