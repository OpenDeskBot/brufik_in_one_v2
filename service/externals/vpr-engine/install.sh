#!/usr/bin/env bash
# vpr-engine 安装脚本（幂等，可在管理后台重复执行）：
# 1. 独立 venv（主服务 venv 无 wespeaker 依赖，不污染主服务）
# 2. pip 安装 wespeaker（自带 torch/torchaudio，macOS CPU wheel）
# 3. 下载 WeSpeaker CN-Celeb ResNet34 模型（256 维 speaker embedding，~100MB）
#    ——国内默认 hf-mirror 镜像 + 禁用 xet（镜像不支持会 401）
# 已安装步骤自动跳过，中断后重跑即可续装。
set -euo pipefail
cd "$(dirname "$0")"

MODEL_REPO="wespeaker/wespeaker-cnceleb-resnet34"
MODEL_DIR="models/wespeaker-cnceleb-resnet34"
# wespeaker 不在 PyPI（官方/镜像 404），唯一官方安装方式是从 GitHub 源码装：
#   pip install git+https://github.com/wenet-e2e/wespeaker.git
# 依赖（torch/torchaudio/kaldiio/librosa 等）由 pip 从配置的镜像（清华）解析。
WESPEAKER_URL="${WESPEAKER_URL:-https://github.com/wenet-e2e/wespeaker.git}"

# 选建 venv 的 Python：wespeaker 无 3.14+ wheel，系统默认 python3 可能是新版。
# 优先主服务 venv 同款（3.11，wespeaker 时代兼容），再退 3.12/3.13/3.11/3.10。
MAIN_VENV_PY="$PWD/../../.venv/bin/python"
PYTHON_BIN=""
if [ -x "$MAIN_VENV_PY" ]; then
  PYTHON_BIN="$MAIN_VENV_PY"
else
  for cand in python3.12 python3.13 python3.11 python3.10; do
    if command -v "$cand" >/dev/null 2>&1; then
      PYTHON_BIN="$(command -v "$cand")"
      break
    fi
  done
fi
if [ -z "$PYTHON_BIN" ]; then
  echo "[vpr-engine] 找不到可用的 Python 3.10~3.13（wespeaker 不支持 3.14+），请安装 python3.11/3.12 后重试" >&2
  exit 1
fi
PY_VER="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

# 已存在的 .venv 版本不匹配（如系统默认 3.14 建的残留）→ 重建，保证幂等可自愈
if [ -x .venv/bin/python ]; then
  CUR_VER="$(.venv/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  if [ "$CUR_VER" != "$PY_VER" ]; then
    echo "[vpr-engine] .venv Python $CUR_VER 与目标 $PY_VER 不符，重建 venv..."
    rm -rf .venv
  fi
fi
if [ ! -d .venv ]; then
  echo "[vpr-engine] 用 $PYTHON_BIN ($PY_VER) 创建独立 venv..."
  "$PYTHON_BIN" -m venv .venv
fi

echo "[vpr-engine] 安装 wespeaker 依赖（源码装，pip 依赖走镜像）..."
.venv/bin/python -m pip install --upgrade pip
# 渠道回退链：GitHub 直连（国外/代理环境）→ codeload 官方归档（国内常可达）
# → gh-proxy 镜像。失败自动降级，全部失败才报错。
if ! .venv/bin/pip install "wespeaker @ git+$WESPEAKER_URL"; then
  echo "[vpr-engine] GitHub 直装失败，改用 codeload 官方归档..."
  if ! .venv/bin/pip install "https://codeload.github.com/wenet-e2e/wespeaker/tar.gz/refs/heads/master"; then
    echo "[vpr-engine] codeload 失败，改用 gh-proxy 镜像..."
    .venv/bin/pip install "https://gh-proxy.com/https://github.com/wenet-e2e/wespeaker/archive/refs/heads/master.tar.gz"
  fi
fi
# wespeaker diar（说话人分离）模块顶层 import onnxruntime——即使不用 diar 也会被拉入
.venv/bin/pip install onnxruntime
# HTTP 服务框架（独立 venv，主服务 venv 的 fastapi/uvicorn 不共享）
.venv/bin/pip install fastapi uvicorn

if [ -f "$MODEL_DIR/config.json" ]; then
  echo "[vpr-engine] 模型已存在，跳过下载"
else
  echo "[vpr-engine] 下载模型 $MODEL_REPO ..."
  mkdir -p models
  export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
  export HF_HUB_DISABLE_XET=1
  .venv/bin/python - "$MODEL_REPO" "$MODEL_DIR" <<'PY'
import sys
from huggingface_hub import snapshot_download

repo, dst = sys.argv[1], sys.argv[2]
snapshot_download(repo_id=repo, local_dir=dst)
print(f"[vpr-engine] 模型下载完成 -> {dst}")
PY
fi

echo "[vpr-engine] 安装完成"
