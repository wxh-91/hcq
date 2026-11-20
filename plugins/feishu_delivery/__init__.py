# -*- coding: utf-8 -*-
"""Feishu Delivery 插件（Plugin 8.0）—— 多模态投递：飞书文件与图片发送。

对外 AUP 宏工具：
  feishu_send_file(filepath)  —— 读取本地文件（csv/xlsx/txt/zip 等）上传飞书并发送附件
  feishu_send_image(filepath) —— 读取本地图片（png/jpg）上传并发送

底层：im/v1/files、im/v1/images 上传 → im/v1/messages 发送到管理员会话。
"""
from __future__ import annotations

import json
import logging
import os

import aiofiles
import aiohttp

from aup_core import aup_tool

logger = logging.getLogger("plugins.feishu_delivery")
FEISHU_BASE = "https://open.feishu.cn/open-apis"
MAX_FILE = 20 * 1024 * 1024  # 飞书单文件上限 20MB

# 扩展名 → 飞书 file_type（stream 为通用二进制）
_FILE_TYPE = {
    ".pdf": "pdf", ".doc": "doc", ".docx": "doc",
    ".xls": "xls", ".xlsx": "xls",
    ".ppt": "ppt", ".pptx": "ppt",
    ".zip": "stream", ".csv": "stream", ".txt": "stream",
    ".json": "stream", ".md": "stream", ".log": "stream",
    ".py": "stream", ".sh": "stream",
}


async def _token() -> str:
    from plugins.core_utils import feishu_bridge

    return await feishu_bridge.get_tenant_token()


def _admin() -> str:
    from plugins.core_utils import feishu_bridge

    return feishu_bridge.admin_open_id()


async def _send_msg(msg_type: str, content: dict) -> str:
    # [R11-feishu-off] 飞书退场开关关闭时不发请求，直接把结论回给调用方（文件仍在本地）
    from plugins.core_utils.notify_bridge import feishu_enabled

    if not feishu_enabled():
        return "飞书已退场（FEISHU_ENABLED=0）：未发送，文件保留在本地原路径。"
    token = await _token()
    admin = _admin()
    if not token or not admin:
        return "发送失败：无法获取 token 或管理员 open_id"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                FEISHU_BASE + "/im/v1/messages?receive_id_type=open_id",
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json; charset=utf-8",
                },
                json={
                    "receive_id": admin,
                    "msg_type": msg_type,
                    "content": json.dumps(content, ensure_ascii=False),
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                obj = await r.json()
        if obj.get("code") == 0:
            return "已发送"
        return "发送失败：%s %s" % (obj.get("code"), obj.get("msg"))
    except Exception as e:
        return "发送异常：%s" % type(e).__name__


async def _upload_file(data: bytes, name: str):
    token = await _token()
    if not token:
        return None
    ext = os.path.splitext(name)[1].lower()
    ftype = _FILE_TYPE.get(ext, "stream")
    try:
        form = aiohttp.FormData()
        form.add_field("file_type", ftype)
        form.add_field("file_name", name)
        form.add_field("file", data, filename=name, content_type="application/octet-stream")
        async with aiohttp.ClientSession() as s:
            async with s.post(
                FEISHU_BASE + "/im/v1/files",
                headers={"Authorization": "Bearer " + token},
                data=form,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                obj = await r.json()
        if obj.get("code") == 0:
            key = obj.get("data", {}).get("file_key")
            logger.info("[feishu_delivery] 文件上传成功 file_key=%s", key)
            return key
        logger.error("[feishu_delivery] 文件上传失败: %s %s", obj.get("code"), obj.get("msg"))
        return None
    except Exception as e:
        logger.error("[feishu_delivery] 文件上传异常: %s", type(e).__name__)
        return None


async def _upload_image(data: bytes, name: str):
    token = await _token()
    if not token:
        return None
    try:
        form = aiohttp.FormData()
        form.add_field("image_type", "message")
        form.add_field("image", data, filename=name, content_type="image/png")
        async with aiohttp.ClientSession() as s:
            async with s.post(
                FEISHU_BASE + "/im/v1/images",
                headers={"Authorization": "Bearer " + token},
                data=form,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                obj = await r.json()
        if obj.get("code") == 0:
            key = obj.get("data", {}).get("image_key")
            logger.info("[feishu_delivery] 图片上传成功 image_key=%s", key)
            return key
        logger.error("[feishu_delivery] 图片上传失败: %s %s", obj.get("code"), obj.get("msg"))
        return None
    except Exception as e:
        logger.error("[feishu_delivery] 图片上传异常: %s", type(e).__name__)
        return None


@aup_tool(
    name="feishu_send_file",
    description=(
        "[宏] 飞书文件投递：读取本地文件（csv/xlsx/txt/zip 等）上传飞书并作为附件发送给用户。"
        "如果你生成了表格数据（CSV）或文档，不要把纯文本贴给用户，"
        "请保存为本地文件后调用本工具直接发送附件。"
    ),
    category="feishu",
    is_danger=False,
    params_schema={
        "required": ["filepath"],
        "properties": {
            "filepath": {"type": "string", "description": "本地文件绝对路径，如 /root/robot/data.csv"},
        },
    },
    suggestion_hint="文件不存在、超过 20MB 或上传失败时说明原因",
)
async def feishu_send_file(filepath: str) -> str:
    real = os.path.expanduser(filepath)
    if not os.path.exists(real):
        return "文件不存在：%s" % filepath
    try:
        async with aiofiles.open(real, "rb") as f:
            data = await f.read()
    except Exception as e:
        return "读取失败：%s" % type(e).__name__
    if len(data) > MAX_FILE:
        return "文件超过 20MB，无法上传"
    name = os.path.basename(real)
    key = await _upload_file(data, name)
    if not key:
        return "上传失败（请检查 token 与权限）"
    return await _send_msg("file", {"file_key": key, "file_name": name})


@aup_tool(
    name="feishu_send_image",
    description=(
        "[宏] 飞书图片投递：读取本地图片（png/jpg 等）上传并作为图片消息发送给用户。"
        "如果你生成了图片文件，请保存为本地文件后调用本工具直接发送，不要贴占位文本。"
    ),
    category="feishu",
    is_danger=False,
    params_schema={
        "required": ["filepath"],
        "properties": {
            "filepath": {"type": "string", "description": "本地图片绝对路径，如 /root/robot/chart.png"},
        },
    },
    suggestion_hint="文件不存在或上传失败时说明原因",
)
async def feishu_send_image(filepath: str) -> str:
    real = os.path.expanduser(filepath)
    if not os.path.exists(real):
        return "文件不存在：%s" % filepath
    try:
        async with aiofiles.open(real, "rb") as f:
            data = await f.read()
    except Exception as e:
        return "读取失败：%s" % type(e).__name__
    if len(data) > MAX_FILE:
        return "文件超过 20MB，无法上传"
    name = os.path.basename(real)
    key = await _upload_image(data, name)
    if not key:
        return "上传失败（请检查 token 与权限）"
    return await _send_msg("image", {"image_key": key})
