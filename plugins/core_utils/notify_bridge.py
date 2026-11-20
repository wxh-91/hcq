# -*- coding: utf-8 -*-
"""notify_bridge —— 统一通知出口（飞书退场支撑层, R11）

设计目的：
    关掉飞书之后，何春秋的「定时任务 / 提醒 / 工具心跳 / 防御巡检报警」
    依然必须能落地。所以所有通知不再直接绑死飞书，而是走本模块：

    1) FEISHU_ENABLED=1（默认）：行为与改造前一致 —— 发飞书私聊。
    2) FEISHU_ENABLED=0 或发送失败：消息绝不丢，写入本地 outbox（JSONL，按天分文件）。
       outbox 默认落在 Windows 侧 D:\\dsh-custom\\_notify\\YYYY-MM-DD.jsonl，
       DSH、PowerShell、文本编辑器都能直接读（WSL 通过 /mnt/d 访问）。
    3) 任何异常都被吞掉并记日志：通知失败不允许拖垮调度器或巡检流程。

对外接口：
    feishu_enabled() -> bool
    async notify(text, title="通知", level="info", feishu=None) -> dict
    notify_sync(text, title="通知", level="info") -> dict
    read_recent(limit=20) -> list[dict]
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime

logger = logging.getLogger("notify_bridge")

FEISHU_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
OUTBOX_DIR = os.getenv("NOTIFY_OUTBOX_DIR", "/mnt/d/dsh-custom/_notify")
_FALSE = ("0", "false", "off", "no", "disabled", "")


def feishu_enabled() -> bool:
    """飞书通道总开关。默认开启，只有显式置 0/false/off/no/disabled 才关闭。"""
    return os.getenv("FEISHU_ENABLED", "1").strip().lower() not in _FALSE


def outbox_path(day: str | None = None) -> str:
    day = day or datetime.now().strftime("%Y-%m-%d")
    return os.path.join(OUTBOX_DIR, day + ".jsonl")


def _write_local(text: str, title: str, level: str, extra: dict | None) -> str:
    """把通知落到本地 outbox；返回文件路径，失败返回空串。"""
    try:
        os.makedirs(OUTBOX_DIR, exist_ok=True)
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "level": level,
            "title": title,
            "text": text,
            "host": "wsl-robot",
        }
        if extra:
            rec.update(extra)
        path = outbox_path()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return path
    except Exception as e:
        logger.error("[notify] 本地落盘失败: %s", e)
        return ""


async def _send_feishu(text: str) -> bool:
    """按改造前同样的方式发飞书私聊（tenant token + admin open_id）。"""
    try:
        from plugins.core_utils import feishu_bridge

        token = await feishu_bridge.get_tenant_token()
        admin = feishu_bridge.admin_open_id()
        if not token or not admin:
            logger.warning("[notify] 飞书 token / admin_open_id 缺失，改走本地")
            return False
        import aiohttp

        async with aiohttp.ClientSession() as s:
            async with s.post(
                FEISHU_URL,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json; charset=utf-8",
                },
                json={
                    "receive_id": admin,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    logger.warning("[notify] 飞书返回 %s，改走本地", r.status)
                    return False
        return True
    except Exception as e:
        logger.warning("[notify] 飞书发送异常(%s)，改走本地", type(e).__name__)
        return False


async def notify(text: str, title: str = "通知", level: str = "info", feishu: bool | None = None) -> dict:
    """统一通知入口：先本地落盘，再按开关尝试飞书。永不抛异常。

    feishu=None  -> 跟随 FEISHU_ENABLED
    feishu=True  -> 本次强制尝试飞书（例如飞书会话内的回复）
    feishu=False -> 本次只写本地
    """
    text = "" if text is None else str(text)
    use_feishu = feishu_enabled() if feishu is None else bool(feishu)

    path = _write_local(text, title, level, {"feishu_enabled": use_feishu})
    delivered = ["local"] if path else []

    if use_feishu:
        if await _send_feishu(text):
            delivered.append("feishu")

    logger.info("[notify] title=%s level=%s channels=%s path=%s", title, level, delivered, path)
    return {"delivered": delivered, "path": path, "feishu_enabled": use_feishu}


def notify_sync(text: str, title: str = "通知", level: str = "info") -> dict:
    """同步版（给非 async 调用方用）：本地落盘 + 尽力飞书（无事件循环时跳过飞书）。"""
    path = _write_local(text, title, level, None)
    delivered = ["local"] if path else []
    if feishu_enabled():
        try:
            import asyncio

            loop = asyncio.new_event_loop()
            try:
                ok = loop.run_until_complete(_send_feishu(text))
            finally:
                loop.close()
            if ok:
                delivered.append("feishu")
        except Exception as e:
            logger.warning("[notify] 同步飞书发送跳过: %s", type(e).__name__)
    return {"delivered": delivered, "path": path}


def read_recent(limit: int = 20) -> list:
    """读取最近的通知（供 DSH / PowerShell 侧巡检用）。"""
    try:
        path = outbox_path()
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        return [json.loads(x) for x in lines[-limit:] if x.strip()]
    except Exception as e:
        logger.warning("[notify] 读取 outbox 失败: %s", e)
        return []


if __name__ == "__main__":
    # 自检：python3 plugins/core_utils/notify_bridge.py "测试文本"
    msg = " ".join(sys.argv[1:]) or "notify_bridge 自检"
    print(json.dumps(notify_sync(msg, title="自检"), ensure_ascii=False))
