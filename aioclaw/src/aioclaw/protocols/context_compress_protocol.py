from __future__ import annotations

from abc	import ABC, abstractmethod
from typing	import TYPE_CHECKING


if TYPE_CHECKING:

	from ..models	import ContextCompressResult


class ContextCompressProtocol(ABC):

	@abstractmethod
	async def compress(self, contexts, **kwargs) -> ContextCompressResult:

		"""对传入上下文列表做本地处理，不负责阈值判断或 API 请求"""
		...
