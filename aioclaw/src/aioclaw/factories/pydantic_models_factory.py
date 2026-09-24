from __future__ import annotations
from pydantic	import BaseModel

from ..protocols	import FactoryProtocol

import logging

from typing		import Type, Self, Dict, Optional, Any

logger = logging.getLogger(__name__)


class PydanticModelsFactory(FactoryProtocol):

	"""Pydantic 模型工厂。"""

	def __init__(self):
		self.registered_models = []

	@staticmethod
	def _static_validate(model_class: Type, data: Dict[str, Any]) -> bool:

		"""对模型字段进行静态检查，减少 try-except 开销。"""

		model_field = model_class.model_fields

		for field_name, field_info in model_field.items():
			if field_info.is_required() and field_name not in data:
				logger.debug(f"缺少 {field_name}")
				return False

		return True

	def register(self, class_: Type, priority: int = 1) -> Self:

		if class_ not in self.registered_models:
			self.registered_models.insert(priority, class_)

		return self

	def dispatcher(self, data: Dict[str, Any]) -> Optional[BaseModel]:

		for model_factory in self.registered_models:

			logger.debug(f"当前: {model_factory.__name__}")
			if "assistant_model_name" in data:
				logger.debug(data["assistant_model_name"])

			if self._static_validate(model_factory, data) is False:
				continue

			try:
				return model_factory.model_validate(data)
			except Exception as e:
				logger.debug(f"模型验证失败: {e}")

		return None
