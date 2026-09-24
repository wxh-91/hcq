from pydantic	import BaseModel, Field


class Function(BaseModel):
	
	name		: str	= Field(default="")
	arguments	: str	= Field(default="")
