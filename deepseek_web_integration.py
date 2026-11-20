"""
deepseek_web_integration.py — C 端 DeepSeek 会话（网页版）接入封装层（v4：HTTP 直连）

职责：
- 直接调用 chat.deepseek.com 内部接口（逆向协议），不再依赖 MCP 子进程 / mcp SDK
- 认证：DEEPSEEK_USER_TOKEN（网页版 LocalStorage 的 userToken，Authorization: Bearer）
- 联网：请求体显式传 search_enabled=true（网页版 UI 权限层开关，非 prompt 关键词触发；
  是否真的搜索由 C 端 AI 意图决定，响应中 search_triggered 字段指示）
- 会话：按 user_id 维护 (chat_session_id, parent_message_id)，续接多轮上下文
- PoW：DeepSeekHashV1 挑战。求解器为内联 Node 脚本，加载 booleamu 包内置
  sha3_wasm_bg.wasm（同一 Rust 编译产物，Node 原生 WebAssembly 零依赖运行）；
  wasm 路径可用环境变量 DEEPSEEK_POW_WASM 覆盖
- 结构化日志：${PROJECT_ROOT}/web_deepseek.log（追加，每行一条 JSON：call/init/restart），
  用于统计成功率、延迟分布、失败原因分布
- 对外接口不变：ask_web_deepseek / is_enabled / get_api_key / close

协议（chat.deepseek.com/api/v0）：
1. POST /chat_session/create            -> chat_session_id
2. POST /chat/create_pow_challenge      -> DeepSeekHashV1 挑战
3. 求解 PoW -> x-ds-pow-response（base64 JSON）
4. POST /chat/completion（SSE）         -> 回复正文 + references + message_id
   body: {chat_session_id, parent_message_id, prompt, ref_file_ids: [],
          thinking_enabled, search_enabled}
   - parent_message_id 必须是数字（u32），取上一轮响应的 response_message_id
   - 服务端会校验 x-ds-pow-response，缺失/错误返回 40300（MISSING_HEADER）
   - search_enabled=true 时若 AI 判定需要搜索，fragments 出现 SEARCH 片段，
     随后 "response/fragments/-1/results" 推送引用结果

设计要点（对齐 pi_integration 的封装风格）：
- 仅闲聊路径使用；此通道不提供思考链（thinking_enabled=false，响应无 reasoning_content）
- 网页版会话：DEEPSEEK_USER_TOKEN 认证（免费），服务端会话有 TTL，重启失效
- 依赖 models.json 中 web_deepseek 策略的 enabled 开关（默认关闭）
- 每次调用为独立 asyncio 操作（aiohttp），无跨 task 生命周期问题；
  close() 关闭复用连接，无子进程残留（Node 求解为短生命子进程，每次 wait 退出）
"""
import os
_ROOT = os.environ.get("PROJECT_ROOT") or os.path.dirname(os.path.abspath(__file__))

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

import aiohttp


