from pydantic	import Field, SerializeAsAny, ConfigDict, field_validator, model_validator

from ...protocols	import ContextsBlockProtocol
from ...factories.contexts_factory	import restore_context_data
from aioverse.models	import BaseContext

from typing		import List, Iterator, Any


class BaseContextsBlock(ContextsBlockProtocol):
	
	model_config = ConfigDict(extra='forbid')
	
	contexts: List[SerializeAsAny[BaseContext]] = Field(default_factory=list)

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
	
	@model_validator(mode='before')
	@classmethod
	def _clean_legacy_fields(cls, data: Any) -> Any:
		"""
		兼容旧版本数据: 
		仅当数据具有 ContextsBlock 特征字段 (tool_calling / tool_outputs / contexts) 时，
		才移除可能混入的 BaseContext 顶层字段 (content, reasoning_content, role)。
		
		如果数据没有 ContextsBlock 特征字段，说明它可能是 BaseContext 数据，
		不应该清理 (否则会误删 BaseContext 的必需字段)。
		"""
		if isinstance(data, dict):
			# 检查是否具有 ContextsBlock 的特征字段
			_is_contexts_block_data = any(
				key in data for key in ('tool_calling', 'tool_outputs', 'contexts')
			)
			
			if _is_contexts_block_data:
				# 这些字段属于 BaseContext 及其子类，不应出现在 ContextsBlock 顶层
				_legacy_keys = {'content', 'reasoning_content', 'role'}
				for key in _legacy_keys:
					data.pop(key, None)
		
		return data
	
	def __len__(self) -> int:
		return len(self.contexts)
	def __iter__(self) -> Iterator[BaseContext]:
		yield from self.contexts
	
	def delete(self, index: int):
		self.contexts.pop(index)
	def insert(self, index: int, context: BaseContext):
		self.contexts.insert(index, context)
	def append(self, context: BaseContext):
		self.contexts.append(context)
