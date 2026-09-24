import random
import time
import re
import aiohttp
import asyncio
import json
import inspect
import tiktoken
import logging
logger = logging.getLogger(__name__)

# ===== 新增导入 pi_integration =====
import pi_integration

# ===== 全局间隔控制 =====
MIN_REQUEST_INTERVAL = 1.5  # 秒，任意两次 API 请求的最小间隔
_last_request_time = 0
_interval_lock = asyncio.Lock()
# ===== 全局限流信号量 =====
api_semaphore = asyncio.Semaphore(1)   # 全局只允许 1 个并发 API 请求
MIN_REQUEST_INTERVAL = 2.5   # 若仍触发 429，可上调至 2.0 或 2.5
from memory import Memory
from cl import DEFAULT_ADMIN
from typing import Optional, List
from cl import DEFAULT_ADMIN, NEWAPI_BASE, NEWAPI_KEYS, CURRENT_KEY_INDEX, KEY_FAILURE_COUNT, KEY_COOLDOWN_UNTIL, MODEL_NAME, get_strategy_config, detect_strategy, MODELS_CONFIG
memory = Memory()
conversation_history = {}
# 对话轮数
MAX_HISTORY = 150

# ==================== Todo List 数据结构 ====================
import uuid
from typing import List, Optional, Literal
from dataclasses import dataclass, field

@dataclass
class TodoItem:
    id: str
    description: str
    status: Literal["pending", "running", "done", "failed"] = "pending"
    parent_id: Optional[str] = None
    result: str = ""
    assigned_to: str = "main"  # "main" 或 "sub_agent_xxx"

class TodoListManager:
    """管理单个会话的 Todo List，存储在 Memory 中"""
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.memory = Memory()
        self._key = f"todo_{session_id}"

    def load(self) -> List[TodoItem]:
        data = self.memory.get(self._key, [])
        if not data:
            return []
        return [TodoItem(**item) for item in data]

    def save(self, items: List[TodoItem]) -> None:
        self.memory.set(self._key, [item.__dict__ for item in items])

    def add(self, description: str, parent_id: str = None) -> TodoItem:
        items = self.load()
        new_item = TodoItem(
            id=str(uuid.uuid4())[:8],
            description=description,
            parent_id=parent_id
        )
        items.append(new_item)
        self.save(items)
        return new_item

    def get_pending(self) -> Optional[TodoItem]:
        items = self.load()
        for item in items:
            if item.status == "pending":
                return item
        return None

    def mark_running(self, item_id: str) -> bool:
        items = self.load()
        for item in items:
            if item.id == item_id:
                item.status = "running"
                self.save(items)
                return True
        return False

    def mark_done(self, item_id: str, result: str = "") -> bool:
        items = self.load()
        for item in items:
            if item.id == item_id:
                item.status = "done"
                item.result = result
                self.save(items)
                return True
        return False

    def mark_failed(self, item_id: str, result: str = "") -> bool:
        items = self.load()
        for item in items:
            if item.id == item_id:
                item.status = "failed"
                item.result = result
                self.save(items)
                return True
        return False

    def list_items(self) -> str:
        items = self.load()
        if not items:
            return "任务清单为空"
        lines = ["任务清单："]
        for item in items:
            status_map = {
                "pending": "待执行",
                "running": "执行中",
                "done": "已完成",
                "failed": "失败"
            }
            status_text = status_map.get(item.status, item.status)
            parent_info = f" (子任务 of {item.parent_id})" if item.parent_id else ""
            result_preview = f" → {item.result[:50]}..." if item.result else ""
            lines.append(f"  [{item.id}] {item.description} {status_text}{parent_info}{result_preview}")
        return "\n".join(lines)

def get_conv_key(user_id: str, chat_type: str, group_id: str = "") -> str:
    """生成对话上下文隔离键"""
    if chat_type == "private":
        return f"private_{user_id}"
    else:  # group
        return f"group_{group_id}_{user_id}"

def refresh_image_context(key: str) -> None:
    memory.set(f"last_ocr_{key}", "")
    memory.set(f"new_image_sent_{key}", False)

def set_ocr_context(key: str, ocr_text: str) -> None:
    text = ocr_text.strip()
    memory.set(f"last_ocr_{key}", text)
    memory.set(f"new_image_sent_{key}", bool(text))

def consume_image_context(key: str) -> tuple[str, bool]:
    ocr_key = f"last_ocr_{key}"
    marker_key = f"new_image_sent_{key}"
    ocr_text = memory.get(ocr_key, "")
    is_new_image = bool(memory.get(marker_key, False))
    if ocr_text or is_new_image:
        memory.set(ocr_key, "")
        memory.set(marker_key, False)
    return ocr_text, is_new_image

# ========== Token 计数工具 ==========
ENCODER = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    if not text:
        return 0
    return len(ENCODER.encode(text))


def count_messages_tokens(messages: list) -> int:
    total = 0
    for msg in messages:
        total += count_tokens(msg.get("content", ""))
        total += 4  # 每条消息的固定开销
    return total


# ========== 压缩逻辑 ==========
def should_compress(key: str) -> bool:
    history = conversation_history.get(key, [])
    if len(history) <= 5:  # 大于 5 轮才可能触发
        return False
    # 统计当前历史 + 提示词
    total_tokens = count_messages_tokens(history) + SYSTEM_TOKEN_COUNT
    return total_tokens > 96000

