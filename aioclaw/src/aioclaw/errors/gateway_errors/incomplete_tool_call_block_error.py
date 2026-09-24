from __future__ import annotations
from .base_gateway_error	import BaseGatewayError


class IncompleteToolCallBlockError(BaseGatewayError):
	
	"""不完整的工具调用块。"""
	...