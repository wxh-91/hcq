# -*- coding: utf-8 -*-
"""极简健康接口服务（标准库，127.0.0.1:8086）。

GET  /api/health  -> {"score","status","label","messages","details"}
GET  /api/metrics -> 原始窗口汇总（各进程上报合并）
POST /api/ingest  -> 进程内埋点上报入口（内存处理，不落盘）
GET  /health      -> "ok"（探活）
"""

import json
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
  from . import aggregator, metrics, top_proc
except ImportError:
  import aggregator
  import metrics
  import top_proc

HOST = "127.0.0.1"
PORT = 8086


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "hcq-health/1.1"

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/api/ingest":
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                aggregator.ingest_peer(json.loads(raw or b"{}"))
                self._send(200, b'{"ok":true}')
            else:
                self._send(404, b'{"ok":false,"error":"not found"}')
        except Exception as e:
            self._send(400, json.dumps({"ok": False, "error": str(e)[:120]}).encode())

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path in ("/api/health", "/healthz"):
                self._send(200, json.dumps(aggregator.current(), ensure_ascii=False).encode())
            elif path == "/api/metrics":
                s = metrics.snapshot()
                sh = aggregator.peer_summary()
                if sh.get("peers"):
                    ok, fail = sh["ok"], sh["fail"]
                    total = ok + fail
                    s.update({
                        "tool_ok": ok, "tool_fail": fail, "tool_total": total,
                        "fail_rate": (fail / total) if total else 0.0,
                        "success_rate": (ok / total) if total else 1.0,
                        "aup_reject": sh["aup"], "rate_limited": sh["r429"],
                        "avg_latency_ms": (sh["lat_sum"] / sh["lat_n"]) if sh["lat_n"] else 0.0,
                        "lat_sum": sh["lat_sum"], "lat_n": sh["lat_n"],
                        "processes": sh["peers"],
                    })
                self._send(200, json.dumps(s, ensure_ascii=False).encode())
            elif path == "/api/top":
                snap = top_proc.snapshot()
                snap.update({"ok": True, "source": "wsl"})
                self._send(200, json.dumps(snap, ensure_ascii=False).encode())
            elif path == "/health":
                self._send(200, b"ok", "text/plain; charset=utf-8")
            else:
                self._send(404, b'{"ok":false,"error":"not found"}')
        except Exception as e:
            self._send(500, json.dumps({"ok": False, "error": str(e)[:120]}, ensure_ascii=False).encode())

    def log_message(self, fmt, *args):
        pass  # 不刷日志，避免噪声与磁盘增长


def main():
    aggregator.start()
    top_proc.start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))   # 后台启动时 SIGINT 可能被继承为忽略
    print("hcq-health listening on http://%s:%d" % (HOST, PORT), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
