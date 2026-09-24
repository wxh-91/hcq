from __future__ import annotations

from typing import Protocol, TYPE_CHECKING


if TYPE_CHECKING:

	from ..models import AssistantModelConfig, AssistantSession


class MultimodalContextHostProtocol(Protocol):

	"""多模态投影适配器从 Gateway 读取的最小宿主契约。"""

	@property
	def assistant_model_config(self) -> "AssistantModelConfig":
		...

	@property
	def assistant_session(self) -> "AssistantSession":
		...
