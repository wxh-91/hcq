from pydantic	import BaseModel, model_serializer

from .argument	import Argument

from typing		import Dict, List, Any


class Parameters(BaseModel):
	
	"""函数参数定义"""
	
	type		: str = "object"
	properties	: Dict[str, Argument]
	required	: List[str] = []
	
	@model_serializer(mode="wrap")
	def serialize(self, handler) -> Dict[str, Any]:
		
		"""序列化时，如果 required 为空列表则移除该字段。"""
		
		data = handler(self)
		
		if not data.get("required"):
			data.pop("required", None)
		
		return data
