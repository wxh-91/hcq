from __future__ import annotations

from aioverse.models import (
	Delta,
	ToolCalling as ToolCallingModel,
	ToolCallingContext,
)

from ..models import AssistantOutput
from ..enums import FinishReasons

from typing import List, Dict, Any, Optional


class StreamHandler:
	
	"""累积 SSE 增量并构建完整的单轮输出"""

	def __init__(self):
		self._content: str = ""
		self._reasoning: str = ""
		self._tool_calls: List[Dict[str, Any]] = []

	def reset(self) -> None:
		self._content = ""
		self._reasoning = ""
		self._tool_calls.clear()

	def merge(self, delta: Delta) -> None:

		if delta.content:
			self._content += delta.content

		if delta.reasoning_content:
			self._reasoning += delta.reasoning_content

		if delta.tool_calls:
			for tool_call in delta.tool_calls:
				self._merge_tool_call(tool_call)

	def _merge_tool_call(self, tool_call: Dict[str, Any]) -> None:

		existing = self._find_tool_call(tool_call.get("index"))

		if existing is not None:
			self._update_tool_call(existing, tool_call)

		else:
			self._add_tool_call(tool_call)

	def _find_tool_call(self, index: Any) -> Optional[Dict[str, Any]]:
		for pending in self._tool_calls:
			if pending.get("index") == index:
				return pending
		return None

	def _update_tool_call(self, target: Dict[str, Any], source: Dict[str, Any]) -> None:

		"""将 tool_call 增量合并到已有调用"""

		call_id = source.get("id")
		if call_id:
			target["id"] = call_id

		call_type = source.get("type")
		if call_type:
			target["type"] = call_type

		source_function = source.get("function")
		if not source_function:
			return

		target_function	= target.setdefault("function", {})
		function_name	= source_function.get("name")

		if function_name:
			target_function["name"] = function_name

		function_arguments = source_function.get("arguments")
		if function_arguments:
			target_function["arguments"] = target_function.get("arguments", "") + function_arguments

	def _add_tool_call(self, tool_call: Dict[str, Any]) -> None:

		tool_call.setdefault("function", {})
		tool_call["function"].setdefault("name", "")
		tool_call["function"].setdefault("arguments", "")
		self._tool_calls.append(tool_call)

	def flush(self, finish_reason: str = FinishReasons.STOP) -> AssistantOutput:

		return AssistantOutput(
			finish_reason		= finish_reason,
			content				= self._content,
			reasoning_content	= self._reasoning,
		)

	def build_tool_calling_context(self):

		tool_calls = [ToolCallingModel.model_validate(tool_call) for tool_call in self._tool_calls]

		return ToolCallingContext(
			content				= self._content,
			reasoning_content	= self._reasoning,
			tool_calls			= tool_calls,
		)

	@property
	def is_empty(self) -> bool:
		return not (self._content or self._reasoning or self._tool_calls)