# ========== 常量 ==========
STRATEGY_NAME = "web_deepseek"
API_KEY_ENV = "DEEPSEEK_USER_TOKEN"
LOG_FILE = _ROOT + "/web_deepseek.log"   # 结构化日志（追加，每行一条 JSON）
DEFAULT_TIMEOUT = 120.0
POW_TIMEOUT = 20.0          # 单次 PoW 求解（含子进程）超时
# DeepSeekHashV1 求解器 wasm（booleamu 包内置，与 xtekky/deepseek4free 同一文件），
# 可用环境变量 DEEPSEEK_POW_WASM 覆盖
_POW_WASM_CANDIDATES = [
    "/usr/local/lib/booleamu-deepseek-mcp-server/src/sha3_wasm_bg.wasm",
    "/usr/local/lib/booleamu-deepseek-mcp-server/dist/sha3_wasm_bg.wasm",
]
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 内联 Node 求解脚本（零依赖：Node 原生 WebAssembly 加载 wasm 解 DeepSeekHashV1）
# 用法: node -e <本脚本> '<challenge_json>'；stdout 输出 {"ok":true,"pow_response":"..."}
_POW_SOLVER_JS = r"""
const fs = require('fs');
const WASM_PATH = process.env.DEEPSEEK_POW_WASM;
async function solve(challenge) {
  if (!WASM_PATH || !fs.existsSync(WASM_PATH)) throw new Error('wasm not found: ' + WASM_PATH);
  const wasmBytes = fs.readFileSync(WASM_PATH);
  const { instance } = await WebAssembly.instantiate(wasmBytes);
  const exports = instance.exports;
  if (challenge.algorithm !== 'DeepSeekHashV1') throw new Error('unsupported algorithm: ' + challenge.algorithm);
  const prefix = challenge.salt + '_' + challenge.expire_at + '_';
  const memory = exports.memory;
  const addToStack = exports.__wbindgen_add_to_stack_pointer;
  const alloc = exports.__wbindgen_export_0;
  const wasmSolve = exports.wasm_solve;
  const encoder = new TextEncoder();
  function writeString(str) {
    const bytes = encoder.encode(str);
    const ptr = alloc(bytes.length, 1);
    new Uint8Array(memory.buffer, ptr, bytes.length).set(bytes);
    return [ptr, bytes.length];
  }
  const retptr = addToStack(-16);
  const [challengePtr, challengeLen] = writeString(challenge.challenge);
  const [prefixPtr, prefixLen] = writeString(prefix);
  wasmSolve(retptr, challengePtr, challengeLen, prefixPtr, prefixLen, challenge.difficulty);
  const resultView = new DataView(memory.buffer);
  const status = resultView.getInt32(retptr, true);
  const value = resultView.getFloat64(retptr + 8, true);
  addToStack(16);
  if (status === 0) throw new Error('pow solve failed');
  const powObj = {
    algorithm: challenge.algorithm,
    challenge: challenge.challenge,
    salt: challenge.salt,
    answer: Math.floor(value),
    signature: challenge.signature,
    target_path: challenge.target_path,
  };
  console.log(JSON.stringify({ ok: true, pow_response: Buffer.from(JSON.stringify(powObj)).toString('base64') }));
}
solve(JSON.parse(process.argv[process.argv.length - 1])).catch(e => { console.log(JSON.stringify({ ok: false, error: String(e && e.message || e) })); process.exit(1); });
"""

# ========== 状态 ==========
_http_session: Optional[aiohttp.ClientSession] = None   # 复用的 HTTP 连接
_initialized = False                                    # 是否已完成初始化（init 事件只记一次）
_init_lock = asyncio.Lock()
_sessions = {}          # user_id -> {"session_id": str, "parent_message_id": int|None}
                        # 模块级；会话重建/失败不清理（与旧版 session_key 缓存同理）


# ========== 结构化日志（与 v3 完全一致） ==========
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


def _pow_wasm_path() -> str:
    """定位 DeepSeekHashV1 求解器 wasm（环境变量优先，其次 booleamu 包内置路径）"""
    env_path = os.getenv("DEEPSEEK_POW_WASM", "").strip()
    if env_path and os.path.exists(env_path):
        return env_path
    for cand in _POW_WASM_CANDIDATES:
        if os.path.exists(cand):
            return cand
    return ""


# ========== 初始化 ==========
async def _ensure_initialized() -> None:
    """幂等初始化：校验 token、建立复用的 aiohttp 连接。首次执行记 init 事件。"""
    global _http_session, _initialized
    if _initialized and _http_session is not None and not _http_session.closed:
        return
    async with _init_lock:
        if _initialized and _http_session is not None and not _http_session.closed:
            return
        t0 = time.monotonic()
        api_key = get_api_key()
        if not api_key:
            _log_event("init", "", "fail", time.monotonic() - t0, "init_fail")
            raise RuntimeError(
                f"未配置 {API_KEY_ENV}（请在 ${PROJECT_ROOT}/.env 或环境变量中提供网页版 User Token）"
            )
        if not _pow_wasm_path():
            _log_event("init", "", "fail", time.monotonic() - t0, "init_fail")
            raise RuntimeError(
                "未找到 DeepSeekHashV1 PoW 求解器 wasm（可用 DEEPSEEK_POW_WASM 指定路径）"
            )
        try:
            _http_session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "x-client-platform": "web",
                    "User-Agent": _UA,
                    "Origin": "https://chat.deepseek.com",
                    "Referer": "https://chat.deepseek.com/",
                }
            )
            _initialized = True
            _log_event("init", "", "ok", time.monotonic() - t0)
            logger.info("[deepseek_web_integration] C 端 DeepSeek（HTTP 直连）初始化成功")
        except Exception as e:
            _log_event("init", "", "fail", time.monotonic() - t0, "init_fail")
            logger.error(f"[deepseek_web_integration] 初始化失败: {e}", exc_info=True)
            raise RuntimeError(f"C 端 DeepSeek 会话初始化失败: {e}") from e


