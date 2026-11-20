# -*- coding: utf-8 -*-
"""WCU 运行时通道：WSL → Windows 的脚本调用封装。

约定（与上一代实现保持一致，Windows 侧脚本无需改动）：
    - 用 WSL 里的 Windows Python 可执行文件（/mnt/c/... 形式）拉起脚本；
    - 入参走 stdin 的单行 JSON，出参取 stdout 最后一行 JSON；
    - 超时/无输出/解析失败一律收敛成 dict，不抛到工具层。
"""
import json
import subprocess

# Windows Python 3.14（宿主真实路径的 WSL 映射）
WIN_PY = "/mnt/c/Users/Admin（无密码）/AppData/Local/Programs/Python/Python314/python.exe"
# 2026-11-07：Public 根目录下的脚本被系统清理（win_shell.py / win_ui_automation.py 均丢失），
# 改为从 wcu_src 子目录读取；该目录在 WSL 与 PowerShell 两侧读写均稳定。
WIN_SHELL_SCRIPT = r"C:\Users\Public\wcu_src\win_shell.py"
WIN_UI_SCRIPT = r"C:\Users\Public\wcu_src\win_ui_automation.py"
WIN_BROWSER_SCRIPT = r"C:\Users\Public\wcu_src\interactive_browser.py"


def win_call(script: str, payload: dict, timeout: int = 360) -> dict:
    """调用 Windows 侧脚本：stdin 传 JSON，取 stdout 最后一行解析。"""
    try:
        proc = subprocess.run(
            [WIN_PY, script],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        return {"status": "error", "error": "SUBPROCESS: %s" % str(exc)[:250]}
    out = (proc.stdout or "").strip()
    if not out:
        return {"status": "error",
                "error": "NO_OUTPUT rc=%s %s" % (proc.returncode, (proc.stderr or "")[:250])}
    try:
        return json.loads(out.splitlines()[-1])
    except Exception:
        return {"status": "raw", "output": out[-800:]}
