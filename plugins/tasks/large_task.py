"""
bot_utils/large_task.py — 大型任务五阶段处理器

阶段 1：规划（v4-pro / large_planner）
阶段 2：执行（a6 flash / large_executor）
阶段 3：失败处理
阶段 4：总结（qwen / large_summarizer）
阶段 5：清理

设计约束：
- 只在私聊触发，群聊不走这条路径
- 同一 user_id 同时只允许一个大型任务
- 128k 上下文只存内存，进程重启即丢
- 不写 bot 相关代码，通过 notify 回调向外发消息
- 不引入新框架、新依赖，不经过 safe_api_request
"""
import asyncio
import json
import logging
import time
from typing import Callable, Awaitable, Optional

import aiohttp

logger = logging.getLogger(__name__)

# ============ 全局状态 ============
_contexts: dict[str, dict] = {}   # user_id -> 上下文
_running: set[str] = set()        # 正在跑大型任务的 user_id

# ============ 预算 ============
OVERALL_TIMEOUT = 600.0           # 整体超时（秒）
CTX_TOKEN_KILL = 100000           # 上下文中断阈值（估算 token）
PLANNER_TIMEOUT = 180.0           # 规划单次超时
EXECUTOR_TIMEOUT = 120.0          # 执行单次超时
SUMMARIZER_TIMEOUT = 60.0         # 总结单次超时
MAX_SUBTASKS = 20                 # 子任务数上限


# ============ 底层模型调用（独立于 safe_api_request） ============
async def _call_model(strategy: str, messages: list, timeout: float) -> tuple[str, str]:
    """
    返回 (content, reasoning)。
    直接读 models.json，不做 key 轮询、不做 fallback。
    大型任务每策略只有一把 key。
    """
    from cl import get_strategy_config
    cfg = get_strategy_config(strategy)
    if not cfg:
        raise RuntimeError(f"未找到策略配置: {strategy}")

    model = cfg.get("model")
    base_url = cfg.get("base_url")
    api_keys = cfg.get("api_keys", [])
    if not api_keys:
        raise RuntimeError(f"策略 {strategy} 未配置 api_key")
    api_key = api_keys[0]

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    data = {"model": model, "messages": messages}

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.post(f"{base_url}/chat/completions", json=data, headers=headers) as resp:
            if resp.status != 200:
                err = await resp.text()
                raise RuntimeError(f"策略 {strategy} HTTP {resp.status}: {err[:300]}")
            raw = await resp.json()

    if "error" in raw:
        raise RuntimeError(f"策略 {strategy} API 错误: {raw['error']}")

    choices = raw.get("choices") or []
    if not choices:
        raise RuntimeError(f"策略 {strategy} 返回无 choices")

    message = choices[0].get("message") or {}
    content = message.get("content", "") or ""
    reasoning = (
        message.get("reasoning_content")
        or message.get("reasoning")
        or message.get("thinking")
        or ""
    )
    return content, reasoning


# ============ 上下文 token 估算（保守） ============
def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return len(text) * 2


def _ctx_size(ctx: dict) -> int:
    total = 0
    total += _estimate_tokens(json.dumps(ctx.get("plan") or {}, ensure_ascii=False))
    for ex in ctx.get("executions", []):
        total += _estimate_tokens(json.dumps(ex, ensure_ascii=False))
    return total


# ============ 提示词 ============
PLANNER_PROMPT = """你是大型任务的「总规划师」。你的职责是把用户的完整指令拆解为若干子任务。

【可用工具清单】
{tool_list}

【输出要求】
严格输出 JSON，不要任何解释、寒暄、Markdown 代码块围栏。
格式：
{{
  "task_name": "整个任务的一句话名称",
  "subtasks": [
    {{
      "id": 1,
      "description": "给用户看的人话描述",
      "tool": "工具名，或 null 表示纯文本子任务",
      "args": {{"参数名": "参数值"}},
      "depends_on": [前序子任务的 id 列表，无依赖填空数组]
    }}
  ]
}}

【规则】
- 工具名必须从上面的「可用工具清单」中选，不得自创。
- args 必须符合该工具的参数要求。
- depends_on 必须是已经出现过的 id，不得指向后面。
- 如果某子任务只是让你"思考/总结"，把 tool 设为 null，args 设为 {{}}。
- 拆解粒度合理：一个子任务完成一件明确的事，不要过细也不要过粗。
- 子任务上限 {max_subtasks} 个。
"""

EXECUTOR_PROMPT = """你是大型任务的「执行员」。总规划师已经拆解出子任务，你的职责是：
- 严格按规划师给出的工具名和参数执行
- 不允许改变工具选择
- 不允许质疑规划师的决策
- 如果参数不完整，只补齐缺失的值，不要改变已有的值

【当前子任务】
{current}

【可用工具清单】
{tool_list}

【相关前序结果】
{prior}

【输出要求】
严格输出 JSON，不要任何解释、寒暄、Markdown 代码块围栏。
格式：
{{
  "tool": "工具名，或 null",
  "args": {{"参数名": "参数值"}},
  "note": "一句话说明（可选）"
}}
"""

