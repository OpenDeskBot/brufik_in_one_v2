#!/usr/bin/env bash
# insightface-engine 独立化安装（幂等，可在管理后台重复执行）：
#   1) 独立 venv（优先主服务 venv 同款 Python；已有 venv 版本不匹配自动重建）
#   2) pip 安装依赖（版本与主服务 pyproject.toml 对齐；mediapipe 自带 opencv-contrib-python，
#      不显式装 opencv-python 防 cv2 包冲突）
#   3) 校验 deskbot_server 运行子集副本（随仓库提交，不在此生成）
#   4) copy 模型（face_landmarker.task 3.6M + buffalo_s/w600k_mbf.onnx ~13M）
#      双路径：主服务 models/ → ~/.insightface 缓存 → 网络下载（镜像回退链）
#   5) warmup：import 校验依赖与副本可加载（模型加载由服务启动完成，/health 覆盖）
# 说明：install 命令由管理后台以 service/ 为 cwd 执行（manager cwd=SERVICE_ROOT），
#       脚本内先 cd 到自身目录；运行期零依赖主服务（venv/模型/源码均自备）。
set -euo pipefail
cd "$(dirname "$0")"   # → externals/insightface-engine

info() { printf '[insightface-engine] %s\n' "$*"; }
fail() { printf '[insightface-engine] 错误: %s\n' "$*" >&2; exit 1; }

# ---------- 1. venv（funasr/vpr-engine 模式：主服务 venv 同款 Python + 版本自愈） ----------
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
# 版本规格一律加引号：裸写 insightface>=0.7.3 会被 bash 把 > 解析成重定向（生成垃圾文件）
.venv/bin/pip install \
  numpy==1.26.4 \
  mediapipe==0.10.33 \
  "insightface>=0.7.3" \
  "onnxruntime>=1.17.0" \
  fastapi==0.139.2 \
  "uvicorn==0.51.0" \
  PyYAML==6.0.2

# ---------- 3. 校验代码副本（随仓库提交，不在安装期生成；防静默漂移） ----------
[ -f deskbot_server/vision/face_identity.py ] && \
[ -f deskbot_server/vision/face_embedding.py ] && \
[ -f deskbot_server/service/application/face_detector.py ] && \
[ -f deskbot_server/utils/paths.py ] || \
  fail "deskbot_server 运行子集副本缺失（deskbot_server/ 目录不完整）。请 git 恢复后重试"

# ---------- 4. 模型（双路径，幂等；中断后重跑先清残片自愈） ----------
mkdir -p models/mediapipe models/buffalo_s

# 4a. MediaPipe face_landmarker.task（3.6M）
TASK="models/mediapipe/face_landmarker.task"
SRC_TASK="$PWD/../../models/mediapipe/face_landmarker.task"
if [ -f "$TASK" ]; then
  info "face_landmarker.task 已存在，跳过"
elif [ -f "$SRC_TASK" ]; then
  info "copy face_landmarker.task（主服务 models/）..."
  cp -f "$SRC_TASK" "$TASK"
else
  info "本机无模型源，从 MediaPipe 官方存储下载 face_landmarker.task..."
  if curl -fsSL --connect-timeout 15 -o "$TASK" \
      "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"; then
    info "face_landmarker.task 下载完成"
  else
    rm -f "$TASK"
    fail "face_landmarker.task 下载失败。可手动放置 models/mediapipe/face_landmarker.task 后重跑"
  fi
fi
[ -f "$TASK" ] || fail "face_landmarker.task 未就绪"

# 4b. InsightFace buffalo_s/w600k_mbf.onnx（~13M；识别 embedding 模型）
ONNX="models/buffalo_s/w600k_mbf.onnx"
SRC_BUFF="$PWD/../../models/buffalo_s/w600k_mbf.onnx"
HF_BUFF="$HOME/.insightface/models/buffalo_s/w600k_mbf.onnx"
if [ -f "$ONNX" ]; then
  info "buffalo_s 模型已存在，跳过"
elif [ -f "$SRC_BUFF" ]; then
  info "copy buffalo_s（主服务 models/）..."
  cp -f "$SRC_BUFF" "$ONNX"
elif [ -f "$HF_BUFF" ]; then
  info "copy buffalo_s（~/.insightface 缓存）..."
  cp -f "$HF_BUFF" "$ONNX"
else
  info "本机无模型源，下载 buffalo_s.zip（GitHub release，镜像回退链）..."
  ZIP="models/buffalo_s.zip"
  dl_ok=""
  for url in \
    "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_s.zip" \
    "https://gh-proxy.com/https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_s.zip" \
    "https://mirror.ghproxy.com/https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_s.zip"; do
    info "尝试 $url ..."
    if curl -fsSL --connect-timeout 15 -o "$ZIP" "$url"; then dl_ok=1; break; fi
  done
  if [ -n "$dl_ok" ] && unzip -o -q "$ZIP" -d models/buffalo_s/; then
    # release zip 根是平铺文件；若带一层目录则拍平
    if [ -f models/buffalo_s/buffalo_s/w600k_mbf.onnx ]; then
      mv models/buffalo_s/buffalo_s/* models/buffalo_s/ 2>/dev/null || true
      rmdir models/buffalo_s/buffalo_s 2>/dev/null || true
    fi
    rm -f "$ZIP"
    info "buffalo_s 下载完成"
  else
    rm -f "$ZIP"
    fail "buffalo_s 下载失败（网络/镜像问题？）。可手动放置 models/buffalo_s/w600k_mbf.onnx 后重跑"
  fi
fi
[ -f "$ONNX" ] || fail "buffalo_s/w600k_mbf.onnx 未就绪"

# ---------- 5. warmup（import 校验依赖与副本可加载；失败仅告警，服务启动时再试） ----------
info "warmup：校验依赖与代码副本可加载..."
if .venv/bin/python -c '
import sys
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))   # cwd = externals/insightface-engine，优先本地副本
import deskbot_server.service.application.face_detector
import deskbot_server.vision.face_embedding
import deskbot_server.vision.face_identity
from deskbot_server.utils.paths import MODELS_DIR, PROJECT_ROOT
print("warmup ok:", PROJECT_ROOT.name, "models=", MODELS_DIR.name)
' 2>&1; then
  info "warmup 完成"
else
  info "warmup 失败（依赖问题？）。服务启动时会再次尝试加载。"
fi

info "安装完成。启动：.venv/bin/python server.py --host 127.0.0.1 --port 9103"
