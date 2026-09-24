from pydantic	import BaseModel, Field
from typing		import List, Optional

from .usage		import Usage
from .choice	import Choice


class Response(BaseModel):
	
	id		: str
	created	: int
	model	: str
	object	: str
	
	choices	: List[Choice]
	usage	: Optional[Usage] = Field(default=None)