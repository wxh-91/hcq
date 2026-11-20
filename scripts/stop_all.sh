#!/bin/bash
# 资源清理脚本（Napcat 已迁移到 Windows 宿主机 C:\Napcat）
# ⚠ 说明：Napcat/QQ 网关在 Windows 侧运行（NapCatWinBootMain.exe）；
#   本脚本负责 WSL 侧清理；Windows 侧进程需跨系统停止：
#   powershell.exe -Command "Stop-Process -Name NapCatWinBootMain -Force"

echo "=== 1. 停止 Python 主进程 ==="
pkill -15 -f "venv310/bin/python dz.py"
pkill -15 -f "python3 dz.py"
sleep 2
pkill -9 -f "venv310/bin/python dz.py"
pkill -9 -f "python3 dz.py"

echo "=== 2. 停止 Windows 侧 Napcat 及底层 QQ（跨系统） ==="
powershell.exe -Command "Stop-Process -Name NapCatWinBootMain -Force -ErrorAction SilentlyContinue" 2>/dev/null && echo "已向 Windows 发送停止 Napcat 指令" || echo "（无法跨系统停止，请在 Windows 任务管理器手动结束 NapCatWinBootMain）"

echo "=== 3. 清理爬虫与 PoW 残留的 Node 进程 ==="
pkill -9 -f "node /root/robot/bot_utils/crawler_tools"
pkill -9 -f "node -e"
echo "已清理 Node 残留。"

echo "=== 4. 清理 Playwright 浏览器残留 ==="
pkill -9 -f "playwright"
pkill -9 -f "chrome-linux"
echo "已清理 Playwright 残留。"

echo "=== 5. 清理临时工作区 ==="
rm -rf /root/robot/bot_utils/crawler_tools/workspace/*
echo "crawler_tools 临时文件已清空。"

echo "=== 6. 检查是否还有相关进程 ==="
ps aux | grep -E "dz.py|playwright|node -e" | grep -v "grep"
echo "清理完毕。"
