from pydantic	import Field
from typing		import Dict, Any

from .object_schema	import ObjectSchema
from .base_schema	import BaseSchema


class FunctionSchema(BaseSchema):
	
	"""function 类型 schema"""
	
	type: str	= Field(default="function", exclude=True, init=False, description="schema 的类别(Function不需要)")
	name: str	= Field(..., description="函数名")
	
	parameters	: ObjectSchema	= Field(..., description="函数参数信息", exclude=True)
	
	
	def to_schema(self) -> Dict[str, Any]:
	
		result					= super().to_schema()	
		result["parameters"]	= self.parameters.to_schema()
		
		return result