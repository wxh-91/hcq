# -*- coding: utf-8 -*-
"""defense_center.enforcer —— 执行器（先斩后奏）。

不经过用户的聊天请求，直接按协议等级处置：
  - 协议三：记录日志 + 封禁 IP（无需汇报）
  - 协议二：终止可疑进程 + 清理端口 + QQ 警告
  - 协议一：切断外网 + 强制重启服务 + 紧急求救 QQ

安全双模式：
  - DRY-RUN（默认）：所有处置动作记录为 SecurityAction 审计日志，不真执行
    （杀进程/断网/重启在测试与演示中绝不误伤真实系统）。
  - REAL（DEFENSE_REAL=1 显式开启）：真实执行动作，通过 admin.execute_tool_directly
    走完整权限校验 + 审计链路；AI 介入通过 ai.safe_api_request 生成阻断命令。

QQ 推送为可注入 notifier：生产由 nonebot Bot 注入（bot.send_private_msg），
测试/演示注入 stub notifier 收集消息产物。
"""
from __future__ import annotations
import os
_ROOT = os.environ.get("PROJECT_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from .policies import (
    PROTOCOL_ACTIONS,
    PROTOCOL_NOTIFY,
    SecurityEvent,
    describe_protocol,
)


logger = logging.getLogger("defense.enforcer")

REAL_MODE = os.environ.get("DEFENSE_REAL", "").strip() == "1"

AUDIT_DIR = Path(_ROOT + "/defense_center")
AUDIT_LOG = AUDIT_DIR / "defense_audit.log"


@dataclass
class SecurityAction:
    """一次处置动作的记录（审计用）。"""
    ts: float
    rule_id: str
    level: int
    action: str
    params: dict = field(default_factory=dict)
    executed: bool = False          # REAL 模式真实执行标记
    result: str = ""


