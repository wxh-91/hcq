from __future__ import annotations
from .base_gateway_error	import BaseGatewayError


class RuntimeInputAdditionError(BaseGatewayError):
	
	"""在网关运行时尝试添加外部输入上下文"""
	...