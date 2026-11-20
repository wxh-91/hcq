# -*- coding: utf-8 -*-
"""Top 进程采样器（psutil，2 秒一轮，供 8086 /api/top）。

单例后台线程；首次调用 start() 后维护 cpu_pct / mem_pct 缓存，
端点读取缓存，不阻塞 HTTP 线程。
"""
from __future__ import annotations

import threading
import time

try:
    import psutil
except Exception:  # psutil 缺失时降级为空
    psutil = None

_INTERVAL = 2.0
_TOP_N = 3
_lock = threading.Lock()
_state = {"ts": 0.0, "cpu": [], "mem": [], "total_mem": 0}
_started = False
_procs: dict[int, "psutil.Process"] = {}

# 黑名单：测试脚本与抓取/统计类工具不参与 Top3 排名，避免污染真实负载
_IGNORE_NAMES = {
    "hcq_load.sh", "hcq_load", "load_test.sh", "stress", "stress-ng",
    "ps", "top", "htop", "atop", "pgrep", "pkill", "killall",
    "grep", "egrep", "fgrep", "awk", "gawk", "sed", "cut", "sort", "uniq",
    "head", "tail", "wc", "xargs", "find", "ss", "lsof", "netstat", "watch",
}
_IGNORE_CMD_HINTS = ("hcq_load", "/tmp/hcq_load", "ps -eo", "ps aux", "ps -ef",
                     "top -", "grep ", "pgrep ", "pkill ", "lsof ", "netstat ")


def _is_ignored(name: str, cmdline: str) -> bool:
    n = (name or "").strip().lower()
    if n in _IGNORE_NAMES:
        return True
    c = (cmdline or "").lower()
    return any(h in c for h in _IGNORE_CMD_HINTS)


def _refresh_proc_set():
    alive = set()
    for p in psutil.process_iter():
        try:
            if p.pid not in _procs:
                _procs[p.pid] = p
                try:
                    p.cpu_percent(None)   # 新进程先建基线，下一轮才有非零值
                except Exception:
                    pass
            alive.add(p.pid)
        except Exception:
            continue
    for pid in list(_procs):
        if pid not in alive:
            _procs.pop(pid, None)


def _sample():
    if psutil is None:
        return
    _refresh_proc_set()
    rows = []
    for pid, p in list(_procs.items()):
        try:
            with p.oneshot():
                name = p.name()
                cmd = " ".join(p.cmdline()[:6])[:180]
                cpu = p.cpu_percent(None)
                mem = p.memory_percent()
                rss = p.memory_info().rss
        except Exception:
            continue
        if _is_ignored(name, cmd):
            continue
        rows.append({"pid": pid, "name": name, "cpu": round(float(cpu), 1),
                     "mem": round(float(mem), 1), "rss": int(rss)})
    vm_total = 0
    try:
        vm_total = int(psutil.virtual_memory().total)
    except Exception:
        pass
    top_cpu = sorted(rows, key=lambda r: r["cpu"], reverse=True)[:_TOP_N]
    top_mem = sorted(rows, key=lambda r: r["mem"], reverse=True)[:_TOP_N]
    with _lock:
        _state.update({"ts": time.time(), "cpu": top_cpu, "mem": top_mem, "total_mem": vm_total})


def _loop():
    # 先空跑一轮给 cpu_percent 建立基线
    try:
        _refresh_proc_set()
        for p in _procs.values():
            try:
                p.cpu_percent(None)
            except Exception:
                pass
        time.sleep(0.5)
    except Exception:
        pass
    while True:
        try:
            _sample()
        except Exception:
            pass
        time.sleep(_INTERVAL)


def start():
    global _started
    if psutil is None or _started:
        return
    _started = True
    t = threading.Thread(target=_loop, name="top-proc", daemon=True)
    t.start()


def snapshot() -> dict:
    with _lock:
        return {"ts": _state["ts"], "cpu": list(_state["cpu"]),
                "mem": list(_state["mem"]), "total_mem": _state["total_mem"]}
