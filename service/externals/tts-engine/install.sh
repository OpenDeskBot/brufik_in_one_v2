#!/usr/bin/env bash
# MOSS-TTS-Nano 外部服务安装：clone 源码 + 独立 venv + 依赖（幂等，可重复执行）
#
# 设计说明：
#   - 用 ONNX CPU 后端（官方推荐：无 PyTorch 依赖，单核可跑，比 PyTorch 快约 2 倍）
#   - torch/torchaudio 仅 PyTorch 后端需要，刻意跳过以缩小安装体积
#   - WeTextProcessing（依赖 pynini）只用预编译 wheel（--only-binary :all:），
#     不做源码编译：PyPI 无 macOS arm64 wheel 属预期（见
#     https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/6），跳过时由
#     patch_wetext.py 降级为 robust fallback 归一化；有 wheel 的平台（如 Linux）照常装上
#   - 模型在首次 serve 时自动下载；可用 HF_ENDPOINT 环境变量换镜像
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"   # service/externals/tts-engine
CHECKOUT="$ROOT/checkout"
VENV="$ROOT/.venv"
GIT_URL="https://github.com/OpenMOSS/MOSS-TTS-Nano.git"

info()  { printf '[tts-engine] %s\n' "$*"; }
fail()  { printf '[tts-engine] 错误: %s\n' "$*" >&2; exit 1; }

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then UV=uv; return; fi
  if [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; return; fi
  info "未找到 uv，正在安装..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || fail "uv 安装失败"
  UV=uv
}

step_clone() {
  if [ ! -d "$CHECKOUT/.git" ]; then
    info "clone MOSS-TTS-Nano ..."
    git clone --depth 1 "$GIT_URL" "$CHECKOUT" || fail "git clone 失败"
  else
    info "checkout 已存在，尝试 git pull 更新（失败不影响现有代码）"
    (cd "$CHECKOUT" && git pull --ff-only) >/dev/null 2>&1 || info "pull 跳过（网络或本地改动）"
  fi
}

step_venv() {
  if [ ! -x "$VENV/bin/python" ]; then
    info "创建独立 venv（python 3.12，由 uv 解析/下载）..."
    "$UV" venv --python 3.12 "$VENV" || fail "venv 创建失败"
  fi
  "$VENV/bin/python" -V
}

step_core_deps() {
  info "安装核心依赖（numpy/onnxruntime 等）..."
  # uv 创建的 venv 不带 pip，统一用 uv pip install
  "$UV" pip install --python "$VENV/bin/python" --quiet \
    numpy fastapi uvicorn python-multipart sentencepiece transformers soundfile onnxruntime \
    || fail "核心依赖安装失败（网络？）"
  # app_onnx 顶层 import app（legacy），app.py 顶层 import torch——
  # ONNX 推理用不到 torch，但 import 链绕不开，装官方版本（macOS arm64 有 wheel）
  info "安装 torch/torchaudio（import 链需要；ONNX 推理不调用）..."
  "$UV" pip install --python "$VENV/bin/python" --quiet torch==2.7.0 torchaudio==2.7.0 \
    || fail "torch 安装失败（网络？）"
  # --no-deps 装 CLI 入口，依赖已手动指定，避免 pyproject 重复解析
  "$UV" pip install --python "$VENV/bin/python" --quiet --no-deps \
    -e "$CHECKOUT" || fail "moss-tts-nano CLI 安装失败"
}

step_text_norm() {
  # --only-binary :all: 保证只认预编译 wheel：没有 wheel 的平台（如 macOS arm64）
  # 立即快速失败并跳过，不会触发 pynini/OpenFST 源码编译；归一化由 patch 降级
  info "安装文本归一化 WeTextProcessing（仅预编译 wheel；无 wheel 平台自动跳过）..."
  if "$UV" pip install --python "$VENV/bin/python" --quiet --only-binary :all: WeTextProcessing; then
    info "WeTextProcessing 安装成功"
  else
    info "WeTextProcessing 无可用预编译 wheel，跳过（归一化已由 patch_wetext.py 降级为 robust fallback，不影响功能）"
  fi
}

step_patch_wetext_degradation() {
  # WeTextProcessing 装不上时 MOSS 的 warmup 会硬失败导致 /api/generate 全 500。
  # patch 两处让其在缺失时降级为 robust fallback 归一化（幂等：已 patch 则跳过）。
  # 注意：不用 heredoc——bash 5.3 在继承终端 stdin 时 heredoc 会死锁（管理后台场景）
  "$VENV/bin/python" "$ROOT/patch_wetext.py" \
    "$CHECKOUT/app.py" "$CHECKOUT/text_normalization_pipeline.py"
}

info "ROOT=$ROOT"
ensure_uv
step_clone
step_venv
step_core_deps
step_text_norm
step_patch_wetext_degradation
info "安装完成。首次启动会自动下载 ONNX 模型（约 740MB，经 hf-mirror 镜像；"
info "  HF_HUB_DISABLE_XET=1 已由 service.yaml 注入）"
