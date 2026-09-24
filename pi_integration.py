"""
pi_integration.py — ECHO 项目接入 pi-backend 的封装层
职责：模式管理、懒初始化、统一调用接口
"""
import logging
logger = logging.getLogger(__name__)

import asyncio
import sys
from typing import Optional, Dict, Any

from pi_backend import PIProcess, PIToolBackend, PIBackend

# ========== 状态单例 ==========
_pi_process: Optional[PIProcess] = None
_pi_backend: Optional[PIBackend] = None
_tool_backend: Optional[PIToolBackend] = None
_tool_server_task: Optional[asyncio.Task] = None

# ========== 模式管理 ==========
_backend_mode: str = "api"          # "api" 或 "pi"
_initialized: bool = False          # Pi后端是否已初始化

# ========== 工具注册 ==========
def _make_async_wrapper(sync_func):
    async def wrapper(**kwargs):
        return await asyncio.to_thread(sync_func, **kwargs)
    return wrapper

def _register_echo_tools(tool_backend: PIToolBackend) -> None:
    try:
        import admin
    except ImportError as e:
        print(f"[pi_integration] 警告: 无法导入 admin 模块，跳过工具注册: {e}")
        return
    tools_dict = getattr(admin, "TOOLS", {})
    if not tools_dict:
        print("[pi_integration] 警告: admin.TOOLS 为空，无工具可注册")
        return
    registered_count = 0
    for tool_name, tool_func in tools_dict.items():
        if not callable(tool_func):
            continue
        async_wrapper = _make_async_wrapper(tool_func)
        try:
            tool_backend.register_tool(async_wrapper, name=tool_name)
            registered_count += 1
        except RuntimeError as e:
            print(f"[pi_integration] 注册工具 {tool_name} 失败: {e}")
    print(f"[pi_integration] 已注册 {registered_count} 个工具到 PIToolBackend")

# ========== 初始化（按需） ==========
async def _ensure_initialized(
    pi_path: Optional[str] = None,
    session_dir: Optional[str] = None,
    socket_host: str = "127.0.0.1",
    socket_port: int = 39999,
    tool_timeout: float = 30.0,
) -> None:
    """内部函数：确保Pi后端已初始化，若未初始化则执行初始化"""
    global _pi_process, _tool_backend, _pi_backend, _tool_server_task, _initialized
    if _initialized:
        return

    print("[pi_integration] 正在初始化 Pi 后端...")
    try:
        _pi_process = await PIProcess.build_process(
            pi_path=pi_path,
            session_dir=session_dir,
            tools=None,
            system_prompt=None,
        )
        _tool_backend = PIToolBackend(host=socket_host, port=socket_port)
        _tool_backend.set_timeout(tool_timeout)
        _register_echo_tools(_tool_backend)
        _, server_task = await _tool_backend.run_server()
        _tool_server_task = server_task
        _pi_backend = PIBackend(pi_process=_pi_process, tool_backend=_tool_backend)
        _initialized = True
        print("[pi_integration] Pi 后端初始化成功")
    except Exception as e:
        await _close_internal()
        raise RuntimeError(f"Pi 后端初始化失败: {e}") from e

async def _close_internal() -> None:
    """内部清理函数（不重置模式）"""
    global _pi_process, _tool_backend, _pi_backend, _tool_server_task, _initialized
    if _tool_backend is not None:
        try:
            await _tool_backend.close_backend()
        except Exception as e:
            print(f"[pi_integration] 关闭 PIToolBackend 时出错: {e}")
    if _tool_server_task is not None and not _tool_server_task.done():
        _tool_server_task.cancel()
        try:
            await _tool_server_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[pi_integration] 取消工具服务任务时出错: {e}")
        _tool_server_task = None
    if _pi_process is not None:
        try:
            await _pi_process.close_process()
        except Exception as e:
            print(f"[pi_integration] 关闭 PIProcess 时出错: {e}")
    _pi_process = None
    _tool_backend = None
    _pi_backend = None
    _initialized = False

# ========== 对外接口 ==========
async def set_mode(mode: str) -> None:
    """
    切换后端模式：'api' 或 'pi'
    若切换到 'pi' 且未初始化，则自动初始化 Pi 后端
    """
    global _backend_mode
    if mode not in ("api", "pi"):
        raise ValueError("mode 必须是 'api' 或 'pi'")
    if mode == _backend_mode:
        return
    if mode == "pi":
        await _ensure_initialized()
    # 切换后，原有Pi后端不关闭（以便快速切回），但若从pi切回api，保留资源不释放，但不会使用
    _backend_mode = mode
    print(f"[pi_integration] 后端模式已切换为: {mode}")

def get_mode() -> str:
    return _backend_mode

