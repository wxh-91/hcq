"""
deepseek_web_integration.py — C 端 DeepSeek 会话（网页版）接入封装层

职责：
- 管理 deepseek-mcp-server（Node，booleamu/deepseek-mcp-server 社区逆向包）子进程生命周期：懒启动、按需关闭
- 通过官方 mcp SDK（pip 包 mcp）走 stdio 协议通信（initialize / tools / call_tool）
- 暴露 ask_web_deepseek()，供 ai.py 的闲聊回复路径作为可选备用通道
- 结构化日志：/root/robot/web_deepseek.log（追加，每行一条 JSON：call/init/restart），
  用于统计成功率、延迟分布、失败原因分布

架构（v3：长驻 worker + 请求队列）：
- MCP Python SDK 基于 anyio，其 CancelScope 的 __aenter__ / __aexit__ 必须在同一个
  asyncio task 里执行。nonebot 每条消息是独立 matcher task：若直接在调用方 task 里
  enter / exit MCP 客户端，必然出现跨 task 清理，触发
  "Attempted to exit a cancel scope that isn't the current task's current cancel scope"。
- 因此 MCP 客户端的完整生命周期（stdio_client / ClientSession 的 enter、调用、exit）
  全部收敛到一个专用长驻 worker task（_mcp_worker）内部。外部 ask_web_deepseek()
  只往请求队列投递（prompt + user_id + Future），await Future 拿结果，不直接触碰 MCP SDK。
- worker 常驻复用同一 session；调用失败（非超时）时在同一 worker 内重建 session 并
  重试一次；收到 None 关闭信号后在自身 task 内正常退出（子进程随之回收）。

设计要点（对齐 pi_integration 的封装风格）：
- 仅闲聊路径使用；此通道不提供思考链（响应不带 reasoning_content）
- 网页版会话：DEEPSEEK_USER_TOKEN 认证（chat.deepseek.com 逆向接口，免费），
  按用户维护 session_key 续接多轮上下文（服务端内存态缓存，30 分钟 TTL，重启失效）
- 依赖 models.json 中 web_deepseek 策略的 enabled 开关（默认关闭）
"""
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# ========== 常量 ==========
STRATEGY_NAME = "web_deepseek"
API_KEY_ENV = "DEEPSEEK_USER_TOKEN"
CHAT_TOOL = "deepseek_chat"
LOG_FILE = "/root/robot/web_deepseek.log"   # 结构化日志（追加，每行一条 JSON）
# 服务端在回复末尾追加 "session_key: <uuid>（传入此值可续接对话）"
SESSION_KEY_RE = re.compile(r"session_key:\s*([0-9a-fA-F-]{36})")
DEFAULT_TIMEOUT = 120.0
INIT_TIMEOUT = 30.0
CLOSE_TIMEOUT = 30.0        # close() 等待 worker 优雅退出的上限
SERVER_TIMEOUT_MS = "120000"   # 同步给服务端 DEEPSEEK_TIMEOUT，避免服务端先于客户端超时

# ========== 状态（worker + 请求队列）==========
_mcp_worker_task: Optional["asyncio.Task"] = None   # 长驻 worker
_request_queue: Optional["asyncio.Queue"] = None    # 请求队列（None 为关闭信号）
_worker_lock = asyncio.Lock()                       # 仅保护 worker 启动
_sessions = {}             # user_id -> session_key（模块级；worker/子进程重启不清理）


@dataclass
class _DeepSeekRequest:
    """一次 ask_web_deepseek 调用的队列请求（携带回传 Future）"""
    prompt: str
    user: str
    timeout: float
    conversation_memory: bool
    t0: float                 # 调用方进入 ask_web_deepseek 的时刻（monotonic）
    future: "asyncio.Future"  # 结果 / 异常回传


