from __future__ import annotations
from .base_gateway_error	import BaseGatewayError


class GatewayBusyError(BaseGatewayError):
	
	"""网关正在工作"""
	...