class DefenseAudit:
    """处置审计：动作与决策全量落盘（defense_center 自有日志，不动生产文件）。"""

    @staticmethod
    def record(entry: dict) -> None:
        try:
            AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            line = "[%s] %s" % (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.get("ts", time.time()))),
                entry,
            )
            with open(AUDIT_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            logger.warning("audit write failed: %s", e)


# ---------------------------------------------------------------------------
# 动作执行体（DRY-RUN 默认；REAL 走 admin.execute_tool_directly 完整链路）
# ---------------------------------------------------------------------------
async def _exec_via_admin(tool_name: str, args: dict) -> str:
    """REAL 模式：调用 admin.execute_tool_directly（含权限校验+审计）。

    关键安全线：真实执行身份严格绑定主管理员 DEFAULT_ADMIN——
    execute_tool_directly 内部 check_permission 会校验该身份，
    非管理员身份（或防御中枢未挂载）将无法执行任何 REAL 动作。
    """
    try:
        from cl import DEFAULT_ADMIN
        admin_qq = str(DEFAULT_ADMIN)
    except Exception:
        admin_qq = "1556998894"   # 兜底：cl.DEFAULT_ADMIN
    from import_center import get_service
    admin = get_service("admin") if get_service("admin") else None
    if admin is None:
        import admin as _admin
        return await _admin.execute_tool_directly(tool_name, args, admin_qq)
    fn = getattr(admin, "execute_tool_directly", None)
    if fn is None:
        return "admin 服务无 execute_tool_directly"
    return await fn(tool_name, args, admin_qq)


# 动作清单：action -> 执行体（返回人读结果字符串）
def _build_action_impls(dry_run: bool):
    if dry_run:
        def _stub(action, params):
            return "[DRY-RUN] 已拦截动作 %s（未真实执行）参数=%s" % (action, params)
        return {a: _stub for a in ("ban_ip", "kill_suspect", "clear_port",
                                   "cut_network", "restart_service")}
    async def ban_ip(params):
        return await _exec_via_admin("execute_shell", {"cmd": "iptables -A INPUT -s %s -j DROP" % params.get("ip", "")})
    async def kill_suspect(params):
        return await _exec_via_admin("execute_shell", {"cmd": "pkill -9 %s" % params.get("pattern", "")})
    async def clear_port(params):
        return await _exec_via_admin("execute_shell", {"cmd": "fuser -k %s/tcp 2>/dev/null" % params.get("port", "")})
    async def cut_network(params):
        return await _exec_via_admin("execute_shell", {"cmd": "ip link set %s down" % params.get("iface", "eth0")})
    async def restart_service(params):
        return await _exec_via_admin("execute_shell", {"cmd": "systemctl restart %s" % params.get("service", "docker")})
    return {"ban_ip": ban_ip, "kill_suspect": kill_suspect, "clear_port": clear_port,
            "cut_network": cut_network, "restart_service": restart_service}


# ---------------------------------------------------------------------------
# AI 介入：复杂异常 -> 唤醒大模型生成阻断命令（safe_api_request）
# ---------------------------------------------------------------------------
async def ai_intervene(event: SecurityEvent) -> str:
    """打包系统状态，请求大模型生成阻断命令。

    Prompt 固定为任务书模板；DRY-RUN 模式仍真实调用（只生成建议，不执行）。
    失败时返回兜底建议，绝不阻塞处置主链路。
    """
    from . import enforcer   # noqa: F401  保证模块初始化
    try:
        from ai import safe_api_request
        prompt = (
            "系统正遭遇异常：[%s] %s（证据：%s）。"
            "根据安全协议，请立刻生成用于阻断攻击的 Shell 命令（仅输出命令本身，"
            "不要解释；命令必须是无害、幂等、可回滚的阻断操作）。"
            % (event.rule_id, event.message, " | ".join(event.evidence[:3]))
        )
        text, _ = await safe_api_request(
            [{"role": "user", "content": prompt}],
            strategy="tool", timeout=30,
        )
        return (text or "").strip()
    except Exception as e:
        return "# AI 介入失败(%s)；按预设动作处置" % type(e).__name__


# ---------------------------------------------------------------------------
# QQ 推送（可注入 notifier）
# ---------------------------------------------------------------------------
class QQNotifier:
    """QQ 报警通道：生产注入 nonebot Bot；测试注入 stub。"""

    def __init__(self):
        self._sender: Optional[Callable] = None
        self._admin_qq: Optional[int] = None

    def attach(self, sender: Callable, admin_qq: int) -> None:
        """生产绑定：sender = lambda text: bot.send_private_msg(user_id=admin_qq, message=text)"""
        self._sender = sender
        self._admin_qq = admin_qq

    async def send(self, text: str) -> str:
        if self._sender is None:
            # [R11-local-fallback] 未注入通道（飞书关闭）时落本地 outbox，报警不丢
            try:
                from plugins.core_utils.notify_bridge import notify

                await notify(text, title="防御报警", level="alert")
                return "[LOCAL-SENT] %s" % text
            except Exception:
                return "[QQ-UNBOUND] 未注入 notifier，报警消息已生成：%s" % text
        try:
            await self._sender(text)
            return "[QQ-SENT] %s" % text
        except Exception as e:
            return "[QQ-FAIL] %s 推送失败: %s" % (text, type(e).__name__)


qq_notifier = QQNotifier()


class Enforcer:
    """执行器：事件 -> 处置路由（先斩后奏）。"""

    def __init__(self, dry_run: Optional[bool] = None):
        # 默认 DRY-RUN（安全优先）；仅 DEFENSE_REAL=1 环境显式开启真实执行
        self.dry_run = (not REAL_MODE) if dry_run is None else dry_run
        self.actions = _build_action_impls(self.dry_run)
        self.history: List[SecurityAction] = []
        self._ai_advice = ai_intervene   # 可注入：测试替换为 stub，不触发真实大模型
        self._audit = DefenseAudit.record  # 可注入：测试替换为收集器

    async def handle(self, event: SecurityEvent) -> List[SecurityAction]:
        """处置入口：按协议等级路由动作 + 按等级决定汇报方式。"""
        performed: List[SecurityAction] = []
        actions = PROTOCOL_ACTIONS.get(event.level, ())
        for act in actions:
            params = self._params_for(event, act)
            impl = self.actions[act]
            if self.dry_run:
                result = impl(act, params)
            else:
                result = await impl(params)
            sa = SecurityAction(
                ts=time.time(), rule_id=event.rule_id, level=event.level,
                action=act, params=params, executed=not self.dry_run, result=str(result),
            )
            performed.append(sa)
            self._audit({
                "event": event.message, "level": event.level, "rule_id": event.rule_id,
                "action": act, "params": params, "executed": sa.executed, "result": str(result),
            })

        # 汇报路由（任务书：低危无需汇报 / 中危警告 / 高危紧急求救）
        notify_level = PROTOCOL_NOTIFY.get(event.level, "none")
        if notify_level != "none":
            await self._notify(event, notify_level, performed)
        self.history.extend(performed)   # 处置历史（供审计/断言/复盘）
        return performed

    def _params_for(self, event: SecurityEvent, action: str) -> dict:
        if action == "ban_ip":
            return {"ip": event.target_ip or "0.0.0.0"}
        if action == "kill_suspect":
            return {"pattern": "minerd|xmrig|cryptonight"}
        if action == "clear_port":
            return {"port": "4444"}
        if action == "cut_network":
            return {"iface": "eth0"}
        if action == "restart_service":
            return {"service": "docker"}
        return {}

    async def _notify(self, event: SecurityEvent, kind: str, performed: List[SecurityAction]) -> None:
        head = "【防御中枢·紧急求救】" if kind == "emergency" else "【防御中枢·安全警告】"
        lines = [
            "%s %s" % (head, describe_protocol(event.level)),
            "事件：%s" % event.message,
            "规则：%s" % event.rule_id,
            "证据：%s" % " | ".join(event.evidence[:3]),
            "已执行动作：%s" % ", ".join(sa.action for sa in performed),
        ]
        if event.target_ip:
            lines.append("来源IP：%s" % event.target_ip)
        # 高危：附带 AI 介入建议
        if event.level == 1:
            advice = await self._ai_advice(event)
            lines.append("AI 阻断建议：%s" % advice)
        await qq_notifier.send("\n".join(lines))


enforcer = Enforcer()


async def manual_trigger(rule_id: str, evidence_lines: Optional[List[str]] = None) -> List[SecurityAction]:
    """手动触发通道：直接按规则构造 SecurityEvent 并处置（供测试/演示）。"""
    from .policies import get_rule
    rule = get_rule(rule_id)
    if rule is None:
        raise ValueError("未知规则: %s" % rule_id)
    ev = SecurityEvent(
        rule_id=rule.rule_id,
        level=rule.level,
        message="%s（手动触发）" % rule.description,
        evidence=evidence_lines or ["[manual] %s" % rule.name],
        target_ip="203.0.113.66",
        source="manual",
        ts=time.time(),
    )
    return await enforcer.handle(ev)
