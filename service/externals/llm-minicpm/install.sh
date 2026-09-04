#!/usr/bin/env bash
# llm-minicpm 外部服务安装：llama.cpp llama-server 二进制 + MiniCPM5-1B-Q4_K_M.gguf 模型（幂等，可重复执行）
#
# 设计说明：
#   - llama-server 是 llama.cpp 官方 release 二进制，安装时按平台自动选择资产：
#     macOS arm64/x64（Metal 加速）/ Linux x86_64、aarch64（官方 ubuntu 前缀 CPU 构建）；
#     下载解压到本目录 bin/，随服务自包含、可卸载
#   - 官方 Linux 构建是 CPU 版；需要 GPU 加速（vulkan/rocm/cuda）时手动改 PLATFORM
#     为对应资产后缀（如 ubuntu-vulkan-x64）即可，URL 会自动拼上
#   - 模型从 HuggingFace 拉取（默认走 hf-mirror 镜像，同 tts-engine 经验），约 657MB
#   - 失败可重复执行；已下载的文件校验通过后跳过；bin/ 带 .platform 标记，
#     平台不一致（如从 macOS 拷贝过来）会清空重下，不会拿错平台的二进制
# 部署参考：https://github.com/OpenBMB/MiniCPM/blob/main/docs/deployment/llama_cpp.md
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"   # service/externals/llm-minicpm
BIN_DIR="$ROOT/bin"
MODELS_DIR="$ROOT/models"
MODEL_FILE="$MODELS_DIR/MiniCPM5-1B-Q4_K_M.gguf"

LLAMA_VERSION="b10717"

info() { printf '[llm-minicpm] %s\n' "$*"; }
fail() { printf '[llm-minicpm] 错误: %s\n' "$*" >&2; exit 1; }

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
# 下载源回退链（GitHub 直连可能不通；gh-proxy 镜像兜底，同 vpr-engine 经验）
LLAMA_URLS=(
  "https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_VERSION}/${LLAMA_ASSET}"
  "https://gh-proxy.com/https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_VERSION}/${LLAMA_ASSET}"
)
# 模型镜像优先（国内直连 huggingface.co 不稳）
MODEL_URLS=(
  "https://hf-mirror.com/openbmb/MiniCPM5-1B-GGUF/resolve/main/MiniCPM5-1B-Q4_K_M.gguf"
  "https://huggingface.co/openbmb/MiniCPM5-1B-GGUF/resolve/main/MiniCPM5-1B-Q4_K_M.gguf"
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
      last_err="curl 失败: $url"
      info "  下载失败，尝试下一镜像"
    fi
  done
  fail "${label} 所有下载源均失败（${last_err}）"
}

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
  tmp_archive="$(mktemp -t llm-minicpm.XXXXXX).tar.gz"
  trap 'rm -f "${tmp_archive}"' RETURN
  download_first_ok "$tmp_archive" "llama-server 二进制（${LLAMA_ASSET}）" "${LLAMA_URLS[@]}"
  info "解压 ${LLAMA_ASSET} → bin/"
  # 归档顶层是 bin/ 目录（含 llama-server/llama-cli/llama-quantize 等），strip 掉后全部保留
  tar -xzf "$tmp_archive" -C "$BIN_DIR" --strip-components=1 || fail "解压失败，请手动检查 ${tmp_archive}"
  chmod +x "$BIN_DIR/llama-server"
  # 先执行校验再写标记：连--version 都跑不起来（错平台/缺 glibc）就不算装好，重跑会重下
  "$BIN_DIR/llama-server" --version 2>&1 | head -2 || fail "llama-server 不可执行（平台 ${PLATFORM} 与系统不符，或缺少系统库）"
  echo "$PLATFORM" > "$BIN_DIR/.platform"
  info "llama-server ${LLAMA_VERSION}（${PLATFORM}）就绪"
}

step_model() {
  # wc -c 而非 stat -f %z：后者是 BSD/macOS 专属参数，Linux GNU stat 会报错导致恒判校验失败
  if [ -f "$MODEL_FILE" ] && [ "$(head -c 4 "$MODEL_FILE")" = "GGUF" ] && [ "$(wc -c < "$MODEL_FILE")" -gt 600000000 ]; then
    info "模型已存在且校验通过（$(du -h "$MODEL_FILE" | cut -f1)），跳过"
    return
  fi
  mkdir -p "$MODELS_DIR"
  if [ -f "$MODEL_FILE" ]; then
    # 校验失败（中断下载的残件）：删掉重下，避免 -C - 续上损坏数据
    info "模型文件不完整，删除后重新下载"
    rm -f "$MODEL_FILE"
  fi
  download_first_ok "$MODEL_FILE" "MiniCPM5-1B-Q4_K_M.gguf（约 657MB）" "${MODEL_URLS[@]}"
  [ "$(head -c 4 "$MODEL_FILE")" = "GGUF" ] || fail "模型文件校验失败（非 GGUF 格式）"
  info "模型就绪：$(du -h "$MODEL_FILE" | cut -f1)"
}

info "ROOT=${ROOT}"
info "平台=${PLATFORM}"
step_binary
step_model
info "安装完成。启动：bin/llama-server -m models/MiniCPM5-1B-Q4_K_M.gguf --host 127.0.0.1 --port 9105 -ngl 99 -c 8192 --jinja"
