# -*- coding: utf-8 -*-
"""feishu_bridge —— QQ(onebot) → 飞书 兼容适配层。

职责（仅替换外壳，业务逻辑零改动）：
  1. 把 nonebot-adapter-feishu 的事件伪装成 dz.py 业务层期望的 onebot 事件接口
     （user_id / self_id / message_type / group_id / get_plaintext / message segments）；
  2. 把 feishu bot 伪装成 onebot bot 接口（send / send_private_msg / call_api）；
  3. 图片/文件资源：飞书消息资源 → 下载到 /tmp/feishu_media/ → 懒启动本地 HTTP 服务
     （127.0.0.1:8765），给业务层提供可下载的 HTTP URL（OCR / 文件处理零改动）。

关键设计：
  - open_id ↔ int 稳定映射（crc32 哈希）：业务层大量 int(event.user_id)、str(event.user_id)、
    send_private_msg(user_id=int(...))，open_id 是字符串无法 int()，因此统一映射为稳定 int，
    业务逻辑无需任何改动；映射表维护双向注册，发送时反查回 open_id。
  - FEISHU_ADMIN_OPEN_ID：防御中枢报警 / 签到通知的兜底收件人；未配置时日志占位、不发送。
  - tenant token 自取：aiohttp 直连飞书 API（POST auth/v3/tenant_access_token/internal），
    不依赖 adapter 内部实现；FEISHU_APP_ID / FEISHU_APP_SECRET 由 dz.py 的 load_dotenv 注入。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import zlib
from pathlib import Path
from typing import Any, List, Optional

import aiohttp

logger = logging.getLogger("feishu.bridge")

FEISHU_BASE = "https://open.feishu.cn/open-apis"
MEDIA_DIR = Path("/tmp/feishu_media")
MEDIA_PORT = 8765

# ---------------------------------------------------------------------------
# open_id ↔ int 稳定映射
# ---------------------------------------------------------------------------
_int_to_oid: dict[int, str] = {}
_file_url_cache: dict[str, str] = {}   # file_key -> 本地 HTTP URL（call_api 拦截用）


def _oid_hash(open_id: str) -> int:
    """open_id 稳定映射为 int（crc32，跨进程/重启一致）。"""
    return zlib.crc32(open_id.encode("utf-8")) & 0x7FFFFFFF


def register_open_id(open_id: str) -> int:
    if not open_id:
        return 0
    uid = _oid_hash(open_id)
    _int_to_oid.setdefault(uid, open_id)
    return uid


def resolve_open_id(uid: Any) -> Optional[str]:
    """int 或 open_id 字符串 → open_id；解析失败返回 None。"""
    if uid is None:
        return None
    if isinstance(uid, str) and uid.startswith("ou_"):
        return uid
    try:
        return _int_to_oid.get(int(uid))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# tenant token（自取，独立于 adapter 内部实现）
# ---------------------------------------------------------------------------
_token_cache: dict = {"token": None, "exp": 0.0}


def _load_env_pair() -> tuple:
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    return app_id, app_secret


async def get_tenant_token() -> Optional[str]:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["exp"] - 60:
        return _token_cache["token"]
    app_id, app_secret = _load_env_pair()
    if not app_id or not app_secret:
        logger.error("[feishu] FEISHU_APP_ID/FEISHU_APP_SECRET 未配置，资源类能力不可用")
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                FEISHU_BASE + "/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                obj = await r.json()
        if obj.get("code") != 0:
            logger.error("[feishu] tenant token 获取失败: %s %s", obj.get("code"), obj.get("msg"))
            return None
        _token_cache["token"] = obj["tenant_access_token"]
        _token_cache["exp"] = now + float(obj.get("expire", 7200))
        return _token_cache["token"]
    except Exception as e:
        logger.error("[feishu] tenant token 获取异常: %s", e, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# 本地媒体 HTTP 服务（懒启动，供 OCR / 文件下载复用）
# ---------------------------------------------------------------------------
_media_server: Optional[subprocess.Popen] = None


def ensure_media_server() -> None:
    global _media_server
    if _media_server is not None:
        return
    try:
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        _media_server = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(MEDIA_PORT), "--directory", str(MEDIA_DIR)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("[feishu] 本地媒体服务已启动: http://127.0.0.1:%s/ (%s)", MEDIA_PORT, MEDIA_DIR)
    except Exception as e:
        logger.error("[feishu] 本地媒体服务启动失败: %s", e, exc_info=True)


async def _download_resource(message_id: str, file_key: str, rtype: str) -> Optional[bytes]:
    """下载飞书消息资源（图片/文件），im:resource 权限。"""
    token = await get_tenant_token()
    if not token:
        return None
    url = f"{FEISHU_BASE}/im/v1/messages/{message_id}/resources/{file_key}?type={rtype}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url, headers={"Authorization": "Bearer " + token},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status == 200:
                    return await r.read()
                logger.error("[feishu] 资源下载失败 HTTP %s (%s)", r.status, rtype)
                return None
    except Exception as e:
        logger.error("[feishu] 资源下载异常: %s", e, exc_info=True)
        return None


def _save_media(data: bytes, ext: str) -> Optional[str]:
    """保存到 MEDIA_DIR，返回本地 HTTP URL。"""
    ensure_media_server()
    try:
        fname = f"m_{int(time.time() * 1000)}_{zlib.crc32(data) & 0x7FFFFFFF}.{ext}"
        (MEDIA_DIR / fname).write_bytes(data)
        return f"http://127.0.0.1:{MEDIA_PORT}/{fname}"
    except Exception as e:
        logger.error("[feishu] 媒体保存失败: %s", e, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# 兼容 segment（onebot 风格：.type / .data(dict)）
# ---------------------------------------------------------------------------
class Seg:
    def __init__(self, type_: str, data: dict):
        self.type = type_
        self.data = data


def _seg_data_of(segment) -> dict:
    try:
        return dict(segment.data)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 事件视图：把 feishu event 伪装成 onebot 事件
# ---------------------------------------------------------------------------
class FeishuEventView:
    def __init__(self, event, bot, message: Optional[List[Seg]] = None):
        self._raw = event          # 原始 feishu 事件（send 时透传）
        self._bot = bot
        self._message = message if message is not None else []
        open_id = ""
        try:
            open_id = event.get_user_id() or ""
        except Exception:
            pass
        self._open_id = open_id
        self.user_id = register_open_id(open_id)          # int（稳定映射）
        try:
            self.self_id = register_open_id(str(bot.self_id))
        except Exception:
            self.self_id = 0

        # message_type / group_id 按事件类型判断
        from nonebot.adapters.feishu import GroupMessageEvent
        self.message_type = "group" if isinstance(event, GroupMessageEvent) else "private"
        self.group_id = ""
        if self.message_type == "group":
            try:
                self.group_id = str(event.event.chat_id or "")
            except Exception:
                self.group_id = ""

    @classmethod
    async def build(cls, event, bot) -> "FeishuEventView":
        """构建视图，并预转换消息段（下载图片/文件资源 → 本地 HTTP URL）。"""
        view = cls(event, bot)
        view._message = await view._convert_message()
        return view

    async def _convert_message(self) -> List[Seg]:
        segs: List[Seg] = []
        try:
            raw_message = self._raw.message
        except Exception:
            return segs
        message_id = ""
        try:
            message_id = str(self._raw.message_id or "")
        except Exception:
            pass
        for seg in raw_message:
            stype = getattr(seg, "type", "")
            data = _seg_data_of(seg)
            if stype == "text":
                segs.append(Seg("text", {"text": data.get("text", "")}))
            elif stype == "image":
                key = data.get("image_key")
                url = None
                if key and message_id:
                    raw = await _download_resource(message_id, key, "image")
                    if raw:
                        url = _save_media(raw, "png")
                segs.append(Seg("image", {"url": url}))
            elif stype == "file":
                fkey = data.get("file_key")
                fname = data.get("file_name", "unknown")
                fsize = data.get("file_size", 0)
                url = None
                if fkey and message_id:
                    raw = await _download_resource(message_id, fkey, "file")
                    if raw:
                        url = _save_media(raw, "bin")
                        _file_url_cache[fkey] = url
                segs.append(Seg("file", {
                    "file": fname,
                    "file_name": fname,
                    "file_size": fsize,
                    "file_id": fkey,
                    "file_url": url,
                }))
            # at / 其他段不参与业务解析，跳过
        return segs

    @property
    def message(self) -> List[Seg]:
        return self._message

    def get_plaintext(self) -> str:
        try:
            return self._raw.get_plaintext() or ""
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# Bot 视图：把 feishu bot 伪装成 onebot bot
# ---------------------------------------------------------------------------
class FeishuBotView:
    def __init__(self, bot):
        self._bot = bot
        try:
            self.self_id = str(bot.self_id)
        except Exception:
            self.self_id = ""

    # ---- 发送 ----
    async def send(self, event, message=None, **kwargs):
        if isinstance(event, FeishuEventView):
            event = event._raw
        message = await self._normalize_outgoing(message)
        return await self._bot.send(event, message, **kwargs)

    async def send_private_msg(self, user_id=None, message=None, **kwargs):
        open_id = resolve_open_id(user_id)
        if not open_id:
            open_id = os.environ.get("FEISHU_ADMIN_OPEN_ID", "")
        if not open_id:
            logger.error("[feishu] send_private_msg 无法解析目标 open_id（user_id=%r），跳过", user_id)
            return None
        text = extract_plain_text(message)
        content = json.dumps({"text": text}, ensure_ascii=False)
        return await self._bot.send_msg(
            receive_id_type="open_id", receive_id=open_id,
            msg_type="text", content=content,
        )

    async def send_msg(self, receive_id_type: str, receive_id: str, content: str, msg_type: str):
        return await self._bot.send_msg(
            receive_id_type=receive_id_type, receive_id=receive_id,
            content=content, msg_type=msg_type,
        )

    # ---- call_api：拦截 QQ 文件下载 API，转飞书资源下载 ----
    async def call_api(self, api: str, **params):
        if api in ("get_group_file_url", "get_private_file_url"):
            file_id = params.get("file_id")
            url = _file_url_cache.get(file_id) or params.get("file_url") or ""
            if url:
                return {"url": url}
            logger.error("[feishu] 资源 URL 不可用（file_id=%r，file_url=%r）", file_id, params.get("file_url"))
            return {"url": ""}
        return await self._bot.call_api(api, **params)

    # ---- 出站消息归一化：image bytes → 上传飞书 → image_key ----
    async def _normalize_outgoing(self, message):
        from nonebot.adapters.feishu import Message, MessageSegment
        if message is None:
            message = ""
        if isinstance(message, str):
            return Message(MessageSegment.text(message))
        if isinstance(message, MessageSegment):
            message = Message(message)
        new_segs = []
        for seg in message:
            if getattr(seg, "type", "") == "image":
                ik = _seg_data_of(seg).get("image_key")
                if isinstance(ik, bytes):
                    key = await self._upload_image(ik)
                    if key:
                        new_segs.append(MessageSegment.image(key))
                        continue
            new_segs.append(seg)
        return Message(new_segs)

    async def _upload_image(self, image_bytes: bytes) -> Optional[str]:
        token = await get_tenant_token()
        if not token:
            return None
        try:
            form = aiohttp.FormData()
            form.add_field("image_type", "message")
            form.add_field("image", image_bytes, filename="shot.png", content_type="image/png")
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
                logger.info("[feishu] 图片上传成功 image_key=%s", key)
                return key
            logger.error("[feishu] 图片上传失败: %s %s", obj.get("code"), obj.get("msg"))
            return None
        except Exception as e:
            logger.error("[feishu] 图片上传异常: %s", e, exc_info=True)
            return None


# ---------------------------------------------------------------------------
# 文本提取（message 可为 str / Message / MessageSegment）
# ---------------------------------------------------------------------------
def extract_plain_text(message) -> str:
    if message is None:
        return ""
    if isinstance(message, str):
        return message
    try:
        if hasattr(message, "extract_plain_text"):
            return message.extract_plain_text() or ""
    except Exception:
        pass
    try:
        return "".join(getattr(s, "data", {}).get("text", "") for s in message if getattr(s, "type", "") == "text")
    except Exception:
        return str(message)


# 飞书管理员 open_id 兼容占位（由人类配置，或私聊后从日志获取）
def admin_open_id() -> str:
    return os.environ.get("FEISHU_ADMIN_OPEN_ID", "")
