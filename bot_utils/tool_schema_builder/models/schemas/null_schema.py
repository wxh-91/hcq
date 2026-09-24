from .base_schema	import BaseSchema

from typing		import Literal
from pydantic	import Field


class NullSchema(BaseSchema):
	
	"""null 类型 schema"""
	
	type: Literal["null"] = Field(default="null", description="强行为 null 类型")