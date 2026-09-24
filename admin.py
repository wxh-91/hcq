import re
import asyncio, aiofiles
import shutil
import json
import time
import logging
logger = logging.getLogger(__name__)
from memory import Memory
from cl import DEFAULT_ADMIN
# 导入工具函数
from bot_utils.downloader import download_m3u8
from bot_utils.wj import backup_project, read_file, write_file, delete_line, replace_line, insert_line
from bot_utils.xt import system_status, execute_shell, ping_host
from bot_utils.admin_gh import add_admin, del_admin, list_admin
from bot_utils.tool_schema_builder import ToolSchemaBuilder, StringSchema, IntegerSchema, BooleanSchema
from bot_utils.script_runner import run_bash_script, _fix_missing_newlines
from bot_utils.checkin import run_checkin
from bot_utils.episodic import recall
from memory import knowledge_db

memory = Memory()

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
    from ai import get_conv_key, TodoListManager
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
    results = recall(keyword, limit=20)
    if not results:
        return f"今天没有找到与「{keyword}」相关的对话记录"
    lines = [f"找到 {len(results)} 条相关记录："]
    for r in results:
        content = r.get("content", "")
        if len(content) > 200:
            content = content[:200] + "..."
        lines.append(f"[{r.get('ts', '')}] {r.get('role', '')}: {content}")
    return "\n".join(lines)
# ========== 工具注册表（AI 可调用） ==========
TOOLS = {
    "checkin": {
        "func": run_checkin,
        "need_confirm": False,
        "is_danger": False,
        "params_desc": {}
    },
    "query_knowledge": {
        "func": query_knowledge,
        "need_confirm": False,
        "is_danger": False,
        "params_desc": {"keyword": "要搜索的关键词"}
    },
    "recall_conversation": {
        "func": recall_conversation,
        "need_confirm": False,
        "is_danger": False,
        "params_desc": {
            "keyword": "检索关键词。必须从用户原话中摘取实际出现过的词（如用户说'今天执行过什么命令'，就取'执行'），不要概括成抽象词（如'命令'）"
        }
    },
    "get_xmkj": {
        "func": get_xmkj,
        "need_confirm": False,
        "is_danger": False,
        "params_desc": {}
    },
    "agent_manager": {
        "func": agent_manager,
        "need_confirm": False,
        "is_danger": False,
        "params_desc": {
            "action": "操作类型：list（查看清单）、add（添加任务）、execute（执行任务）、status（查看状态）",
            "description": "任务描述（action=add 时必填）",
            "task_id": "任务ID（action=execute/status 时必填）"
        }
    },
    "read_file": {
        "func": read_file,
        "need_confirm": False,
        "is_danger": False,
        "params_desc": {
            "filepath": "要读取的文件路径（相对于项目根目录）",
            "max_len": "最大返回字符数，默认12000"
        }
    },
    "write_file": {
        "func": write_file,
        "need_confirm": True,
        "is_danger": True,
        "params_desc": {
            "filepath": "文件绝对路径或相对路径（如 ~/robot/rt.py），严禁包含'创建一个名为'、'在...目录'等中文描述，只输出纯路径！",
            "content": "要写入的完整内容",
            "backup": "是否自动备份原文件，默认True"
        }
    },
    "replace_line": {
        "func": replace_line,
        "need_confirm": True,
        "is_danger": True,
        "params_desc": {
            "filepath": "文件路径",
            "line_num": "要替换的行号（从1开始）",
            "new_content": "新行内容"
        }
    },
    "delete_line": {
        "func": delete_line,
        "need_confirm": True,
        "is_danger": True,
        "params_desc": {
            "filepath": "文件路径",
            "line_num": "要删除的行号（从1开始）"
        }
    },
    "ping_host": {
        "func": ping_host,
        "need_confirm": False,
        "is_danger": False,
        "params_desc": {"host": "要检测的主机名或IP地址"}
    },
    "insert_line": {
        "func": insert_line,
        "need_confirm": True,
        "is_danger": True,
        "params_desc": {
            "filepath": "文件路径",
            "line_num": "要在哪一行前插入（有效范围 1~行数+1）",
            "new_content": "新行内容"
        }
    },
    "backup_project": {"func": backup_project, "need_confirm": False, "is_danger": False, "params_desc": {}},
    "execute_shell": {
        "func": execute_shell,
        "need_confirm": True,
        "is_danger": True,
        "params_desc": {"cmd": "要执行的Shell命令"}
    },
    "system_status": {"func": system_status, "need_confirm": False, "is_danger": False, "params_desc": {}},
    "list_admin": {"func": list_admin, "need_confirm": False, "is_danger": False, "params_desc": {}},
    "add_admin": {
        "func": add_admin,
        "need_confirm": True,
        "is_danger": False,
        "params_desc": {
            "sender_id": "发起操作的管理员QQ号（系统自动注入）",
            "new_admin": "要添加的新管理员QQ号"
        }
    },
    "del_admin": {
        "func": del_admin,
        "need_confirm": True,
        "is_danger": False,
        "params_desc": {
            "sender_id": "发起操作的管理员QQ号（系统自动注入）",
            "del_admin": "要删除的管理员QQ号"
        }
    },
    "set_group_chat_enabled": {
        "func": set_group_chat_enabled,
        "need_confirm": False,
        "is_danger": False,
        "params_desc": {"enabled": "是否开启群聊功能（true=开启，false=关闭）"}
    },

    "get_group_chat_status": {"func": get_group_chat_status, "need_confirm": False, "is_danger": False, "params_desc": {}},
    "download_m3u8": {
        "func": download_m3u8,
        "need_confirm": False,
        "is_danger": False,
        "params_desc": {
            "url": "m3u8视频流链接（必填）",
            "referer": "来源防盗链地址（可选，不填自动补全）",
            "output_name": "输出文件名（不含后缀，可选，默认为video）"
        }
    },
    "run_bash_script": {
        "func": run_bash_script,
        "need_confirm": True,
        "is_danger": True,
        "params_desc": {
            "script_content": "要执行的多行 Bash 脚本内容（纯文本）",
            "timeout": "超时秒数，默认30，可选"
        }
    },
}

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
        "download_m3u8": "从m3u8链接下载视频，支持加密流自动解密合并。参数：url(必填)、referer(可选)、output_name(可选)",
        "run_bash_script": "执行AI动态生成的多行Bash脚本（高危，支持超时控制）",
    }
    return default_descs.get(tool_name, "无描述")

