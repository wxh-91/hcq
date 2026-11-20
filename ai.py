import os
_ROOT = os.environ.get("PROJECT_ROOT") or os.path.dirname(os.path.abspath(__file__))

import random
import time
import re
import functools
import aiohttp
import asyncio
import json
import inspect
import tiktoken
import logging
logger = logging.getLogger(__name__)
try:  # 业务埋点（导入失败不影响主流程）
    from monitor import metrics as _qm
except Exception:
    _qm = None

# ===== 新增导入 pi_integration =====
import pi_integration
# ===== 新增导入 deepseek_web_integration（C端DeepSeek备用通道，可选）=====
try:
    import deepseek_web_integration
except Exception as _wd_import_err:
    # 通道缺失不应影响主流程：导入失败时降级为不可用
    deepseek_web_integration = None
    logger.warning("deepseek_web_integration 导入失败，web_deepseek 通道不可用: %s" % _wd_import_err)

# ===== 全局间隔控制 =====
_last_request_time = 0
_interval_lock = asyncio.Lock()
# ===== 全局限流信号量 =====
api_semaphore = asyncio.Semaphore(1)   # 全局只允许 1 个并发 API 请求
MIN_REQUEST_INTERVAL = 2.5   #  删除重复定义，只保留此值
from aup_core import AUPError  # [Tool Boundary] 明确业务错误（UNSUPPORTED_PLATFORM 等）
from import_center import get_service
from config_center import get_config
from memory import Memory, load_profile
from memory_center import MemoryCenter, SUMMARY_SYSTEM_PROMPT
from cl import DEFAULT_ADMIN
from typing import Optional, List
from cl import DEFAULT_ADMIN, NEWAPI_BASE, NEWAPI_KEYS, CURRENT_KEY_INDEX, KEY_FAILURE_COUNT, KEY_COOLDOWN_UNTIL, MODEL_NAME, DEFAULT_MODEL, get_strategy_config, detect_strategy, MODELS_CONFIG
# ===== [HostEnv] Windows 宿主机环境库接口（host_env.db / sys_env） =====
try:
    from host_env import format_env_snapshot, get_env
except Exception as _host_env_import_err:
    format_env_snapshot = lambda keys=None: ""
    get_env = lambda key, default="": default
    logger.warning("host_env 导入失败，环境情报注入不可用: %s" % _host_env_import_err)
memory = get_service("memory")
conversation_history = MemoryCenter.get_storage()   # 短期上下文物理归 MemoryCenter 持有（唯一容器）
_last_exp_hit = {}  # [Feedback Loop] key -> trace_id：记录上一轮命中经验的轨迹（用户纠正检测）
# 对话轮数
MAX_HISTORY = 150

# ===== 意图澄清暂存（内存，进程重启即丢）=====
# user_id -> {"prompt": str, "chat_type": str, "group_id": str}
_pending_clarify: dict = {}
# 意图判断置信度阈值：低于此值追问用户
INTENT_CONFIDENCE_THRESHOLD = 0.6
# ==================== Todo List 数据结构 ====================
import uuid
from typing import List, Optional, Literal
from dataclasses import dataclass, field
import os


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
        # [fix] 不再 pop 澄清状态：澄清拦截由 _ask_ai_impl 统一消费，
        # 此处 pop 会吃掉 pending 导致「用户回复 tool/chat 仍被丢进意图分类器」的死循环
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


# ===== 压缩判定/记忆巩固已收编 memory_center（should_compress/compress） =====

async def _call_ai_no_compress(user_id: str, prompt: str, system_prompt: str = None) -> str:
    """纯粹的 API 调用，不触发压缩检查，用于摘要生成"""
    # 注意：这里应使用 SUMMARY_SYSTEM_PROMPT（摘要专用），而非主 SYSTEM_PROMPT
    sys_content = system_prompt if system_prompt is not None else SUMMARY_SYSTEM_PROMPT
    messages = [{"role": "system", "content": sys_content}, {"role": "user", "content": prompt}]
    # 维护性调用（会话摘要），固定用便宜的 deepseek-flash，与用户本轮选的模型无关
    content, _ = await safe_api_request(messages, model="deepseek-flash")
    return content



# ===== compress_history 已收编 memory_center（MemoryCenter.compress） =====

# ========== 摘要生成专用系统提示词（不含人设） ==========

# ===== 人格常量（P0~P4）已迁移 memory_center（MemoryCenter 唯一持有与注入） =====
# 启动时读一次 profile.md，注入 system prompt 用，避免每次请求读文件
PROFILE_CONTENT = load_profile()

def _sanitize_for_chat(history: list) -> list:
    """
    将对话历史中带 tool_calls 的 assistant 消息、role="tool" 的消息
    降级为纯文本，避免 chat 模型模仿工具调用格式。
    只作用于发送给模型的 messages，不改动 conversation_history 本身。
    """
    sanitized = []
    for msg in history:
        role = msg.get("role", "")
        content = msg.get("content", "") or ""

        if role == "assistant" and msg.get("tool_calls"):
            # [Output Sanitization] 只保留工具名，绝对不拼接原始 arguments JSON：
            # 原始参数是泄漏源（含转义符/URL/脚本），模型复读即泄密。
            parts = []
            for tc in msg["tool_calls"]:
                func = tc.get("function", {}) if isinstance(tc, dict) else {}
                name = func.get("name", "unknown")
                parts.append(f"（已调用工具：{name}）")
            merged = content
            if parts:
                merged = (merged + "\n" if merged else "") + "\n".join(parts)
            sanitized.append({"role": "assistant", "content": merged})
            continue

        if role == "tool":
            sanitized.append({
                "role": "user",
                "content": f"（工具返回：{content}）"
            })
            continue

        sanitized.append(msg)
    return sanitized



# ===== [Output Sanitization] 代码级输出清洗层：物理拦截，禁止依赖 Prompt 约束 =====
_SANITIZE_FALLBACK = "系统内部处理中，请重试。"
# 能力否认话术修正（代码层，防 C 端闲聊通道误回复"无法主动发送/没有定时提醒"）
_AI_LIMITATION_PAT = re.compile(r"(?:没有|无法|不能).{0,6}(?:定时|主动).{0,4}(?:提醒|推送|发送).{0,8}(?:能力|功能)")
_FIX_LIMITATION_MSG = "主人，由于当前处于闲聊通道，请对我说“设定一个定时任务”来触发我的本地闹钟功能。"
# 泄漏特征（叙述区检测；代码块内的转义是合法内容，先剔除再检测）
_LEAK_PAT_STRONG = [
    re.compile(r"调用了\s*工具\s*\S+\s*，\s*参数\s*：\s*\{"),   # sanitize 旧格式（含参数 JSON）
    re.compile(r"tool_calls"),                                             # 结构字段名
    re.compile(r"\{\s*[\"'](script_content|cmd|url|filepath|tool_name|target)[\"']\s*:"),  # 已知参数 key 泄漏
    re.compile(r"\{\s*[\"'](function|arguments)[\"']\s*:"),          # function/arguments 结构泄漏
]
_LEAK_PAT_WEAK = [
    re.compile(r"\\n"),   # 字面反斜杠+n（即字面转义换行）
    re.compile(r"\\\""), # 字面反斜杠+引号
]

# ===== [UX Purge] 体验净化：格式违规特征（泄漏工具调用细节）→ 判定违规打回重试 =====
_UX_VIOLATION = "__UX_VIOLATION__"   # sanitize_output 命中格式违规时返回的标记（区别于占位文案）
# 1) [调用工具: xxx] / [调用工具：xxx]
_UX_TOOL_CALL_PAT = re.compile(r"\[\s*(?:调用|使用|执行|运行)\s*工具\s*[:：]\s*[\w]+\]")
# 2) （执行命令: xxx）/(执行命令: xxx)/(运行命令：xxx) 等
_UX_EXEC_PAT = re.compile(r"[（(]\s*(?:执行|运行|调用)\s*命令\s*[:：]")
# 3) shell 命令原文复述（ls -l / ls -la / rm -rf / rm -r / kill -9 / cd / cat / mkdir / chmod / sudo / mv / cp / systemctl）
_UX_SHELL_PAT = re.compile(
    r"(?:^|[\s。；；，,：:])(?:ls\s+-[laA]|rm\s+-[rf]|rm\s+-[ri]|kill\s+-9|killall\s+\w+"
    r"|cd\s+/[\w./-]+|cat\s+[\w./-]+|mkdir\s+-p\s+[\w./-]+|chmod\s+\d{3,4}\s+[\w./-]+"
    r"|sudo\s+\w+|mv\s+\S+\s+\S+|cp\s+\S+\s+\S+|systemctl\s+(?:start|stop|restart|status)\s+\w+"
    r"|rm\s+-rf)(?:\s|$|[。；])"
)
_UX_LEAK_PATS = [_UX_TOOL_CALL_PAT, _UX_EXEC_PAT, _UX_SHELL_PAT]


def _strip_code_blocks(text):
    """剔除 ``` 围栏代码块，代码块内的转义符是合法内容，不参与泄漏检测。"""
    return re.sub(r"```.*?```", "", text, flags=re.S)


# ===== Sanitizer V2：裸链接 / 长参数链接 / 裸 JSON（防飞书风控） =====
_SAN_V2_FALLBACK = "[含敏感链接或数据，已自动拦截]"
# 剔除 Markdown 链接 [文字](url)，裸链接检测不误伤 Markdown 格式
_V2_MD_LINK = re.compile(r"\[[^\]]*\]\([^)]*\)", re.S)
# 裸媒体 URL：https?:// 且含 .m3u8/.mp4/.ts 后缀（可带查询串）
_V2_RAW_MEDIA_URL = re.compile(r"https?://[^\s\"')\]]+\.(?:m3u8|mp4|ts)(?:\?[^\s\"')\]]*)?", re.I)
# 长参数链接：长度 > 100 且含大量 ?、&、= 参数特征
_V2_LONG_PARAM_URL = re.compile(r"https?://[^\s\"')\]]{100,}", re.I)
# 裸 JSON：以 {" 或 [{ 开头且含 ": 的数据块
_V2_RAW_JSON = re.compile(r'^\s*(\{\s*"|\[\s*\{\s*")[\s\S]*?":')


def _strip_md_links(text):
    """剔除 Markdown 链接 [文字](url)，供裸链接检测使用"""
    return _V2_MD_LINK.sub("", text)


# ===== [Protocol Firewall] 唯一协议：原生 Function Calling；XML/伪协议在 sanitize 之前剥离并路由 =====
_LT = chr(60)
_GT = chr(62)
_TC_O = _LT + "tool_call" + _GT
_TC_C = _LT + "/tool_call" + _GT
_XML_TC_RE = re.compile(re.escape(_TC_O) + r"[\s\S]{0,4000}?" + re.escape(_TC_C), re.I)
_XML_FN_RE = re.compile(_LT + r"function\s*=\s*([A-Za-z0-9_.\-]+)\s*" + _GT
                        + r"([\s\S]{0,4000}?)" + re.escape(_LT + "/function" + _GT), re.I)
_XML_ARGS_RE = re.compile(_LT + r"parameter\s*=\s*([A-Za-z0-9_\-]+)\s*" + _GT
                          + r"(.*?)" + re.escape(_LT + "/parameter" + _GT), re.I | re.S)
_PSEUDO_TC_RE = re.compile(r"\[调用工具[:：][^\]\n]{1,120}\]|（执行命令[:：][^）\n]{1,300}）", re.I)


def _coerce_scalar(v):
    if isinstance(v, str):
        s = v.strip()
        if s.lower() in ("true", "false"):
            return s.lower() == "true"
        try:
            return int(s) if re.fullmatch(r"-?\d+", s) else float(s)
        except Exception:
            return v
    return v


def _strip_protocol_artifacts(text):
    """剥离非原生协议片段，返回 (clean_text, parsed_tool_calls)。解析出的调用交给原生路由执行。"""
    if not isinstance(text, str) or not text:
        return text, []
    parsed = []
    for block in _XML_TC_RE.findall(text):
        body = re.sub(_TC_O + "|" + _TC_C, "", block, flags=re.I).strip()
        try:
            obj = json.loads(body)
            name = obj.get("name") or obj.get("tool") or (obj.get("function") or {}).get("name")
            args = obj.get("arguments") or obj.get("args") or obj.get("parameters") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            if name:
                parsed.append({"id": "xml_%d" % len(parsed), "type": "function",
                               "function": {"name": name, "arguments": args}})
        except Exception:
            continue
    for m in _XML_FN_RE.finditer(text):
        name, body = m.group(1), m.group(2)
        args = {}
        for k, v in _XML_ARGS_RE.findall(body):
            args[k] = _coerce_scalar(v.strip())
        if not args:
            try:
                args = json.loads(body.strip())
            except Exception:
                args = {}
        if name:
            parsed.append({"id": "xml_%d" % len(parsed), "type": "function",
                           "function": {"name": name, "arguments": args}})
    clean = _XML_TC_RE.sub("", text)
    clean = _XML_FN_RE.sub("", clean)
    clean = _PSEUDO_TC_RE.sub("", clean)
    return clean.strip(), parsed


def _coerce_protocol(content, tool_calls):
    """模型响应统一出口：协议片段先剥离再清洗；原生 tool_calls 优先，其次路由解析结果。"""
    clean, parsed = _strip_protocol_artifacts(content or "")
    if parsed:
        logger.warning("[Protocol Firewall] 拦截 XML/伪协议工具调用 %d 个，已转为原生路由", len(parsed))
    if tool_calls:
        return clean, tool_calls
    if parsed:
        return clean, parsed
    return clean, tool_calls


def sanitize_output(text) -> str:
    """物理清洗层：任何要发往 QQ/飞书通道的回复都先过这里。

    - 命中强特征（工具参数 JSON 复述 / tool_calls / 结构字段）→ 直接拦截
    - 命中弱特征（密集字面转义符）≥ 2 项 → 拦截
    - Sanitizer V2：裸媒体链接 / 长参数链接 / 裸 JSON → 拦截
    - 命中后返回固定占位文案，绝不把原始工具调用 JSON 泄漏给前端。
    """
    if not isinstance(text, str) or not text:
        return text
    probe = _strip_code_blocks(text)
    strong_hits = [p for p in _LEAK_PAT_STRONG if p.search(probe)]
    weak_count = sum(len(p.findall(probe)) for p in _LEAK_PAT_WEAK)
    if strong_hits or weak_count >= 2:
        logger.warning(
            f"[sanitize_output] 拦截疑似泄漏输出（强{len(strong_hits)} 弱{weak_count}）：{text[:150]!r}"
        )
        return _SANITIZE_FALLBACK
    # ===== [UX Purge] 格式违规检测：泄漏工具调用/命令细节 → 标记打回重试 =====
    ux_hits = [p for p in _UX_LEAK_PATS if p.search(probe)]
    if ux_hits:
        logger.warning(
            f"[sanitize_output] UX 格式违规（泄漏工具/命令细节，命中{len(ux_hits)}条）：{text[:150]!r}"
        )
        return _UX_VIOLATION
    # ===== Sanitizer V2：裸链接 / 长参数链接 / 裸 JSON =====
    no_md = _strip_md_links(probe)
    if _V2_RAW_MEDIA_URL.search(no_md):
        logger.warning(f"[sanitize_output] V2 拦截裸媒体链接：{text[:150]!r}")
        return _SAN_V2_FALLBACK
    m = _V2_LONG_PARAM_URL.search(no_md)
    if m and ("?" in m.group(0) and ("&" in m.group(0) or "=" in m.group(0))):
        logger.warning(f"[sanitize_output] V2 拦截长参数链接（{len(m.group(0))}字符）：{text[:150]!r}")
        return _SAN_V2_FALLBACK
    if _V2_RAW_JSON.search(probe):
        logger.warning(f"[sanitize_output] V2 拦截裸 JSON：{text[:150]!r}")
        return _SAN_V2_FALLBACK
    if _AI_LIMITATION_PAT.search(probe):
        return _FIX_LIMITATION_MSG
    return text

