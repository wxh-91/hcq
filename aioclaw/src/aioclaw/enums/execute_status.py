from __future__ import annotations
from enum	import Enum


class ExecuteStatus(str, Enum):
	
	pending	: str = "Pending"
	finish	: str = "Finish"
	hanging	: str = "Hanging"
	error	: str = "Error"