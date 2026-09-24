from __future__ import annotations

from ..protocols	import ToolSetProtocol
from ..models		import AssistantOutput

from aioverse.models	import Response

from typing import Type


def chain_tools_by_class(*tool_classes, name: str="FinalTools") -> Type[ToolSetProtocol]:

	if not tool_classes:
		raise ValueError("至少需要一个工具类")
	
	return type(name, tool_classes, {})

def chain_tools_by_instance(*tool_instances) -> _FinalTools:
	
	from ..tools	import _FinalTools
	return _FinalTools(*tool_instances)

def generate_assistant_output_by_response(response: Response, index: int = 0) -> AssistantOutput:
	
	finish_reason		= response.choices[index].finish_reason
	content				= response.choices[index].message.content
	reasoning_content	= response.choices[index].message.reasoning_content
	
	output = AssistantOutput(
		finish_reason		= finish_reason,
		content				= content,
		reasoning_content	= reasoning_content
	)
	
	return output

async def kill_async_proc(proc):
	
	proc.kill()
	await proc.wait()