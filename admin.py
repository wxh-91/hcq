import os
import re
import asyncio, aiofiles
import shutil
import json
import time
import threading
import logging
logger = logging.getLogger(__name__)
try:  # 业务埋点（导入失败不影响主流程）
    from monitor import metrics as _qm
except Exception:
    _qm = None
from import_center import get_service
from cl import DEFAULT_ADMIN, is_main_admin
# 导入工具函数
from plugins.core_utils.downloader import download_m3u8
from plugins.system_ops.wj import backup_project, read_file, write_file, delete_line, replace_line, insert_line
from plugins.system_ops.xt import system_status, ping_host
from plugins.core_utils.aup_tools import aup_execute_shell, aup_run_bash_script
from aup_core import AUPError, AUPResult
from plugins.core_utils.admin_gh import add_admin, del_admin, list_admin
from plugins.core_utils.tool_schema_builder import ToolSchemaBuilder, StringSchema, IntegerSchema, BooleanSchema
from plugins.system_ops.script_runner import run_bash_script, _fix_missing_newlines
from plugins.core_utils.checkin import run_checkin
from memory_center import MemoryCenter   # 长期记忆统一出入口（episodic.log 不再直接操作）
from plugins.core_utils.media import clean_media
registry_manager = get_service("registry_manager")
knowledge_db = get_service("knowledge_db")

memory = get_service("memory")

async def set_group_chat_enabled(enabled: bool) -> str:
    """开启或关闭群聊功能（enabled: true/false）"""
    memory.set("group_chat_enabled", enabled)
    status = "开启" if enabled else "关闭"
    return f"群聊功能已{status}"

async def get_group_chat_status() -> str:
    """查询当前群聊功能状态"""
    status = memory.get("group_chat_enabled", False)
    return f"群聊功能当前为{'开启' if status else '关闭'}"

async def agent_manager(
    action: str,
    description: str = "",
    task_id: str = "",
    sender_id: str = ""
) -> str:
    get_conv_key = get_service("ai.get_conv_key")
    TodoListManager = get_service("ai.TodoListManager")
    """Todo List 管理器工具"""
    # 获取当前会话 ID（通过 sender_id 推断）
    # 注意：此处简化，实际使用时需从上下文中获取 session_id
    # 在 web 端，sender_id 即为 user_id
    session_id = get_conv_key(str(sender_id), "web", "web")  # 临时兼容
    todo_mgr = TodoListManager(session_id)

    if action == "list":
        return todo_mgr.list_items()

    if action == "add":
        if not description:
            return "请提供任务描述"
        item = todo_mgr.add(description)
        return f"已添加任务：{description} (ID: {item.id})"

    if action == "execute":
        if not task_id:
            pending = todo_mgr.get_pending()
            if not pending:
                return "没有待执行的任务"
            task_id = pending.id
        # 标记为运行中（实际执行由主循环驱动）
        if todo_mgr.mark_running(task_id):
            return f"任务 {task_id} 已标记为执行中，请继续使用其他工具完成"
        return f"任务 {task_id} 不存在或无法执行"

    if action == "status":
        return todo_mgr.list_items()

    return f"未知操作：{action}，支持的操作：list/add/execute/status"

async def get_xmkj() -> str:
    """读取项目框架文件（项目框架.txt），返回项目结构描述"""
    return await read_file("XMKJ.txt")

async def query_knowledge(keyword: str) -> str:
    """从知识库中检索与关键词匹配的历史解决方案"""
    results = knowledge_db.search_solutions(keyword, limit=5)
    if not results:
        return f"未找到与「{keyword}」相关的历史记录"
    lines = [f"找到 {len(results)} 条相关历史记录："]
    for r in results:
        lines.append(f"- 问题：{r['problem']}")
        lines.append(f"  方案：{r['solution'][:200]}...")
        lines.append(f"  时间：{r['created_at']}")
    return "\n".join(lines)

async def recall_conversation(keyword: str) -> str:
    """检索今天的历史对话内容（含闲聊、工具调用、工具结果）"""
    results = MemoryCenter.recall_long_term(keyword, limit=20)
    if not results:
        return f"今天没有找到与「{keyword}」相关的对话记录"
    lines = [f"找到 {len(results)} 条相关记录："]
    for r in results:
        content = r.get("content", "")
        if len(content) > 200:
            content = content[:200] + "..."
        lines.append(f"[{r.get('ts', '')}] {r.get('role', '')}: {content}")
    return "\n".join(lines)