async def _call_tool_router(user_input: str, sender_id: str) -> dict | None:
    """使用 OpenAI function calling 进行工具路由"""
    tools = []
    for name, info in TOOLS.items():
        func = info["func"]
        params_desc = info.get("params_desc", {})
        schema = build_tool_schema(name, func, params_desc)
        tools.append(schema)
    
    messages = [
        {"role": "system", "content": "你是一个工具调用助手，根据用户指令选择合适的工具。如果不需要工具，则直接回复。"},
        {"role": "user", "content": user_input}
    ]
    
    from ai import safe_api_request
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
    return None

async def _execute_admin_command(cmd: str, sender_id: str, chat_type: str = "private", group_id: str = "") -> str | None:
    logger.debug(f"_execute_admin_command 收到 cmd: {cmd}")

    # ----- 特殊：帮助指令（无需权限） -----
    if cmd == "帮助" or cmd == "help":
        return get_help_text()

    if cmd in FAST_COMMANDS:
        if str(sender_id) != DEFAULT_ADMIN:
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
        from ai import ask_ai
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
    logger.debug(f"准备执行工具: {tool_name}, 参数: {args}")

    tool = TOOLS[tool_name]
    func = tool["func"]
    logger.debug(f"实际执行函数: {func.__name__}")
    if tool_name in ["add_admin", "del_admin"]:
        args["sender_id"] = sender_id
    
    try:
        if asyncio.iscoroutinefunction(func):
            result = await func(**args)
        else:
            result = func(**args)
        logger.debug(f"工具执行完成，结果长度: {len(result) if result else 0}")
        return result if isinstance(result, str) else str(result)
    except TypeError as e:
        logger.error(f"参数类型错误: {e}, 参数: {args}", exc_info=True)
        return f"参数错误：{e}。AI 生成的参数与工具不匹配。"
    except ValueError as e:
        logger.error(f"参数值错误: {e}, 参数: {args}", exc_info=True)
        return f"参数值错误：{e}"
    except Exception as e:
        logger.error(f"工具执行异常: {e}, 工具: {tool_name}", exc_info=True)
        return f"执行失败：{str(e)}"

async def handle_admin_command(raw_input, sender_id, force_tool_route=False, chat_type="private", group_id=""):
    logger.debug(f"handle_admin_command 被调用，raw_input={raw_input}")
    is_super_admin = str(sender_id) == DEFAULT_ADMIN
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

