import asyncio
import os
import tempfile
import logging
import re

logger = logging.getLogger(__name__)

def _fix_missing_newlines(script: str) -> str:
    """修复 AI 生成的代码中缺少换行的问题"""
    if re.search(r'\b(import|from|def|class)\b', script):
        patterns = [
            (r'(import\s+[\w_]+)\s+(from\s+[\w_]+)', r'\1\n\2'),
            (r'(from\s+[\w_]+\s+import\s+[\w_]+)\s+(import\s+[\w_]+)', r'\1\n\2'),
            (r'(def\s+[\w_]+\s*\([^)]*\):)\s*(import|from)', r'\1\n\2'),
            (r'(class\s+[\w_]+:)\s*(import|from)', r'\1\n\2'),
        ]
        for pat, repl in patterns:
            script = re.sub(pat, repl, script)
        if '\n' not in script and ';' in script:
            script = script.replace(';', '\n')
    return script


async def run_bash_script(script_content: str, timeout: int = 30) -> str:
    """
    执行 AI 动态生成的多行 Bash 脚本。

    参数:
        script_content: 多行 Bash 脚本内容（纯文本）
        timeout: 超时秒数，默认 30 秒

    返回:
        执行结果或错误信息
    """
    if not script_content or not script_content.strip():
        return "错误：脚本内容为空"

    # 自动修复缺失换行
    script_content = _fix_missing_newlines(script_content)

    # 创建临时脚本文件
    fd, script_path = tempfile.mkstemp(suffix=".sh")
    os.close(fd)

    try:
        with open(script_path, 'w', encoding='utf-8') as f:
            f.write("#!/bin/bash\n")
            f.write(script_content)
        os.chmod(script_path, 0o755)

        proc = await asyncio.create_subprocess_shell(
            script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        output = stdout.decode().strip() or stderr.decode().strip()
        if not output:
            output = "脚本执行完成（无输出）"

        # 阈值调高至 2000
        if len(output) > 2000:
            output = output[:2000] + "\n... (输出过长，已截断)"

        return f"成功：脚本执行成功 (返回码: {proc.returncode})\n{output}"

    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except:
            pass
        return f"错误：脚本执行超时（超过 {timeout} 秒）"
    except Exception as e:
        return f"错误：脚本执行失败: {str(e)}"
    finally:
        if os.path.exists(script_path):
            os.unlink(script_path)