# ========== 结构化日志 ==========
def _log_event(event: str, user: str, status: str, elapsed: float, reason: str = "") -> None:
    """结构化日志：每行一条 JSON，追加写入、写完即关（不常驻句柄）。

    字段: ts(ISO 秒级) / event(call|init|restart) / user / status(ok|fail) /
          elapsed(秒, 保留两位) / reason(失败原因，成功为空串)。
    写日志自身异常一律吞掉，不影响主流程；文件不存在时自动创建。
    """
    try:
        record = {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "event": event,
            "user": user or "",
            "status": status,
            "elapsed": round(float(elapsed), 2),
            "reason": reason or "",
        }
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[deepseek_web_integration] 写结构化日志失败: {e}")


def _reason_of(exc: BaseException) -> str:
    """把异常映射为结构化日志的 reason（按 401 > 429 > timeout > empty > other 判定）

    401 额外匹配服务端网页版通道的真实报错文案 "Authorization Failed (invalid token)"，
    避免 Token 失效被误归为 other。
    """
    msg = str(exc)
    text = msg.lower()
    if ("401" in msg or "unauthorized" in text or "未授权" in msg
            or "authorization failed" in text or "invalid token" in text):
        return "401"
    if "429" in msg or "rate limit" in text or "限流" in msg:
        return "429"
    if isinstance(exc, asyncio.TimeoutError) or "超时" in msg:
        return "timeout"
    if isinstance(exc, RuntimeError) and "返回空内容" in msg:
        return "empty"
    return "other"


def _get_config():
    from cl import get_strategy_config
    return get_strategy_config(STRATEGY_NAME)


def is_enabled() -> bool:
    """models.json 中 web_deepseek 策略是否存在且 enabled=true"""
    cfg = _get_config()
    if not cfg:
        return False
    return bool(cfg.get("enabled", False))


def get_api_key() -> str:
    return os.getenv(API_KEY_ENV, "").strip()


def _extract_text(result) -> str:
    """从 CallToolResult 中提取全部文本"""
    parts = []
    for item in result.content:
        if isinstance(item, str):
            parts.append(item)
            continue
        txt = getattr(item, "text", None)
        if txt:
            parts.append(txt)
    return "\n".join(parts).strip()


def _remember_session(text: str, user_id) -> None:
    """记录本次回复中的 session_key，供后续续接"""
    if not user_id:
        return
    m = SESSION_KEY_RE.search(text)
    if m:
        _sessions[user_id] = m.group(1)


def _clean_reply(text: str) -> str:
    """去掉服务端附加的 session_key 提示行与 usage 尾注，只留对话正文"""
    lines = [ln for ln in text.splitlines()
             if not ln.strip().startswith("session_key:")]
    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"\n?---\nToken:.*$", "", cleaned).strip()
    return cleaned


# ========== worker 内部：会话生命周期（全部在 worker task 内执行）==========
async def _open_session():
    """启动子进程并建立 MCP 会话；成功返回 (client_cm, session_cm, session)。

    失败时记 init/fail 并抛 RuntimeError（异常语义与旧版一致）。
    必须仅在 worker task 内调用（enter / exit 同 task）。
    """
    t0 = time.monotonic()
    api_key = get_api_key()
    if not api_key:
        _log_event("init", "", "fail", time.monotonic() - t0, "init_fail")
        raise RuntimeError(
            f"未配置 {API_KEY_ENV}（请在 /root/robot/.env 或环境变量中提供网页版 User Token）"
        )
    env = dict(os.environ)
    env[API_KEY_ENV] = api_key
    env["DEEPSEEK_TIMEOUT"] = SERVER_TIMEOUT_MS
    server_params = StdioServerParameters(
        command="deepseek-mcp-server",
        args=[],
        env=env,
    )

    client_cm = None
    session_cm = None
    try:
        client_cm = stdio_client(server_params)
        read, write = await client_cm.__aenter__()
        session_cm = ClientSession(read, write)
        session = await session_cm.__aenter__()
        await asyncio.wait_for(session.initialize(), timeout=INIT_TIMEOUT)
        _log_event("init", "", "ok", time.monotonic() - t0)
        logger.info("[deepseek_web_integration] C 端 DeepSeek 会话初始化成功")
        return (client_cm, session_cm, session)
    except Exception as e:
        _log_event("init", "", "fail", time.monotonic() - t0, "init_fail")
        logger.error(f"[deepseek_web_integration] 初始化失败: {e}", exc_info=True)
        # 清理已进入的部分（仍在同一 worker task 内）
        if session_cm is not None:
            try:
                await session_cm.__aexit__(None, None, None)
            except Exception:
                pass
        if client_cm is not None:
            try:
                await client_cm.__aexit__(None, None, None)
            except Exception:
                pass
        raise RuntimeError(f"C 端 DeepSeek 会话初始化失败: {e}") from e


