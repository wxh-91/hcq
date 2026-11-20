# -*- coding: utf-8 -*-
"""Experience Bank v4 — 基于 SOP 的标准作业程序经验库。

相对 v3（随机经验轨迹）的重构：
  - 每条经验是一份结构化 SOP：触发条件 Trigger + 解决步骤 Action + 来源 Source；
  - 来源分两类：human（人类经 inject_experience.py 录入）/ auto_distill（本地 Agent
    跑通成功链路后自动蒸馏）；
  - 防噪点规则：只收宏工具闭环与存在生态联动的多工具链路，孤立单原子工具不入库；
  - 对外保留 ai.py 既有钩子签名：
      match_experience(query) -> (golden_text, matched_keywords, sop_id, score)
      record_feedback(sop_id, outcome)  # outcome: adopt / success / fail
"""
import os
_ROOT = os.environ.get("PROJECT_ROOT") or os.path.dirname(os.path.abspath(__file__))

import json
import os
import re
import sqlite3
from datetime import datetime


DB_PATH = _ROOT + "/plugins/core_utils/experience.db"
# 命中门槛：1 个 curated 关键词直配（权重 10）或 ≥2 个 n-gram 重叠
MIN_SCORE = 2.0
CURRENT_TOOL_VERSION = 1
DECAY_DAYS = 30
DECAY_FACTOR = 0.5
FAIL_DECAY_AFTER = 3

# 宏工具闭环：单工具即构成一条可复用 SOP，自动蒸馏允许入库。
MACRO_TOOLS = {
    "download_audio_mp3",
    "download_video_by_url",
    "crawl_video_link",
    "interactive_browser",
    "win_ui_automation",
    "win_shell",
    "gui_shopping_login",
    "gui_shopping_assistant",
    "feishu_send_file",
    "web_security_audit",
}

# 孤立单原子工具：单独出现属于噪点，不自动蒸馏入库（参与多工具联动链路时除外）。
ATOMIC_NOISE = {
    "read_file", "write_file", "replace_line", "delete_line", "insert_line",
    "regex_extract", "json_query", "http_request", "crawler_probe",
    "download_m3u8", "execute_shell", "run_bash_script",
}

_cache = {"rows": None}


