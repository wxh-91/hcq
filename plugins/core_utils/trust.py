# -*- coding: utf-8 -*-
"""HCQ-Core 高危工具信任边界（P0）。

信任判定优先级（仅作用于 DANGER_TOOLS）：
  1. 主管理员（QQ DEFAULT_ADMIN / 飞书 open_id）→ 放行；
  2. 可信本地来源（回环 127.0.0.1/::1，或 .env 中 TRUSTED_LOCAL_HOSTS 显式列出的地址）→ 机主，放行；
  3. 其他非可信来源（局域网 / 外部 / 经端口代理转发的 vSwitch 网关地址）→ 必须携带与 MOBILE_API_KEY 一致的移动端专属 Key；
  4. 其余一律拒绝。

vSwitch 默认网关地址背后可能挂着未经鉴权的局域网设备，因此不再自动视为机主；
旧的 test_sandbox_123_ 前缀旁路已废弃：会话前缀只用于记忆分桶，不再赋予任何高危权限。
HTTP 端点在进入流水线前用 set_request_context() 写入源 IP 与 Key，工具调度处读取。
"""
import contextvars
import hmac
import os

_request_ctx: "contextvars.ContextVar" = contextvars.ContextVar(
    "hcq_request_ctx", default=None
)
# 工具运行期上下文：由工具调度层写入发起本轮请求的 sender_id（会话键），
# 供后台 Worker 等脱离请求上下文的任务回推消息时定位归属会话。
_runtime_ctx: "contextvars.ContextVar" = contextvars.ContextVar(
    "hcq_runtime_ctx", default=None
)

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "0:0:0:0:0:0:0:1"}

# 允许 .env 追加额外可信本地地址（逗号分隔）
_TRUSTED_EXTRA = set()
for _h in os.getenv("TRUSTED_LOCAL_HOSTS", "").split(","):
    _h = _h.strip()
    if _h:
        _TRUSTED_EXTRA.add(_h)


def set_request_context(host: str = "", api_key: str = "", role: str = "", user_id: str = "") -> None:
    """由 HTTP 端点写入本次请求的源 IP、移动端 Key、角色与真实用户 ID。"""
    _request_ctx.set({"host": str(host or ""), "api_key": str(api_key or ""),
                      "role": str(role or ""), "user_id": str(user_id or ""),
                      "search_calls": 0})


def clear_request_context() -> None:
    """请求结束后清理请求上下文。"""
    _request_ctx.set(None)


def get_context() -> dict:
    return dict(_request_ctx.get() or {})


def consume_search_call(max_calls: int) -> tuple[bool, int]:
    """本轮请求内消费一次搜索额度。返回 (allowed, used)。"""
    ctx = _request_ctx.get()
    if ctx is None:
        return True, 0
    try:
        used = int(ctx.get("search_calls", 0) or 0)
    except Exception:
        used = 0
    if used >= int(max_calls):
        return False, used
    ctx["search_calls"] = used + 1
    return True, used + 1


def set_runtime(sender_id: str = "") -> None:
    """工具调度层写入本轮发起者的会话键（sender_id），供后台任务回推定位。"""
    _runtime_ctx.set({"sender_id": str(sender_id or "")})


def runtime_sender() -> str:
    """读取本轮发起者的会话键；脱离请求上下文时返回空串。"""
    return str((_runtime_ctx.get() or {}).get("sender_id", "") or "")


def clear_runtime() -> None:
    _runtime_ctx.set(None)


def is_loopback(host: str) -> bool:
    h = str(host or "").strip().strip("[]")
    return h in _LOOPBACK_HOSTS or h.startswith("127.")


def is_trusted_local(host: str) -> bool:
    """回环地址，或 .env 显式列出的额外可信本地地址。"""
    return is_loopback(host) or host in _TRUSTED_EXTRA


def mobile_api_key() -> str:
    return os.getenv("MOBILE_API_KEY", "") or ""


def verify_mobile_key(key: str) -> bool:
    """恒定时间比对移动端专属 Key；未配置期望 Key 时失败关闭（fail-closed）。"""
    expected = mobile_api_key()
    if not expected:
        return False
    return bool(key) and hmac.compare_digest(str(key), expected)


def danger_allowed(sender_id, main_admin: bool):
    """高危工具判定。返回 (allowed: bool, reason: str)。

    角色优先：ctx role=member 拒绝高危；role=admin 放行；
    未带角色时回退原有主管理员 / 可信本地 / 移动端 Key 判定（兼容旧通道）。
    """
    if main_admin:
        return True, "main_admin"
    ctx = get_context()
    role = str(ctx.get("role", "") or "").lower()
    if role == "member":
        return False, "member 角色被拒绝执行高危操作"
    if role == "admin":
        return True, "role_admin"
    host = ctx.get("host", "")
    if is_trusted_local(host):
        return True, "trusted_local"
    if verify_mobile_key(ctx.get("api_key", "")):
        return True, "mobile_key"
    return False, "非可信本地来源且缺少或无效移动端 API Key，高危操作被拒绝"