async def clean_media_tool(scope: str = "both") -> str:
    """清理 mp4/mp3 文件。scope 可选：downloads、robot、both"""
    return clean_media(scope)

async def checkin_tool() -> str:
    """签到工具（受 CHECKIN_ENABLED 开关控制）"""
    from cl import CHECKIN_ENABLED
    if not CHECKIN_ENABLED:
        return "签到功能已停用"
    return await run_checkin()
# 工具注册表（AI 可调用）：数据源为注册网关（SQLite 元数据），此处仅为 dict 形态兼容视图
TOOLS = registry_manager.build_compat_view()

# ========== 快速硬指令（不走 AI，直接执行） ==========
FAST_COMMANDS = {
    "备份项目": backup_project,
    "系统状态": system_status,
    "管理员列表": list_admin,
}

def get_help_text() -> str:
    """动态生成帮助文本，自动同步工具列表"""
    lines = []
    lines.append("**可用指令列表**（所有指令需以 `@` 开头）")
    lines.append("")
    lines.append("**【快速硬指令】**（直接执行，不调用 AI）：")
    for cmd in FAST_COMMANDS:
        desc = {
            "备份项目": "打包当前项目代码（生成 zip 到 /root/）",
            "系统状态": "查看 CPU、内存、磁盘使用率",
            "管理员列表": "列出当前所有管理员 QQ 号",
        }.get(cmd, "")
        lines.append(f"- `{cmd}`：{desc}")

    lines.append("")
    lines.append("**【AI 智能工具】**（发送后由 AI 自动识别并调用）：")
    # 动态生成工具描述
    for tool_name, info in TOOLS.items():
        func = info["func"]
        # 从函数文档字符串获取描述
        doc = func.__doc__
        if doc:
            # 取第一行作为描述
            desc = doc.strip().split('\n')[0]
        else:
            # 无文档时提供默认描述
            desc = _get_tool_default_desc(tool_name)
        lines.append(f"- `{tool_name}`：{desc}")
    
    lines.append("")
    lines.append("**【通用 AI 对话】**")
    lines.append("- `问 <问题>`：直接向 AI 提问，不调用工具，适合闲聊或复杂分析。")
    lines.append("")
    lines.append("**提示**：对于大多数任务，直接描述你要做的事即可（如“读文件 cl.py”、“执行命令 ls”），AI 会自动选择合适的工具。")
    return "\n".join(lines)

def build_tool_schema(tool_name: str, func, params_desc: dict) -> dict:
    """用 tool-schema-builder 构建单个工具的 OpenAI Schema"""
    import inspect
    sig = inspect.signature(func)
    builder = ToolSchemaBuilder()
    
    for p_name, p in sig.parameters.items():
        if p_name == "sender_id":
            continue
        if p.annotation == int:
            schema = IntegerSchema(description=params_desc.get(p_name, f"参数 {p_name}"))
        elif p.annotation == bool:
            schema = BooleanSchema(description=params_desc.get(p_name, f"参数 {p_name}"))
        else:
            schema = StringSchema(description=params_desc.get(p_name, f"参数 {p_name}"))
        builder.set_parameter(p_name, schema)
        if p.default == inspect.Parameter.empty:
            builder.set_required(p_name)
    
    return (
        builder.build_parameters()
        .build_function(tool_name)
        .build_tool()
        .to_schema()
    )

def _get_tool_default_desc(tool_name: str) -> str:
    """为没有文档字符串的工具提供默认描述"""
    default_descs = {
        "read_file": "读取指定文件内容（路径需在项目根目录下）",
        "write_file": "覆盖写入文件内容（危险操作，请谨慎）",
        "replace_line": "替换文件指定行的内容（行号从1开始）",
        "delete_line": "删除文件指定行",
        "insert_line": "在指定行前插入新行",
        "execute_shell": "执行任意 Shell 命令（高危，仅限主管理员）",
        "add_admin": "添加新管理员（参数：QQ号）",
        "del_admin": "删除管理员（参数：QQ号）",
        "set_group_chat_enabled": "开启或关闭群聊功能",
        "get_group_chat_status": "查询当前群聊功能状态",
        "download_m3u8": "从m3u8链接下载视频，支持加密流自动解密合并；长清单转入后台任务返回 task_id。参数：url(必填)、referer(可选)、output_name(可选)",
        "download_task_status": "查询后台 m3u8 下载任务的进度、结果路径或失败原因。参数：task_id(必填)",
        "run_bash_script": "执行AI动态生成的多行Bash脚本（高危，支持超时控制）",
    }
    return default_descs.get(tool_name, "无描述")

