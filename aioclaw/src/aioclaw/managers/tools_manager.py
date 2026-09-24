from __future__ import annotations

from ..protocols import ToolsManagerProtocol
from ..services import (
	ToolExecutor,
	ToolOutputContent,
	ToolRegistry,
	func2coro,
	normalize_tool_output,
	safe_execute_tool,
)

from aioverse.models import ToolOutputContext

from typing import List, Dict, Any, Callable, TYPE_CHECKING


if TYPE_CHECKING:

	from ..models import Tool
	from aioverse.models import ToolCallingContext


class ToolsManager(ToolsManagerProtocol):

	"""兼容旧 API 的工具门面，注册与执行由独立服务负责。"""

	def __init__(
		self,
		timeout: int = 30,
		*,
		registry: ToolRegistry | None = None,
		executor: ToolExecutor | None = None,
	):

		self._registry = ToolRegistry() if registry is None else registry
		self._executor = ToolExecutor(timeout=timeout) if executor is None else executor

	@property
	def schema(self) -> Dict[str, tuple]:
		"""保留旧的 schema 访问入口。"""
		return self._registry.schema

	@schema.setter
	def schema(self, value: Dict[str, tuple]) -> None:
		self._registry.schema = value

	@property
	def timeout(self) -> int:
		return self._executor.timeout

	@timeout.setter
	def timeout(self, value: int) -> None:
		self._executor.set_timeout(value)

	def register(self, func: Callable[..., Any], schema: "Tool"):
		self._registry.register(func, schema)

	def set_timeout(self, timeout: int):
		self._executor.set_timeout(timeout)

	async def execute_tool(self, tool_calling: "ToolCallingContext") -> ToolOutputContext:
		return await self._executor.execute(
			tool_calling,
			self._registry.get,
		)

	def to_list(self) -> List[Dict[str, Any]]:
		return self._registry.to_list()


tools_manager = ToolsManager()


__all__ = [
	"ToolOutputContent",
	"ToolsManager",
	"func2coro",
	"normalize_tool_output",
	"safe_execute_tool",
	"tools_manager",
]
