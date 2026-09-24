from __future__ import annotations

from aioverse.errors import ResponseCodeError
from aioverse.OpenAI import OpenAIClient
from aioverse.models import BaseContext, SystemContext, ToolCallingContext

from .compresser		import Compresser
from .stream_handler	import StreamHandler
from .token_tracker		import TokenTracker, token_tracker
from ..errors			import (
	GatewayBusyError,
	IncompleteToolCallBlockError,
	ModelConfigMissingError,
	RuntimeInputAdditionError,
)
from ..managers	import KeysManager, ToolsManager
from ..mixins	import (
	ContextCompressionMixin,
	MultimodalContextMixin,
	RequestHandlingMixin,
	ValueNotifier,
)
from ..models import (
	AssistantModelConfig,
	AssistantPrompt,
	AssistantSession,
	AssistantOutput,
	BaseContextsBlock,
	ClawConfig,
	ContextCompressionPrompt,
	ToolCallingContextsBlock,
)

from ..services import ContextRequestProjector

from typing import Iterator, Optional, Union

import asyncio
import logging
import time
import aiohttp


logger = logging.getLogger(__name__)


class AssistantGateway(
	ContextCompressionMixin,
	MultimodalContextMixin,
	RequestHandlingMixin,
	ValueNotifier,
):

	"""OpenAI 兼容的 Agent 网关，内置上下文压缩能力。"""

	def __init__(
		self, *,
		claw_config					: ClawConfig,
		assistant_session			: AssistantSession,
		openai_client				: Optional[OpenAIClient] = None,
		tools_manager				: Optional[ToolsManager] = None,
		assistant_prompt			: Optional[AssistantPrompt] = None,
		token_tracker				: Optional[TokenTracker] = None,
		compresser					: Optional[Compresser] = None,
		stream_handler				: Optional[StreamHandler] = None,
		context_compression_prompt	: Optional[ContextCompressionPrompt] = None,
		context_request_projector	: Optional[ContextRequestProjector] = None,
	):
		
		self.claw_config		= claw_config
		self.assistant_session	= assistant_session

		super().__init__(
			compresser=compresser,
			context_compression_prompt=context_compression_prompt,
			context_request_projector=context_request_projector,
		)

		self._openai_client		= openai_client
		self._assistant_prompt	= assistant_prompt
		self._tools_manager		= tools_manager
		self._token_tracker		= token_tracker
		self._stream_handler	= stream_handler

		self._keys_manager			: Optional[KeysManager] = None
		self._assistant_model_config: Optional[AssistantModelConfig] = None

		self._client_session					: Optional[aiohttp.ClientSession] = None
		self._request_estimated_tokens			: Optional[int] = None
		self._request_raw_estimated_tokens		: Optional[int] = None
		self._request_prompt_tokens				: Optional[int] = None
		self._request_token_calibration_scope	: Optional[str] = None
		self._generator_start_timestamp			: Optional[float] = None
		self._generator_complete_timestamp		: Optional[float] = None
		self._retry_count = 0

		self._is_round_processing		: bool	= False
		self._is_generator_processing	: bool	= False
		self._is_stopping_generator		: bool	= False

	def set_assistant_model_config(self, assistant_model_config: AssistantModelConfig):
		
		self._assistant_model_config = assistant_model_config
		
		if hasattr(self, "assistant_session"):
			self.assistant_session.contexts_status.clear_tokens_cache()

	def set_assistant_prompt(self, assistant_prompt: AssistantPrompt):
		
		self._assistant_prompt = assistant_prompt
		
		if hasattr(self, "assistant_session"):
			self.assistant_session.contexts_status.clear_tokens_cache()

	def set_keys_manager(self, keys_manager: KeysManager):
		self._keys_manager = keys_manager

	def set_tools_manager(self, tools_manager: ToolsManager):
		self._tools_manager = tools_manager

	def set_token_tracker(self, token_tracker: TokenTracker):
		
		self._token_tracker = token_tracker
		
		if hasattr(self, "assistant_session"):
			self.assistant_session.contexts_status.clear_tokens_cache()

	def set_openai_client(self, openai_client: OpenAIClient):
		self._openai_client = openai_client

	def set_stream_handler(self, stream_handler: StreamHandler):
		self._stream_handler = stream_handler

	def set_generator_start_timestamp(self, timestamp: Optional[float]):
		self._generator_start_timestamp = timestamp

	def set_generator_complete_timestamp(self, timestamp: Optional[float]):
		self._generator_complete_timestamp = timestamp



	def set_processing(self, status: bool):
		self.change("_is_round_processing", status)
	
	def set_generator_processing(self, status: bool):
		self.change("_is_generator_processing", status)
	
	def set_stop_generator(self, status: bool):
		self.change("_is_stopping_generator", status)



	def change_model(self, model_name: str) -> bool:
		
		"""切换到指定模型并重建对应的 Key 管理器。"""
		
		for assistant_model_config in self.claw_config.models_config:

			if assistant_model_config.model_name == model_name:
				self.set_assistant_model_config(assistant_model_config)
				self.set_keys_manager(KeysManager(assistant_model_config.model_keys))
				return True

		return False

	async def input(self, context: BaseContext):

		"""在网关空闲时添加外部输入。"""

		if self.is_round_processing:
			raise RuntimeInputAdditionError("不可在运行时添加新的输入")

		await self.on_adding_context(context)

	async def wait_for_round_process(self, timeout: int = 30):

		async def _wait():
			while self.is_round_processing:
				await self.wait_change()

		await asyncio.wait_for(_wait(), timeout=timeout)

	async def wait_for_generator_process(self, timeout: int = 180):

		async def _wait():
			while self.is_generator_processing:
				await self.wait_change()

		await asyncio.wait_for(_wait(), timeout=timeout)

	@property
	def token_tracker(self) -> TokenTracker:

		if self._token_tracker is None:
			return token_tracker

		return self._token_tracker

	@property
	def keys_manager(self) -> KeysManager:

		if self._keys_manager is None:
			self.set_keys_manager(KeysManager())

		return self._keys_manager

	@property
	def assistant_prompt(self) -> AssistantPrompt:

		if self._assistant_prompt is None:
			self.set_assistant_prompt(AssistantPrompt())

		return self._assistant_prompt

	@property
	def tools_manager(self) -> ToolsManager:

		if self._tools_manager is None:
			self.set_tools_manager(ToolsManager())

		return self._tools_manager

	@property
	def assistant_model_config(self) -> AssistantModelConfig:

		if self._assistant_model_config is None:

			if not self.claw_config.models_config:
				raise ModelConfigMissingError("无任何可用模型的信息")

			model_name = self.assistant_session.assistant_model_name

			if not self.change_model(model_name):
				raise ModelConfigMissingError(f"找不到会话指定的模型: {model_name}")

		return self._assistant_model_config

	@property
	def client_session(self) -> aiohttp.ClientSession:

		if self._client_session is None:
			self._client_session = aiohttp.ClientSession()

		return self._client_session

	@property
	def openai_client(self) -> OpenAIClient:

		if self._openai_client is None:
			self.set_openai_client(OpenAIClient(session=self.client_session))

		return self._openai_client

	@property
	def stream_handler(self) -> StreamHandler:

		if self._stream_handler is None:
			self._stream_handler = StreamHandler()

		return self._stream_handler

	@property
	def generator_start_timestamp(self) -> Optional[float]:
		return self._generator_start_timestamp

	@property
	def generator_complete_timestamp(self) -> Optional[float]:
		return self._generator_complete_timestamp

	@property
	def generator_elapsed_seconds(self) -> Optional[float]:
		start_timestamp = self.generator_start_timestamp
		if start_timestamp is None:
			return None

		complete_timestamp	= self.generator_complete_timestamp
		end_timestamp		= complete_timestamp if complete_timestamp is not None else time.time()

		return max(end_timestamp - start_timestamp, 0.0)

	@property
	def is_round_processing(self) -> bool:
		return self._is_round_processing

	@property
	def is_stopping_generator(self) -> bool:
		return self._is_stopping_generator

	@property
	def is_generator_processing(self) -> bool:
		return self._is_generator_processing

	async def on_adding_context(self, context: BaseContext) -> None:
		self.assistant_session.contexts_status.add_context(context)

	async def on_adding_context_block(self, context_block: BaseContextsBlock) -> None:
		self.assistant_session.contexts_status.add_context(context_block)

	async def on_tool_calling(self, context: ToolCallingContext) -> None:
		if not context.tool_calls:
			raise IncompleteToolCallBlockError("tool calling context 没有调用内容")

		contexts_block = ToolCallingContextsBlock(tool_calling=context)

		for tool_call in context.tool_calls:
			logger.info(
				"执行工具 %s，参数: %s",
				tool_call.function.name,
				tool_call.function.arguments,
			)
			tool_output = await self.tools_manager.execute_tool(tool_call)
			contexts_block.append(tool_output)

		if not contexts_block.is_complete():
			raise IncompleteToolCallBlockError("tool calling block 完整性验证失败")

		await self.on_adding_context_block(contexts_block)

	async def on_context(self, context: BaseContext) -> None:
		await self.on_adding_context(context)

	async def on_round_initiate(self) -> None:

		if self.is_round_processing:
			raise GatewayBusyError("网关正在工作")

		self.set_processing(True)

		try:

			self._request_estimated_tokens			= None
			self._request_raw_estimated_tokens		= None
			self._request_prompt_tokens				= None
			self._request_token_calibration_scope	= None
			
			self.stream_handler.reset()

			if self.assistant_model_config.model_name != self.assistant_session.assistant_model_name:
				if not self.change_model(self.assistant_session.assistant_model_name):
					raise ModelConfigMissingError(f"找不到会话指定的模型: {self.assistant_session.assistant_model_name}")

			prompt_content = self.assistant_prompt.model_dump_json()
			current_prompt = self.assistant_session.contexts_status.prompt

			if current_prompt is None or current_prompt.content != prompt_content:
				self.assistant_session.contexts_status.set_prompt(SystemContext(content=prompt_content))

			await self.on_prepare_context_before_request()

			self._request_estimated_tokens			= self.get_request_estimated_tokens()
			self._request_raw_estimated_tokens		= self.get_request_raw_estimated_tokens()
			self._request_token_calibration_scope	= self._get_token_calibration_scope(self._get_context_token_tools_json())

		except Exception:
			self.set_processing(False)
			raise

	async def on_round_complete(self) -> None:
		
		if self.is_round_processing:
			self.set_processing(False)

		contexts_status	= self.assistant_session.contexts_status
		guessed			= self._request_estimated_tokens
		raw_guessed		= self._request_raw_estimated_tokens
		actual			= self._request_prompt_tokens

		if guessed is not None and actual is not None:
			calibrate_estimate = getattr(self.token_tracker, "calibrate_estimate", None)

			if callable(calibrate_estimate) and raw_guessed is not None:
				calibrate_estimate(
					guessed=raw_guessed,
					actual=actual,
					calibration_scope=self._request_token_calibration_scope,
				)
			
			else:
				self.token_tracker.calibrate_ratio(guessed=guessed, actual=actual)

			contexts_status.clear_tokens_cache()
			
			logger.info("估算 prompt tokens: %s", guessed)
			logger.info("原始估算 prompt tokens: %s", raw_guessed)
			logger.info("实际 prompt tokens: %s", actual)

			get_summary = getattr(self.token_tracker, "get_calibration_summary", None)
			
			if callable(get_summary):
				
				ratio, offset, samples = get_summary(calibration_scope=self._request_token_calibration_scope)
				logger.info("当前 token 校准: slope=%s, offset=%s, samples=%s", ratio, offset, samples)
			
			else:
				logger.info("当前 token 校准比例: %s", self.token_tracker.ratio)

		else:
			logger.info("本轮没有可用于校准的 prompt token usage")

		logger.info("实际 total tokens: %s", contexts_status.token)

	async def on_round_error(self, exception: Exception) -> None:
		
		"""处理可重试的网络或 API 错误，且不重置重试状态"""
		
		if isinstance(exception, ResponseCodeError):
			code = exception.code

			if code == 429:
				delay = min(2 ** self._retry_count, 60)
				self._retry_count += 1
				logger.warning(f"限流 (429), {delay} 后重试 (第{self._retry_count}次)")
				await asyncio.sleep(delay)
				return

			if code == 402:
				logger.error("余额不足 (402), 停止生成器")
				self.set_stop_generator(True)
				raise

			if code == 401:
				logger.warning("认证失败 (401), 尝试切换 key")
				current = self.keys_manager.get_available_key()
				if current:
					current.is_enable = False
				self.keys_manager.uncache_key()
				return

			if 500 <= code < 600:
				delay = min(2 ** self._retry_count, 30)
				self._retry_count += 1
				logger.warning("服务器错误 ({code}), {delay} 后重试")
				await asyncio.sleep(delay)
				return

		if isinstance(exception, (asyncio.TimeoutError, aiohttp.ClientError)):
			delay = min(2 ** self._retry_count, 30)
			self._retry_count += 1
			logger.warning(f"网络错误: {exception}, {delay} 后重试")
			await asyncio.sleep(delay)
			return

		logger.error("未处理的错误: %s: %s", type(exception).__name__, exception)
		self.set_stop_generator(True)
		raise exception

	async def on_generator_initiate(self) -> None:

		self.set_generator_start_timestamp(time.time())
		self.set_generator_complete_timestamp(None)
		self.set_generator_processing(True)
		self._retry_count = 0

	async def on_generator_end(self) -> None:

		self.set_generator_complete_timestamp(time.time())
		self.set_generator_processing(False)

	async def on_generator_error(self, exception: Exception) -> None:
		raise exception

	async def on_gateway_close(self) -> None:

		if (
			self._client_session is not None
			and not self._client_session.closed
		):
			await self._client_session.close()

	async def round_call(self) -> Union[AssistantOutput, None]:
		
		round_started = False

		try:

			await self.on_round_initiate()
			round_started = True

			if self.is_stopping_generator:
				return None

			if self.assistant_model_config.support_streaming:
				return await self.on_stream_request()

			return await self.on_common_request()

		except Exception as exception:
			await self.on_round_error(exception)

		finally:
			if round_started:
				await self.on_round_complete()

	async def async_generator(self) -> Iterator[AssistantOutput]:

		await self.on_generator_initiate()

		try:
			while True:

				if result := await self.round_call():
					yield result

				if self.is_stopping_generator:
					self.set_stop_generator(False)
					break

		except GeneratorExit:
			raise

		except Exception as exception:
			await self.on_generator_error(exception)

		finally:
			await self.on_generator_end()
