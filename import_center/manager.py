"""统一导入单元：集中式服务容器（ServiceLocator）。

设计要点：
- 惰性工厂：服务首次被 get 时才 import 对应模块并实例化，彻底消除
  模块间顶层导入的顺序耦合与循环依赖。
- 函数 / 类 / 实例统一按名字注册与取用，散落的跨模块 import 收口到容器。
- 仅使用标准库（importlib / threading），不引入任何第三方依赖。
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional


class ServiceContainer:
    """极简服务容器：注册（实例或惰性工厂）与取用。"""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], Any]] = {}
        self._instances: dict[str, Any] = {}
        self._lock = threading.RLock()

    def register(
        self,
        name: str,
        instance: Any = None,
        *,
        factory: Optional[Callable[[], Any]] = None,
    ) -> "ServiceContainer":
        """注册服务。instance 优先；否则用 factory 惰性创建（首次 get 时执行并缓存）。"""
        if not name:
            raise ValueError("服务名不能为空")
        with self._lock:
            if instance is not None:
                self._instances[name] = instance
                self._factories.pop(name, None)
            elif factory is not None:
                self._factories[name] = factory
            else:
                raise ValueError(f"服务 '{name}' 必须提供 instance 或 factory")
        return self

    def get(self, name: str) -> Any:
        """取服务实例：命中缓存直接返回；否则执行工厂并缓存。未注册则抛 KeyError。"""
        with self._lock:
            if name in self._instances:
                return self._instances[name]
            factory = self._factories.get(name)
            if factory is None:
                raise KeyError(f"未注册的服务: {name}")
            instance = factory()
            self._instances[name] = instance
            return instance

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._instances or name in self._factories

    def __contains__(self, name: str) -> bool:
        return self.has(name)


# 全局唯一容器（进程级单例）
container = ServiceContainer()


def get_service(name: str) -> Any:
    """统一取服务入口：散落的跨模块 import 一律改为向容器要。"""
    return container.get(name)


def register_service(name: str, instance: Any = None, *, factory: Optional[Callable[[], Any]] = None) -> None:
    container.register(name, instance=instance, factory=factory)
