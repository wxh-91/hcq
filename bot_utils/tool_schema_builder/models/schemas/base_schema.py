from pydantic	import BaseModel, Field, ConfigDict
from typing		import Dict, Any, Optional

from ...traits	import ToSchemaAble


class BaseSchema(BaseModel):

	"""所有 Schema 的基类"""

	model_config = ConfigDict(populate_by_name=True)
	
	type: str = Field(..., description="schema的类别")
	
	description	: Optional[str] = Field(default=None, description="schema 的描述")
	
	
	def to_schema(self) -> Dict[str, Any]:
		return self.model_dump(exclude_none=True, by_alias=True)