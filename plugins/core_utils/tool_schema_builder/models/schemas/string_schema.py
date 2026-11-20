from .base_schema	import BaseSchema

from typing		import Literal, List, Optional
from pydantic	import Field


class StringSchema(BaseSchema):

	"""string 类型 schema"""
	
	type: Literal["string"] = Field(default="string", description="强行为 string 类型的字符串")
	
	enum		: Optional[List[str]]	= Field(default=None, description="字符串的枚举值")
	max_length	: Optional[int]			= Field(default=None, description="字符串的最大长度", alias="maxLength")
	min_length	: Optional[int]			= Field(default=None, description="字符串的最小长度", alias="minLength", ge=1)