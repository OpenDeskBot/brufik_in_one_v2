#!/usr/bin/env bash
# funasr 独立化安装（幂等，可在管理后台重复执行）：
#   1) 独立 venv（优先主服务 venv 同款 Python；已有 venv 版本不匹配自动重建）
#   2) pip 安装依赖（版本与主服务 pyproject.toml 对齐）
#   3) 校验 deskbot_server 运行子集副本（随仓库提交，不在此生成）
#   4) copy SenseVoiceSmall 模型（约 2G）到服务目录（关键文件存在则跳过）
#   5) warmup：构造 FunAsrAdapter 验证模型可加载（失败仅告警，不阻塞）
# 说明：install 命令由管理后台以 service/ 为 cwd 执行（manager cwd=SERVICE_ROOT），
#       脚本内先 cd 到自身目录；运行期零依赖主服务（venv/模型/源码均自备）。
set -euo pipefail
cd "$(dirname "$0")"   # → externals/funasr

info() { printf '[funasr] %s\n' "$*"; }
fail() { printf '[funasr] 错误: %s\n' "$*" >&2; exit 1; }

# ---------- 1. venv（vpr-engine 模式：主服务 venv 同款 Python + 版本自愈） ----------
MAIN_VENV_PY="$PWD/../../.venv/bin/python"
PYTHON_BIN=""
if [ -x "$MAIN_VENV_PY" ]; then
  PYTHON_BIN="$MAIN_VENV_PY"
else
  for cand in python3.12 python3.11 python3.10; do
    if command -v "$cand" >/dev/null 2>&1; then PYTHON_BIN="$(command -v "$cand")"; break; fi
  done
fi
[ -n "$PYTHON_BIN" ] || fail "找不到 Python 3.10~3.12，请安装后重试"
PY_VER="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [ -x .venv/bin/python ]; then
  CUR_VER="$(.venv/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  if [ "$CUR_VER" != "$PY_VER" ]; then
    info "已有 venv 版本 ${CUR_VER} != ${PY_VER}，重建"
    rm -rf .venv
  fi
fi
if [ ! -d .venv ]; then
  info "用 $PYTHON_BIN ($PY_VER) 创建独立 venv"
  "$PYTHON_BIN" -m venv .venv
fi

# ---------- 2. 依赖（显式 pin 与主服务一致；pip 源走用户环境镜像） ----------
.venv/bin/python -m pip install --upgrade pip
# funasr 依赖链含 pytorch-wpe → torch-complex → torch，必须显式 pin torch 2.2.2 防拉成新版；
# numpy==1.26.4 防 funasr 1.2.7 在 numpy 2.x 下的已知兼容问题
.venv/bin/pip install \
  funasr==1.2.7 \
  funasr-onnx==0.4.1 \
  onnxruntime==1.27.0 \
  torch==2.2.2 torchaudio==2.2.2 \
  numpy==1.26.4 \
  librosa==0.11.0 \
  fastapi==0.139.2 \
  "uvicorn==0.51.0" \
  PyYAML==6.0.2

# ---------- 3. 校验代码副本（随仓库提交，不在安装期生成；防静默漂移） ----------
[ -f deskbot_server/infrastructure/asr/funasr.py ] && \
[ -f deskbot_server/infrastructure/asr/protocol.py ] && \
[ -f deskbot_server/model/settings.py ] || \
  fail "deskbot_server 运行子集副本缺失（deskbot_server/ 目录不完整）。请 git 恢复后重试"

# ---------- 4. copy 模型（2G，幂等；中断后重跑先清残片自愈） ----------
SRC_MODEL="$PWD/../../models/SenseVoiceSmall"
DST_MODEL="models/SenseVoiceSmall"
if [ -f "$SRC_MODEL/model_quant.onnx" ] && [ -f "$SRC_MODEL/model.pt" ]; then
  if [ -f "$DST_MODEL/model_quant.onnx" ] && [ -f "$DST_MODEL/model.pt" ] && [ -f "$DST_MODEL/model.onnx" ]; then
    info "模型已存在（${DST_MODEL}），跳过 copy"
  else
    info "copy 模型（约 2G，需 1~2 分钟）..."
    rm -rf "$DST_MODEL"
    mkdir -p models
    cp -RL "$SRC_MODEL" models/   # -L: 跟随符号链接，杜绝指向主服务的隐藏依赖
    [ -f "$DST_MODEL/model_quant.onnx" ] || fail "模型 copy 不完整"
    info "模型 copy 完成"
  fi
else
  fail "主服务模型缺失: ${SRC_MODEL}。请先在 service/ 下执行 python scripts/download_model.py 再安装本服务"
fi

# ---------- 5. warmup（真实加载一次，失败仅告警；llm-engine 风格） ----------
info "warmup：加载模型验证可运行..."
if HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}" HF_HUB_DISABLE_XET=1 \
    .venv/bin/python -c '
import sys
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))   # cwd = externals/funasr，优先本地副本
import yaml
from deskbot_server.model.settings import AppSettings
from deskbot_server.infrastructure.asr.funasr import FunAsrAdapter
cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8")) or {}
a = FunAsrAdapter(AppSettings.from_config({"asr": cfg}))
print("warmup ok:", a._model_dir, "onnx=", a._onnx_model is not None, "pt=", a._pt_model is not None)
' 2>&1; then
  info "warmup 完成"
else
  info "warmup 失败（依赖/模型问题？）。服务启动时会再次尝试加载。"
fi

info "安装完成。启动：.venv/bin/python server.py --host 127.0.0.1 --port 9102"
