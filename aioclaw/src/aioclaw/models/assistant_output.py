# 类型两件套
from pydantic	import BaseModel, model_validator, Field

from typing		import Optional, Dict, Any


class AssistantOutput(BaseModel):
	
	finish_reason		: str
	content				: str	= Field(default_factory=str)
	reasoning_content	: str	= Field(default_factory=str)
	
	
	@model_validator(mode="before")
	@classmethod
	def none_to_string(cls, data: Dict[str, Any]) -> Dict[str, Any]:
		
		content				= data.get("content")
		reasoning_content	= data.get("reasoning_content")
		
		if content is None:
			data["content"] = ""
		if reasoning_content is None:
			data["reasoning_content"] = ""
		
		return data