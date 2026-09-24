from __future__ import annotations
from ..protocols	import ToolsManagerProtocol
from .base_tool		import BaseTool

from typing	import List


class _FinalTools:

	"""
	该类是通过添加已实例化的工具集 来提取工具
	这个类**不算**工具
	因为他虽然实现了register等方法 但实际他并不负责实现工具
	只负责聚合、缓存工具，并在聚合的工具中查找所需工具
	"""

	__slots__ = ("tools", "tools_cache")

	def __init__(self, *tool_instances):
		self.tools			= tool_instances
		self.tools_cache	= {} # 实现O(1)查找

	def __getattr__(self, name: str):

		# 通过缓存找工具
		tool_func = self.tools_cache.get(name, None)
		if tool_func:
			return tool_func

		for tool_instance in self.tools:

			if not hasattr(tool_instance, name):
				continue

			# 添加工具缓存并返回工具
			tool_func				= getattr(tool_instance, name)
			self.tools_cache[name]	= tool_func

			return tool_func

		raise AttributeError(f"已添加的工具不含有 {name}")

	def register(self, tools_manager: ToolsManagerProtocol):
		for tool in self.tools:
			tool.register(tools_manager)