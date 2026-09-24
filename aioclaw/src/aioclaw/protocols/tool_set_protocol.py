from __future__ import annotations

from abc	import ABC, abstractmethod
from typing	import TYPE_CHECKING


if TYPE_CHECKING:

	from .tools_manager_protocol import ToolsManagerProtocol


# 工具集
class ToolSetProtocol(ABC):
	
	@abstractmethod
	def __init__(self, *args, **kwargs):
		
		"""注意 这里是*args **kwargs 不是空参数"""
		...
	
	# 必须实现的register方法
	@abstractmethod
	def register(self, tools_manager: ToolsManagerProtocol) -> None:
		
		"""该协议通过该方法一键注册所有的工具"""
		...