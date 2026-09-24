import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

CHECKIN_SCRIPT = "/home/wxh/checkin.sh"
LOG_FILE = "/home/wxh/checkin.log"
STATE_FILE = "/root/robot/checkin_state.json"


def get_today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def load_state() -> dict:
    if not Path(STATE_FILE).exists():
        return {}
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(data: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def has_checked_in_today() -> bool:
    state = load_state()
    return state.get("last_checkin_date") == get_today()


def parse_checkin_result() -> dict:
    """
    从日志文件中解析最新一次签到结果。
    只读取最后一次 '=== ... 开始签到 ===' 之后的记录，避免历史记录污染。
    """
    if not Path(LOG_FILE).exists():
        return {"success": False, "error": "日志文件不存在"}

    with open(LOG_FILE, "r") as f:
        lines = f.readlines()

    # 从后往前找到最近一次“开始签到”的位置
    last_start_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if "开始签到" in lines[i]:
            last_start_idx = i
            break

    if last_start_idx is None:
        return {"success": False, "error": "日志中未找到签到记录"}

    # 只取最近一次执行的日志片段
    latest_lines = lines[last_start_idx:]
    text = "\n".join(latest_lines)

    result = {"success": False, "award": 0, "total": 0, "quota": 0, "time": ""}

    # 提取签到时间
    time_match = re.search(r"=== (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) 开始签到 ===", text)
    if time_match:
        result["time"] = time_match.group(1)

    # 判断本次结果（互斥，不会同时匹配历史）
    if "签到成功" in text:
        result["success"] = True
        award_match = re.search(r"今日获得 (\d+) 额度", text)
        if award_match:
            result["award"] = int(award_match.group(1))
    elif "今日已签到，跳过" in text:
        result["success"] = True
        result["award"] = 0
    else:
        # 可能是 401 或其他失败
        result["success"] = False
        error_match = re.search(r"❌ (.+)", text)
        if error_match:
            result["error"] = error_match.group(1).strip()

    # 提取累计数据
    total_match = re.search(r"累计签到: (\d+) 天", text)
    if total_match:
        result["total"] = int(total_match.group(1))
    quota_match = re.search(r"累计总额度: (\d+)", text)
    if quota_match:
        result["quota"] = int(quota_match.group(1))

    return result

async def execute_checkin() -> dict:
    """
    执行签到脚本，返回结构化结果。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", CHECKIN_SCRIPT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return {"success": False, "error": f"脚本执行失败，返回码 {proc.returncode}"}
        return parse_checkin_result()
    except Exception as e:
        return {"success": False, "error": str(e)}


async def auto_checkin() -> dict:
    """
    启动时自动签到：若今日已签到则跳过，否则执行并记录状态。
    返回结构化结果，供 dz.py 拼装通知消息。
    """
    if has_checked_in_today():
        state = load_state()
        return {
            "skipped": True,
            "success": True,
            "time": state.get("last_checkin_time", ""),
            "award": state.get("award", 0),
            "total": state.get("total", 0),
            "quota": state.get("quota", 0),
            "date": get_today(),
        }

    result = await execute_checkin()
    if result.get("success"):
        save_state({
            "last_checkin_date": get_today(),
            "last_checkin_time": result.get("time", ""),
            "award": result.get("award", 0),
            "total": result.get("total", 0),
            "quota": result.get("quota", 0),
            "success": True,
        })
    return result


async def run_checkin() -> str:
    """
    供 AI 工具调用的入口，返回纯文本字符串。
    """
    result = await auto_checkin()

    if result.get("skipped"):
        return (
            f"今日已签到，无需重复签到。\n"
            f"签到日期：{result.get('date', '未知')}\n"
            f"签到时间：{result.get('time', '未知')}\n"
            f"获得额度：{result.get('award', 0)}\n"
            f"累计签到：{result.get('total', 0)} 天\n"
            f"累计总额度：{result.get('quota', 0)}"
        )

    if result.get("success"):
        if result.get("award", 0) > 0:
            return (
                f"签到成功。\n"
                f"签到日期：{get_today()}\n"
                f"签到时间：{result.get('time', '未知')}\n"
                f"获得额度：{result.get('award', 0)}\n"
                f"累计签到：{result.get('total', 0)} 天\n"
                f"累计总额度：{result.get('quota', 0)}"
            )
        else:
            return (
                f"今日已签到，跳过。\n"
                f"累计签到：{result.get('total', 0)} 天\n"
                f"累计总额度：{result.get('quota', 0)}"
            )

    return f"签到失败：{result.get('error', '未知错误')}"
