#!/usr/bin/env bash
# Offline import: do not share model processes or modify the reference project.
set -euo pipefail
module_dir="$(cd "$(dirname "$0")" && pwd)"
service_dir="$(cd "$module_dir/../.." && pwd)"
binary_dir="${LLM_REALTIME_BINARY_SOURCE:-$service_dir/externals/llm-qwen/bin}"
model_source="${LLM_REALTIME_MODEL_SOURCE:-}"
model_target="$module_dir/models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
mkdir -p "$module_dir/models"
if [[ ! -x "$module_dir/bin/llama-server" ]]; then
  [[ -x "$binary_dir/llama-server" ]] || { echo "请设置 LLM_REALTIME_BINARY_SOURCE 为已安装 llama.cpp 的 bin 目录" >&2; exit 1; }
  cp -R "$binary_dir" "$module_dir/bin"
fi
if [[ ! -f "$model_target" ]]; then
  [[ -n "$model_source" && -f "$model_source" ]] || { echo "请设置 LLM_REALTIME_MODEL_SOURCE 为现有 Qwen3-4B-Instruct-2507-Q4_K_M.gguf" >&2; exit 1; }
  cp "$model_source" "$model_target"
fi
printf 'Local realtime model and runtime are ready.\n'
