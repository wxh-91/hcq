from pydantic	import BaseModel, PrivateAttr, Field


class AssistantKey(BaseModel):
	
	key: str
	
	_is_available	: bool = PrivateAttr(default=True)
	is_enable		: bool = Field(default=True)
	
	@property
	def is_available(self) -> bool:
		return self._is_available
	
	def set_unavailable(self):
		self._is_available = False
	def set_available(self):
		self._is_available = True