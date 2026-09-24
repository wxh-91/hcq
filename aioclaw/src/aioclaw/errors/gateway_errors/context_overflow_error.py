from __future__ import annotations
from .base_gateway_error	import BaseGatewayError


class ContextOverflowError(BaseGatewayError):

	"""上下文在压缩后仍超过模型限制"""
	...