async def _call_ai_no_compress(user_id: str, prompt: str, system_prompt: str = None) -> str:
    """纯粹的 API 调用，不触发压缩检查，用于摘要生成"""
    # 注意：这里应使用 SUMMARY_SYSTEM_PROMPT（摘要专用），而非主 SYSTEM_PROMPT
    sys_content = system_prompt if system_prompt is not None else SUMMARY_SYSTEM_PROMPT
    messages = [{"role": "system", "content": sys_content}, {"role": "user", "content": prompt}]
    # 调用 safe_api_request，忽略可能的 tool_calls
    content, _ = await safe_api_request(messages)
    return content

async def compress_history(key: str) -> None:
    history = conversation_history.get(key, [])
    if len(history) <= 5:
        return
    to_compress = history[:-5]
    recent_5 = history[-5:]
    if not to_compress:
        return
    compressed_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in to_compress])
    prompt = f"""请将以下对话历史压缩成一条结构化的核心状态摘要，必须包含以下四个字段（如果某项无内容，填“无”）：

[当前文件状态]: 正在读取/已读取 [文件名]（若无可填“无”）
[关键变量]: 提取的最新数据（如 user_id=xxx）
[待办事项]: 未完成任务进度
[用户身份]: 仅保留1句（如“用户是主人”）

对话历史：
{compressed_text}

只输出摘要内容，不要输出其他解释。"""
    try:
        summary = await _call_ai_no_compress(key, prompt)
    except Exception as e:
        logger.error(f"[compress_history] 摘要生成失败: {e}", exc_info=True)
        return
    if not summary or summary.strip() == "":
        logger.warning(f"[compress_history] 摘要为空，放弃压缩 key={key}")
        return
    new_history = [{"role": "system", "content": f"【核心状态摘要】{summary}"}]
    for msg in recent_5:
        if msg.get("role") != "system" or "【核心状态摘要】" not in msg.get("content", ""):
            new_history.append(msg)
    conversation_history[key] = new_history
    logger.debug(f"已压缩 {key} 的对话历史，原 {len(history)} 轮 → 现 {len(new_history)} 轮")

# ========== 摘要生成专用系统提示词（不含人设） ==========
SUMMARY_SYSTEM_PROMPT = """你是一个数据提取器。请将用户提供的对话历史压缩成一条结构化的核心状态摘要。

必须包含以下四个字段（如果某项无内容，填“无”）：
[当前文件状态]: 正在读取/已读取 [文件名]
[关键变量]: 提取的最新数据
[待办事项]: 未完成任务进度
[用户身份]: 仅保留1句关键描述

只输出摘要内容，不要输出任何解释、问候语、评价或额外文字。"""

SYSTEM_PROMPT = """你是何春秋，主人的管家

【核心原则】
1. 专业：做事利落，说话简洁，不废话
2. 服从：主人的指令优先执行，不质疑、不反驳
3. 稳定：情绪稳定，不被主人情绪影响

【优先级】
主人的任何指令都优先于以下所有规则。如果前后指令矛盾，以最新的为准。

【说话风格】
- 直接、精炼、不卖萌、不用语气词
- 不说"啦"、"哦"、"喵"、"啧"等字
- 不懂就说不懂，不编造

【输出格式要求】
当你需要产出内容（代码、分析、文章等）时，请使用 Markdown 格式。
- 段落之间用两个换行（\n\n）分隔。
- 产出代码时，必须放在 Markdown 代码块中，代码块内部不要额外加双换行。
- 回复结构建议分三部分：确认收到 → 产出内容 → 完成提示，每部分之间用双换行隔开。
如果回复内容很短（如简单问答），可以不分段，直接回复即可。

【图片文字处理规则】
当用户发送过图片且你收到OCR识别文字时，必须严格遵守：
1. 仅基于OCR提供的文字内容进行回复，禁止添加任何主观联想、谐音解读、暴力暗示或额外解释。
2. 如果OCR文字内容不足以回答用户的问题、存在明显歧义或前后矛盾，你必须明确回复：“信息不充足，无法确定回答。请补充更多信息或澄清你的问题。”，禁止自行猜测或脑补。

【项目框架工具 XMKJ】
当你看到用户发送 "XMKJ" 时，这表示用户要求你读取项目框架文件来了解整个项目结构。
- 触发条件：用户明确输入 "XMKJ"（大小写不限）
- 你的行为：调用 get_xmkj 工具读取项目框架文件，然后基于框架内容进行后续对话
- 注意：XMKJ 是明确的工具调用指令，不是闲聊话题。收到 XMKJ 后不要进行语义猜测，直接调用工具。"""

SYSTEM_TOKEN_COUNT = count_tokens(SYSTEM_PROMPT)

