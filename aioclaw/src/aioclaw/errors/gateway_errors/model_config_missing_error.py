from __future__ import annotations
from .base_gateway_error	import BaseGatewayError


class ModelConfigMissingError(BaseGatewayError):
	
	"""模型配置缺失"""
	...