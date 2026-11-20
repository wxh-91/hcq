from .base_schema	import BaseSchema

from typing		import Literal, Optional, List, Any, Dict
from pydantic	import Field, SerializeAsAny


SchemaItems = List[SerializeAsAny[BaseSchema]]


# HACK: 目前将items过滤并手动实现转换逻辑，后续通过pydantic自带的(反)序列化验证函数来进行转换逻辑
class ArraySchema(BaseSchema):

	"""array 类型 schema"""

	type: Literal["array"] = Field(default="array", description="强行为 array 类型")
	
	items		: Optional[SchemaItems]	= Field(default=None, description="数组元素的 schema", min_length=1, exclude=True)
	min_items	: Optional[int]			= Field(default=None, description="数组最小元素数量", alias="minItems", ge=1)
	max_items	: Optional[int]			= Field(default=None, description="数组最大元素数量", alias="maxItems")
	unique_items: Optional[bool]		= Field(default=None, description="数组元素是否需要唯一", alias="uniqueItems")
	
	
	def to_schema(self) -> Dict[str, Any]:
	
		"""手动实现 to_schema 改成递归调用"""
		
		result = super().to_schema()
		
		if self.items is not None:
			result["items"] = [item.to_schema() for item in self.items]
		
		return result
