# -*- coding: utf-8 -*-
"""进程内指标埋点（零外部依赖，环形 300 个秒桶 ≈ 5 分钟）。

跨进程可见性：每 5 秒把本进程窗口汇总以 JSON POST 到 8086 `/api/ingest`，
由聚合器在内存中按 pid 合并，超时 30 s 自动剔除。**不做任何磁盘写入**。

对外热路径：tool_ok / tool_fail / aup_reject / rate_limited（整数加法 + 一次标志判断）。
"""

import json
import os
import threading
import time
import urllib.request
from collections import deque

WINDOW_SEC = 300
REPORT_SEC = 5
REPORT_URL = "http://127.0.0.1:8086/api/ingest"

_lock = threading.Lock()
_buckets = deque(maxlen=WINDOW_SEC)
_last_sec = 0
_reporter_started = False
_extra: dict = {}


def set_extra(key: str, value) -> None:
    """附加实时字段（活跃工具队列 / Token 统计），随下一次上报带给 8086。"""
    with _lock:
        _extra[str(key)] = value


def clear_extra(key: str) -> None:
    with _lock:
        _extra.pop(str(key), None)


def _bucket(now_sec: int):
    global _last_sec
    if _last_sec == now_sec and _buckets:
        return _buckets[-1]
    b = {"t": now_sec, "ok": 0, "fail": 0, "aup": 0, "r429": 0,
         "lat_sum": 0.0, "lat_n": 0}
    _buckets.append(b)
    _last_sec = now_sec
    return b


# ---------- 上报（内存 → 8086，不走磁盘） ----------
def _report_once():
    s = snapshot()
    with _lock:
        extra = dict(_extra)
    payload = json.dumps({
        "pid": os.getpid(), "ts": time.time(),
        "ok": s["tool_ok"], "fail": s["tool_fail"],
        "aup": s["aup_reject"], "r429": s["rate_limited"],
        "lat_sum": s["lat_sum"], "lat_n": s["lat_n"],
        **extra,
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(REPORT_URL, data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=1.0).close()


def _report_loop():
    while True:
        time.sleep(REPORT_SEC)
        try:
            _report_once()
        except Exception:
            pass  # 8086 未起时静默，业务不受影响


def _ensure_reporter():
    global _reporter_started
    if _reporter_started:
        return
    _reporter_started = True
    threading.Thread(target=_report_loop, name="metrics-reporter", daemon=True).start()


# ---------- 埋点接口 ----------
def tool_ok(latency_ms: float = 0.0) -> None:
    now = int(time.time())
    with _lock:
        b = _bucket(now)
        b["ok"] += 1
        if latency_ms > 0:
            b["lat_sum"] += float(latency_ms)
            b["lat_n"] += 1
    _ensure_reporter()


def tool_fail(latency_ms: float = 0.0) -> None:
    now = int(time.time())
    with _lock:
        b = _bucket(now)
        b["fail"] += 1
        if latency_ms > 0:
            b["lat_sum"] += float(latency_ms)
            b["lat_n"] += 1
    _ensure_reporter()


def aup_reject() -> None:
    now = int(time.time())
    with _lock:
        _bucket(now)["aup"] += 1
    _ensure_reporter()


def rate_limited() -> None:
    now = int(time.time())
    with _lock:
        _bucket(now)["r429"] += 1
    _ensure_reporter()


def snapshot() -> dict:
    """本进程窗口汇总。"""
    now = int(time.time())
    with _lock:
        cut = now - WINDOW_SEC
        items = [b for b in _buckets if b["t"] > cut]
    ok = sum(b["ok"] for b in items)
    fail = sum(b["fail"] for b in items)
    aup = sum(b["aup"] for b in items)
    r429 = sum(b["r429"] for b in items)
    lat_sum = sum(b["lat_sum"] for b in items)
    lat_n = sum(b["lat_n"] for b in items)
    total = ok + fail
    return {
        "window_sec": WINDOW_SEC,
        "tool_ok": ok,
        "tool_fail": fail,
        "tool_total": total,
        "fail_rate": (fail / total) if total else 0.0,
        "success_rate": (ok / total) if total else 1.0,
        "aup_reject": aup,
        "rate_limited": r429,
        "avg_latency_ms": (lat_sum / lat_n) if lat_n else 0.0,
        "lat_sum": lat_sum,
        "lat_n": lat_n,
        "sampled_sec": len(items),
    }