# ===== [UX Purge] 打回重试 + [Execution Guard] 真执行强校验 =====
_UX_CORRECTION_MSG = (
    "【格式纠正】你上一条回复泄漏了工具调用细节（工具名/命令原文/参数）。"
    "请只用自然语言重述已完成的事与真实结果（如“已完成删除，剩余 N 个任务”），"
    "禁止出现任何工具名、函数名、命令原文（如 ls -l、rm -rf）、[调用工具: ...]、（执行命令: ...）等字样。"
)
_EXEC_GUARD_MSG = (
    "【执行校验】你声称已完成操作，但回复中没有工具执行后的真实返回结果。"
    "请把工具执行后的真实返回结果告诉我（例如实际输出、剩余任务列表、具体数值/状态），不要只复述命令或意图。"
)
_EXEC_TOOL_NAMES = (
    "cancel_scheduled_task", "clean_media", "del_admin", "delete_line",
    "execute_shell", "aup_execute_shell", "run_bash_script",
)


def _exec_tool_used(tool_calls) -> bool:
    """本轮是否调用了删除/执行类工具（需要真实结果佐证）。"""
    if not tool_calls:
        return False
    for tc in tool_calls:
        name = ""
        try:
            name = (tc.get("function") or {}).get("name", "")
        except Exception:
            name = ""
        if name in _EXEC_TOOL_NAMES:
            return True
    return False


def _evidence_tokens(output: str):
    """从工具真实输出中提取证据 token：任务 id / 数字 / 状态词。"""
    tokens = set()
    for m in re.finditer(r"sched_[a-zA-Z0-9]{4,}", output):
        tokens.add(m.group(0))
    for m in re.finditer(r"[0-9]{1,}", output):
        tokens.add(m.group(0))
    for w in ("成功", "失败", "完成", "已删", "已取消", "已移除", "剩余", "列表", "不存在", "错误", "已执行"):
        if w in output:
            tokens.add(w)
    return tokens


def _has_real_result(reply: str, tool_results: list) -> bool:
    """最终回复是否包含工具真实返回的关键内容（而非只复述命令/意图）。"""
    if not tool_results:
        return True
    reply_l = reply.lower()
    evidences = set()
    for r in tool_results:
        output = str(r.get("output", "") or "")
        evidences |= _evidence_tokens(output)
    if not evidences:
        return True  # 工具输出无证据 token（如纯提示性输出），不强校验
    # 删除/取消类工具：要求出现"剩余列表"特征（证明确实展示了删除后的剩余任务）
    joined = " ".join(str(r.get("output", "") or "") for r in tool_results)
    if "cancel_scheduled_task" in joined or "剩余任务" in joined or "task_id" in joined:
        if not re.search(r"剩余|当前还有|还有\s*[0-9]|共\s*[0-9]+\s*个|暂无.*任务|列表", reply):
            return False
    return any(str(tok) in reply_l for tok in evidences if len(str(tok)) >= 1)


async def _finalize_with_guard(messages, reply, tool_calls=None, tool_results=None, max_retry=2) -> str:
    """最终回复收口：格式违规 → 打回重试；删除/执行类轮次缺真实结果 → 注入追问重试。最多 max_retry 次。"""
    for attempt in range(max_retry + 1):
        sanitized = sanitize_output(reply)
        if sanitized == _UX_VIOLATION:
            logger.warning(f"[UX Purge] 格式违规打回重试（第{attempt + 1}次）：{reply[:100]!r}")
            messages.append({"role": "system", "content": _UX_CORRECTION_MSG})
            try:
                reply, _ = await safe_api_request(messages, strategy="chat", tools=None)
            except Exception as _re:
                logger.error(f"[UX Purge] 重试生成失败: {_re}", exc_info=True)
                break
            continue
        _exec_suspect = bool(tool_calls and _exec_tool_used(tool_calls) and tool_results
                             and not _has_real_result(reply, tool_results))
        if _exec_suspect and attempt == 0:
            logger.info("[Execution Guard] 首轮宽限放行，不打断：%r", reply[:80])
        if _exec_suspect and attempt > 0:
            logger.warning(f"[Execution Guard] 缺真实结果打回重试（第{attempt + 1}次）：{reply[:100]!r}")
            messages.append({"role": "system", "content": _EXEC_GUARD_MSG})
            try:
                reply, _ = await safe_api_request(messages, strategy="chat", tools=None)
            except Exception as _re2:
                logger.error(f"[Execution Guard] 重试生成失败: {_re2}", exc_info=True)
                break
            continue
        return sanitized
    return sanitize_output(reply)


def _ordered_messages(messages: list) -> list:
    """把 system 消息统一搬到最前面（组内顺序不变）。

    本仓库会在末尾追加「当前系统时间」「宿主环境」等 system 块，而 qwen 一类上游
    严格要求 system 只能出现在最开头；这一步只调整顺序、不改内容、不改模型。
    """
    if not isinstance(messages, list):
        return messages
    sys_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]
    others = [m for m in messages if not (isinstance(m, dict) and m.get("role") == "system")]
    if not sys_msgs:
        return messages
    if len(sys_msgs) > 1:
        # 多条 system 合并成一条：内容不丢，只按顺序拼接（qwen 一类上游只收单条 system）
        joined = "\n\n".join(str(m.get("content") or "") for m in sys_msgs).strip()
        sys_msgs = [{"role": "system", "content": joined}]
    if len(sys_msgs) == 1 and len(messages) == len(others) + 1 and messages[0].get("role") == "system":
        if messages[0].get("content") == sys_msgs[0].get("content"):
            return messages  # 已经是一条 system 且在最前，原样返回
    return sys_msgs + others


async def safe_api_request(
    messages: list,
    model: str = None,
    strategy: str = None,   # 历史形参：R18 起只当作模型别名解析，不再代表“岗位策略”
    timeout: int = None,
    tools: list = None,
    tool_choice: str = "auto",
    retries: int = None
) -> tuple[str, Optional[List[dict]]]:
    global _last_request_time
    global CURRENT_KEY_INDEX

    # ===== R18 模型优先：调用方给谁就发给谁；解析不出来就如实报错，绝不静默换模型 =====
    target = str(model or strategy or DEFAULT_MODEL).strip()
    config = get_strategy_config(target)
    if config is None:
        logger.error("[model] 未知模型：%s（models.json 里没有这个模型，拒绝请求）", target)
        return f"未知模型 {target}（models.json 里没有这个模型）", None

    upstream_model = config.get("model") or config.get("id") or target
    base_url = config.get("base_url") or NEWAPI_BASE
    api_keys = config.get("api_keys") or NEWAPI_KEYS
    if timeout is None:
        timeout = config.get("timeout", get_config("API_TIMEOUT_SECONDS"))
    if retries is None:
        retries = config.get("retries", 3)
    logger.info("[model] 请求模型=%s（入口=%s，base=%s）", upstream_model, target, base_url)

    last_used_key = None
    key_index = 0

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
                # R18：不回落其他模型（人工指定的模型失败就如实报错）
                return "所有 Key 不可用", None

            headers = {"Authorization": f"Bearer {current_key}", "Content-Type": "application/json"}
            data = {"model": upstream_model, "messages": _ordered_messages(messages)}
            logger.info("[model] 实际下发 roles=%s", [str(m.get("role")) for m in data["messages"]][:10])
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
                                # R18：不回落其他模型（人工指定的模型失败就如实报错）
                                return f"API HTTP 错误 {resp.status}: {error_text[:200]}", None
                            if resp.status == 429:
                                if _qm:
                                    _qm.rate_limited()
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
                        return _coerce_protocol(content, tool_calls)

        except asyncio.TimeoutError:
            if attempt < retries:
                delay = min(2 ** (attempt + 1), 30) + random.uniform(0, 0.3 * (2 ** attempt))
                logger.warning(f"请求超时，第 {attempt+1}/{retries} 次重试，等待 {delay:.2f}s")
                await asyncio.sleep(delay)
                continue
            # 尝试 fallback
            # R18：不回落其他模型（人工指定的模型失败就如实报错）
            return f"请求超时（{get_config('API_TIMEOUT_SECONDS')}秒），请稍后重试", None

        except Exception as e:
            if attempt < retries:
                error_str = str(e)
                if "429" in error_str or "tpm exhausted" in error_str or "rpm exhausted" in error_str:
                    if _qm:
                        _qm.rate_limited()
                    base_delay = min(5 * (2 ** attempt), 30)
                else:
                    base_delay = min(2 ** (attempt + 1), 30)
                delay = base_delay + random.uniform(0, base_delay * 0.3)
                logger.warning(f"请求异常: {e}，第 {attempt+1}/{retries} 次重试，等待 {delay:.2f}s")
                await asyncio.sleep(delay)
                continue
            # 尝试 fallback
            # R18：不回落其他模型（人工指定的模型失败就如实报错）
            return f"请求异常: {str(e)}", None

    # 所有重试用完
    # R18：不回落其他模型（人工指定的模型失败就如实报错）
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
                    return _coerce_protocol(content, tool_calls)
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
    TOOLS = get_service("admin.TOOLS")
    execute_tool_directly = get_service("admin.execute_tool_directly")

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

【绝对禁令】不得以任何方式改写 config_center/settings.py 或 wcu_policy.json；遇合规阻力调用 submit_system_proposal 呈递议案，不得绕行。
"""
    MemoryCenter.set_system(key, system_prompt)

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
            MemoryCenter.append_message(key, "tool", result[:500],
                                         tool_call_id=tc.get("id", f"step_{step}"))

    return "子任务执行超时或达到最大步数"

# [本地短路] 明确工具词 → 不请求大模型，直接判 tool（用户指定规则）
_STRONG_TOOL_RE = re.compile(
    r"(win_shell|win_ui_automation|interactive_browser|powershell|pwsh|终端|命令行|"
    r"remote-debugging|\bcdp\b)", re.I)
_SOFT_TOOL_RE = re.compile(
    r"(浏览器|\bchrome\b|\bedge\b|网页|\burl\b|https?://)", re.I)
_ACTION_VERB_RE = re.compile(
    r"(打开|启动|执行|运行|访问|截图|抓取|下载|登录|跳转|检查|测试|调用|连上|连接)", re.I)


# 动作词 + 界面对象（"打开计算器""点击发送按钮"）：明确的本机操作指令
_UI_OBJ_RE = re.compile(
    r"(计算器|记事本|画图|微信|qq|浏览器|网页|窗口|按钮|菜单|输入框|标签页|桌面|任务栏)",
    re.I)


def _local_rule_tool(prompt: str) -> str:
    """返回命中理由；空串表示未命中。浏览器类词必须带动作动词，避免把科普提问判成工具。"""
    p = prompt or ""
    m = _STRONG_TOOL_RE.search(p)
    if m:
        return "命中工具名「%s」" % m.group(0)
    m = _SOFT_TOOL_RE.search(p)
    if m and _ACTION_VERB_RE.search(p):
        return "命中「%s」+ 动作动词" % m.group(0)
    # [关键词兜底] 打开/点击… + 具体界面对象 → tool（用户指定规则）
    if _ACTION_VERB_RE.search(p) and _UI_OBJ_RE.search(p):
        return "动作词 + 界面对象"
    return ""


async def _detect_intent(prompt: str, context_messages: list = None) -> dict:
    """EHIR 三层漏斗入口：
    L1 静态正则预判（极速拦截，0 API）→ L3 触发检查（长消息/因果词强制大模型）
    → L2 动态词库（SQLite 命中，0 API）→ L3 大模型兜底 + 学习入库。
    返回 dict：{"label": "tool|chat|web", "reason": str, "confidence": float}
    """
    from plugins.core_utils.lexicon import LexiconDB, rule_match, should_llm_route

    # L0：本地短路（明确工具词）—— 0 依赖、0 API，不受上游 JSON 故障影响
    _quick = _local_rule_tool(prompt)
    if _quick:
        logger.info("[EHIR] 本地短路 → tool（%s）", _quick)
        return {"label": "tool", "reason": "本地短路:" + _quick, "confidence": 0.95}

    # L1：静态正则（极速拦截）
    hit = rule_match(prompt)
    if hit:
        return {"label": hit[0], "reason": hit[1], "confidence": hit[2]}

    # L3 触发检查：长消息 / 因果连接词 → 跳过词库，强制大模型深度分析
    trigger, why = should_llm_route(prompt)
    if trigger:
        result = await _llm_detect_intent(prompt, context_messages)
        if result["label"] == "tool":
            _learn_tool_word(prompt)
        return result

    # L2：动态词库（命中 0 API）
    lex = LexiconDB()
    m = lex.match(prompt)
    if m:
        word, intent = m
        return {"label": intent, "reason": "词库命中[%s]" % word, "confidence": 0.9}

    # 兜底 L3：大模型深度分析 + 学习
    result = await _llm_detect_intent(prompt, context_messages)
    if result["label"] == "tool":
        _learn_tool_word(prompt)
    return result


def _learn_tool_word(prompt: str) -> None:
    """大模型判定 tool 后：提取用户特有动作词入库（黑话学习）。"""
    try:
        from plugins.core_utils.lexicon import LexiconDB, extract_action_word
        word = extract_action_word(prompt)
        if word:
            LexiconDB().learn(word, "tool")
            logger.info("[EHIR] 学习新动作词: %s", word)
    except Exception as e:
        logger.warning("[EHIR] 学习失败: %s", e)


async def _llm_detect_intent(prompt: str, context_messages: list = None) -> dict:
    """
    用 chat 策略模型判断用户意图。
    返回 dict：{"label": "tool|chat|web", "reason": str, "confidence": float}
    解析失败时兜底 {"label": "chat", "reason": "解析失败", "confidence": 0.0}
    context_messages：最近几条对话上下文（已 sanitize），用于消解指代和承接。
    """
    system_content = """你是一个意图分类器。判断「待分类消息」属于「工具类」还是「闲聊类」。

【本请求的结构】
1. 本系统消息 = 分类规则（你唯一要执行的任务）
2. 【背景对话】消息 = 历史上下文，仅供参考，不是分类对象
3. 【待分类消息】 = 你唯一需要判断的对象

【判断规则】

