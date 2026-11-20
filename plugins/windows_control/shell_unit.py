# -*- coding: utf-8 -*-
"""win_shell 实现本体（从 ai.py 迁入 WCU）。

流程：零信任闸门 → 调 Windows 脚本 → 格式化人类可读结果。
"""
import logging

from aup_core import AUPError

from . import guards, winenv

logger = logging.getLogger("plugins.windows_control.shell")


async def run_shell(command: str = "", shell: str = "powershell", timeout: int = 60,
                    elevated: bool = False, cwd: str = "") -> str:
    """在 Windows 宿主机执行命令，返回 UTF-8 输出与权限状态。"""
    if not (command or "").strip():
        raise AUPError("MISSING_COMMAND", "win_shell 必须提供 command")

    # 规则 3：未授权下载/安装 → 拦截
    guards.assert_shell_safe(command)

    payload = {
        "command": command,
        "shell": (shell or "powershell").strip().lower(),
        "timeout": int(timeout or 60),
        "elevated": bool(elevated),
        "cwd": (cwd or "").strip(),
    }
    logger.info("[win_shell] exec shell=%s len=%s elevated=%s",
                payload["shell"], len(payload["command"]), payload["elevated"])

    result = winenv.win_call(winenv.WIN_SHELL_SCRIPT, payload)
    status = result.get("status")
    if status in ("ok", "error", "timeout"):
        lines = [
            "Windows 命令执行结果",
            "shell: %s | 退出码: %s | 管理员权限: %s | 耗时: %sms | 提权通道: %s" % (
                result.get("shell"), result.get("exit_code"),
                "是" if result.get("is_admin") else "否",
                result.get("duration_ms"), result.get("elevated_via")),
            "【stdout】",
            ((result.get("stdout") or "").strip() or "(空)"),
        ]
        stderr = (result.get("stderr") or "").strip()
        if stderr:
            lines.append("【stderr】")
            lines.append(stderr)
        if result.get("truncated"):
            lines.append("（输出过长已截断）")
        if result.get("note"):
            lines.append("备注: %s" % result.get("note"))
        return "\n".join(lines)
    return "Windows 命令执行失败: %s" % (result.get("error") or result.get("output") or "未知错误")