async def _close_session(ctx) -> None:
    """在 worker task 内关闭一个已建立的会话（enter / exit 同 task）"""
    client_cm, session_cm, _ = ctx
    try:
        await session_cm.__aexit__(None, None, None)
    except Exception as e:
        logger.warning(f"[deepseek_web_integration] 关闭 ClientSession 时出错: {e}")
    try:
        await client_cm.__aexit__(None, None, None)
    except Exception as e:
        logger.warning(f"[deepseek_web_integration] 关闭子进程时出错: {e}")


async def _process_once(ctx, req: _DeepSeekRequest) -> str:
    """用当前 session 执行一次 call_tool；成功返回清理后的回复文本。

    失败抛异常（asyncio.TimeoutError / RuntimeError），由 worker 主循环决定重试策略。
    仅 worker task 内调用。
    """
    _, _, session = ctx
    args = {"message": req.prompt}
    if req.conversation_memory and req.user:
        sk = _sessions.get(req.user)
        if sk:
            args["session_key"] = sk
    result = await asyncio.wait_for(
        session.call_tool(CHAT_TOOL, args),
        timeout=req.timeout,
    )
    # mcp 2.x 的错误标记字段（is_error / 兼容 isError）
    is_error = bool(getattr(result, "is_error", False)
                    or getattr(result, "isError", False))
    text = _extract_text(result)
    if is_error:
        raise RuntimeError(f"C 端 DeepSeek 会话返回错误: {text[:200]}")
    if not text:
        raise RuntimeError("C 端 DeepSeek 会话返回空内容")
    _remember_session(text, req.user)
    return _clean_reply(text)


def _deliver(req: _DeepSeekRequest, reply: str) -> None:
    """成功回传：先记 call/ok，再把结果交给调用方"""
    _log_event("call", req.user, "ok", time.monotonic() - req.t0)
    if not req.future.done():
        req.future.set_result(reply)


def _deliver_fail(req: _DeepSeekRequest, exc: BaseException) -> None:
    """失败回传：把异常交给调用方（调用方 await future 时原样抛出）"""
    if not req.future.done():
        req.future.set_exception(exc)


async def _mcp_worker() -> None:
    """长驻 worker：单 task 内管理 MCP 客户端生命周期并串行处理请求队列。

    - 正常路径：一个 session 服务多个请求；收到 None 关闭信号后在同一 task 内退出。
    - 失败自愈：调用失败（非超时）→ 关闭并重建 session → 重试一次；
      session 初始化失败不影响 worker，下一个请求会再次尝试。
    """
    ctx = None
    try:
        while True:
            req = await _request_queue.get()
            if req is None:
                break
            if req.future.done():      # 调用方已取消 / 超时，跳过
                continue
            if ctx is None:
                try:
                    ctx = await _open_session()
                except Exception as e:
                    # init 失败：init/fail 已记；把异常回传给调用方（保持 call/fail 语义）
                    _log_event("call", req.user, "fail", time.monotonic() - req.t0, _reason_of(e))
                    _deliver_fail(req, e)
                    continue
            try:
                reply = await _process_once(ctx, req)
                _deliver(req, reply)
            except Exception as e:
                _log_event("call", req.user, "fail", time.monotonic() - req.t0, _reason_of(e))
                if isinstance(e, asyncio.TimeoutError):
                    # 超时：不重试，重建 session（丢弃脏状态）
                    await _close_session(ctx)
                    ctx = None
                    _deliver_fail(req, RuntimeError(f"C 端 DeepSeek 会话超时（{req.timeout}s）"))
                    continue
                # 可重试错误：重建 session 重试一次
                await _close_session(ctx)
                ctx = None
                try:
                    ctx = await _open_session()
                    _log_event("restart", req.user, "ok", time.monotonic() - req.t0)
                except Exception as e_init:
                    _log_event("restart", req.user, "fail", time.monotonic() - req.t0, "restart_fail")
                    _deliver_fail(req, RuntimeError(f"C 端 DeepSeek 会话调用失败: {e_init}"))
                    continue
                try:
                    reply = await _process_once(ctx, req)
                    _deliver(req, reply)
                except Exception as e2:
                    _log_event("call", req.user, "fail", time.monotonic() - req.t0, "restart_fail")
                    await _close_session(ctx)
                    ctx = None
                    _deliver_fail(req, RuntimeError(f"C 端 DeepSeek 会话调用失败: {e2}"))
    finally:
        if ctx is not None:
            try:
                await _close_session(ctx)
            except Exception:
                logger.warning("[deepseek_web_integration] worker 退出时关闭会话失败")
        ctx = None