async def ask_pi(
    user_input: str,
    timeout_seconds: float = 60.0,
    idle_timeout_seconds: float = 10.0,
    images: Optional[list] = None
) -> str:
    """
    向 Pi 发送消息并等待最终回复（流式滑窗超时版）。

    参数:
        user_input: 用户文本
        timeout_seconds: 整体最大等待时间（秒）
        idle_timeout_seconds: 连续无数据超时（秒），即如果 Pi 超过此时间未输出任何行，则判定失败
        images: 可选图片列表

    返回:
        助手的最终回复文本
    """
    if _backend_mode != "pi":
        raise RuntimeError("当前模式为 'api'，不能调用 ask_pi，请先切换至 'pi' 模式")

    await _ensure_initialized()
    if _pi_backend is None:
        raise RuntimeError("Pi 后端未正确初始化")

    # ---- 步骤1：清空管道残留（防止旧任务堵塞） ----
    try:
        # 尝试中止旧任务
        await _pi_backend.abort()
        # 快速读取残留数据（最多 10 行），避免干扰新任务
        for _ in range(10):
            try:
                await asyncio.wait_for(_pi_backend.read_jsonl(), timeout=0.5)
            except (asyncio.TimeoutError, Exception):
                break
    except Exception as e:
        logger.warning(f"[ask_pi] 清理管道时异常: {e}")

    # ---- 步骤2：发送新指令 ----
    try:
        await _pi_backend.prompt(message=user_input, images=images, request_id=None)
    except Exception as e:
        # 写入失败说明管道可能已损坏，尝试重置后端
        logger.error(f"[ask_pi] 写入失败: {e}，尝试重置 Pi 后端")
        await close_pi_backend()
        raise RuntimeError(f"Pi 写入失败，后端已重置: {e}") from e

    # ---- 步骤3：流式滑窗读取 ----
    start_time = asyncio.get_event_loop().time()
    last_activity = start_time
    collected_texts = []

    while True:
        # 检查整体超时
        if asyncio.get_event_loop().time() - start_time > timeout_seconds:
            raise asyncio.TimeoutError(f"Pi 响应整体超时 ({timeout_seconds}s)")

        # 单行读取（带短超时，用于检测空闲）
        try:
            raw = await asyncio.wait_for(_pi_backend.read_jsonl(), timeout=1.0)
        except asyncio.TimeoutError:
            # 单行超时：检查空闲超时
            if asyncio.get_event_loop().time() - last_activity > idle_timeout_seconds:
                raise RuntimeError(f"Pi 空闲超时（{idle_timeout_seconds}s 内无输出），可能进程已卡死")
            # 否则继续等待
            continue
        except Exception as e:
            raise RuntimeError(f"读取 Pi 响应时发生异常: {e}") from e

        # 收到数据，刷新活动时间戳
        last_activity = asyncio.get_event_loop().time()

        # 调试日志（生产环境可保留）
        logger.info(f"[ask_pi] 收到行: {raw}")

        # ---- 事件处理 ----
        event_type = raw.get("type")

        # 结束事件（agent_end / turn_end）
        if event_type in ("agent_end", "turn_end"):
            # 尝试从 messages 或 message 中提取最终文本
            messages = raw.get("messages") or [raw.get("message", {})]
            for msg in messages if isinstance(messages, list) else [messages]:
                if msg.get("role") == "assistant":
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        texts = [item.get("text", "") for item in content if item.get("type") == "text"]
                        if texts:
                            return "".join(texts)
                    elif isinstance(content, str) and content:
                        return content
            # 如果结束事件中没有文本，继续等待（可能是空回复）
            continue

        # 流式文本增量（message_update / text_delta）
        if event_type == "message_update":
            event_data = raw.get("assistantMessageEvent", {})
            if event_data.get("type") == "text_delta":
                delta = event_data.get("delta", "")
                if delta:
                    collected_texts.append(delta)
                # 部分内容可能已在 partial 中，但我们只收集 delta，防止重复
                continue

        # 兜底：如果有直接文本字段，立即返回
        text = raw.get("text") or raw.get("content") or raw.get("message")
        if text and isinstance(text, str) and text.strip():
            return text

        # 错误事件
        if event_type == "error":
            raise RuntimeError(f"Pi 错误事件: {raw.get('error', '未知错误')}")

        # 其他事件忽略（如 thinking、tool_result 等）

        # 可选：如果收集到的文本片段达到一定长度且包含结束标志，提前返回
        # 但大多数情况由 agent_end 驱动，不额外处理

async def close_pi_backend() -> None:
    """彻底关闭Pi后端并释放资源（无论当前模式）"""
    await _close_internal()
    # 重置模式为 api（因为Pi已不可用）
    global _backend_mode
    _backend_mode = "api"
    print("[pi_integration] Pi 后端已关闭，模式自动切换为 'api'")
