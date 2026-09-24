from __future__ import annotations
from ..protocols	import ToolSetProtocol, ToolsManagerProtocol


class BaseTool(ToolSetProtocol):
	
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
	
	def register(self, tools_manager: ToolsManagerProtocol):
		super().register(tools_manager)