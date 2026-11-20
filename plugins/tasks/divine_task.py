"""
bot_utils/divine_task.py — 神授模式：自主循环 + 多轮决策

用户私聊发送「君权神授 <目标>」触发。AI 自主循环：
- 自己决定下一步做什么
- 自己判断何时结束
- 涉及文件操作时默认停下问用户
- 只在完成或必须停下时汇报

与 large_task.py 的关系：参考其上下文管理、notify 回调、日志格式；
架构不同（多轮循环 vs 单向流水线），独立模块，不复用。
"""
import asyncio
import re
import json
import logging
import time
from typing import Callable, Awaitable, Optional

import aiohttp

logger = logging.getLogger(__name__)
from config_center import get_config

# ============ 触发关键词 ============
TRIGGER = "君权神授"

# ============ 单轮上限 ============
MAX_STEPS = get_config("DIVINE_MAX_STEPS_PER_ROUND", 100)  # 单轮最大步数（中央控制台）
MAX_TIME = 3600.0            # 单轮最大时间（秒）

# ============ 整体上限（跨轮累加）============
TOTAL_MAX_STEPS = get_config("LARGE_TASK_TOTAL_MAX_STEPS", 500)  # 整体最大步数（中央控制台）
TOTAL_MAX_TIME = 10800.0         # 整体最大时间（秒，3 小时）
TOTAL_MAX_TOKENS = 1500000       # 整体最大 token 估算

# ============ 其他 ============
COMPRESS_THRESHOLD = 128000  # 上下文压缩阈值
HEARTBEAT_INTERVAL = 60.0   # 心跳间隔（秒）——[Chronos] 长任务心跳：每 1 分钟向飞书汇报进度

# ============ 单次调用超时 ============
EXEC_TIMEOUT = 180.0
SUMMARIZE_TIMEOUT = 60.0

# ============ 全局状态 ============
_contexts: dict[str, dict] = {}   # user_id -> ctx（运行中的）
_pending: dict[str, dict] = {}    # user_id -> {"ctx": ..., "reason": ...}（暂停的）
_running: set[str] = set()        # user_id -> 正在跑

# ============ [V2] 磁盘持久化 + 裁剪 + 熔断 ============
import os as _os
_CONTEXT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "workspace")
TOOL_RESULT_MAX = 2000      # 工具结果内联最大字符数
TOOL_RESULT_SUMMARY = 200   # 超长结果在上下文中保留的前缀摘要长度
FUSE_LIMIT = 3              # 连续失败熔断次数


def _ctx_path(user_id: str) -> str:
    return _os.path.join(_CONTEXT_DIR, f"divine_context_{user_id}.json")


def _save_ctx(user_id: str, ctx: dict) -> None:
    """增量持久化：完整 ctx 原子写入 workspace/divine_context_<user>.json"""
    try:
        _os.makedirs(_CONTEXT_DIR, exist_ok=True)
        data = {"user_id": user_id, "saved_at": time.time(), "ctx": ctx}
        tmp = _ctx_path(user_id) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        _os.replace(tmp, _ctx_path(user_id))
    except Exception as e:
        logger.warning("[divine] 上下文持久化失败: %s", e)


def _load_ctx(user_id: str) -> Optional[dict]:
    """读取磁盘上下文（进程重启后恢复）；无效/缺失返回 None"""
    try:
        p = _ctx_path(user_id)
        if not _os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        ctx = data.get("ctx")
        if not isinstance(ctx, dict) or not ctx.get("goal"):
            return None
        ctx.setdefault("tool_history", [])
        ctx.setdefault("executions", [])
        ctx.setdefault("messages", [])
        return ctx
    except Exception:
        return None


def _delete_ctx(user_id: str) -> None:
    try:
        p = _ctx_path(user_id)
        if _os.path.exists(p):
            _os.remove(p)
            logger.info("[divine] 上下文文件已清理: %s", p)
    except Exception:
        pass