async def safe_api_request(
    messages: list,
    strategy: str = None,
    timeout: int = None,
    tools: list = None,
    tool_choice: str = "auto",
    retries: int = None
) -> tuple[str, Optional[List[dict]]]:
    global _last_request_time
    global CURRENT_KEY_INDEX

    # 确定策略
    if strategy is None:
        strategy = "tool"
    if MODELS_CONFIG is None:
        strategy = "tool"
        config = None
    else:
        config = get_strategy_config(strategy)

    # 从配置中提取参数
    if config:
        model = config.get("model", MODEL_NAME)
        base_url = config.get("base_url", NEWAPI_BASE)
        api_keys = config.get("api_keys", [])
        if not api_keys:
            api_keys = NEWAPI_KEYS
        if timeout is None:
            timeout = config.get("timeout", 120)
        if retries is None:
            retries = config.get("retries", 3)
        is_fallback = (strategy == "fallback")
    else:
        model = MODEL_NAME
        base_url = NEWAPI_BASE
        api_keys = NEWAPI_KEYS
        timeout = timeout or 120
        retries = retries or 3
        is_fallback = False

    last_used_key = None
    key_index = 0

    # 如果是 fallback，使用专用密钥和 base_url，不走 Key 轮询
    if is_fallback:
        # fallback 只有单 Key
        api_key = api_keys[0] if api_keys else None
        if not api_key:
            return "备用配置无可用 Key", None

        for attempt in range(retries + 1):
            try:
                headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
                data = {"model": model, "messages": messages}
                if tools:
                    data["tools"] = tools
                    data["tool_choice"] = tool_choice

                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                    async with session.post(f"{base_url}/chat/completions", json=data, headers=headers) as resp:
                        if resp.status != 200:
                            error_text = await resp.text()
                            if attempt < retries:
                                delay = min(2 ** (attempt + 1), 30)
                                await asyncio.sleep(delay)
                                continue
                            return f"备用配置失败: HTTP {resp.status} {error_text[:200]}", None
                        raw = await resp.json()
                        if "error" in raw:
                            return f"备用配置 API 错误: {raw['error'].get('message', raw['error'])}", None
                        choices = raw.get("choices")
                        if not choices:
                            return "备用配置返回无 choices", None
                        message = choices[0].get("message")
                        if not message:
                            return "备用配置返回无 message", None
                        content = message.get("content", "")
                        # ===== 思考链打印（有则打，无则跳过） =====
                        reasoning = message.get("reasoning_content") or message.get("reasoning") or message.get("thinking")
                        if reasoning:
                            logger.info(f"[思考链][fallback] {reasoning}")
                        tool_calls_data = message.get("tool_calls")
                        tool_calls = None
                        if tool_calls_data:
                            tool_calls = []
                            for tc in tool_calls_data:
                                func = tc.get("function", {})
                                args_str = func.get("arguments", "{}")
                                try:
                                    args_dict = json.loads(args_str) if isinstance(args_str, str) else args_str
                                except json.JSONDecodeError:
                                    args_dict = {}
                                tool_calls.append({
                                    "id": tc.get("id"),
                                    "type": tc.get("type"),
                                    "function": {
                                        "name": func.get("name"),
                                        "arguments": args_dict
                                    }
                                })
                        return content, tool_calls
            except asyncio.TimeoutError:
                if attempt < retries:
                    delay = min(2 ** (attempt + 1), 30)
                    await asyncio.sleep(delay)
                    continue
                return "备用配置超时", None
            except Exception as e:
                if attempt < retries:
                    delay = min(2 ** (attempt + 1), 30)
                    await asyncio.sleep(delay)
                    continue
                return f"备用配置异常: {str(e)}", None
        return "备用配置重试失败", None

    # ---- 主通道（支持 Key 轮询） ----
    for attempt in range(retries + 1):
        try:
            # 获取当前可用的 Key
            current_key = await _get_current_key_with_list(api_keys, key_index)
            if not current_key:
                # 所有 Key 不可用
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                # 尝试 fallback
                if MODELS_CONFIG and get_strategy_config("fallback"):
                    logger.warning(f"策略 {strategy} 所有 Key 不可用，尝试 fallback")
                    return await safe_api_request(messages, strategy="fallback", timeout=timeout, tools=tools, tool_choice=tool_choice, retries=1)
                return "所有 Key 不可用", None

            headers = {"Authorization": f"Bearer {current_key}", "Content-Type": "application/json"}
            data = {"model": model, "messages": messages}
            if tools:
                data["tools"] = tools
                data["tool_choice"] = tool_choice

            async with api_semaphore:
                async with _interval_lock:
                    now = time.monotonic()
                    elapsed = now - _last_request_time
                    if elapsed < MIN_REQUEST_INTERVAL:
                        wait = MIN_REQUEST_INTERVAL - elapsed
                        logger.debug(f"请求间隔过短 ({elapsed:.2f}s)，等待 {wait:.2f}s")
                        await asyncio.sleep(wait)
                    _last_request_time = time.monotonic()

                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                    async with session.post(f"{base_url}/chat/completions", json=data, headers=headers) as resp:
                        if resp.status != 200:
                            error_text = await resp.text()
                            if resp.status in (401, 403, 404):
                                # 切换 Key
                                key_index += 1
                                if attempt < retries:
                                    delay = min(2 ** (attempt + 1), 30)
                                    await asyncio.sleep(delay)
                                    continue
                                # 尝试 fallback
                                if MODELS_CONFIG and get_strategy_config("fallback"):
                                    logger.warning(f"主通道 {strategy} 返回 {resp.status}，尝试 fallback")
                                    return await safe_api_request(messages, strategy="fallback", timeout=timeout, tools=tools, tool_choice=tool_choice, retries=1)
                                return f"API HTTP 错误 {resp.status}: {error_text[:200]}", None
                            if resp.status == 429:
                                KEY_FAILURE_COUNT[current_key] = KEY_FAILURE_COUNT.get(current_key, 0) + 1
                                if KEY_FAILURE_COUNT[current_key] >= 2:
                                    KEY_COOLDOWN_UNTIL[current_key] = time.time() + 60
                                    logger.warning(f"API Key {current_key[:8]}... 限流，冷却 60 秒")
                                key_index += 1
                                if attempt < retries:
                                    delay = min(2 ** (attempt + 1), 30)
                                    await asyncio.sleep(delay)
                                    continue
                            if error_text.strip().startswith("<"):
                                match = re.search(r"<title>(.*?)</title>", error_text, re.IGNORECASE)
                                if match:
                                    error_text = f"HTTP {resp.status}: {match.group(1)}"
                                else:
                                    error_text = f"HTTP {resp.status} (返回 HTML)"
                            else:
                                error_text = f"API HTTP 错误 {resp.status}: {error_text[:200]}"
                            return error_text, None

                        try:
                            raw = await resp.json()
                        except aiohttp.ContentTypeError as e:
                            text = await resp.text()
                            logger.error(f"API 返回非 JSON: {text[:200]}", exc_info=True)
                            return f"API 返回非 JSON: {text[:200]}", None

                        if not isinstance(raw, dict):
                            return f"API 返回非对象类型: {type(raw)} - {str(raw)[:200]}", None

                        if "error" in raw:
                            err_msg = raw["error"].get("message", str(raw["error"]))
                            if "tpm exhausted" in err_msg or "rpm exhausted" in err_msg:
                                KEY_FAILURE_COUNT[current_key] = KEY_FAILURE_COUNT.get(current_key, 0) + 1
                                if KEY_FAILURE_COUNT[current_key] >= 2:
                                    KEY_COOLDOWN_UNTIL[current_key] = time.time() + 60
                                    logger.warning(f"API Key {current_key[:8]}... 限流，冷却 60 秒")
                                key_index += 1
                                if attempt < retries:
                                    delay = min(2 ** (attempt + 1), 30)
                                    await asyncio.sleep(delay)
                                    continue
                            else:
                                return f"API 错误: {err_msg}", None

                        choices = raw.get("choices")
                        if not choices or not isinstance(choices, list) or len(choices) == 0:
                            return "API 返回数据异常：无 choices", None

                        message = choices[0].get("message")
                        if not message:
                            return "API 返回数据异常：无 message", None

                        content = message.get("content", "")
                        # ===== 思考链打印（有则打，无则跳过） =====
                        reasoning = message.get("reasoning_content") or message.get("reasoning") or message.get("thinking")
                        if reasoning:
                            logger.info(f"[思考链][{strategy}] {reasoning}")
                        tool_calls_data = message.get("tool_calls")

                        tool_calls = None
                        if tool_calls_data and isinstance(tool_calls_data, list):
                            tool_calls = []
                            for tc in tool_calls_data:
                                if not isinstance(tc, dict):
                                    continue
                                func = tc.get("function", {})
                                args_str = func.get("arguments", "{}")
                                try:
                                    args_dict = json.loads(args_str) if isinstance(args_str, str) else args_str
                                except json.JSONDecodeError as e:
                                    logger.error(f"解析 tool_calls arguments 失败: {args_str[:100]}", exc_info=True)
                                    args_dict = {}
                                tool_calls.append({
                                    "id": tc.get("id"),
                                    "type": tc.get("type"),
                                    "function": {
                                        "name": func.get("name"),
                                        "arguments": args_dict
                                    }
                                })

                        KEY_FAILURE_COUNT[current_key] = 0
                        return content, tool_calls

        except asyncio.TimeoutError:
            if attempt < retries:
                delay = min(2 ** (attempt + 1), 30) + random.uniform(0, 0.3 * (2 ** attempt))
                logger.warning(f"请求超时，第 {attempt+1}/{retries} 次重试，等待 {delay:.2f}s")
                await asyncio.sleep(delay)
                continue
            # 尝试 fallback
            if MODELS_CONFIG and get_strategy_config("fallback"):
                logger.warning(f"策略 {strategy} 超时，尝试 fallback")
                return await safe_api_request(messages, strategy="fallback", timeout=timeout, tools=tools, tool_choice=tool_choice, retries=1)
            return "请求超时（120秒），请稍后重试", None

        except Exception as e:
            if attempt < retries:
                error_str = str(e)
                if "429" in error_str or "tpm exhausted" in error_str or "rpm exhausted" in error_str:
                    base_delay = min(5 * (2 ** attempt), 30)
                else:
                    base_delay = min(2 ** (attempt + 1), 30)
                delay = base_delay + random.uniform(0, base_delay * 0.3)
                logger.warning(f"请求异常: {e}，第 {attempt+1}/{retries} 次重试，等待 {delay:.2f}s")
                await asyncio.sleep(delay)
                continue
            # 尝试 fallback
            if MODELS_CONFIG and get_strategy_config("fallback"):
                logger.warning(f"策略 {strategy} 异常，尝试 fallback: {e}")
                return await safe_api_request(messages, strategy="fallback", timeout=timeout, tools=tools, tool_choice=tool_choice, retries=1)
            return f"请求异常: {str(e)}", None

    # 所有重试用完
    if MODELS_CONFIG and get_strategy_config("fallback"):
        logger.warning(f"策略 {strategy} 重试用完，尝试 fallback")
        return await safe_api_request(messages, strategy="fallback", timeout=timeout, tools=tools, tool_choice=tool_choice, retries=1)
    return "请求失败，已达最大重试次数", None


