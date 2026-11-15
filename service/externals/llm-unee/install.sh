#!/usr/bin/env bash
# llm-unee 外部服务安装（幂等，可在管理后台重复执行）：
# 1. llama.cpp llama-server 二进制（官方 release，b10733：qwen35/Gated DeltaNet 需要 ≥ b10360）
# 2. 独立 venv + pip 安装 unee==0.3.0（仅 httpx/starlette/uvicorn，不污染主服务 venv）
# 3. unee-0.8b-Q4_K_M.gguf（约 505MB）：优先复用已有文件 / UNEE_MODEL_SRC 本地拷贝 /
#    本机 sibling checkout 自动复用，否则走 hf-mirror → huggingface.co 下载；
#    大小 + SHA256 精确校验
# 4. run.sh：启动包装器（整组回收 unee + 内部 llama-server，见 run.sh 注释）
#
# 用法：
#   bash externals/llm-unee/install.sh
#   UNEE_MODEL_SRC=/Users/mi/work/src/unee/models/unee-0.8b-Q4_K_M.gguf \
#     bash externals/llm-unee/install.sh
#
# 已安装步骤自动跳过，中断后重跑即可续装。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"   # service/externals/llm-unee
BIN_DIR="$ROOT/bin"
VENV_DIR="$ROOT/.venv"
MODELS_DIR="$ROOT/models"
MODEL_FILE="$MODELS_DIR/unee-0.8b-Q4_K_M.gguf"

UNEE_VERSION="0.3.0"
LLAMA_VERSION="b10733"
# unee-0.8b-Q4_K_M.gguf 的精确大小与 SHA256（与 HuggingFace LFS oid 一致），
# 防止半包/损坏文件被当成有效模型
MODEL_SIZE=529296928
MODEL_SHA256="34b783d5882644efe89a33e532be29de160ee619e095bb60e1d089f300515e38"

info() { printf '[llm-unee] %s\n' "$*"; }
fail() { printf '[llm-unee] 错误: %s\n' "$*" >&2; exit 1; }

# ---- 平台探测：llama.cpp 官方 release 资产按 OS/arch 分（Linux 资产沿用 ubuntu 命名）----
detect_platform() {
  local os arch
  os="$(uname -s)"
  arch="$(uname -m)"
  case "${os}-${arch}" in
    Darwin-arm64)  echo macos-arm64 ;;   # Metal 加速（本机默认）
    Darwin-x86_64) echo macos-x64 ;;
    Linux-x86_64)  echo ubuntu-x64 ;;    # 官方 CPU 构建；GPU 版需手动换 vulkan/rocm/cuda 资产
    Linux-aarch64) echo ubuntu-arm64 ;;
    *) fail "不支持的平台 ${os}-${arch}（仅支持 macOS arm64/x64 与 Linux x86_64/aarch64）" ;;
  esac
}
PLATFORM="$(detect_platform)"
LLAMA_ASSET="llama-${LLAMA_VERSION}-bin-${PLATFORM}.tar.gz"
# 下载源回退链（GitHub 直连可能不通；gh-proxy 镜像兜底，同 llm-qwen 经验）
LLAMA_URLS=(
  "https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_VERSION}/${LLAMA_ASSET}"
  "https://gh-proxy.com/https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_VERSION}/${LLAMA_ASSET}"
)
# 模型镜像优先（国内直连 huggingface.co 不稳）
MODEL_URLS=(
  "https://hf-mirror.com/uneeverse/unee-0.8b-GGUF/resolve/main/unee-0.8b-Q4_K_M.gguf"
  "https://huggingface.co/uneeverse/unee-0.8b-GGUF/resolve/main/unee-0.8b-Q4_K_M.gguf"
)