SUMMARIZER_PROMPT = """你是信息总结助手。只做总结，不做决策。你是 AI。

【任务名】
{task_name}

【子任务执行结果】
{executions}

【输出要求】
用中文写一段人话汇报，包含：
- 整体任务名
- 逐个子任务的结果（成功/失败 + 简要说明）
- 最终状态（全部成功 / 部分失败 / 全部失败）

不要使用 Markdown 代码块围栏。不要添加额外解释。
"""


def _build_tool_list() -> str:
    """从 admin.TOOLS 生成工具清单文本"""
    from admin import TOOLS
    lines = []
    for name, info in TOOLS.items():
        func = info.get("func")
        doc = (func.__doc__ or "").strip().split("\n")[0] if func else "无描述"
        lines.append(f"- {name}: {doc}")
        params = info.get("params_desc", {})
        for k, v in params.items():
            lines.append(f"  参数 {k}: {v}")
    return "\n".join(lines)


# ============ JSON 提取 ============
def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return None


# ============ 阶段 1：规划 ============
async def _plan(user_input: str, tool_list: str) -> dict:
    prompt = PLANNER_PROMPT.format(tool_list=tool_list, max_subtasks=MAX_SUBTASKS)
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_input},
    ]
    content, reasoning = await _call_model("large_planner", messages, PLANNER_TIMEOUT)
    plan = _extract_json(content)
    if plan is None:
        logger.warning("[large-task] 规划输出非 JSON，重试一次")
        content, reasoning = await _call_model("large_planner", messages, PLANNER_TIMEOUT)
        plan = _extract_json(content)
        if plan is None:
            raise RuntimeError("规划失败：输出格式错误（非 JSON）")
    return {"plan": plan, "reasoning": reasoning, "raw": content}


# ============ 阶段 2：执行 ============
async def _execute_one(subtask: dict, ctx: dict, tool_list: str, sender_id: str) -> dict:
    """
    执行一个子任务。返回 execution dict。
    """
    sub_id = subtask["id"]
    description = subtask.get("description", "")
    tool = subtask.get("tool")
    args = subtask.get("args", {}) or {}
    depends_on = subtask.get("depends_on", []) or []

    # 收集依赖前序的结果
    prior_lines = []
    for dep_id in depends_on:
        for ex in ctx["executions"]:
            if ex["id"] == dep_id:
                prior_lines.append(
                    f"[子任务 {dep_id}] {ex['description']}\n"
                    f"状态: {ex['status']}\n"
                    f"输出: {ex['output'][:1000]}"
                )
    prior_text = "\n\n".join(prior_lines) if prior_lines else "（无）"

    # 纯文本子任务：让执行员生成一段文本作为结果
    if tool is None:
        current_info = f"id={sub_id}, description={description}, tool=null"
        prompt = EXECUTOR_PROMPT.format(
            current=current_info, tool_list=tool_list, prior=prior_text
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "请针对当前纯文本子任务给出结果。"},
        ]
        try:
            content, reasoning = await _call_model("large_executor", messages, EXECUTOR_TIMEOUT)
        except Exception as e:
            return {
                "id": sub_id, "description": description, "status": "failed",
                "output": "", "error": str(e), "reasoning": "", "tool_raw": "",
            }
        return {
            "id": sub_id, "description": description, "status": "success",
            "output": content[:2000], "error": "", "reasoning": reasoning, "tool_raw": "",
        }

    # 有工具的子任务：执行员生成参数 -> 代码执行
    current_info = json.dumps(
        {"id": sub_id, "description": description, "tool": tool, "args": args},
        ensure_ascii=False,
    )
    prompt = EXECUTOR_PROMPT.format(
        current=current_info, tool_list=tool_list, prior=prior_text
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "请为当前子任务生成工具调用参数。"},
    ]

    try:
        content, reasoning = await _call_model("large_executor", messages, EXECUTOR_TIMEOUT)
    except Exception as e:
        return {
            "id": sub_id, "description": description, "status": "failed",
            "output": "", "error": f"执行员调用失败: {e}", "reasoning": "", "tool_raw": "",
        }

    parsed = _extract_json(content) or {}
    final_tool = parsed.get("tool") or tool
    final_args = parsed.get("args") or args

    from admin import execute_tool_directly
    try:
        output = await execute_tool_directly(final_tool, final_args, sender_id)
    except Exception as e:
        return {
            "id": sub_id, "description": description, "status": "failed",
            "output": "", "error": f"工具执行异常: {e}",
            "reasoning": reasoning, "tool_raw": content,
        }

    output_str = str(output)
    head = output_str[:50]
    is_failed = any(kw in head for kw in ("失败", "错误", "异常"))
    return {
        "id": sub_id, "description": description,
        "status": "failed" if is_failed else "success",
        "output": output_str[:2000],
        "error": output_str[:200] if is_failed else "",
        "reasoning": reasoning, "tool_raw": content,
    }


