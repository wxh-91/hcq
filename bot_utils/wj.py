import os
import asyncio
import aiofiles
import subprocess, datetime, shutil

PROJECT_ROOT = os.path.expanduser('~/robot')  # 使用波浪号表示用户主目录下的 robot 文件夹

def _safe_path(filepath: str) -> str:
    filepath = os.path.expanduser(filepath)
    if not os.path.isabs(filepath):
        filepath = os.path.join(PROJECT_ROOT, filepath)
    # 确保目录存在
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    return os.path.realpath(filepath)

async def backup_project():
    backup_dir = os.path.join(PROJECT_ROOT, 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    t = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'{backup_dir}/bot_backup_{t}.zip'
    # 切换到项目根目录并打包，生成到 backup_dir
    cmd = f'cd {PROJECT_ROOT} && zip -r {filename} dz.py admin.py memory.py cl.py memory.json start_bot.sh watchdog.py bot_utils'
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return f"备份失败: {stderr.decode().strip()}"
    return f"已备份至 {filename}"

async def read_file(filepath, max_len=12000, retries=2):
    for attempt in range(retries + 1):
        try:
            real_path = _safe_path(filepath)
            async with aiofiles.open(real_path, 'r', encoding='utf-8') as f:
                content = await f.read()
            if len(content) > max_len:
                content = content[:max_len] + "\n\n... [内容过长，已截断]"
            return content
        except (PermissionError, FileNotFoundError, OSError) as e:
            if attempt == retries:
                return f'读取失败（重试{retries}次）: {str(e)}'
            await asyncio.sleep(0.2)

async def write_file(filepath, content, backup=True, retries=2):
    # 清洗路径：从自然语言中提取类路径字符串
    import re
    match = re.search(r'([~\w/.-]+\.\w+)', filepath)
    if match:
        filepath = match.group(1)
    else:
        # 按逗号/中文逗号切分取第一段（兜底）
        filepath = filepath.split('，')[0].split(',')[0].strip()
    
    for attempt in range(retries + 1):
        try:
            real_path = _safe_path(filepath)
            # 确保父目录存在（增强健壮性）
            parent = os.path.dirname(real_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            # ★ 只在原文件已存在时才备份 ★
            if backup and os.path.exists(real_path):
                await asyncio.to_thread(shutil.copy2, real_path, real_path + '.bak')
            async with aiofiles.open(real_path, 'w', encoding='utf-8') as f:
                await f.write(content)
            return f'已写入 {filepath}'
        except (PermissionError, OSError) as e:
            if attempt == retries:
                return f'写入失败（重试{retries}次）: {str(e)}'
            await asyncio.sleep(0.2)

async def delete_line(filepath, line_num):
    """异步删除指定行（行号从1开始）"""
    try:
        real_path = _safe_path(filepath)
    except PermissionError as e:
        return f'{str(e)}'
    
    try:
        async with aiofiles.open(real_path, 'r', encoding='utf-8') as f:
            content = await f.read()
        lines = content.splitlines(keepends=True)
        if line_num < 1 or line_num > len(lines):
            return f'行号 {line_num} 超出范围（文件共 {len(lines)} 行）'
        del lines[line_num - 1]
        return await write_file(real_path, ''.join(lines), backup=True)
    except FileNotFoundError:
        return f'文件不存在：{filepath}'  # 返回原始路径便于用户理解
    except PermissionError:
        return f'权限不足，无法操作：{filepath}'
    except Exception as e:
        return f'操作失败：{str(e)}'

async def replace_line(filepath, line_num, new_content):
    """异步替换指定行，自动沿用原行换行符"""
    try:
        real_path = _safe_path(filepath)
    except PermissionError as e:
        return f'{str(e)}'
    try:
        async with aiofiles.open(real_path, 'r', encoding='utf-8') as f:
            content = await f.read()
        lines = content.splitlines(keepends=True)
        if line_num < 1 or line_num > len(lines):
            return f'行号 {line_num} 超出范围（文件共 {len(lines)} 行）'
        old_line = lines[line_num - 1]
        line_ending = '\n'
        if old_line.endswith('\r\n'):
            line_ending = '\r\n'
        elif old_line.endswith('\n'):
            line_ending = '\n'
        new_line = new_content.rstrip('\n\r') + line_ending
        lines[line_num - 1] = new_line
        return await write_file(real_path, ''.join(lines), backup=True)
    except FileNotFoundError:
        return f'文件不存在：{filepath}'
    except PermissionError:
        return f'权限不足，无法操作：{filepath}'
    except Exception as e:
        return f'操作失败：{str(e)}'

async def insert_line(filepath, line_num, new_content):
    """异步在指定行前插入新行（行号从1开始）"""
    try:
        real_path = _safe_path(filepath)
    except PermissionError as e:
        return f'{str(e)}'
    try:
        async with aiofiles.open(real_path, 'r', encoding='utf-8') as f:
            content = await f.read()
        lines = content.splitlines(keepends=True)
        if line_num < 1 or line_num > len(lines) + 1:
            return f'行号 {line_num} 超出范围（有效范围 1~{len(lines) + 1}）'
        line_ending = '\n'
        for line in lines:
            if line.endswith('\r\n'):
                line_ending = '\r\n'
                break
            elif line.endswith('\n'):
                line_ending = '\n'
                break
        new_line = new_content.rstrip('\n\r') + line_ending
        lines.insert(line_num - 1, new_line)
        return await write_file(real_path, ''.join(lines), backup=True)
    except FileNotFoundError:
        return f'文件不存在：{filepath}'
    except PermissionError:
        return f'权限不足，无法操作：{filepath}'
    except Exception as e:
        return f'操作失败：{str(e)}'
