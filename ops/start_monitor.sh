#!/bin/sh
# 监控启动器（被 Windows 侧 start_all.ps1 / hcq_dev.sh 调用）
# 常驻拉起：KULA 8085（系统指标） + 健康聚合 8086（业务指标）
# 单独前台运行：sh start_monitor.sh
BIN_DIR=/root/robot/monitor
PY=/root/robot/venv310/bin/python

cd "$BIN_DIR" || exit 1
export KULA_PORT=8085 HEALTH_PORT=8086

# 只拉未占用的端口（幂等，重复调用不会双开）
if ! ss -ltn 2>/dev/null | grep -q ":$KULA_PORT "; then
  "$BIN_DIR/bin/kula" serve -config ./config.yaml >> kula.log 2>&1 &
fi
if ! ss -ltn 2>/dev/null | grep -q ":$HEALTH_PORT "; then
  "$PY" server.py >> health.log 2>&1 &
fi

wait
