# -*- coding: utf-8 -*-
"""Crawler 插件（Plugin 1.0）—— 爬虫系统物理隔离单元。

对外 AUP 工具（注册于 tool_center.db，module_path=plugins.crawler）：
  - crawl_video_link : 多级备用方案视频直链爬取（robust 漏斗）
  - crawler_probe    : 无头浏览器网络探针
  - crawler_run_node : 通用 Node 执行器（高危）
  - crawler_dom_hook : DOM 暴力嗅探
物理隔离：Node 脚本（probe/run/dom_hook + fetch_by_id/download_video/diag）与
workspace 全部位于本插件目录 crawler_tools/，路径基于 PLUGIN_ROOT 动态计算，
子进程调用不受 cwd 影响（防止 No such file or directory）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from urllib.parse import urlparse, urljoin

import aiohttp

from aup_core import AUPResult, aup_tool
from plugins.core_utils import file_delivery

logger = logging.getLogger("plugins.crawler")

# 插件根目录（绝对路径——子进程调用不受 cwd 影响）
PLUGIN_ROOT = os.path.dirname(os.path.abspath(__file__))
CRAWLER_TOOLS_DIR = os.path.join(PLUGIN_ROOT, "crawler_tools")
WS_DIR = os.path.join(CRAWLER_TOOLS_DIR, "workspace")


# ===== [安全别名映射] KISS：极简字典，拒绝过度设计 =====
_SITE_MAP_PATH = os.path.join(PLUGIN_ROOT, "site_map.json")
try:
    with open(_SITE_MAP_PATH, encoding="utf-8") as _f:
        SITE_MAP = json.load(_f)
except Exception:
    SITE_MAP = {}


def _resolve_site_url(raw) -> str:
    """极简别名解析：'tz 92285' / 'tz#92285' → 字典拼装完整 URL；完整 URL 原样返回。"""
    if not isinstance(raw, str):
        raw = str(raw)
    raw = raw.strip()
    if not raw or raw.startswith(("http://", "https://")):
        return raw
    for sep in (" ", "#"):
        if sep in raw:
            k, _, v = raw.partition(sep)
            k, v = k.strip(), v.strip()
            if k in SITE_MAP and v:
                return SITE_MAP[k] + v
    return raw


_SHORTLINK_HOSTS = {"b23.tv", "b23.ac", "t.bilibili.com", "b23.cc"}
_BILI_HOST_RE = re.compile(r"bilibili\.com|BV[0-9A-Za-z]+|av\d+")


async def resolve_short_url(url: str, max_hops: int = 3) -> str:
    """跟随 b23.tv 等短链 301/302 读 Location 拿真实长链；非短链或异常原样返回。"""
    if not isinstance(url, str) or not url.strip().startswith(("http://", "https://")):
        return url if isinstance(url, str) else str(url)
    cur = url.strip()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}
    hops = 0
    try:
        async with aiohttp.ClientSession(
                headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as sess:
            while hops < max_hops:
                if urlparse(cur).netloc not in _SHORTLINK_HOSTS:
                    break
                loc = ""
                try:
                    async with sess.head(cur, allow_redirects=False) as resp:
                        loc = resp.headers.get("Location") or ""
                        if resp.status in (301, 302, 303, 307, 308) and loc:
                            cur = urljoin(cur, loc)
                            hops += 1
                            continue
                except Exception:
                    loc = ""
                try:
                    async with sess.get(cur, allow_redirects=False, ssl=False) as resp:
                        loc = resp.headers.get("Location") or ""
                        if resp.status in (301, 302, 303, 307, 308) and loc:
                            cur = urljoin(cur, loc)
                            hops += 1
                            continue
                except Exception:
                    pass
                break
    except Exception as e:
        logger.warning("[crawler] 短链解析异常，沿用原链: %s", e)
    return cur


async def _download_bilibili_video(url: str) -> str:
    """B站长链走 yt-dlp 下载 MP4（fetch_by_id.js 仅适配短剧站，不支持 B 站）。"""
    from plugins.core_utils import downloader
    stem = (downloader._bili_api_title(url)
            or downloader._extract_bili_id(url) or "bili_video")
    try:
        dest = await downloader._run_plain_sync(url, "", stem)
    except Exception as e:
        return json.dumps({"success": False, "error": f"B站视频下载失败: {e}"},
                          ensure_ascii=False)
    frec = file_delivery.register_file(dest, name=os.path.basename(dest),
                                      mime="video/mp4")
    payload = {"success": True, "msg": "视频已下载", "path": dest}
    if frec:
        payload["file_id"] = frec["file_id"]
        payload["file_name"] = frec["name"]
        payload["marker"] = file_delivery.marker(frec)
    return json.dumps(payload, ensure_ascii=False)


def _clean_workspace() -> None:
    """清理 workspace（保留 .gitignore），防止旧输出残留干扰判定。"""
    if os.path.isdir(WS_DIR):
        for f in os.listdir(WS_DIR):
            if f != ".gitignore":
                try:
                    os.remove(os.path.join(WS_DIR, f))
                except Exception:
                    pass


async def _run_node(js_path: str, args: list, timeout_ms: int = 60000) -> tuple:
    """调起 Node 脚本（绝对路径），返回 (rc, stdout, stderr)。"""
    proc = await asyncio.create_subprocess_exec(
        "node", js_path, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_ms / 1000)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", "超时（%sms）" % timeout_ms
    return proc.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


@aup_tool(
    name="crawl_video_link",
    description="视频爬取首选唯一入口：传入视频页面 URL 或极简安全别名（如 \"tz 92285\" / \"yt abc\"），内部自动完成别名拼装与 DOM 嗅探→网络探针→视频下载的多级降级漏斗。直接调用本工具即可，不要先调用 crawler_dom_hook / crawler_probe 等其他爬虫工具（它们仅供高级调试）。支持极简安全别名：用户发送如 “tz 92285” 或 “tz#92285” 的简短指令时，直接原样作为 url 参数传入即可，后台自动解析为完整链接；不要向用户询问完整 URL。",
    category="network",
    is_danger=False,
    params_schema={
        "required": ["url"],
        "properties": {"url": {"type": "string", "description": "要爬取的视频页面 URL"}},
    },
    suggestion_hint="视频爬取失败，请确认 URL 可达且页面含视频资源",
)
async def crawl_video_link(url: str) -> str:
    from plugins.crawler.robust_crawler import robust_video_crawl
    url = _resolve_site_url(url)  # [安全别名] tz 92285 / yt abc 自动拼装
    url = await resolve_short_url(url)  # 短链前置跟随 302 取真实长链
    result = await robust_video_crawl(url)
    return json.dumps(result, ensure_ascii=False)


@aup_tool(
    name="crawler_probe",
    description="通用网络探针：使用无头浏览器访问 URL，抓取网络请求和 m3u8 内容",
    category="network",
    is_danger=False,
    params_schema={
        "required": ["url"],
        "properties": {
            "url": {"type": "string", "description": "要探测的 URL"},
            "timeout": {"type": "integer", "description": "超时毫秒，默认 15000"},
        },
    },
    suggestion_hint="网络探针失败，请检查 URL 可达性与目标站点反爬策略",
)
async def crawler_probe(url: str, timeout: int = 15000) -> str:
    _clean_workspace()
    js_path = os.path.join(CRAWLER_TOOLS_DIR, "probe.js")
    try:
        rc, out, err = await _run_node(js_path, [url, str(timeout)], timeout_ms=timeout + 10000)
        if rc != 0:
            return "探针执行失败: %s" % err
        from plugins.core_utils.security_scanner import sanitize_crawler_output
        return sanitize_crawler_output(out.strip()[:8000])
    except Exception as e:
        return "探针调用异常: %s" % e


@aup_tool(
    name="crawler_run_node",
    description="通用 Node 执行器：执行 AI 生成的临时 Node 脚本（带超时控制），结果返回 stdout",
    category="network",
    is_danger=True,
    params_schema={
        "required": ["code"],
        "properties": {"code": {"type": "string", "description": "要执行的 Node 脚本源码"}},
    },
    suggestion_hint="Node 脚本执行失败，请检查脚本语法与依赖",
)
async def crawler_run_node(code: str) -> str:
    js_path = os.path.join(CRAWLER_TOOLS_DIR, "run.js")
    try:
        rc, out, err = await _run_node(js_path, [code], timeout_ms=30000)
        return out.strip()
    except Exception as e:
        return "执行异常: %s" % e


@aup_tool(
    name="crawler_dom_hook",
    description="DOM 暴力嗅探：无头浏览器打开网页，自动尝试点击播放，抓取 video 标签和 m3u8",
    category="network",
    is_danger=False,
    params_schema={
        "required": ["url"],
        "properties": {
            "url": {"type": "string", "description": "要嗅探的 URL"},
            "timeout": {"type": "integer", "description": "超时毫秒，默认 20000"},
        },
    },
    suggestion_hint="DOM 嗅探失败，请确认页面存在 video 标签或 m3u8 流",
)
async def crawler_dom_hook(url: str, timeout: int = 20000) -> str:
    js_path = os.path.join(CRAWLER_TOOLS_DIR, "dom_hook.js")
    try:
        rc, out, err = await _run_node(js_path, [url, str(timeout)], timeout_ms=timeout + 10000)
        if rc != 0:
            return "DOM 探针执行失败: %s" % err
        from plugins.core_utils.security_scanner import sanitize_crawler_output
        return sanitize_crawler_output(out.strip()[:8000])
    except Exception as e:
        return "DOM 探针调用异常: %s" % e


@aup_tool(
    name="crawl_custom_by_id",
    description="针对特定站点（tobvic 短剧/影视模板）的极速定制化解析器（fetch_by_id 遗产链路）。"
                "当通用 crawl_video_link 失败、或目标站点为短剧/影视模板（如 tob.tobvic.com）时，优先尝试此工具。域名含 tob.tobvic.com 的完整 URL 必须直接调用本工具，不要使用 crawl_video_link。"
                "它直接调用站点详情 API 提取 m3u8（已内置 XOR 解密，无需逆向任何加密 JS），响应极快，不走无头浏览器。"
                "注意：依赖 cookies.json（位于插件根目录），无有效 cookie 时可能被 JS 挑战拦截。支持极简安全别名：用户发送如 “tz 92285” 或 “tz#92285” 的简短指令时，直接原样作为 url 参数传入即可，后台自动解析为完整链接；不要向用户询问完整 URL。",
    category="network",
    is_danger=False,
    params_schema={
        "required": ["url"],
        "properties": {
            "url": {"type": "string",
                    "description": "目标视频页 URL（如 https://tob.tobvic.com:52000/video/detail/xxx）"},
        },
    },
    suggestion_hint="定制解析失败：请确认站点 API 域名有效、cookies.json 有效（缺失时需人类补充）",
)
async def crawl_custom_by_id(url: str) -> str:
    """调用 fetch_by_id.js 流式解析：只取 m3u8 行，不等待整段视频下载（读取到即终止子进程）。

    返回 AUPResult 兼容 JSON：{success, title?, id?, error?, missing_cookie?}
    [安全别名] url 支持 'tz 92285' / 'tz#92285' 极简指令，后台自动拼装完整链接。
    [防泄漏] 成功返回不含完整 URL / m3u8 地址（仅本地日志留档），防止聊天记录被平台扫描。
    """
    url = _resolve_site_url(url)
    js_path = os.path.join(PLUGIN_ROOT, "fetch_by_id.js")
    cookie_path = os.path.join(PLUGIN_ROOT, "cookies.json")
    if not os.path.exists(js_path):
        return json.dumps({"success": False, "error": "fetch_by_id.js 不存在（插件文件缺失）"}, ensure_ascii=False)
    missing_cookie = not os.path.exists(cookie_path)
    proc = await asyncio.create_subprocess_exec(
        "node", js_path, url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=PLUGIN_ROOT,  # cookies.json / detail_*.json 为相对路径，必须固定 cwd=插件根
    )
    m3u8, title = None, None
    tail = []
    try:
        while True:
            try:
                line_b = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                break
            if not line_b:
                break
            line = line_b.decode("utf-8", "replace").rstrip()
            tail.append(line)
            if len(tail) > 60:
                tail.pop(0)
            if "m3u8   :" in line:
                v = line.split("m3u8   :", 1)[1].strip()
                if v and v.startswith("http"):
                    m3u8 = v
            elif line.startswith("标题   :"):
                title = line.split(":", 1)[1].strip()
            if m3u8:
                proc.kill()
                break
    finally:
        if proc.returncode is None:
            proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
    if m3u8:
        m = __import__("re").search(r"/video/detail/(\d+)", url)
        video_id = m.group(1) if m else (url if url.isdigit() else "未知")
        logger.info("[crawl_custom_by_id] 解析成功 id=%s m3u8=%s", video_id, m3u8)  # 完整链接仅本地日志留档
        note = "M3U8 提取成功（完整链接已隐藏，日志留档）"
        if missing_cookie:
            note += "；cookies.json 缺失，部分反爬拦截可能失效"
        return json.dumps({
            "success": True, "title": title or "", "id": video_id, "note": note,
        }, ensure_ascii=False)
    err_tail = " | ".join(tail[-15:])
    return json.dumps({
        "success": False,
        "error": "未能解析出 m3u8（站点详情 API 可能失效或被 JS 挑战拦截）",
        "stderr_tail": err_tail[:800],
        "missing_cookie": missing_cookie,
    }, ensure_ascii=False)


# ===== [宏工具] 下载业务闭环：URL/别名 → 一条龙 → 本地 MP4（黑盒，AI 只看最终路径） =====
@aup_tool(
    name="download_video_by_url",
    description="[宏] 下载特定影视站点的视频（依赖 video ID 解析）。仅适用于以 tz 开头的特定站点 URL。"
                "如果是普通的 .mp4 直链或其他不需要 ID 解析的网页，请优先使用 download_m3u8 或 execute_shell 的 curl/wget 下载。",
    category="network",
    is_danger=False,
    params_schema={
        "required": ["url"],
        "properties": {"url": {"type": "string", "description": "视频页面 URL 或极简别名（如 tz 92285）"}},
    },
    suggestion_hint="视频下载失败，请确认链接可达且未受反爬拦截",
)
async def download_video_by_url(url: str) -> str:
    """宏工具：URL/别名 → 解析 → 下载 → 转码 → 本地 MP4。黑盒，不暴露中间过程。"""
    url = _resolve_site_url(url)  # [安全别名] 物理兜底：tz 92285 → 完整 URL
    url = await resolve_short_url(url)  # 短链前置跟随 302 取 BV 长链
    if _BILI_HOST_RE.search(url or ""):
        return await _download_bilibili_video(url)
    from plugins.crawler.video_crawler import crawl_video
    result = await crawl_video(url)
    if result.get("success"):
        return json.dumps({"success": True, "msg": "视频已下载", "path": result["mp4_path"]}, ensure_ascii=False)
    return json.dumps({"success": False, "error": result.get("error", "下载失败")}, ensure_ascii=False)
