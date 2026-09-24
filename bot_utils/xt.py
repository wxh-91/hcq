import asyncio
import platform, datetime

# 尝试导入 psutil，如果没有装就提示
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

async def system_status():
    """查看系统状态（CPU、内存、磁盘、运行时间）"""
    if not PSUTIL_AVAILABLE:
        return "需要安装 psutil 库，请执行：pip install psutil"
    try:
        cpu = await asyncio.to_thread(psutil.cpu_percent, interval=1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        boot_time = datetime.datetime.fromtimestamp(psutil.boot_time())
        uptime = str(datetime.datetime.now() - boot_time).split('.')[0]
        return (
            f"系统状态\n"
            f"CPU: {cpu}%\n"
            f"内存: {mem.used//1024//1024}MB / {mem.total//1024//1024}MB ({mem.percent}%)\n"
            f"磁盘: {disk.used//1024//1024//1024}GB / {disk.total//1024//1024//1024}GB ({disk.percent}%)\n"
            f"运行时长: {uptime}\n"
            f"系统: {platform.system()} {platform.release()}"
        )
    except Exception as e:
        return f"获取状态失败: {str(e)}"

async def execute_shell(cmd):
    """异步执行shell命令，带15秒超时控制（原生异步，不依赖subprocess）"""
    try:
        def _fix_missing_newlines(cmd: str) -> str:
            """修复 AI 生成的 Python 代码中缺少换行的问题"""
            import re
            # 检测是否包含 Python 特征（import/from/def/class）但缺少换行
            if re.search(r'\b(import|from|def|class)\b', cmd):
                # 如果存在 "import os from" 这种错误模式，尝试修正
                # 具体规则：在 import、from、def、class 前添加换行
                patterns = [
                    (r'(import\s+[\w_]+)\s+(from\s+[\w_]+)', r'\1\n\2'),  # "import os from dotenv" → "import os\nfrom dotenv"
                    (r'(from\s+[\w_]+\s+import\s+[\w_]+)\s+(import\s+[\w_]+)', r'\1\n\2'),  # "from a import b import c" → 分开
                    (r'(def\s+[\w_]+\s*\([^)]*\):)\s*(import|from)', r'\1\n\2'),  # def 后紧跟 import
                    (r'(class\s+[\w_]+:)\s*(import|from)', r'\1\n\2'),  # class 后紧跟 import
                ]
                for pat, repl in patterns:
                    cmd = re.sub(pat, repl, cmd)
                # 如果仍然没有换行，但有多条语句，用分号分隔的位置尝试加换行
                if '\n' not in cmd and ';' in cmd:
                    cmd = cmd.replace(';', '\n')
            return cmd
        cmd = _fix_missing_newlines(cmd)
        # 创建子进程，捕获输出
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            # 设置15秒超时，等待命令执行完毕
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            output = stdout.decode().strip() or stderr.decode().strip()
            if not output:
                output = "命令执行完成（无输出）"
            # 限制返回长度，避免刷屏
            if len(output) > 2000:
                output = output[:2000] + "\n... (内容过长已截断)"
            return f"执行成功\n{output}"
        except asyncio.TimeoutError:
            # 超时后先温和终止，再强制杀死
            proc.terminate()   # SIGTERM
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                proc.kill()    # SIGKILL
                await proc.wait()
            return "命令执行超时（超过15秒）"
    except Exception as e:
        return f"执行失败: {str(e)}"
async def ping_host(host: str) -> str:
    """检测主机是否可达（使用 ping -c 1）"""
    if not host:
        return "请提供主机名或IP"
    proc = await asyncio.create_subprocess_shell(
        f"ping -c 1 -W 2 {host}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        return f"{host} 可达\n{stdout.decode().strip()}"
    else:
        return f"{host} 不可达\n{stderr.decode().strip() or stdout.decode().strip()}"
