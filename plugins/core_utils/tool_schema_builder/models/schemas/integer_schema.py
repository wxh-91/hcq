from .base_schema	import BaseSchema

from typing		import Literal, Optional, List
from pydantic	import Field


class IntegerSchema(BaseSchema):
	
	"""integer 类型 schema"""
	
	type: Literal["integer"] = Field(default="integer", description="强行为 integer 类型")
	
	enum				: Optional[List[int]]	= Field(default=None, description="整数的枚举值")
	minimum				: Optional[int]			= Field(default=None, description="整数的最小值 (包含)", alias="minimum")
	maximum				: Optional[int]			= Field(default=None, description="整数的最大值 (包含)", alias="maximum")
	exclusive_minimum	: Optional[int]			= Field(default=None, description="整数的最小值 (不包含)", alias="exclusiveMinimum")
	exclusive_maximum	: Optional[int]			= Field(default=None, description="整数的最大值 (不包含)", alias="exclusiveMaximum")
	multiple_of			: Optional[int]			= Field(default=None, description="整数的倍数", alias="multipleOf")