def _trim_result(result, ctx: dict, tool: str) -> tuple:
    """工程级裁剪：超长结果转存 /tmp/，上下文只留前 200 字摘要 + 文件路径"""
    s = str(result)
    if len(s) <= TOOL_RESULT_MAX:
        return s, None
    path = f"/tmp/divine_result_{ctx.get('user_id','u')}_{len(ctx.get('executions',[]))+1}_{tool}.txt"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(s)
        inline = f"[结果过长（{len(s)} 字符），完整内容已转存 {path}]" + chr(10) + f"摘要（前{TOOL_RESULT_SUMMARY}字）：{s[:TOOL_RESULT_SUMMARY]}"
        logger.info("[divine] 结果超长裁剪: %d 字符 -> %s", len(s), path)
    except Exception:
        inline = s[:TOOL_RESULT_MAX]
    return inline, path


def _args_signature(args) -> str:
    """参数相似度签名：排序键 + 值前缀，稳定可比较"""
    try:
        return json.dumps(args or {}, sort_keys=True, ensure_ascii=False)[:200]
    except Exception:
        return str(args)[:200]


def _is_error_result(text) -> bool:
    if not text:
        return True
    low = str(text).lower()
    return any(k in low for k in ("失败", "error", "exception", "异常", "invalid", "错误", "超时", "not found"))


def _check_fuse(ctx: dict) -> Optional[str]:
    """熔断检测：连续 3 次同一工具 + 相同参数签名 + 均报错 → 返回熔断原因"""
    hist = ctx.get("tool_history", [])
    if len(hist) < FUSE_LIMIT:
        return None
    recent = hist[-FUSE_LIMIT:]
    if not all(h.get("error") for h in recent):
        return None
    if len({h.get("tool") for h in recent}) != 1:
        return None
    if len({h.get("sig") for h in recent}) != 1:
        return None
    tool = recent[-1]["tool"]
    return f"[熔断] 工具 {tool} 连续 {FUSE_LIMIT} 次调用失败（相同参数），已中断防死循环；最近参数：{recent[-1]['sig'][:100]}"


# ============ 底层模型调用（独立于 safe_api_request）============
async def _call_model(strategy: str, messages: list, timeout: float) -> str:
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
    return message.get("content", "") or ""


# ============ token 估算 ============
def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return len(text) * 2


def _ctx_size(ctx: dict) -> int:
    total = _estimate_tokens(ctx.get("goal", ""))
    for m in ctx.get("messages", []):
        total += _estimate_tokens(m.get("content", ""))
    for ex in ctx.get("executions", []):
        total += _estimate_tokens(json.dumps(ex, ensure_ascii=False))
    return total


# ============ 提示词 ============
DIVINE_SYSTEM_PROMPT = """你是神授模式的自主执行者。用户已授予你在本次任务中自主决策、连续调用工具的权限。

【你的职责】
- 自主判断下一步该做什么
- 每轮返回一个 JSON 决策
- 任务完成时主动结束

【工具调用】
从系统提供的可用工具清单中选择工具并传参。每轮只能选一个动作，不要输出多个工具调用。

【危险判定 · 必须严格遵守】
以下操作必须先走 ask_user，除非用户在原始目标中明确说出了文件/目录的名字：
1. 删除任何文件（无论什么类型）
2. 修改任何文件的内容
3. rm -rf 任何目录
4. 修改 .env / models.json / profile.md

判断"用户是否点名"：
- 用户说"删掉 analyze_scores.py" → 点名了，可以直接执行
- 用户说"删掉日志文件" → 没点名，必须 ask_user 并列出候选文件
- 用户说"清理缓存" → 没点名，必须 ask_user

非文件类操作（读取文件、执行只读命令、查询系统状态、ping、读目录）可以直接执行，不需要 ask_user。

【输出格式】
严格输出 JSON，不要任何解释、寒暄、Markdown 代码块围栏。

完成任务：
{"done": true, "reason": "任务完成说明", "action": null}

继续执行：
{"done": false, "reason": "当前状态和下一步意图", "action": {"type": "tool", "tool": "工具名", "args": {"参数名": "值"}}}

需要用户确认：
{"done": false, "reason": "为什么需要确认", "action": {"type": "ask_user", "question": "要向用户提的问题（要具体，比如列出候选文件让用户选）"}}

【注意】
- 如果判断任务已完成，必须 done=true
- 如果用户回复明显不是在回答你的问题、或表达终止意愿，必须 done=true 结束
- reason 字段要简短，一句话说明当前状态
"""

