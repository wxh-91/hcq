from .base_multiple_schema	import BaseMultipleSchemas

from pydantic	import Field
from typing		import Literal


class AllOfSchema(BaseMultipleSchemas):

	"""allOf schema 支持"""

	type: Literal["allOf"] = Field(default="allOf", description="强行为 allOf 类型")