download_first_ok() {
  # download_first_ok <dest> <label> <url...>：依次尝试各 URL，首个成功的返回 0
  local dest="$1" label="$2"; shift 2
  local last_err=""
  for url in "$@"; do
    info "下载 ${label}：${url}"
    if curl -fL --retry 3 --connect-timeout 15 -C - -o "$dest" "$url" 2>/dev/null; then
      return 0
    else
      last_err="curl 失败: ${url}"
      info "  下载失败，尝试下一镜像"
    fi
  done
  fail "${label} 所有下载源均失败（${last_err}）"
}

sha256_of() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    fail "缺少 shasum/sha256sum，无法校验模型完整性"
  fi
}

file_valid() {
  [ -f "$1" ] \
    && [ "$(head -c 4 "$1" 2>/dev/null || true)" = "GGUF" ] \
    && [ "$(wc -c < "$1")" -eq "$MODEL_SIZE" ] \
    && [ "$(sha256_of "$1")" = "$MODEL_SHA256" ]
}

model_valid() { file_valid "$MODEL_FILE"; }

step_binary() {
  # 跳过条件 = .platform 标记与当前平台一致 且 llama-server 可执行。
  # bin/ 不被 git 跟踪，但整目录拷贝（macOS→Linux）会带着错平台二进制：
  # 无标记/标记不一致一律清空重下，避免 spawn 时 Exec format error
  if [ "$(cat "$BIN_DIR/.platform" 2>/dev/null || true)" = "$PLATFORM" ] && [ -x "$BIN_DIR/llama-server" ]; then
    info "llama-server 已存在（${BIN_DIR}/llama-server，${PLATFORM}），跳过"
    return
  fi
  if [ -e "$BIN_DIR" ]; then
    info "bin/ 平台标记不匹配或缺失，清空后重新下载（${PLATFORM}）"
    rm -rf "$BIN_DIR"
  fi
  mkdir -p "$BIN_DIR"
  local tmp_archive
  tmp_archive="$(mktemp -t llm-unee.XXXXXX).tar.gz"
  trap 'rm -f "${tmp_archive}"' RETURN
  download_first_ok "$tmp_archive" "llama-server 二进制（${LLAMA_ASSET}）" "${LLAMA_URLS[@]}"
  info "解压 ${LLAMA_ASSET} → bin/"
  # 归档顶层是 bin/ 目录（含 llama-server/llama-cli 等），strip 掉后全部保留
  tar -xzf "$tmp_archive" -C "$BIN_DIR" --strip-components=1 || fail "解压失败，请手动检查 ${tmp_archive}"
  chmod +x "$BIN_DIR/llama-server"
  # 先执行校验再写标记：连--version 都跑不起来（错平台/缺 glibc）就不算装好，重跑会重下
  "$BIN_DIR/llama-server" --version 2>&1 | head -2 || fail "llama-server 不可执行（平台 ${PLATFORM} 与系统不符，或缺少系统库）"
  echo "$PLATFORM" > "$BIN_DIR/.platform"
  info "llama-server ${LLAMA_VERSION}（${PLATFORM}）就绪"
}

find_python() {
  # 优先主服务 venv 的 Python（3.11，与本项目其它服务一致）；unee 仅要求 >=3.10
  local cand
  for cand in "$ROOT/../../.venv/bin/python" python3.12 python3.13 python3.11 python3.10; do
    if [ -x "$cand" ] || command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null; then
        printf '%s\n' "$cand"
        return 0
      fi
    fi
  done
  return 1
}

step_venv() {
  local py
  if ! py="$(find_python)"; then
    fail "找不到 Python >= 3.10，请安装 python3.11 后重试"
  fi
  # 已存在的 venv 若解释器不可用/低于 3.10 → 重建（幂等自愈）
  if [ -e "$VENV_DIR" ]; then
    if ! "$VENV_DIR/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null; then
      info "现有 .venv 不可用或 Python < 3.10，重建"
      rm -rf "$VENV_DIR"
    fi
  fi
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    info "用 ${py} 创建独立 venv"
    "$py" -m venv "$VENV_DIR"
  fi
  if "$VENV_DIR/bin/python" -c 'import unee, sys; raise SystemExit(0 if unee.__version__ == sys.argv[1] else 1)' "$UNEE_VERSION" 2>/dev/null; then
    info "unee ${UNEE_VERSION} 已安装，跳过 pip"
    return
  fi
  info "安装 unee==${UNEE_VERSION}（独立 venv）"
  "$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
  if ! "$VENV_DIR/bin/python" -m pip install "unee==${UNEE_VERSION}"; then
    info "默认 PyPI 失败，改用清华镜像重试"
    "$VENV_DIR/bin/python" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple "unee==${UNEE_VERSION}"
  fi
  "$VENV_DIR/bin/python" -c 'import unee; print(f"[llm-unee] unee {unee.__version__} 安装完成")'
}