async def _get_current_key_with_list(api_keys: list, start_index: int = 0) -> str:
    """从指定的 Key 列表中获取一个可用 Key，支持冷却检测"""
    if not api_keys:
        return None
    total = len(api_keys)
    for i in range(total):
        idx = (start_index + i) % total
        key = api_keys[idx]
        cooldown_until = KEY_COOLDOWN_UNTIL.get(key, 0)
        if time.time() >= cooldown_until:
            return key
    # 所有 Key 都冷却中，等待最短冷却
    min_cooldown = min(KEY_COOLDOWN_UNTIL.get(k, 0) for k in api_keys)
    if min_cooldown > 0:
        wait_time = min_cooldown - time.time() + 0.5
        if wait_time > 0:
            logger.warning(f"所有 Key 均在冷却中，等待 {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
        return api_keys[start_index % total]
    return None

async def _try_backup(messages, model, timeout, tools, tool_choice, retries):
    """尝试使用备用配置发送一次请求（不带 Key 轮询，单 Key）"""
    if not BACKUP_BASE or not BACKUP_KEY:
        return "主配置失效且未配置备用", None

    headers = {"Authorization": f"Bearer {BACKUP_KEY}", "Content-Type": "application/json"}
    data = {"model": model, "messages": messages}
    if tools:
        data["tools"] = tools
        data["tool_choice"] = tool_choice

    # 简单重试一次
    for attempt in range(retries + 1):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.post(f"{BACKUP_BASE}/chat/completions", json=data, headers=headers) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        if attempt < retries:
                            delay = min(2 ** (attempt + 1), 30)
                            await asyncio.sleep(delay)
                            continue
                        return f"备用配置也失败: HTTP {resp.status} {error_text[:200]}", None
                    raw = await resp.json()
                    if "error" in raw:
                        return f"备用配置 API 错误: {raw['error'].get('message', raw['error'])}", None
                    choices = raw.get("choices")
                    if not choices:
                        return "备用配置返回无 choices", None
                    message = choices[0].get("message")
                    if not message:
                        return "备用配置返回无 message", None
                    content = message.get("content", "")
                    tool_calls_data = message.get("tool_calls")
                    tool_calls = None
                    if tool_calls_data:
                        tool_calls = []
                        for tc in tool_calls_data:
                            func = tc.get("function", {})
                            args_str = func.get("arguments", "{}")
                            try:
                                args_dict = json.loads(args_str) if isinstance(args_str, str) else args_str
                            except json.JSONDecodeError:
                                args_dict = {}
                            tool_calls.append({
                                "id": tc.get("id"),
                                "type": tc.get("type"),
                                "function": {
                                    "name": func.get("name"),
                                    "arguments": args_dict
                                }
                            })
                    return content, tool_calls
        except asyncio.TimeoutError:
            if attempt < retries:
                delay = min(2 ** (attempt + 1), 30)
                await asyncio.sleep(delay)
                continue
            return "备用配置超时", None
        except Exception as e:
            if attempt < retries:
                delay = min(2 ** (attempt + 1), 30)
                await asyncio.sleep(delay)
                continue
            return f"备用配置异常: {str(e)}", None
    return "备用配置重试失败", None

async def _get_current_key() -> str:
    """获取当前可用的 API Key（跳过冷却中的 Key），若全部冷却则等待最短冷却结束"""
    global CURRENT_KEY_INDEX
    total_keys = len(NEWAPI_KEYS)
    if total_keys == 0:
        raise RuntimeError("没有可用的 API Key")

    for _ in range(total_keys):
        key = NEWAPI_KEYS[CURRENT_KEY_INDEX]
        cooldown_until = KEY_COOLDOWN_UNTIL.get(key, 0)
        if time.time() >= cooldown_until:
            return key
        CURRENT_KEY_INDEX = (CURRENT_KEY_INDEX + 1) % total_keys

    if KEY_COOLDOWN_UNTIL:
        min_cooldown = min(KEY_COOLDOWN_UNTIL.values())
        wait_time = min_cooldown - time.time() + 0.5
        if wait_time > 0:
            logger.warning(f"所有 API Key 均在冷却中，等待 {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
        return NEWAPI_KEYS[CURRENT_KEY_INDEX]
    else:
        return NEWAPI_KEYS[CURRENT_KEY_INDEX]

async def run_sub_agent(
    user_id: str,
    sub_goal: str,
    context_summary: str,
    chat_type: str = "private",
    group_id: str = "",
    max_steps: int = 3
) -> str:
    """
    执行子 Agent 任务（隔离上下文，限制工具集）。
    子 Agent 只能使用 read_file 和 execute_shell 工具。
    """
    from admin import TOOLS, execute_tool_directly

    sub_session_id = f"{user_id}_sub_{uuid.uuid4().hex[:6]}"
    key = get_conv_key(sub_session_id, chat_type, group_id)

    system_prompt = f"""你是子 Agent，负责执行特定子任务。

【上下文摘要】
{context_summary}

【你的目标】
{sub_goal}

【限制】
- 你只能使用 read_file 和 execute_shell 工具。
- 你不能调用 agent_manager 或创建新的子任务。
- 完成目标后，直接输出结果，不要额外解释。
"""
    conversation_history[key] = [{"role": "system", "content": system_prompt}]

    for step in range(1, max_steps + 1):
        response = await get_ai_response(
            sub_session_id,
            f"步骤 {step}/{max_steps}：执行子任务。如果已完成，输出 FINISH。",
            chat_type=chat_type,
            tool_whitelist=["read_file", "execute_shell"]
        )

        content = response.get("content", "")
        tool_calls = response.get("tool_calls", [])

        if not tool_calls or "FINISH" in content:
            return content if content else "子任务完成"

        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            if tool_name not in ["read_file", "execute_shell"]:
                continue
            tool_args = tc["function"].get("arguments", {})
            result = await execute_tool_directly(tool_name, tool_args, user_id)
            conversation_history[key].append({
                "role": "tool",
                "tool_call_id": tc.get("id", f"step_{step}"),
                "content": result[:500]
            })

    return "子任务执行超时或达到最大步数"

async def _detect_intent(prompt: str) -> str:
    """
    用 deepseek-flash 判断用户意图。
    返回 "tool"（需要调用工具）或 "chat"（闲聊）。
    """
    messages = [
        {
            "role": "system",
            "content": (
                "你是一个意图分类器。判断用户消息是「工具类」还是「闲聊类」。"
                "只返回一个词：tool 或 chat，不要输出任何其他内容。\n\n"
                "判断规则：\n"
                "- tool：用户需要执行实际动作，如执行命令、读写文件、下载、爬取、备份、改配置、"
                "检索历史对话/记忆、回忆之前说过的内容等\n"
                "- chat：闲聊、问答、分析、写作、翻译、解释、讲笑话等纯文本交互"
            ),
        },
        {"role": "user", "content": prompt},
    ]
    try:
        reply, _ = await safe_api_request(messages, strategy="tool", tools=None)
        reply = (reply or "").strip().lower()
        if "tool" in reply:
            return "tool"
        return "chat"
    except Exception as e:
        logger.error(f"[意图判断] 失败: {e}，兜底走 chat")
        return "chat"


# ==================== ask_ai 核心函数（已增加 Pi 模式分支） ====================
async def ask_ai(user_id: str, prompt: str, chat_type: str = "private", group_id: str = "") -> str:
    from admin import build_tool_schema
    from admin import TOOLS, execute_tool_directly
    user_id = str(user_id)
    key = get_conv_key(user_id, chat_type, group_id)
    # 确保对话历史键存在
    if key not in conversation_history:
        conversation_history[key] = []

    # ===== 硬性指令：查询知识库=====
    if "查询知识库" in prompt or "调用知识库" in prompt or "搜索知识库" in prompt:
        # 提取关键词
        import re
        match = re.search(r'[：:]\s*(.+?)$', prompt)
        keyword = match.group(1).strip() if match else prompt
        # 如果关键词太长（可能是整句话），截取前20个字作为搜索词
        if len(keyword) > 50:
            keyword = keyword[:50]
        from memory import knowledge_db
        results = knowledge_db.search_solutions(keyword, limit=5)
        if not results:
            return f"未找到与「{keyword}」相关的历史记录"
        lines = [f"找到 {len(results)} 条相关历史记录："]
        for r in results:
            lines.append(f"- 问题：{r['problem']}")
            lines.append(f"  方案：{r['solution'][:200]}...")
            lines.append(f"  时间：{r['created_at']}")
        return "\n".join(lines)
    if prompt.startswith("记住") or prompt.startswith("记住："):
        memory_text = prompt[2:].strip()
        if memory_text:
            if memory_text.startswith("："):
                memory_text = memory_text[1:].strip()
            if memory_text:
                result = memory.add_memory(memory_text)
                return result
        return "请告诉我要记住什么内容"

    ocr_text, is_new_image = consume_image_context(key)
    logger.debug(f"ask_ai: key={key}, ocr_text={ocr_text[:50] if ocr_text else '空'}, is_new_image={is_new_image}")
    if is_new_image:
        prompt = f"关于我刚发的这张新图片：{prompt}"

    # ----- 前置处理（历史、压缩、记忆） -----
    history = conversation_history.get(key, [])[-(MAX_HISTORY * 2):]
    if len(history) >= 10 and len(history) % 10 == 0:
        if should_compress(key):
            await compress_history(key)
            history = conversation_history.get(key, [])[-(MAX_HISTORY * 2):]
    if len(history) > MAX_HISTORY:
        conversation_history[key] = history[-MAX_HISTORY:]
        history = conversation_history[key]
    messages = []
    if SYSTEM_PROMPT:
        messages.append({"role": "system", "content": SYSTEM_PROMPT})

    if ocr_text:
        messages.append({"role": "system", "content": f"【附加信息】用户最近发送的图片识别文字为：\n{ocr_text}"})
        messages.append({"role": "system", "content": "【强制规则】请忽略之前所有关于其他图片的讨论，仅基于当前提供的图片文字作答。如果当前图片文字中没有提及任何暴力、威胁或负面词汇，不得主动引入此类内容。"})
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    # ===== 超限裁剪 =====
    if count_messages_tokens(messages) > 8000:
        truncated_history = history[-5:] if len(history) >= 5 else history
        conversation_history[key] = truncated_history
        history = truncated_history
        messages = []
        if SYSTEM_PROMPT:
            messages.append({"role": "system", "content": SYSTEM_PROMPT})
        if ocr_text:
            messages.append({"role": "system", "content": f"【附加信息】用户最近发送的图片识别文字为：\n{ocr_text}"})
            messages.append({"role": "system", "content": "【强制规则】请忽略之前所有关于其他图片的讨论，仅基于当前提供的图片文字作答。如果当前图片文字中没有提及任何暴力、威胁或负面词汇，不得主动引入此类内容。"})
        messages.extend(history)
        messages.append({"role": "user", "content": prompt})
        logger.warning(f"[压缩] 超限，截断至 {len(history)} 轮")

    # ===== 长期记忆注入（无论是否超限都执行，插入到 system 之后） =====
    matched_memories = memory.search_memories(prompt)
    if matched_memories:
        memory_text = "\n".join([f"- {m}" for m in matched_memories])
        memory_msg = {
            "role": "system",
            "content": f"【长期记忆】用户之前提到过以下信息，请参考：\n{memory_text}"
        }
        messages.insert(1, memory_msg)

    # ========== 核心修改：模式判断（Pi / API）==========
    # 在真正调用模型之前，检查当前后端模式
    if pi_integration.get_mode() == "pi":
        # Pi 模式：直接调用 pi_integration.ask_pi，跳过所有后续的 API 请求和工具循环
        logger.info(f"[ask_ai] Pi 模式：转发用户消息（长度 {len(prompt)}）")
        try:
            # 注意：pi_integration.ask_pi 内部会自行管理超时和会话
            # 我们传递的 prompt 已经包含了 OCR 文本、记忆等上下文（由上面的前置逻辑注入）
            pi_reply = await pi_integration.ask_pi(prompt)
            # Pi 返回结果后，不更新本地 conversation_history（由 Pi 管理）
            return pi_reply
        except Exception as e:
            logger.error(f"[ask_ai] Pi 模式调用失败: {e}", exc_info=True)
            # 失败时返回错误信息，不切换到 API（由用户决定是否切换）
            return f"Pi 后端错误: {str(e)}"

    # ----- 以下为原有的 API 模式逻辑（完全不变） -----
    tools_schema = []
    for name, info in TOOLS.items():
        func = info["func"]
        params_desc = info.get("params_desc", {})
        schema = build_tool_schema(name, func, params_desc)
        tools_schema.append(schema)

    try:
        # ===== 用 deepseek 判断意图 =====
        strategy = await _detect_intent(prompt)
        logger.info(f"[ask_ai] 意图判断: {strategy}")
        if strategy == "tool":
            reply, tool_calls = await safe_api_request(
                messages, strategy="tool", tools=tools_schema, tool_choice="auto"
            )
        else:
            reply, tool_calls = await safe_api_request(
                messages, strategy="chat", tools=None
            )
    except Exception as e:
        logger.error(f"ask_ai API 调用失败: {e}", exc_info=True)
        return f"AI 请求失败: {str(e)}"

    if tool_calls:
        assistant_msg = {
            "role": "assistant",
            "content": reply,
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": json.dumps(tc["function"]["arguments"], ensure_ascii=False)
                    }
                }
                for tc in tool_calls
            ]
        }
        history = conversation_history.get(key, [])
        conversation_history[key] = history + [
            {"role": "user", "content": prompt},
            assistant_msg
        ]

        tool_results = []
        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            args = tc["function"]["arguments"]
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError as e:
                    logger.error(f"解析工具参数失败: {args[:100]}", exc_info=True)
                    args = {}
            try:
                result = await execute_tool_directly(tool_name, args, user_id)
            except Exception as e:
                logger.error(f"执行工具 {tool_name} 失败: {e}", exc_info=True)
                result = f"工具执行异常: {str(e)}"
            tool_results.append({
                "tool_call_id": tc.get("id", f"call_{len(tool_results)}"),
                "output": result if isinstance(result, str) else str(result)
            })

        tool_messages = []
        for tr in tool_results:
            tool_messages.append({
                "role": "tool",
                "tool_call_id": tr["tool_call_id"],
                "content": tr["output"][:4000] if len(tr["output"]) > 4000 else tr["output"]
            })
        conversation_history[key].extend(tool_messages)

        final_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        final_messages.extend(conversation_history[key])
        try:
            # ===== 修改点：最终总结使用 chat 策略（不需要工具） =====
            final_reply, _ = await safe_api_request(final_messages, strategy="chat", tools=None)
        except Exception as e:
            logger.error(f"生成最终回复失败: {e}", exc_info=True)
            final_reply = f"生成总结失败: {str(e)}"

        conversation_history[key].append({"role": "assistant", "content": final_reply})

        if len(conversation_history[key]) > MAX_HISTORY * 2:
            conversation_history[key] = conversation_history[key][-(MAX_HISTORY * 2):]

        # ===== 情景记忆落盘检查 =====
        try:
            from bot_utils.episodic import maybe_flush
            maybe_flush(key, conversation_history[key])
        except Exception as e:
            logger.error(f"[episodic] 落盘检查失败: {e}", exc_info=True)

        return final_reply

    # ===== 非工具路径：保存本轮对话到历史 =====
    conversation_history[key].append({"role": "user", "content": prompt})
    conversation_history[key].append({"role": "assistant", "content": reply})

    if len(conversation_history[key]) > MAX_HISTORY * 2:
        conversation_history[key] = conversation_history[key][-(MAX_HISTORY * 2):]

    # ===== 情景记忆落盘检查 =====
    try:
        from bot_utils.episodic import maybe_flush
        maybe_flush(key, conversation_history[key])
    except Exception as e:
        logger.error(f"[episodic] 落盘检查失败: {e}", exc_info=True)

    # ===== 知识积累逻辑（在返回之前执行） =====
    try:
        from memory import knowledge_db
        # 检测是否有问题-解决方案模式
        if "解决办法" in prompt or "修复" in prompt or "解决" in prompt:
            # 简单提取：如果 AI 回复中包含解决方案，则存入数据库
            # 更精确的做法是让 AI 主动标记，但这里先用关键词匹配
            if "修复" in reply or "解决" in reply or "方案" in reply:
                knowledge_db.add_solution(prompt[:100], reply[:500])
                logger.info("知识库：已存储解决方案")
    except Exception as e:
        logger.warning(f"知识积累失败: {e}")
    return reply

