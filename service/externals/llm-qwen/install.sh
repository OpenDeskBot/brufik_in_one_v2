#!/usr/bin/env bash
# llm-qwen 外部服务安装：llama.cpp llama-server 二进制 + Qwen3.8-2B-Distill Q4_K_M 模型（幂等，可重复执行）
#
# 设计说明：
#   - llama-server 是 llama.cpp 官方 macOS arm64 release 二进制（Metal 加速，无 Python 依赖），
#     下载解压到本目录 bin/，随服务自包含、可卸载
#   - 版本固定 b10733：qwen35（Gated DeltaNet）架构需 ≥ b10360，且 b10733 含 Gated DeltaNet
#     fused Metal kernel（PR #19504，Apple Silicon 不走 CPU 回退）
#   - 模型从 HuggingFace 拉取（默认走 hf-mirror 镜像，同 llm-minicpm 经验），约 1.31GB
#   - 失败可重复执行；已下载的文件校验通过后跳过
# 部署参考：https://huggingface.co/empero-ai/Qwen3.8-2B-Distill-GGUF
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"   # service/externals/llm-qwen
BIN_DIR="$ROOT/bin"
MODELS_DIR="$ROOT/models"
MODEL_FILE="$MODELS_DIR/Qwen3.8-2B-Q4_K_M.gguf"

LLAMA_VERSION="b10733"
LLAMA_ASSET="llama-${LLAMA_VERSION}-bin-macos-arm64.tar.gz"
# 下载源回退链（GitHub 直连可能不通；gh-proxy 镜像兜底，同 vpr-engine 经验）
LLAMA_URLS=(
  "https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_VERSION}/${LLAMA_ASSET}"
  "https://gh-proxy.com/https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_VERSION}/${LLAMA_ASSET}"
)
# 模型镜像优先（国内直连 huggingface.co 不稳）
MODEL_URLS=(
  "https://hf-mirror.com/empero-ai/Qwen3.8-2B-Distill-GGUF/resolve/main/Qwen3.8-2B-Q4_K_M.gguf"
  "https://huggingface.co/empero-ai/Qwen3.8-2B-Distill-GGUF/resolve/main/Qwen3.8-2B-Q4_K_M.gguf"
)

info() { printf '[llm-qwen] %s\n' "$*"; }
fail() { printf '[llm-qwen] 错误: %s\n' "$*" >&2; exit 1; }

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

step_binary() {
  if [ -x "$BIN_DIR/llama-server" ]; then
    info "llama-server 已存在（${BIN_DIR}/llama-server），跳过"
    return
  fi
  mkdir -p "$BIN_DIR"
  local tmp_archive
  tmp_archive="$(mktemp -t llm-qwen.XXXXXX).tar.gz"
  trap 'rm -f "${tmp_archive}"' RETURN
  download_first_ok "$tmp_archive" "llama-server 二进制（约 11MB）" "${LLAMA_URLS[@]}"
  info "解压 ${LLAMA_ASSET} → bin/"
  # 归档顶层是 bin/ 目录（含 llama-server/llama-cli/llama-quantize 等），strip 掉后全部保留
  tar -xzf "$tmp_archive" -C "$BIN_DIR" --strip-components=1 || fail "解压失败，请手动检查 ${tmp_archive}"
  chmod +x "$BIN_DIR/llama-server"
  "$BIN_DIR/llama-server" --version 2>&1 | head -2 || fail "llama-server 不可执行"
  info "llama-server ${LLAMA_VERSION} 就绪"
}

step_model() {
  if [ -f "$MODEL_FILE" ] && [ "$(head -c 4 "$MODEL_FILE")" = "GGUF" ] && [ "$(stat -f %z "$MODEL_FILE")" -gt 1200000000 ]; then
    info "模型已存在且校验通过（$(du -h "$MODEL_FILE" | cut -f1)），跳过"
    return
  fi
  mkdir -p "$MODELS_DIR"
  if [ -f "$MODEL_FILE" ]; then
    # 校验失败（中断下载的残件）：删掉重下，避免 -C - 续上损坏数据
    info "模型文件不完整，删除后重新下载"
    rm -f "$MODEL_FILE"
  fi
  download_first_ok "$MODEL_FILE" "Qwen3.8-2B-Q4_K_M.gguf（约 1.31GB）" "${MODEL_URLS[@]}"
  [ "$(head -c 4 "$MODEL_FILE")" = "GGUF" ] || fail "模型文件校验失败（非 GGUF 格式）"
  info "模型就绪：$(du -h "$MODEL_FILE" | cut -f1)"
}

info "ROOT=${ROOT}"
step_binary
step_model
info "安装完成。启动：bin/llama-server -m models/Qwen3.8-2B-Q4_K_M.gguf --host 127.0.0.1 --port 9106 -ngl 99 -c 8192 --jinja -rea off --alias qwen3.8-2b"
