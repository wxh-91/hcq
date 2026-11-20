# -*- coding: utf-8 -*-
"""WCU 请求语境：把"用户本轮原话"带到工具层，供零信任校验器比对。

为什么需要它：
    URL 校验器要判断"这个域名是否在用户原话里出现过"，但工具函数本身
    只拿到 url / app_name 这类参数，看不到用户说了什么。ai.py 在每轮对话
    入口用 set_utterance(prompt) 写入当前协程的 ContextVar，
    工具层再用 get_utterance() 取回。ContextVar 是按协程隔离的，
    多用户并发（飞书 + QQ）不会串味。

安全默认：取不到原话时返回空串 —— 空串意味着"用户没说过"，校验器一律从严。
"""
from contextvars import ContextVar

_UTTERANCE: "ContextVar[str]" = ContextVar("wcu_utterance", default="")


def set_utterance(text: str) -> None:
    """写入本轮用户原话（ai.py 每轮入口调用）。"""
    try:
        _UTTERANCE.set(text or "")
    except Exception:
        pass


def get_utterance() -> str:
    """读取本轮用户原话；无上下文时返回空串（从严）。"""
    try:
        return _UTTERANCE.get() or ""
    except Exception:
        return ""


def reset_utterance() -> None:
    """清理上下文（任务结束/异常收尾时调用，避免串轮）。"""
    try:
        _UTTERANCE.set("")
    except Exception:
        pass
