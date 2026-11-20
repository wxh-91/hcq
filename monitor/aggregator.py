# -*- coding: utf-8 -*-
"""健康分数聚合器：每 5 秒按业务埋点 + 本机系统指标算 0-100 分。

业务埋点来源：各进程每 5 秒 POST 到 8086 `/api/ingest`，本模块在**内存**里按 pid
保存最新窗口汇总（30 s 未更新即剔除），不再读写任何磁盘文件。

打分规则（起点 100，逐项扣分，下限 0）：
- 工具失败率 > 10% 扣 20；> 25% 再扣 15（合计 -35）
- 出现 429 限流 扣 30
- AUP 拒绝 > 5 次 扣 15；> 20 次 再扣 10
- 平均延迟 > 3s 扣 10；> 8s 再扣 10
- CPU > 85% 扣 10；> 95% 再扣 5
- 磁盘剩余 < 20% 扣 10；< 10% 再扣 10
- 可用内存 < 10% 扣 10
状态：>=85 运行平稳 / 60-84 需要留意 / <60 存在风险
"""

import shutil
import threading
import time

try:  # 既支持包导入（monitor.aggregator），也支持同目录脚本直跑
    from . import metrics as _m
except ImportError:  # pragma: no cover
    import metrics as _m

INTERVAL_SEC = 5
PEER_TTL_SEC = 30          # 5 s 上报周期，容忍 6 次丢报

# 可重入锁：start() 持锁内会调用 compute()，而 compute() 又会访问 _peers（同一把锁）
_lock = threading.RLock()
_cache = {
    "score": 100,
    "status": "ok",
    "label": "运行平稳",
    "messages": ["监控启动中"],
    "ts": 0,
    "details": {},
}
_peers = {}                # pid -> {"ts","ok","fail","aup","r429","lat_sum","lat_n"}
_prev_cpu = {"idle": 0, "total": 0}
_thread = None
_stop = threading.Event()


# ---------- 外部进程上报（内存合并，无磁盘） ----------
def ingest_peer(payload: dict) -> None:
    """接收某个进程的窗口汇总（覆盖式，保留最新值）。"""
    try:
        pid = int(payload.get("pid") or 0)
    except Exception:
        return
    if pid <= 0:
        return
    rec = {
        "ts": time.time(),
        "ok": int(payload.get("ok") or 0),
        "fail": int(payload.get("fail") or 0),
        "aup": int(payload.get("aup") or 0),
        "r429": int(payload.get("r429") or 0),
        "lat_sum": float(payload.get("lat_sum") or 0.0),
        "lat_n": int(payload.get("lat_n") or 0),
        "active_tools": payload.get("active_tools") if isinstance(payload.get("active_tools"), list) else [],
        "tokens": payload.get("tokens") if isinstance(payload.get("tokens"), dict) else {},
    }
    with _lock:
        _peers[pid] = rec


def peer_summary() -> dict:
    """汇总所有存活进程的窗口值，剔除超时进程。"""
    now = time.time()
    total = {"ok": 0, "fail": 0, "aup": 0, "r429": 0, "lat_sum": 0.0, "lat_n": 0,
             "peers": 0, "active_tools": [], "token_total": 0, "token_prompt": 0,
             "token_completion": 0, "token_requests": 0, "token_rate_per_min": 0.0,
             "token_ts": 0.0, "token_model": ""}
    with _lock:
        dead = [p for p, r in _peers.items() if now - r["ts"] > PEER_TTL_SEC]
        for p in dead:
            _peers.pop(p, None)
        for p, r in _peers.items():
            total["ok"] += r["ok"]
            total["fail"] += r["fail"]
            total["aup"] += r["aup"]
            total["r429"] += r["r429"]
            total["lat_sum"] += r["lat_sum"]
            total["lat_n"] += r["lat_n"]
            total["peers"] += 1
            for item in r.get("active_tools") or []:
                if not isinstance(item, dict):
                    continue
                tool = str(item.get("tool") or item.get("name") or "").strip()
                if not tool:
                    continue
                total["active_tools"].append({
                    "tool": tool,
                    "pid": p,
                    "started_at": item.get("started_at") or "",
                    "elapsed_sec": item.get("elapsed_sec"),
                    "sender": item.get("sender") or "",
                })
            tok = r.get("tokens") or {}
            if isinstance(tok, dict) and r["ts"] >= total["token_ts"]:
                total["token_ts"] = r["ts"]
                total["token_total"] = int(tok.get("total") or 0)
                total["token_prompt"] = int(tok.get("prompt") or 0)
                total["token_completion"] = int(tok.get("completion") or 0)
                total["token_requests"] = int(tok.get("requests") or 0)
                total["token_rate_per_min"] = float(tok.get("rate_per_min") or 0.0)
                total["token_model"] = str(tok.get("model") or "")
    total["active_tools"] = total["active_tools"][:20]
    return total


def _read_cpu():
    """返回 (cpu_used_pct, load1)。基于 /proc/stat 差分。"""
    try:
        with open("/proc/stat", "rb") as f:
            fields = f.readline().split()
        vals = [int(x) for x in fields[1:11]]
        idle = vals[3] + vals[4]        # idle + iowait
        total = sum(vals)
        d_idle = idle - _prev_cpu["idle"]
        d_total = total - _prev_cpu["total"]
        _prev_cpu["idle"], _prev_cpu["total"] = idle, total
        pct = (100.0 * (d_total - d_idle) / d_total) if d_total > 0 else 0.0
        load = float(open("/proc/loadavg").read().split()[0])
        return max(0.0, min(100.0, pct)), load
    except Exception:
        return 0.0, 0.0


