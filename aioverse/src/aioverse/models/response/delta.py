from pydantic	import BaseModel, Field, model_validator
from typing		import List, Optional, Dict, Any


class Delta(BaseModel):
	
	"""
	SSE 流式增量
	对应 OpenAI 兼容 API 的 stream chunk 中的 delta 字段
	
	tool_calls 使用宽松的 List[Dict] 类型，因为流式分片中各字段可能不在同一 chunk 到达
	(如 id/type 仅首个 chunk 有, function.arguments 分多个 chunk 推送)
	"""
	
	role				: Optional[str]				= Field(default=None)
	content				: Optional[str]				= Field(default=None)
	reasoning_content	: Optional[str]				= Field(default=None)
	tool_calls			: Optional[List[Dict[str, Any]]]	= Field(default=None)
	
	
	@model_validator(mode='before')
	@classmethod
	def _ensure_not_none(cls, data: Dict[str, Any]) -> Dict[str, Any]:
		
		"""确保所有字段至少存在 (兼容部分 API 不传字段的情况)"""
		
		for field_name in ('role', 'content', 'reasoning_content', 'tool_calls'):
			if field_name not in data:
				data[field_name] = None
		
		return data