SUMMARIZER_PROMPT = """你是汇报助手。用户用神授模式执行了一个任务，现在生成最终汇报。你是 AI。

【原始目标】
{goal}

【结束原因】
{reason}

【执行记录】
{executions}

【输出要求】
生成一段中文汇报，包含：
- 任务名（从原始目标提取）
- 执行了哪些关键步骤
- 最终状态（完成 / 中断 / 失败）

不要使用 Markdown 代码块围栏。不要添加额外解释。
"""


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
    except (json.JSONDecodeError, ValueError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            pass
    return None


# ============ 模型单轮调用 ============
async def _call_model_once(ctx: dict) -> str:
    from admin import TOOLS
    tool_lines = []
    for name, info in TOOLS.items():
        func = info.get("func")
        doc = (func.__doc__ or "").strip().split("\n")[0] if func else "无描述"
        tool_lines.append(f"- {name}: {doc}")
    tool_list = "\n".join(tool_lines)

    messages = [
        {"role": "system", "content": DIVINE_SYSTEM_PROMPT},
        {"role": "system", "content": f"【可用工具清单】\n{tool_list}"},
        {"role": "system", "content": "【执行纪律】下载/爬取视频类任务：直接调用 download_video_by_url（cookie 与反爬已内置处理，无需先访问页面、无需检查凭证、无需过 JS challenge）；其他需求按可用工具执行，不要做多余的环境排查。"},
        {"role": "system", "content": f"【原始目标】\n{ctx['goal']}\n（目标中的别名已物理解析为完整数据，请直接使用，无需再猜测缩写含义）"},
    ]
    messages.extend(ctx.get("messages", []))
    if not ctx.get("messages"):
        messages.append({"role": "user", "content": "开始执行。"})

    return await _call_model("divine", messages, EXEC_TIMEOUT)


# ============ 上下文压缩 ============
async def _compress_context(ctx: dict) -> None:
    """把早期执行结果压缩成摘要，保留最近 5 条完整"""
    executions = ctx.get("executions", [])
    if len(executions) <= 5:
        return
    early = executions[:-5]
    recent = executions[-5:]

    lines = []
    for ex in early:
        lines.append(f"[{ex['step']}] {ex['tool']}: {ex['result'][:500]}")
    early_text = "\n".join(lines)

    prompt = f"""请把以下执行记录压缩成简洁摘要，保留关键信息（操作对象、结果、异常）。

{early_text}

只输出摘要内容，不要其他文字。"""

    try:
        summary = await _call_model("large_summarizer", [
            {"role": "system", "content": "你是数据压缩助手。只输出摘要。"},
            {"role": "user", "content": prompt},
        ], SUMMARIZE_TIMEOUT)
    except Exception as e:
        logger.error(f"[divine] 压缩失败: {e}")
        # 压缩失败时只截断最早的记录
        ctx["executions"] = recent
        return

    # 把摘要作为一条"伪执行记录"插到最前
    summary_ex = {
        "step": 0,
        "tool": "[早期摘要]",
        "args": {},
        "result": summary.strip()[:3000],
        "ts": early[0]["ts"] if early else time.time(),
    }
    ctx["executions"] = [summary_ex] + recent
    logger.info(f"[divine] 上下文已压缩：{len(early)} 条 → 摘要")


# ============ 上限检查 ============
def _check_limits(ctx: dict) -> tuple[Optional[str], bool]:
    """返回 (中断原因, 是否终止)。
    - (None, False)：未超限
    - (reason, False)：单轮超限，可暂停恢复
    - (reason, True)：整体超限，直接终止
    """
    now = time.time()
    total_steps = len(ctx["executions"])
    round_steps = total_steps - ctx["round_start_step"]
    total_time = now - ctx["total_start_ts"]
    round_time = now - ctx["round_start_ts"]

    # 步数上限每次现读中央配置，覆盖层改动不重启即生效
    max_steps = get_config("DIVINE_MAX_STEPS_PER_ROUND", 100)
    total_max_steps = get_config("LARGE_TASK_TOTAL_MAX_STEPS", 500)

    # 单轮（可暂停）
    if round_steps >= max_steps:
        return f"本轮已达最大步数 {max_steps}（整体 {total_steps} 步）", False
    if round_time >= MAX_TIME:
        return f"本轮已达最大时间 {int(MAX_TIME)} 秒（整体 {int(total_time)} 秒）", False

    # 整体（终止）
    if total_steps >= total_max_steps:
        return f"整体任务已达最大步数 {total_max_steps}", True
    if total_time >= TOTAL_MAX_TIME:
        return f"整体任务已达最大时间 {int(TOTAL_MAX_TIME)} 秒", True
    if _ctx_size(ctx) >= TOTAL_MAX_TOKENS:
        return f"整体任务已达最大 token 估算 {TOTAL_MAX_TOKENS}", True

    return None, False

# ============ 汇报 ============
async def _finalize_with_summary(user_id: str, ctx: dict, notify, reason: str) -> str:
    exec_lines = []
    for ex in ctx.get("executions", []):
        exec_lines.append(f"[{ex['step']}] {ex['tool']}: {ex['result'][:300]}")
    exec_text = "\n".join(exec_lines) if exec_lines else "（无执行记录）"

    prompt = SUMMARIZER_PROMPT.format(
        goal=ctx["goal"], reason=reason or "任务完成", executions=exec_text,
    )
    try:
        content = await _call_model("large_summarizer", [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "请汇报。"},
        ], SUMMARIZE_TIMEOUT)
        summary = content.strip() or _fallback_summary(ctx, reason)
    except Exception as e:
        logger.error(f"[divine] 汇报失败: {e}")
        summary = _fallback_summary(ctx, reason)

    _contexts.pop(user_id, None)
    _pending.pop(user_id, None)
    _delete_ctx(user_id)  # [V2] 任务结束，清理磁盘持久化
    return summary