# ============ 阶段 4：总结 ============
async def _summarize(task_name: str, executions: list) -> str:
    lines = []
    for ex in executions:
        line = f"[{ex['id']}] {ex['description']}\n状态: {ex['status']}"
        if ex.get("error"):
            line += f"\n错误: {ex['error']}"
        line += f"\n输出: {ex['output'][:1500]}"
        lines.append(line)
    exec_text = "\n\n".join(lines)

    prompt = SUMMARIZER_PROMPT.format(task_name=task_name, executions=exec_text)
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "请汇报。"},
    ]
    try:
        content, _ = await _call_model("large_summarizer", messages, SUMMARIZER_TIMEOUT)
        return content.strip() or _fallback_summary(executions)
    except Exception as e:
        logger.error(f"[large-task] 总结失败: {e}")
        return _fallback_summary(executions)


def _fallback_summary(executions: list) -> str:
    lines = ["【大型任务执行结果】"]
    for ex in executions:
        mark = "" if ex["status"] == "success" else ""
        lines.append(f"{mark} 第 {ex['id']} 步: {ex['description']} — {ex['status']}")
        if ex.get("error"):
            lines.append(f"   错误: {ex['error']}")
    return "\n".join(lines)


# ============ 主流程 ============
async def run_large_task(
    user_input: str,
    user_id: str,
    notify: Callable[[str], Awaitable[None]],
) -> str:
    """
    大型任务主入口。
    """
    # 并发检查
    if user_id in _running:
        msg = "上一个大型任务执行中，请等待完成。"
        await notify(msg)
        return msg
    _running.add(user_id)

    start_ts = time.time()
    ctx = {"plan": None, "executions": [], "reasoning_log": []}
    _contexts[user_id] = ctx

    try:
        # ============ 阶段 1：规划 ============
        await notify("收到，规划中…")

        body = user_input[len("#大型"):].strip()
        if not body:
            msg = "请补充任务描述。"
            await notify(msg)
            return msg

        try:
            tool_list = _build_tool_list()
            plan_result = await _plan(body, tool_list)
        except Exception as e:
            msg = f"规划失败：{e}"
            await notify(msg)
            return msg

        plan = plan_result["plan"]
        ctx["plan"] = plan
        ctx["reasoning_log"].append({"stage": "plan", "reasoning": plan_result["reasoning"]})

        task_name = plan.get("task_name", "未命名任务")
        subtasks = plan.get("subtasks", [])
        if not subtasks:
            msg = "未能规划出可执行子任务。"
            await notify(msg)
            return msg
        if len(subtasks) > MAX_SUBTASKS:
            subtasks = subtasks[:MAX_SUBTASKS]

        total = len(subtasks)
        await notify(f"任务「{task_name}」，共 {total} 个子任务，开始执行。")

        # ============ 阶段 2 + 3：执行 ============
        failed_ids = set()
        chain_broken = False

        for idx, st in enumerate(subtasks, 1):
            # 整体超时
            if time.time() - start_ts > OVERALL_TIMEOUT:
                await notify(f"整体超时（>{OVERALL_TIMEOUT:.0f}s），中断。")
                break

            # 上下文溢出
            if _ctx_size(ctx) > CTX_TOKEN_KILL:
                await notify("上下文超出上限，中断。")
                break

            sub_id = st["id"]
            depends_on = st.get("depends_on", []) or []

            # 依赖检查
            dep_failed = any(dep_id in failed_ids for dep_id in depends_on)
            if chain_broken and depends_on:
                ex = {
                    "id": sub_id, "description": st.get("description", ""),
                    "status": "skipped", "output": "", "error": "前序链已断",
                    "reasoning": "", "tool_raw": "",
                }
                ctx["executions"].append(ex)
                await notify(f"第 {idx}/{total} 步跳过：{st.get('description', '')}")
                continue
            if dep_failed:
                ex = {
                    "id": sub_id, "description": st.get("description", ""),
                    "status": "skipped", "output": "", "error": "依赖子任务失败",
                    "reasoning": "", "tool_raw": "",
                }
                ctx["executions"].append(ex)
                await notify(f"第 {idx}/{total} 步跳过：{st.get('description', '')}")
                continue

            # 执行
            ex = await _execute_one(st, ctx, tool_list, user_id)
            ctx["executions"].append(ex)

            if ex["status"] == "success":
                await notify(f"第 {idx}/{total} 步：{ex['description']} 完成")
            else:
                failed_ids.add(sub_id)
                if depends_on:
                    chain_broken = True
                await notify(
                    f"第 {idx}/{total} 步：{ex['description']} 失败，"
                    f"原因：{ex['error'][:100]}"
                )

        # ============ 阶段 4：总结 ============
        final_report = await _summarize(task_name, ctx["executions"])
        await notify(final_report)
        return final_report

    finally:
        # ============ 阶段 5：清理 ============
        _contexts.pop(user_id, None)
        _running.discard(user_id)