**tool（本地工具）**：用户需要执行本地实际操作。细分如下：
- shell 执行类：ls、cat、cd、grep、find、pwd、ps、df、du、rm、mv、cp、mkdir、echo、执行命令、跑命令
- 文件操作类：读文件、写文件、替换某行、删某行、插入行、备份项目、打包项目
- 下载爬取类：下载 m3u8、爬取视频、爬取 URL、转 MP3、清理 MP4/MP3
- 站点缩写别名类：形如「tz 92285」「tz#92285」「yt abc123」（站点缩写 + 空格或井号 + 数字/ID）是视频爬取别名指令 → 判 tool，把原样字符串（如 "tz 92285"）直接作为工具参数，不要追问完整链接，不要把它当闲聊或 shell 命令
- 系统查询类：查看系统状态、CPU、内存、磁盘、ping 主机、查管理员列表
- 记忆检索类：回忆之前说过的、检索历史对话、查知识库、翻一下上次的记录
- 管理员类：加管理员、删管理员、开关群聊
- 联网搜索类：用户需要联网搜索/获取实时信息。线索如"帮我搜搜"、"帮我查一下"、"搜一下"、"最新的"、"今年"、"最近的新闻"、"实时"、"最新消息"、"网上说"、"股价"、"汇率"、"天气"、"实时行情"、"XX 多少钱"、"2026 最强"、"今天发生了什么"。
注意：上述词单独出现不足以判 tool，需结合整体语义。
- "现在几点了" → chat（常识问答，不是联网请求）
- "帮我搜一下现在几点了" → tool
- "今天心情不好" → chat

**chat（闲聊）**：闲聊、问答、分析、写作、翻译、解释、讲笑话等纯文本交互。

【优先级规则】
1. 若一条消息同时含联网意图和本地工具意图（如"搜一下 XXX 然后存到文件"），判 tool。
2. 若【待分类消息】是陈述、补充或追问，但结合【背景对话】是在承接某个工具类任务（补充路径、参数、确认对象），判 tool。
3. 二分类互斥，label 只能是一个词。

【对称化代价 · 必须遵守】
判错 tool 与判错 chat 的代价是对等的：
- 把明确指令判成 chat = 用户指令被当空气，等于失职
- 把闲聊判成 tool = 多调一次工具，只是浪费
禁止因"不确定"或"害怕出错"而选 chat。
若消息含明确动作指令（命令、文件操作、下载、爬取、备份、搜索、查询、绘图/画图/生成图像等），必须判 tool。
即使你觉得这个操作"看起来不危险"或"可能不需要执行"，也必须判 tool——是否执行由代码决定，不是你的责任。

【输出格式】
只输出一个 JSON 对象，不要任何解释、寒暄、Markdown 代码块围栏。
格式：{"label": "tool", "reason": "简短理由（不超过 30 字）", "confidence": 0.0}
label 只能是 tool / chat 之一；confidence 为 0.0 到 1.0 之间。

【示例】
"ls /root" → {"label": "tool", "reason": "明确的 shell 命令", "confidence": 0.98}
"你好" → {"label": "chat", "reason": "普通问候", "confidence": 0.98}
"帮我搜一下 2026 年 AI 大事" → {"label": "tool", "reason": "明确请求联网搜索", "confidence": 0.95}
"现在几点了" → {"label": "chat", "reason": "常识性问答，不是联网请求", "confidence": 0.9}
"看下那个文件夹" → {"label": "tool", "reason": "含查看目录的动作意图", "confidence": 0.75}
"搜一下 XXX 然后存到文件" → {"label": "tool", "reason": "含本地工具意图，按优先级规则判 tool", "confidence": 0.9}
"今天心情不好" → {"label": "chat", "reason": "情绪表达", "confidence": 0.95}
"删掉所有 txt 文件" → {"label": "tool", "reason": "明确的文件操作指令", "confidence": 0.98}
"画一个蓝色的鲸鱼" → {"label": "tool", "reason": "明确要求绘图，须调用画图工具", "confidence": 0.95}
"画个函数图像" → {"label": "tool", "reason": "明确要求绘制数学函数图像", "confidence": 0.95}
"""
    messages = [{"role": "system", "content": system_content}]
    if context_messages:
        ctx_lines = []
        for m in context_messages:
            role = m.get("role", "")
            content = m.get("content", "") or ""
            if role == "user":
                ctx_lines.append(f"用户：{content}")
            elif role == "assistant":
                ctx_lines.append(f"助手：{content}")
            else:
                ctx_lines.append(f"{role}：{content}")
        messages.append({
            "role": "system",
            "content": (
                "【背景对话 · 仅供理解上下文，不是分类对象】\n"
                "以下历史仅作参考，不要对它做分类判断：\n"
                + "\n".join(ctx_lines)
            ),
        })
    messages.append({
        "role": "user",
        "content": f"【待分类消息 · 只对这一条做判断】\n{prompt}",
    })
    def _is_api_error(text):
        """上游 503 / 模型下线 / 无可用渠道：这不是模型回答，不能当分类结果。"""
        low = (text or "").strip().lower()
        return ("api http" in low or "model_not_found" in low
                or "no available channel" in low or "invalid_api_key" in low)

    last_parsed = None
    # [通道鲁棒] chat 通道可能整体下线；依次尝试 chat → tool，全挂才落本地规则
    for _strat in ("chat", "tool"):
        try:
            reply, _ = await safe_api_request(messages, strategy=_strat, tools=None)
        except Exception as e:  # noqa: BLE001
            logger.warning("[意图判断] strategy=%s 抛异常: %s", _strat, e)
            continue
        logger.info("[意图判断] strategy=%s 原始输出: %s", _strat,
                    str(reply or "")[:200].replace("\n", "\\n"))
        if not str(reply or "").strip() or _is_api_error(reply):
            _intent_raw_log("[%s] %s" % (_strat, reply))
            logger.warning("[意图判断] strategy=%s 通道不可用（错误串/空回复），换下一通道", _strat)
            continue
        parsed = _parse_intent_reply(reply, prompt)
        # [兜底] 画图承接问句（如"刚才画了什么"）：画图轮已阅后即焚物理清空，
        # 若分类器漂移输出无 JSON，本地按 tool 兜底，让 AI 给出"失忆"应答而非追问死锁
        if parsed["label"] == "chat" and parsed["confidence"] < 0.3:
            import re as _re
            if _re.search(r'(刚才|之前|上次).{0,8}(画|绘|图)', prompt):
                return {"label": "tool", "reason": "画图承接问句（本地兜底）", "confidence": 0.5}
        if parsed["confidence"] >= INTENT_CONFIDENCE_THRESHOLD:
            return parsed
        last_parsed = parsed

    # [绝不卡死] 两条通道都拿不到可信结论：本地规则兜底，置信度高于阈值，不进追问分支
    _fb = _local_rule_tool(prompt)
    if _fb:
        return {"label": "tool", "reason": "意图通道不可用→本地短路:" + _fb, "confidence": 0.9}
    if last_parsed:
        return {"label": last_parsed["label"],
                "reason": "意图通道低置信（%s）→按原判继续" % last_parsed["label"],
                "confidence": INTENT_CONFIDENCE_THRESHOLD}
    return {"label": "chat", "reason": "意图通道不可用（默认闲聊，不追问死锁）", "confidence": 0.65}


def _intent_raw_log(reply: str) -> None:
    """解析失败时把上游原始输出留样，便于事后定位（不阻塞主流程）。"""
    try:
        import os as _os
        import time as _t
        _dir = _ROOT + "/logs"
        _os.makedirs(_dir, exist_ok=True)
        with open(_os.path.join(_dir, "intent_raw.log"), "a", encoding="utf-8") as _f:
            _f.write("%s\t%s\n" % (_t.strftime("%Y-%m-%d %H:%M:%S"),
                                    (reply or "").replace("\n", "\\n")[:400]))
    except Exception:
        pass


def _parse_intent_reply(reply: str, prompt: str = "") -> dict:
    """解析意图判断模型的输出；**任何畸形输出都不再返回 0 置信度的死锁值**。

    顺序：去围栏 → json.loads → 贪婪 {.*} → 宽松修复（单引号/尾逗号）
    → "label": "tool|chat" 关键词抽取 → 裸词 tool/chat → 原始输出留样 + 低置信兜底。
    """
    if not reply:
        _intent_raw_log(reply)
        return {"label": "chat", "reason": "空回复", "confidence": 0.55}
    text = reply.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        body = "\n".join(lines[1:])
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        text = body.strip()

    obj = None
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        obj = None

    if not isinstance(obj, dict):
        import re as _re
        m = _re.search(r'\{.*\}', text, _re.DOTALL)
        if m:
            raw = m.group(0)
            for cand in (raw, raw.replace("'", '"'),
                         _re.sub(r',\s*([}\]])', r'\1', raw)):
                try:
                    obj = json.loads(cand)
                    break
                except (json.JSONDecodeError, ValueError):
                    continue

    if not isinstance(obj, dict):
        import re as _re
        m = _re.search(r'["\']?label["\']?\s*[:=]\s*["\']?\s*(tool|chat|web)', text, _re.I)
        if not m:
            m = _re.search(r'\b(tool|chat|web)\b', text, _re.I)
        if m:
            lab = m.group(1).lower()
            return {"label": "tool" if lab == "web" else lab,
                    "reason": "非 JSON，按关键词抽取", "confidence": 0.7}
        _intent_raw_log(reply)
        # [关键词兜底] 彻底解析失败也不追问：按用户原话里的动作/工具词判 tool，否则 chat
        _kw = _local_rule_tool(prompt)
        if _kw:
            return {"label": "tool", "reason": "解析失败→关键词兜底:" + _kw, "confidence": 0.75}
        return {"label": "chat", "reason": "解析失败→默认闲聊（已留样）", "confidence": 0.65}
    label = str(obj.get("label", "")).strip().lower()
    # [生态收敛] web 意图已并入 tool：旧缓存/模型输出 web → 归一化为 tool
    if label == "web":
        label = "tool"
    if label not in ("tool", "chat"):
        return {"label": "chat", "reason": f"非法 label: {label}", "confidence": 0.0}
    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(obj.get("reason", ""))[:60]
    return {"label": label, "reason": reason, "confidence": confidence}
# ==================== ask_ai 核心函数（已增加 Pi 模式分支） ====================
# [Plugin 4.0+] 多轮工具循环上限：模型可基于工具结果连续决策（初始 1 轮 + 追加轮）
TOOL_MAX_ROUNDS = 5  # 模型可基于工具结果连续决策（爬取漏斗：dom_hook->probe->video_link->custom 需多轮）

# ==================== [C-End Tool Proxy] C 端代理工具调用 MVP ====================
# 硬开关：外部 one-api 网关故障时，用免费 C 端（DeepSeek 网页版）代理工具调用决策
# ===== [智能熔断] 废弃硬编码旁路：默认关闭 C 端代理，仅主 API 异常时熔断降级 =====
USE_CEND_FOR_TOOL_PROXY = False


def _tool_role_allowed(name: str, info: dict, user_id: str) -> bool:
    """单用户本地部署：角色白名单已废弃，全部工具对所有请求可见。"""
    return True


def _filter_tools_for_prompt(user_prompt: str, tools_schema: list, max_n: int = 6) -> list:
    """按用户消息关键词筛选最相关工具，压缩注入列表，降低 C 端"没有工具"幻觉。"""
    kw = user_prompt.lower()
    # 高频触发词 → 强相关工具名
    strong_map = {
        "搜索": ["web_search", "search_and_read"],
        "查": ["web_search", "search_and_read"],
        "搜": ["web_search", "search_and_read"],
        "价格": ["web_search", "search_and_read"],
        "行情": ["web_search", "search_and_read"],
        "新闻": ["web_search", "search_and_read"],
        "天气": ["web_search", "search_and_read"],
        "爬取": ["crawler_download", "robust_crawler", "search_and_read"],
        "下载": ["crawler_download", "robust_crawler", "download_video_by_url"],
        "视频": ["download_video_by_url", "crawler_download", "robust_crawler"],
        "文件": ["feishu_send_file", "file_write", "file_read"],
        "发送": ["feishu_send_file", "feishu_send_image"],
        "打开": ["interactive_browser"],
        "看看": ["interactive_browser"],
        "浏览": ["interactive_browser"],
        "网页": ["interactive_browser"],
        "网站": ["interactive_browser", "web_security_audit"],
        "网址": ["interactive_browser"],
        "http": ["interactive_browser"],
        "巡检": ["web_security_audit"],
        "扫描": ["web_security_audit"],
        "漏洞": ["web_security_audit"],
        "安全": ["web_security_audit"],
        "检测": ["web_security_audit"],
        "powershell": ["win_shell"],
        "cpu": ["win_shell"],
        "内存": ["win_shell"],
        "磁盘": ["win_shell"],
        "进程": ["win_shell"],
        "注册表": ["win_shell"],
        "命令行": ["win_shell"],
        "cmd": ["win_shell"],
        "windows": ["win_shell", "win_ui_automation"],
        "计算器": ["win_ui_automation"],
        "记事本": ["win_ui_automation"],
        "微信": ["win_ui_automation"],
        "qq": ["win_ui_automation"],
        "控件": ["win_ui_automation"],
        "桌面软件": ["win_ui_automation"],
        "ui自动化": ["win_ui_automation"],
        "议案": ["submit_system_proposal"],
        "提案": ["submit_system_proposal"],
        "修改提案": ["submit_system_proposal"],
        "放权": ["submit_system_proposal"],
        "proposal": ["submit_system_proposal"],
        "白名单": ["win_ui_automation", "submit_system_proposal"],
        "拦截": ["submit_system_proposal"],
    }
    picked = []
    seen = set()
    for _k, _names in strong_map.items():
        if _k in kw:
            for _n in _names:
                if _n not in seen:
                    for s in tools_schema:
                        if (s.get("name") or s.get("function", {}).get("name")) == _n:
                            picked.append(s)
                            seen.add(_n)
                            break
    if not picked:
        # 无关键词命中：取 web 类 + 前几个通用工具
        for s in tools_schema:
            name = (s.get("name") or s.get("function", {}).get("name") or "")
            if name in ("web_search", "search_and_read") and name not in seen:
                picked.append(s)
                seen.add(name)
    for s in tools_schema:
        if len(picked) >= max_n:
            break
        name = (s.get("name") or s.get("function", {}).get("name") or "")
        if name not in seen:
            picked.append(s)
            seen.add(name)
    # [配置保护] 沙盒修改提案工具常驻：不受 max_n 裁剪，被拦时模型必须看得见它
    for s in tools_schema:
        _nm = (s.get("name") or s.get("function", {}).get("name") or "")
        if _nm == "submit_system_proposal" and _nm not in seen:
            picked.insert(0, s)
            seen.add(_nm)
    return picked[:max_n]


def _build_cend_tool_prompt(user_prompt: str, tools_schema: list) -> str:
    """把可用工具列表 + 强制 JSON 格式约束 + 用户问题拼成单个 C 端 prompt。"""
    lines = [
        MemoryCenter.build_system_prompt(),
        "",
        "你是「工具决策模块」，运行在一个可以调用外部工具的系统里。",
        "你【确实】具备调用工具的能力，工具列表如下：",
        "",
    ]
    for s in tools_schema:
        name = s.get("name") or s.get("function", {}).get("name", "?")
        desc = (s.get("description") or s.get("function", {}).get("description") or "")[:150]
        params = s.get("parameters", {}).get("properties", {})
        pnames = ",".join(params.keys()) if params else "无参数"
        lines.append(f"- {name}({pnames}): {desc}")
    lines += [
        "",
        "【调用规则·必须遵守】",
        "1. 如果你需要调用工具来回答，请仅输出如下 JSON 格式，不要包含任何其他解释文字：",
        '{"tool": "工具名", "args": {"参数名": "参数值"}}',
        "2. 如果你可以直接回答用户，请直接输出纯文本，不要输出 JSON。",
        "3. 绝对禁止回答“我没有该工具/没有搜索功能/无法查询”之类的否定话术。",
        "   ——你能调用上述工具；搜索类问题必须优先输出 web_search 或 search_and_read 的 JSON。",
        "4. 示例（仅当需要搜索时）：{\"tool\": \"web_search\", \"args\": {\"query\": \"关键词\"}}",
        "",
        f"用户问题：{user_prompt}",
    ]
    return "\n".join(lines)


def _parse_cend_tool_call(text: str):
    """解析 C 端输出：命中工具 JSON 则返回 (tool_name, args)，否则返回 None（纯文本）。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if len(lines) >= 3:
            t = "\n".join(lines[1:-1]).strip()
    obj = None
    try:
        obj = json.loads(t)
    except (json.JSONDecodeError, ValueError):
        start = t.find("{")
        end = t.rfind("}")
        if start != -1 and end > start:
            try:
                obj = json.loads(t[start:end + 1])
            except (json.JSONDecodeError, ValueError):
                return None
    if not isinstance(obj, dict):
        return None
    tool = str(obj.get("tool", "")).strip()
    if not tool:
        return None
    args = obj.get("args")
    if isinstance(args, dict):
        return tool, args
    if isinstance(args, str):
        try:
            return tool, json.loads(args)
        except (json.JSONDecodeError, ValueError):
            return tool, {}
    return tool, {}


