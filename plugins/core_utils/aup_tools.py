# -*- coding: utf-8 -*-
"""AUP 标杆工具：execute_shell 的 AUP 协议完全重写版。

与旧版 plugins.core_utils.xt.execute_shell 的能力对齐（异步执行、超时控制），
但按 AUP 统一协议重构：
  - @aup_tool 自动入参校验（cmd 必填、string 类型）
  - 多级降级链：primary(subprocess_shell) -> fallback1(subprocess_exec 无 shell)
    -> fallback2(同步 subprocess.run 兜底)，全灭则抛 AUPError 且链路完整记录
  - 输出校验：stdout/stderr 乱码与超长防护（输出截断 + 空输出判定）
"""
from __future__ import annotations

import asyncio
import re
import shlex
import subprocess
import os
import signal

HARD_TIMEOUT_SEC = 30.0   # 死超时：无论调用方传多少，单条命令最长 30 秒，超时即杀进程组
import sys

from aup_core import AUPError, AUPResult, aup_tool

_CMD_SCHEMA = {
    "required": ["cmd"],
    "properties": {
        "cmd": {"type": "string", "description": "要执行的 Shell 命令"},
        "timeout_sec": {"type": "number", "description": "超时秒数，默认 15"},
    },
}

_MAX_OUTPUT_LEN = 8000
_EMPTY_RE = re.compile(r"^\s*$")


def _sanitize_output(text: str, label: str) -> str:
    """输出校验：乱码/超长防护。"""
    if text is None:
        return ""
    # 空输出视为无有效信息
    if _EMPTY_RE.match(text):
        return ""
    if len(text) > _MAX_OUTPUT_LEN:
        return text[:_MAX_OUTPUT_LEN] + "\n...[AUP 截断: 输出超过 %d 字符]" % _MAX_OUTPUT_LEN
    return text


