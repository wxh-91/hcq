from enum	import Enum


class Roles(str, Enum):
	
	USER			= "user"
	ASSISTANT		= "assistant"
	SYSTEM			= "system"
	TOOL_OUTPUT		= "tool"
	TOOL_CALLING	= "assistant"