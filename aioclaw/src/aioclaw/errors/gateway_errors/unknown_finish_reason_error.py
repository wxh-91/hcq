from __future__ import annotations
from .base_gateway_error	import BaseGatewayError


class UnknownFinishReasonError(BaseGatewayError):
	
	"""未知或未处理的 finish_reason。"""
	...