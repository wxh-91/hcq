from pydantic	import BaseModel

from .parameters	import Parameters


class Function(BaseModel):

	name		: str
	description	: str
	parameters	: Parameters