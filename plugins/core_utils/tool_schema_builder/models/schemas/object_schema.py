from .base_schema	import BaseSchema

from typing		import Literal, Dict, List, Any, Optional
from pydantic	import Field, SerializeAsAny


SchemaProperties = Dict[str, SerializeAsAny[BaseSchema]]


# HACK: 目前将properties过滤并手动实现转换逻辑，后续通过pydantic自带的(反)序列化验证函数来进行转换逻辑
class ObjectSchema(BaseSchema):
	
	"""object 类型 schema"""
	
	type: Literal["object"] = Field(default="object", description="强行为 object 类型")
	
	properties	: SchemaProperties		= Field(..., description="对象键 schema 定义", exclude=True)
	required	: Optional[List[str]]	= Field(default=None, description="存必须的键", min_length=1)
	
	
	def to_schema(self) -> Dict[str, Any]:
	
		"""手动实现 to_schema 改成递归调用"""
		
		result = super().to_schema()
		
		# 手动实现properties转换逻辑
		result["properties"] = {
			schema_name: schema_instance.to_schema()
			for schema_name, schema_instance in self.properties.items()
		}
		
		return result
