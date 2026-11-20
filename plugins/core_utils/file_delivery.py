# -*- coding: utf-8 -*-
"""受控附件交付：把工具生成的文件落盘到本机受控目录并登记归属，产出可点击下载地址。

- 物理目录：D:\\dsh-custom\\files（8083 下载端点 GET /v1/files/{id} 从这里出流）
- 归属库：D:\\dsh-custom\\files\\data\\auth.db 的 file_registry 表
- 标记格式：[FILE: id=f_xxx, name=yyy.mp3, size=12345] 下载：http://127.0.0.1:8083/v1/files/f_xxx

单用户本地部署：owner 恒为 local-admin，不再按角色限制。
"""
import os
_ROOT = os.environ.get("PROJECT_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import contextvars
import mimetypes
import re
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
import os


FILE_URL_BASE = "http://127.0.0.1:8083/v1/files/"
FILES_DIR = Path(_ROOT + "/downloads/files")
DB_PATH = FILES_DIR / "data" / "auth.db"

_ctx = contextvars.ContextVar("hcq_file_ctx", default=None)

_SAFE_RE = re.compile(r"[^A-Za-z0-9._\-一-鿿]+")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def set_owner(owner_id: str) -> None:
    """由 HTTP 端点写入本次请求的归属用户；同时清空上一轮遗留的待发文件。"""
    _ctx.set({"owner": str(owner_id or "").strip(), "files": []})


def clear() -> None:
    _ctx.set(None)


def _ctx_dict() -> dict:
    return dict(_ctx.get() or {"owner": "", "files": []})


def current_owner() -> str:
    return _ctx_dict().get("owner", "")


def pending_markers() -> list:
    """本轮请求已登记的全部文件标记（供端点拼接到回复文本末尾）。"""
    return [marker(r) for r in _ctx_dict().get("files", [])]


def _safe_name(name: str) -> str:
    name = Path(str(name or "file")).name
    name = _SAFE_RE.sub("_", name).strip("._")
    return name or "file"


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS file_registry (
            file_id    TEXT PRIMARY KEY,
            owner_id   TEXT NOT NULL,
            name       TEXT NOT NULL,
            size       INTEGER NOT NULL DEFAULT 0,
            mime       TEXT,
            rel_path   TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def register_file(src_path, name: str = None, mime: str = None) -> dict | None:
    """把已生成的文件复制进受控目录并登记归属。返回登记记录；源文件不存在则返回 None。"""
    src = Path(src_path)
    if not src.is_file():
        return None

    FILES_DIR.mkdir(parents=True, exist_ok=True)
    owner = current_owner() or "local-admin"
    file_id = "f_" + uuid.uuid4().hex[:16]
    fname = _safe_name(name or src.name)
    rel = f"{file_id}_{fname}"
    dest = FILES_DIR / rel
    shutil.copy2(src, dest)
    size = dest.stat().st_size
    mime = mime or mimetypes.guess_type(fname)[0] or "application/octet-stream"

    conn = sqlite3.connect(str(DB_PATH))
    try:
        _ensure_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO file_registry "
            "(file_id, owner_id, name, size, mime, rel_path, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (file_id, owner, fname, size, mime, rel, _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()

    rec = {"file_id": file_id, "owner_id": owner, "name": fname, "size": size, "mime": mime}
    ctx = _ctx.get()
    if ctx is None:
        ctx = {"owner": owner, "files": []}
        _ctx.set(ctx)
    ctx["files"].append(rec)
    return rec


def marker(rec: dict) -> str:
    return (f"[FILE: id={rec['file_id']}, name={rec['name']}, size={rec['size']}] "
            f"下载：{FILE_URL_BASE}{rec['file_id']}")