async def _ensure_worker() -> None:
    """确保长驻 worker 已启动（幂等；worker 结束 / 崩溃后自动重建）"""
    global _mcp_worker_task, _request_queue
    if _mcp_worker_task is not None and not _mcp_worker_task.done():
        return
    async with _worker_lock:
        if _mcp_worker_task is not None and not _mcp_worker_task.done():
            return
        _request_queue = asyncio.Queue()
        _mcp_worker_task = asyncio.create_task(_mcp_worker(), name="deepseek-mcp-worker")


async def ask_web_deepseek(prompt: str, user_id: str = None, timeout_seconds: float = None) -> str:
    """
    向 C 端 DeepSeek 网页版会话发送闲聊消息并返回最终文本。

    参数:
        prompt: 用户消息（ai.py 已注入 OCR 前缀等上下文）
        user_id: 会话隔离键（可选）；传入时按用户维护独立 session_key 续接上下文
        timeout_seconds: 覆盖 models.json 中的 timeout

    返回:
        助手的最终回复文本（已去掉 session_key 尾注；此通道无思考链）
    异常:
        RuntimeError: 未配置 Token / 初始化失败 / 超时 / 调用失败

    结构化日志：每次调用记 call/ok 或 call/fail；初始化记 init/ok|fail；
    异常重启记 restart/ok|fail。返回值与异常语义保持不变。

    实现：不直接触碰 MCP SDK——投递请求到队列并 await Future，MCP 客户端
    生命周期全部在长驻 worker task 内完成（避免 anyio cancel scope 跨 task）。
    """
    if not prompt or not str(prompt).strip():
        return ""
    t0 = time.monotonic()
    user = str(user_id) if user_id is not None else ""
    cfg = _get_config() or {}
    timeout = float(timeout_seconds or cfg.get("timeout", DEFAULT_TIMEOUT))
    conversation_memory = bool(cfg.get("conversation_memory", True))

    await _ensure_worker()
    if _request_queue is None:
        raise RuntimeError("C 端 DeepSeek 通道已关闭")

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    await _request_queue.put(_DeepSeekRequest(
        prompt=str(prompt),
        user=user,
        timeout=timeout,
        conversation_memory=conversation_memory,
        t0=t0,
        future=future,
    ))
    return await future   # 异常（RuntimeError 等）原样传播


async def close() -> None:
    """彻底关闭 C 端 DeepSeek 通道：投递关闭信号并等待 worker 在其自身 task 内清理结束。

    与旧版不同：这里必须真的等 worker 结束（子进程在 worker task 内回收），
    而不是发个信号就返回；等待超时时强制取消 worker 作为兜底。
    """
    global _mcp_worker_task, _request_queue
    task = _mcp_worker_task
    if task is None or task.done():
        _mcp_worker_task = None
        _request_queue = None
        logger.info("[deepseek_web_integration] C 端 DeepSeek 会话已关闭")
        return
    try:
        await _request_queue.put(None)
    except Exception:
        pass
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=CLOSE_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("[deepseek_web_integration] 关闭超时，强制取消 worker")
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    _mcp_worker_task = None
    _request_queue = None
    logger.info("[deepseek_web_integration] C 端 DeepSeek 会话已关闭")