async def _call_tool_router(user_input: str, sender_id: str) -> dict | None:
    """使用 OpenAI function calling 进行工具路由"""
    # [fix] 动态刷新注册视图：兼容运行期新注册入库的工具（注册在 ai.py 导入时，晚于 admin 模块加载）
    tools_view = registry_manager.build_compat_view()
    tools = []
    for name, info in tools_view.items():
        func = info["func"]
        params_desc = info.get("params_desc", {})
        schema = build_tool_schema(name, func, params_desc)
        tools.append(schema)
    
    messages = [
        {"role": "system", "content": "你是一个工具调用助手，根据用户指令选择合适的工具。如果不需要工具，则直接回复。"},
        {"role": "user", "content": user_input}
    ]
    
    safe_api_request = get_service("ai.safe_api_request")
    content, tool_calls = await safe_api_request(messages, tools=tools, tool_choice="auto")
    
    # 以下逻辑不变
    if tool_calls and isinstance(tool_calls, list) and len(tool_calls) > 0:
        first_call = tool_calls[0]
        func_info = first_call.get("function", {})
        tool_name = func_info.get("name")
        args_str = func_info.get("arguments", "{}")
        try:
            args = json.loads(args_str) if isinstance(args_str, str) else args_str
        except json.JSONDecodeError:
            args = {}
        if tool_name and tool_name in TOOLS:
            return {"tool": tool_name, "args": args}
        if tool_name:
            # [防呆] AI 调用了不存在的工具名：显式标记，让执行层报错并要求重试
            return {"tool": "__missing__", "args": {"name": tool_name}}
    return None

async def _execute_admin_command(cmd: str, sender_id: str, chat_type: str = "private", group_id: str = "") -> str | None:
    logger.debug(f"_execute_admin_command 收到 cmd: {cmd}")

    # ----- 特殊：帮助指令（无需权限） -----
    if cmd == "帮助" or cmd == "help":
        return get_help_text()

    if cmd in FAST_COMMANDS:
        if not is_main_admin(sender_id):
            return "权限不足"
        func = FAST_COMMANDS[cmd]
        if asyncio.iscoroutinefunction(func):
            result = await func()
        else:
            result = func()
        return result if isinstance(result, str) else str(result)

    # ----- 特殊：以“问 ”开头的直接走 AI 问答（不强制工具调用）-----
    if cmd.startswith("问 "):
        question = cmd[2:].strip()
        if not question:
            return "你想问什么？"
        ask_ai = get_service("ai.ask_ai")
        # 需要从外部传入 chat_type 和 group_id，因此需修改函数签名
        # 建议将 _execute_admin_command 增加参数 chat_type="private", group_id=""
        answer = await ask_ai(sender_id, question, chat_type, group_id)
        return answer
    # ----- 其余命令：走独立工具路由 -----
    route_result = await _call_tool_router(cmd, sender_id)
    logger.debug(f"route_result: {route_result}")
    if route_result is None:
        print("[DEBUG] route_result 为 None，返回 None")
        return None
    tool_name = route_result["tool"]
    args = route_result["args"]
    if tool_name == "__missing__":
        missing = args.get("name", "?")
        return f"AI 调用了不存在的工具「{missing}」，请重新生成正确的工具调用（先查看可用工具列表再选择）"
    logger.debug(f"准备执行工具: {tool_name}, 参数: {args}")

    # [AUP-集成] 统一执行边界：所有工具调用收敛到 execute_tool_directly
    # （AUP 协议渲染 / 参数过滤与类型转换 / 权限校验 / 审计日志 / 降级链）
    # 消除旧直连旁路：此前直接 func(**args) 会绕过 AUP 边界并对 AUPResult 崩溃
    return await execute_tool_directly(tool_name, args, sender_id)