def _fallback_summary(ctx: dict, reason: str) -> str:
    lines = [f"【神授·结束】{ctx['goal'][:100]}"]
    if reason:
        lines.append(f"结束原因：{reason}")
    lines.append(f"执行了 {len(ctx.get('executions', []))} 步")
    for ex in ctx.get("executions", [])[-5:]:
        lines.append(f"- [{ex['step']}] {ex['tool']}")
    return "\n".join(lines)


# ============ Execution Guard：交付强校验（只找链接不干活 → 强制重试/中断） ============
_FILE_NEED_KWS = ("爬取", "下载", "获取文件", "抓取", "保存到", "保存为")
_FILE_PATH_RE = re.compile(r"(?:/root/robot/downloads/|/root/robot/[\w\-/]*/|/tmp/)[\w\-/]+\.(?:mp4|ts|csv|xlsx|xls|png|jpg|pdf|txt|zip|json)\b")
_GUARD_RETRY_MSG = "任务未完成！你只找到了信息，未交付实际文件（{reason}）。请立刻调用工具下载/保存文件，并返回本地绝对路径。"
_GUARD_MAX_RETRY = 2


def _check_final_deliverable(ctx: dict) -> Optional[str]:
    """目标需要交付文件，但执行记录中无本地文件路径 → 返回拦截原因；否则返回 None（放行）。"""
    goal = ctx.get("goal", "")
    if not any(kw in goal for kw in _FILE_NEED_KWS):
        return None  # 非文件交付类目标，不拦截
    for ex in ctx.get("executions", []):
        result = str(ex.get("result", ""))
        stored = str(ex.get("stored_path", "") or "")
        tool = str(ex.get("tool", ""))
        combined = result + " " + stored
        # 本地绝对文件路径命中 → 已交付
        if _FILE_PATH_RE.search(combined):
            return None
        # 下载类宏工具已成功执行（结果非错误）→ 视为已交付
        if ("download" in tool) and not _is_error_result(result):
            return None
    return "执行记录中无本地文件路径（未下载/未保存任何文件）"


