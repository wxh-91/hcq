# -*- coding: utf-8 -*-
"""defense_center —— 主动防御中枢（对用户侧 AI 静默的底层安全机制）。

只负责"看"（watchdog 探针）与"救火"（enforcer 执行器），
律法写死于 policies；三者均按需加载，空闲零开销。
"""
from __future__ import annotations

from .policies import (
    LEVEL_1,
    LEVEL_2,
    LEVEL_3,
    PROTOCOL_ACTIONS,
    PROTOCOL_NAMES,
    PROTOCOL_NOTIFY,
    RULES,
    SecurityEvent,
    TriggerRule,
    describe_protocol,
    get_rule,
    match_line,
)
from .watchdog import Watchdog, default_watchdog, scan_once
from .enforcer import (
    Enforcer,
    SecurityAction,
    DefenseAudit,
    ai_intervene,
    enforcer,
    manual_trigger,
    qq_notifier,
)

__all__ = [
    "LEVEL_1", "LEVEL_2", "LEVEL_3",
    "PROTOCOL_ACTIONS", "PROTOCOL_NAMES", "PROTOCOL_NOTIFY",
    "RULES", "SecurityEvent", "TriggerRule",
    "describe_protocol", "get_rule", "match_line",
    "Watchdog", "default_watchdog", "scan_once",
    "Enforcer", "SecurityAction", "DefenseAudit",
    "ai_intervene", "enforcer", "manual_trigger", "qq_notifier",
]
