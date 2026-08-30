# shellcheck shell=bash
# 跨平台辅助（Linux / macOS / Windows Git Bash），供 start.sh 等脚本 source。

platform_is_windows() {
  case "$(uname -s 2>/dev/null)" in
    MINGW* | MSYS* | CYGWIN*) return 0 ;;
  esac
  case "${OS:-}" in
    Windows_NT) return 0 ;;
  esac
  return 1
}

# 输出 venv 内 Python 可执行文件路径；失败返回非 0。
platform_venv_python() {
  local root="${1:-.}"
  local candidates=(
    "$root/.venv/Scripts/python.exe"
    "$root/.venv/bin/python"
    "$root/.venv/bin/python3"
  )
  local c
  for c in "${candidates[@]}"; do
    if [[ -f "$c" ]]; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

# 检查解释器版本 >= major.minor
platform_python_version_ok() {
  local bin="$1"
  local major="$2"
  local minor="$3"
  "$bin" -c "import sys; exit(0 if sys.version_info >= (${major}, ${minor}) else 1)" 2>/dev/null
}

# 解析为 sys.executable 绝对路径（便于 Windows py launcher）
platform_resolve_python_executable() {
  local launcher="$1"
  if [[ "$launcher" == *" "* ]]; then
    eval "$launcher -c \"import sys; print(sys.executable)\"" 2>/dev/null
  else
    "$launcher" -c "import sys; print(sys.executable)" 2>/dev/null
  fi
}

# 查找满足版本的 Python；输出绝对路径。失败返回 1。
platform_find_python() {
  local req_mm="${1:-3.11}"
  local major="${req_mm%%.*}"
  local minor="${req_mm#*.}"
  minor="${minor%%.*}"

  local launchers=()
  if platform_is_windows; then
    launchers=("py -${major}.${minor}" "py -${major}" "py -3" "python" "python3" "python${major}" "python${major}.${minor}")
  else
    launchers=("python${major}.${minor}" "python${major}" "python3" "python")
  fi

  local launcher exe
  for launcher in "${launchers[@]}"; do
    exe="$(platform_resolve_python_executable "$launcher")" || continue
    if platform_python_version_ok "$exe" "$major" "$minor"; then
      echo "$exe"
      return 0
    fi
  done
  return 1
}

# 检测 libGLESv2（MediaPipe camera_frame 人脸检测在 Linux 上需要）
platform_has_libgles() {
  if platform_is_windows || [[ "$(uname -s 2>/dev/null)" == "Darwin" ]]; then
    return 0
  fi
  if ldconfig -p 2>/dev/null | grep -q 'libGLESv2\.so\.2'; then
    return 0
  fi
  local p
  for p in \
    /usr/lib64/libGLESv2.so.2 \
    /usr/lib/x86_64-linux-gnu/libGLESv2.so.2 \
    /usr/lib/libGLESv2.so.2; do
    if [[ -f "$p" || -L "$p" ]]; then
      return 0
    fi
  done
  return 1
}

platform_libgles_install_hint() {
  if command -v apt-get >/dev/null 2>&1 || command -v apt >/dev/null 2>&1; then
    echo "Debian/Ubuntu: sudo apt install -y libgles2-mesa libegl1-mesa"
  elif command -v dnf >/dev/null 2>&1; then
    echo "RHEL 8+: sudo dnf install -y mesa-libGLES mesa-libEGL"
  elif command -v yum >/dev/null 2>&1; then
    echo "CentOS 7: sudo yum install -y mesa-libGLES mesa-libEGL"
  else
    echo "请安装 mesa 提供的 libGLESv2.so.2（详见 README 人脸检测依赖）"
  fi
}

# 仅检查、不安装系统包；缺依赖时打印警告。
platform_warn_system_deps() {
  local missing=()
  command -v ffmpeg >/dev/null 2>&1 || missing+=("ffmpeg")
  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "[warn] 未检测到: ${missing[*]}" >&2
    if platform_is_windows; then
      echo "[warn] Windows: winget install ffmpeg" >&2
    elif command -v apt-get >/dev/null 2>&1 || command -v apt >/dev/null 2>&1; then
      echo "[warn] Debian/Ubuntu: sudo apt update && sudo apt install -y ffmpeg" >&2
    elif command -v dnf >/dev/null 2>&1; then
      echo "[warn] RHEL 8+: sudo dnf install -y epel-release && sudo dnf install -y ffmpeg" >&2
    elif command -v yum >/dev/null 2>&1; then
      echo "[warn] CentOS 7: sudo yum install -y epel-release && sudo yum install -y ffmpeg" >&2
    else
      echo "[warn] 请安装 ffmpeg（详见仓库 README「ffmpeg」）。" >&2
    fi
    echo "[warn] 缺少 ffmpeg 时 opus 转码可能失败；可设 SKIP_SYSTEM_CHECK=1 跳过此检查。" >&2
  fi

  if ! platform_has_libgles; then
    echo "[warn] 未检测到 libGLESv2.so.2（camera_frame 人脸检测/MediaPipe 需要）。" >&2
    echo "[warn] $(platform_libgles_install_hint)" >&2
    echo "[warn] 未安装时人脸推理会失败；语音 /asr_chat 不受影响。" >&2
  fi
  return 0
}

# Git Bash 下 wait_tcp：优先 /dev/tcp，失败则用 python 探测。
platform_wait_tcp() {
  local host="$1"
  local port="$2"
  local max_wait="${3:-120}"
  local i

  for ((i = 0; i < max_wait; i++)); do
    if (echo >/dev/tcp/"$host"/"$port") 2>/dev/null; then
      return 0
    fi
    sleep 1
  done

  local py
  py="$(command -v python 2>/dev/null || command -v python3 2>/dev/null || true)"
  if [[ -z "$py" ]]; then
    return 1
  fi
  for ((i = 0; i < max_wait; i++)); do
    if "$py" -c "import socket; s=socket.create_connection(('$host', $port), 1); s.close()" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

platform_run_sh() {
  local script="$1"
  shift
  if [[ -x "$script" ]]; then
    "$script" "$@"
  else
    bash "$script" "$@"
  fi
}

