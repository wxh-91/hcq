from pydantic	import Field, PrivateAttr, SerializeAsAny

from aioverse.models	import (
	ToolOutputContext,
	BaseContext,
	ToolCallingContext
)
from .base_contexts_block	import BaseContextsBlock

from typing		import List, Iterator


class ToolCallingContextsBlock(BaseContextsBlock):
	
	"""
	工具调用上下文块 (Tool Calling Contexts Block)
	包含请求与结果
	不支持delete方法: 破坏调用链结构👎🏻
	"""
	
	contexts		: List[SerializeAsAny[BaseContext]] = Field(default_factory=list, exclude=True)
	tool_calling	: ToolCallingContext
	tool_outputs	: List[ToolOutputContext] = Field(default_factory=list)
	
	# 懒加载，避免重复计算 ID
	_id_cache	: List[str]	= PrivateAttr(default=None)
	_is_dirty	: bool		= PrivateAttr(default=True)
	
	def __iter__(self) -> Iterator[BaseContext]:
		yield self.tool_calling
		yield from self.tool_outputs
	
	def __len__(self) -> int:
		return len(self.tool_calling.tool_calls) + len(self.tool_outputs)
		
	def set_dirty(self):
		self._is_dirty = True
	def unset_dirty(self):
		self._is_dirty = False
	
	@property
	def id_cache(self) -> List[str]:
		
		"""懒加载 tool_calling_ids 的实现"""
		
		if self._is_dirty is True:
			self._id_cache = [tool_call.id for tool_call in self.tool_calling.tool_calls]
		
		return self._id_cache
	
	def is_complete(self) -> bool:
		
		"""验证 tool_calling 结果是否完整"""
		
		tool_output_ids = [
			tool_output.tool_call_id
			for tool_output in self.tool_outputs
		]
		is_complete		= all(
			(tool_calling_id in tool_output_ids)
			for tool_calling_id in self.id_cache
		)
		
		return is_complete
	
	def delete(self, index: int):
		raise NotImplementedError(f"{self.__class__.__name__} 不支持该方法")
	def insert(self, index: int, context: ToolOutputContext):
		self.set_dirty()
		self.tool_outputs.insert(index, context)
	def append(self, context: ToolOutputContext):
		self.set_dirty()
		self.tool_outputs.append(context)