# ------------------------------------------------------------------ 基础层

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger_text TEXT NOT NULL,
            trigger_keywords TEXT NOT NULL DEFAULT '[]',
            action_steps TEXT NOT NULL DEFAULT '[]',
            tool_chain TEXT NOT NULL DEFAULT '[]',
            category TEXT NOT NULL DEFAULT 'macro',
            source TEXT NOT NULL DEFAULT 'human',
            adopt_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            fail_count INTEGER NOT NULL DEFAULT 0,
            tool_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT,
            last_used_at TEXT
        )
        """
    )
    return conn


def _tokenize(text: str) -> set:
    tokens = set()
    clean = re.sub(r"[\s/\\\.:,，。；;！!？?()（）【】\[\]{}\"'“”]+", "", str(text))
    for n in (2, 3, 4, 5):
        for i in range(0, len(clean) - n + 1):
            seg = clean[i:i + n]
            if seg and any("\u4e00" <= c <= "\u9fff" for c in seg):
                tokens.add(seg)
    for en in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", str(text)):
        tokens.add(en.lower())
    return tokens


def _decode(raw: str, default):
    try:
        return json.loads(raw or "[]")
    except Exception:
        return default


def _row_to_dict(r) -> dict:
    return {
        "id": r[0],
        "trigger": r[1],
        "keywords": _decode(r[2], []),
        "steps": _decode(r[4], []),
        "chain": _decode(r[3], []),
        "category": r[5],
        "source": r[6],
        "adopt": r[7],
        "success": r[8],
        "fail": r[9],
        "last_used": r[10],
        "version": r[11],
    }


def _load() -> list:
    if _cache["rows"] is not None:
        return _cache["rows"]
    conn = _connect()
    rows = conn.execute(
        "SELECT id, trigger_text, trigger_keywords, tool_chain, action_steps, "
        "category, source, COALESCE(adopt_count,0), COALESCE(success_count,0), "
        "COALESCE(fail_count,0), COALESCE(last_used_at,''), COALESCE(tool_version,1) "
        "FROM sops WHERE COALESCE(tool_version,1) = ?",
        (CURRENT_TOOL_VERSION,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        blob = " ".join(
            [d["trigger"]] + [str(x) for x in d["keywords"]] +
            [str(x) for x in d["chain"]] +
            [str(st.get("tool", "")) + str(st.get("note", "")) for st in d["steps"] if isinstance(st, dict)]
        )
        d["doc_tokens"] = _tokenize(blob)
        out.append(d)
    _cache["rows"] = out
    return out


def _decay(score: float, d: dict) -> float:
    s = score
    if d["last_used"]:
        try:
            used_dt = datetime.strptime(d["last_used"], "%Y-%m-%d %H:%M:%S")
            days = (datetime.now() - used_dt).total_seconds() / 86400.0
            if days > DECAY_DAYS:
                s *= DECAY_FACTOR
        except Exception:
            pass
    if d["fail"] >= FAIL_DECAY_AFTER and d["fail"] > d["success"]:
        s *= DECAY_FACTOR
    return s


# ------------------------------------------------------------------ 写入层

def inject_sop(record: dict) -> int:
    """录入一条人类 SOP，返回 sop_id。record 字段见 inject_experience.py 说明。"""
    conn = _connect()
    now = _now()
    cur = conn.execute(
        "INSERT INTO sops (trigger_text, trigger_keywords, action_steps, tool_chain, "
        "category, source, adopt_count, success_count, fail_count, tool_version, "
        "created_at, last_used_at) VALUES (?,?,?,?,?,?,0,0,0,?,?,?)",
        (
            str(record.get("trigger_text", "")).strip(),
            json.dumps(record.get("trigger_keywords", []), ensure_ascii=False),
            json.dumps(record.get("action_steps", []), ensure_ascii=False),
            json.dumps(record.get("tool_chain", []), ensure_ascii=False),
            str(record.get("category", "macro")),
            str(record.get("source", "human")),
            CURRENT_TOOL_VERSION,
            now, now,
        ),
    )
    conn.commit()
    sid = cur.lastrowid
    conn.close()
    _cache["rows"] = None
    return sid


def inject_sops(records: list) -> list:
    """批量录入，返回每条 sop_id 列表。"""
    return [inject_sop(r) for r in records]


def _should_distill(chain: list) -> bool:
    """防噪点判定：宏工具闭环，或含 ≥2 个非原子噪声工具的联动链路，才允许自动入库。"""
    names = [str(c.get("tool", "")) for c in chain if isinstance(c, dict)]
    if any(n in MACRO_TOOLS for n in names):
        return True
    distinct = [n for n in dict.fromkeys(names) if n not in ATOMIC_NOISE]
    return len(distinct) >= 2


def record_auto_sop(prompt: str, chain: list) -> int | None:
    """本地 Agent 跑通成功链路后自动蒸馏入库。

    chain: [{"tool": name, "args_brief": {...}, "note": ""}, ...]，按执行顺序。
    命中防噪点规则才入库；同签名既有 SOP 只回写成功计数，不重复插入。
    """
    if not chain or not _should_distill(chain):
        return None
    conn = _connect()
    now = _now()
    tool_names = [c.get("tool") for c in chain]
    row = conn.execute(
        "SELECT id FROM sops WHERE tool_chain = ? AND source = 'auto_distill'",
        (json.dumps(tool_names, ensure_ascii=False),),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE sops SET success_count = COALESCE(success_count,0)+1, last_used_at=? WHERE id=?",
            (now, row[0]),
        )
        conn.commit()
        conn.close()
        _cache["rows"] = None
        return row[0]

    keywords = [w for w in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,6}", str(prompt))][:12]
    cur = conn.execute(
        "INSERT INTO sops (trigger_text, trigger_keywords, action_steps, tool_chain, "
        "category, source, adopt_count, success_count, fail_count, tool_version, "
        "created_at, last_used_at) VALUES (?,?,?,?,?,?,0,1,0,?,?,?)",
        (
            str(prompt).strip()[:200],
            json.dumps(keywords, ensure_ascii=False),
            json.dumps(chain, ensure_ascii=False),
            json.dumps(tool_names, ensure_ascii=False),
            "chain" if len(set(tool_names)) >= 2 else "macro",
            "auto_distill",
            CURRENT_TOOL_VERSION,
            now, now,
        ),
    )
    conn.commit()
    sid = cur.lastrowid
    conn.close()
    _cache["rows"] = None
    return sid


# ------------------------------------------------------------------ 检索层

def _score(query: str, d: dict):
    q_low = query.lower()
    qt = _tokenize(query)
    kw_hits = [k for k in d["keywords"] if k and str(k).lower() in q_low]
    overlap = qt & d["doc_tokens"]
    raw = len(kw_hits) * 10 + len(overlap)
    return _decay(float(raw), d), kw_hits, overlap


def match_experience(query: str):
    """检索最匹配 SOP。返回 (golden_text, matched_keywords, sop_id, score)。"""
    if not str(query or "").strip():
        return None, [], None, 0.0
    try:
        rows = _load()
        if not rows:
            return None, [], None, 0.0
        best = None
        best_score = -1.0
        best_kw = []
        best_overlap = set()
        for d in rows:
            score, kw_hits, overlap = _score(query, d)
            if score > best_score:
                best, best_score, best_kw, best_overlap = d, score, kw_hits, overlap
        if best_score < MIN_SCORE:
            return None, [], best["id"], round(best_score, 4)
        matched = best_kw + sorted(best_overlap)[:6]
        return _format_sop(best), matched, best["id"], round(best_score, 4)
    except Exception:
        return None, [], None, 0.0


def list_matches(query: str, k: int = 3):
    """Eval 用：top-k (sop_id, score)，不做阈值门控。"""
    if not str(query or "").strip():
        return []
    try:
        rows = _load()
        scored = []
        for d in rows:
            score, _, _ = _score(query, d)
            scored.append((round(score, 4), d["id"]))
        scored.sort(key=lambda x: -x[0])
        return scored[:k]
    except Exception:
        return []


def record_feedback(sop_id, outcome: str):
    """反馈回写：adopt（采纳）/ success（跑通）/ fail（失败）。"""
    if sop_id is None:
        return
    try:
        conn = _connect()
        now = _now()
        col = {"adopt": "adopt_count", "success": "success_count", "fail": "fail_count"}.get(outcome)
        if col:
            conn.execute(
                f"UPDATE sops SET {col} = COALESCE({col},0)+1, last_used_at=? WHERE id=?",
                (now, sop_id),
            )
            conn.commit()
        conn.close()
        _cache["rows"] = None
    except Exception:
        pass


def list_sops(limit: int = 50) -> list:
    """取证/注入脚本用：列出全部 SOP 摘要。"""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, trigger_text, tool_chain, source, category, adopt_count, "
        "success_count, fail_count, created_at FROM sops ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [
        {
            "id": r[0], "trigger_text": r[1],
            "tool_chain": json.loads(r[2] or "[]"), "source": r[3],
            "category": r[4], "adopt_count": r[5], "success_count": r[6],
            "fail_count": r[7], "created_at": r[8],
        }
        for r in rows
    ]


# ------------------------------------------------------------------ 格式化

def _format_sop(d: dict) -> str:
    source_label = "人类录入" if d["source"] == "human" else "自动蒸馏"
    lines = ["【SOP 标准作业程序】", "触发：" + str(d["trigger"])]
    if d["steps"]:
        lines.append("标准步骤：")
        for i, st in enumerate(d["steps"], 1):
            if isinstance(st, dict):
                tool = st.get("tool", "")
                brief = st.get("args_brief") or st.get("args", {})
                note = st.get("note", "")
                brief_s = json.dumps(brief, ensure_ascii=False) if brief else ""
                lines.append(f"  {i}. {tool} {brief_s}".strip() + (f"（{note}）" if note else ""))
    if d["chain"]:
        lines.append("工具链：" + " → ".join(str(x) for x in d["chain"]))
    lines.append("来源：" + source_label)
    return "\n".join(lines)
