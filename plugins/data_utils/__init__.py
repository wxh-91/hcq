# -*- coding: utf-8 -*-
"""Data Utils 插件（Plugin 8.1）—— 数据处理与网络交互原子工具箱 + 本地鉴权保险库。

对外 AUP 工具：
  原子：
    http_request(url, method, headers, json_body) —— aiohttp 请求，JSON 自动格式化，超长截断 5000
    regex_extract(text, pattern)                   —— 正则提取全部匹配列表
    text_diff(text_a, text_b)                      —— difflib 文本差异，标记增/删/改行
    json_query(filepath, json_path)                —— 读本地 JSON，按路径表达式取字段
    auth_http_request(url, method, auth_profile, json_body)
        —— 带本地凭证的 HTTP 请求：从 secrets/{auth_profile}_headers.json 读取鉴权头
           拼接后发送；凭证只在本地使用绝不回显；失败只返回 HTTP 状态码
  宏：
    auth_playwright_fetch(url, auth_profile)       —— 无头浏览器注入登录态抓取页面正文
"""
from __future__ import annotations
import os
_ROOT = os.environ.get("PROJECT_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import difflib
import json
import logging
import os
import re
import subprocess
import time

import asyncio
import aiohttp

from aup_core import aup_tool, AUPError
from urllib.parse import urlsplit


logger = logging.getLogger("plugins.data_utils")
MAX_RESP = 5000  # 返回截断上限
SECRETS_DIR = _ROOT + "/secrets"


def _clip(text: str, limit: int = MAX_RESP) -> str:
    """截断超长输出，防止上下文爆炸"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（输出过长已截断，省略 {len(text) - limit} 字）…"


def _redact(text: str, creds: dict) -> str:
    """凭证净化：返回文本中出现任一凭证值 → 替换为 [REDACTED]，双保险防泄漏"""
    if not creds:
        return text
    out = text
    for v in creds.values():
        if isinstance(v, str) and len(v) >= 4 and v in out:
            out = out.replace(v, "[REDACTED]")
    return out


def _parse_headers(headers) -> dict:
    """把模型传入的 headers 归一化为 dict。

    兼容三种形态：
      1. dict 原样返回；
      2. JSON 字符串（'{"User-Agent": "..."}'）-> json.loads；
      3. 原始文本头（'User-Agent: xxx\nReferer: yyy'）-> 逐行按 'Key: Value' 解析。
    解析失败返回空 dict，避免类型不符把整个请求拦在校验层。
    """
    if headers is None:
        return {}
    if isinstance(headers, dict):
        return headers
    if isinstance(headers, str):
        t = headers.strip()
        if not t:
            return {}
        if t.startswith("{"):
            try:
                obj = json.loads(t)
                if isinstance(obj, dict):
                    return {str(k): str(v) for k, v in obj.items()}
            except (json.JSONDecodeError, ValueError):
                pass
        out = {}
        for line in t.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if k:
                out[k] = v
        return out
    return {}


def _load_headers(profile: str):
    """读 secrets/{profile}_headers.json；返回 (headers_dict 或 None, 错误信息)"""
    p = os.path.join(SECRETS_DIR, f"{profile}_headers.json")
    if not os.path.exists(p):
        return None, "凭证不存在：secrets/%s_headers.json 未找到（可向主人索要该凭证）" % profile
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f), ""
    except json.JSONDecodeError as e:
        return None, "凭证解析失败（%s）" % e
    except Exception as e:
        return None, "凭证读取失败：%s" % type(e).__name__


@aup_tool(
    name="http_request",
    description="[原子] HTTP 请求：向外部 API 发送 GET/POST 等请求。返回 JSON 时自动格式化缩进；返回过长自动截断到 5000 字。适用于获取网页/API 数据。",
    category="network",
    is_danger=False,
    params_schema={
        "required": ["url"],
        "properties": {
            "url": {"type": "string", "description": "请求 URL"},
            "method": {"type": "string", "description": "HTTP 方法：GET/POST/PUT/DELETE，默认 GET"},
            "headers": {"type": "any", "description": "请求头字典（可选），如 {\"Authorization\": \"Bearer xxx\"}"},
            "json_body": {"type": "object", "description": "JSON 请求体字典（POST 时可选）"},
        },
    },
    suggestion_hint="请求失败：检查 URL 可达性、参数与网络",
)
async def http_request(url: str, method: str = "GET", headers: dict = None, json_body: dict = None) -> str:
    method = (method or "GET").upper()
    headers = _parse_headers(headers)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.request(
                method,
                url,
                headers=headers or None,
                json=json_body if method in ("POST", "PUT", "PATCH") else None,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                raw = await r.text(encoding="utf-8", errors="replace")
                status = r.status
        if not raw:
            return "HTTP %d：响应为空" % status
        try:
            obj = json.loads(raw)
            body = json.dumps(obj, ensure_ascii=False, indent=2)
        except Exception:
            body = raw
        return "HTTP %d\n%s" % (status, _clip(body))
    except Exception as e:
        return "请求失败：%s" % type(e).__name__


@aup_tool(
    name="regex_extract",
    description="[原子] 正则提取：从大段文本中用正则表达式提取所有匹配结果，返回匹配列表。没有匹配时返回明确提示。",
    category="text",
    is_danger=False,
    params_schema={
        "required": ["text", "pattern"],
        "properties": {
            "text": {"type": "string", "description": "目标文本"},
            "pattern": {"type": "string", "description": "正则表达式，如 r'\\d+' 提取所有数字"},
        },
    },
    suggestion_hint="无匹配：检查正则写法或换用更宽松的表达式",
)
async def regex_extract(text: str, pattern: str) -> str:
    if not text:
        return "提取结果：文本为空"
    try:
        matches = re.findall(pattern, text)
    except re.error as e:
        return "正则错误：%s" % e
    if not matches:
        return "未匹配到任何结果（pattern=%s）" % pattern[:100]
    shown = []
    for m in matches[:50]:
        if isinstance(m, tuple):
            shown.append("(" + ", ".join(str(x) for x in m) + ")")
        else:
            shown.append(str(m))
    extra = "，共 %d 个匹配" % len(matches) if len(matches) > 50 else ""
    return "匹配到 %d 个结果%s：\n%s" % (len(matches), extra, "\n".join(shown))


@aup_tool(
    name="text_diff",
    description="[原子] 文本差异对比：对比两段文本，返回增/删/改行的差异结果（+ 为新增行，- 为删除行，! 为修改行）。适用于版本对比、代码审查。",
    category="text",
    is_danger=False,
    params_schema={
        "required": ["text_a", "text_b"],
        "properties": {
            "text_a": {"type": "string", "description": "对比基准文本"},
            "text_b": {"type": "string", "description": "对比目标文本"},
        },
    },
    suggestion_hint="差异为空：两段文本完全一致",
)
async def text_diff(text_a: str, text_b: str) -> str:
    if text_a == text_b:
        return "两段文本完全一致，无差异"
    lines_a = text_a.splitlines()
    lines_b = text_b.splitlines()
    diff = difflib.ndiff(lines_a, lines_b)
    out = []
    for line in diff:
        code = line[:1]
        body = line[1:].strip()
        if code == "+":
            out.append("[新增] " + body)
        elif code == "-":
            out.append("[删除] " + body)
        elif code == "?":
            continue
        else:
            out.append("       " + body)
    if not out:
        return "差异为空"
    return "差异结果（%d 行）：\n%s" % (len(out), "\n".join(out[:80]))


def _navigate(obj, path: str):
    """按路径表达式导航 JSON：data.items[0].name / items.0.name 两种写法都支持"""
    tokens = re.findall(r"[^.\[\]]+|\[\d+\]", path)
    cur = obj
    for tok in tokens:
        if tok.startswith("["):
            cur = cur[int(tok[1:-1])]
        elif tok.isdigit():
            cur = cur[int(tok)]
        else:
            cur = cur[tok]
    return cur


@aup_tool(
    name="json_query",
    description="[原子] JSON 查询：读取本地 JSON 文件，按路径表达式（如 data.items[0].name）提取指定字段，只返回目标值，防止全文进上下文。",
    category="file",
    is_danger=False,
    params_schema={
        "required": ["filepath", "json_path"],
        "properties": {
            "filepath": {"type": "string", "description": "本地 JSON 文件绝对路径"},
            "json_path": {"type": "string", "description": "字段路径，如 data.items[0].name 或 items.0.name"},
        },
    },
    suggestion_hint="查询失败：检查文件路径、JSON 合法性或路径表达式",
)
async def json_query(filepath: str, json_path: str) -> str:
    try:
        if not os.path.exists(filepath):
            return "文件不存在：%s" % filepath
        with open(filepath, encoding="utf-8") as f:
            obj = json.load(f)
    except json.JSONDecodeError as e:
        return "JSON 解析失败：%s" % e
    except Exception as e:
        return "读取失败：%s" % type(e).__name__
    try:
        val = _navigate(obj, json_path)
    except (KeyError, IndexError, TypeError) as e:
        return "路径未命中（%s）：%s" % (json_path, e)
    if isinstance(val, (dict, list)):
        return _clip(json.dumps(val, ensure_ascii=False, indent=2))
    return str(val)


# ============ Local Auth Vault：本地鉴权保险库 ============


@aup_tool(
    name="auth_http_request",
    description="[原子] 带凭证的 HTTP 请求：从本地 secrets/{auth_profile}_headers.json 读取鉴权请求头（Cookie/Authorization 等）并拼接发送。凭证只在本地使用，绝不回显给 AI；请求失败只返回 HTTP 状态码。适用于需要登录态的 API/网页。",
    category="network",
    is_danger=False,
    params_schema={
        "required": ["url", "auth_profile"],
        "properties": {
            "url": {"type": "string", "description": "目标 URL"},
            "method": {"type": "string", "description": "HTTP 方法，默认 GET"},
            "auth_profile": {"type": "string", "description": "凭证名（如 github），对应 secrets/github_headers.json"},
            "json_body": {"type": "object", "description": "JSON 请求体字典（POST 时可选）"},
        },
    },
    suggestion_hint="请求失败：检查凭证是否配置（secrets/{profile}_headers.json）、URL 是否可达",
)
async def auth_http_request(url: str, method: str = "GET", auth_profile: str = "", json_body: dict = None) -> str:
    headers, err = _load_headers(auth_profile)
    if err:
        return err
    if not isinstance(headers, dict) or not headers:
        return "凭证内容为空：secrets/%s_headers.json 需为包含请求头键值对的 JSON" % auth_profile
    method = (method or "GET").upper()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.request(
                method,
                url,
                headers=headers,
                json=json_body if method in ("POST", "PUT", "PATCH") else None,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                raw = await r.text(encoding="utf-8", errors="replace")
                status = r.status
    except Exception as e:
        return "请求失败：%s" % type(e).__name__
    if status >= 400:
        # 失败只返回 HTTP 状态码：凭证错误时响应体可能回显敏感信息，一律不渲染
        return "HTTP %d" % status
    if not raw:
        return "HTTP %d：响应为空" % status
    try:
        obj = json.loads(raw)
        body = json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        body = raw
    return "HTTP %d\n%s" % (status, _redact(_clip(body), headers))




@aup_tool(
    name="auth_playwright_fetch",
    description="遇到任何登录或凭证捕获需求，必须调用 auth_playwright_fetch(url=目标URL, profile_name=凭证名, mode=login)（存凭证/重新登录必须 mode=login 强制弹窗扫码）；仅抓取已有凭证内容用 mode=fetch（默认）。没有任何例外。[原子] 通用带凭证网页抓取/登录（与站点无关，底层 win_auth_runner.py）：传入 url 与 profile_name，自动带上对应登录态（C:/Users/Public/cred_states/{profile_name}_state.json）静默抓取页面正文；若该凭证尚未保存，自动在 Windows 桌面弹出 Edge 登录窗口等待主人扫码，扫码完成后再请求即静默抓取。凭证本地使用绝不回显。返回结果含 logged_in: true/false 标记。",
    category="network",
    is_danger=False,
    params_schema={
        "required": ["url", "profile_name"],
        "properties": {
            "url": {"type": "string", "description": "目标页面 URL（http/https）"},
            "profile_name": {"type": "string", "description": "凭证名（如 pdd），对应 {profile_name}_state.json；未保存时自动弹窗扫码"},
            "mode": {"type": "string", "description": "fetch=静默抓取（默认）；login=强制在 Windows 桌面弹出登录窗等待扫码（存凭证/重新登录场景必用，杜绝匿名首页假阳性）"},
        },
    },
    suggestion_hint="抓取失败：检查登录态文件、目标页可达性与浏览器依赖",
)
def auth_playwright_fetch(url: str, profile_name: str = "", mode: str = "fetch") -> str:
    if not profile_name:
        return "缺少凭证名 profile_name"
    return _run_win_auth(mode, url, profile_name)


# [Auth Precision] 凭证名别名归一化：AI/LLM 可能猜全称（pinduoduo/taobao/meituan），
# 凭证文件实际用短名（pdd）。统一在此归一化，避免用错 profile → 无凭证 → 假阳性重登。
_PROFILE_ALIASES = {
    "pinduoduo": "pdd",
    "pdd": "pdd",
    "taobao": "taobao",
    "tmall": "tmall",
    "jingdong": "jd",
    "jd": "jd",
    "meituan": "meituan",
    "xianyu": "xianyu",
}


def _run_win_auth(mode: str, url: str, profile: str) -> str:
    """调 Windows 宿主侧通用凭证脚本（auth_profile_tool.py），结果转文本。

    [通用化] 登录窗口必须在 Windows 桌面弹出（WSL 无浏览器二进制）；
    storage_state 统一存 Windows 侧 C:/Users/Public/cred_states\{profile}_state.json。
    """
    profile = _PROFILE_ALIASES.get(profile, profile)
    win_py = "/mnt/c/Users/Admin（无密码）/AppData/Local/Programs/Python/Python314/python.exe"
    script = r"C:\Users\Public\win_auth_runner.py"
    # 兼容旧版：WSL secrets 里已存的 state 先同步到 Windows 侧
    if mode == "fetch":
        _old = os.path.join(SECRETS_DIR, f"{profile}_state.json")
        _new_dir = "/mnt/c/Users/Public/cred_states"
        if os.path.exists(_old) and os.path.isdir(_new_dir):
            try:
                shutil.copy(_old, os.path.join(_new_dir, f"{profile}_state.json"))
            except Exception:
                pass
    try:
        proc = subprocess.run(
            [win_py, script, mode, url, profile],
            capture_output=True, text=True, encoding="utf-8", timeout=1800,
        )
    except subprocess.TimeoutExpired:
        return "凭证操作超时（180s）：登录窗口可能已打开但未完成扫码，请再次请求"
    except Exception as e:
        return "凭证脚本调用失败：%s" % type(e).__name__
    out = (proc.stdout or "").strip().splitlines()
    if not out:
        return "凭证脚本无输出（rc=%s）：%s" % (proc.returncode, (proc.stderr or "")[:200])
    try:
        res = json.loads(out[-1])
    except Exception:
        return "凭证脚本输出异常：%s" % out[-1][:200]
    status = res.get("status", "unknown")
    logged_in = res.get("logged_in", "unknown")
    reason = res.get("reason", "")
    if status == "error" and reason == "need_login":
        # [契约] 业务失败：未登录/弹窗超时 → AUPResult(status=error, error_code=NEED_LOGIN)
        raise AUPError("NEED_LOGIN", res.get("message", "需扫码登录"),
                       suggestion="登录窗口已弹出或会话已保存，等待主人扫码后再次请求抓取/验证")
    if status == "need_login":
        # 兼容旧版返回（status=need_login 未加 reason）
        raise AUPError("NEED_LOGIN", res.get("message", "需扫码登录"),
                       suggestion="登录窗口已弹出或会话已保存，等待主人扫码后再次请求抓取/验证")
    if status == "error":
        # [Tool Boundary] 通用业务错误：reason 直接映射为 error_code（如 empty_page → EMPTY_PAGE）
        _code = reason.upper() if reason else "TOOL_ERROR"
        raise AUPError(_code, res.get("message", "工具执行失败"),
                       suggestion="请根据错误信息修正后重试（若为登录/反爬类问题请先完成授权）")
    if status == "ok":
        return "logged_in: %s\n抓取成功（profile=%s）：\n%s" % (logged_in, res.get("profile", profile), res.get("body", ""))
    if status == "saved":
        if str(logged_in).lower() != "true":
            raise AUPError("NEED_LOGIN", res.get("message", "需扫码登录"),
                           suggestion="等待主人扫码后再次请求抓取/验证")
        return "logged_in: %s\n%s（%s）" % (logged_in, res.get("message", "凭证已保存"), res.get("path") or res.get("storage_state", ""))
    # [Anti-Hallucination] 非成功状态（save_failed/fetch_failed/launch_failed 等）一律转业务失败，
    # 禁止把失败渲染成 "✅ 执行成功" 让 LLM 顺杆爬。reason 优先，否则用 status 大写。
    _code = reason.upper() if reason else status.upper()
    raise AUPError(_code, res.get("message", "凭证操作失败"),
                   suggestion="请稍后重试；若为登录/验证，请先完成扫码。")


# ===== [Web Security Audit] 轻量非破坏性安全巡检原子工具 =====
# 红线（代码层强制，不可由 LLM 解除）：
#   1) 串行请求，每次间隔 >= 1 秒（_SA_INTERVAL）
#   2) 单次巡检总请求数 <= 200（_SA_REQ_LIMIT）
#   3) 任何 5xx / 网络异常连续出现 >= 2 次立即熔断（_SA_BREAK_N）
_SA_REQ_LIMIT = 200
_SA_INTERVAL = 1.0
_SA_TIMEOUT = 12
_SA_BREAK_N = 2

_SA_SECURITY_HEADERS = [
    ("content-security-policy", "Content-Security-Policy"),
    ("x-frame-options", "X-Frame-Options"),
    ("x-content-type-options", "X-Content-Type-Options"),
    ("strict-transport-security", "Strict-Transport-Security"),
    ("referrer-policy", "Referrer-Policy"),
    ("permissions-policy", "Permissions-Policy"),
    ("cross-origin-opener-policy", "Cross-Origin-Opener-Policy"),
]

_SA_LEAK_PATHS = [
    "/robots.txt", "/.git/config", "/.git/HEAD", "/.env", "/.env.bak",
    "/backup.zip", "/backup.bak", "/config.php.bak", "/db.sql", "/dump.sql",
    "/data.zip", "/swagger.json", "/openapi.json", "/swagger-ui.html",
    "/phpinfo.php", "/test.php", "/.DS_Store", "/web.config", "/config.json",
    "/appsettings.json", "/wp-config.php.bak", "/id_rsa", "/.ssh/id_rsa",
]

_SA_DIR_WORDS = [
    "admin", "login", "api", "api/v1", "backup", "config", "dev", "test",
    "docs", "upload", "uploads", "static", "assets", "register", "users",
    "console", "debug", "logs", "data", "db", "phpmyadmin", "wp-admin",
    "cgi-bin", "private", "secret", "internal", "status", "health",
    "version", "search", "cart", "checkout", "account", "profile",
    "settings", "flag", "source", "git", "archive",
]

_SA_SQL_ERR_MARKERS = [
    "syntax error", "sqlstate", "unclosed quotation", "mysql_", "ora-",
    "postgresql", "sqlite", "odbc", "microsoft ole db",
    "you have an error in your sql", "supplied argument is not a valid mysql",
    "pg_query", "mariadb", "oracle",
]

_SA_SOFT404_MARKERS = ["404", "not found", "页面不存在", "不存在", "找不到", "file not found"]



@aup_tool(
    name="web_security_audit",
    description="[Web 安全巡检专用] 当主人要求检测/检查某个网站是否安全、有没有漏洞/泄露/配置问题时调用。非破坏性巡检：检查信息泄露路径（robots.txt/.git/备份文件/常见 API 配置）、HTTP 安全头与 CORS 配置、对目标做极轻量 XSS/SQLi 反射探测（发现回显或报错立即停止，绝不深入脱库）、轻量目录扫描。强制红线（代码层）：串行请求且每次间隔≥1 秒；单次巡检总请求数≤200；任何 5xx 或异常连续出现立即熔断停止。只收集信息并生成报告，绝不执行破坏性操作（DDoS/提权/文件上传/脱库）。参数：url(目标站点 URL，必填)。",
    category="network",
    is_danger=False,
    params_schema={
        "required": ["url"],
        "properties": {"url": {"type": "string", "description": "目标站点 URL（http/https），如 https://example.com"}},
    },
    suggestion_hint="巡检完成：按报告分级处理；若目标不可达，检查 URL 与网络",
)

async def web_security_audit(url: str) -> str:
    """[Web 安全巡检专用] 当主人要求检测/检查某个网站是否安全、有没有漏洞/泄露/配置问题时调用。
    非破坏性巡检：信息泄露路径（robots.txt/.git/备份文件/常见配置）、HTTP 安全头与 CORS 配置、
    对目标做极轻量 XSS/SQLi 反射探测（发现回显或报错立即停止，绝不深入）、轻量目录扫描。
    强制红线（代码层）：串行请求且间隔≥1 秒；单次总请求≤200；5xx/异常连续出现立即熔断。
    只收集信息并生成报告，绝不执行破坏性操作（DDoS/提权/文件上传/脱库）。"""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return json.dumps({"status": "param_error", "message": "url 必须以 http:// 或 https:// 开头"}, ensure_ascii=False)
    try:
        _sp = urlsplit(url)
        _origin = "%s://%s" % (_sp.scheme, _sp.netloc)
    except Exception as _e:
        return json.dumps({"status": "param_error", "message": "URL 解析失败: %s" % str(_e)[:120]}, ensure_ascii=False)
    _base = _origin + "/"
    _target = url if _sp.path and _sp.path != "/" else _base

    _findings = []          # [{category, severity, url, detail}]
    _state = {"count": 0, "errors": 0, "broken": False}

    async def _get(session, req_url, extra_headers=None, allow_broken=False):
        """串行 GET：计数 + 限速 + 熔断。返回 (status, headers, text)。"""
        if _state["broken"] and not allow_broken:
            return (None, {}, "")
        _state["count"] += 1
        if _state["count"] > _SA_REQ_LIMIT:
            _state["broken"] = True
            return (None, {}, "")
        await asyncio.sleep(_SA_INTERVAL)
        try:
            async with session.get(req_url, headers=extra_headers or {}, timeout=aiohttp.ClientTimeout(total=_SA_TIMEOUT), ssl=False) as resp:
                _txt = ""
                try:
                    _txt = (await resp.text(errors="ignore"))[:4000]
                except Exception:
                    pass
                if resp.status >= 500:
                    _state["errors"] += 1
                    if _state["errors"] >= _SA_BREAK_N:
                        _state["broken"] = True
                else:
                    _state["errors"] = 0
                return (resp.status, dict(resp.headers), _txt)
        except Exception as _e:
            _state["errors"] += 1
            if _state["errors"] >= _SA_BREAK_N:
                _state["broken"] = True
            return (None, {}, "")

    def _is_soft404(status, text):
        if status != 200:
            return False
        return any(m in (text or "").lower() for m in _SA_SOFT404_MARKERS)

    async with aiohttp.ClientSession() as _sess:
        # ---------- A. 信息泄露路径（相对站点根） ----------
        for _p in _SA_LEAK_PATHS:
            if _state["broken"]:
                break
            _u = _base + _p.lstrip("/")
            _st, _hd, _tx = await _get(_sess, _u)
            if _st is None:
                continue
            if _st == 200 and not _is_soft404(_st, _tx):
                _low = (_tx or "").lower()
                if ".git/config" in _p and "repositoryformatversion" in _low:
                    _sev, _detail = "high", "敏感文件可访问：暴露 .git/config（版本库元数据，可能泄露源码/提交历史）"
                elif "robots.txt" in _p:
                    _lines = [l for l in (_tx or "").splitlines() if l.strip() and not l.startswith("#")][:10]
                    _sev, _detail = "info", "robots.txt 可访问：%s" % ("；".join(_lines) if _lines else "（内容为空）")
                elif ".env" in _p or "config.json" in _p or "appsettings.json" in _p or "wp-config" in _p:
                    _sev, _detail = "high", "疑似配置文件泄露（%s），可能包含密钥/数据库口令" % _p
                elif _p.endswith((".zip", ".bak", ".sql", ".dump", "id_rsa")) or ".ssh" in _p:
                    _sev, _detail = "high", "疑似敏感备份/密钥文件可下载（%s）" % _p
                elif "swagger" in _p or "openapi" in _p:
                    _sev, _detail = "medium", "API 文档端点暴露（%s），可能泄露接口结构与参数" % _p
                elif "phpinfo" in _p or "test.php" in _p:
                    _sev, _detail = "medium", "信息探测脚本可访问（%s），可能暴露运行环境细节" % _p
                else:
                    _sev, _detail = "medium", "疑似敏感路径可访问（%s），响应 %d 字节" % (_p, len(_tx or ""))
                _findings.append({"category": "info_leak", "severity": _sev, "url": _u, "detail": _detail})

        # ---------- B. HTTP 安全头（对目标路径） ----------
        _st, _hd, _tx = await _get(_sess, _target)
        if _st is not None and not _state["broken"]:
            _miss = []
            for _hk, _hn in _SA_SECURITY_HEADERS:
                if _hk not in {k.lower() for k in _hd}:
                    _miss.append(_hn)
            if _miss:
                _findings.append({
                    "category": "security_headers",
                    "severity": "medium",
                    "url": _target,
                    "detail": "缺失安全响应头：%s（建议补充 CSP / X-Frame-Options / HSTS 等）" % "、".join(_miss),
                })
            else:
                _findings.append({"category": "security_headers", "severity": "ok", "url": _target, "detail": "主要安全响应头齐全"})

            # ---------- C. CORS 配置（反射/通配检测） ----------
            _cors_st, _cors_hd, _cors_tx = await _get(_sess, _target, extra_headers={"Origin": "https://evil.example"})
            if _cors_st is not None:
                _acao = (_cors_hd or {}).get("Access-Control-Allow-Origin", "")
                _acac = (_cors_hd or {}).get("Access-Control-Allow-Credentials", "")
                if _acao == "*" or (_acao == "https://evil.example" and str(_acac).lower() == "true"):
                    _findings.append({
                        "category": "cors",
                        "severity": "high" if _acao == "https://evil.example" else "medium",
                        "url": _target,
                        "detail": "CORS 配置风险：ACAO=%r，凭据放行=%r（任意站点可跨域读取）" % (_acao, _acac),
                    })
                elif _acao == "https://evil.example":
                    _findings.append({"category": "cors", "severity": "low", "url": _target, "detail": "CORS 反射任意 Origin（未放行凭据）"})

        # ---------- D. 极轻量注入反射探测（发现即停止，绝不深入） ----------
        if not _state["broken"]:
            _base_q = _target
            if _sp.query:
                _base_q = _target + ("&" if "?" in _target else "?")
            else:
                _base_q = _target + ("&" if "?" in _target else "?") + "q="
            _sqli_probes = ["%27", "%27%20OR%20%271%27%3D%271"]
            _xss_probes = ["%3Cscript%3Ealert(1)%3C%2Fscript%3E", "%22%3E%3Csvg%2Fonload%3Dalert(1)%3E"]
            _inj_done = False
            for _payload in _sqli_probes:
                if _state["broken"] or _inj_done:
                    break
                _u = _base_q + _payload
                _st, _hd, _tx = await _get(_sess, _u)
                if _st is None:
                    continue
                _low = (_tx or "").lower()
                if any(m in _low for m in _SA_SQL_ERR_MARKERS):
                    _findings.append({"category": "injection", "severity": "high", "url": _u, "detail": "SQLi 反射迹象：响应出现数据库报错特征（%s），已停止深入" % next((m for m in _SA_SQL_ERR_MARKERS if m in _low), "?")})
                    _inj_done = True
            for _payload in _xss_probes:
                if _state["broken"] or _inj_done:
                    break
                _u = _base_q + _payload
                _st, _hd, _tx = await _get(_sess, _u)
                if _st is None:
                    continue
                if "<script>alert(1)</script>" in (_tx or "") or "<svg/onload=alert(1)>" in (_tx or ""):
                    _findings.append({"category": "injection", "severity": "medium", "url": _u, "detail": "XSS 反射迹象：payload 原样回显且未转义，已停止深入"})
                    _inj_done = True
            if not _inj_done and not _state["broken"]:
                _findings.append({"category": "injection", "severity": "ok", "url": _target, "detail": "极轻量注入反射探测未发现回显/报错"})

        # ---------- E. 轻量目录扫描（相对站点根） ----------
        _found_dirs = []
        for _w in _SA_DIR_WORDS:
            if _state["broken"]:
                break
            _u = _base + _w.lstrip("/")
            _st, _hd, _tx = await _get(_sess, _u)
            if _st is None:
                continue
            if _st in (200, 301, 302, 403):
                _soft = _st == 200 and _is_soft404(_st, _tx)
                if not _soft:
                    _found_dirs.append({"path": "/" + _w.lstrip("/"), "status": _st})
        if _found_dirs:
            _top = ", ".join("%s(%d)" % (d["path"], d["status"]) for d in _found_dirs[:20])
            _findings.append({"category": "dir_scan", "severity": "low", "url": _base, "detail": "扫描发现可访问路径：%s%s" % (_top, "（…）" if len(_found_dirs) > 20 else "")})
        else:
            _findings.append({"category": "dir_scan", "severity": "ok", "url": _base, "detail": "轻量目录扫描未发现额外可访问路径"})

    _findings = [f for f in _findings if f.get("severity") != "ok"]
    _report = {
        "status": "done",
        "target": _target,
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_requests": _state["count"],
        "circuit_broken": _state["broken"],
        "findings": _findings[:60],
        "recommendations": _recommend(_findings),
    }
    return json.dumps(_report, ensure_ascii=False)


def _recommend(findings):
    _cats = {f.get("category") for f in findings}
    _rec = []
    if "info_leak" in _cats:
        _rec.append("删除/禁止访问泄露的敏感文件（.git、.env、备份、配置），并在 Web 服务器层屏蔽点号开头与常见备份扩展名")
    if "security_headers" in _cats:
        _rec.append("补充 Content-Security-Policy、X-Frame-Options、HSTS、X-Content-Type-Options 等安全响应头")
    if "cors" in _cats:
        _rec.append("收紧 CORS：固定允许来源白名单，禁止反射任意 Origin 与通配符，且不随行 Allow-Credentials")
    if "injection" in _cats:
        _rec.append("对输入做参数化查询与输出编码（XSS 转义/SQL 参数绑定），部署 WAF 并禁止把数据库错误回显给客户端")
    if "dir_scan" in _cats:
        _rec.append("收敛/隐藏非必要可访问路径（admin/api/backup 等），对管理后台增加访问控制")
    if not _rec:
        _rec.append("未发现明显高风险项，建议持续保持安全头与访问控制基线")
    return _rec


# ===== [Web Security Audit] 注册入库（AI 工具列表可见） =====
try:
    from tool_center.registry_manager import register_tool as _register_tool_wsa
    _register_tool_wsa(
        "web_security_audit",
        web_security_audit,
        description="[Web 安全巡检专用] 当主人要求检测/检查某个网站是否安全、有没有漏洞/泄露/配置问题时调用。非破坏性巡检：信息泄露路径（robots.txt/.git/备份文件/常见 API 配置）、HTTP 安全头与 CORS 配置、对目标做极轻量 XSS/SQLi 反射探测（发现回显或报错立即停止，绝不深入脱库）、轻量目录扫描。强制红线（代码层）：串行请求且每次间隔≥1 秒；单次巡检总请求数≤200；任何 5xx 或异常连续出现立即熔断停止。只收集信息并生成报告，绝不执行破坏性操作（DDoS/提权/文件上传/脱库）。参数：url(目标站点 URL，必填)。",
        category="network",
    )
    logger.info("[web_security_audit] 已注册入库（SQLite）")
except Exception as _reg_wsa_err:
    logger.warning("[web_security_audit] 注册入库失败: %s", _reg_wsa_err)