# ============ 主循环 ============
async def _run_loop(user_id: str, ctx: dict, notify) -> str:
    while True:
        # 1. 上限检查
        limit_reason, is_terminal = _check_limits(ctx)
        if limit_reason:
            if is_terminal:
                return await _finalize_with_summary(
                    user_id, ctx, notify, limit_reason
                )
            _pending[user_id] = {"ctx": ctx, "reason": limit_reason}
            return f"神授模式已暂停：{limit_reason}\n回复以继续执行，或回复其他指令。"

        # 2. 上下文压缩
        if _ctx_size(ctx) >= COMPRESS_THRESHOLD:
            await _compress_context(ctx)
            if _ctx_size(ctx) >= MAX_TOKENS:
                _pending[user_id] = {"ctx": ctx, "reason": "压缩后仍超 token 上限"}
                return "神授模式已暂停：上下文超限，压缩后仍超。"

        # 3. 心跳
        _hb = get_config("TASK_HEARTBEAT_INTERVAL", 60.0)
        if time.time() - ctx["last_notify_ts"] >= _hb:
            await notify(
                f"[神授·心跳] 已完成 {len(ctx['executions'])} 步，"
                f"运行 {int(time.time() - ctx['start_ts'])} 秒"
            )
            ctx["last_notify_ts"] = time.time()

        # 4. 调用模型（失败重试一次）
        reply = None
        for attempt in (1, 2):
            try:
                reply = await _call_model_once(ctx)
                break
            except Exception as e:
                logger.warning(f"[divine] 模型调用失败（第 {attempt} 次）: {e}")
                if attempt == 2:
                    return await _finalize_with_summary(
                        user_id, ctx, notify, f"模型调用失败: {e}"
                    )

        # 5. 解析决策（失败重试一次）
        decision = _extract_json(reply)
        if decision is None:
            try:
                reply = await _call_model_once(ctx)
                decision = _extract_json(reply)
            except Exception:
                pass

        if decision is None:
            return await _finalize_with_summary(
                user_id, ctx, notify, "模型输出无法解析为 JSON"
            )

        # 6. done?
        if decision.get("done") is True:
            # ===== [Execution Guard] 交付强校验：只找链接不干活 → 强制重试，2 次后中断 =====
            guard_reason = _check_final_deliverable(ctx)
            if guard_reason:
                ctx["guard_retries"] = ctx.get("guard_retries", 0) + 1
                if ctx["guard_retries"] >= _GUARD_MAX_RETRY:
                    return await _finalize_with_summary(
                        user_id, ctx, notify,
                        f"任务失败：连续 {ctx['guard_retries']} 次未交付实际文件（{guard_reason}）"
                    )
                retry_msg = _GUARD_RETRY_MSG.format(reason=guard_reason)
                ctx["messages"].append({"role": "user", "content": retry_msg})
                logger.warning(
                    f"[divine] ExecutionGuard 强制重试（{ctx['guard_retries']}/{_GUARD_MAX_RETRY}）：{ctx['goal'][:60]}"
                )
                continue
            return await _finalize_with_summary(
                user_id, ctx, notify, decision.get("reason", "任务完成")
            )

        # 7. 处理 action
        action = decision.get("action") or {}
        action_type = action.get("type")

        if action_type == "ask_user":
            question = action.get("question", "需要你确认")
            _pending[user_id] = {"ctx": ctx, "reason": "AI 请求确认"}
            return f"❔ {question}\n（请回复以继续）"

        if action_type == "tool":
            tool_name = action.get("tool")
            tool_args = action.get("args", {}) or {}
            exec_error = False
            try:
                from admin import execute_tool_directly
                result = await execute_tool_directly(tool_name, tool_args, user_id)
            except Exception as e:
                result = f"工具执行异常: {e}"
                exec_error = True

            result_str = str(result)
            exec_error = exec_error or _is_error_result(result_str)
            # [V2] 熔断记录
            ctx.setdefault("tool_history", []).append({
                "tool": tool_name, "sig": _args_signature(tool_args),
                "error": exec_error, "ts": time.time(),
            })
            fuse_reason = _check_fuse(ctx)
            if fuse_reason:
                return await _finalize_with_summary(user_id, ctx, notify, fuse_reason)

            # [V2] 工程级裁剪：超长转存 /tmp/，上下文留摘要+路径
            inline_result, _stored_path = _trim_result(result_str, ctx, tool_name)
            ctx["executions"].append({
                "step": len(ctx["executions"]) + 1,
                "tool": tool_name,
                "args": tool_args,
                "result": inline_result,
                "stored_path": _stored_path,
                "ts": time.time(),
            })
            ctx["messages"].append({
                "role": "user",
                "content": (
                    f"【上一步结果】\n"
                    f"工具：{tool_name}\n"
                    f"参数：{json.dumps(tool_args, ensure_ascii=False)}\n"
                    f"结果：\n{inline_result}"
                ),
            })
            # [V2] 每轮增量持久化（跨会话恢复）
            _save_ctx(user_id, ctx)
            continue

        # 未知 action 类型
        return await _finalize_with_summary(
            user_id, ctx, notify, f"未知 action 类型: {action_type}"
        )


