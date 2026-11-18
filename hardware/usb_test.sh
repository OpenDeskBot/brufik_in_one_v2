#!/usr/bin/env bash
# Local audio/display/AEC bench over USB; no server or Wi-Fi required.
set -euo pipefail
MODULE_ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$MODULE_ROOT/.." && pwd)"
PYTHON="$REPO_ROOT/service/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "需要 service/.venv 中的 Python（numpy、pyserial）。" >&2
  exit 1
fi
if [[ $# -eq 0 ]]; then set -- run; fi
exec "$PYTHON" "$MODULE_ROOT/tools/usb_audio_bench.py" "$@"
