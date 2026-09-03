#!/bin/sh
set -e

# 定位随 deepseek-harness-runtime-bin wheel 安装的 dsh 运行时（按架构命名不同），
# 让 deepseek_harness_adapter.resolve_harness_binary 开箱即用。
if [ -z "$DEEPSEEK_HARNESS_BIN" ]; then
  BIN=$(find /usr/local/lib/python3.12/site-packages/deepseek_harness_runtime/runtime \
        -maxdepth 1 -type f -name 'deepseek-harness-sdk-runtime-*' ! -name '*-rg*' 2>/dev/null | head -1)
  if [ -n "$BIN" ]; then
    chmod +x "$BIN" 2>/dev/null || true
    export DEEPSEEK_HARNESS_BIN="$BIN"
  fi
fi

exec python server.py serve --host 0.0.0.0 --port 4173
