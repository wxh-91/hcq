# -*- coding: utf-8 -*-
"""defense_center.policies —— 律法库（三级安全协议，代码写死）。

协议三（低危：违规探测）：连续 SSH 认证失败 / 高频非法路径访问。
    处置：记录日志，封禁 IP，无需汇报。
协议二（中危：系统异常）：CPU/内存 ≥95% 持续 5 分钟（疑似挖矿）/ 未知高危端口监听。
    处置：终止可疑进程，清理端口，向用户发送 QQ 警告。
协议一（高危：临界破坏）：核心文件被篡改 / 毁灭性命令（rm -rf /）/ 提示词注入致命越权。
    处置：切断外网连接，强制重启相关服务，立刻发送紧急求救 QQ 消息。

本模块纯数据定义，无任何副作用；watchdog/enforcer 均按需 import 使用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 协议等级（写死）
# ---------------------------------------------------------------------------
LEVEL_3 = 3   # 低危：违规探测
LEVEL_2 = 2   # 中危：系统异常
LEVEL_1 = 1   # 高危：临界破坏


@dataclass
class TriggerRule:
    """一条触发规则：正则命中即产出 SecurityEvent。"""
    rule_id: str                       # 规则编号（如 SSH_FAIL）
    level: int                         # 归属协议等级
    name: str                          # 人读规则名
    patterns: Tuple[str, ...]          # 命中正则（日志行级）
    count_threshold: int = 1           # 窗口内命中次数阈值（连续失败等）
    window_sec: int = 300              # 计数窗口（秒）
    extract_ip: bool = False           # 是否提取 IP 用于封禁
    ip_pattern: Optional[str] = None   # 提取 IP 的正则
    description: str = ""              # 规则说明
    actions: Tuple[str, ...] = ()      # 处置动作清单（见 enforcer.ACTIONS）
    notify: str = "none"               # none | warning | emergency


@dataclass
class SecurityEvent:
    """探针产出的安全事件（喂给 enforcer.enforce）。"""
    rule_id: str
    level: int
    message: str
    evidence: List[str] = field(default_factory=list)
    target_ip: Optional[str] = None
    source: str = ""                   # 探针来源（auth.log / proc / manual）
    ts: float = 0.0


# ---------------------------------------------------------------------------
# 律法表（写死）：规则 -> 协议
# ---------------------------------------------------------------------------
SSH_FAIL_IP_RE = r"from\s+([0-9a-fA-F.:]+)\s+port"

RULES: List[TriggerRule] = [
    # ---- 协议三：低危违规探测 ----
    TriggerRule(
        rule_id="SSH_FAIL",
        level=LEVEL_3,
        name="连续 SSH 认证失败",
        patterns=(r"Failed password",),
        count_threshold=5,
        window_sec=300,
        extract_ip=True,
        ip_pattern=SSH_FAIL_IP_RE,
        description="5 分钟窗口内 5 次以上 SSH 密码错误：疑似暴力破解",
        actions=("ban_ip",),
        notify="none",
    ),
    TriggerRule(
        rule_id="INVALID_USER",
        level=LEVEL_3,
        name="高频非法用户名访问",
        patterns=(r"Invalid user",),
        count_threshold=5,
        window_sec=300,
        extract_ip=True,
        ip_pattern=SSH_FAIL_IP_RE,
        description="非法用户名高频尝试：自动化扫描特征",
        actions=("ban_ip",),
        notify="none",
    ),
    # ---- 协议二：中危系统异常 ----
    TriggerRule(
        rule_id="HIGH_LOAD",
        level=LEVEL_2,
        name="CPU/内存持续高位（疑似挖矿）",
        patterns=(r"__proc__",),       # 由探针注入 CPU/MEM 指标文本
        count_threshold=1,
        window_sec=300,
        description="CPU 或内存占用 ≥95% 且持续 5 分钟：疑似挖矿/异常进程",
        actions=("kill_suspect", "clear_port"),
        notify="warning",
    ),
    TriggerRule(
        rule_id="SUSPECT_PORT",
        level=LEVEL_2,
        name="未知高危端口监听",
        patterns=(r"4444|31337|8333|9999",),
        count_threshold=1,
        window_sec=300,
        description="检测到已知挖矿回连/后门常用端口监听",
        actions=("clear_port",),
        notify="warning",
    ),
    # ---- 协议一：高危临界破坏 ----
    TriggerRule(
        rule_id="RM_RF_ROOT",
        level=LEVEL_1,
        name="毁灭性命令特征（rm -rf /）",
        patterns=(r"rm\s+-\s*rf\s*(\s*/\s*|/\s*\*)",),
        count_threshold=1,
        window_sec=300,
        description="检测到 rm -rf / 等毁灭性命令执行尝试",
        actions=("cut_network", "restart_service"),
        notify="emergency",
    ),
    TriggerRule(
        rule_id="FILE_TAMPER",
        level=LEVEL_1,
        name="核心文件被篡改",
        patterns=(r"__hash_mismatch__",),
        count_threshold=1,
        window_sec=300,
        description="核心系统文件哈希与基线不符",
        actions=("cut_network", "restart_service"),
        notify="emergency",
    ),
    TriggerRule(
        rule_id="PROMPT_INJECT",
        level=LEVEL_1,
        name="提示词注入致命越权",
        patterns=(r"ignore\s+(all\s+)?(previous|prior|above)|system\s*:\s*you\s+are|越权|绕过.*限制",),
        count_threshold=1,
        window_sec=300,
        description="检测到针对 AI 的提示词注入/致命越权特征",
        actions=("cut_network", "restart_service"),
        notify="emergency",
    ),
]

_RULE_INDEX = {r.rule_id: r for r in RULES}
PROTOCOL_NAMES = {
    LEVEL_3: "协议三·低危·违规探测",
    LEVEL_2: "协议二·中危·系统异常",
    LEVEL_1: "协议一·高危·临界破坏",
}
PROTOCOL_ACTIONS = {
    LEVEL_3: ("ban_ip",),
    LEVEL_2: ("kill_suspect", "clear_port"),
    LEVEL_1: ("cut_network", "restart_service"),
}
# 协议等级 -> 是否汇报用户（任务书：协议三无需汇报，中危警告，高危紧急求救）
PROTOCOL_NOTIFY = {
    LEVEL_3: "none",
    LEVEL_2: "warning",
    LEVEL_1: "emergency",
}


def get_rule(rule_id: str) -> Optional[TriggerRule]:
    return _RULE_INDEX.get(rule_id)


def match_line(line: str, rule: TriggerRule) -> Optional[re.Match]:
    """单行文本对单条规则做正则匹配。"""
    for pat in rule.patterns:
        m = re.search(pat, line, re.IGNORECASE)
        if m:
            return m
    return None


def extract_ip(line: str, rule: TriggerRule) -> Optional[str]:
    if not rule.extract_ip or not rule.ip_pattern:
        return None
    m = re.search(rule.ip_pattern, line)
    return m.group(1) if m else None


def describe_protocol(level: int) -> str:
    return PROTOCOL_NAMES.get(level, "未知协议")


def describe_actions(level: int) -> Tuple[str, ...]:
    return PROTOCOL_ACTIONS.get(level, ())
