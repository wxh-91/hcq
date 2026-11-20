"""启动引导：在进程最早期登记所有核心服务。

全部使用惰性工厂，本模块自身不 import 任何业务模块——
首次 get_service 时才加载目标模块，从根上避免循环导入与导入顺序耦合。
bootstrap() 幂等，可重复调用。
"""
from __future__ import annotations

import importlib
from typing import Any, Callable

from import_center.manager import container


def _attr_factory(module_name: str, attr_name: str | None) -> Callable[[], Any]:
    """惰性加载：get 时 import 模块并返回其中的属性；attr_name 为空则返回模块本身。"""

    def _load() -> Any:
        mod = importlib.import_module(module_name)
        if attr_name is None:
            return mod
        return getattr(mod, attr_name)

    return _load


def _call_factory(module_name: str, attr_name: str, args: tuple = (), kwargs: dict | None = None) -> Callable[[], Any]:
    """惰性实例化：get 时 import 模块、取属性并调用（用于需要 new 的类）。"""

    def _load() -> Any:
        mod = importlib.import_module(module_name)
        target = getattr(mod, attr_name)
        return target(*args, **(kwargs or {}))

    return _load


# 核心服务登记表：name -> (module, attr, 是否实例化调用)
_SERVICES: dict[str, tuple] = {
    # 核心单例对象（首次取用时实例化）
    "memory": ("memory", "Memory", "call"),            # memory.Memory() 进程级单例
    "knowledge_db": ("memory", "knowledge_db", "attr"),  # memory.knowledge_db 实例
    "registry_manager": ("tool_center.registry_manager", None, "attr"),  # 子模块本体
    # admin 侧跨模块函数 / 视图（ai.py 及外部模块经容器取用）
    "admin.TOOLS": ("admin", "TOOLS", "attr"),
    "admin.execute_tool_directly": ("admin", "execute_tool_directly", "attr"),
    "admin.build_tool_schema": ("admin", "build_tool_schema", "attr"),
    # ai 侧跨模块函数 / 类（admin.py 经容器取用）
    "ai.get_conv_key": ("ai", "get_conv_key", "attr"),
    "ai.TodoListManager": ("ai", "TodoListManager", "attr"),
    "ai.safe_api_request": ("ai", "safe_api_request", "attr"),
    "ai.ask_ai": ("ai", "ask_ai", "attr"),
    # 中央控制台（只读配置，人类手动维护 settings.py）
    "config_center": ("config_center", "get_config", "attr"),
    "config_center.settings": ("config_center", "settings", "attr"),
}


def bootstrap() -> None:
    """登记全部核心服务（幂等：已登记的服务跳过）。"""
    for name, (module_name, attr_name, kind) in _SERVICES.items():
        if name in container:
            continue
        if kind == "call":
            factory = _call_factory(module_name, attr_name)
        else:
            factory = _attr_factory(module_name, attr_name)
        container.register(name, factory=factory)
