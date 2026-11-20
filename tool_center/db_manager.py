"""工具元数据存储驱动：SQLite 读写底层原语。

只提供数据读写能力，不感知上层调用逻辑。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Optional

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tool_center.db")
_lock = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS tools (
    tool_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT UNIQUE NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    category         TEXT NOT NULL DEFAULT 'general',
    schema_json      TEXT NOT NULL DEFAULT '{}',
    is_danger        INTEGER NOT NULL DEFAULT 0,
    is_active        INTEGER NOT NULL DEFAULT 1,
    need_confirm     INTEGER NOT NULL DEFAULT 0,
    params_desc_json TEXT NOT NULL DEFAULT '{}',
    roles_json       TEXT NOT NULL DEFAULT '["admin","member"]',
    module_path      TEXT NOT NULL DEFAULT '',
    func_name        TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_tools_category ON tools(category);
CREATE INDEX IF NOT EXISTS idx_tools_active ON tools(is_active);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock:
        conn = _connect()
        try:
            conn.executescript(_DDL)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(tools)").fetchall()}
            if "roles_json" not in cols:
                conn.execute(
                    "ALTER TABLE tools ADD COLUMN roles_json TEXT NOT NULL "
                    "DEFAULT '[\"admin\", \"member\"]'"
                )
            conn.commit()
        finally:
            conn.close()


def upsert_tool(record: dict) -> int:
    """按 name 插入或更新，返回 tool_id。"""
    with _lock:
        conn = _connect()
        try:
            roles = record.get("roles") or ["admin", "member"]
            if not isinstance(roles, (list, tuple)):
                roles = ["admin", "member"]
            roles = [str(r).strip().lower() for r in roles if str(r).strip()]
            if not roles:
                roles = ["admin", "member"]
            conn.execute(
                """
                INSERT INTO tools
                    (name, description, category, schema_json, is_danger, is_active,
                     need_confirm, params_desc_json, roles_json, module_path, func_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    description = excluded.description,
                    category = excluded.category,
                    schema_json = excluded.schema_json,
                    is_danger = excluded.is_danger,
                    is_active = excluded.is_active,
                    need_confirm = excluded.need_confirm,
                    params_desc_json = excluded.params_desc_json,
                    roles_json = excluded.roles_json,
                    module_path = excluded.module_path,
                    func_name = excluded.func_name,
                    updated_at = datetime('now', 'localtime')
                """,
                (
                    record["name"], record["description"], record["category"],
                    record["schema_json"], int(record["is_danger"]), int(record["is_active"]),
                    int(record["need_confirm"]), json.dumps(record.get("params_desc", {}), ensure_ascii=False),
                    json.dumps(roles, ensure_ascii=False),
                    record.get("module_path", ""), record.get("func_name", ""),
                ),
            )
            conn.commit()
            row = conn.execute("SELECT tool_id FROM tools WHERE name = ?", (record["name"],)).fetchone()
            return row["tool_id"]
        finally:
            conn.close()


def set_active(name: str, active: bool) -> bool:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE tools SET is_active = ?, updated_at = datetime('now','localtime') WHERE name = ?",
                (int(active), name),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def fetch_all(active_only: bool = True) -> list[dict]:
    conn = _connect()
    try:
        sql = "SELECT * FROM tools"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY tool_id"
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def fetch_by_name(name: str) -> Optional[dict]:
    conn = _connect()
    try:
        r = conn.execute("SELECT * FROM tools WHERE name = ?", (name,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def fetch_by_category(category: str, active_only: bool = True) -> list[dict]:
    conn = _connect()
    try:
        sql = "SELECT * FROM tools WHERE category = ?"
        if active_only:
            sql += " AND is_active = 1"
        sql += " ORDER BY tool_id"
        return [dict(r) for r in conn.execute(sql, (category,)).fetchall()]
    finally:
        conn.close()


def count() -> int:
    conn = _connect()
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM tools").fetchone()["c"]
    finally:
        conn.close()
