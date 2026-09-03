#!/usr/bin/env bash
# 本地一键停止：deskbot-server 主服务 + 全部独立外部子服务（start.sh 的配套停止脚本）
# 用法（在 service 目录）:
#   ./stop.sh
#
# 停止顺序与理由：
#   1. 先 SIGTERM 主服务——主 FastAPI lifespan shutdown 会先停 watchdog，再逐个
#      优雅回收外部服务（ExternalServiceManager.shutdown）。不能反过来先杀外部
#      服务：主服务内的 watchdog 会按指数退避把意外退出的服务自动重启，形成
#      "杀不完"的竞态。
#   2. 主服务退出后，再兜底扫描 data/services/*/*.pid 回收遗留孤儿进程
#      （主服务被 SIGKILL / 卡死超时强杀时的场景）。
#   3. 信号语义与 infrastructure/external/process_supervisor.py 一致：
#      SIGTERM → 宽限（外部服务 5s；主服务 90s，含逐个回收外部服务的时间）→ SIGKILL。
#
# 主服务识别：argv 为 `python -m deskbot_server` 且工作目录 == 本仓库 service/
# （start.sh / setup_venv.sh 启动前均 cd $ROOT）。不能用可执行文件路径锚定：
# macOS 上 venv python 启动时会 re-exec 进 python.org/homebrew 框架二进制，
# ps 显示的 argv0 是 .../Python.app/.../MacOS/Python，与 sys.executable 无
# 字符串关系；cwd 才是两个启动脚本都保证的稳定身份。
#
# 支持 Linux / macOS。Windows Git Bash 下外部服务启动契约（.venv/bin/... 等
# POSIX 路径）本就不可用，本脚本不适用——会提示后退出；
# Windows 上主服务请在运行 start.sh 的终端直接 Ctrl+C 停止。

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/platform.sh"

MAIN_TERM_GRACE_S=90   # 主服务优雅退出宽限（内部逐个回收 6 个外部服务 + uvicorn 收尾）
EXT_TERM_GRACE_S=5     # 外部服务 SIGTERM 宽限（与 ProcessSupervisor.TERM_GRACE_S 一致）
KILL_WAIT_S=3          # SIGKILL 后确认退出的等待
POLL_INTERVAL_S=2      # 主服务退出轮询间隔

# --- 进程原语 -----------------------------------------------------------------

