#!/usr/bin/env bash
# llm-engine 外部服务安装：独立 venv + cactus-needle + warmup 下载权重（幂等，可重复执行）
#
# 设计说明：
#   - cactus-needle 是纯 pip 包（模型权重内嵌引擎，首用从 HuggingFace 拉取约 14MB），
#     无需 clone 源码；独立 venv 避免污染主服务 venv
#   - install 里 warmup 触发权重下载（经 hf-mirror 镜像），避免首次启动卡在下载
#   - 失败可重复执行；已存在的步骤跳过
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"   # service/externals/llm-engine
VENV="$ROOT/.venv"

info() { printf '[llm-engine] %s\n' "$*"; }
fail() { printf '[llm-engine] 错误: %s\n' "$*" >&2; exit 1; }

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then UV=uv; return; fi
  if [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; return; fi
  info "未找到 uv，正在安装..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || fail "uv 安装失败"
  UV=uv
}

step_venv() {
  if [ ! -x "$VENV/bin/python" ]; then
    info "创建独立 venv（python 3.11）..."
    "$UV" venv --python 3.11 "$VENV" || fail "venv 创建失败"
  fi
  "$VENV/bin/python" -V
}

step_deps() {
  info "安装 cactus-needle / fastapi / uvicorn ..."
  # uv 创建的 venv 不带 pip，统一用 uv pip install
  "$UV" pip install --python "$VENV/bin/python" --quiet \
    cactus-needle fastapi uvicorn || fail "依赖安装失败（网络？）"
}

step_warmup() {
  # 触发权重下载并验证引擎可加载（失败仅告警，不阻塞安装——启动时会重试）
  info "warmup：下载 Needle 2 权重并验证加载（约 14MB，经 hf-mirror）..."
  if HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}" HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}" \
      "$VENV/bin/python" -c '
import needle
a = needle.Needle()
out = a.complete("hi")
print("warmup ok:", str(out)[:120])
' 2>&1; then
    info "warmup 完成"
  else
    info "warmup 失败（网络/镜像问题？）。服务首次启动时会再次尝试下载权重。"
  fi
}

info "ROOT=$ROOT"
ensure_uv
step_venv
step_deps
step_warmup
info "安装完成。启动：.venv/bin/python server.py --host 127.0.0.1 --port 9104"
