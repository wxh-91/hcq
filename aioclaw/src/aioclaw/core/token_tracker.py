from __future__ import annotations

from tiktoken		import encoding_for_model
from collections	import deque
from itertools	import combinations
from math			import ceil
from typing			import Deque, Dict, List, Optional, Tuple


class TokenTracker:

	def __init__(
		self,
		default_model		: str	= "gpt-4o",
		calibration_percent	: float	= 1.05,
		window_length		: int	= 15 # 测试得出最佳值 ~15
	):

		self._enc	= encoding_for_model(default_model)

		self._ratios				= deque(maxlen=window_length)
		self._window_length			= window_length
		self._calibration_samples	: Dict[str, Deque[Tuple[int, int]]] = {}
		self._calibration_percent	= calibration_percent

	@staticmethod
	def _get_calibration_scope(calibration_scope: Optional[str]) -> str:
		return calibration_scope or ""

	def _get_calibration_samples(
		self,
		calibration_scope: Optional[str],
		*,
		create: bool = False,
	) -> Deque[Tuple[int, int]]:

		scope = self._get_calibration_scope(calibration_scope)

		if create and scope not in self._calibration_samples:
			self._calibration_samples[scope] = deque(
				maxlen=self._window_length
			)

		return self._calibration_samples.get(scope, deque())

	def _get_calibration_parameters(
		self,
		calibration_scope: Optional[str] = None,
	) -> Tuple[float, int]:

		"""返回覆盖历史 usage 样本的线性校准参数。"""

		samples = list(self._get_calibration_samples(calibration_scope))
		if not samples:
			return 1.0, 0

		slopes = [1.0] + [
			(other_actual - actual) / (other_raw_guess - raw_guess)
			for (raw_guess, actual), (other_raw_guess, other_actual)
			in combinations(samples, 2)
			if abs(other_raw_guess - raw_guess) >= max(
				128,
				int(max(raw_guess, other_raw_guess) * 0.1),
			)
		]

		slope = max(1.0, max(slopes))
		offset = max(
			0.0,
			max(actual - raw_guess * slope for raw_guess, actual in samples),
		)

		return slope, ceil(offset)

	@property
	def ratio(self) -> float:

		"""返回历史实际值/估算值的平均比值；没有数据时返回 1.0。"""

		if not self._ratios:
			return 1.0

		ratio = sum(self._ratios) / len(self._ratios)

		return ratio if ratio > 0 else 1.0

	def get_calibration_cache_key(
		self,
		*,
		calibration_scope: Optional[str] = None,
	) -> str:

		"""返回可用于上下文估算缓存的当前校准版本。"""

		ratio, offset = self._get_calibration_parameters(calibration_scope)
		return f"{ratio:.12f}:{offset}"

	def get_calibration_summary(
		self,
		*,
		calibration_scope: Optional[str] = None,
	) -> Tuple[float, int, int]:

		"""返回当前作用域的斜率、固定开销和样本数量。"""

		ratio, offset = self._get_calibration_parameters(calibration_scope)
		samples = self._get_calibration_samples(calibration_scope)

		return ratio, offset, len(samples)

	def reset_calibration(
		self,
		*,
		calibration_scope: Optional[str] = None,
	):

		"""清空指定作用域的校准样本，避免上下文形态变化后沿用旧样本。"""

		if calibration_scope is None:
			self._calibration_samples.clear()
		else:
			scope = self._get_calibration_scope(calibration_scope)
			self._calibration_samples.pop(scope, None)

		# 旧版比例队列没有作用域，无法安全地保留其中一部分。
		self._ratios.clear()

	def calibrate_estimate(
		self,
		guessed: int,
		actual: int,
		*,
		calibration_scope: Optional[str] = None,
	):

		"""记录原始本地估算与供应商 prompt usage 的对应关系。"""

		if guessed > 0 and actual > 0:
			samples = self._get_calibration_samples(
				calibration_scope,
				create=True,
			)
			samples.append((guessed, actual))

	def calibrate_ratio(self, guessed: int, actual: int):

		"""兼容旧接口，保留比例校准并记录默认作用域样本。"""

		if guessed > 0 and actual > 0:
			self._ratios.append(actual / guessed)

		self.calibrate_estimate(guessed=guessed, actual=actual)

	def _get_tokens_by_tiktoken(self, contents: List[str]) -> int:

		"""通过 tiktoken 估算 token 数量"""

		tokens = (len(self._enc.encode(c, disallowed_special=())) for c in contents)
		return sum(tokens)

	def estimate_raw(self, contents: List[str]) -> int:

		"""返回未应用供应商 usage 校准的本地估算。"""

		guess_token = self._get_tokens_by_tiktoken(contents)	# 原始估算
		guess_token = guess_token * self._calibration_percent	# 保守偏大系数

		return int(max(guess_token, 0)) # 确保返回自然数

	def estimate_with_scope(
		self,
		contents: List[str],
		*,
		calibration_scope: Optional[str] = None,
	) -> int:

		"""按作用域应用线性校准，避免固定开销放大大附件。"""

		raw_guess = self.estimate_raw(contents)
		ratio, offset = self._get_calibration_parameters(calibration_scope)

		return int(max(ceil(raw_guess * ratio + offset), 0))

	def estimate(self, contents: List[str]) -> int:

		guess_token = self._get_tokens_by_tiktoken(contents)	# 原始估算
		guess_token = guess_token * self._calibration_percent	# 保守偏大系数
		guess_token = guess_token * self.ratio					# 比例校准（替代差值）

		return int(max(guess_token, 0)) # 确保返回自然数


# 全局单例
token_tracker = TokenTracker()
