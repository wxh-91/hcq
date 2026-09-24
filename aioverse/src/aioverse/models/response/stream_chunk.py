from pydantic	import BaseModel, Field, SerializeAsAny
from typing		import List, Optional

from .delta		import Delta
from .usage		import Usage


class StreamChoice(BaseModel):
	
	"""流式响应中的单个 choice"""
	
	index			: int
	delta			: Delta
	finish_reason	: Optional[str] = Field(default=None)


class StreamChunk(BaseModel):
	
	"""SSE 流式数据块 对应一次 data: {...}"""
	
	id		: str
	created	: int
	model	: str
	object	: str
	
	choices	: List[StreamChoice]
	usage	: Optional[Usage] = Field(default=None)
