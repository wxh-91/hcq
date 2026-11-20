#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
host_env.py — Windows 宿主机环境库查询接口（host_env.db / sys_env 表）
提供 get_env(key) / get_env_snapshot(keys) 供 ai.py 日常上下文注入。
"""
import os
import sqlite3

_HOST_ENV_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "host_env.db")

# 核心情报键：System Prompt 注入时默认携带
CORE_ENV_KEYS = ("browser_edge", "python_win", "wsl_ip", "proxy_status", "node_path", "tools_port")


def get_env(key: str, default: str = "") -> str:
    """按 key 查询宿主环境信息；未命中返回 default（只读，失败静默降级）"""
    try:
        conn = sqlite3.connect(_HOST_ENV_DB, timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT env_value FROM sys_env WHERE env_key=?", (key,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default


def get_env_snapshot(keys=None):
    """批量读取环境情报（默认核心键），供 System Prompt 注入。
    返回 [(env_key, env_value, description), ...]"""
    if keys is None:
        keys = CORE_ENV_KEYS
    out = []
    try:
        conn = sqlite3.connect(_HOST_ENV_DB, timeout=5)
        cur = conn.cursor()
        for k in keys:
            try:
                cur.execute("SELECT env_value, description FROM sys_env WHERE env_key=?", (k,))
                row = cur.fetchone()
                if row:
                    out.append((k, row[0], row[1] or ""))
            except Exception:
                continue
        conn.close()
    except Exception:
        pass
    return out


def format_env_snapshot(keys=None) -> str:
    """格式化为【本地宿主环境情报】文本块，供 System Prompt 直接拼装"""
    snap = get_env_snapshot(keys)
    if not snap:
        return ""
    lines = ["【本地宿主环境情报】", "你运行在 WSL（/root/robot）内，但有一个 Windows 宿主可供 execute_shell 直接调用："]
    for k, v, desc in snap:
        if not v:
            continue
        lines.append(f"- {k}: {v}" + (f"（{desc}）" if desc else ""))
    lines.append("- 你可以通过 execute_shell 调用这些宿主程序（例如启动浏览器、运行 Python/Node 脚本）。")
    return "\n".join(lines)


if __name__ == "__main__":
    # 自检
    print("get_env(browser_edge):", get_env("browser_edge"))
    print("get_env(python_win):", get_env("python_win"))
    print("get_env(wsl_ip):", get_env("wsl_ip"))
    print("get_env(proxy_status):", get_env("proxy_status"))
    print("get_env(node_path):", get_env("node_path"))
    print("--- snapshot ---")
    print(format_env_snapshot())
