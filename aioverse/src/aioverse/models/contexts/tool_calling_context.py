from .base_context	import BaseContext
from ..segments		import BaseSegment
from ..tool_calling	import ToolCalling
from ...enums		import Roles

from typing		import Literal, List


class ToolCallingContext(BaseContext):

	role		: Literal[Roles.TOOL_CALLING] = Roles.TOOL_CALLING
	tool_calls	: List[ToolCalling]

	def __str__(self) -> str:

		tool_arguments = "".join([tc.function.arguments for tc in self.tool_calls])
		return self.content + tool_arguments