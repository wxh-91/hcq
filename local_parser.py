import asyncio
import subprocess
import os
import tempfile

async def parse_log_file(filepath: str, pattern: str = "") -> str:
    """
    本地解析日志文件（使用 awk/grep），只返回统计摘要。
    - 统计行数、错误数、匹配 pattern 的行数
    - 如果文件 > 10MB，只分析前 10000 行
    """
    if not os.path.exists(filepath):
        return f"文件不存在: {filepath}"

    # 检测文件大小，超大文件只取前 10000 行
    stat = os.stat(filepath)
    if stat.st_size > 10 * 1024 * 1024:  # 10MB
        head_cmd = f"head -n 10000 {filepath}"
        proc = await asyncio.create_subprocess_shell(
            head_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        content = stdout.decode('utf-8', errors='ignore')
        truncated = True
    else:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        truncated = False

    lines = content.splitlines()
    total_lines = len(lines)

    # 统计错误数量（常见关键字）
    error_keywords = ['error', 'fail', 'exception', 'critical', 'panic', '致命']
    error_count = 0
    matched_lines = 0

    for line in lines:
        lower_line = line.lower()
        if any(kw in lower_line for kw in error_keywords):
            error_count += 1
        if pattern and pattern.lower() in lower_line:
            matched_lines += 1

    result = f"""📊 日志分析摘要:
- 总行数: {total_lines}（{"仅前10000行" if truncated else "完整文件"}）
- 错误/异常行数: {error_count}
- 匹配 "{pattern}" 的行数: {matched_lines if pattern else '未指定'}"""
    return result


async def fast_grep(filepath: str, pattern: str) -> str:
    """快速 grep 封装，返回匹配行（最多 50 行）"""
    if not os.path.exists(filepath):
        return f"文件不存在: {filepath}"
    cmd = f"grep -n '{pattern}' {filepath} | head -n 50"
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return f"未匹配到 '{pattern}' 或 grep 失败"
    return stdout.decode('utf-8', errors='ignore')


async def extract_key_value(filepath: str, key: str) -> str:
    """提取配置文件中的 key=value 对（支持 yaml/ini 风格）"""
    if not os.path.exists(filepath):
        return f"文件不存在: {filepath}"
    cmd = f"grep -E '^{key}\\s*[=:]' {filepath} | head -n 10"
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await proc.communicate()
    result = stdout.decode('utf-8', errors='ignore')
    if not result.strip():
        return f"未找到键: {key}"
    return result
