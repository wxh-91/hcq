# -*- coding: utf-8 -*-
"""Experience Bank — 经验库匹配模块（Distillation Center）

从 experience.db 检索与当前 query 最相似的历史成功轨迹（"学霸作业"），
返回格式化后的可读文本供 ai.py 注入 system 消息。

数据流：distill_center.sh（提取脚本）→ experience.db → 本模块 → ai.py 抄作业。
"""
import os
_ROOT = os.environ.get("PROJECT_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import re
import sqlite3
import os


DB_PATH = _ROOT + "/plugins/core_utils/experience.db"
MIN_HITS = 2  # 高匹配阈值：query 特征词命中 ≥2 个才算匹配

# 停用词（只过滤无法携带意图的虚词）
_STOP = set("的了吗呢啊吧与和或及是在我有你要他她们它这那什么怎么如何哪个给我帮我请问请把给为被从到对向")


def extract_keywords(query: str) -> set:
    """提取 query 的 2-5 字中文连续片段（意图特征词）"""
    words = set()
    clean = re.sub(r'[\s/\\\.:,，。；;！!？?()（）【】\[\]{}]+', "", query)
    for n in (2, 3, 4, 5):
        for i in range(0, len(clean) - n + 1):
            seg = clean[i:i + n]
            if (
                seg
                and not all(ch in _STOP for ch in seg)
                and any("\u4e00" <= ch <= "\u9fff" for ch in seg)
            ):
                words.add(seg)
    return words


def _format_trace(golden_trace: str) -> str:
    """把 golden_trace JSON 转成对 LLM 友好的可读文本"""
    try:
        t = json.loads(golden_trace)
    except Exception:
        return golden_trace[:1500]
    lines = ["【历史成功示例】"]
    lines.append("用户指令：" + str(t.get("query", "")))
    thoughts = t.get("thought_chain") or []
    if thoughts:
        lines.append("思考链：" + " → ".join(str(x)[:120] for x in thoughts[:3]))
    tools = []
    for c in t.get("tools_used") or []:
        tool = c.get("tool", "")
        status = c.get("status", "")
        tools.append("%s(%s)" % (tool, status))
    if tools:
        lines.append("工具序列：" + " → ".join(tools))
    return "\n".join(lines)


def match_experience(query: str, min_hits: int = MIN_HITS):
    """检索最相似历史经验。

    返回 (golden_text, matched_keywords)：
      - golden_text: 格式化后的可读轨迹（无匹配返回 None）
      - matched_keywords: 命中的特征词列表
    """
    qk = extract_keywords(query)
    if not qk:
        return None, []
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        best, best_score, best_kw = None, 0, []
        for r in conn.execute("SELECT intent_keywords, golden_trace FROM experiences"):
            try:
                kw = set(json.loads(r["intent_keywords"]))
            except Exception:
                continue
            hits = kw & qk
            score = len(hits)
            # 领域加分：下载视频类意图命中 download_video_by_url 时加权
            if "download_video_by_url" in hits and re.search(r"下载|视频|tz\s*\d", query, re.I):
                score += 2
            if score > best_score:
                best, best_score, best_kw = r, score, sorted(hits)
        if best and best_score >= min_hits:
            return _format_trace(best["golden_trace"]), best_kw
        return None, []
    except Exception:
        return None, []
    finally:
        if conn:
            conn.close()