async def handle_admin_command(raw_input, sender_id, force_tool_route=False, chat_type="private", group_id=""):
    logger.debug(f"handle_admin_command 被调用，raw_input={raw_input}")
    is_super_admin = is_main_admin(sender_id)
    is_normal_admin = memory.is_admin(sender_id) and not is_super_admin

    if not is_super_admin and not is_normal_admin:
        return None

    # 如果 force_tool_route 为 False 且消息不以 @ 开头（群聊的普通命令），则跳过
    if not force_tool_route and not raw_input.startswith("@"):
        return None

    if is_super_admin:
        cmd = raw_input.strip()
        if cmd.startswith("@"):
            cmd = cmd[1:].strip()
        return await _execute_admin_command(cmd, sender_id)

    if is_normal_admin:
        if not raw_input.startswith("@"):
            return None
        cmd = raw_input[1:].strip()
        return await _execute_admin_command(cmd, sender_id)

    return None


def _render_aup_result(res: "AUPResult") -> str:
    """AUP 协议渲染：解析 status/data/error_code/fallback_chain/suggestion -> 结构化字符串。

    这是新老体系合并的关键边界——AI 与硬指令拿到的是解析后的 AUP 语义，
    而非裸 dataclass repr 或旧版裸字符串。
    """
    if res.status == "ok":
        if isinstance(res.data, dict):
            core = "\n".join("%s: %s" % (k, v) for k, v in res.data.items())
        else:
            core = str(res.data)
        chain = ("\n[降级链路] " + " -> ".join(res.fallback_chain)) if res.fallback_chain else ""
        return "✅ 执行成功 (status=ok, error_code=%s)\n%s%s" % (res.error_code, core, chain)
    return "❌ 执行失败 (status=error, error_code=%s)\n%s%s" % (
        res.error_code,
        res.suggestion or str(res.data) or "无详细信息",
        ("\n[降级链路] " + " -> ".join(res.fallback_chain)) if res.fallback_chain else "",
    )


# ===== [生态收敛] 工具级心跳：任何工具执行 >60s 自动通知（不限神授模式） =====
TOOL_HEARTBEAT_INTERVAL = 60.0
_heartbeat_cb = None  # 由上层（dz.py）注册回调，发送飞书心跳


def set_heartbeat_callback(cb):
    """注册心跳回调（async callable，接收心跳消息文本）。"""
    global _heartbeat_cb
    _heartbeat_cb = cb


# ---- Debug Console：真实活跃工具队列 ----
_ACTIVE_TOOLS: list[dict] = []
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_SEQ = 0


def _active_begin(tool_name: str, sender: str = "") -> int:
    global _ACTIVE_SEQ
    _ACTIVE_SEQ += 1
    entry = {
        "id": _ACTIVE_SEQ,
        "tool": tool_name,
        "sender": str(sender or ""),
        "started_at": time.strftime("%H:%M:%S"),
        "start_ts": time.time(),
    }
    with _ACTIVE_LOCK:
        _ACTIVE_TOOLS.append(entry)
    return entry["id"]


def _active_end(entry_id: int) -> None:
    with _ACTIVE_LOCK:
        for i, item in enumerate(_ACTIVE_TOOLS):
            if item.get("id") == entry_id:
                _ACTIVE_TOOLS.pop(i)
                break


def debug_active_tools() -> list[dict]:
    now = time.time()
    with _ACTIVE_LOCK:
        return [{
            "tool": item.get("tool"),
            "sender": item.get("sender"),
            "started_at": item.get("started_at"),
            "elapsed_sec": round(now - float(item.get("start_ts") or now), 2),
        } for item in _ACTIVE_TOOLS]


async def _await_with_heartbeat(tool_name: str, coro):
    """等待协程完成；每 60s 未完成则触发一次心跳回调，直到完成。"""
    active_id = _active_begin(tool_name)
    try:
        task = asyncio.ensure_future(coro)
        while True:
            done, _ = await asyncio.wait({task}, timeout=TOOL_HEARTBEAT_INTERVAL)
            if task in done:
                return task.result()
            if _heartbeat_cb is not None:
                try:
                    await _heartbeat_cb(f"[心跳] 工具 {tool_name} 仍在执行中...")
                except Exception as _e:
                    logger.warning("[心跳] 发送失败: %s", _e)
    finally:
        _active_end(active_id)