step_model() {
  if model_valid; then
    info "模型已存在且校验通过（$(du -h "$MODEL_FILE" | cut -f1)），跳过"
    return
  fi
  mkdir -p "$MODELS_DIR"
  rm -f "$MODEL_FILE" "$MODEL_FILE.part"
  local src=""

  # 1) 显式指定本地来源（文件或目录）——校验不过直接报错，不静默改下载
  if [ -n "${UNEE_MODEL_SRC:-}" ]; then
    src="$UNEE_MODEL_SRC"
    if [ -d "$src" ]; then
      src="$src/unee-0.8b-Q4_K_M.gguf"
    fi
    [ -f "$src" ] || fail "UNEE_MODEL_SRC 指向的模型不存在: $src"
    file_valid "$src" || fail "本地模型校验失败（大小/SHA256 不符）: $src"
    info "从本地拷贝模型：$src"
    cp -f "$src" "$MODEL_FILE.part" || fail "拷贝模型失败: $src"
    file_valid "$MODEL_FILE.part" || fail "拷贝后校验失败: $src"
    mv -f "$MODEL_FILE.part" "$MODEL_FILE"
    info "模型就绪（本地拷贝）：$(du -h "$MODEL_FILE" | cut -f1)"
    return
  fi

  # 2) 本机 sibling checkout 便捷复用（/Users/mi/work/src/unee 与仓库同级时）
  src="$ROOT/../../../../unee/models/unee-0.8b-Q4_K_M.gguf"
  if [ -f "$src" ] && file_valid "$src"; then
    info "发现本机已安装的 unee 模型，直接复用：$src"
    cp -f "$src" "$MODEL_FILE.part" || fail "拷贝模型失败: $src"
    file_valid "$MODEL_FILE.part" || fail "拷贝后校验失败: $src"
    mv -f "$MODEL_FILE.part" "$MODEL_FILE"
    info "模型就绪（复用本机副本）：$(du -h "$MODEL_FILE" | cut -f1)"
    return
  fi

  # 3) 下载（不用 -C - 续传：残件 SHA 不可信，每个镜像都从头下）
  local url
  for url in "${MODEL_URLS[@]}"; do
    info "下载 unee-0.8b-Q4_K_M.gguf（约 505MB）：${url}"
    rm -f "$MODEL_FILE.part"
    if ! curl -fL --retry 3 --connect-timeout 15 -o "$MODEL_FILE.part" "$url"; then
      info "  下载失败，尝试下一镜像"
      continue
    fi
    if ! file_valid "$MODEL_FILE.part"; then
      info "  文件校验失败（大小/SHA256 不符），尝试下一镜像"
      rm -f "$MODEL_FILE.part"
      continue
    fi
    mv -f "$MODEL_FILE.part" "$MODEL_FILE"
    info "模型就绪：$(du -h "$MODEL_FILE" | cut -f1)"
    return
  done
  fail "模型所有下载源均失败或校验不通过"
}

info "ROOT=${ROOT}"
info "平台=${PLATFORM}"
step_binary
step_venv
step_model
# unee 的 --llama-log 用 open(..., "a")，父目录不存在会直接失败
mkdir -p "$ROOT/logs"
info "安装完成。启动：bash run.sh（由独立服务管理器的 start 命令调用；直接跑 unee serve 会失去进程组回收保护）"
