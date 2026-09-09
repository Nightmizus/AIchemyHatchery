#!/bin/sh
set -eu

# 定位随 deepseek-harness-runtime-bin wheel 安装的 dsh 运行时（按架构命名不同），
# 让 deepseek_harness_adapter.resolve_harness_binary 开箱即用。
if [ -z "${DEEPSEEK_HARNESS_BIN:-}" ]; then
  BIN=$(find /usr/local/lib/python3.12/site-packages/deepseek_harness_runtime/runtime \
        -maxdepth 1 -type f -name 'deepseek-harness-sdk-runtime-*' ! -name '*-rg*' 2>/dev/null | head -1)
  if [ -n "$BIN" ]; then
    # 执行位已在构建期赋好（非 root 用户改不了别人的文件），这里只做兜底。
    chmod +x "$BIN" 2>/dev/null || true
    export DEEPSEEK_HARNESS_BIN="$BIN"
  fi
fi

# 漏洞（监听地址被硬编码）：原先无条件 --host 0.0.0.0，运营方无法把容器限制到回环或指定网卡。
exec python server.py serve \
  --host "${ALCHEMY_HATCHERY_HOST:-0.0.0.0}" \
  --port "${ALCHEMY_HATCHERY_PORT:-4173}"