async def execute_tool_directly(tool_name: str, args: dict, sender_id: str) -> str:
    if tool_name == "execute_shell":
        arg_key = "cmd"
    elif tool_name == "run_bash_script":
        arg_key = "script_content"
    else:
        arg_key = None

    if arg_key and arg_key in args:
        cmd = args[arg_key]
        if isinstance(cmd, str):
            args[arg_key] = _fix_missing_newlines(cmd)

    # 如果 args 是字符串，尝试解析为字典
    if isinstance(args, str):
        try:
            import json
            args = json.loads(args)
        except json.JSONDecodeError:
            return "参数格式错误：无法解析 JSON 字符串"
    import inspect
    tool = TOOLS.get(tool_name)
    if not tool:
        return f"未知工具：{tool_name}"
    
    # 危险工具权限检查
    if tool.get("is_danger", False):
        if str(sender_id) != DEFAULT_ADMIN:
            return f"危险操作 {tool_name} 仅限主管理员执行"
    
    func = tool["func"]
    
    # ===== 特殊处理：需要注入 sender_id 的工具 =====
    if tool_name in ["add_admin", "del_admin"]:
        args["sender_id"] = sender_id

    # ===== 安全检测（防止 Python 代码被当作 Shell 执行） =====
    if tool_name == "execute_shell":
        cmd = args.get("cmd", "")
        if isinstance(cmd, str) and re.search(r'\b(import|from|def|class|if __name__)\b', cmd):
            return "检测到 Python 代码特征，execute_shell 只允许执行 Shell 命令（如 ls、cd、echo 等）"

    if tool_name == "run_bash_script":
        script = args.get("script_content", "")
        if isinstance(script, str) and re.search(r'\b(import|from|def|class)\b', script):
            return "检测到 Python 代码特征，run_bash_script 只用于 Bash 脚本"

    # ===== 参数过滤与别名映射 =====
    sig = inspect.signature(func)
    valid_params = {}
    
    # 定义常见别名映射（参数名 → 标准名）
    alias_map = {
        "path": "filepath",
        "file": "filepath",
        "content": "new_content",      # 用于 replace_line/insert_line
        "line": "line_num",
        "line_number": "line_num",
    }
    
    # 1. 先将别名转换为标准名
    normalized_args = {}
    for k, v in args.items():
        if k in alias_map:
            normalized_args[alias_map[k]] = v
        else:
            normalized_args[k] = v
    
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
                    return f"缺少必填参数：{param_name}"
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

    start_time = time.time()
    try:
        if asyncio.iscoroutinefunction(func):
            result = await func(**valid_params)
        else:
            result = func(**valid_params)

        elapsed = time.time() - start_time

        # ===== 加分：白名单工具成功执行后 +5 =====
        try:
            from bot_utils.scores import add_score
            add_score(tool_name)
        except Exception as e:
            logger.error(f"[加分] 失败: {e}", exc_info=True)

        logger.info(f"[工具] {tool_name} | 耗时 {elapsed:.2f}s | 状态: 成功")

        if result is None:
            return "执行完成（无输出）"
        return result if isinstance(result, str) else str(result)
    except TypeError as e:
        elapsed = time.time() - start_time
        logger.info(f"[工具] {tool_name} | 耗时 {elapsed:.2f}s | 状态: 失败 | 错误: 参数类型错误")
        logger.error(f"参数类型错误: {e}, 传入: {args}, 有效: {valid_params}", exc_info=True)
        return f"参数错误：{e}\n传入参数：{args}\n有效参数：{valid_params}"
    except ValueError as e:
        elapsed = time.time() - start_time
        logger.info(f"[工具] {tool_name} | 耗时 {elapsed:.2f}s | 状态: 失败 | 错误: 参数值错误")
        logger.error(f"参数值错误: {e}, 传入: {args}, 有效: {valid_params}", exc_info=True)
        return f"参数值错误：{e}"
    except asyncio.TimeoutError as e:
        elapsed = time.time() - start_time
        logger.info(f"[工具] {tool_name} | 耗时 {elapsed:.2f}s | 状态: 失败 | 错误: 超时")
        logger.error(f"工具执行超时: {e}, 工具: {tool_name}", exc_info=True)
        return f"工具执行超时：{str(e)}"
    except Exception as e:
        elapsed = time.time() - start_time
        logger.info(f"[工具] {tool_name} | 耗时 {elapsed:.2f}s | 状态: 失败 | 错误: {str(e)[:50]}")
        logger.error(f"工具执行异常: {e}, 工具: {tool_name}", exc_info=True)
        return f"执行失败：{str(e)}"
