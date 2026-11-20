#!/bin/bash
# 换号脚本（Napcat 已迁移到 Windows 宿主机 C:\Napcat）
# ⚠ 重要说明（2026-10-24 起）：
#   - Napcat 网关运行在 Windows 宿主机（C:\Napcat，NapCatWinBootMain.exe 扫码登录），
#     不再运行在 WSL 内（旧路径 /root/Napcat 已废弃，勿再引用）。
#   - 本脚本只在 WSL 侧执行：清理 dz.py / 爬虫工作区 / 跨系统停 Napcat。
#   - 停止 Windows 侧 Napcat/QQ：powershell.exe -Command "Stop-Process -Name NapCatWinBootMain -Force"
#   - 换号后请在 Windows 上手动重启 NapCatWinBootMain.exe 并重新扫码。
# 用法: $0 <旧QQ号>  （旧QQ号参数保留仅为兼容，Windows 侧配置为单文件 onebot11.json）

OLD_QQ=$1
if [ -z "$OLD_QQ" ]; then
    echo "用法: $0 <旧QQ号>"
    echo "示例: $0 3819432508"
    exit 1
fi

WORKSPACE_DIR="/root/robot/bot_utils/crawler_tools/workspace"

echo "=== 1. 停止 WSL 侧相关进程（含 venv310 实际启动模式） ==="
pkill -9 -f "venv310/bin/python dz.py"
pkill -9 -f "python3 dz.py"
pkill -9 -f "node /root/robot/bot_utils/crawler_tools"
pkill -9 -f "playwright"
echo "WSL 侧进程清理完毕，等待2秒..."
sleep 2

echo "=== 2. 停止 Windows 侧 Napcat/QQ（跨系统） ==="
powershell.exe -Command "Stop-Process -Name NapCatWinBootMain -Force -ErrorAction SilentlyContinue" 2>/dev/null && echo "已向 Windows 发送停止 Napcat 指令" || echo "（无法跨系统停止，请在 Windows 任务管理器手动结束 NapCatWinBootMain）"

echo "=== 3. Windows 侧配置提示 ==="
echo "Windows 侧配置文件：C:\Napcat\config\onebot11.json（WSL 内路径 /mnt/c/Napcat/config/onebot11.json）"
echo "如需重置账号配置，请人工在 Windows 上编辑；本脚本不再直接操作该文件。"

echo "=== 4. 清理 WSL 侧爬虫临时工作区 ==="
if [ -d "$WORKSPACE_DIR" ]; then
    rm -rf "$WORKSPACE_DIR"/*
    echo "crawler_tools 临时文件已清空。"
fi

echo "=== 5. 换号完成 ==="
echo "请在 Windows 宿主机手动启动 C:\Napcat\NapCatWinBootMain.exe 并扫码登录新账号。"
echo "（登录后 WSL 侧 dz.py 会自动经 ws://<宿主IP>:3000 重连）"