def _looks_like_shopping(prompt: str) -> bool:
    """购物意图静态判定（用于路由保护）。"""
    return any(k in prompt for k in ("帮我买", "帮我购买", "买一个", "买两个", "买三", "购买", "下单", "加购物车", "去结算", "停在支付页面", "停在支付页", "帮我买点"))


def _extract_shopping_query(prompt: str) -> str:
    """从购物指令中提取商品关键词：'帮我买一个鼠标垫' → '鼠标垫'。"""
    t = prompt
    for _s in ("停在支付页面", "停在支付页", "记住", "先清空购物车", "先清空", "清空购物车", "，", ",", "。", "！", "？"):
        t = t.replace(_s, " ")
    for _a in ("帮我买一个", "帮我买一份", "帮我购买一个", "帮我购买", "帮我买", "购买一个", "买一个", "买", "下单"):
        if _a in t:
            t = t.split(_a, 1)[1]
            break
    t = t.strip()
    for _sep in (" ", "，", ",", "的", "吧"):
        if _sep in t:
            t = t.split(_sep, 1)[0]
            break
    return t.strip() or "商品"


async def _cend_tool_proxy(user_prompt: str, user_id: str, tools_schema: list) -> str:
    """C 端代理：文本模拟 Function Calling（MVP）。
    1) 注入工具列表 + 强制 JSON 格式 → 调 C 端
    2) 解析 JSON：命中 tool 字段 → execute_tool_directly 执行
    3) 工具结果作为 user 消息二次传回 C 端 → 最终自然语言回复
    4) 解析失败 → C 端纯文本直接返回（降级纯文本兜底）
    """
    if deepseek_web_integration is None:
        raise RuntimeError("C 端通道不可用")
    sub_tools = _filter_tools_for_prompt(user_prompt, tools_schema)
    c_prompt = _build_cend_tool_prompt(user_prompt, sub_tools)
    logger.info("[C端代理] 第 1 轮：调用 C 端（注入 %d/%d 个工具）", len(sub_tools), len(tools_schema))
    first = await deepseek_web_integration.ask_web_deepseek(c_prompt, user_id=user_id)
    logger.info("[C端代理] C 端原始输出: %s", (first or "")[:300])
    parsed = _parse_cend_tool_call(first)
    if parsed is None:
        logger.info("[C端代理] 无工具 JSON，走纯文本兜底（长度 %d）", len(first or ""))
        return first or ""
    tool_name, args = parsed
    logger.info("[C端代理] 解析到工具调用: %s args=%s", tool_name, json.dumps(args, ensure_ascii=False)[:300])
    # [购物路由保护] 购物指令被误选成登录工具 → 强制重定向到购物助手（购物绝不预登录）
    if tool_name == "gui_shopping_login" and _looks_like_shopping(user_prompt):
        _q = _extract_shopping_query(user_prompt)
        _site_url = ""
        for _site, (_url, _prof) in _L1_SITES.items():
            if _site in (user_prompt or ""):
                _site_url = _url
                break
        logger.info("[购物路由] 拦截误选 gui_shopping_login → 重定向 gui_shopping_assistant (query=%s url=%s)", _q, _site_url)
        tool_name = "gui_shopping_assistant"
        args = {"action": "search", "url": _site_url, "query": _q}
    execute_tool_directly = get_service("admin.execute_tool_directly")
    try:
        result = await execute_tool_directly(tool_name, args, user_id)
    except Exception as e:
        logger.error(f"[C端代理] 执行工具 {tool_name} 失败: {e}", exc_info=True)
        result = f"工具执行异常: {str(e)}"
        # [技能库自愈] C端代理路径：报错 → skill_base 检索排错指南，附带在结果中供二次传回
        # [Tool Boundary] AUP 明确业务错误（UNSUPPORTED_PLATFORM 等）→ 不查技能库，直接回传错误原因
        if not _is_aup_business_error(str(e)):
            try:
                _skill_guides = _query_skill_base(str(e))
                if _skill_guides:
                    result = f"{result}\n【排错指南·{_skill_guides[0]['title']}】{_skill_guides[0]['content']}"
                    logger.info("[技能库] C端代理工具报错命中排错指南: %s", _skill_guides[0]["title"])
            except Exception:
                pass
    # [生态收敛] 本地检索工具失败/被反爬 → ask_cend_ai 兜底
    if tool_name in ("web_search", "search_and_read", "read_webpage"):
        _rs = str(result)[:200]
        _ok = ("执行成功" in _rs) or ("status=ok" in _rs) or ('"status": "ok"' in _rs)
        if not _ok and any(_k in _rs.lower() for _k in ("失败", "错误", "加载", "无法", "异常", "超时", "timeout", "error", "反爬")):
            logger.info("[C端代理] 本地 %s 结果异常，自动改调 ask_cend_ai 兜底", tool_name)
            try:
                cend_fb = await ask_cend_ai(user_prompt)
                if hasattr(cend_fb, "data"):
                    cend_fb = cend_fb.data  # AUPResult 解包（aup_tool wrapper 返回）
                if cend_fb:
                    result = f"{result}\n\n[ask_cend_ai 兜底结果]\n{cend_fb}"
                    logger.info("[C端代理] ask_cend_ai 兜底完成（长度 %d）", len(str(cend_fb)))
            except Exception as e:
                logger.error("[C端代理] ask_cend_ai 兜底失败: %s", e)
    logger.info("[C端代理] 工具结果（前 200 字）: %s", str(result)[:200])
    # ===== 二次对话：工具结果传回 C 端 =====
    round2 = (
        f"[工具执行结果]: {result}\n\n"
        f"请根据以上工具执行结果，用自然语言总结回答用户的问题。\n"
        f"用户问题：{user_prompt}"
    )
    logger.info("[C端代理] 第 2 轮：工具结果二次传回 C 端")
    final = await deepseek_web_integration.ask_web_deepseek(round2, user_id=user_id)
    logger.info("[C端代理] 最终回复（前 200 字）: %s", (final or "")[:200])
    return final or ""


# ==================== [智能熔断] 主 API 异常 → C 端代理降级 helper ====================
# safe_api_request 失败时返回错误串（此前缀可判，含主通道与 fallback 备用配置路径）
_MAIN_API_FAIL_PREFIXES = ("请求超时", "请求异常", "请求失败", "已达最大重试", "备用配置超时", "备用配置异常", "备用配置重试失败")


def _is_main_api_failure(reply) -> bool:
    """safe_api_request 失败时返回错误串（此前缀可判）；异常路径由调用方 try 捕获"""
    return isinstance(reply, str) and reply.startswith(_MAIN_API_FAIL_PREFIXES)


async def _circuit_breaker_to_cend(prompt: str, user_id: str, tools_schema: list, error) -> tuple[str, Optional[List[dict]]]:
    """[熔断生效] 主 API 异常 → 本次请求自动降级至 C 端代理完成响应"""
    logger.warning("[熔断生效] 主 API 异常（%s），本次请求自动降级至 C 端代理", error)
    if deepseek_web_integration is not None and deepseek_web_integration.is_enabled():
        try:
            reply = await _cend_tool_proxy(prompt, user_id, tools_schema)
            return reply, None
        except Exception as e2:
            logger.error("[熔断降级] C 端代理也失败: %s", e2)
            return f"主 API 异常（{error}），C 端代理降级也失败（{e2}）", None
    return f"主 API 异常（{error}），且 C 端代理不可用", None

# ==================== [生态收敛] ask_cend_ai 系统级宏工具 ====================
# C 端代理（文本模拟 Function Calling）固化注册为标准 @aup_tool，
# 与本地检索插件（web_search / read_webpage）互补：本地搜索失效/反爬时由 AI 主动调用兜底。
try:
    from aup_core.decorators import aup_tool as _aup_tool

    @_aup_tool(
        name="ask_cend_ai",
        description="当本地搜索工具(web_search)无法获取有效信息，或遇到强力反爬时，调用此工具。它会借助外部大模型的联网能力获取信息。",
        category="network",
        is_danger=False,
        params_schema={
            "required": ["prompt"],
            "properties": {
                "prompt": {"type": "string", "description": "要查询的问题或指令"}
            },
        },
        version="1.0.0",
    )
    async def ask_cend_ai(prompt: str) -> str:
        """借助外部大模型（DeepSeek 网页版）的联网能力获取信息。"""
        if deepseek_web_integration is None or not deepseek_web_integration.is_enabled():
            return "C 端通道不可用：deepseek_web_integration 未启用"
        try:
            logger.info("[ask_cend_ai] 调用 C 端联网获取信息: %s", (prompt or "")[:80])
            result = await deepseek_web_integration.ask_web_deepseek(prompt, user_id=None)
            return result or "C 端未返回有效内容"
        except Exception as e:
            logger.error("[ask_cend_ai] 调用失败: %s", e)
            return f"C 端联网失败: {str(e)[:200]}"

    # 注册入库（SQLite 注册网关，CompatView 实时可见，重启不丢）
    try:
        from tool_center.registry_manager import register_tool as _register_tool
        _register_tool(
            "ask_cend_ai",
            ask_cend_ai,
            description="当本地搜索工具(web_search)无法获取有效信息，或遇到强力反爬时，调用此工具。它会借助外部大模型的联网能力获取信息。",
            category="network",
        )
        logger.info("[ask_cend_ai] 已注册入库（SQLite）")
    except Exception as _reg_err:
        logger.warning("[ask_cend_ai] 注册入库失败: %s", _reg_err)
except Exception as _aup_err:
    logger.warning("[ask_cend_ai] aup_tool 导入失败，工具不可用: %s", _aup_err)

# ===== [Semi-Auto Shopping Assistant] 半自动购物宏工具（Windows 宿主有头 Edge + Playwright） =====
try:
    import subprocess as _sp
    import asyncio as _asyncio_mod

    def _shop_serialized(async_fn):
        """浏览器资源串行化：同一 shop_profile 的 Edge 不允许并发触发（防 User Data Dir 被占用冲突）。
        多个 Playwright 购物/登录工具同时触发时，全局互斥排队执行。"""
        try:
            _shop_lock = _asyncio_mod.Lock()
        except Exception:
            _shop_lock = None

        async def _wrap(*args, **kwargs):
            if _shop_lock is None:
                return await async_fn(*args, **kwargs)
            async with _shop_lock:
                return await async_fn(*args, **kwargs)

        # [Critical] 必须保留原签名：AUP wrapper 依赖 inspect.signature 构造 valid_params，
        # 若不 wraps，签名退化为 (*args, **kwargs) 会导致 execute_tool_directly 参数全丢 → INVALID_ARGUMENT
        return functools.wraps(async_fn)(_wrap)

    @_aup_tool(
        name="gui_shopping_assistant",
        description="【购物下单专用】当用户要求买/购买/下单/加购物车/去结算/停在支付页面等购物操作时，必须调用本工具。半自动购物助手：接受通用动作 search/add_to_cart/checkout 与目标站点 URL，Windows 宿主有头 Edge 打开 URL，页面定位（搜索框/加购/结算按钮）由 DOM 文本判断，绝不写死任何平台选择器；自动执行动作并停在订单确认页截图，交由人类扫码支付；AI 绝不碰支付密码/最终付款。参数：action(通用动作：search/add_to_cart/checkout，默认 search)、url(目标购物站点URL，必填)、query(商品关键词)、quantity(数量,默认1)。",
        category="shopping",
        is_danger=True,
        params_schema={
            "required": ["url"],
            "properties": {
                "action": {"type": "string", "description": "通用动作：search / add_to_cart / checkout，默认 search"},
                "url": {"type": "string", "description": "目标购物站点 URL（必填），页面定位由 DOM 文本判断"},
                "query": {"type": "string", "description": "要购买的商品关键词，如：可乐"},
                "quantity": {"type": "integer", "description": "购买数量，默认 1"}
            },
        },
        version="1.1.0",
    )
    @_shop_serialized
    async def gui_shopping_assistant(action: str = "search", url: str = "", query: str = "", quantity: int = 1) -> str:
        """半自动购物助手（通用动作 + URL）：Windows 宿主有头 Edge 打开任意购物站点 URL，
        页面定位由 DOM 文本判断（不写死任何平台选择器），自动执行动作并停在订单确认页。"""
        _action = (action or "search").strip().lower()
        if _action not in ("search", "add_to_cart", "checkout"):
            # [Tool Boundary] 动作边界：只接受通用动作，页面定位交给 LLM/DOM 文本判断
            raise AUPError("UNSUPPORTED_ACTION", f"动作 {_action} 暂不支持（仅支持 search/add_to_cart/checkout）")
        if not url:
            raise AUPError("MISSING_URL", "gui_shopping_assistant 必须提供目标站点 URL（页面定位由 DOM 文本判断）")
        _platform = "generic" 
        _win_py = "/mnt/c/Users/Admin（无密码）/AppData/Local/Programs/Python/Python314/python.exe"
        _script = r"C:\Users\Public\shop_tool.py"
        try:
            _qty = int(quantity) if str(quantity).isdigit() else 1
        except Exception:
            _qty = 1
        logger.info("[gui_shopping_assistant] 启动 platform=%s query=%s qty=%d", _platform, query, _qty)
        try:
            _proc = _sp.run(
                [_win_py, _script, _action, url, str(query), str(_qty)],
                capture_output=True, text=True, timeout=200,
            )
        except Exception as _sp_err:
            logger.error("[gui_shopping_assistant] 执行异常: %s", _sp_err)
            return f"购物助手执行失败: {str(_sp_err)[:200]}"
        _out = (_proc.stdout or "").strip()
        if not _out:
            return f"购物助手无输出（rc={_proc.returncode}）：{(_proc.stderr or '')[:200]}"
        try:
            _res = json.loads(_out.splitlines()[-1])
        except Exception:
            _res = {"status": "raw", "output": _out[-500:]}
        _lines = [f"站点: {_res.get('platform', _platform)} | 动作: {_action} | 状态: {_res.get('status', 'unknown')}"]
        for _i, _p in enumerate(_res.get("products", [])[:3], 1):
            _lines.append(f"{_i}. {_p.get('title', '')} | {_p.get('price', '')}")
        if _res.get("order_total"):
            _lines.append(f"订单合计: {_res['order_total']}")
        if _res.get("screenshot"):
            _lines.append(f"截图: {_res['screenshot']}")
        if _res.get("message"):
            _lines.append(_res["message"])
        elif _res.get("status") == "error":
            _lines.append(f"错误: {_res.get('message', '')}")
        _lines.append("（请勿在回复中粘贴任何网址链接，直接告知用户已停在支付确认页即可）")
        return "\n".join(_lines)

    try:
        from tool_center.registry_manager import register_tool as _register_tool2
        _register_tool2(
            "gui_shopping_assistant",
            gui_shopping_assistant,
            description="【购物下单专用】当用户要求买/购买/下单/加购物车/去结算/停在支付页面等购物操作时，必须调用本工具。半自动购物助手：接受通用动作 search/add_to_cart/checkout 与目标站点 URL，Windows 宿主有头 Edge 打开 URL，页面定位（搜索框/加购/结算按钮）由 DOM 文本判断，绝不写死任何平台选择器；自动执行动作并停在订单确认页截图，交由人类扫码支付；AI 绝不碰支付密码/最终付款。每次执行会先强制清空购物车（保证订单里只有本次目标商品）；自带登录态检测，登录失效时返回 login_expired——此时请主人确认后改用 auth_playwright_fetch(url=当前页面URL, profile_name=shop_profile) 弹窗扫码登录，不要预先登录。",
            category="shopping",
        )
        logger.info("[gui_shopping_assistant] 已注册入库（SQLite）")
    except Exception as _reg2_err:
        logger.warning("[gui_shopping_assistant] 注册入库失败: %s", _reg2_err)
