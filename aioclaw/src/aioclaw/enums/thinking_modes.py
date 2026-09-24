from __future__ import annotations
from enum	import Enum


class ThinkingModes(str, Enum):
	
	DISABLED	= "disabled"
	ENABLED		= "enabled"
	ADAPTIVE	= "adaptive"