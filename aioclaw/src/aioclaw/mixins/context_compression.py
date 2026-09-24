from __future__ import annotations

from aioverse.models import BaseContext, SystemContext, UserContext, Request, Response

from ..models import ContextCompressResult, ContextCompressionPrompt
from ..errors import ContextOverflowError, UnknownFinishReasonError
from ..enums import FinishReasons

from copy import deepcopy
from typing import Optional, List, Any, Tuple, Dict

import hashlib
import logging
import orjson


logger = logging.getLogger(__name__)


class ContextCompressionMixin:

	"""为网关提供上下文估算、本地压缩和 API 摘要能力。"""

	def __init__(
		self, *,
		compresser					: Optional[Any] = None,
		context_compression_prompt	: Optional[ContextCompressionPrompt] = None,
		**kwargs,
	):
		super().__init__(**kwargs)

		self._compresser					= compresser
		self._context_compression_prompt	= ContextCompressionPrompt() if context_compression_prompt is None else context_compression_prompt
		
		self._is_compresser				= False
		self._is_compressing_context	= False

	@staticmethod
	def _validate_context_compression_keep_contexts(count: int) -> int:
		
		if isinstance(count, bool) or not isinstance(count, int) or count < 1:
			raise ValueError("context_compression_keep_contexts 必须是大于等于 1 的整数")
		
		return count

	@staticmethod
	def _validate_context_compression_max_tokens(max_tokens: Optional[int]) -> Optional[int]:
		
		if max_tokens is None:
			return None
		
		if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
			raise ValueError("context_compression_max_tokens 必须是正整数或 None")
		
		return max_tokens

	def set_compresser(self, compresser: Optional[Any]):
		self._compresser = compresser

	def set_context_compression_prompt(self, prompt: ContextCompressionPrompt):
		self._context_compression_prompt = prompt

	def set_context_compression_keep_contexts(self, count: int):
		count = self._validate_context_compression_keep_contexts(count)
		self.claw_config.context_compression_keep_contexts = count

	def set_context_compression_max_tokens(self, max_tokens: Optional[int]):
		max_tokens = self._validate_context_compression_max_tokens(max_tokens)
		self.claw_config.context_compression_max_tokens = max_tokens

	def set_compresser_processing(self, status: bool):
		self.change("_is_compresser", status)

	@property
	def compresser(self) -> Optional[Any]:
		return self._compresser

	@property
	def context_compression_prompt(self) -> ContextCompressionPrompt:
		return self._context_compression_prompt

	@property
	def context_compression_keep_contexts(self) -> int:
		return self.claw_config.context_compression_keep_contexts

	@property
	def context_compression_max_tokens(self) -> Optional[int]:
		return self.claw_config.context_compression_max_tokens

	@property
	def is_compresser(self) -> bool:
		return self._is_compresser

	def _get_context_token_tools_json(self) -> Optional[str]:
		
		"""返回普通 Agent 请求中包含的工具 Schema。"""
		
		if self.assistant_model_config.support_tool is False:
			return None

		tools = self.tools_manager.to_list()
		return orjson.dumps(tools).decode() if tools else None

	def _get_token_calibration_scope(
		self,
		tools_json: Optional[str] = None,
	) -> str:

		"""为同一供应商请求形态隔离 token usage 校准样本"""

		config = self.assistant_model_config
		scope_parts = (
			config.api_url,
			config.model_name,
			str(config.support_tool),
			str(config.support_thinking),
			str(config.support_streaming),
			str(config.support_image),
			str(config.support_audio),
			str(config.support_video),
			str(getattr(config, "tool_output_image_mode", "tool")),
			str(self.assistant_session.assistant_think_mode),
			str(self.assistant_session.assistant_think_effort),
			hashlib.sha256((tools_json or "").encode()).hexdigest(),
		)

		return hashlib.sha256("\0".join(scope_parts).encode()).hexdigest()

	def _get_token_calibration_cache_key(self, tools_json: Optional[str]) -> str:

		tracker = self.token_tracker
		get_cache_key = getattr(tracker, "get_calibration_cache_key", None)

		if callable(get_cache_key):
			return get_cache_key(
				calibration_scope=self._get_token_calibration_scope(tools_json)
			)

		return f"{getattr(tracker, 'ratio', 1.0):.12f}"

	def _estimate_tokens(
		self,
		contents: List[str],
		*,
		tools_json: Optional[str] = None,
		raw: bool = False,
	) -> int:

		tracker = self.token_tracker

		if raw:
			estimate_raw = getattr(tracker, "estimate_raw", None)
			if callable(estimate_raw):
				return estimate_raw(contents)

		estimate_with_scope = getattr(tracker, "estimate_with_scope", None)
		if callable(estimate_with_scope):
			return estimate_with_scope(
				contents,
				calibration_scope=self._get_token_calibration_scope(tools_json),
			)

		return tracker.estimate(contents)

	def _get_context_token_cache_key(self, tools_json: Optional[str]) -> str:
		"""为当前模型、能力、预算和工具 Schema 构造缓存键。"""
		tools_hash = hashlib.sha256((tools_json or "").encode()).hexdigest()
		config = self.assistant_model_config

		return ":".join(
			(
				config.model_name,
				getattr(config, "tool_output_image_mode", "tool"),
				str(getattr(config, "support_image", False)),
				str(getattr(config, "support_audio", False)),
				str(getattr(config, "support_video", False)),
				str(config.max_context_length),
				str(getattr(config, "reserved_completion_tokens", 0)),
				str(getattr(config, "context_safety_margin", 0)),
				str(id(self.token_tracker)),
				self._get_token_calibration_cache_key(tools_json),
				tools_hash,
			)
		)

	def _estimate_current_context_tokens(
		self,
		tools_json: Optional[str],
		*,
		raw: bool = False,
	) -> int:
		
		"""估算下一次普通 Agent 请求的序列化输入。"""
		contexts_json	= orjson.dumps(self._get_request_messages()).decode()
		contents		= [contexts_json]

		if tools_json is not None:
			contents.append(tools_json)

		return self._estimate_tokens(
			contents,
			tools_json=tools_json,
			raw=raw,
		)

	def _reset_token_calibration(self) -> None:

		"""上下文结构变化后清空当前请求形态的旧校准样本。"""

		reset_calibration = getattr(self.token_tracker, "reset_calibration", None)
		if callable(reset_calibration):
			reset_calibration(
				calibration_scope=self._get_token_calibration_scope(
					self._get_context_token_tools_json()
				),
			)

		self.assistant_session.contexts_status.clear_tokens_cache()

	def get_request_estimated_tokens(self) -> int:
		return self.estimated_context_tokens

	def get_request_raw_estimated_tokens(self) -> int:

		"""返回当前请求的未校准本地估算，供 usage 校准使用。"""

		if not callable(getattr(self.token_tracker, "estimate_raw", None)):
			return self.get_request_estimated_tokens()

		tools_json = self._get_context_token_tools_json()
		return self._estimate_current_context_tokens(
			tools_json,
			raw=True,
		)

	@property
	def estimated_context_tokens(self) -> int:
		
		"""返回当前普通请求的缓存估算值。"""
		
		contexts_status	= self.assistant_session.contexts_status
		tools_json		= self._get_context_token_tools_json()
		cache_key		= self._get_context_token_cache_key(tools_json)
		cached_tokens	= contexts_status.get_cached_tokens(cache_key)

		if cached_tokens is not None:
			return cached_tokens

		estimated_tokens = self._estimate_current_context_tokens(tools_json)
		contexts_status.set_cached_tokens(cache_key, estimated_tokens)
		
		return estimated_tokens

	@property
	def effective_context_limit(self) -> int:
		
		"""返回扣除输出预留和安全余量后的输入预算。"""
		config		= self.assistant_model_config
		reserved	= getattr(config, "reserved_completion_tokens", 0)
		margin		= getattr(config, "context_safety_margin", 0)
		
		return max(config.max_context_length - reserved - margin, 1)

	@property
	def is_context_cleanup_required(self) -> bool:
		
		threshold = min(self.assistant_model_config.cleanup_threshold, self.effective_context_limit)
		
		return self.estimated_context_tokens >= threshold

	@property
	def is_context_overflow(self) -> bool:
		return self.estimated_context_tokens >= self.effective_context_limit

	async def on_select_contexts_for_compression(self) -> Tuple[List[Any], List[Any]]:
		
		"""选择较旧的顶层上下文项，并保持上下文块的原子性。"""
		
		contexts = list(self.assistant_session.contexts_status.contexts)

		if len(contexts) <= 1:
			return [], contexts

		keep_count = min(self.context_compression_keep_contexts, len(contexts) - 1)
		
		return contexts[:-keep_count], contexts[-keep_count:]

	def _build_compression_memory_payload(self) -> Optional[Dict[str, Any]]:
		
		memory = self.assistant_session.contexts_status.memory
		
		if memory is None:
			return None

		return memory.model_dump(mode="json", exclude_none=True)

	def _build_compression_context_payload(self, index: int, context: Any) -> Dict[str, Any]:
		
		return {
			"index"		: index,
			"context"	: context.model_dump(mode="json", exclude_none=True)
		}

	def _build_compression_payload(self, contexts: List[Any]) -> str:
		
		"""将历史序列化为数据，确保不会执行历史工具调用。"""
		
		context_payloads = []
		
		for index, context in enumerate(contexts, start=1):
			context_payloads.append(self._build_compression_context_payload(index, context))

		payload = {
			"memory"	: self._build_compression_memory_payload(),
			"contexts"	: context_payloads,
		}
		payload_json = orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()
		
		return f"{self.context_compression_prompt.payload_prefix}\n\n{payload_json}"

	def _build_compression_messages(self, contexts: List[Any]) -> List[BaseContext]:
		return [
			SystemContext(content=self.context_compression_prompt.system_prompt),
			UserContext(content=self._build_compression_payload(contexts)),
		]

	def _build_compression_messages_data(self, contexts: List[Any]) -> List[Dict[str, Any]]:
		return [
			context.model_dump(mode="json", exclude_none=True)
			for context in self._build_compression_messages(contexts)
		]

	def _estimate_compression_input_tokens(self, contexts: List[Any]) -> int:
		
		"""估算独立摘要请求的输入部分。"""
		request_body = {
			"model"		: self.assistant_model_config.model_name,
			"messages"	: self._build_compression_messages_data(contexts),
			"stream"	: False,
		}
		return self._estimate_tokens(
			[orjson.dumps(request_body).decode()],
			raw=True,
		)


	@property
	def compression_output_tokens(self) -> Optional[int]:
		
		"""返回适配供应商限制的摘要输出预算"""
		
		configured = self.context_compression_max_tokens	
		if configured is None:
			return None

		margin = getattr(self.assistant_model_config, "context_safety_margin", 0)
		return min(
			configured,
			max(self.assistant_model_config.max_context_length - margin - 1, 1),
		)

	@property
	def compression_input_limit(self) -> int:
		
		"""返回摘要请求可使用的输入预算。"""
		config			= self.assistant_model_config
		output_budget	= self.compression_output_tokens
		
		if output_budget is None:
			output_budget = getattr(config, "reserved_completion_tokens", 0)
		
		margin = getattr(config, "context_safety_margin", 0)
		return max(config.max_context_length - output_budget - margin, 1)

	async def on_build_compression_request(self, contexts: List[Any]) -> Request:
		
		"""构造独立的非流式、无工具摘要请求。"""
		config					= self.assistant_model_config
		request_model			= Request(url=config.api_url)

		request_model.set_header("Authorization", self.keys_manager.get_available_key().key)
		request_model.set_header("Content-Type", "application/json")
		request_model.set_body("model", config.model_name)
		request_model.set_body(
			"messages",
			self._build_compression_messages_data(contexts),
		)
		request_model.set_body("stream", False)

		if self.compression_output_tokens is not None:
			request_model.set_body(
				"max_tokens",
				self.compression_output_tokens,
			)

		return request_model

	async def on_compression_request(self, request: Request) -> Response:
		return await self.openai_client.call(request=request)

	async def on_parse_compression_response(self, response: Response) -> str:
		
		if not response.choices:
			raise UnknownFinishReasonError("上下文压缩响应没有 choices")

		choice = response.choices[0]
		if choice.finish_reason != FinishReasons.STOP:
			raise UnknownFinishReasonError(f"上下文压缩响应的 finish_reason 不支持: {choice.finish_reason}")

		message = choice.message
		if getattr(message, "tool_calls", None):
			raise UnknownFinishReasonError("上下文压缩响应不允许包含 tool_calls")

		content = message.content
		if not isinstance(content, str) or not content.strip():
			raise ValueError("上下文压缩响应内容为空或不是 Markdown 文本")
		return content.strip()

	def _get_compressed_contexts(self, result: Any) -> Optional[List[Any]]:
		
		if isinstance(result, ContextCompressResult):
			return result.contexts
		
		if isinstance(result, list):
			return result

	def _commit_local_compression(
		self,
		contexts_status		: Any,
		old_contexts		: List[Any],
		compressed_contexts	: List[Any],
		old_tokens			: int,
	) -> bool:
		
		try:
			contexts_status.replace_contexts(compressed_contexts)
			new_tokens = self.get_request_raw_estimated_tokens()
		
		except Exception:
			contexts_status.replace_contexts(old_contexts)
			raise

		if new_tokens >= old_tokens:
			contexts_status.replace_contexts(old_contexts)
			logger.info("本地上下文压缩没有减少 token，已回滚")
			return False

		self._reset_token_calibration()
		new_tokens = self.estimated_context_tokens

		logger.info("本地上下文压缩完成: %s -> %s", old_tokens, new_tokens)
		return True

	async def on_local_context_compress(self) -> bool:
		
		"""对上下文副本运行可选的本地变换，并原子提交结果。"""
		
		if self.compresser is None:
			return False

		try:
			
			self.set_compresser_processing(True)
			logger.info("开始上下文压缩")
			
			contexts_status		= self.assistant_session.contexts_status
			old_contexts		= list(contexts_status.contexts)
			old_tokens			= self.get_request_raw_estimated_tokens()
			working_contexts	= deepcopy(old_contexts)
			result				= await self.compresser.compress(working_contexts)
			compressed_contexts	= self._get_compressed_contexts(result)

			if compressed_contexts is None:
				return False

			return self._commit_local_compression(
				contexts_status,
				old_contexts,
				compressed_contexts,
				old_tokens,
			)
		
		finally:
			self.set_compresser_processing(False)

	def _restore_compression_snapshot(
		self,
		contexts_status	: Optional[Any],
		old_contexts	: Optional[List[Any]],
		old_memory		: Optional[SystemContext],
	) -> None:
		
		if contexts_status is None or old_contexts is None:
			return

		contexts_status.set_memory(old_memory)
		contexts_status.replace_contexts(old_contexts)

	def _validate_compression_input(self, contexts: List[Any]) -> None:
		
		compression_input_tokens = self._estimate_compression_input_tokens(contexts)
		compression_input_limit = self.compression_input_limit
		
		if compression_input_tokens < compression_input_limit:
			return

		raise ContextOverflowError(
			f"上下文压缩请求本身超过有效输入预算，当前估算 token: "
			f"{compression_input_tokens}，有效上限: "
			f"{compression_input_limit}"
		)

	async def _request_compression_summary(self, contexts: List[Any]) -> str:
		
		self._validate_compression_input(contexts)
		
		request		= await self.on_build_compression_request(contexts)
		response	= await self.on_compression_request(request)
		
		return await self.on_parse_compression_response(response)

	def _build_compression_memory_context(self, summary: str) -> SystemContext:
		return SystemContext(
			content=(
				f"{self.context_compression_prompt.memory_prefix}\n\n"
				f"{summary}"
			)
		)

	def _commit_api_compression(
		self,
		contexts_status	: Any,
		contexts_to_keep: List[Any],
		old_tokens		: int,
		summary			: str,
		old_contexts	: List[Any],
		old_memory		: Optional[SystemContext],
	) -> bool:
		
		memory = self._build_compression_memory_context(summary)
		contexts_status.set_memory(memory)
		contexts_status.replace_contexts(contexts_to_keep)
		new_tokens = self.get_request_raw_estimated_tokens()

		if new_tokens >= old_tokens:
			
			self._restore_compression_snapshot(contexts_status, old_contexts, old_memory)
			logger.warning("上下文压缩没有减少 token: %s -> %s，已回滚", old_tokens, new_tokens)
			
			return False

		self._reset_token_calibration()
		new_tokens = self.estimated_context_tokens

		logger.info("API 上下文压缩完成: %s -> %s", old_tokens, new_tokens)
		return True

	async def compress_contexts(self) -> bool:
		
		"""通过 API 将旧历史摘要为 Markdown Memory。"""
		
		if self._is_compressing_context:
			return False

		self._is_compressing_context	= True
		contexts_status					= None
		old_contexts					= None
		old_memory						= None

		try:
			
			self.set_compresser_processing(True)
			logger.info("开始上下文压缩")
			
			contexts_status	= self.assistant_session.contexts_status
			old_contexts	= list(contexts_status.contexts)
			old_memory		= contexts_status.memory

			contexts_to_compress, contexts_to_keep = await self.on_select_contexts_for_compression()
			
			if not contexts_to_compress:
				return False

			old_tokens	= self.get_request_raw_estimated_tokens()
			summary		= await self._request_compression_summary(contexts_to_compress)
			
			return self._commit_api_compression(contexts_status, contexts_to_keep, old_tokens, summary, old_contexts, old_memory)

		except Exception:
			
			self._restore_compression_snapshot(contexts_status, old_contexts, old_memory)
			
			raise

		finally:
			self._is_compressing_context = False
			self.set_compresser_processing(False)

	def _raise_context_overflow_if_needed(self) -> None:
		
		if not self.is_context_overflow:
			return

		raise ContextOverflowError(
			f"上下文超过有效模型限制，当前估算 token: "
			f"{self.estimated_context_tokens}，有效上限: "
			f"{self.effective_context_limit}"
		)

	async def _try_api_context_compress(self) -> None:
		
		if not self.is_context_cleanup_required:
			return

		try:
			await self.compress_contexts()
		
		except Exception as exception:
			logger.warning("API 上下文压缩失败，本轮保留原上下文: %s", exception)

	async def on_prepare_context_before_request(self) -> None:
		
		"""先应用本地硬限制保护，再尝试可选的 API 清理。"""
		
		parent_hook = getattr(super(), "on_prepare_context_before_request", None)
		if parent_hook is not None:
			await parent_hook()

		if self.is_context_overflow:
			await self.on_local_context_compress()
		
		self._raise_context_overflow_if_needed()

		await self._try_api_context_compress()

		if self.is_context_overflow:
			await self.on_local_context_compress()
		
		self._raise_context_overflow_if_needed()

