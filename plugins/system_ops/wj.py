import os
import asyncio
import aiofiles
import subprocess, datetime, shutil
import logging
logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.expanduser('~/robot')  # 使用波浪号表示用户主目录下的 robot 文件夹

def _safe_path(filepath: str) -> str:
    filepath = os.path.expanduser(filepath)
    if not os.path.isabs(filepath):
        filepath = os.path.join(PROJECT_ROOT, filepath)
    # 确保目录存在
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    return os.path.realpath(filepath)

async def backup_project():
    from plugins.core_utils.backup_manifest import (
        is_manifest_stale, refresh_manifest, load_manifest, BLACKLIST
    )

    # ===== 1. 缓存失效检测与刷新（失败直接报错，Q6=A） =====
    try:
        if is_manifest_stale():
            logger.info("[backup_project] XMKJ.txt 已变更，重新解析清单")
            await refresh_manifest()
        items = load_manifest()
    except Exception as e:
        logger.error(f"[backup_project] 解析 XMKJ.txt 失败: {e}", exc_info=True)
        return f"备份失败：解析 XMKJ.txt 出错 - {str(e)}"

    # ===== 2. 提取路径并过滤黑名单 =====
    paths = []
    for it in items:
        p = str(it.get('path', '')).strip().strip('/')
        if not p:
            continue
        top = p.split('/')[0]
        if top in BLACKLIST:
            logger.debug(f"[backup_project] 跳过黑名单项: {p}")
            continue
        paths.append(p)

    # ===== 3. 去重：父目录已包含则跳过子路径（Q2=C 代码侧兜底） =====
    paths = sorted(set(paths), key=lambda x: x.count('/'))
    final_paths = []
    for p in paths:
        if any(p == fp or p.startswith(fp + '/') for fp in final_paths):
            continue
        final_paths.append(p)

    if not final_paths:
        return "备份失败：清单为空（XMKJ.txt 解析结果中无可备份项）"

    # ===== 4. 打包 =====
    backup_dir = os.path.join(PROJECT_ROOT, 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    t = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'{backup_dir}/bot_backup_{t}.zip'

    logger.info(f"[backup_project] 执行打包，共 {len(final_paths)} 项")
    import zipfile

    # 用 zipfile 打包并置 UTF-8 文件名标记：Info-ZIP 在该环境不写 Unicode 路径，
    # 中文名会被按 cp437 解码成乱码；Python 写入会带上 UTF-8 位，Windows 解压可正常显示。
    added = 0
    try:
        with zipfile.ZipFile(filename, 'w', zipfile.ZIP_DEFLATED) as zf:
            for p in final_paths:
                abs_p = os.path.join(PROJECT_ROOT, p)
                if os.path.isdir(abs_p):
                    for root, dirs, files in os.walk(abs_p):
                        rel_root = os.path.relpath(root, PROJECT_ROOT)
                        zf.write(root, rel_root + '/')
                        for f in sorted(files):
                            fp = os.path.join(root, f)
                            if os.path.islink(fp) or not os.path.isfile(fp):
                                continue
                            zf.write(fp, os.path.relpath(fp, PROJECT_ROOT))
                            added += 1
                elif os.path.isfile(abs_p):
                    zf.write(abs_p, p)
                    added += 1
    except Exception as e:
        logger.error(f"[backup_project] 打包失败: {e}", exc_info=True)
        return f"备份失败: {str(e)}"
    if added == 0:
        return "备份失败：清单项均不存在"
    return f"已备份至 {filename}（{added} 个文件）"

async def read_file(filepath, max_len=12000, retries=2, mode="raw"):
    for attempt in range(retries + 1):
        try:
            real_path = _safe_path(filepath)
            async with aiofiles.open(real_path, 'r', encoding='utf-8') as f:
                content = await f.read()
            if mode == "summary":
                # ===== [Chronos·降维] 摘要模式：头 200 行 + 尾 50 行，防 Token 爆炸 =====
                lines = content.splitlines()
                if len(lines) > 250:
                    head = "\n".join(lines[:200])
                    tail = "\n".join(lines[-50:])
                    content = "%s\n\n...（中间省略 %d 行）...\n\n%s" % (head, len(lines) - 250, tail)
                content += "\n\n【注意】文件内容已按摘要模式返回（可能已截断），请勿全文复述给用户，请提取核心结论。"
            elif len(content) > max_len:
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