except Exception as _shop_err:
    logger.warning("[gui_shopping_assistant] 宏工具导入失败: %s", _shop_err)




# ===== [Semi-Auto Shopping] 京东扫码登录宏工具（配套登录态更新） =====
try:
    @_aup_tool(
        name="gui_shopping_login",
        description="【已废弃，勿调用】任何登录/存凭证意图一律使用通用凭证工具 auth_playwright_fetch(url=目标URL, profile_name=凭证名)，与站点无关。本工具（gui_shopping_login）仅保留兼容：仅在历史场景触发时弹出 Edge 窗口等待扫码（登录态自动保存到 shop_profile，等待最长 5 分钟）。常规购物请求不要预先调用本工具。",
        category="shopping",
        is_danger=False,
        params_schema={"required": [], "properties": {}},
        version="1.0.0",
    )
    @_shop_serialized
    async def gui_shopping_login() -> str:
        """京东扫码登录：Windows Edge 弹窗等待扫码，登录态保存 shop_profile。"""
        import subprocess as _sp
        _win_py = "/mnt/c/Users/Admin（无密码）/AppData/Local/Programs/Python/Python314/python.exe"
        _script = r"C:\Users\Public\shop_login.py"
        logger.info("[gui_shopping_login] 启动扫码登录窗口")
        try:
            _proc = _sp.run([_win_py, _script, "300"], capture_output=True, text=True, timeout=320)
        except Exception as _sp_err:
            logger.error("[gui_shopping_login] 执行异常: %s", _sp_err)
            return f"登录窗口启动失败: {str(_sp_err)[:200]}"
        _out = (_proc.stdout or "").strip()
        _last = _out.splitlines()[-1] if _out else ""
        try:
            _r = json.loads(_last)
        except Exception:
            return f"登录窗口已关闭（rc={_proc.returncode}）：{_out[-200:]}"
        if _r.get("logged"):
            return "京东扫码登录成功，登录态已保存到 shop_profile。主人可以继续告诉我买什么了。"
        return "登录超时或未完成扫码，京东窗口已关闭。可让主人再次发送“帮我扫码登录京东”重新弹出登录窗口。"

    try:
        from tool_center.registry_manager import register_tool as _register_tool3
        _register_tool3(
            "gui_shopping_login",
            gui_shopping_login,
            description="【已废弃，勿调用】本工具已隐藏（is_active=0）。任何登录/存凭证意图一律使用 auth_playwright_fetch(url=目标URL, profile_name=凭证名) 弹窗扫码（底层 auth_profile_tool.py）。",
            category="shopping",
            is_active=False,
        )
        logger.info("[gui_shopping_login] 已注册入库（SQLite）")
    except Exception as _reg3_err:
        logger.warning("[gui_shopping_login] 注册入库失败: %s", _reg3_err)
except Exception as _login_err:
    logger.warning("[gui_shopping_login] 宏工具导入失败: %s", _login_err)

# ===== [生态收敛] ask_ai 全局超时拦截（终极安全网）：单次请求最高 300s，卡死强制中断 =====
ASK_AI_GLOBAL_TIMEOUT = 300.0


# ===== [实战技能库] 工具报错 → skill_base 检索（AI 自我修复支撑） =====
_SKILL_DB = _ROOT + "/knowledge.db"


# ===== [Tool Boundary] AUP 明确业务错误码：命中 → 不查技能库排错指南，直接回传 AI =====
_AUP_BUSINESS_CODES = ("UNSUPPORTED_PLATFORM", "INVALID_ARGUMENT", "NEED_LOGIN", "EMPTY_PAGE", "LOGIN_INTERRUPTED",
                       "PARAM_ERROR", "AUTH_REQUIRED", "TOOL_VERSION_EXPIRED")


async def _auto_file_proposal(code, args, raw, tool_name=""):
    """[配置保护·兜底修改提案] 模型被拦后没有主动呈递时，由代码层代拟一份并送达。

    args / raw 必须来自**本次被拦的那一次工具调用**（tool_name + 该次参数 + 该条返回），
    不再用整批 _halt_sig，避免议案记到别的命令上。
    只写议案、不改任何配置；送达失败返回失败串，绝不谎称已送达。
    """
    try:
        from plugins.proposal_center import submit_system_proposal as _submit
    except Exception as exc:  # noqa: BLE001
        logger.warning("[配置保护] 议案通道不可用: %s", exc)
        return ""
    _type_map = {
        "UNAUTHORIZED_APP": "whitelist_add",
        "UNAUTHORIZED_STARTUP": "whitelist_add",
        "CONTACT_NOT_FOUND": "tool_gap",
        "MESSENGER_SHELL_FORBIDDEN": "tool_gap",
        "MESSENGER_ACTION_FORBIDDEN": "tool_gap",
        "UNAUTHORIZED_DOWNLOAD": "browser_policy",
        "UNAUTHORIZED_CLICK": "ui_path",
        "BLIND_CLICK": "ui_path",
        "BLIND_SEND_FORBIDDEN": "ui_path",
        "SETTINGS_WRITE_FORBIDDEN": "config_change",
    }
    ptype = _type_map.get(code, "other")
    call = args if isinstance(args, dict) else {}
    cmd = str(call.get("command") or "").strip()
    receiver = str(call.get("receiver") or "").strip()
    app = str(call.get("app_name") or "").strip()
    act = str(call.get("action") or "").strip()
    arg_brief = json.dumps(call, ensure_ascii=False)[:220] if call else "（无参数）"
    desc = ("AI 执行任务时被安全盾拦截，拦截码 %s。\n"
            "被拦工具：%s\n被拦参数：%s\n系统返回：%s\n"
            "本议案由系统在模型未自行呈递时自动起草（取自本次被拦的那一次调用），请求主人裁定是否放权。"
            % (code, tool_name or "未知工具", arg_brief,
               (raw or "").strip().replace("\n", " ")[:220]))
    patch = ["# 自动草案（仅呈递，不会被执行）", "# 拦截码：%s" % code,
             "# 被拦工具：%s" % (tool_name or "未知工具")]
    if cmd:
        patch.append("# 被拦命令：%s" % cmd[:300])
    if app:
        patch.append("# 被拦应用：%s%s" % (app, (" / action=%s" % act) if act else ""))
    if receiver:
        patch.append("# 收件人：%s" % receiver[:80])
    if ptype == "whitelist_add" and cmd:
        try:
            from plugins.windows_control import guards as _g
            targets = _g.startup_targets(cmd)
        except Exception:  # noqa: BLE001
            targets = []
        if targets:
            patch.append("startup_whitelist:")
            patch.append("  add:")
            patch.extend("    - %s" % t for t in targets)
    if ptype == "config_change":
        patch.append("config_change:")
        patch.append("  target: \"%s/config_center/settings.py\"" % _ROOT)
        patch.append("  mode: \"human_apply_only\"   # 主人已授权：AI 只可上奏，不得代改")
    if ptype == "tool_gap":
        patch.append("tool_gap:")
        patch.append("  need: \"可枚举联系人/会话的通讯端接入通道\"")
        patch.append("  why: \"当前通讯端 UI 树无法读取联系人，导致无法确认收件人\"")
    patch.append("# 建议：由主人评审后写入 config_center/wcu_policy.json 并重启生效")
    try:
        _res = await _submit(ptype, desc, "\n".join(patch))
        text = getattr(_res, "data", None) or str(_res)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[配置保护] 兜底修改提案发送失败 %s: %s", code, exc)
        return "（《系统修改提案》自动起草未送达：%s）" % str(exc)[:120]
    logger.info("[配置保护] 兜底修改提案已自动呈递：%s", text.replace("\n", " ")[:160])
    return text


def _wcu_halt_reply(code, raw, args=None):
    """[WCU 零信任硬闸] 拦截命中后的确定性收尾文案：不交给模型自由发挥。

    原则：只陈述"没做成"与下一步怎么办，绝不出现任何"已发送/已完成"的措辞。
    """
    import re as _re
    m = (_re.search("与「(.+?)」", raw or "")
         or _re.search(r"receiver[=:：]\s*\"?([^\",\s]+)", raw or "")
         or _re.search("「(.+?)」", raw or ""))
    target = m.group(1) if m else ""
    if not target and isinstance(args, dict):
        target = str(args.get("receiver") or args.get("app_name") or "").strip()
    if code == "CONTACT_NOT_FOUND":
        return (
            "未发送。\n\n"
            "QQ 的会话列表/联系人面板里没有找到与「%s」完全同名的联系人，系统已中止，"
            "没有点击任何发送按钮，消息也没有发出。\n\n"
            "请确认好友名是否正确；确认无误的话，先在 QQ 里手动打开一次该会话，再让我重发。"
            % (target or "该收件人")
        )
    if code == "UNAUTHORIZED_APP":
        return (
            "已拦截，没有执行。\n\n"
            "目标应用不在 WIN_ALLOWED_APPS 白名单内。AI 无权自行扩权：如确有需要，"
            "应由主人评审后在 config_center/wcu_policy.json 中手工加入并重启；"
            "本次未执行任何操作。"
        )
    if code == "UNAUTHORIZED_STARTUP":
        return (
            "已拦截，没有启动。\n\n"
            "命令行启动目标不在 allowed_apps 白名单内，且你本轮没有点名要求启动它。"
            "当前只允许启动 config_center/wcu_policy.json 里 allowed_apps 列出的应用。\n\n"
            "如确需启动，请通过 submit_system_proposal 呈递《系统修改提案》，"
            "由主人评审后写入该文件并重启生效。"
        )
    if code == "SETTINGS_WRITE_FORBIDDEN":
        return (
            "已拦截，没有执行。\n\n"
            "config_center/settings.py 与 wcu_policy.json 是人类的只读控制台（已 chattr +i 锁定），"
            "任何脚本、工具或代码改写都会被代码层阻断。\n\n"
            "如确需调整策略，请通过 submit_system_proposal 呈递《系统修改提案》，由主人评审落地。"
        )
    if code == "UNAUTHORIZED_DOWNLOAD":
        return "已拦截，没有执行。\n\n命令包含下载/安装动作，但你本轮没有明确要求下载或安装。需要的话请明确说要下载什么。"
    if code in ("UNAUTHORIZED_CLICK", "BLIND_CLICK", "BLIND_SEND_FORBIDDEN"):
        return "已拦截，没有执行。\n\n该点击属于高风险或未指名控件，系统禁止盲点。请先说明要点哪个控件、做什么操作。"
    if code in ("MESSENGER_ACTION_FORBIDDEN", "MESSENGER_SHELL_FORBIDDEN"):
        return (
            "已拦截，没有执行。\n\n通讯软件只允许受控发送通道 send_message(receiver, message)，"
            "不允许裸点击、裸注入或用命令行绕行，以免点错人。\n\n"
            "如需发送，请告诉我要发给谁、发什么内容。"
        )
    return "操作被零信任策略拦截，没有执行。"


def _is_aup_business_error(text: str) -> bool:
    """判定文本是否携带 AUP 明确业务错误码（如 error_code=UNSUPPORTED_PLATFORM）。

    这类错误是"参数/平台/凭证"等业务问题，查排错手册没有意义：直接把原因回传 AI。
    """
    if not text:
        return False
    return any(("error_code=%s" % _c) in text for _c in _AUP_BUSINESS_CODES)


def _query_skill_base(error_text: str, limit: int = 1) -> list:
    """用报错信息检索 skill_base 排错指南。
    打分：报错文本命中 SOP 的 keywords/title/applicable_condition 关键词即得分，按分排序取 top。
    返回 [{"title","content","applicable_condition","tool_name"}, ...]；异常或未命中返回 []。"""
    try:
        import sqlite3
        if not error_text:
            return []
        _conn = sqlite3.connect(_SKILL_DB, timeout=5)
        _rows = _conn.execute(
            "SELECT id,title,category,keywords,content,applicable_condition,tool_name,verified FROM skill_base"
        ).fetchall()
        _conn.close()
        _et = error_text.lower()
        _hits = []
        for _r in _rows:
            _hay = " ".join(str(x) for x in (_r[1], _r[3], _r[4], _r[5], _r[6]) if x)
            _hay_low = _hay.lower()
            _score = 0
            for _k in re.split(r"[,\s，、]+", str(_r[3] or "")):
                _k = _k.strip().lower()
                if _k and _k in _et:
                    _score += 3
            for _k in (_r[1] or "", _r[5] or ""):
                if _k and _k.lower() in _et:
                    _score += 2
            if _score > 0:
                _hits.append({
                    "score": _score,
                    "title": _r[1],
                    "content": _r[4],
                    "applicable_condition": _r[5],
                    "tool_name": _r[6],
                })
        _hits.sort(key=lambda x: -x["score"])
        return _hits[:limit]
    except Exception as _skill_err:
        logger.warning("[技能库] 检索异常: %s", _skill_err)
        return []


