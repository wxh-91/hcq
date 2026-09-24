from pydantic	import BaseModel, Field, SerializeAsAny

from ..contexts	import BaseContext


class Choice(BaseModel):
	
	finish_reason		: str
	index				: int
	message				: SerializeAsAny[BaseContext]