def _normalize_tool_args(args):
    """[Parameter Normalization] LLM 多包一层 args/kwargs → 智能解包。

    模型调用工具时偶尔把真实参数包成 {"args": {...}} / {"kwargs": "{\"...\": ...}"}
    形态，导致 AUP 校验 required 缺失而 INVALID_ARGUMENT。规则：
    - 仅当 args 的所有键都落在 {args, kwargs} 内（纯包裹形态）才解包；
    - 内层值为 dict 直接提取；值为 JSON 字符串则解析后提取；
    - 解包结果若仍是纯包裹形态，递归再拍平一层，防止双重嵌套。
    正常调用（如 {"query": "..."}）不满足条件，原样返回。
    """
    if not isinstance(args, dict) or not args:
        return args
    keys = set(args.keys())
    if not (keys <= {"args", "kwargs"}) or not keys:
        return args
    inner = {}
    for wk in ("args", "kwargs"):
        if wk not in args:
            continue
        v = args[wk]
        if isinstance(v, dict):
            inner.update(v)
        elif isinstance(v, str):
            try:
                jv = json.loads(v)
                if isinstance(jv, dict):
                    inner.update(jv)
            except Exception:
                pass
    if not inner:
        return args
    iks = set(inner.keys())
    if iks and iks <= {"args", "kwargs"}:
        return _normalize_tool_args(inner)
    return inner


# ===== [Param Align] 语义同义参数强制对齐：模型写 script / script_content 也必须能调通 =====
_SYNONYM_GROUPS = [
    # 执行命令类
    ("cmd", {"command", "shell_command", "script", "script_content", "code", "cmdline",
             "shell", "bash", "program", "exec", "command_line", "cmd_str", "script_text"}),
    # 脚本内容类
    ("script_content", {"script", "code", "bash", "content", "body", "script_text", "cmd"}),
    # 路径类
    ("filepath", {"path", "file", "file_path", "filename", "file_name", "target",
                  "target_path", "dest", "dest_path", "save_path", "output", "out",
                  "directory", "dir"}),
    # 文本内容类
    ("content", {"text", "body", "data", "value", "new_content", "contents"}),
    # 链接类
    ("url", {"link", "href", "video_url", "page_url", "target_url", "uri"}),
    # 其他常见
    ("line_num", {"line", "line_number", "lineno"}),
    ("pattern", {"keyword", "query", "search", "regex", "needle"}),
    ("name", {"task_name", "title", "name_str"}),
]