# ========== 网页版 API 调用 ==========
async def _post_json(session: aiohttp.ClientSession, path: str, body: dict,
                     timeout: float, expect_json: bool = True):
    """POST JSON，返回 (http_status, 解析结果)；HTTP 错误/业务错误统一抛 RuntimeError"""
    try:
        async with session.post(
                f"https://chat.deepseek.com/api/v0{path}",
                json=body,
                headers={"Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"},
                timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            text = await resp.text()
    except asyncio.TimeoutError:
        raise
    except (aiohttp.ClientError, OSError) as e:
        raise RuntimeError(f"C 端 DeepSeek 网络请求失败: {e}") from e

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        raise RuntimeError(
            f"C 端 DeepSeek 返回非 JSON（HTTP {resp.status}）: {text[:200]}")

    code = data.get("code")
    if code not in (0, None):
        msg = str(data.get("msg") or "")
        if resp.status == 401 or code == 401 or "authorization" in msg.lower() \
                or "invalid token" in msg.lower():
            raise RuntimeError(f"Authorization Failed (invalid token): {msg}")
        if resp.status == 429 or code == 429 or "rate limit" in msg.lower():
            raise RuntimeError(f"Rate limit exceeded: {msg}")
        raise RuntimeError(f"API 错误 code={code} msg={msg} (HTTP {resp.status})")
    return data


async def _solve_pow(session: aiohttp.ClientSession) -> str:
    """获取并求解 DeepSeekHashV1 挑战，返回 x-ds-pow-response 头值"""
    data = await _post_json(
        session, "/chat/create_pow_challenge",
        {"target_path": "/api/v0/chat/completion"}, POW_TIMEOUT)
    challenge = data["data"]["biz_data"]["challenge"]
    env = dict(os.environ)
    env["DEEPSEEK_POW_WASM"] = _pow_wasm_path()
    proc = await asyncio.create_subprocess_exec(
        "node", "-e", _POW_SOLVER_JS, json.dumps(challenge),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=POW_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError("C 端 DeepSeek PoW 求解超时")
    if proc.returncode != 0:
        raise RuntimeError(f"C 端 DeepSeek PoW 求解失败: {(err or out).decode('utf-8', 'ignore')[:200]}")
    try:
        result = json.loads(out.decode("utf-8", "ignore"))
    except (json.JSONDecodeError, ValueError):
        raise RuntimeError("C 端 DeepSeek PoW 求解输出无法解析")
    if not result.get("ok"):
        raise RuntimeError(f"C 端 DeepSeek PoW 求解失败: {result.get('error', '')}")
    return result["pow_response"]


async def _create_session(session: aiohttp.ClientSession) -> str:
    """新建网页版会话，返回 chat_session_id"""
    data = await _post_json(session, "/chat_session/create", {"agent": "chat"},
                            DEFAULT_TIMEOUT)
    # [Bug Buster] 安全取值：接口字段变动时禁止协程崩溃，拿不到 id 抛明确错误
    # 交由上层（ai.py ask_ai 的 try/except）平滑降级到本地 chat 策略。
    try:
        # [抓包实证 2026-10-24] /chat_session/create 原始响应：
        # data.biz_data.chat_session.id 才是会话 id（biz_data.id 不存在，为 None）
        _biz = (data or {}).get("data", {}).get("biz_data", {}) or {}
        _cs = _biz.get("chat_session") or {}
        sid = _cs.get("id") or _biz.get("id")   # chat_session.id 优先，biz_data.id 兜底
    except AttributeError:
        sid = None
    if not sid:
        raise RuntimeError(
            "C 端 DeepSeek 创建会话失败：响应缺少会话 id"
            "（接口可能已变更，原始响应片段: %s）" % str(data)[:120]
        )
    return sid


async def _chat_once(session: aiohttp.ClientSession, session_id: str,
                     parent_message_id: Optional[int], prompt: str,
                     timeout: float) -> tuple:
    """发送一次对话请求并解析 SSE 流。

    返回 (text, new_parent_message_id, references)；
    联网结果（references）为 [{url, title, cite_index}]。
    """
    pow_response = await _solve_pow(session)
    body = {
        "chat_session_id": session_id,
        "parent_message_id": parent_message_id,   # 必须是数字或 null
        "prompt": prompt,
        "ref_file_ids": [],
        "thinking_enabled": False,   # 此通道不提供思考链
        "search_enabled": True,      # 打开网页版 UI 权限层联网开关
    }
    try:
        async with session.post(
                "https://chat.deepseek.com/api/v0/chat/completion",
                json=body,
                headers={"Content-Type": "application/json",
                         "Accept": "text/event-stream",
                         "x-ds-pow-response": pow_response},
                timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                err_text = await resp.text()
                raise RuntimeError(
                    f"C 端 DeepSeek 对话请求失败（HTTP {resp.status}）: {err_text[:300]}")
            return await _parse_sse(resp.content)
    except asyncio.TimeoutError:
        raise
    except (aiohttp.ClientError, OSError) as e:
        raise RuntimeError(f"C 端 DeepSeek 网络请求失败: {e}") from e


async def _parse_sse(stream) -> tuple:
    """解析 chat/completion 的 SSE 流（兼容网页版实测结构与 booleamu 解析逻辑）。

    返回 (content, message_id, references)。message_id 取 event:ready 的
    response_message_id（数字），作为下一轮 parent_message_id。
    references 为搜索引用 [{url, title, cite_index}]。
    """
    content = ""
    message_id: Optional[int] = None
    references: list = []
    saw_search = False            # 出现过 SEARCH 片段（后续 results 归属它）
    last_append_field = "content"  # 简化 chunk {"v":...} 的追加目标
    buffer = ""
    current_event = ""

    async for raw in stream:
        try:
            chunk_str = raw.decode("utf-8", "ignore")
        except Exception:
            continue
        buffer += chunk_str
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if line.startswith("event: "):
                current_event = line[7:].strip()
                continue
            if not line or not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if payload in ("[DONE]", "{}"):
                continue
            try:
                chunk = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                continue

            # event: ready -> 拿 response_message_id
            if current_event == "ready" and isinstance(chunk, dict):
                mid = chunk.get("response_message_id")
                if isinstance(mid, int):
                    message_id = mid
                current_event = ""
                continue

            if not isinstance(chunk, dict):
                continue

            # 完整数据块 {"v":{"response":{...,"fragments":[...]}}}
            v = chunk.get("v")
            if isinstance(v, dict) and isinstance(v.get("response"), dict):
                resp = v["response"]
                mid = resp.get("message_id")
                if isinstance(mid, int):
                    message_id = mid
                frags = resp.get("fragments") or []
                for frag in frags:
                    ftype = frag.get("type")
                    if ftype == "RESPONSE" and frag.get("content"):
                        content += frag["content"]
                        last_append_field = "content"
                    elif ftype == "THINKING" and frag.get("content"):
                        last_append_field = "thinking"
                    elif ftype == "SEARCH":
                        saw_search = True
                if resp.get("search_triggered") is True:
                    saw_search = True
                continue

            # 内容追加 {"p":"response/fragments/-1/content","o":"APPEND","v":"文本"}
            p = chunk.get("p")
            o = chunk.get("o")
            if isinstance(p, str) and "content" in p and o == "APPEND":
                if isinstance(chunk.get("v"), str):
                    content += chunk["v"]
                    last_append_field = "content"
                continue
            if isinstance(p, str) and "thinking" in p and o == "APPEND":
                last_append_field = "thinking"
                continue

            # 搜索引用结果 {"p":"response/fragments/-1/results","v":[{url,title,...}]}
            if isinstance(p, str) and p.endswith("/results") and isinstance(chunk.get("v"), list):
                for item in chunk["v"]:
                    if isinstance(item, dict) and item.get("url"):
                        references.append({
                            "url": item.get("url", ""),
                            "title": item.get("title") or "",
                            "cite_index": item.get("cite_index"),
                        })
                continue

            # BATCH / status / token 用量等：清空追加目标
            if (p == "response" and o == "BATCH") or p in ("response/status",
                                                            "response/accumulated_token_usage"):
                last_append_field = ""
                continue

            # 简化 chunk {"v":"文本"}（无 p/o）
            if "v" in chunk and "p" not in chunk and "o" not in chunk \
                    and isinstance(chunk.get("v"), str):
                if last_append_field == "thinking":
                    continue
                content += chunk["v"]

            # 兼容 OpenAI 格式 delta（兜底）
            if chunk.get("choices"):
                delta = chunk["choices"][0].get("delta") or {}
                if delta.get("content"):
                    content += delta["content"]
    # end async for

    content = content.strip()
    if not content:
        raise RuntimeError("C 端 DeepSeek 会话返回空内容")
    return content, message_id, references


def _format_reply(content: str, references: list) -> str:
    """把搜索引用（联网结果）附到正文末尾，便于上层/用户直接看到实时来源"""
    refs = [r for r in references if r.get("url")]
    if not refs:
        return content
    lines = []
    for i, r in enumerate(refs[:5], 1):
        title = (r.get("title") or "").strip()
        url = r["url"]
        lines.append(f"[{i}] {title} - {url}" if title else f"[{i}] {url}")
    return content + "\n\n参考链接：\n" + "\n".join(lines)


# ========== 对外接口 ==========
async def ask_web_deepseek(prompt: str, user_id: str = None, timeout_seconds: float = None) -> str:
    """
    向 C 端 DeepSeek 网页版会话发送消息并返回最终文本。

    参数:
        prompt: 用户消息（ai.py 已注入 OCR 前缀等上下文）
        user_id: 会话隔离键（可选）；传入时按用户维护独立会话续接上下文
        timeout_seconds: 覆盖 models.json 中的 timeout

    返回:
        助手的最终回复文本（联网命中时末尾附参考链接；此通道无思考链）
    异常:
        RuntimeError: 未配置 Token / 初始化失败 / 超时 / 调用失败

    结构化日志：每次调用记 call/ok 或 call/fail；初始化记 init/ok|fail；
    异常重启记 restart/ok|fail（重试成功时保留首次失败记录并追加 call/ok）。
    返回值与异常语义与旧版（MCP）一致。
    """
    if not prompt or not str(prompt).strip():
        return ""
    t0 = time.monotonic()
    user = str(user_id) if user_id is not None else ""
    cfg = _get_config() or {}
    timeout = float(timeout_seconds or cfg.get("timeout", DEFAULT_TIMEOUT))
    conversation_memory = bool(cfg.get("conversation_memory", True))

    try:
        await _ensure_initialized()
    except Exception as e:
        _log_event("call", user, "fail", time.monotonic() - t0, _reason_of(e))
        raise

    session = _http_session
    if session is None or session.closed:
        raise RuntimeError("C 端 DeepSeek 通道已关闭")

    try:
        reply = await _do_call(session, user, str(prompt), timeout, conversation_memory)
    except Exception as e:
        _log_event("call", user, "fail", time.monotonic() - t0, _reason_of(e))
        raise
    _log_event("call", user, "ok", time.monotonic() - t0)
    return reply


async def _do_call(session, user: str, prompt: str, timeout: float,
                   conversation_memory: bool) -> str:
    """单次（含失败重试一次）对话编排：取/建会话 -> 发送 -> 更新会话状态"""
    use_memory = bool(conversation_memory and user)
    state = _sessions.get(user) if use_memory else None

    try:
        return await _do_call_once(session, state, user, prompt, timeout, use_memory)
    except asyncio.TimeoutError:
        # 超时：不重试（与旧版一致），丢弃当前会话状态
        if state is not None and use_memory:
            _sessions.pop(user, None)
        raise RuntimeError(f"C 端 DeepSeek 会话超时（{timeout}s）")
    except Exception:
        # 可重试错误：重建会话重试一次（restart 事件记录）
        if state is not None and use_memory:
            _sessions.pop(user, None)
        state = None
        try:
            reply = await _do_call_once(session, None, user, prompt, timeout, use_memory)
            _log_event("restart", user, "ok", 0.0)
            return reply
        except Exception as e2:
            _log_event("restart", user, "fail", 0.0, "restart_fail")
            raise RuntimeError(f"C 端 DeepSeek 会话调用失败: {e2}") from e2


async def _do_call_once(session, state, user: str, prompt: str, timeout: float,
                        use_memory: bool) -> str:
    """单次发送：无 state 时先建会话；成功后更新 parent_message_id"""
    if state is None:
        sid = await _create_session(session)
        state = {"session_id": sid, "parent_message_id": None}
    content, message_id, references = await _chat_once(
        session, state["session_id"], state["parent_message_id"], prompt, timeout)
    if message_id is not None:
        state["parent_message_id"] = message_id
    if use_memory and user:
        _sessions[user] = state
    return _format_reply(content, references)


async def close() -> None:
    """彻底关闭 C 端 DeepSeek 通道：关闭复用连接，清理会话状态。

    无子进程残留（Node PoW 求解为每次调用短生命进程，调用期间已等待退出）。
    """
    global _http_session, _initialized, _sessions
    if _http_session is not None:
        try:
            await _http_session.close()
        except Exception as e:
            logger.warning(f"[deepseek_web_integration] 关闭 HTTP 连接时出错: {e}")
    _http_session = None
    _initialized = False
    _sessions = {}
    logger.info("[deepseek_web_integration] C 端 DeepSeek 通道已关闭")