# ==================== ECHO 专用接口（get_ai_response 也增加 Pi 模式支持） ====================
async def get_ai_response(
    user_id: str,
    prompt: str,
    chat_type: str = "web",
    tool_whitelist: List[str] = None
) -> dict:
    from admin import build_tool_schema
    from admin import TOOLS, build_tool_schema
    key = get_conv_key(user_id, chat_type, "web")

    history = conversation_history.get(key, [])[-(MAX_HISTORY * 2):]
    if len(history) >= 10 and len(history) % 10 == 0:
        if should_compress(key):
            await compress_history(key)
            history = conversation_history.get(key, [])[-(MAX_HISTORY * 2):]
    if len(history) > MAX_HISTORY:
        conversation_history[key] = history[-MAX_HISTORY:]
        history = conversation_history[key]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    # Pi 模式检测（get_ai_response 也支持 Pi，但 Web 端通常默认 API）
    if pi_integration.get_mode() == "pi":
        # 对于 Web 端，Pi 模式直接返回文本
        try:
            reply = await pi_integration.ask_pi(prompt)
            # 为了兼容返回格式，包装为 dict
            return {"content": reply, "tool_calls": []}
        except Exception as e:
            logger.error(f"[get_ai_response] Pi 模式调用失败: {e}", exc_info=True)
            return {"content": f"Pi 后端错误: {str(e)}", "tool_calls": []}

    # API 模式原逻辑
    tools_schema = []
    for name, info in TOOLS.items():
        if tool_whitelist and name not in tool_whitelist:
            continue
        func = info["func"]
        params_desc = info.get("params_desc", {})
        schema = build_tool_schema(name, func, params_desc)
        tools_schema.append(schema)

    # ===== 修改点：web 端默认使用 tool 策略 =====
    reply, tool_calls = await safe_api_request(messages, strategy="tool", tools=tools_schema, tool_choice="auto")

    assistant_msg = {"role": "assistant", "content": reply}
    if tool_calls:
        assistant_msg["tool_calls"] = [
            {"id": tc["id"], "type": "function", "function": tc["function"]}
            for tc in tool_calls
        ]
    conversation_history[key] = history + [{"role": "user", "content": prompt}, assistant_msg]

    return {
        "content": reply,
        "tool_calls": tool_calls if tool_calls else []
    }