pid_alive() { # $1 pid → 存活返回 0；不合法/不存在返回 1（不发送信号）
  local pid="$1"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

wait_pid_exit() { # $1 pid  $2 宽限秒 → 宽限内退出返回 0，超时返回 1
  local pid="$1" deadline
  deadline=$(( $(date +%s) + $2 ))
  while pid_alive "$pid"; do
    [[ $(date +%s) -ge "$deadline" ]] && return 1
    sleep 1
  done
  return 0
}

# 收集某 pid 的全部后代（逐层递归，输出空格分隔；uvicorn --workers 等派生子进程用）
_desc=""
_desc_collect() {
  local children child
  children="$(ps -A -o pid=,ppid= | awk -v p="$1" '$2 == p {print $1}')"
  for child in $children; do
    _desc="$_desc $child"
    _desc_collect "$child"
  done
}
descendant_pids() {
  _desc=""
  _desc_collect "$1"
  echo "$_desc"
}

# --- 主服务 -------------------------------------------------------------------

# 输出某 pid 的工作目录；取不到输出空
cwd_of() {
  local cwd
  if command -v lsof >/dev/null 2>&1; then
    cwd="$(lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')"
    if [[ -n "$cwd" ]]; then
      echo "$cwd"
      return 0
    fi
  fi
  # Linux 无 lsof 时的兜底（procps 提供 pwdx）
  command -v pwdx >/dev/null 2>&1 && pwdx "$1" 2>/dev/null | sed -n 's/^[0-9]*: //p'
}

# 输出本仓库主服务进程 pid（空格分隔）；无则输出空
find_main_pids() {
  local pid cwd out=""
  # 先按 argv 取候选（模块名全局唯一，可排除外部子服务的 server.py 等进程），
  # 再用 cwd == $ROOT 锚定本仓库实例，排除其它 checkout 的同名进程
  for pid in $(ps -A -o pid=,command= | awk '$3 == "-m" && $4 == "deskbot_server" { print $1 }'); do
    cwd="$(cwd_of "$pid")"
    [[ "$cwd" == "$ROOT" ]] && out="$out $pid"
  done
  echo "$out"
}

kill_hard() { # $1 pid：SIGKILL 进程与其存活后代，确认退出；失败输出到 stderr
  local pid="$1" child
  for child in $(descendant_pids "$pid"); do
    kill -KILL "$child" 2>/dev/null || true
  done
  kill -KILL "$pid" 2>/dev/null || true
  if ! wait_pid_exit "$pid" "$KILL_WAIT_S"; then
    echo "  [error] pid=$pid 无法终止（进程可能处于不可中断状态）" >&2
    return 1
  fi
  return 0
}

stop_main_service() {
  local pids pid still deadline
  pids="$(find_main_pids)"
  if [[ -z "$pids" ]]; then
    echo "  · 主服务未在运行（未找到 cwd=$ROOT 的 python -m deskbot_server 进程）"
    return 0
  fi

  for pid in $pids; do
    echo "  · SIGTERM 主服务 pid=${pid}（其 shutdown 将逐个优雅回收外部子服务）..."
    kill -TERM "$pid" 2>/dev/null || true
  done

  deadline=$(( $(date +%s) + MAIN_TERM_GRACE_S ))
  while [[ $(date +%s) -lt "$deadline" ]]; do
    still=""
    for pid in $pids; do
      pid_alive "$pid" && still="$still $pid"
    done
    if [[ -z "$still" ]]; then
      echo "  · 主服务已优雅退出"
      return 0
    fi
    sleep "$POLL_INTERVAL_S"
  done

  for pid in $still; do
    echo "  · 主服务 pid=$pid 未在 ${MAIN_TERM_GRACE_S}s 内退出，SIGKILL（含存活子进程）"
    kill_hard "$pid" || return 1
  done
  return 0
}

# --- 独立外部子服务（兜底回收，主服务已停止时执行） -----------------------------

stop_external_services() {
  local svc_dir="$ROOT/data/services" f name pid child any=""
  if [[ ! -d "$svc_dir" ]]; then
    echo "  · 无独立子服务数据目录（${svc_dir}），跳过"
    return 0
  fi

  for f in "$svc_dir"/*/*.pid; do
    [[ -f "$f" ]] || continue # 没有任何 pid 文件
    name="$(basename "$(dirname "$f")")"
    pid="$(head -n 1 "$f" 2>/dev/null || true)"
    if ! pid_alive "$pid"; then
      echo "  · 子服务 ${name}：进程已不在，清理过期 pid 文件 $f"
      rm -f "$f"
      continue
    fi

    any="$any $name"
    echo "  · SIGTERM 子服务 $name pid=$pid ..."
    kill -TERM "$pid" 2>/dev/null || true
    if wait_pid_exit "$pid" "$EXT_TERM_GRACE_S"; then
      rm -f "$f"
      continue
    fi

    echo "  · 子服务 $name 未在 ${EXT_TERM_GRACE_S}s 内优雅退出，SIGKILL"
    for child in $(descendant_pids "$pid"); do
      kill -KILL "$child" 2>/dev/null || true # 防 uvicorn --workers 等后代孤儿化
    done
    kill -KILL "$pid" 2>/dev/null || true
    if wait_pid_exit "$pid" "$KILL_WAIT_S"; then
      rm -f "$f"
    else
      echo "  [error] 子服务 $name pid=$pid 无法终止（下次主服务启动时 manager 会再尝试接管）" >&2
    fi
  done

  if [[ -z "$any" ]]; then
    echo "  · 无仍运行的独立子服务（主服务优雅退出时已一并回收）"
  fi
}

# --- main ---------------------------------------------------------------------

main() {
  if platform_is_windows; then
    echo "stop.sh 仅支持 Linux / macOS（外部子服务为 POSIX 启动契约）。" >&2
    echo "Windows 请直接关闭运行 start.sh 的终端（Ctrl+C），或手动结束 python 进程。" >&2
    exit 1
  fi

  echo "== 停止 deskbot-server（主服务 + 独立外部子服务）=="
  stop_main_service
  stop_external_services

  local left
  left="$(find_main_pids)"
  if [[ -n "$left" ]]; then
    echo "[error] 仍有主服务进程存活，请手动处理: $left" >&2
    return 1
  fi
  echo "== 已全部停止 =="
}

main "$@"
