from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple, TYPE_CHECKING


if TYPE_CHECKING:

	from ..models import Tool


ToolRegistration = Tuple[Callable[..., Any], "Tool"]


class ToolRegistry:

	"""只负责工具注册与 Schema 查询，不参与工具执行。"""

	def __init__(self):
		self.schema: Dict[str, ToolRegistration] = {}

	def register(self, func: Callable[..., Any], schema: "Tool") -> None:

		if func.__name__ not in self.schema:
			self.schema[func.__name__] = (func, schema)

	def get(self, tool_name: str) -> ToolRegistration | None:
		return self.schema.get(tool_name)

	def to_list(self) -> List[Dict[str, Any]]:
		return [schema.model_dump() for _, schema in self.schema.values()]