# ===== [Cost Termination] 排错指南去重集合（按 session+title，同一条指南只注入一次）=====
_injected_guides: dict = {}

# ===== [Cost Termination] L1 物理短路：高置信度 auth 规则 → 代码层直接构建 tool_calls =====
_L1_SITES = {
    "拼多多": ("https://mobile.yangkeduo.com/", "pdd"),
    "京东": ("https://www.jd.com/", "jd"),
    "淘宝": ("https://www.taobao.com/", "taobao"),
    "美团": ("https://www.meituan.com/", "meituan"),
    "闲鱼": ("https://www.goofish.com/", "xianyu"),
}
_L1_AUTH_RE = re.compile(
    r"(登录|没存凭证|没有存凭证|存凭证|未登录|凭证捕获|抓取|爬取|访问|打开)"
    r".{0,8}(拼多多|京东|淘宝|美团|闲鱼)"
    r"|(拼多多|京东|淘宝|美团|闲鱼).{0,8}(登录|凭证|抓取|爬取|访问|打开)",
    re.I,
)


def _l1_verdict_text(res_text) -> str:
    """[Anti-Hallucination] L1 短路结果代码层直判：按 AUP 契约 status/logged_in/error_code 输出。

    有明确状态返回的工具严禁交给 LLM 总结——status/logged_in/error_code 全部由代码判定，
    彻底切断"一句话总结"环节的幻觉空间。顺序：先失败类，再成功类，再通用失败。
    """
    _t = str(res_text or "")
    # 业务失败（需用户动作）→ 固定如实回显
    if "error_code=NEED_LOGIN" in _t or "需扫码登录" in _t or "need_login" in _t:
        return "登录窗口已弹出（或需要重新扫码），请完成扫码。"
    if "error_code=LOGIN_INTERRUPTED" in _t or "登录中断" in _t:
        return "登录中断：浏览器窗口在完成登录前被关闭，凭证未确认、未保存。请重新发起登录。"
    if "error_code=EMPTY_PAGE" in _t or "页面内容为空" in _t:
        return "页面内容为空，可能被反爬或未登录，本次抓取未成功。"
    if "error_code=AUTH_REQUIRED" in _t or "AUTH_REQUIRED" in _t:
        return "需要登录授权后才能执行该操作，请先完成扫码登录。"
    # 成功（凭证可用 / 登录态已确认）→ 固定回显
    if "logged_in: true" in _t or "logged_in: True" in _t or "已检测到登录态" in _t or "凭证已捕获并保存" in _t:
        return "凭证已捕获并保存，后续可静默抓取。"
    # 通用失败 / 异常 / 超时 → 回显错误信息（绝不编造成功）
    if "error_code=" in _t or "执行失败" in _t or "❌" in _t or "失败" in _t or "异常" in _t or "超时" in _t or "下线" in _t:
        return "工具执行失败：" + _t[:200]
    if _t.strip():
        return _t[:300]
    return "工具执行无输出，请重试。"


def _l1_auth_shortcut(prompt: str):
    """高置信度 auth 意图 → 直接构造 auth_playwright_fetch 的 tool_calls 结构。

    命中返回 ("auth_playwright_fetch", {"url":..., "profile_name":...})，否则 None。
    规则要求站点名与登录/凭证/抓取类动作词同时出现，避免闲聊误伤。
    """
    if not _L1_AUTH_RE.search(prompt or ""):
        return None
    for site, (url, prof) in _L1_SITES.items():
        if site in (prompt or ""):
            # [Auth Precision] 存凭证/重新登录意图 → 强制 login 模式（Windows 桌面弹窗扫码）。
            # 避免 fetch 无头判定把"匿名首页 cookies>2 + 无登录诱导"误判为已登录（假阳性 saved）。
            _p = prompt or ""
            _save = ("存" in _p and "凭证" in _p) or ("重新登录" in _p) or ("扫码登录" in _p) or ("重新扫码" in _p)
            _mode = "login" if _save else "fetch"
            return ("auth_playwright_fetch", {"url": url, "profile_name": prof, "mode": _mode})
    return None


def _inject_skill_guide(loop_msgs: list, guide: dict, key: str) -> bool:
    """[去重注入] 排错指南按 session+title 去重：已注入过 → 跳过追加（防上下文爆炸）。"""
    gkey = "%s::%s" % (key, guide["title"])
    if gkey in _injected_guides:
        logger.info("[技能库] 排错指南已注入过，跳过重复注入: %s", guide["title"])
        return False
    loop_msgs.append({
        "role": "system",
        "content": (
            f"【排错指南·{guide['title']}】上一个工具执行报错，请严格按以下已核验 SOP 自我修复"
            f"（纠正参数/换工具/按步骤处理），然后重试或向用户说明修复方案：\n{guide['content']}"
        ),
    })
    _injected_guides[gkey] = True
    logger.info("[技能库] 已注入排错指南 system 消息（首次）: %s", guide["title"])
    return True


async def ask_ai(user_id: str, prompt: str, chat_type: str = "private", group_id: str = "", model: str | None = None, forced_strategy: str | None = None) -> str:
    # R18：model 是唯一入口（前端选中的真实模型名）；forced_strategy 只为旧调用点保留，语义等同 model。
    # [生态收敛] 神授模式豁免全局超时：divine_task 内部 MAX_TIME=3600 已兜底，不在此截断
    timeout = ASK_AI_GLOBAL_TIMEOUT
    if "君权神授" in (prompt or ""):
        timeout = 3600.0

    def _persist_failure(status_text: str) -> None:
        # 异常/超时兜底：必须把本轮用户原话 + 失败状态强制写回 MemoryCenter，
        # 禁止裸 return 导致下一轮追问“好了吗”时模型失忆。
        try:
            k = get_conv_key(str(user_id), chat_type, group_id)
            hist = MemoryCenter.get_context(k)
            last = hist[-1] if hist else {}
            if last.get("role") != "user" or str(last.get("content")) != str(prompt):
                MemoryCenter.append_message(k, "user", prompt)
            MemoryCenter.append_message(k, "assistant", status_text)
            try:
                MemoryCenter.flush_episodic(k)
            except Exception:
                pass
        except Exception as _pe:
            logger.warning("[ask_ai] 失败回写历史异常: %s", _pe)

    async def _run() -> str:
        return await _ask_ai_impl(user_id, prompt, chat_type, group_id, model=model, forced_strategy=forced_strategy)

    try:
        return await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.error("[ask_ai] 全局超时 %.0fs，强制中断返回", timeout)
        _timeout_msg = f"[系统提示] 本轮任务超时中断（{int(timeout)}s 未完成），任务尚未结束；可追问进度或要求重试。"
        _persist_failure(_timeout_msg)
        return "任务超时中断"
    except Exception as e:
        logger.error("[ask_ai] 内部异常: %s", e, exc_info=True)
        _fail_msg = f"[系统提示] 本轮执行异常：{str(e)[:200]}；任务未完成。"
        _persist_failure(_fail_msg)
        return f"执行异常: {str(e)[:200]}"


