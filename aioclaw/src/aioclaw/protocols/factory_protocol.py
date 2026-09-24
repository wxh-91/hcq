from __future__ import annotations

from abc		import ABC, abstractmethod
from typing		import Any, Type, Self


class FactoryProtocol(ABC):
	
	@abstractmethod
	def register(self, class_: Type, priority: int) -> Self:
		
		"""根据 priority 顺序 注册工厂"""
		...
	
	@abstractmethod
	def dispatcher(self, data: Any) -> Any:
		
		"""基于注册的工厂 尝试将data转为对应工厂的实例"""
		...