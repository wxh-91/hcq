from pydantic	import Field
from typing		import Literal, Dict, Any

from .function_schema	import FunctionSchema
from .base_schema		import BaseSchema


class ToolSchema(BaseSchema):

	"""顶层 tool schema"""

	type: Literal["function"] = Field(default="function", description="schema的类别")
	
	function: FunctionSchema = Field(..., description="工具的函数信息", exclude=True)
	
	
	def to_schema(self) -> Dict[str, Any]:
		
		result				= super().to_schema()
		result["function"]	= self.function.to_schema()
		
		return result