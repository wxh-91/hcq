# -*- coding: utf-8 -*-
"""Info Retrieval 插件（Plugin 5.0）—— 联网搜索与网页正文提取。

黑盒宏工具三件套（全部 AUP 协议、全部硬超时 15s、异常零泄漏）：
  web_search(query)       —— 联网搜索，只返回标题+摘要，严禁返回 URL。
  read_webpage(url)       —— trafilatura 提取正文，输出截断 3000 字。
  search_and_read(query)  —— 宏工具 Orchestrator：搜索 → 自动抓最匹配链接正文 → 返回。

搜索链路：优先 duckduckgo-search（DDGS）；该库在当前网络环境默认后端
bing.com 不可解析（返回空），自动降级 cn.bing.com 直连解析（同接口语义：
只给标题+摘要，URL 仅供 search_and_read 内部使用，绝不外泄）。
"""
from __future__ import annotations

import asyncio
import html as html_mod
import json
import re
from urllib.parse import quote

from aup_core import aup_tool

# 所有网络请求的硬超时（秒）
_NET_TIMEOUT = 15
# 正文截断上限（字）
_MAX_TEXT = 3000
_BING_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _ok(data) -> str:
    return json.dumps(data, ensure_ascii=False)


def _err(msg: str) -> str:
    return json.dumps({"success": False, "error": msg}, ensure_ascii=False)


def _safe(fn):
    """统一异常收敛：任何异常/超时都转业务错误文本，绝不泄漏堆栈给 AI。"""
    import functools as _functools

    @_functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await asyncio.wait_for(fn(*args, **kwargs), timeout=_NET_TIMEOUT)
        except asyncio.TimeoutError:
            return _err("请求超时（%s 秒），请稍后重试" % _NET_TIMEOUT)
        except Exception as e:
            msg = str(e) or type(e).__name__
            return _err("请求失败：%s" % msg[:150])

    return wrapper


def _strip_tags(seg: str) -> str:
    return re.sub(r"<[^>]+>", "", seg).strip()


def _bing_results(query: str, max_results: int = 5) -> list:
    """cn.bing.com 直连搜索（DDGS 在当前网络不可用时的降级通道）。

    返回 [{title, snippet, url}]；url 仅供内部抓正文使用，不出现在任何
    对外返回值中。
    """
    import httpx

    url = "https://cn.bing.com/search?q=%s&count=%d" % (quote(query), max_results * 2)
    r = httpx.get(
        url,
        headers={"User-Agent": _BING_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        timeout=12,
        follow_redirects=True,
    )
    r.raise_for_status()
    blocks = re.findall(r'<li class="b_algo".*?</li>', r.text, re.S)
    items = []
    for b in blocks:
        tm = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not tm:
            continue
        href = html_mod.unescape(tm.group(1))
        if href.startswith("http") and "bing.com" not in href.split("/")[2]:
            items.append(
                {
                    "title": _strip_tags(tm.group(2))[:200],
                    "snippet": "",
                    "url": href,
                }
            )
            if len(items) >= max_results:
                break
    # 摘要（p 标签，按块对应）
    for i, b in enumerate(blocks):
        pm = re.search(r'<p[^>]*>(.*?)</p>', b, re.S)
        if pm and i < len(items):
            items[i]["snippet"] = _strip_tags(pm.group(1))[:400]
        if len(items) >= max_results:
            break
    return items


def _ddgs_results(query: str, max_results: int = 5) -> list:
    """duckduckgo-search 优先通道。失败抛异常交给上层降级。"""
    from duckduckgo_search import DDGS

    with DDGS() as ddgs:
        raw = list(ddgs.text(query, max_results=max_results))
    items = []
    for r in raw:
        title = (r.get("title") or "").strip()[:200]
        snippet = (r.get("body") or "").strip()[:400]
        url = r.get("href") or ""
        if title or snippet:
            items.append({"title": title, "snippet": snippet, "url": url})
    return items


@_safe
@aup_tool(
    name="web_search",
    description="联网搜索：输入任意查询词，返回最多 5 条结果的标题与摘要（snippet）。注意：结果中不含任何 URL 链接，只有标题和摘要文本。适用于查询新闻、资讯、知识等实时信息。",
    category="network",
    is_danger=False,
    params_schema={
        "required": ["query"],
        "properties": {"query": {"type": "string", "description": "搜索查询词，如 现在的黄金价格"}},
    },
    suggestion_hint="搜索无结果时，可换关键词重试或直接说明查不到",
)
async def web_search(query: str) -> str:
    try:
        items = _ddgs_results(query)
    except Exception:
        items = _bing_results(query)
    out = []
    for it in items:
        out.append({"title": it["title"], "snippet": it["snippet"]})  # 红线：剔除 URL
    if not out:
        return _err("没有搜到相关结果，请尝试更换关键词")
    return _ok({"success": True, "count": len(out), "results": out})


@_safe
@aup_tool(
    name="read_webpage",
    description="读取网页正文：传入网页 URL，自动提取正文并截断至 3000 字返回。适用于阅读新闻文章、博客、文档等。",
    category="network",
    is_danger=False,
    params_schema={
        "required": ["url"],
        "properties": {"url": {"type": "string", "description": "要读取正文的网页 URL"}},
    },
    suggestion_hint="网页正文提取失败时，可换源站或说明无法读取",
)
async def read_webpage(url: str) -> str:
    import trafilatura

    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return _err("无法获取网页内容（可能不可达或被反爬拦截）")
    text = trafilatura.extract(
        downloaded, include_comments=False, include_tables=False, include_images=False
    )
    if not text or not text.strip():
        return _err("网页无正文可提取")
    content = text.strip()[:_MAX_TEXT]
    return _ok({"success": True, "content": content})


@_safe
@aup_tool(
    name="search_and_read",
    description="搜索并阅读宏工具：输入查询词，自动完成 搜索 → 抓取最匹配链接正文 → 返回摘要正文 的全流程。让 AI 一步到位看懂内容，不要拆成两步分别调用 web_search 和 read_webpage。",
    category="network",
    is_danger=False,
    params_schema={
        "required": ["query"],
        "properties": {"query": {"type": "string", "description": "搜索查询词"}},
    },
    suggestion_hint="正文抓取失败时返回摘要并说明",
)
async def search_and_read(query: str) -> str:
    import trafilatura

    try:
        items = _ddgs_results(query, max_results=3)
    except Exception:
        items = _bing_results(query, max_results=3)
    if not items:
        return _err("没有搜到相关结果，请尝试更换关键词")
    picked = items[0]
    title = picked.get("title", "")
    snippet = picked.get("snippet", "")
    content = ""
    # 逐个候选尝试抓正文，直到拿到非空正文
    for cand in items:
        url = cand.get("url", "")
        if not url:
            continue
        try:
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                text = trafilatura.extract(
                    downloaded,
                    include_comments=False,
                    include_tables=False,
                    include_images=False,
                )
                if text and text.strip():
                    content = text.strip()[:_MAX_TEXT]
                    title = cand.get("title", title)
                    snippet = cand.get("snippet", snippet)
                    break
        except Exception:
            continue
    return _ok(
        {
            "success": True,
            "title": title,
            "snippet": snippet,
            "content": content or "（正文提取失败，仅返回摘要）",
        }
    )
