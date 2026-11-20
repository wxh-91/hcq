#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
robust_crawler.py —— 爬虫"总调度器"（生产反哺版）

把测试单元 /root/dev_test_unit 中验证过的"多级降级漏斗"（A→B→C + 输出校验器 + 链路日志）
移植回生产环境，作为爬虫的统一调度入口：

    优先级 1（最稳）：dom_hook.js   —— 隐身浏览器暴力嗅探（video 标签 + 媒体响应 + m3u8 清单）
    优先级 2（次稳）：probe.js      —— 无头浏览器抓网络请求（xhr / fetch / media）
    优先级 3（全挂）：返回明确的失败标识（success=False）+ 完整链路日志 chain_log

与测试单元一致的硬契约：
    - 输出校验器：提取到的 URL 必须通过 is_video_url 校验（.mp4/.m3u8 等后缀或视频格式特征）；
      乱码 / 空数据 / 未过校验器 -> 视为本方案失败，强制进入下一级降级；
    - 任何单方案异常（退出码非 0 / JSON 损坏 / 超时 / 无候选）只记录、不中断漏斗；
    - 本模块只做调度与校验，不修改任何 Node 脚本，不触碰其他生产模块。
"""
import os
_ROOT = os.environ.get("PROJECT_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import asyncio
import json
import logging
import os
import re


logger = logging.getLogger(__name__)

# Node 脚本目录（默认生产路径；可用环境变量覆盖，便于镜像/备份目录自测）
CRAWLER_TOOLS_DIR = os.environ.get(
    "CRAWLER_TOOLS_DIR", _ROOT + "/plugins/crawler/crawler_tools"
)

# 单方案超时（ms），与生产 JS 默认一致：dom_hook 最稳最慢，probe 次之
DOM_HOOK_TIMEOUT_MS = 20000
PROBE_TIMEOUT_MS = 15000

# ===================== 输出校验器 =====================
# 直接后缀：.mp4 / .m3u8 / .flv / .webm / .ts（可带 query/hash）
_VIDEO_EXT_RE = re.compile(
    r"^https?://[^\s\"'<>]+?\.(mp4|m3u8|flv|webm|ts)(?:[?#][^\s\"'<>]*)?$",
    re.IGNORECASE,
)
# 视频格式特征：媒体后缀 / 常见视频字段 / 播放路径
_VIDEO_MARKER_RE = re.compile(
    r"(?i)\.(mp4|m3u8|flv|webm|ts)([?#]|$)|/video/|videourl|playurl|play_url|video_url"
)
# m3u8 清单文本特征（作为证据校验）
_M3U8_SIGNATURE_RE = re.compile(r"#EXTM3U|#EXT-X-STREAM-INF|#EXT-X-MEDIA-SEQUENCE")


def is_video_url(url: str) -> bool:
    """输出校验器：判断 URL 是否为有效视频链接（拒绝乱码 / 空 / 非 http 协议）。"""
    if not url or not isinstance(url, str):
        return False
    u = url.strip().strip('"').strip("'")
    if not u.startswith(("http://", "https://")):
        return False
    # 控制字符视为乱码
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", u):
        return False
    if len(u) > 2048:
        return False
    return bool(_VIDEO_EXT_RE.match(u) or _VIDEO_MARKER_RE.search(u))


def _has_m3u8_signature(text: str) -> bool:
    """证据校验：响应体文本含 m3u8 清单特征，也算视频证据。"""
    if not text:
        return False
    return bool(_M3U8_SIGNATURE_RE.search(text[:2000]))


async def _run_node(script_path: str, args: list, timeout_ms: int):
    """执行 Node 脚本，看门狗超时强杀。返回 (returncode, stdout, stderr)。"""
    proc = await asyncio.create_subprocess_exec(
        "node", script_path, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=CRAWLER_TOOLS_DIR,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_ms / 1000.0
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(
            f"Node 脚本超时（{timeout_ms}ms）：{os.path.basename(script_path)}"
        )
    return proc.returncode, stdout.decode("utf-8", "replace"), stderr.decode(
        "utf-8", "replace"
    )


def _parse_dom_hook(stdout: str):
    """解析 dom_hook.js 输出 -> (候选URL列表, url->证据文本)。"""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        raise ValueError("dom_hook 输出不是合法 JSON（疑似脏数据）")
    if isinstance(data, dict) and data.get("error"):
        raise ValueError(f"dom_hook 返回错误：{str(data['error'])[:200]}")
    urls = data.get("urls") if isinstance(data, dict) else None
    if not isinstance(urls, list):
        raise ValueError("dom_hook 输出缺少 urls 列表（空数据）")
    evidence = {}
    if isinstance(data, dict) and data.get("m3u8_preview"):
        evidence = {u: data["m3u8_preview"] for u in urls if isinstance(u, str)}
    return [u for u in urls if isinstance(u, str)], evidence


def _parse_probe(stdout: str):
    """解析 probe.js 输出 -> (候选URL列表, url->证据文本)。"""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        raise ValueError("probe 输出不是合法 JSON（疑似脏数据）")
    reqs = data.get("requests") if isinstance(data, dict) else None
    if not isinstance(reqs, list):
        raise ValueError("probe 输出缺少 requests 列表（空数据）")
    candidates, evidence = [], {}
    for r in reqs:
        if not isinstance(r, dict):
            continue
        url = r.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        rtype = r.get("type", "")
        if rtype in ("media", "fetch", "xhr"):
            candidates.append(url)
        if r.get("response_preview"):
            evidence[url] = r["response_preview"]
    return candidates, evidence


def _pick_valid_video(candidates, evidence):
    """输出校验器终审：挑出第一个有效视频链接；乱码/空/未过校验 -> None。"""
    for raw in candidates:
        u = raw.strip().strip('"').strip("'")
        if is_video_url(u):
            return u
        # URL 不带视频后缀，但响应体是 m3u8 清单文本（证据校验），同样认可
        if _has_m3u8_signature(evidence.get(u, "")):
            return u
    return None


async def robust_video_crawl(url: str, timeout: int = DOM_HOOK_TIMEOUT_MS) -> dict:
    """
    爬虫总调度器：多级降级漏斗。

    参数:
        url:     目标网页 URL
        timeout: 每个降级方案的最大执行时间（ms），默认 20000

    返回 dict:
        success=True  -> {"success": True, "url", "source_scheme", "chain_log"}
        success=False -> {"success": False, "error", "last_error", "chain_log", "detail"}
    """
    chain_log: list = []
    last_error: str = ""

    schemes = (
        ("dom_hook", "dom_hook.js", DOM_HOOK_TIMEOUT_MS, _parse_dom_hook),
        ("probe", "probe.js", PROBE_TIMEOUT_MS, _parse_probe),
    )

    for name, script, scheme_timeout, parser in schemes:
        chain_log.append(name)
        script_path = os.path.join(CRAWLER_TOOLS_DIR, script)
        try:
            rc, stdout, stderr = await _run_node(
                script_path, [url, str(scheme_timeout)], scheme_timeout
            )
            if rc != 0:
                raise ValueError(f"{name} 退出码 {rc}：{stderr[:300] or stdout[:300]}")
            candidates, evidence = parser(stdout)
            if not candidates:
                raise ValueError(f"{name} 未提取到任何候选链接（空数据）")
            video = _pick_valid_video(candidates, evidence)
            if video is None:
                raise ValueError(f"{name} 候选未通过输出校验器（判定为乱码/非视频链接）")
            logger.info("[robust_crawler] %s 命中视频直链：%s", name, video[:120])
            return {
                "success": True,
                "url": video,
                "source_scheme": name,
                "chain_log": chain_log,
            }
        except (ValueError, TimeoutError, OSError) as e:
            last_error = f"{name}：{e}"
            logger.warning("[robust_crawler] 方案 %s 失败，继续降级：%s", name, last_error)
            continue

    return {
        "success": False,
        "error": "所有降级方案均失败，未获得视频直链",
        "last_error": last_error,
        "chain_log": chain_log,
        "detail": f"已尝试方案（按优先级）：{' → '.join(chain_log) or '无'}",
    }
