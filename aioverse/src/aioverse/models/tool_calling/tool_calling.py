from pydantic	import BaseModel, Field
from typing		import Optional

from .function	import Function


class ToolCalling(BaseModel):

	index	: Optional[int]	= Field(default=None)
	id		: str			= Field(default="")
	type	: str			= Field(default="function")
	function: Function		= Field(default_factory=Function)
