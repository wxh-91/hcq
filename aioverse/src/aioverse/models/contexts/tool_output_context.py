from .base_context	import BaseContext
from ...enums		import Roles

from typing	import Literal


class ToolOutputContext(BaseContext):
	
	role		: Literal[Roles.TOOL_OUTPUT] = Roles.TOOL_OUTPUT
	tool_call_id: str