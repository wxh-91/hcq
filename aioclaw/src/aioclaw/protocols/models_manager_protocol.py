from __future__ import annotations

from abc	import ABC, abstractmethod
from typing	import Optional, List, TYPE_CHECKING


if TYPE_CHECKING:
	
	from ..models import AssistantModelConfig


class ModelsManagerProtocol(ABC):
	
	@abstractmethod
	def find_model(
		self,
		model_name	: Optional[str] = None,
		model_alias	: Optional[str] = None
	) -> AssistantModelConfig:
		
		"""找模型配置"""
		...