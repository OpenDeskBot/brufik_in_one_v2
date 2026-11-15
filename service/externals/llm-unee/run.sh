#!/usr/bin/env bash
# llm-unee 启动包装器（manager 的 start.command 是 `bash run.sh`）。
#
# 为什么需要包装器：unee 0.3.0 的 `serve` 在被 SIGTERM 时并不可靠回收内部
# llama-server（实测：uvicorn 退出后 llama-server 变成 PPID=1 的孤儿，继续占内存）；
# 而 manager 只对直接子进程发 SIGTERM、5s 后 SIGKILL，更无法触及孙进程。
# 本脚本把 unee 放进独立进程组，停止时整组 TERM → 3s → KILL，保证内部
# llama-server 一起退出；manager 的 5s 宽限内完成清理。
#
# 另外：
#   - llama-server 日志超过 20MB 时启动前轮转（manager 只轮转自己的 unee.log）
#   - 上次被 SIGKILL 遗留的进程组记录在 logs/unee.pgid，下次启动前回收
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs

MODEL="${UNEE_MODEL:-models/unee-0.8b-Q4_K_M.gguf}"
HOST="${UNEE_HOST:-127.0.0.1}"
PORT="${UNEE_PORT:-9107}"
GPU_LAYERS="${UNEE_GPU_LAYERS:-99}"
SLOTS="${UNEE_SLOTS:-2}"
CTX="${UNEE_CTX:-16384}"
MODEL_NAME="${UNEE_MODEL_NAME:-unee}"
LLAMA_SERVER="${UNEE_LLAMA_SERVER:-bin/llama-server}"
LLAMA_LOG="logs/llama-server.log"
PGID_FILE="logs/unee.pgid"

# 控制 unee 的 --llama-log 大小（manager 不会轮转服务目录内的文件）
if [ -f "$LLAMA_LOG" ] && [ "$(wc -c < "$LLAMA_LOG")" -gt 20971520 ]; then
  mv -f "$LLAMA_LOG" "$LLAMA_LOG.1"
fi

# 回收上次被 SIGKILL 遗留的进程组（此时 manager 的 pid 文件已失效，只能靠这里）
if [ -f "$PGID_FILE" ]; then
  old="$(cat "$PGID_FILE" 2>/dev/null || true)"
  if [ -n "$old" ] && pgrep -g "$old" >/dev/null 2>&1 \
    && ps -axo pgid=,command= | awk -v g="$old" '$1 == g' | grep -qE 'unee|llama-server'; then
    echo "[llm-unee] 回收上次遗留进程组 pgid=$old"
    kill -TERM -- "-$old" 2>/dev/null || true
    sleep 1
    kill -KILL -- "-$old" 2>/dev/null || true
  fi
  rm -f "$PGID_FILE"
fi

# set -m：让后台 unee 进入以自身 pid 为 PGID 的独立进程组，便于整组回收
set -m
.venv/bin/unee serve \
  --model "$MODEL" \
  --host "$HOST" \
  --port "$PORT" \
  --gpu-layers "$GPU_LAYERS" \
  --slots "$SLOTS" \
  --ctx "$CTX" \
  --model-name "$MODEL_NAME" \
  --llama-server "$LLAMA_SERVER" \
  --llama-log "$LLAMA_LOG" &
child=$!
set +m
echo "$child" > "$PGID_FILE"

cleanup() {
  trap - TERM INT EXIT
  # 先 TERM 整组（unee + 内部 llama-server），最多等 3s，再 KILL 整组
  kill -TERM -- "-$child" 2>/dev/null || kill -TERM "$child" 2>/dev/null || true
  for _ in $(seq 1 30); do
    kill -0 "$child" 2>/dev/null || break
    sleep 0.1
  done
  kill -KILL -- "-$child" 2>/dev/null || true
  rm -f "$PGID_FILE"
}
trap cleanup TERM INT EXIT

code=0
wait "$child" || code=$?
cleanup
exit "$code"
