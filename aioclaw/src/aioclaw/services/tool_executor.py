from __future__ import annotations

import asyncio
import orjson

from aioverse.models import BaseSegment, ToolCallingContext, ToolOutputContext

from typing import Any, Awaitable, Callable, List, Union


ToolOutputContent = Union[str, List[BaseSegment]]


def normalize_tool_output(tool_output: Any) -> ToolOutputContent:

	"""保留多模态消息段，其他工具结果统一转为文本。"""

	if isinstance(tool_output, BaseSegment):
		return [tool_output]

	if isinstance(tool_output, list):
		if all(isinstance(segment, BaseSegment) for segment in tool_output):
			return tool_output

	return str(tool_output)


async def safe_execute_tool(coro, timeout: int = 30) -> ToolOutputContent:

	try:
		tool_output = await asyncio.wait_for(coro, timeout=timeout)
		return normalize_tool_output(tool_output)

	except Exception as exception:
		return f"{type(exception).__name__}: {exception}"


def func2coro(func: Callable[..., Any], *args, **kwargs) -> Awaitable:

	return (
		func(*args, **kwargs)
		if asyncio.iscoroutinefunction(func)
		else asyncio.to_thread(func, *args, **kwargs)
	)


class ToolExecutor:

	"""只负责参数解析、工具调用、超时和结果规范化。"""

	def __init__(self, *, timeout: int = 30):
		self.timeout = timeout

	def set_timeout(self, timeout: int) -> None:
		self.timeout = timeout

	async def execute(
		self,
		tool_calling: ToolCallingContext,
		resolver: Callable[[str], Any],
	) -> ToolOutputContext:

		tool_name = tool_calling.function.name
		tool_id = tool_calling.id
		registration = resolver(tool_name)

		if registration is None:
			tool_output = f"无法调用不存在的工具: {tool_name}"
			return ToolOutputContext(tool_call_id=tool_id, content=tool_output)

		func, _ = registration

		try:
			tool_arguments = orjson.loads(tool_calling.function.arguments)
			if not isinstance(tool_arguments, dict):
				raise TypeError("工具参数必须是 JSON 对象")

			tool_coro = func2coro(func, **tool_arguments)

		except Exception as exception:
			tool_output = f"{type(exception).__name__}: {exception}"

		else:
			tool_output = await safe_execute_tool(
				tool_coro,
				timeout=self.timeout,
			)

		return ToolOutputContext(tool_call_id=tool_id, content=tool_output)
