from .base_multiple_schema	import BaseMultipleSchemas

from pydantic	import Field
from typing		import Literal


class OneOfSchema(BaseMultipleSchemas):

	"""oneOf schema 支持"""

	type: Literal["oneOf"] = Field(default="oneOf", description="强行为 oneOf 类型")