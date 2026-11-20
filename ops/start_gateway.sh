#!/bin/sh
# R11 网关启动器（被 Windows 侧 start_all.ps1 调用）
# 用 wrap 脚本而不是把 bash -lc 长命令塞进 Start-Process，避免引号/路径被吃掉。
# 用法：start_gateway.sh [port]   默认 8083
PORT="${1:-8083}"
cd /mnt/d/dsh-custom/wsl-gateway || exit 1
exec /root/robot/venv310/bin/python -m uvicorn gateway:app \
  --host 0.0.0.0 --port "$PORT" --no-proxy-headers \
  >> /mnt/d/dsh-custom/wsl-gateway/uvicorn.log 2>&1