async def submit_tool_results(user_id: str, tool_results: list) -> str:
    """
    提交工具执行结果给 AI，获取最终总结。
    tool_results: [{"tool_call_id": "call_xxx", "output": "stdout 文本"}]
    """
    from admin import TOOLS
    key = get_conv_key(user_id, "web", "web")
    history = conversation_history.get(key, [])

    tool_messages = []
    for result in tool_results:
        tool_messages.append({
            "role": "tool",
            "tool_call_id": result["tool_call_id"],
            "content": (result["output"][:2000] + "\n... (输出过长，已截断)") if len(result["output"]) > 2000 else result["output"]
        })

    history.extend(tool_messages)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)

    reply, _ = await safe_api_request(messages, tools=None)

    conversation_history[key] = history + [{"role": "assistant", "content": reply}]

    return reply

class TaskContext:
    """任务链上下文（轻量级状态传递）"""
    def __init__(self, user_goal: str):
        self.user_goal = user_goal[:200]
        self.last_output = ""
        self.files_read = []
        self.step_count = 0

    def to_prompt(self) -> str:
        lines = []
        lines.append(f"【原始目标】{self.user_goal}")
        lines.append(f"【已执行步数】{self.step_count}")
        if self.files_read:
            lines.append(f"【已读取文件】{', '.join(self.files_read[-3:])}")
        if self.last_output:
            lines.append(f"【上一步输出】{self.last_output[:500]}")
        return "\n".join(lines)


