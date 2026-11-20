# -*- coding: utf-8 -*-
"""统一日志脱敏：任何 handler 输出前，把 sk-/Token/Key/Authorization/Cookie 打码并截断长文本。"""
import logging
import re

MAX_LOG_LEN = 2000

_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{6,}"), "sk-***"),
    (re.compile(r"(?i)\b(bearer)(\s+)[A-Za-z0-9._\-]{8,}"), r"\1\2***"),
    (re.compile(r"(?i)\b(api[_-]?key|apikey|access[_-]?key|secret[_-]?key)\b(\s*[:=]\s*)[\"']?([^\s'\"\\,;}\]]{6,})"),
     r"\1\2***"),
    (re.compile(r"(?i)\b(access[_-]?token|refresh[_-]?token|id[_-]?token|token)\b(\s*[:=]\s*)[\"']?([^\s'\"\\,;}\]]{8,})"),
     r"\1\2***"),
    (re.compile(r"(?i)\b(secret|password|passwd|passphrase)\b(\s*[:=]\s*)[\"']?([^\s'\"\\,;}\]]{4,})"), r"\1\2***"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*)[^\s,;}]{8,}"), r"\1***"),
    (re.compile(r"(?i)(cookie\s*[:=]\s*)[^\n]{10,}"), r"\1***"),
    (re.compile(r"(?i)(x-user-id\s*[:=]\s*)([A-Za-z0-9_\-]{10,})"), r"\1***"),
]


def redact(text):
    """对任意字符串做密钥打码 + 超长截断。"""
    if not isinstance(text, str) or not text:
        return text
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    if len(out) > MAX_LOG_LEN:
        out = out[:MAX_LOG_LEN] + "...[TRUNCATED %d chars]" % (len(out) - MAX_LOG_LEN)
    return out


class RedactFilter(logging.Filter):
    """挂在 handler 上：消息、参数、异常文本全部先脱敏。"""

    def filter(self, record):
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: (redact(v) if isinstance(v, str) else v) for k, v in record.args.items()}
                elif isinstance(record.args, tuple):
                    record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
        except Exception:
            pass
        return True


def install():
    """给 root 与所有已注册 logger 的 handler 装上脱敏过滤器（幂等）。"""
    targets = [logging.getLogger()]
    for name, lg in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(lg, logging.Logger):
            targets.append(lg)
    for lg in targets:
        for h in list(getattr(lg, "handlers", []) or []):
            if not any(isinstance(f, RedactFilter) for f in h.filters):
                h.addFilter(RedactFilter())
    return True
