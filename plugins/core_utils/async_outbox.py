# -*- coding: utf-8 -*-
"""异步产物消息出站箱：后台 Worker 完成后，把附件卡片消息推回发起会话。

背景：m3u8 长清单转入后台 Worker 时，原始 HTTP 请求已经结束，请求级
file_delivery 上下文已清空；Worker 完成后无法再把 [FILE:] 标记拼进当轮回复。
本模块按「归属用户 + 前端会话」落一份待发消息，前端轮询取走后渲染附件卡片。

- 存储：auth-gateway/data/async_outbox.jsonl（追加写，进程重启不丢）
- 归属：owner_id 来自请求上下文（JWT sub / user_ref）；client_sid 为前端会话 id
- 消费：GET /api/pending_messages（经 8083/8084 反代），取走即标记 delivered
"""
import json
import os
import threading
import time
import uuid
from pathlib import Path

DATA_DIR = Path(os.environ.get(
    "ASYNC_OUTBOX_DIR", "/mnt/d/dsh-custom/auth-gateway/data"
))
FILE_PATH = DATA_DIR / "async_outbox.jsonl"
_LOCK = threading.Lock()
MAX_RETAINED = 500


def _load() -> list:
    if not FILE_PATH.is_file():
        return []
    rows = []
    try:
        for line in FILE_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return rows


def _save(rows: list) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    keep = rows[-MAX_RETAINED:]
    tmp = FILE_PATH.with_suffix(".jsonl.tmp")
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in keep)
    if keep:
        body += "\n"
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, FILE_PATH)


def enqueue(owner_id: str, session_key: str, client_sid: str,
            content: str, files: list) -> dict:
    """登记一条待发助手消息。files 元素形如 {id,name,size,mime}。"""
    rec = {
        "id": "msg_" + uuid.uuid4().hex[:16],
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "owner_id": str(owner_id or "").strip(),
        "session_key": str(session_key or ""),
        "client_sid": str(client_sid or "").strip(),
        "content": str(content or ""),
        "files": files or [],
        "delivered": False,
    }
    with _LOCK:
        rows = _load()
        rows.append(rec)
        _save(rows)
    return rec


def fetch_pending(owner_id: str, client_sid: str = None) -> list:
    """取走归属当前用户（可再按前端会话过滤）的未投递消息，并标记已投递。"""
    owner = str(owner_id or "").strip()
    sid = str(client_sid or "").strip()
    with _LOCK:
        rows = _load()
        out = []
        for r in rows:
            if r.get("delivered"):
                continue
            if r.get("owner_id") != owner:
                continue
            if sid and r.get("client_sid") and r["client_sid"] != sid:
                continue
            r["delivered"] = True
            out.append(r)
        if out:
            _save(rows)
    return out
