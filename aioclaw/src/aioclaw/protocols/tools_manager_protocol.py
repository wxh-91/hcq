from __future__ import annotations

from abc	import ABC, abstractmethod
from typing	import List, TYPE_CHECKING


if TYPE_CHECKING:

	from ..models	import ToolCalling, Tool, ToolOutput


class ToolsManagerProtocol(ABC):
	
	@abstractmethod
	def register(self, tool_func: callable, tool_schema: Tool):
		
		"""实现工具注册"""
		...
	
	@abstractmethod
	async def execute_tool(self, tool_calling: ToolCalling) -> ToolOutput:
		
		"""运行工具并返回工具上下文"""
		...
	
	@abstractmethod
	def to_list(self) -> List[Tool]:
		
		"""返回Tool schema列表"""
		...