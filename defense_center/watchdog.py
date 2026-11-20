# -*- coding: utf-8 -*-
"""defense_center.watchdog —— 环境探针（按需加载，极度轻量）。

设计：
- 平时只做两件事：增量读日志文件（记录 offset，不重读历史）、轻量读 /proc 指标。
- 只有命中 policies.RULES 时才产出 SecurityEvent 交给 enforcer —— 完整逻辑
  （AI 介入、QQ 推送、动作执行）全部延迟 import，绝不在空闲时拖慢主程序。
- 后台循环：asyncio.Task，每 interval 秒 scan_once() 一次；脱离 dz.py 的
  QQ 消息驱动，进程内自启动。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import List, Optional

from .policies import (
    RULES,
    SecurityEvent,
    extract_ip,
    match_line,
)

logger = logging.getLogger("defense.watchdog")

# 默认监控对象（可被注入覆盖）
DEFAULT_LOG_PATHS = ("/var/log/auth.log",)
MAX_LINE_LEN = 1024
_offsets: dict[str, int] = {}          # path -> 已读字节偏移（增量）
_scanned_at: Optional[float] = None


# ---------------------------------------------------------------------------
# 轻量指标（无第三方依赖，读 /proc）
# ---------------------------------------------------------------------------
def read_cpu_percent() -> Optional[float]:
    """读 /proc/stat 计算瞬时 CPU 使用率（两次采样间隔由调用方控制）。"""
    try:
        with open("/proc/stat", "r") as f:
            first = f.readline().split()
        time.sleep(0.2)
        with open("/proc/stat", "r") as f:
            second = f.readline().split()
        idle1 = int(first[4]); total1 = sum(int(x) for x in first[1:])
        idle2 = int(second[4]); total2 = sum(int(x) for x in second[1:])
        dt = total2 - total1
        if dt <= 0:
            return None
        return round(100.0 * (1 - (idle2 - idle1) / dt), 1)
    except Exception:
        return None


def read_mem_percent() -> Optional[float]:
    try:
        with open("/proc/meminfo", "r") as f:
            vals = {}
            for line in f:
                k, _, v = line.partition(":")
                vals[k] = int(v.split()[0])
        total = vals.get("MemTotal", 0)
        available = vals.get("MemAvailable", vals.get("MemFree", 0))
        if total <= 0:
            return None
        return round(100.0 * (1 - available / total), 1)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 增量日志读取
# ---------------------------------------------------------------------------
def read_new_lines(path: str, max_len: int = MAX_LINE_LEN) -> List[str]:
    """只读上次 offset 之后的新行（不重读历史）。"""
    try:
        p = Path(path)
        if not p.exists():
            return []
        size = p.stat().st_size
        start = _offsets.get(path, 0)
        if size < start:                # 日志被轮转/截断 -> 重置
            start = 0
        with open(path, "r", errors="replace") as f:
            if start:
                f.seek(start)
            data = f.read()
        _offsets[path] = size
        lines = data.splitlines()
        return [ln[:max_len] for ln in lines]
    except Exception as e:
        logger.debug("read %s failed: %s", path, e)
        return []


# ---------------------------------------------------------------------------
# 单次扫描（核心）
# ---------------------------------------------------------------------------
def scan_once(
    log_paths: tuple = DEFAULT_LOG_PATHS,
    cpu_hi: Optional[float] = None,
    mem_hi: Optional[float] = None,
    extra_lines: Optional[List[str]] = None,
) -> List[SecurityEvent]:
    """执行一轮轻量扫描，返回命中事件列表。

    cpu_hi/mem_hi：显式注入阈值时额外做指标检测（如 95.0 持续）。
    extra_lines：测试/手动触发可注入伪造日志行。
    """
    global _scanned_at
    events: List[SecurityEvent] = []
    window_lines: List[str] = []
    for path in log_paths:
        window_lines.extend(read_new_lines(path))
    if extra_lines:
        window_lines.extend(extra_lines)

    # 规则命中（含计数窗口语义简化：每轮窗口内计数）
    for rule in RULES:
        hits = 0
        evidences: List[str] = []
        ip_hits: dict[str, int] = {}
        for ln in window_lines:
            m = match_line(ln, rule)
            if not m:
                continue
            hits += 1
            if len(evidences) < 5:
                evidences.append(ln)
            if rule.extract_ip:
                ip = extract_ip(ln, rule)
                if ip:
                    ip_hits[ip] = ip_hits.get(ip, 0) + 1
        if hits >= rule.count_threshold:
            target_ip = None
            if rule.extract_ip and ip_hits:
                target_ip = max(ip_hits, key=ip_hits.get)
            events.append(
                SecurityEvent(
                    rule_id=rule.rule_id,
                    level=rule.level,
                    message="%s（命中 %d 次）" % (rule.description, hits),
                    evidence=evidences,
                    target_ip=target_ip,
                    source=",".join(log_paths),
                    ts=time.time(),
                )
            )

    # 指标检测（CPU/MEM ≥ 阈值 -> 协议二 HIGH_LOAD）
    if cpu_hi is not None or mem_hi is not None:
        cpu = read_cpu_percent()
        mem = read_mem_percent()
        flag = []
        if cpu_hi is not None and cpu is not None and cpu >= cpu_hi:
            flag.append("CPU=%.1f%%" % cpu)
        if mem_hi is not None and mem is not None and mem >= mem_hi:
            flag.append("MEM=%.1f%%" % mem)
        if flag:
            events.append(
                SecurityEvent(
                    rule_id="HIGH_LOAD",
                    level=2,
                    message="系统资源持续高位：%s" % ", ".join(flag),
                    evidence=flag,
                    source="proc",
                    ts=time.time(),
                )
            )
    _scanned_at = time.time()
    return events


# ---------------------------------------------------------------------------
# 后台循环（脱离 QQ 消息驱动，asyncio.Task）
# ---------------------------------------------------------------------------
from config_center import get_config


class Watchdog:
    """后台循环探针：start() 后在独立 Task 中每 interval 秒扫描一次。

    空闲时零开销（只读日志尾部）；命中才完整处理（延迟 import enforcer）。
    """

    def __init__(self, interval: float | None = None, log_paths: tuple = DEFAULT_LOG_PATHS,
                 cpu_hi: Optional[float] = 95.0, mem_hi: Optional[float] = 95.0):
        if interval is None:
            interval = get_config("DEFENSE_WATCHDOG_INTERVAL", 30)
        self.interval = interval
        self.log_paths = log_paths
        self.cpu_hi = cpu_hi
        self.mem_hi = mem_hi
        self._task: Optional[asyncio.Task] = None
        self._enforcer = None

    async def _loop(self) -> None:
        while True:
            try:
                events = await asyncio.to_thread(
                    scan_once, self.log_paths, self.cpu_hi, self.mem_hi
                )
                for ev in events:
                    if self._enforcer is None:
                        from .enforcer import enforcer   # 延迟加载：空闲不 import
                        self._enforcer = enforcer
                    await self._enforcer.handle(ev)
            except Exception as e:
                logger.warning("watchdog scan error: %s", e)
            self.interval = get_config("DEFENSE_WATCHDOG_INTERVAL", self.interval)
            await asyncio.sleep(self.interval)

    def start(self) -> asyncio.Task:
        """启动后台循环（幂等：已启动则直接返回现有 Task）。"""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop())
        return self._task

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


def default_watchdog() -> Watchdog:
    return Watchdog()
