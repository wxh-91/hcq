from __future__ import annotations

from ..models			import ContextCompressResult
from ..protocols		import ContextCompressProtocol

from typing	import Any, List


class Compresser(ContextCompressProtocol):

	"""本地上下文列表压缩器基类"""

	def __init__(self):

		"""压缩策略由子类按需实现"""
		...

	async def _compress(
		self,
		contexts: List[Any],
		**kwargs
	) -> List[Any]:

		"""默认不改变上下文，具体策略由子类实现"""

		return contexts

	async def compress(
		self,
		contexts: List[Any],
		**kwargs
	) -> ContextCompressResult:

		"""只对传入列表做本地处理，不判断阈值、不请求 API"""

		compressed_contexts = await self._compress(contexts, **kwargs)
		is_compressed = compressed_contexts is not contexts

		return ContextCompressResult(
			is_out=False,
			is_compressed=is_compressed,
			contexts=compressed_contexts
		)
