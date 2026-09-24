from .base_multiple_schema	import BaseMultipleSchemas

from pydantic	import Field
from typing		import Literal


class AnyOfSchema(BaseMultipleSchemas):

	"""anyOf schema 支持"""

	type: Literal["anyOf"] = Field(default="anyOf", description="强行为 anyOf 类型")