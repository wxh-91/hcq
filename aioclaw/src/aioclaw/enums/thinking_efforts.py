from __future__ import annotations
from enum	import Enum


class ThinkingEfforts(str, Enum):

	NONE	= "none"
	LOW		= "low"
	MEDIUM	= "medium"
	HIGH	= "high"
	XHIGH	= "xhigh"
	MAX		= "max"