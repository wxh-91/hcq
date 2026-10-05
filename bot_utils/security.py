"""
bot_utils/security.py — 代码层安全约束

1. 统一权限检查（check_permission）
2. 全量审计日志（log_audit）

设计原则：
- 独立于 P0 身份提示词
- 权限检查在工具执行入口强制实施
- 审计日志记录每一次危险工具的执行意图与实际结果
"""
import json
import logging
from datetime import datetime
from typing import Tuple

logger = logging.getLogger(__name__)

# [large-task] 审计日志路径
AUDIT_LOG = "/root/robot/audit.log"

# [large-task] 危险工具：仅主管理员可执行
DANGER_TOOLS = {
    "execute_shell",
    "run_bash_script",
    "write_file",
    "replace_line",
    "delete_line",
    "insert_line",
    "clean_media",
    "add_admin",
    "del_admin",
}


def _sanitize_args(args) -> dict:
    """脱敏：截断过长内容，隐藏敏感字段"""
    if not isinstance(args, dict):
        return {"_raw": str(args)[:200]}
    sanitized = {}
    for k, v in args.items():
        if k in ("api_key", "password", "token", "secret"):
            sanitized[k] = "***"
        elif isinstance(v, str) and len(v) > 500:
            sanitized[k] = v[:500] + "...(截断)"
        else:
            sanitized[k] = v
    return sanitized


def log_audit(tool_name: str, args, sender_id: str, result: str,
              elapsed: float, status: str) -> None:
    """
    写审计日志。

    status 取值：
    - "success"：执行成功
    - "failed"：执行失败
    - "denied"：权限拒绝
    """
    try:
        record = {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "tool": tool_name,
            "args": _sanitize_args(args),
            "sender_id": str(sender_id),
            "status": status,
            "elapsed": round(elapsed, 3),
            "result_preview": (result or "")[:200],
        }
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"[audit] 写审计日志失败: {e}", exc_info=True)


def check_permission(tool_name: str, sender_id: str, default_admin: str) -> Tuple[bool, str]:
    """统一权限检查。返回 (allowed, reason)"""
    if tool_name in DANGER_TOOLS:
        if str(sender_id) != str(default_admin):
            return False, f"危险操作 {tool_name} 仅限主管理员执行"
    return True, ""
