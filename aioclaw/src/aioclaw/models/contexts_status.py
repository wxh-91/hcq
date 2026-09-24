from pydantic	import BaseModel, PrivateAttr, Field, SerializeAsAny, field_validator

from .context_blocks	import BaseContextsBlock, ToolCallingContextsBlock
from ..factories.contexts_factory	import restore_context_data
from aioverse.models	import BaseContext, SystemContext

from typing		import List, Optional, Union, Dict, Any


ContextsListSupportType = List[Union[
	SerializeAsAny[ToolCallingContextsBlock],
	SerializeAsAny[BaseContextsBlock],
	SerializeAsAny[BaseContext]
]]


class ContextsStatus(BaseModel):
	
	contexts: ContextsListSupportType = Field(default_factory=list, description="上下文列表")
	prompt	: Optional[SystemContext] = Field(default=None, description="提示词")
	memory	: Optional[SystemContext] = Field(default=None, description="压缩后的历史记忆")
	
	token	: int	= Field(default=0)
	
	_is_dirty			: bool			= PrivateAttr(default=True)
	_contexts_cache		: List[SerializeAsAny[BaseContext]]	= PrivateAttr(default_factory=list)
	_tokens_cache		: Dict[str, int]	= PrivateAttr(default_factory=dict)
	
	@field_validator("contexts", mode="before")
	@classmethod
	def _restore_contexts(cls, contexts: Any) -> Any:

		"""恢复持久化上下文的具体模型类型。"""

		if not isinstance(contexts, list):
			return contexts

		return [
			restore_context_data(context)
			for context in contexts
		]

	
	def is_dirty(self) -> bool:
		return self._is_dirty
	
	
	def set_dirty(self, status: bool):
		self._is_dirty = status
		if status is True:
			self.clear_tokens_cache()
	def mark_dirty(self):
		self.set_dirty(True)
	def set_token(self, token: int):
		self.token = token
	def set_prompt(self, prompt: Optional[SystemContext]):
		self.prompt = prompt
		self.mark_dirty()
	def set_memory(self, memory: Optional[SystemContext]):
		self.memory = memory
		self.mark_dirty()
	def set_contexts_cache(self, contexts_list: List[BaseContext]):
		self._contexts_cache = contexts_list
	def clear_tokens_cache(self):
		self._tokens_cache.clear()
	def get_cached_tokens(self, cache_key: str) -> Optional[int]:

		"""读取当前上下文对应的 token 缓存。"""

		if self.is_dirty():
			self._rebuild_contexts_cache_list()

		return self._tokens_cache.get(cache_key)
	def set_cached_tokens(self, cache_key: str, tokens: int):
		self._tokens_cache[cache_key] = tokens
	
	
	def _rebuild_contexts_cache_list(self) -> None:
		
		"""
		重建上下文缓存
		"""
		
		contexts_list = []
		
		for context in self.contexts:
			
			if isinstance(context, BaseContextsBlock):
				contexts_list.extend(ctx for ctx in context)
			else:
				contexts_list.append(context)
			
		self.set_contexts_cache(contexts_list)
		self.set_dirty(False)
		
	def add_context(self, context: Union[BaseContextsBlock, BaseContext]):
		self.contexts.append(context)
		self.mark_dirty()

	def replace_contexts(self, contexts: ContextsListSupportType):

		"""整体替换上下文，供压缩等批量操作使用"""

		self.contexts = list(contexts)
		self.mark_dirty()
	
	def flatten_contexts(
		self, *,
		filter_prompt: bool = False
	) -> List[BaseContext]:
		
		"""根据 _is_dirty 自动决定是否重建缓存。"""
		
		if self.is_dirty():
			self._rebuild_contexts_cache_list()
		
		# 主提示词与历史记忆固定放在上下文最前面
		if filter_prompt is False:
			prefix_contexts = []

			if self.prompt is not None:
				prefix_contexts.append(self.prompt)

			if self.memory is not None:
				prefix_contexts.append(self.memory)

			return [*prefix_contexts, *self._contexts_cache]
		
		return self._contexts_cache
	
	def to_list(self, **kwargs) -> List[Dict[str, Any]]:
		
		"""转为列表"""
		
		contexts_list = [
			ctx.model_dump(mode="json", exclude_none=True)
			for ctx in self.flatten_contexts(**kwargs)
		]
		
		return contexts_list
