from __future__ import annotations
from ..protocols	import ToolSetProtocol, ToolsManagerProtocol

from typing	import TYPE_CHECKING


# 待办：暂时没有实际用途，先不实现
class SystemOperationTools(ToolSetProtocol):
	
	def __init__(self, *args, **kwargs):
		
		super().__init__(*args, **kwargs)
	
	def register(self, tools_manager: ToolsManagerProtocol):
		
		super().register(tools_manager)
		
		...
	
	