def _read_mem():
    """返回 (可用内存百分比, 已用百分比)。"""
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k.strip()] = int(v.strip().split()[0])
        total = info.get("MemTotal", 0) or 1
        avail = info.get("MemAvailable", 0)
        return 100.0 * avail / total, 100.0 * (1 - avail / total)
    except Exception:
        return 0.0, 0.0


def _read_disk():
    try:
        u = shutil.disk_usage("/")
        return 100.0 * u.free / u.total
    except Exception:
        return 100.0


def compute() -> dict:
    shared = peer_summary()
    if shared.get("peers"):
        ok, fail = shared["ok"], shared["fail"]
        total = ok + fail
        snap = {
            "tool_ok": ok, "tool_fail": fail, "tool_total": total,
            "fail_rate": (fail / total) if total else 0.0,
            "success_rate": (ok / total) if total else 1.0,
            "aup_reject": shared["aup"], "rate_limited": shared["r429"],
            "avg_latency_ms": (shared["lat_sum"] / shared["lat_n"]) if shared["lat_n"] else 0.0,
        }
    else:
        snap = _m.snapshot()
    cpu_pct, load1 = _read_cpu()
    mem_free_pct, _ = _read_mem()
    disk_free_pct = _read_disk()

    score = 100
    msgs = []

    fr = snap["fail_rate"]
    if fr > 0.25:
        score -= 35
        msgs.append("工具失败率 %.0f%%，明显偏高" % (fr * 100))
    elif fr > 0.10:
        score -= 20
        msgs.append("工具失败率 %.0f%%，需要关注" % (fr * 100))
    else:
        msgs.append("工具调用正常（成功率 %.0f%%）" % (snap["success_rate"] * 100))

    if snap["rate_limited"] > 0:
        score -= 30
        msgs.append("近 5 分钟出现 %d 次 429 限流" % snap["rate_limited"])

    if snap["aup_reject"] > 20:
        score -= 25
        msgs.append("AUP 拦截 %d 次，疑似异常调用" % snap["aup_reject"])
    elif snap["aup_reject"] > 5:
        score -= 15
        msgs.append("AUP 拦截 %d 次" % snap["aup_reject"])

    lat = snap["avg_latency_ms"]
    if lat > 8000:
        score -= 20
        msgs.append("平均延迟 %.1fs，过高" % (lat / 1000))
    elif lat > 3000:
        score -= 10
        msgs.append("平均延迟 %.1fs" % (lat / 1000))

    if cpu_pct > 95:
        score -= 15
        msgs.append("CPU %.0f%%，接近饱和" % cpu_pct)
    elif cpu_pct > 85:
        score -= 10
        msgs.append("CPU %.0f%%，偏高" % cpu_pct)

    if disk_free_pct < 10:
        score -= 20
        msgs.append("磁盘剩余 %.1f%%，空间紧张" % disk_free_pct)
    elif disk_free_pct < 20:
        score -= 10
        msgs.append("磁盘剩余 %.1f%%" % disk_free_pct)

    if mem_free_pct < 10:
        score -= 10
        msgs.append("可用内存仅 %.1f%%" % mem_free_pct)

    score = int(max(0, min(100, score)))
    if score >= 85:
        status, label = "ok", "运行平稳"
    elif score >= 60:
        status, label = "warn", "需要留意"
    else:
        status, label = "alert", "存在风险"

    return {
        "score": score,
        "status": status,
        "label": label,
        "messages": msgs[:4],
        "ts": int(time.time() * 1000),
        "details": {
            "cpu_pct": round(cpu_pct, 1),
            "load1": round(load1, 2),
            "mem_free_pct": round(mem_free_pct, 1),
            "disk_free_pct": round(disk_free_pct, 1),
            "success_rate": round(snap["success_rate"] * 100, 1),
            "tool_total": snap["tool_total"],
            "avg_latency_ms": round(snap["avg_latency_ms"], 1),
            "peers": shared.get("peers", 0),
            "active_tools": shared.get("active_tools", []),
            "active_count": len(shared.get("active_tools", [])),
            "token_total": int(shared.get("token_total") or 0),
            "token_prompt": int(shared.get("token_prompt") or 0),
            "token_completion": int(shared.get("token_completion") or 0),
            "token_requests": int(shared.get("token_requests") or 0),
            "token_rate_per_min": round(float(shared.get("token_rate_per_min") or 0.0), 1),
            "token_model": shared.get("token_model", ""),
        },
    }


def _loop():
    while not _stop.wait(INTERVAL_SEC):
        try:
            data = compute()
            with _lock:
                _cache.update(data)
        except Exception:
            pass


def start() -> None:
    """启动后台聚合线程（幂等）。"""
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return
        _stop.clear()
        try:
            _cache.update(compute())
        except Exception:
            pass
        _thread = threading.Thread(target=_loop, name="hcq-health-agg", daemon=True)
        _thread.start()


def current() -> dict:
    with _lock:
        return dict(_cache)
