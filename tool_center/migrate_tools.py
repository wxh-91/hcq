"""一次性迁移：把 admin.py 中存量工具元数据抽离并写入 SQLite。

执行方式（root）：
    cd /root/robot && /root/robot/venv310/bin/python -m tool_center.migrate_tools
"""
from __future__ import annotations
import os
_ROOT = os.environ.get("PROJECT_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


import importlib
import json
import os
import sys

sys.path.insert(0, _ROOT)

from tool_center.registry_manager import register_tool  # noqa: E402


# 按工具语义归类（file / network / system / media / knowledge / admin）
CATEGORY_MAP = {
    "read_file": "file", "write_file": "file", "replace_line": "file",
    "delete_line": "file", "insert_line": "file", "backup_project": "file",
    "execute_shell": "system", "system_status": "system",
    "run_bash_script": "system", "get_xmkj": "system",
    "ping_host": "network", "download_m3u8": "network",
    "crawl_video_link": "network", "crawler_run_node": "network",
    "query_knowledge": "knowledge", "recall_conversation": "knowledge",
    "add_admin": "admin", "del_admin": "admin", "list_admin": "admin",
    "set_group_chat_enabled": "admin", "get_group_chat_status": "admin",
    "agent_manager": "admin", "checkin": "admin",
    "clean_media": "media",
}


def _first_doc_line(func) -> str:
    doc = func.__doc__
    if doc:
        return doc.strip().split("\n")[0]
    return ""


def main() -> int:
    admin = importlib.import_module("admin")
    tools = admin.TOOLS
    total = len(tools)
    print(f"[migrate] 读取 admin.TOOLS，共 {total} 个工具")
    ok = 0
    for name, meta in tools.items():
        func = meta["func"]
        desc = _first_doc_line(func)
        if not desc:
            desc = admin._get_tool_default_desc(name)
        schema = admin.build_tool_schema(name, func, meta.get("params_desc", {}))
        schema_str = json.dumps(schema, ensure_ascii=False)
        category = CATEGORY_MAP.get(name, "general")
        tid = register_tool(
            name=name,
            func=func,
            description=desc,
            category=category,
            schema_json=schema_str,
            is_danger=bool(meta.get("is_danger", False)),
            is_active=True,
            need_confirm=bool(meta.get("need_confirm", False)),
            params_desc=meta.get("params_desc", {}),
            module_path=func.__module__,
            func_name=func.__name__,
        )
        ok += 1
        print(
            f"[migrate] tool_id={tid:>2} name={name:<26} "
            f"category={category:<9} danger={meta.get('is_danger', False)} "
            f"desc={desc[:28]}"
        )
    print(f"[migrate] 完成：{ok}/{total} 个工具写入 tool_center.db")
    return 0


if __name__ == "__main__":
    sys.exit(main())