def _norm_key(k: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


_SYN_LOOKUP = {}
for _canon, _alts in _SYNONYM_GROUPS:
    for _a in _alts:
        _SYN_LOOKUP.setdefault(_norm_key(_a), set()).add(_canon)


def _normalize_args(args: dict, valid_param_names: set) -> dict:
    """按签名强制对齐参数：同名/同义/大小写下划线差异全部归一到签名参数名，避免"语义相同却报缺参"。"""
    used, out = set(), {}
    norm_valid = {_norm_key(n): n for n in valid_param_names}
    for k, v in args.items():
        nk = _norm_key(k)
        target = norm_valid.get(nk)
        if target is None:
            for canon in _SYN_LOOKUP.get(nk, ()):
                if canon in valid_param_names and canon not in out and k not in valid_param_names:
                    target = canon
                    break
        if target is None:
            out[k] = v
        else:
            out.setdefault(target, v)
        used.add(k)
    # 仍未提供的关键参数：从剩余别名里再兜一次
    for canon, alts in _SYNONYM_GROUPS:
        if canon in valid_param_names and canon not in out:
            for a in sorted(alts):
                for k, v in args.items():
                    if _norm_key(k) == _norm_key(a):
                        out[canon] = v
                        break
                if canon in out:
                    break
    return out


# 全盘扫描类命令：会阻塞全局事件循环，底层直接拒绝
_BLOCKED_SHELL_PAT = re.compile(
    r"(?:\bfind\s+/(?:\s|$|\|)|"
    r"\bdu\s+-[a-zA-Z]*\s*/(?:\s|$|\|)|"
    r"\bls\s+-[a-zA-Z]*[Rr][a-zA-Z]*\s*/(?:\s|$|\|)|"
    r"\bgrep\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+[^\n|]*\s/(?:\s|$|\|)|"
    r"\blocate\s+|"
    r"\btar\s+-[a-zA-Z]*[cC][a-zA-Z]*[fF]\s+[^\n|]*\s/(?:\s|$|\|))"
)


def _pick_cmd(args: dict) -> str:
    for k in ("cmd", "command", "shell_command", "script", "script_content", "code", "cmdline", "shell"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


async def execute_tool_directly(
    tool_name: str, args: dict, sender_id: str
) -> str:
    # 先解析 JSON 字符串，避免后续 args[arg_key] 索引出错
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return "参数格式错误：无法解析 JSON 字符串"
    # [Parameter Normalization] LLM 多包一层 args/kwargs 时智能解包
    args = _normalize_tool_args(args)

    # [配置保护] settings.py / wcu_policy.json 只读死命令（代码层，先于一切工具执行）
    try:
        from plugins.proposal_center.guard import violation as _ro_violation
        _ro_msg = _ro_violation(tool_name, args)
        if _ro_msg:
            return _ro_msg
    except Exception as _ro_err:
        logger.warning("[配置保护] 只读守卫异常: %s", _ro_err)

    if tool_name in ("execute_shell", "aup_execute_shell", "run_bash_script"):
        arg_key = "script_content" if tool_name == "run_bash_script" else "cmd"
        _c = _pick_cmd(args)
        if isinstance(_c, str) and _c:
            args[arg_key] = _fix_missing_newlines(_c)
    import inspect
    tool = registry_manager.get_tool_info(tool_name)
    if not tool:
        return f"未知工具：{tool_name}"
    if not tool.get("is_active", True):
        return f"工具已下线：{tool_name}"
    # [large-task] 安全防火墙：权限检查和审计日志
    from plugins.core_utils.security import (
        check_permission, check_tool_roles, check_search_budget, log_audit,
    )

    allowed, reason = check_permission(tool_name, sender_id, DEFAULT_ADMIN)
    if allowed:
        allowed, reason = check_tool_roles(tool_name, tool.get("roles") or [], sender_id)
    if allowed:
        allowed, reason = check_search_budget(tool_name, sender_id)
    if not allowed:
        # AUP 协议拒绝：角色不允许执行高危工具，返回结构化 AUPResult 而非裸字符串
        deny = AUPResult(
            status="error",
            data="权限不足",
            error_code="FORBIDDEN",
            suggestion=reason or "当前账号无该高危操作权限",
        )
        if _qm:
            _qm.aup_reject()
        log_audit(tool_name, args, sender_id, deny.to_json(), 0.0, "denied")
        logger.info("[工具] %s | 状态: 拒绝 | 原因: %s", tool_name, reason)
        return _render_aup_result(deny)

    func = tool["func"]
    # [C-End Tool Proxy fix] func 丢失兜底：AUP 装饰器只存元数据（module_path/func_name），
    # 注册中心 func 字段为 None 时按 module_path + func_name 动态加载真实函数/wrapper
    if func is None:
        try:
            mod_path = tool.get("module_path") or ""
            func_name = tool.get("func_name") or ""
            _mod = __import__(mod_path, fromlist=["*"])
            # 优先按工具名取属性（func_name 可能是内部 wrapper 名，不可靠）
            func = getattr(_mod, tool_name, None)
            if func is None or not callable(func):
                func = getattr(_mod, func_name, None)
            if func is None or not callable(func):
                raise AttributeError(f"模块 {mod_path} 中找不到可调用 {tool_name}")
            logger.info("[execute_tool_directly] 动态加载工具 %s: %s.%s", tool_name, mod_path, func_name)
        except Exception as _e:
            logger.error("动态加载工具 %s 失败: %s", tool_name, _e)
            return f"工具加载失败：{tool_name}"
    if func is None or not callable(func):
        return f"工具不可调用：{tool_name}"
    # ===== 特殊处理：需要注入 sender_id 的工具 =====
    if tool_name in ["add_admin", "del_admin"]:
        args["sender_id"] = sender_id

    # ===== 安全检测（防止 Python 代码被当作 Shell 执行） =====
    if tool_name in ("execute_shell", "aup_execute_shell"):
        cmd = _pick_cmd(args)
        if _BLOCKED_SHELL_PAT.search(cmd):
            return ("已拦截全盘扫描命令（会阻塞全局）：请把范围限定到具体目录，"
                    "例如 find /root/robot -maxdepth 3 -name '*.log'")
        if isinstance(cmd, str) and re.search(r'\b(import|from|def|class|if __name__)\b', cmd):
            return "检测到 Python 代码特征，execute_shell 只允许执行 Shell 命令（如 ls、cd、echo 等）"

    if tool_name == "run_bash_script":
        script = args.get("script_content") or _pick_cmd(args) or ""
        if isinstance(script, str) and re.search(r'\b(import|from|def|class)\b', script):
            return "检测到 Python 代码特征，run_bash_script 只用于 Bash 脚本"

    # ===== 参数过滤与别名映射 =====
    sig = inspect.signature(func)
    valid_params = {}
    valid_param_names = set(sig.parameters.keys())

    # 定义常见别名映射（参数名 → 标准名）
    # [fix] 别名只在"目标名存在于函数签名、且原名不在签名里"时才生效。
    #       原逻辑无条件映射，导致 write_file 的 content 被误映射为 new_content，
    #       随后按签名过滤时被丢弃，最终缺 content 报 TypeError。
    alias_map = {
        "path": "filepath",
        "file": "filepath",
        "content": "new_content",      # 仅对 replace_line/insert_line 生效
        "line": "line_num",
        "line_number": "line_num",
        "command": "cmd",              # [fix] 模型常把 execute_shell 的参数写成 command
        "shell_command": "cmd",        # [fix] 同上，兼容另一种常见写法
    }

    # 1. 别名归一化：签名精确名优先 → 同义组强制对齐（script / script_content → cmd 等）
    normalized_args = _normalize_args(args, valid_param_names | set(alias_map.keys()))
    
    # 2. 只保留函数签名中存在的参数
    for param_name, param in sig.parameters.items():
        if param_name == "sender_id":
            continue  # sender_id 已在上面单独处理
        if param_name in normalized_args:
            valid_params[param_name] = normalized_args[param_name]
        # 如果参数有默认值，且未传入，则使用默认值
        elif param.default != inspect.Parameter.empty:
            valid_params[param_name] = param.default
        # 如果参数没有默认值且未传入，尝试用 None（但会触发 TypeError，交给外层捕获）
        else:
            # 对于必填参数，若未传入则尝试智能补全
            if param_name == "filepath" and "filepath" not in valid_params:
                # 尝试从用户输入中猜测文件路径（兜底）
                if "path" in args:
                    valid_params[param_name] = args["path"]
                elif "file" in args:
                    valid_params[param_name] = args["file"]
                else:
                    # 实在找不到就报错
                    _alts = sorted({a for canon, alts in _SYNONYM_GROUPS if canon == param_name for a in alts})
                    return (f"缺少必填参数：{param_name}"
                            + (f"（也接受：{', '.join(_alts)}）" if _alts else ""))
    # ===== 类型转换层（根据函数签名自动转换参数类型） =====
    type_hints = sig.parameters
    for param_name, param in type_hints.items():
        if param_name == "sender_id" or param_name not in valid_params:
            continue
        annotation = param.annotation
        if annotation == inspect.Parameter.empty:
            continue
        value = valid_params[param_name]

        # bool 类型转换（支持 true/false 字符串、0/1）
        if annotation == bool:
            if isinstance(value, str):
                if value.lower() in ("true", "1", "yes", "on"):
                    valid_params[param_name] = True
                elif value.lower() in ("false", "0", "no", "off"):
                    valid_params[param_name] = False
                else:
                    return f"参数 {param_name} 需要布尔值，收到字符串：{value}"
            elif isinstance(value, (int, float)):
                valid_params[param_name] = bool(value)

        # int 类型转换
        elif annotation == int:
            if isinstance(value, str):
                try:
                    valid_params[param_name] = int(value)
                except ValueError:
                    return f"参数 {param_name} 需要整数，收到：{value}"
            elif isinstance(value, float):
                valid_params[param_name] = int(value)

        # float 类型转换
        elif annotation == float:
            if isinstance(value, str):
                try:
                    valid_params[param_name] = float(value)
                except ValueError:
                    return f"参数 {param_name} 需要浮点数，收到：{value}"

        # str 类型：确保是字符串（非字符串转为字符串）
        elif annotation == str:
            if not isinstance(value, str):
                valid_params[param_name] = str(value)

    # 显式补上 add_admin/del_admin 的 sender_id，防止被参数过滤跳过
    if tool_name in ("add_admin", "del_admin"):
        valid_params["sender_id"] = sender_id

    # 工具运行期上下文：写入本轮 sender_id，供后台 Worker 回推消息定位归属会话
    try:
        from plugins.core_utils.trust import set_runtime
        set_runtime(sender_id)
    except Exception:
        pass

    start_time = time.time()
    try:
        if asyncio.iscoroutinefunction(func):
            result = await _await_with_heartbeat(tool_name, func(**valid_params))
        else:
            result = await _await_with_heartbeat(tool_name, asyncio.to_thread(func, **valid_params))

        elapsed = time.time() - start_time

        # ===== 审计日志（成功） =====
        log_audit(tool_name, args, sender_id, result.to_json() if isinstance(result, AUPResult) else str(result), elapsed, "success")

        # ===== 加分：白名单工具成功执行后 +5 =====
        try:
            from plugins.core_utils.scores import add_score
            add_score(tool_name)
        except Exception as e:
            logger.error(f"[加分] 失败: {e}", exc_info=True)

        if _qm:
            _qm.tool_ok(elapsed * 1000.0)
        logger.info(f"[工具] {tool_name} | 耗时 {elapsed:.2f}s | 状态: 成功")

        if result is None:
            return "执行完成（无输出）"
        # [AUP-集成] 协议渲染边界：AUPResult -> 结构化字符串（解析 status/data/error_code）
        if isinstance(result, AUPResult):
            return _render_aup_result(result)
        return result if isinstance(result, str) else str(result)
    except AUPError as e:
        # [AUP-集成] 协议异常专属渲染：AUPError -> AUPResult -> 结构化字符串
        elapsed = time.time() - start_time
        log_audit(tool_name, args, sender_id, e.to_result().to_json(), elapsed, "failed")
        if _qm:
            _qm.tool_fail(elapsed * 1000.0)
        logger.info(f"[工具] {tool_name} | 耗时 {elapsed:.2f}s | 状态: 失败 | AUP错误: {e.error_code}")
        return _render_aup_result(e.to_result())
    except TypeError as e:
        elapsed = time.time() - start_time
        log_audit(tool_name, args, sender_id, str(e), elapsed, "failed")
        if _qm:
            _qm.tool_fail(elapsed * 1000.0)
        logger.info(f"[工具] {tool_name} | 耗时 {elapsed:.2f}s | 状态: 失败 | 错误: 参数类型错误")
        logger.error(f"参数类型错误: {e}, 传入: {args}, 有效: {valid_params}", exc_info=True)
        return f"参数错误：{e}\n传入参数：{args}\n有效参数：{valid_params}"
    except ValueError as e:
        elapsed = time.time() - start_time
        log_audit(tool_name, args, sender_id, str(e), elapsed, "failed")
        if _qm:
            _qm.tool_fail(elapsed * 1000.0)
        logger.info(f"[工具] {tool_name} | 耗时 {elapsed:.2f}s | 状态: 失败 | 错误: 参数值错误")
        logger.error(f"参数值错误: {e}, 传入: {args}, 有效: {valid_params}", exc_info=True)
        return f"参数值错误：{e}"
    except asyncio.TimeoutError as e:
        elapsed = time.time() - start_time
        log_audit(tool_name, args, sender_id, str(e), elapsed, "failed")
        if _qm:
            _qm.tool_fail(elapsed * 1000.0)
        logger.info(f"[工具] {tool_name} | 耗时 {elapsed:.2f}s | 状态: 失败 | 错误: 超时")
        logger.error(f"工具执行超时: {e}, 工具: {tool_name}", exc_info=True)
        return f"工具执行超时：{str(e)}"
    except Exception as e:
        elapsed = time.time() - start_time
        log_audit(tool_name, args, sender_id, str(e), elapsed, "failed")
        if _qm:
            _qm.tool_fail(elapsed * 1000.0)
        logger.info(f"[工具] {tool_name} | 耗时 {elapsed:.2f}s | 状态: 失败 | 错误: {str(e)[:50]}")
        logger.error(f"工具执行异常: {e}, 工具: {tool_name}", exc_info=True)
        return f"执行失败：{str(e)}"
