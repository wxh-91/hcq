"""
bot_utils/security.py — 代码层安全约束

1. 统一权限检查（check_permission）
2. 全量审计日志（log_audit）

设计原则：
- 独立于 P0 身份提示词
- 权限检查在工具执行入口强制实施
- 审计日志记录每一次危险工具的执行意图与实际结果
"""
import os
_ROOT = os.environ.get("PROJECT_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import logging
from datetime import datetime
from typing import Tuple
from cl import FEISHU_ADMIN_OPEN_ID, is_main_admin
from plugins.core_utils.trust import (
    consume_search_call, danger_allowed, get_context, is_trusted_local, verify_mobile_key,
)
import os


logger = logging.getLogger(__name__)

# [large-task] 审计日志路径
AUDIT_LOG = _ROOT + "/audit.log"

# [large-task] 危险工具：仅主管理员 / 本机回环 / 已鉴权移动端可执行
SEARCH_TOOL_NAMES = {"lc_web_search", "web_search", "search_and_read"}
MEMBER_SEARCH_LIMIT = 1

DANGER_TOOLS = {
    "execute_shell",
    "aup_execute_shell",
    "run_bash_script",
    "run_shell",
    "write_file",
    "replace_line",
    "delete_line",
    "insert_line",
    "clean_media",
    "add_admin",
    "del_admin",
    "win_shell",
    "win_ui_automation",
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
    """统一权限检查。返回 (allowed, reason)。

    高危工具放行身份（P0 信任边界，前缀旁路已废弃）：
    1. 远程主管理员：QQ DEFAULT_ADMIN / 飞书 FEISHU_ADMIN_OPEN_ID（含 crc32 映射）；
    2. 本机回环请求：操作者即机主；
    3. 非回环请求：必须携带与 MOBILE_API_KEY 一致的移动端专属 Key。
    会话带 test_sandbox_123_ 前缀不再赋予任何高危权限。
    """
    if tool_name in DANGER_TOOLS:
        allowed, reason = danger_allowed(sender_id, is_main_admin(sender_id))
        if allowed:
            return True, ""
        return False, reason or f"危险操作 {tool_name} 仅限主管理员 / 本机回环 / 已鉴权移动端执行"
    return True, ""


def check_tool_roles(tool_name: str, roles, sender_id: str) -> Tuple[bool, str]:
    """工具级角色白名单。roles 为空表示沿用历史全角色行为。"""
    allowed_roles = {str(r).strip().lower() for r in (roles or []) if str(r).strip()}
    if not allowed_roles:
        return True, ""
    ctx = get_context()
    role = str(ctx.get("role") or "").strip().lower()
    if role in ("admin", "member"):
        if role in allowed_roles:
            return True, ""
        return False, (
            "角色 %s 无权调用工具 %s（允许: %s）"
            % (role, tool_name, ",".join(sorted(allowed_roles)))
        )
    if role:
        return False, "未知角色 %s，无权调用工具 %s" % (role, tool_name)
    # 兼容旧通道：无角色时只按既有可信边界推导为 admin
    if is_main_admin(sender_id) or is_trusted_local(ctx.get("host", "")) or verify_mobile_key(ctx.get("api_key", "")):
        if "admin" in allowed_roles:
            return True, ""
        return False, "admin 身份无权调用工具 %s" % tool_name
    return False, "未识别身份，无权调用工具 %s" % tool_name


def check_search_budget(tool_name: str, sender_id: str) -> Tuple[bool, str]:
    """Member 单轮搜索预算：最多 2 次；第 3 次直接拒绝，逼模型基于已有结果回复。"""
    if tool_name not in SEARCH_TOOL_NAMES:
        return True, ""
    role = str(get_context().get("role") or "").strip().lower()
    if role != "member":
        return True, ""
    allowed, used = consume_search_call(MEMBER_SEARCH_LIMIT)
    if allowed:
        return True, ""
    return False, (
        "本轮搜索次数已达上限（%d 次），请立即基于已有搜索结果生成最终回复，"
        "禁止继续换关键词或换工具搜索" % MEMBER_SEARCH_LIMIT
    )
