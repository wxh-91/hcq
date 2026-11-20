# -*- coding: utf-8 -*-
"""System & File 插件（Plugin 2.0）—— 底层系统交互物理隔离单元。

对外 AUP 工具（注册于 tool_center.db，module_path=plugins.system_ops）：
  - read_file / write_file / delete_line / replace_line / insert_line / backup_project（文件操作，wj）
  - system_status / ping_host（系统状态与连通性，xt）
  - script_runner（run_bash_script 实现本体）：AUP 版由 plugins.core_utils.aup_tools.aup_run_bash_script
    延迟 import 本插件子模块，db 注册指向 aup_tools（不重复封装）。
设计：
  - wj.py / xt.py / script_runner.py 为纯实现子模块（依赖仅标准库 + aiofiles/psutil），
    封装层提供同名 @aup_tool 函数，入参/出参自动校验并收敛为 AUPResult；
  - is_danger 与 tool_center.db 保持一致（write/delete/replace/insert=1 高危）。
"""
from __future__ import annotations

import logging

from aup_core import aup_tool

logger = logging.getLogger("plugins.system_ops")

from . import wj, xt, script_runner  # noqa: E402  （纯实现子模块）


@aup_tool(
    name="backup_project",
    description="按 XMKJ.txt 清单备份项目核心文件到 backups/（自动刷新清单，跳过黑名单）",
    category="system",
    is_danger=False,
    params_schema={},
    suggestion_hint="备份失败，请检查 XMKJ.txt 清单或磁盘空间",
)
async def backup_project() -> str:
    return await wj.backup_project()


@aup_tool(
    name="read_file",
    description="读取指定文件内容（路径需在项目根目录下，自动防穿越）。mode=raw 默认原样返回（适合小文件）；mode=summary 摘要模式只读头 200 行+尾 50 行并强制提示提取核心结论（适合大文件防 Token 爆炸）。",
    category="file",
    is_danger=False,
    params_schema={
        "required": ["filepath"],
        "properties": {
            "filepath": {"type": "string", "description": "文件路径（相对项目根或绝对路径）"},
            "max_len": {"type": "integer", "description": "最大返回长度，默认 12000"},
            "mode": {"type": "string", "description": "raw=原样返回（默认）；summary=摘要模式（头200行+尾50行+提取核心结论提示）"},
        },
    },
    suggestion_hint="文件读取失败，请确认路径存在且权限足够",
)
async def read_file(filepath: str, max_len: int = 12000, mode: str = "raw") -> str:
    return await wj.read_file(filepath, max_len, mode=mode)


@aup_tool(
    name="summarize_large_file",
    description=(
        "[宏] 大文件智能摘要：后台读取本地文件（超大文件自动截断为头 200 行+尾 50 行），"
        "调用大模型压缩为几百字核心摘要返回。面对超大文件（日志、CSV、长文档）时优先用本工具，避免全文进入上下文。"
    ),
    category="file",
    is_danger=False,
    params_schema={
        "required": ["filepath"],
        "properties": {
            "filepath": {"type": "string", "description": "本地文件绝对路径，如 /root/robot/big.log"},
        },
    },
    suggestion_hint="文件不存在或摘要失败时说明原因",
)
async def summarize_large_file(filepath: str) -> str:
    content = await wj.read_file(filepath, max_len=8000, mode="summary")
    if content.startswith("读取失败") or content.startswith("文件不存在"):
        return content
    try:
        from ai import safe_api_request

        reply, _ = await safe_api_request(
            [
                {
                    "role": "system",
                    "content": "你是文件摘要助手。把用户提供的文件内容压缩为 300 字以内核心摘要，分要点列出关键信息，不要复述原文。",
                },
                {
                    "role": "user",
                    "content": "文件：%s\n内容（可能已截断）：\n%s" % (filepath, content[:8000]),
                },
            ],
            strategy="chat",
            tools=None,
        )
        return "【文件摘要】%s\n%s" % (filepath, (reply or "摘要失败")[:600])
    except Exception as e:
        return "摘要失败：%s" % type(e).__name__


@aup_tool(
    name="write_file",
    description="覆盖写入文件内容（危险操作，原文件存在时自动备份 .bak）",
    category="file",
    is_danger=True,
    params_schema={
        "required": ["filepath", "content"],
        "properties": {
            "filepath": {"type": "string", "description": "目标文件路径"},
            "content": {"type": "string", "description": "要写入的完整内容"},
        },
    },
    suggestion_hint="文件写入失败，请检查路径、权限与磁盘空间",
)
async def write_file(filepath: str, content: str) -> str:
    return await wj.write_file(filepath, content)


@aup_tool(
    name="delete_line",
    description="删除文件指定行（行号从 1 开始，危险操作）",
    category="file",
    is_danger=True,
    params_schema={
        "required": ["filepath", "line_num"],
        "properties": {
            "filepath": {"type": "string", "description": "目标文件路径"},
            "line_num": {"type": "integer", "description": "要删除的行号（1 开始）"},
        },
    },
    suggestion_hint="行删除失败，请检查文件路径与行号范围",
)
async def delete_line(filepath: str, line_num: int) -> str:
    return await wj.delete_line(filepath, line_num)


@aup_tool(
    name="replace_line",
    description="替换文件指定行的内容（行号从 1 开始，危险操作）",
    category="file",
    is_danger=True,
    params_schema={
        "required": ["filepath", "line_num", "new_content"],
        "properties": {
            "filepath": {"type": "string", "description": "目标文件路径"},
            "line_num": {"type": "integer", "description": "要替换的行号（1 开始）"},
            "new_content": {"type": "string", "description": "新的行内容"},
        },
    },
    suggestion_hint="行替换失败，请检查文件路径与行号范围",
)
async def replace_line(filepath: str, line_num: int, new_content: str) -> str:
    return await wj.replace_line(filepath, line_num, new_content)


@aup_tool(
    name="insert_line",
    description="在指定行前插入新行（行号从 1 开始，危险操作）",
    category="file",
    is_danger=True,
    params_schema={
        "required": ["filepath", "line_num", "new_content"],
        "properties": {
            "filepath": {"type": "string", "description": "目标文件路径"},
            "line_num": {"type": "integer", "description": "插入位置行号（1 开始）"},
            "new_content": {"type": "string", "description": "要插入的新行内容"},
        },
    },
    suggestion_hint="行插入失败，请检查文件路径与行号范围",
)
async def insert_line(filepath: str, line_num: int, new_content: str) -> str:
    return await wj.insert_line(filepath, line_num, new_content)


@aup_tool(
    name="system_status",
    description="查看系统状态（CPU、内存、磁盘、运行时长、系统版本）",
    category="system",
    is_danger=False,
    params_schema={},
    suggestion_hint="系统状态获取失败，请检查 psutil 是否安装",
)
async def system_status() -> str:
    return await xt.system_status()


@aup_tool(
    name="ping_host",
    description="检测主机是否可达（ping -c 1，参数化执行防注入）",
    category="network",
    is_danger=False,
    params_schema={
        "required": ["host"],
        "properties": {"host": {"type": "string", "description": "主机名或 IP"}},
    },
    suggestion_hint="Ping 失败，请检查目标主机可达性",
)
async def ping_host(host: str) -> str:
    return await xt.ping_host(host)
