import json
import os
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ===== 配置 =====
SCORES_FILE = "/root/robot/scores.json"
HALF_LIFE_DAYS = 15          # 半衰期 15 天
SUCCESS_SCORE = 5            # 每次成功 +5
WHITELIST = [
    "write_file",
    "replace_line",
    "delete_line",
    "insert_line",
    "backup_project",
    "execute_shell",
    "run_bash_script",
    "download_m3u8",
]

DEFAULT_SCORES = {
    "version": 1,
    "updated_at": "",
    "scores": {}
}


def _load() -> dict:
    if not Path(SCORES_FILE).exists():
        return DEFAULT_SCORES.copy()
    try:
        with open(SCORES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"加载 scores.json 失败: {e}")
        return DEFAULT_SCORES.copy()


def _save(data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = SCORES_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SCORES_FILE)
    except Exception as e:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        logger.error(f"保存 scores.json 失败: {e}")
        raise


def _decay(score: float, last_success: str) -> float:
    """指数衰减：score × 0.5^(Δt/15)"""
    if not last_success:
        return score
    try:
        last = time.mktime(time.strptime(last_success, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return score
    now = time.time()
    delta_days = (now - last) / 86400
    if delta_days <= 0:
        return score
    return score * (0.5 ** (delta_days / HALF_LIFE_DAYS))


def add_score(tool_name: str) -> None:
    """工具成功后加分（白名单内的工具才加分）"""
    if tool_name not in WHITELIST:
        return
    data = _load()
    scores = data.setdefault("scores", {})
    entry = scores.get(tool_name, {})
    current = entry.get("score", 0)
    last = entry.get("last_success", "")
    current = _decay(current, last)
    current += SUCCESS_SCORE
    entry["score"] = round(current, 2)
    entry["last_success"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    entry["success_count"] = entry.get("success_count", 0) + 1
    scores[tool_name] = entry
    _save(data)
    logger.info(f"[scores] {tool_name} 加分后 = {entry['score']}")


def get_scores(tool_names: list) -> dict:
    """获取指定工具的当前分数（含衰减），返回 {工具名: 分数}"""
    data = _load()
    scores = data.get("scores", {})
    result = {}
    for name in tool_names:
        entry = scores.get(name, {})
        raw = entry.get("score", 0)
        last = entry.get("last_success", "")
        result[name] = round(_decay(raw, last), 2)
    return result