def _kill_group(proc) -> None:
    """死超时后杀掉整个进程组，杜绝孤儿 curl/find/ffmpeg 继续跑。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


async def _run_via_subprocess_shell(cmd: str, timeout_sec: float) -> tuple:
    """降级通道 1：原生异步 create_subprocess_shell（最接近旧版行为）。"""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_sec
        )
    except asyncio.TimeoutError:
        _kill_group(proc)
        raise TimeoutError("channel-1(subprocess_shell) 超时 %.1fs" % timeout_sec)
    return proc.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


async def _run_via_subprocess_exec(cmd: str, timeout_sec: float) -> tuple:
    """降级通道 2：无 shell 的 exec 模式（规避 shell 解析/注入面）。"""
    parts = shlex.split(cmd)
    if not parts:
        raise ValueError("channel-2(subprocess_exec): 空命令")
    proc = await asyncio.create_subprocess_exec(
        *parts,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_sec
        )
    except asyncio.TimeoutError:
        _kill_group(proc)
        raise TimeoutError("channel-2(subprocess_exec) 超时 %.1fs" % timeout_sec)
    return proc.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


def _run_via_subprocess_run(cmd: str, timeout_sec: float) -> tuple:
    """降级通道 3：同步 subprocess.run 兜底（最差但最快）。"""
    res = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=timeout_sec
    )
    return res.returncode, res.stdout, res.stderr


def _fix_missing_newlines(cmd: str) -> str:
    """修复 AI 生成的 Python 代码中缺少换行的问题（与旧版能力对齐）。"""
    if re.search(r"\b(import|from|def|class)\b", cmd):
        patterns = [
            (r"(import\s+[\w_]+)\s+(from\s+[\w_]+)", r"\1\n\2"),
            (r"(from\s+[\w_]+\s+import\s+[\w_]+)\s+(import\s+[\w_]+)", r"\1\n\2"),
            (r"(def\s+[\w_]+\s*\([^)]*\):)\s*(import|from)", r"\1\n\2"),
            (r"(class\s+[\w_]+:)\s*(import|from)", r"\1\n\2"),
        ]
        for pat, repl in patterns:
            cmd = re.sub(pat, repl, cmd)
        if "\n" not in cmd and ";" in cmd:
            cmd = cmd.replace(";", "\n")
    return cmd


@aup_tool(
    name="aup_execute_shell",
    description="执行任意 Shell 命令（高危，仅限主管理员；带多级降级链与输出校验）",
    category="system",
    is_danger=True,
    params_schema=_CMD_SCHEMA,
    suggestion_hint="命令执行失败，请检查命令语法、权限或目标路径",
)
async def aup_execute_shell(cmd: str, timeout_sec: float = 15.0) -> AUPResult:
    """AUP 版 execute_shell：三段降级漏斗 + AUPResult 输出。"""
    chain: list = ["execute_shell:enter"]
    cmd = _fix_missing_newlines(cmd)
    try:
        timeout_sec = float(timeout_sec)
    except Exception:
        timeout_sec = 15.0
    timeout_sec = max(1.0, min(timeout_sec, HARD_TIMEOUT_SEC))
    chain.append("execute_shell:timeout_cap=%.0fs" % timeout_sec)

    channels = [
        ("subprocess_shell", _run_via_subprocess_shell),
        ("subprocess_exec", _run_via_subprocess_exec),
        ("subprocess_run", _run_via_subprocess_run),
    ]
    last_error: Exception | None = None
    for name, runner in channels:
        try:
            rc, out, err = await runner(cmd, timeout_sec) if name != "subprocess_run" \
                else await asyncio.to_thread(runner, cmd, timeout_sec)
            out = _sanitize_output(out, "stdout")
            err = _sanitize_output(err, "stderr")
            if rc == 0:
                chain.append(f"execute_shell:{name}:ok(rc=0)")
                return AUPResult(
                    status="ok",
                    data={"rc": rc, "stdout": out, "stderr": err},
                    error_code="0",
                    fallback_chain=chain,
                )
            # 命令本身失败（rc!=0）—— 继续降级
            chain.append(f"execute_shell:{name}:failed(rc={rc})")
            last_error = RuntimeError("%s 返回非零 rc=%s" % (name, rc))
        except TimeoutError as te:  # 死超时：已杀进程组，绝不降级重试（避免放大阻塞）
            chain.append(f"execute_shell:{name}:timeout")
            return AUPResult(
                status="error",
                data={"timeout_sec": timeout_sec},
                error_code="TIMEOUT",
                fallback_chain=chain,
                suggestion="命令超过死超时 %.0fs，已强制终止；请缩小命令范围或改用分步执行" % timeout_sec,
            )
        except Exception as e:  # 通道异常（不存在等）—— 继续降级
            chain.append(f"execute_shell:{name}:exception:{type(e).__name__}")
            last_error = e
    # 全灭：抛 AUPError 携带完整链路
    raise AUPError(
        "TOOL_EXECUTION_FAILED",
        "execute_shell 三条通道全部失败: %s" % last_error,
        detail={"cmd_preview": cmd[:120]},
        fallback_chain=chain,
        suggestion="命令执行失败，请检查命令语法、权限或目标路径",
    )


# 同步便捷入口（测试台可直接调用）
async def _demo_success() -> AUPResult:
    return await aup_execute_shell("echo AUP-OK")


# ---------------------------------------------------------------------------
# AUP 版 run_bash_script（高频系统工具 #2）
# ---------------------------------------------------------------------------
_BASH_SCHEMA = {
    "required": ["script_content"],
    "properties": {
        "script_content": {"type": "string", "description": "多行 Bash 脚本内容（纯文本）"},
        "timeout": {"type": "integer", "description": "超时秒数，默认 30"},
    },
}


@aup_tool(
    name="run_bash_script",
    description="执行 AI 动态生成的多行 Bash 脚本（高危，仅限主管理员；带降级与输出校验）",
    category="system",
    is_danger=True,
    params_schema=_BASH_SCHEMA,
    suggestion_hint="脚本执行失败，请检查脚本语法或超时参数",
)
async def aup_run_bash_script(script_content: str, timeout: int = 30) -> AUPResult:
    """AUP 版 run_bash_script：包装 script_runner 原实现为 AUP 协议。"""
    chain: list = ["run_bash_script:enter"]
    try:
        from plugins.system_ops.script_runner import run_bash_script as _raw
        raw = await _raw(script_content, timeout)
        chain.append("run_bash_script:primary:ok")
    except Exception as e:
        chain.append("run_bash_script:primary:exception:%s" % type(e).__name__)
        raise AUPError(
            "TOOL_EXECUTION_FAILED",
            "run_bash_script 执行失败: %s" % e,
            fallback_chain=chain,
            suggestion="脚本执行失败，请检查脚本语法或超时参数",
        ) from e
    # 输出校验：空输出/纯错误信息判定
    cleaned = _sanitize_output(raw or "", "script")
    if not cleaned:
        cleaned = "（脚本执行完成，无有效输出）"
    return AUPResult(
        status="ok",
        data={"output": cleaned},
        error_code="0",
        fallback_chain=chain,
    )
