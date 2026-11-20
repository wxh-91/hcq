from .base_schema	import BaseSchema

from typing		import Literal, Optional, List
from pydantic	import Field


class NumberSchema(BaseSchema):
	
	"""number 类型 schema"""
	
	type: Literal["number"] = Field(default="number", description="强行为 number 类型")
	
	enum				: Optional[List[float]]	= Field(default=None, description="浮点数的枚举值")
	minimum				: Optional[float]		= Field(default=None, description="浮点数的最小值 (包含)", alias="minimum")
	maximum				: Optional[float]		= Field(default=None, description="浮点数的最大值 (包含)", alias="maximum")
	exclusive_minimum	: Optional[float]		= Field(default=None, description="浮点数的最小值 (不包含)", alias="exclusiveMinimum")
	exclusive_maximum	: Optional[float]		= Field(default=None, description="浮点数的最大值 (不包含)", alias="exclusiveMaximum")
	multiple_of			: Optional[float]		= Field(default=None, description="浮点数的倍数", alias="multipleOf")