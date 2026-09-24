from pydantic	import BaseModel, ConfigDict, Field, SerializeAsAny, model_validator
from typing		import List, Optional, Any, Union, Dict

from ..segments	import (
	BaseSegment,
	TextSegment,
	ImageUrlSegment,
	ImageBase64Segment,
	AudioInputSegment,
	AudioUrlSegment,
	FileSegment,
	VideoInputSegment,
	VideoUrlSegment,
	UnknownSegment
)
from ...enums	import Roles


SEGMENTS_LIST = List[Union[
	SerializeAsAny[TextSegment],			# 最高频 优先匹配
	SerializeAsAny[ImageUrlSegment],
	SerializeAsAny[ImageBase64Segment],
	SerializeAsAny[AudioInputSegment],
	SerializeAsAny[AudioUrlSegment],
	SerializeAsAny[FileSegment],
	SerializeAsAny[VideoInputSegment],
	SerializeAsAny[VideoUrlSegment],
	SerializeAsAny[UnknownSegment],			# 兜底 匹配任意未知 type
	SerializeAsAny[BaseSegment],			# 最后手段
]]


class BaseContext(BaseModel):
	
	model_config = ConfigDict(slots=True, extra='allow')
	
	role				: Roles						= Field(..., description="上下文角色")
	content				: Union[str, SEGMENTS_LIST]	= Field(..., description="上下文正文")
	reasoning_content	: str						= Field(..., description="上下文思维链")
	
	def __str__(self) -> str:
		return self.content
	
	
	@model_validator(mode="before")
	def none_to_string(cls, data: Dict[str, Any]) -> Dict[str, Any]:
		
		"""确保正文和思维链必须是字符串"""
		
		content		= data.get("content")
		r_content	= data.get("reasoning_content")
		
		if content is None:
			data["content"] = ""
		
		if r_content is None:
			data["reasoning_content"] = ""
		
		return data
