from pydantic	import BaseModel

from .function	import Function


class Tool(BaseModel):
	
	type	: str = "function"
	function: Function