async def run_async_task_chain(
    user_id: str,
    goal: str,
    max_steps: int = 10,
    chat_type: str = "web",
    planning_mode: bool = True
) -> str:
    from admin import execute_tool_directly

    if not planning_mode:
        return await _run_fast_chain(user_id, goal, max_steps, chat_type)

    session_id = get_conv_key(user_id, chat_type, "web")
    todo_mgr = TodoListManager(session_id)

    todo_mgr.add(f"完成目标：{goal[:100]}")

    final_summary = ""
    steps_executed = 0

    while steps_executed < max_steps:
        pending = todo_mgr.get_pending()
        if not pending:
            final_summary = todo_mgr.list_items()
            break

        todo_mgr.mark_running(pending.id)

        prompt = f"""当前待执行任务：{pending.description}
任务清单状态：
{todo_mgr.list_items()}

请决定：
- 如果这个任务可以直接完成，请调用相应的工具（如 read_file、execute_shell）。
- 如果这个任务需要拆解，请调用 agent_manager 添加子任务。
- 如果任务已由其他方式完成，请调用 agent_manager 标记为 done。
"""
        response = await get_ai_response(user_id, prompt, chat_type=chat_type)
        tool_calls = response.get("tool_calls", [])

        if not tool_calls:
            todo_mgr.mark_done(pending.id, response.get("content", "无输出"))
            steps_executed += 1
            continue

        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            tool_args = tc["function"].get("arguments", {})

            if tool_name == "agent_manager":
                result = await execute_tool_directly(tool_name, tool_args, user_id)
                todo_mgr.mark_done(pending.id, result)
            else:
                result = await execute_tool_directly(tool_name, tool_args, user_id)
                todo_mgr.mark_done(pending.id, result)

        steps_executed += 1

    if not final_summary:
        final_summary = todo_mgr.list_items()

    return final_summary

async def _run_fast_chain(user_id: str, goal: str, max_steps: int, chat_type: str) -> str:
    return "快速链式模式已弃用，请使用规划模式（默认启用）"
