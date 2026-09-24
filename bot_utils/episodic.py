import json
import os
from datetime import datetime
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

EPISODIC_FILE = "/root/robot/episodic.log"
WRITE_INTERVAL = 15


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load_state() -> dict:
    """从 memory.json 读取 episodic 状态，跨天时自动重置"""
    from memory import Memory
    mem = Memory()
    state = mem.get("episodic_state", {})
    today = _today()
    if state.get("date") != today:
        state = {"date": today, "flushed_turn": 0}
        mem.set("episodic_state", state)
    return state


def _save_state(state: dict) -> None:
    from memory import Memory
    Memory().set("episodic_state", state)


def count_today_turns() -> int:
    """返回当天已落盘的轮数"""
    return _load_state().get("flushed_turn", 0)


def append_messages(messages: list, start_turn: int) -> None:
    """把一批消息追加到情景文件"""
    today = _today()
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    turn = start_turn
    try:
        with open(EPISODIC_FILE, "a", encoding="utf-8") as f:
            for msg in messages:
                role = msg.get("role", "")
                if role == "user":
                    turn += 1
                rec = {
                    "ts": now,
                    "date": today,
                    "turn": turn,
                    "role": role,
                    "content": msg.get("content", ""),
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        logger.info(f"[episodic] 已落盘 {len(messages)} 条消息，到第 {turn} 轮")
    except IOError as e:
        logger.error(f"[episodic] 写入失败: {e}")


def maybe_flush(key: str, history: list) -> None:
    """
    检查是否需要落盘。达到 15 轮就追加写入，计数器精确跟踪。
    全部由代码自动完成，不经过 AI。
    """
    if not history:
        return

    user_indices = [i for i, m in enumerate(history) if m.get("role") == "user"]
    total_turns = len(user_indices)

    state = _load_state()
    flushed = state.get("flushed_turn", 0)

    pending = total_turns - flushed
    if pending < WRITE_INTERVAL:
        return

    if flushed >= len(user_indices):
        return

    start_idx = user_indices[flushed]
    new_messages = history[start_idx:]

    append_messages(new_messages, start_turn=flushed)

    state["flushed_turn"] = total_turns
    _save_state(state)


def recall(keyword: str, limit: int = 20) -> list:
    """按关键词检索当天情景记忆"""
    if not Path(EPISODIC_FILE).exists():
        return []
    today = _today()
    results = []
    try:
        with open(EPISODIC_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("date") != today:
                    continue
                if keyword in rec.get("content", ""):
                    results.append(rec)
    except IOError as e:
        logger.error(f"[episodic] 检索失败: {e}")
    return results[-limit:]


def clear_expired() -> None:
    """清除过期日期（非今天）的记录，启动时调用"""
    if not Path(EPISODIC_FILE).exists():
        return
    today = _today()
    keep = []
    try:
        with open(EPISODIC_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("date") == today:
                    keep.append(line)
        with open(EPISODIC_FILE, "w", encoding="utf-8") as f:
            for line in keep:
                f.write(line + "\n")
        logger.info(f"[episodic] 已清除过期记录，保留 {len(keep)} 条")
    except IOError as e:
        logger.error(f"[episodic] 清理失败: {e}")