async def _ask_ai_impl(user_id: str, prompt: str, chat_type: str = "private", group_id: str = "", model: str | None = None, forced_strategy: str | None = None) -> str:
    # [WCU] 注入本轮用户原话：零信任校验器据此判断「用户是否明确要求过」
    try:
        from plugins.windows_control import wcu_context as _wcu_ctx
        _wcu_ctx.set_utterance(prompt)
    except Exception:
        pass
    build_tool_schema = get_service("admin.build_tool_schema")
    TOOLS = get_service("admin.TOOLS")
    execute_tool_directly = get_service("admin.execute_tool_directly")
    user_id = str(user_id)
    _exp_trace_id = None  # [Feedback Loop] 本轮命中经验的 trace_id
    key = get_conv_key(user_id, chat_type, group_id)
    # ===== [Feedback Loop] 用户纠正检测：上轮命中经验 + 本轮否定词 → 上条经验 fail+1 =====
    _NEG_WORDS = ("不对", "错了", "不是", "重来", "重新", "搞错", "错误", "不行")
    if key in _last_exp_hit and any(w in prompt for w in _NEG_WORDS):
        _prev_trace = _last_exp_hit.pop(key)
        try:
            from experience_bank import record_feedback
            record_feedback(_prev_trace, "fail")
            logger.info("[反馈闭环] 用户纠正，轨迹 %s fail_count+1（否定词命中）", _prev_trace)
        except Exception as e:
            logger.warning("[反馈闭环] 用户纠正埋点失败: %s", e)
    # 确保对话历史键存在
    if key not in MemoryCenter.get_storage():
        MemoryCenter.reset(key)
    _art_burn = False  # [阅后即焚] 本轮是否执行了画图/重型任务工具（主循环路径标记）
    _round_failed = False  # [回写闸门] 本轮工具层是否出现业务失败（禁止对失败轮次回写 success）
    _failed_tool_notes = []  # [记忆兜底] 本轮失败工具的真实报错，强制回写历史，防止“失败即失忆”

    def _flush_failed_notes() -> None:
        # 把本轮工具失败/异常/超时的真实原因以 system 消息落回 MemoryCenter，
        # 确保下一轮追问“好了吗”时模型能据此说明任务未完成及原因。
        if not _failed_tool_notes:
            return
        try:
            MemoryCenter.append_message(
                key, "system",
                "【工具失败记录】本轮以下工具调用失败或返回错误，后续被追问进度时，"
                "必须如实说明任务尚未完成及具体原因，禁止谎称已完成：\n- "
                + "\n- ".join(_failed_tool_notes[-10:]),
            )
            _failed_tool_notes.clear()
            MemoryCenter.flush_episodic(key)
        except Exception as _ne:
            logger.warning("[记忆兜底] 失败记录回写异常: %s", _ne)

    # ===== [Cost Termination] L1 物理短路：命中高置信度 auth 规则 → 跳过意图判断与主对话 =====
    try:
        _sc = _l1_auth_shortcut(prompt)
        if _sc:
            _sc_tool, _sc_args = _sc
            logger.info("L1 短路直接构建 tool_calls: %s", json.dumps({"tool": _sc_tool, "args": _sc_args}, ensure_ascii=False))
            try:
                _sc_res = await execute_tool_directly(_sc_tool, _sc_args, user_id)
            except Exception as _sc_e:
                _sc_res = "工具执行异常: %s" % str(_sc_e)[:200]
            # [Anti-Hallucination] L1 短路去 LLM 总结：代码层按 AUP 契约直判结果。
            # 有明确状态返回的工具严禁交给模型自由发挥——status/logged_in/error_code 全部由代码判定，
            # 彻底切断"一句话总结"环节的编造空间。
            _final = _l1_verdict_text(_sc_res)
            logger.info("[L1短路] 完成，代码层直判结果（已切断 LLM 总结幻觉空间，0 次总结 API）")
            # [失忆断层修复] L1 短路也必须完整写入全局上下文，确保下一轮无缝衔接
            MemoryCenter.append_message(key, "user", prompt)
            MemoryCenter.append_message(key, "assistant", _final)
            MemoryCenter.flush_episodic(key)
            return sanitize_output(_final)
    except Exception as _l1_e:
        logger.warning("[L1短路] 规则层异常，回退常规链路: %s", _l1_e)

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
            _kb_reply = f"未找到与「{keyword}」相关的历史记录"
            MemoryCenter.append_message(key, "user", prompt)
            MemoryCenter.append_message(key, "assistant", _kb_reply)
            return _kb_reply
        lines = [f"找到 {len(results)} 条相关历史记录："]
        for r in results:
            lines.append(f"- 问题：{r['problem']}")
            lines.append(f"  方案：{r['solution'][:200]}...")
            lines.append(f"  时间：{r['created_at']}")
        _kb_reply = "\n".join(lines)
        MemoryCenter.append_message(key, "user", prompt)
        MemoryCenter.append_message(key, "assistant", _kb_reply)
        return _kb_reply
    if prompt.startswith("记住") or prompt.startswith("记住："):
        memory_text = prompt[2:].strip()
        if memory_text:
            if memory_text.startswith("："):
                memory_text = memory_text[1:].strip()
            if memory_text:
                result = memory.add_memory(memory_text)
                MemoryCenter.append_message(key, "user", prompt)
                MemoryCenter.append_message(key, "assistant", result)
                return result
        _mem_reply = "请告诉我要记住什么内容"
        MemoryCenter.append_message(key, "user", prompt)
        MemoryCenter.append_message(key, "assistant", _mem_reply)
        return _mem_reply

    # ===== R18：本轮唯一模型 = 人类在前端选的模型（没选才用 default_model） =====
    _explicit = model or forced_strategy
    chosen_model = str(_explicit).strip() if _explicit else DEFAULT_MODEL
    # 旧「回 tool/chat 澄清」机制已废弃，仅清掉残留状态，不再据此换模型
    _pending_clarify.pop(user_id, None)

    ocr_text, is_new_image = consume_image_context(key)
    logger.debug(f"ask_ai: key={key}, ocr_text={ocr_text[:50] if ocr_text else '空'}, is_new_image={is_new_image}")
    if is_new_image:
        prompt = f"关于我刚发的这张新图片：{prompt}"

    # ----- 前置处理（历史、压缩、记忆）统一走 MemoryCenter -----
    history = MemoryCenter.get_context(key, MAX_HISTORY * 2)
    if MemoryCenter.should_compress(key):
        await MemoryCenter.compress(key, call_llm=lambda _p: _call_ai_no_compress(key, _p))
        history = MemoryCenter.get_context(key, MAX_HISTORY * 2)
    if len(history) > MAX_HISTORY:
        MemoryCenter.trim(key, MAX_HISTORY)
        history = MemoryCenter.get_context(key)

    # ===== 人格注入（P0~P4 + P2 档案 + P3 图片上下文）唯一入口：MemoryCenter.build_initial_messages =====
    messages = MemoryCenter.build_initial_messages(PROFILE_CONTENT, ocr_text)
    # ===== 历史 + 当前消息 =====
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    # ===== [Chronos] 当前时间注入（降本增效：禁止 AI 查时间） =====
    try:
        from datetime import datetime
        _now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        messages.append({
            "role": "system",
            "content": (
                "【当前系统时间】现在是 %s（UTC+8）。"
                "你已自动获得当前时间，无需为查询时间调用任何工具。"
                "涉及 今天/明天/半小时后/几点 等相对时间表达，请基于本时间推导绝对时间。"
            ) % _now_str,
        })
    except Exception:
        pass

    # ===== [HostEnv] 本地宿主环境情报注入（意图命中：系统操作/打开软件/运行命令） =====
    try:
        _env_trigger = ("系统操作", "打开软件", "运行命令", "用 Windows", "浏览器", "宿主", "打开", "启动", "运行", "执行命令", "命令行", "开一下", "帮我开", "edge", "chrome", "python", "node")
        _pl = (prompt or "").lower()
        if any(_k in _pl for _k in _env_trigger):
            _env_block = format_env_snapshot()
            if _env_block:
                _env_block += "\n\n【宿主使用硬约束】当你需要启动浏览器、截图或调用 Windows 程序时，请优先阅读并使用上述路径，直接调用 Windows 宿主程序（尤其 msedge.exe）；绝对不要在 WSL 内寻找或安装 playwright/chrome/google-chrome。"
                # [截图 SOP] 任务涉及网页截图时，直接附加 skill_base 高优 SOP 原文（防止 AI 自行变形命令）
                if any(_k in _pl for _k in ("截图", "截个图", "截屏", "截一下", "抓图", "快照", "网页", "网页快照", "screen", "screenshot")):
                    try:
                        _shot_guides = _query_skill_base("网页截图 浏览器 msedge 快照", limit=1)
                        if _shot_guides and _shot_guides[0].get("content"):
                            _env_block += "\n\n【截图任务标准做法·务必逐字执行】\n" + _shot_guides[0]["content"]
                            logger.info("[HostEnv] 已附加截图 SOP（%s）", _shot_guides[0].get("title"))
                    except Exception as _sop_err:
                        logger.warning("[HostEnv] 截图 SOP 注入异常: %s", _sop_err)
                messages.append({"role": "system", "content": _env_block})
                logger.info("[HostEnv] 已注入本地宿主环境情报（意图命中）")
    except Exception as _env_err:
        logger.warning("[HostEnv] 注入异常: %s", _env_err)

    # ===== 长期记忆注入（P3 级别，插到 history 之前） =====
    matched_memories = MemoryCenter.recall_long_term(prompt)
    if matched_memories:
        memory_text = "\n".join([f"- {m}" for m in matched_memories])
        memory_msg = {
            "role": "system",
            "content": f"【P3·长期记忆】用户之前提到过以下信息，请参考：\n{memory_text}"
        }
        # history 的起始位置 = 末尾 user prompt 之前，回退 len(history) 条
        insert_pos = len(messages) - 1 - len(history)
        if insert_pos < 0:
            insert_pos = 0
        messages.insert(insert_pos, memory_msg)

    # ========== 模式判断（Pi / API）==========
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

    tools_schema = []
    for name, info in TOOLS.items():
        if not _tool_role_allowed(name, info, user_id):
            continue
        func = info["func"]
        params_desc = info.get("params_desc", {})
        schema = build_tool_schema(name, func, params_desc)
        tools_schema.append(schema)

    try:
        # ===== R18：模型直达。本轮只有一个决定性输入：人类选的模型 =====
        _model_cfg = get_strategy_config(chosen_model) or {}
        strategy = chosen_model
        _kind = str(_model_cfg.get("kind") or "").strip().lower()
        _tools_on = bool(_model_cfg.get("supports_tools"))
        logger.info(
            "[ask_ai] 模型=%s（kind=%s，工具=%s）— 不做意图判断、不做策略分类",
            strategy, _kind or "openai", "on" if _tools_on else "off",
        )
        if _kind == "c-end-web":
            # C 端网页免费通道：它本身就是一个模型通道
            reply = await deepseek_web_integration.ask_web_deepseek(prompt, user_id=user_id)
            tool_calls = None
        elif _tools_on:
            # ===== [Distillation] 经验库检索：抄学霸作业 =====
            try:
                from experience_bank import match_experience, record_feedback
                golden, matched_kw, _exp_trace_id, _exp_score = match_experience(prompt)
                if golden:
                    logger.info("[经验库] 匹配到了历史经验: %s (score=%.3f)", "、".join(matched_kw[:6]), _exp_score)
                    record_feedback(_exp_trace_id, "adopt")  # [Feedback Loop] 采纳计数
                    _last_exp_hit[key] = _exp_trace_id  # [Feedback Loop] 记录本轮命中，供下轮纠正检测
                    messages.append({
                        "role": "system",
                        "content": "参考以下优秀历史执行记录来解决问题：\n" + golden,
                    })
                else:
                    logger.debug("[经验库] 无高匹配经验（hits<2），走默认逻辑")
            except Exception as e:
                logger.warning("[经验库] 检索失败: %s", e)
            # ===== 工具循环：tool 意图进本地工具调用 =====
            # [生态收敛] web 意图已并入 tool：模型按工具描述选择 web_search / search_and_read 等联网工具，
            # 本地工具返回结果更可控；失败/反爬时可调用 ask_cend_ai 兜底
            if (USE_CEND_FOR_TOOL_PROXY
                    and deepseek_web_integration is not None
                    and deepseek_web_integration.is_enabled()):
                # 显式开关（旧配置兼容）：强制走 C 端代理
                logger.info("[C端代理] 工具策略改走 C 端代理（显式开关=%s）", USE_CEND_FOR_TOOL_PROXY)
                reply = await _cend_tool_proxy(prompt, user_id, tools_schema)
                tool_calls = None
            else:
                try:
                    reply, tool_calls = await safe_api_request(
                        messages, model=strategy, tools=tools_schema, tool_choice="auto"
                    )
                    # [熔断检测] safe_api_request 失败返回错误串（此前缀）；主 API 正常时绝不触碰 C 端代理
                    if _is_main_api_failure(reply):
                        raise RuntimeError(reply[:200])
                except Exception as _breach_err:
                    reply, tool_calls = await _circuit_breaker_to_cend(prompt, user_id, tools_schema, _breach_err)
            # ===== [Plugin 4.0+] 多轮工具循环：模型可基于工具结果继续决策 =====
            loop_msgs = list(messages)
            _pending_skill_guides = None  # [技能库自愈] 本轮工具报错命中的排错指南
            _proposal_filed = False  # [配置保护] 本轮是否已呈递修改提案（避免重复起草）
            _executed_chain = []  # [SOP 蒸馏] 本轮成功执行的工具链，跑通后自动沉淀
            for _round in range(TOOL_MAX_ROUNDS - 1):
                if not tool_calls:
                    break
                results = []
                for tc in tool_calls:
                    tool_name = tc["function"]["name"]
                    args = tc["function"].get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    logger.info("[工具调用] %s args=%s", tool_name, json.dumps(args, ensure_ascii=False)[:300])
                    # [阅后即焚] 画图/重型任务（代码/坐标数据可达几万字）：主循环命中即标记，
                    # 收尾时物理隔离本轮上下文，避免几万字代码污染下一次闲聊
                    if tool_name in ("render_python_code", "draw_math_functions", "draw_geometric_shapes"):
                        _art_burn = True
                        logger.info("[阅后即焚] 画图工具 %s 执行完毕，本轮上下文将在交付后物理隔离", tool_name)
                    try:
                        result = await execute_tool_directly(tool_name, args, user_id)
                    except Exception as e:
                        logger.error(f"执行工具 {tool_name} 失败: {e}", exc_info=True)
                        result = f"工具执行异常: {str(e)}"
                        _failed_tool_notes.append(f"{tool_name} 执行异常: {str(e)[:160]}")
                        # [技能库自愈] 报错 → skill_base 检索排错指南，待下一轮注入 system
                        # [Tool Boundary] AUP 明确业务错误 → 不查技能库，直接回传错误原因
                        if not _is_aup_business_error(str(e)):
                            try:
                                _skill_guides = _query_skill_base(str(e))
                                if _skill_guides:
                                    _pending_skill_guides = _skill_guides[0]
                                    logger.info("[技能库] 工具报错命中排错指南: %s", _skill_guides[0]["title"])
                            except Exception:
                                pass
                    # [回写闸门] 工具层业务失败信号检测：logged_in:false / error_code / 失败词 → 本轮禁止回写 success
                    _sig = str(result)
                    if any(_k in _sig for _k in ("logged_in: false", "error_code=NEED_LOGIN", "status=error",
                                                 "执行失败", "执行异常", "工具执行异常", "凭证操作未完成",
                                                 "已拒绝执行", "超时", "需扫码登录")):
                        _round_failed = True
                        _failed_tool_notes.append(f"{tool_name} 返回失败: {_sig[:160]}")
                    results.append({
                        "tool_call_id": tc.get("id", f"call_{_round}_{len(results)}"),
                        "output": str(result),
                    })
                    _executed_chain.append({"tool": tool_name, "args_brief": {k: str(v)[:80] for k, v in (args or {}).items()}, "note": ""})
                    # [技能库自愈] 工具返回错误串（非异常）也做检索，覆盖插件返回型错误（如 234011）
                    if _pending_skill_guides is None:
                        _rs = str(result)
                        if len(_rs) < 300 and any(_k in _rs for _k in ("失败", "错误", "异常", "超时", "error", "Error", "ERROR", "234011")) and not _is_aup_business_error(_rs):
                            try:
                                _skill_guides = _query_skill_base(_rs)
                                if _skill_guides:
                                    _pending_skill_guides = _skill_guides[0]
                                    logger.info("[技能库] 工具结果命中排错指南: %s", _skill_guides[0]["title"])
                            except Exception:
                                pass
                loop_msgs.append({
                    "role": "assistant",
                    "content": reply,
                    "tool_calls": [
                        {
                            "id": tc.get("id", f"call_{_round}_{i}"),
                            "type": "function",
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": json.dumps(tc["function"].get("arguments", {}), ensure_ascii=False),
                            },
                        }
                        for i, tc in enumerate(tool_calls)
                    ],
                })
                for tr in results:
                    loop_msgs.append({
                        "role": "tool",
                        "tool_call_id": tr["tool_call_id"],
                        "content": tr["output"][:4000],
                    })
                # ===== [WCU 零信任硬闸] 被拦截的工具结果不得再换通道重试 =====
                # 场景：QQ/微信找不到联系人、应用不在白名单、下载/盲点被拦。
                # 这些拦截是"物理层已中止"的信号，必须由代码层直接收尾并如实告知，
                # 否则模型会改用 win_shell、搜索框或按键模拟绕行，甚至谎报"已发送"。
                _WCU_HALT_CODES = ("CONTACT_NOT_FOUND", "MESSENGER_SHELL_FORBIDDEN",
                                   "MESSENGER_ACTION_FORBIDDEN", "UNAUTHORIZED_APP",
                                   "UNAUTHORIZED_STARTUP",
                                   "UNAUTHORIZED_DOWNLOAD", "UNAUTHORIZED_CLICK",
                                   "BLIND_CLICK", "BLIND_SEND_FORBIDDEN",
                                   "SETTINGS_WRITE_FORBIDDEN")
                _halt_sig = "\n".join(str(tr.get("output", "")) for tr in results)
                _halt_code = next((c for c in _WCU_HALT_CODES if c in _halt_sig), "")
                if any(tc.get("function", {}).get("name") == "submit_system_proposal"
                       for tc in tool_calls):
                    _proposal_filed = True
                # [配置保护] 全量拦截 → 先给模型一次呈递修改提案的机会（拦截本身不解除）
                _BOUNCE_CODES = ("CONTACT_NOT_FOUND", "MESSENGER_SHELL_FORBIDDEN",
                                 "MESSENGER_ACTION_FORBIDDEN", "UNAUTHORIZED_APP",
                                 "UNAUTHORIZED_STARTUP", "UNAUTHORIZED_DOWNLOAD",
                                 "UNAUTHORIZED_CLICK", "BLIND_CLICK",
                                 "BLIND_SEND_FORBIDDEN", "SETTINGS_WRITE_FORBIDDEN")
                _TYPE_HINT = {
                    "UNAUTHORIZED_APP": "whitelist_add",
                    "UNAUTHORIZED_STARTUP": "whitelist_add",
                    "CONTACT_NOT_FOUND": "tool_gap",
                    "MESSENGER_SHELL_FORBIDDEN": "tool_gap",
                    "MESSENGER_ACTION_FORBIDDEN": "tool_gap",
                    "UNAUTHORIZED_DOWNLOAD": "browser_policy",
                    "UNAUTHORIZED_CLICK": "ui_path",
                    "BLIND_CLICK": "ui_path",
                    "BLIND_SEND_FORBIDDEN": "ui_path",
                    "SETTINGS_WRITE_FORBIDDEN": "config_change",
                }
                _bounced = False
                if _halt_code in _BOUNCE_CODES:
                    _proposal_ready = any(
                        (s.get("name") or s.get("function", {}).get("name") or "") == "submit_system_proposal"
                        for s in tools_schema
                    )
                    _hinted = any("【沙盒申请通道·修改提案】" in str(m.get("content") or "")
                                  for m in loop_msgs if m.get("role") == "system")
                    if _proposal_ready and not _hinted:
                        logger.info("[配置保护] %s 命中，转入提案通道（不解除拦截）", _halt_code)
                        loop_msgs.append({
                            "role": "system",
                            "content": (
                                "【沙盒申请通道·修改提案】你的动作被安全盾拦截（%s），该动作已被物理中止，"
                                "不要重试、不要改用 win_shell / 按键模拟 / 改名换路径绕行。\n"
                                "请立刻调用 submit_system_proposal，说明你要干什么、为什么被拦、请求什么样的授权："
                                "proposal_type 用 %s；description 写清目标、卡点与理由；"
                                "patch_content 给出建议的配置片段或改造方案。\n"
                                "提交后如实告知主人“已呈递《系统修改提案》，等待裁决”，不得声称已打开/已发送/已完成。"
                            ) % (_halt_code, _TYPE_HINT.get(_halt_code, "other")),
                        })
                        _bounced = True
                if _halt_code and not _bounced:
                    logger.info("[WCU] 零信任硬闸命中 %s，终止工具循环并由代码层收尾", _halt_code)
                    _halt_args, _halt_tool, _halt_raw = {}, "", ""
                    # [精度] 只认真正产出该拦截码的那一次调用（results 与 tool_calls 同序）
                    for _i, _tr in enumerate(results):
                        if _halt_code not in str(_tr.get("output", "")) or _i >= len(tool_calls):
                            continue
                        _halt_raw = str(_tr.get("output", ""))
                        _halt_tool = tool_calls[_i].get("function", {}).get("name", "")
                        _a = tool_calls[_i].get("function", {}).get("arguments", {})
                        if isinstance(_a, str):
                            try:
                                _a = json.loads(_a)
                            except json.JSONDecodeError:
                                _a = {}
                        _halt_args = _a if isinstance(_a, dict) else {}
                        break
                    if not _halt_args:
                        # 参数解析失败时退回合并（只影响收尾文案，不影响议案定位）
                        for _tc in tool_calls:
                            _a = _tc.get("function", {}).get("arguments", {})
                            if isinstance(_a, str):
                                try:
                                    _a = json.loads(_a)
                                except json.JSONDecodeError:
                                    _a = {}
                            if isinstance(_a, dict):
                                _halt_args.update(_a)
                    reply = _wcu_halt_reply(_halt_code, _halt_raw or _halt_sig, _halt_args)
                    # [配置保护·兜底修改提案] 模型被拦后没主动呈递时，代码层代拟并送达，诉求不会丢
                    if _halt_code in _BOUNCE_CODES and not _proposal_filed:
                        _auto_ok = await _auto_file_proposal(
                            _halt_code, _halt_args, _halt_raw or _halt_sig, _halt_tool)
                        if _auto_ok:
                            reply = _auto_ok + "\n\n" + reply
                            _proposal_filed = True
                    tool_calls = None
                    break
                # [技能库自愈] 命中排错指南 → 作为 system 消息注入，让 AI 自我修复
                if _pending_skill_guides:
                    _inject_skill_guide(loop_msgs, _pending_skill_guides, key)
                    _pending_skill_guides = None
                try:
                    reply, tool_calls = await safe_api_request(
                        loop_msgs, model=strategy, tools=tools_schema, tool_choice="auto"
                    )
                    if _is_main_api_failure(reply):
                        raise RuntimeError(reply[:200])
                except Exception as _breach_err:
                    # [熔断生效] 续轮主 API 异常 → C 端代理收尾，结束多轮循环
                    reply, tool_calls = await _circuit_breaker_to_cend(prompt, user_id, tools_schema, _breach_err)
                    break
            # ===== [Evidence-Based Delivery] 长文本投递守卫（防呆拦截） =====
            _called = [tc.get("function", {}).get("name", "") for tc in (tool_calls or [])]
            if reply and len(reply) > 1000 and "feishu_send_file" not in _called:
                logger.warning("[投递拦截] 最终回复 %d 字且未调用 feishu_send_file，注入强制提示重试", len(reply))
                loop_msgs.append({
                    "role": "system",
                    "content": "你的输出过长且未发送文件，请立刻将其写入本地文件并调用 feishu_send_file 发送附件，聊天框只允许回复“文件已发送，请查收”。",
                })
                try:
                    reply, tool_calls = await safe_api_request(
                        loop_msgs, model=strategy, tools=tools_schema, tool_choice="auto"
                    )
                    if _is_main_api_failure(reply):
                        raise RuntimeError(reply[:200])
                except Exception as _breach_err:
                    reply, tool_calls = await _circuit_breaker_to_cend(prompt, user_id, tools_schema, _breach_err)
                if tool_calls:
                    results = []
                    for tc in tool_calls:
                        tool_name = tc["function"]["name"]
                        args = tc["function"].get("arguments", {})
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {}
                        logger.info("[工具调用][投递守卫] %s args=%s", tool_name, json.dumps(args, ensure_ascii=False)[:300])
                        try:
                            result = await execute_tool_directly(tool_name, args, user_id)
                        except Exception as e:
                            logger.error(f"执行工具 {tool_name} 失败: {e}", exc_info=True)
                            result = f"工具执行异常: {str(e)}"
                            _failed_tool_notes.append(f"{tool_name} 执行异常: {str(e)[:160]}")
                            # [技能库自愈] 守卫轮：报错 → skill_base 检索排错指南
                            # [Tool Boundary] AUP 明确业务错误 → 不查技能库，直接回传错误原因
                            if not _is_aup_business_error(str(e)):
                                try:
                                    _skill_guides = _query_skill_base(str(e))
                                    if _skill_guides:
                                        _pending_skill_guides = _skill_guides[0]
                                        logger.info("[技能库] 投递守卫工具报错命中排错指南: %s", _skill_guides[0]["title"])
                                except Exception:
                                    pass
                        # [回写闸门·守卫轮] 守卫轮工具业务失败同样触发闸门，禁止 success 回写
                        _g_sig = str(result)
                        if any(_k in _g_sig for _k in ("logged_in: false", "error_code=NEED_LOGIN", "status=error",
                                                       "执行失败", "执行异常", "工具执行异常", "凭证操作未完成",
                                                       "已拒绝执行", "超时", "需扫码登录")):
                            _round_failed = True
                            _failed_tool_notes.append(f"{tool_name} 返回失败: {_g_sig[:160]}")
                        results.append({
                            "tool_call_id": tc.get("id", f"guard_{len(results)}"),
                            "output": str(result),
                        })
                        _executed_chain.append({"tool": tool_name, "args_brief": {k: str(v)[:80] for k, v in (args or {}).items()}, "note": ""})
                        # [技能库自愈] 守卫轮：工具返回错误串也做检索
                        if _pending_skill_guides is None:
                            _rs = str(result)
                            if len(_rs) < 300 and any(_k in _rs for _k in ("失败", "错误", "异常", "超时", "error", "Error", "ERROR", "234011")) and not _is_aup_business_error(_rs):
                                try:
                                    _skill_guides = _query_skill_base(_rs)
                                    if _skill_guides:
                                        _pending_skill_guides = _skill_guides[0]
                                        logger.info("[技能库] 守卫轮工具结果命中排错指南: %s", _skill_guides[0]["title"])
                                except Exception:
                                    pass
                    loop_msgs.append({
                        "role": "assistant",
                        "content": reply,
                        "tool_calls": [
                            {
                                "id": tc.get("id", f"guard_{i}"),
                                "type": "function",
                                "function": {
                                    "name": tc["function"]["name"],
                                    "arguments": json.dumps(tc["function"].get("arguments", {}), ensure_ascii=False),
                                },
                            }
                            for i, tc in enumerate(tool_calls)
                        ],
                    })
                    for tr in results:
                        loop_msgs.append({
                            "role": "tool",
                            "tool_call_id": tr["tool_call_id"],
                            "content": tr["output"][:4000],
                        })
                    # [技能库自愈] 守卫轮命中排错指南 → 注入 system，让 AI 自我修复
                    if _pending_skill_guides:
                        _inject_skill_guide(loop_msgs, _pending_skill_guides, key)
                        _pending_skill_guides = None
                    try:
                        reply, tool_calls = await safe_api_request(
                            loop_msgs, model=strategy, tools=tools_schema, tool_choice="auto"
                        )
                        if _is_main_api_failure(reply):
                            raise RuntimeError(reply[:200])
                    except Exception as _breach_err:
                        reply, tool_calls = await _circuit_breaker_to_cend(prompt, user_id, tools_schema, _breach_err)
                if _exp_trace_id and not _round_failed:
                    record_feedback(_exp_trace_id, "success")  # [Feedback Loop] 任务成功计数（工具层无业务失败才回写）
                if _executed_chain and not _round_failed:
                    try:
                        from experience_bank import record_auto_sop
                        record_auto_sop(prompt, _executed_chain)
                    except Exception:
                        pass
        else:
            # ===== 纯文本模型：一律用人类选中的这个模型，不换通道、不降级 =====
            reply, tool_calls = await safe_api_request(
                _sanitize_for_chat(messages), model=strategy, tools=None
            )
    except Exception as e:
        logger.error(f"ask_ai API 调用失败: {e}", exc_info=True)
        if _exp_trace_id:
            try:
                record_feedback(_exp_trace_id, "fail")  # [Feedback Loop] 任务失败计数
            except Exception:
                pass
        # [记忆兜底] 异常分支不得裸 return：把用户原话 + 真实报错强制写回历史
        try:
            _fail_msg = f"[系统提示] AI 请求失败：{str(e)[:200]}；本轮任务未完成。"
            _hist = MemoryCenter.get_context(key)
            _last = _hist[-1] if _hist else {}
            if _last.get("role") != "user" or str(_last.get("content")) != str(prompt):
                MemoryCenter.append_message(key, "user", prompt)
            _flush_failed_notes()
            MemoryCenter.append_message(key, "assistant", _fail_msg)
            MemoryCenter.flush_episodic(key)
        except Exception as _pe:
            logger.warning("[记忆兜底] 异常回写历史失败: %s", _pe)
        return sanitize_output(f"AI 请求失败: {str(e)}")

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
        history = MemoryCenter.get_context(key)
        MemoryCenter.append_message(key, "user", prompt)
        _tc = assistant_msg.get("tool_calls")
        MemoryCenter.append_message(key, "assistant", assistant_msg.get("content") or "",
                                    extra={"tool_calls": _tc} if _tc else None)

        tool_results = []
        _art_burn = False  # [阅后即焚] 本轮是否执行了画图/重型任务工具
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
                logger.info("[工具调用] %s args=%s", tool_name, json.dumps(args, ensure_ascii=False)[:300])
                result = await execute_tool_directly(tool_name, args, user_id)
            except Exception as e:
                logger.error(f"执行工具 {tool_name} 失败: {e}", exc_info=True)
                result = f"工具执行异常: {str(e)}"
                _failed_tool_notes.append(f"{tool_name} 执行异常: {str(e)[:160]}")
            # [阅后即焚] 画图/重型任务（代码/坐标数据可达几万字）：执行完毕即物理清空本轮上下文，
            # 避免污染下一次闲聊
            logger.info("BURN_PROBE tool=%s", tool_name)
            if tool_name in ("render_python_code", "draw_math_functions", "draw_geometric_shapes"):
                _art_burn = True
                logger.info("[阅后即焚] 画图工具 %s 执行完毕，本轮上下文将在交付后物理清空", tool_name)
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
        for _tm in tool_messages:
            MemoryCenter.append_message(key, "tool", _tm.get("content", ""), tool_call_id=_tm.get("tool_call_id"))
        _flush_failed_notes()

        # [阅后即焚] 画图/重型任务：本轮 user/assistant/tool 记录物理抹除（回退到本轮开始前），
        # 几万字的代码/坐标数据不进入后续任何闲聊上下文
        if _art_burn:
            MemoryCenter.rollback_to_last_user(key)
            logger.info("[阅后即焚] 画图工具本轮上下文已物理回退至上一个 user 锚点（MemoryCenter 统一收口）")

        # ===== 人格注入（P0~P4 + P2 档案）唯一入口：MemoryCenter.build_initial_messages =====
        final_messages = MemoryCenter.build_initial_messages(PROFILE_CONTENT)
        # ===== 历史（sanitize 后） =====
        final_messages.extend(_sanitize_for_chat(MemoryCenter.get_context(key)))
        # ===== [Anti-Hallucination] 事实优先死命令：最终总结必须严格依据工具真实结果 =====
        final_messages.append({"role": "system", "content": (
            "你必须严格依据 role=\"tool\" 返回的真实结果作答。如果工具返回了报错、空内容或 NEED_LOGIN，"
            "你必须如实告知用户“执行失败”或“需要扫码”，绝对禁止使用“已成功”“已完成”等幻觉词汇来安慰用户。"
            "宁可如实说“未执行成功/暂未获取到数据”，也绝不编造成功。"
        )})
        try:
            # ===== 最终总结：仍用人类选中的同一个模型（不偷偷换成别的模型） =====
            final_reply, _ = await safe_api_request(final_messages, model=strategy, tools=None)
        except Exception as e:
            logger.error(f"生成最终回复失败: {e}", exc_info=True)
            final_reply = f"生成总结失败: {str(e)}"

        if _art_burn:
            # [阅后即焚] 画图/重型任务（主循环路径）：本轮指令/工具/代码/坐标仅存在于本轮内存，
            # 收尾不写入历史，让下一轮闲聊保持干净（"刚才画了什么"应当失忆）
            logger.info("[阅后即焚] 画图工具本轮上下文已物理隔离（不写入历史），后续闲聊上下文保持干净")
        else:
            MemoryCenter.append_message(key, "assistant", final_reply)
            MemoryCenter.trim(key, MAX_HISTORY * 2)

        # ===== 情景记忆落盘（MemoryCenter 统一封装） =====
        MemoryCenter.flush_episodic(key)

        return await _finalize_with_guard(final_messages, final_reply, tool_calls, tool_results)

    # ===== 非工具路径：保存本轮对话到历史（MemoryCenter 唯一出入口） =====
    _flush_failed_notes()
    MemoryCenter.append_message(key, "user", prompt)
    MemoryCenter.append_message(key, "assistant", reply)
    MemoryCenter.trim(key, MAX_HISTORY * 2)

    # ===== 情景记忆落盘（MemoryCenter 统一封装） =====
    MemoryCenter.flush_episodic(key)

    # ===== [Output Sanitization] 物理清洗后返回 =====
    reply = await _finalize_with_guard(messages, reply)

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
    build_tool_schema = get_service("admin.build_tool_schema")
    TOOLS = get_service("admin.TOOLS")
    key = get_conv_key(user_id, chat_type, "web")

    history = MemoryCenter.get_context(key, MAX_HISTORY * 2)
    if MemoryCenter.should_compress(key):
        await MemoryCenter.compress(key, call_llm=lambda _p: _call_ai_no_compress(key, _p))
        history = MemoryCenter.get_context(key, MAX_HISTORY * 2)
    if len(history) > MAX_HISTORY:
        MemoryCenter.trim(key, MAX_HISTORY)
        history = MemoryCenter.get_context(key)

    messages = MemoryCenter.build_initial_messages(PROFILE_CONTENT)
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
        if not _tool_role_allowed(name, info, user_id):
            continue
        func = info["func"]
        params_desc = info.get("params_desc", {})
        schema = build_tool_schema(name, func, params_desc)
        tools_schema.append(schema)

    # ===== 修改点：web 端默认使用 tool 策略 =====
    reply, tool_calls = await safe_api_request(messages, model=strategy, tools=tools_schema, tool_choice="auto")

    assistant_msg = {"role": "assistant", "content": reply}
    if tool_calls:
        assistant_msg["tool_calls"] = [
            {"id": tc["id"], "type": "function", "function": tc["function"]}
            for tc in tool_calls
        ]
    MemoryCenter.append_message(key, "user", prompt)
    _tc = assistant_msg.get("tool_calls")
    MemoryCenter.append_message(key, "assistant", assistant_msg.get("content") or "",
                                extra={"tool_calls": _tc} if _tc else None)

    return {
        "content": reply,
        "tool_calls": tool_calls if tool_calls else []
    }

