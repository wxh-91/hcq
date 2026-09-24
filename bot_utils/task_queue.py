import asyncio
import logging
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

SEPARATOR = "＊"          # 全角星号
MAX_TASKS = 5


def split_tasks(text: str) -> list:
    """按全角星号拆分任务，过滤空项"""
    if SEPARATOR not in text:
        return [text.strip()] if text.strip() else []
    parts = [p.strip() for p in text.split(SEPARATOR)]
    return [p for p in parts if p]


async def map_tasks_to_tools(tasks: list, tool_names: list) -> list:
    """
    一次 AI 调用，把多个子任务映射到工具名。
    返回：[{"task": "任务描述", "tool": "工具名 或 None"}, ...]
    tool 为 None 表示对话类任务，不进队列。
    """
    from ai import safe_api_request

    task_list = "\n".join([f"{i+1}. {t}" for i, t in enumerate(tasks)])
    prompt = f"""请判断以下每个任务应该调用什么工具。

可用工具：
{chr(10).join(tool_names)}

任务列表：
{task_list}

只返回工具名，每行一个，用逗号分隔。如果某个任务不需要工具（纯对话），返回 none。
格式示例：
download_m3u8,execute_shell,none
"""

    messages = [
        {"role": "system", "content": "你是一个工具路由助手，只输出工具名。"},
        {"role": "user", "content": prompt},
    ]

    try:
        reply, _ = await safe_api_request(messages, strategy="chat", tools=None)
    except Exception as e:
        logger.error(f"[task_queue] AI 映射失败: {e}")
        return [{"task": t, "tool": None} for t in tasks]

    # 解析 AI 返回的工具名列表
    names = [n.strip() for n in reply.replace("\n", ",").split(",") if n.strip()]

    result = []
    for i, task in enumerate(tasks):
        tool = names[i] if i < len(names) else None
        if tool and tool.lower() == "none":
            tool = None
        result.append({"task": task, "tool": tool})
    return result


async def sort_by_scores(items: list) -> list:
    """
    按分数排序。分数不同时直接排序；分数相同时交给 AI 判断。
    items: [{"task": ..., "tool": ...}, ...]
    """
    from bot_utils.scores import get_scores
    from ai import safe_api_request

    tool_names = [it["tool"] for it in items if it["tool"]]
    scores = get_scores(tool_names)

    # 检查是否存在相同分数
    score_values = list(scores.values())
    has_tie = len(score_values) != len(set(score_values))

    if not has_tie:
        # 直接按分数降序排列
        items_sorted = sorted(
            items,
            key=lambda it: scores.get(it["tool"], 0) if it["tool"] else 0,
            reverse=True,
        )
        return items_sorted

    # 分数有相同时，交给 AI 判断
    score_info = "\n".join([f"- {k}: {v} 分" for k, v in scores.items()])
    task_info = "\n".join([f"{i+1}. {it['task']}（工具：{it['tool']}）" for i, it in enumerate(items)])
    prompt = f"""以下任务涉及的工具分数如下：
{score_info}

任务列表：
{task_info}

请按执行顺序排序，只返回任务序号，用逗号分隔（如 2,1,3）。
"""

    messages = [
        {"role": "system", "content": "你是一个任务调度助手，只输出序号。"},
        {"role": "user", "content": prompt},
    ]

    try:
        reply, _ = await safe_api_request(messages, strategy="chat", tools=None)
        order = [int(n.strip()) - 1 for n in reply.replace("\n", ",").split(",") if n.strip().isdigit()]
        if len(order) == len(items) and sorted(order) == list(range(len(items))):
            return [items[i] for i in order]
    except Exception as e:
        logger.error(f"[task_queue] AI 排序失败: {e}")

    # 兜底：按分数降序
    return sorted(
        items,
        key=lambda it: scores.get(it["tool"], 0) if it["tool"] else 0,
        reverse=True,
    )


class TaskQueue:
    """任务队列（单例，串行执行）"""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._lock = asyncio.Lock()
        return cls._instance

    async def submit(self, text: str, user_id: str, chat_type: str, group_id: str) -> None:
        """
        提交一条包含多个任务的消息。内部负责拆分、映射、排序、执行。
        """
        async with self._lock:
            tasks = split_tasks(text)
            if not tasks:
                return

            if len(tasks) > MAX_TASKS:
                logger.warning(f"[task_queue] 任务数 {len(tasks)} 超过上限 {MAX_TASKS}，只取前 {MAX_TASKS} 个")
                tasks = tasks[:MAX_TASKS]

            # 单任务：直接走原 ask_ai
            if len(tasks) == 1:
                from ai import ask_ai
                reply = await ask_ai(user_id, tasks[0], chat_type, group_id)
                await self._notify(user_id, reply)
                return

            # 多任务：映射工具
            from admin import TOOLS
            tool_names = list(TOOLS.keys())
            items = await map_tasks_to_tools(tasks, tool_names)

            # 过滤出工具类任务（tool 不为 None）
            tool_items = [it for it in items if it["tool"]]
            chat_items = [it for it in items if not it["tool"]]

            if not tool_items:
                # 全是对话类，逐个走 ask_ai
                from ai import ask_ai
                for it in chat_items:
                    reply = await ask_ai(user_id, it["task"], chat_type, group_id)
                    await self._notify(user_id, reply)
                return

            # 排序
            sorted_items = await sort_by_scores(tool_items)

            # 串行执行
            from ai import ask_ai
            for it in sorted_items:
                reply = await ask_ai(user_id, it["task"], chat_type, group_id)
                await self._notify(user_id, reply)

            # 对话类任务最后处理
            for it in chat_items:
                reply = await ask_ai(user_id, it["task"], chat_type, group_id)
                await self._notify(user_id, reply)

    async def _notify(self, user_id: str, message: str) -> None:
        """私聊推送结果"""
        import nonebot
        try:
            bot = nonebot.get_bot()
            await bot.send_private_msg(user_id=int(user_id), message=message)
        except Exception as e:
            logger.error(f"[task_queue] 推送失败: {e}")
