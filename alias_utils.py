# -*- coding: utf-8 -*-
"""alias_utils — [安全别名] 统一数据预处理管道（公共模块）

从 dz.py._preprocess_alias 抽离为公共模块，供 dz.py / divine_task.py / large_task.py 复用。
能力：
  - preprocess_alias(text)：原逻辑，仅匹配"以别名开头"的消息（普通对话入口兼容）
  - resolve_aliases_in_text(text)：文本内任意位置提取别名片段（'tz 99032' / 'tz#99032' / '抓取tz99032'），
    物理替换为完整 URL —— 神授/大任务入口的环境对齐
别名解析绝不交给大模型：命中即替换，解析失败保留原文。
"""
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

_SITE_MAP_CACHE = None
_SITE_MAP_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "plugins", "crawler", "site_map.json"
)


def _load_site_map() -> dict:
    global _SITE_MAP_CACHE
    if _SITE_MAP_CACHE is not None:
        return _SITE_MAP_CACHE
    try:
        with open(_SITE_MAP_PATH, encoding="utf-8") as f:
            _SITE_MAP_CACHE = json.load(f)
    except Exception:
        _SITE_MAP_CACHE = {}
    return _SITE_MAP_CACHE


def preprocess_alias(text: str) -> str:
    """原逻辑：仅当文本以 '别名<空格|#>ID' 开头时替换为完整 URL。"""
    s = (text or "").strip()
    if not s or s.startswith(("http://", "https://")):
        return text
    site_map = _load_site_map()
    for k, base in site_map.items():
        for sep in (" ", "#"):
            prefix = k + sep
            if s.startswith(prefix):
                tail = s[len(prefix):].strip()
                if tail:
                    resolved = base + tail.split()[0]
                    logger.info("[安全别名] 前置解析: '%s' -> %s", s, resolved)
                    return resolved
    return text


_ALIAS_SEG_RE = None


def _get_alias_seg_re():
    """构造匹配任意别名的正则：tz 99032 / tz#99032 / tz99032（数字或短ID）"""
    global _ALIAS_SEG_RE
    if _ALIAS_SEG_RE is not None:
        return _ALIAS_SEG_RE
    site_map = _load_site_map()
    keys = sorted(site_map.keys(), key=len, reverse=True)
    if not keys:
        _ALIAS_SEG_RE = re.compile(r"(?!)")  # 永不匹配
        return _ALIAS_SEG_RE
    # 别名后允许：空格+ID、#+ID、直接跟ID（字母数字，≥3 位降低误伤）
    pat = r"(?<![A-Za-z0-9])(%s)(?:\s*[#:：]?\s*|\s*)([A-Za-z0-9_]{3,40})" % "|".join(
        re.escape(k) for k in keys
    )
    _ALIAS_SEG_RE = re.compile(pat)
    return _ALIAS_SEG_RE


def resolve_aliases_in_text(text: str) -> str:
    """文本内任意位置别名替换：'抓取 tz 99032' → '抓取 https://tob.tobvic.com:52000/video/detail/99032'。

    只替换"别名 + ID"连续片段；已含 http 的文本不再处理。
    """
    if not text:
        return text
    if text.strip().startswith(("http://", "https://")):
        return text
    site_map = _load_site_map()
    if not site_map:
        return text
    seg_re = _get_alias_seg_re()

    def _rep(m):
        key = m.group(1)
        tail = m.group(2)
        base = site_map.get(key)
        if not base:
            return m.group(0)
        resolved = base + tail
        logger.info("[安全别名] 文本内解析: '%s%s' -> %s", key, tail, resolved)
        return resolved

    return seg_re.sub(_rep, text)
