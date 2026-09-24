from typing	import runtime_checkable, Protocol, Dict, Any


@runtime_checkable
class ToSchemaAble(Protocol):
	
	def to_schema(self) -> Dict[str, Any]:
		...