from .base_schema	import BaseSchema

from pydantic	import Field, SerializeAsAny
from typing import Literal, Tuple, Dict, List, Any
from typing_extensions import Self

MultipleSchemaTuple = Tuple[SerializeAsAny[BaseSchema], ...]


class BaseMultipleSchemas(BaseSchema):

	type: str = Field(..., description="schema 的类别", exclude=True)

	schemas: MultipleSchemaTuple = Field(default_factory=tuple, exclude=True, min_length=2)


	def to_schema(self) -> Dict[str, List[Any]]:

		schema_list = [s.to_schema() for s in self.schemas]

		return {self.type: schema_list}


	@classmethod
	def build(cls, *schemas: BaseSchema) -> Self:
		return cls(schemas=schemas)
