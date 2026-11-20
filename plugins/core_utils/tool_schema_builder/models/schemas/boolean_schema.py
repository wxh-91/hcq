from .base_schema	import BaseSchema

from typing		import Literal, Optional, List
from pydantic	import Field


class BooleanSchema(BaseSchema):
	
	"""boolean 类型 schema"""
	
	type: Literal["boolean"] = Field(default="boolean", description="强行为 boolean 类型")
	
	enum: Optional[List[bool]]	= Field(default=None, description="boolean 枚举 没必要改")