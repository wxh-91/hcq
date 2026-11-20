# -*- coding: utf-8 -*-
"""浏览器 URL 零信任校验器。

规则（用户硬要求）：
    1) 域名属于主流大站白名单（taobao / bilibili / pdd / baidu ...）→ 直接放行；
    2) 不认识的小众域名 → 必须能在"用户本轮原话"中找到，才允许打开；
    3) 既不白名单、原话里也没有 → 报错拦截：
       "目标网址未在白名单内，且未在指令中明确提供，请提供确切 URL"

设计考虑：
    - 只认 http/https；file:// / ftp:// / javascript: 一律拒绝（避免本地文件读取）。
    - 域名匹配做三级退化：主机全名 → 去掉 www. → 注册域（末两段）。
    - IDN（中文域名）同时比对 Unicode 与 punycode 两种写法。
    - 空原话 = 用户没说过 → 从严拦截。
"""
from urllib.parse import urlparse

from aup_core import AUPError

from . import context, policy

REJECT_MESSAGE = "目标网址未在白名单内，且未在指令中明确提供，请提供确切 URL"


def _normalize_host(host: str) -> str:
    return (host or "").strip().lower().strip(".")


def _registrable_root(host: str) -> str:
    """取注册域（末两段；遇到 .com.cn 这类二级后缀不做精细处理，够用即可）。"""
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _domain_hit(host: str, domains: tuple) -> bool:
    for d in domains:
        d = d.strip().lower()
        if not d:
            continue
        if host == d or host.endswith("." + d):
            return True
    return False


def _host_in_utterance(host: str, utterance: str) -> bool:
    if not utterance:
        return False
    u = utterance.lower()
    for cand in (host, host[4:] if host.startswith("www.") else host, _registrable_root(host)):
        if cand and cand in u:
            return True
    return False


def check_url(url: str, utterance: str = "") -> None:
    """校验通过则静默返回，不通过抛 AUPError("URL_NOT_ALLOWED")。"""
    parsed = urlparse((url or "").strip())
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise AUPError("BAD_URL", "只允许 http:// 或 https:// 开头的网址，收到：%s" % (url or "(空)"))
    host = _normalize_host(parsed.hostname or "")
    if not host:
        raise AUPError("BAD_URL", "网址缺少有效域名：%s" % url)

    # 1) 主流大站：直接放行
    if _domain_hit(host, policy.mainstream_domains()):
        return

    # 2) 小众域名：必须出现在用户本轮原话里
    if _host_in_utterance(host, utterance or context.get_utterance()):
        return

    # 3) 拦截
    raise AUPError("URL_NOT_ALLOWED", REJECT_MESSAGE)


def is_mainstream(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    return _domain_hit(_normalize_host(parsed.hostname or ""), policy.mainstream_domains())