async def submit_tool_results(user_id: str, tool_results: list) -> str:
    """
    提交工具执行结果给 AI，获取最终总结。
    tool_results: [{"tool_call_id": "call_xxx", "output": "stdout 文本"}]
    """
    TOOLS = get_service("admin.TOOLS")
    key = get_conv_key(user_id, "web", "web")
    history = MemoryCenter.get_context(key)

    for result in tool_results:
        _content = (result["output"][:2000] + "\n... (输出过长，已截断)") if len(result["output"]) > 2000 else result["output"]
        MemoryCenter.append_message(key, "tool", _content, tool_call_id=result["tool_call_id"])

    history = MemoryCenter.get_context(key)

    messages = [{"role": "system", "content": MemoryCenter.build_system_prompt()}]
    messages.extend(history)

    reply, _ = await safe_api_request(messages, tools=None)

    MemoryCenter.append_message(key, "assistant", reply)

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
    execute_tool_directly = get_service("admin.execute_tool_directly")

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

# ==================== [Windows God Mode] 已迁入 plugins/windows_control（Plugin 6.0 / WCU） ====================
# win_shell / win_ui_automation 的实现、注册与零信任拦截全部收敛到 WCU 单元，
# 本文件不再保留任何 Windows 操控的硬编码副本（单一事实来源）。
# 入口：plugins/windows_control/__init__.py（工具注册）→ guards.py（四道闸门）。
