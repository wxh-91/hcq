from __future__ import annotations

from enum import Enum


class FinishReasons(str, Enum):
	
	TOOL_CALLING	= "tool_calls"
	STOP			= "stop"
	LENGTH			= "length"
