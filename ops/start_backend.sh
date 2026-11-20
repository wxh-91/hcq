#!/bin/sh
# R11 后端启动器（何春秋 dz.py：nonebot 飞书通道 + 8082 FastAPI）
# 用法：start_backend.sh [1|0]
#   1 = 飞书在线（默认，跟随 .env）
#   0 = 飞书退场（不注册 adapter，无长连接/心跳；8082 与调度器、巡检照常工作）
FLAG="${1:-}"
if [ -n "$FLAG" ]; then
  export FEISHU_ENABLED="$FLAG"
fi
cd /home/wxh/robot || exit 1
exec /root/robot/venv310/bin/python dz.py >> /root/robot/dz.log 2>&1
