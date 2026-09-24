from pydantic	import BaseModel, Field, PrivateAttr
from typing		import Dict, List, Any, Optional, Self


class Request(BaseModel):
	
	url		: str = Field(..., description="需要请求的网址")
	timeout	: int = Field(default=300)
	
	_headers: Dict[str, Any] = PrivateAttr(default_factory=dict)
	_body	: Dict[str, Any] = PrivateAttr(default_factory=dict)
	_params	: Dict[str, Any] = PrivateAttr(default_factory=dict)
	
	def set_header(self, key: str, value: Any) -> Self:
		self._headers[key] = value
		return self
	def set_body(self, key: str, value: Any) -> Self:
		self._body[key] = value
		return self
	def set_param(self, key: str, value: Any) -> Self:
		self._params[key] = value
		return self
	
	@property
	def headers(self):
		return self._headers
	@property
	def body(self):
		return self._body
	@property
	def params(self):
		return self._params
	