# ============ 取消意图判断 ============
def _is_cancel_intent(text: str) -> bool:
    t = text.strip().lower()
    return any(kw in t for kw in ("取消", "退出", "算了", "不做了", "停止", "终止"))


# ============ 对外接口 ============
async def run_divine_task(
    user_input: str,
    user_id: str,
    notify: Callable[[str], Awaitable[None]],
) -> str:
    if user_id in _running:
        msg = "上一个神授任务执行中，请等待完成。"
        await notify(msg)
        return msg
    if user_id in _pending:
        msg = "你有一个暂停的神授任务，请先回复它。"
        await notify(msg)
        return msg

    goal = user_input[len(TRIGGER):].strip()
    if not goal:
        msg = "请补充任务描述。"
        await notify(msg)
        return msg

    # [V2] 统一数据预处理：别名物理解析为完整数据（'tz 99032' → 完整 URL）
    from alias_utils import resolve_aliases_in_text
    raw_goal = goal
    goal = resolve_aliases_in_text(goal)

    # [V2] 磁盘恢复：进程重启后，优先读取上次未完成任务继续执行
    disk_ctx = _load_ctx(user_id)
    if disk_ctx is not None:
        ctx = disk_ctx
        ctx["messages"].append({
            "role": "user",
            "content": f"【用户重启后发送】\n{goal or '继续'}",
        })
        ctx["last_notify_ts"] = time.time()
        ctx["round_start_step"] = len(ctx["executions"])
        ctx["round_start_ts"] = time.time()
        _contexts[user_id] = ctx
        _running.add(user_id)
        await notify(
            f"【神授】检测到上次未完成任务（已执行 {len(ctx['executions'])} 步），正在恢复执行。"
        )
        try:
            result = await _run_loop(user_id, ctx, notify)
            await notify(result)
            return result
        finally:
            _running.discard(user_id)
            _contexts.pop(user_id, None)

    ctx = {
        "user_id": user_id,
        "goal": goal,
        "raw_goal": raw_goal,
        "messages": [],
        "executions": [],
        "tool_history": [],
        "start_ts": time.time(),               # 整体开始时间（用于心跳）
        "last_notify_ts": time.time(),         # 心跳时间戳
        # ===== 累加记录字段 =====
        "round_start_step": 0,                 # 本轮开始时的步数
        "round_start_ts": time.time(),         # 本轮开始时间
        "total_start_ts": time.time(),         # 整体开始时间
    }
    if goal != raw_goal:
        logger.info("[divine] 别名净化: '%s' -> %s", raw_goal[:80], goal[:120])
    _contexts[user_id] = ctx
    _running.add(user_id)
    _save_ctx(user_id, ctx)  # [V2] 初始化即持久化

    await notify(f"【神授】开始执行：{goal[:100]}")

    try:
        result = await _run_loop(user_id, ctx, notify)
        await notify(result)
        return result
    finally:
        _running.discard(user_id)
        _contexts.pop(user_id, None)


def is_pending(user_id: str) -> bool:
    return user_id in _pending


def clear_pending(user_id: str) -> None:
    _pending.pop(user_id, None)


async def resume_divine_task(
    user_id: str,
    user_input: str,
    notify: Callable[[str], Awaitable[None]],
) -> str:
    pending = _pending.pop(user_id, None)
    if not pending:
        return ""

    ctx = pending["ctx"]

    # 用户表达终止意愿
    if _is_cancel_intent(user_input):
        ctx["messages"].append({
            "role": "user",
            "content": f"【用户在暂停后回复】\n{user_input}\n（用户已表达终止意愿）",
        })
        result = await _finalize_with_summary(user_id, ctx, notify, "用户主动终止")
        await notify(result)
        return result

    # 正常回复，注入上下文
    ctx["messages"].append({
        "role": "user",
        "content": f"【用户在暂停后回复】\n{user_input}",
    })
    ctx["last_notify_ts"] = time.time()
    # ===== 重置单轮上限计数（累加记录保留）=====
    ctx["round_start_step"] = len(ctx["executions"])
    ctx["round_start_ts"] = time.time()

    if user_id in _running:
        return ""

    _running.add(user_id)
    _contexts[user_id] = ctx
    try:
        result = await _run_loop(user_id, ctx, notify)
        await notify(result)
        return result
    finally:
        _running.discard(user_id)
        _contexts.pop(user_id, None)
