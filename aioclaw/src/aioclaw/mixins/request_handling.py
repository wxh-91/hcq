from __future__ import annotations

from aioverse.OpenAI import OpenAIClient
from aioverse.models import (
	AssistantContext,
	Request,
	Response,
	StreamChunk,
	ToolCallingContext,
)

from ..enums import FinishReasons
from ..errors import IncompleteToolCallBlockError, UnknownFinishReasonError
from ..factories import contexts_factory
from ..models import AssistantOutput
from ..utils import generate_assistant_output_by_response

from typing import Optional, Union

import logging


logger = logging.getLogger(__name__)


class RequestHandlingMixin:

	"""处理 OpenAI 兼容请求构建、普通响应和流式响应。"""

	async def on_build_request(self) -> Request:

		request_model	= Request(url=self.assistant_model_config.api_url)
		assistant_key	= self.keys_manager.get_available_key()

		request_model.set_header("Authorization", assistant_key.key)
		request_model.set_header("Content-Type", "application/json")
		request_model.set_body("model", self.assistant_model_config.model_name)
		request_model.set_body("messages", self._get_request_messages())
		request_model.set_body("stream", self.assistant_model_config.support_streaming)

		if self.assistant_model_config.support_tool:
			request_model.set_body("tools", self.tools_manager.to_list())

		if self.assistant_model_config.support_thinking:
			request_model.set_body("thinking", {
				"type": self.assistant_session.assistant_think_mode
			})
			request_model.set_body(
				"reasoning_effort",
				self.assistant_session.assistant_think_effort,
			)

		return request_model

	async def on_request(self, request: Request) -> Response:
		return await self.openai_client.call(request=request)

	async def on_response(self, response: Response) -> None:

		if not response.choices:
			raise UnknownFinishReasonError("模型响应没有 choices")

		choice			= response.choices[0]
		message			= choice.message
		context			= contexts_factory.dispatcher(message.model_dump())
		finish_reason	= choice.finish_reason

		if response.usage is not None:
			self.assistant_session.contexts_status.set_token(response.usage.total_tokens)
			self._request_prompt_tokens = response.usage.prompt_tokens

		if finish_reason == FinishReasons.TOOL_CALLING:

			if not isinstance(context, ToolCallingContext):
				raise IncompleteToolCallBlockError(
					"tool_calls finish_reason 的消息格式无效"
				)

			await self.on_tool_calling(context)
			return None

		if finish_reason in (FinishReasons.STOP, FinishReasons.LENGTH):
			if getattr(message, "tool_calls", None):
				raise IncompleteToolCallBlockError(
					f"finish_reason={finish_reason} 时 tool_calls 不完整"
				)

			await self.on_context(context)

			if finish_reason == FinishReasons.LENGTH:
				logger.warning("模型输出因达到长度上限而截断")

			if self.is_generator_processing:
				self.set_stop_generator(True)

			return None

		raise UnknownFinishReasonError(f"未处理的finish_reason: {finish_reason}")

	async def on_stream_chunk(self, chunk: StreamChunk) -> Optional[AssistantOutput]:

		if not chunk.choices:
			return None

		choice = chunk.choices[0]
		self.stream_handler.merge(choice.delta)

		if choice.finish_reason is None:
			return None

		finish_reason	= choice.finish_reason
		handler			= self.stream_handler

		if finish_reason == FinishReasons.TOOL_CALLING:
			if not handler._tool_calls:
				raise IncompleteToolCallBlockError(
					"tool_calls finish_reason 没有携带调用内容"
				)

			await self.on_tool_calling(handler.build_tool_calling_context())
			handler.reset()

			return None

		if finish_reason in (FinishReasons.STOP, FinishReasons.LENGTH):
			if handler._tool_calls:
				raise IncompleteToolCallBlockError(
					f"finish_reason={finish_reason} 时 tool_calls 不完整"
				)

			assistant_ctx = AssistantContext(
				content				= handler._content,
				reasoning_content	= handler._reasoning,
			)
			await self.on_context(assistant_ctx)

			if finish_reason == FinishReasons.LENGTH:
				logger.warning("流式模型输出因达到长度上限而截断")

			if self.is_generator_processing:
				self.set_stop_generator(True)

			return handler.flush(finish_reason=finish_reason)

		raise UnknownFinishReasonError(f"未处理的finish_reason: {finish_reason}")

	async def on_build_output(self, response: Response) -> Optional[AssistantOutput]:
		return generate_assistant_output_by_response(response)

	async def on_stream_request(self) -> Union[AssistantOutput, None]:

		request			= await self.on_build_request()
		contexts_status	= self.assistant_session.contexts_status

		async for chunk in self.openai_client.call_stream(request=request):

			if self.is_stopping_generator:
				logger.info("流式请求被 stop 信号中断")
				return None

			if chunk.usage is not None:
				contexts_status.set_token(chunk.usage.total_tokens)
				self._request_prompt_tokens = chunk.usage.prompt_tokens

			if output := await self.on_stream_chunk(chunk):
				return output

		if self.stream_handler.is_empty:
			return None

		if self.stream_handler._tool_calls:
			raise IncompleteToolCallBlockError("流式连接在 tool call 完成前关闭")

		assistant_ctx = AssistantContext(
			content				= self.stream_handler._content,
			reasoning_content	= self.stream_handler._reasoning,
		)
		await self.on_context(assistant_ctx)

		if self.is_generator_processing:
			self.set_stop_generator(True)

		return self.stream_handler.flush()

	async def on_common_request(self) -> Union[AssistantOutput, None]:

		request		= await self.on_build_request()
		response	= await self.on_request(request)

		await self.on_response(response)
		return await self.on_build_output(response)
