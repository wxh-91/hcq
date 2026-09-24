from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest

from pydantic import ValidationError

from aioverse.models import (
	AudioInputSegment,
	AudioUrlSegment,
	ImageBase64Segment,
	ImageUrlSegment,
	Response,
	SystemContext,
	TextSegment,
	ToolCallingContext,
	ToolOutputContext,
	UnknownSegment,
	VideoInputSegment,
	VideoUrlSegment,
	UserContext,
)

from aioclaw.core import AssistantGateway
from aioclaw.models import (
	AssistantPrompt,
	AssistantKey,
	AssistantModelConfig,
	AssistantSession,
	BaseContextsBlock,
	ClawConfig,
	ContextsStatus,
	ToolCallingContextsBlock,
)


class RecordingTokenTracker:

	def __init__(self):
		self.ratio = 1.0
		self.contents = None
		self.estimate_calls = 0

	def estimate(self, contents):
		self.contents = contents
		self.estimate_calls += 1
		return 1

	def calibrate_ratio(self, guessed, actual):
		...


class RecordingResponseClient:

	def __init__(self):
		self.requests = []

	async def call(self, *, request):
		self.requests.append(request)
		return Response.model_validate({
			"id": "response-1",
			"created": 1,
			"model": "demo",
			"object": "chat.completion",
			"choices": [{
				"index": 0,
				"finish_reason": "stop",
				"message": {
					"role": "assistant",
					"content": "request accepted",
					"reasoning_content": "",
				},
			}],
			"usage": {
				"completion_tokens": 1,
				"prompt_tokens": 10,
				"total_tokens": 11,
			},
		})


class ToolOutputImageAdapterTests(unittest.TestCase):

	def _gateway(
		self,
		contexts,
		*,
		mode="tool",
		support_image=False,
		support_audio=False,
		support_video=False,
		token_tracker=None,
	):
		model_config = AssistantModelConfig(
			api_url="https://example.invalid/v1/chat/completions",
			model_name="demo",
			model_keys=[AssistantKey(key="Bearer test")],
			max_context_length=100000,
			cleanup_threshold=90000,
			support_streaming=False,
			support_image=support_image,
			support_audio=support_audio,
			support_video=support_video,
			tool_output_image_mode=mode,
		)
		return AssistantGateway(
			claw_config=ClawConfig(models_config=[model_config]),
			assistant_session=AssistantSession(
				assistant_model_name="demo",
				contexts_status=ContextsStatus(contexts=contexts),
			),
			token_tracker=token_tracker,
		)

	def _tool_calling_context(self, *tool_calls):
		return ToolCallingContext.model_validate({
			"role": "assistant",
			"content": "",
			"reasoning_content": "",
			"tool_calls": [{
				"id": tool_id,
				"type": "function",
				"function": {
					"name": "view_photo",
					"arguments": "{}",
				},
			} for tool_id in tool_calls],
		})

	def _image_context_block(self):
		tool_calling = self._tool_calling_context("call-1", "call-2")
		return ToolCallingContextsBlock(
			tool_calling=tool_calling,
			tool_outputs=[
				ToolOutputContext(
					tool_call_id="call-1",
					content=[
						TextSegment(text="第一张图片附件已添加到工具结果上下文。"),
						ImageBase64Segment(
							data="aW1hZ2UtMQ==",
							media_type="image/png",
						),
					],
				),
				ToolOutputContext(
					tool_call_id="call-2",
					content=[
						TextSegment(text="第二张图片附件已添加到工具结果上下文。"),
						ImageUrlSegment(
							url="https://example.com/image-2.webp",
							detail="high",
						),
					],
				),
			],
		)

	@staticmethod
	def _segment_types(message):
		content = message["content"]
		if not isinstance(content, list):
			return []

		return [segment["type"] for segment in content]

	def test_tool_mode_keeps_original_tool_image_messages_when_supported(self):
		gateway = self._gateway([
			UserContext(content="请查看图片"),
			self._image_context_block(),
		], support_image=True)

		raw_messages = gateway.assistant_session.contexts_status.to_list()
		request_messages = gateway._get_request_messages()

		self.assertEqual(request_messages, raw_messages)
		self.assertEqual(request_messages[-1]["role"], "tool")
		self.assertEqual(request_messages[-1]["content"][1]["type"], "image_url")
		self.assertEqual(request_messages[-2]["content"][1]["type"], "image")

	def test_disabled_image_input_is_removed_only_from_request(self):
		user_image = ImageBase64Segment(
			data="dXNlci1pbWFnZQ==",
			media_type="image/png",
		)
		gateway = self._gateway([
			UserContext(content=[
				TextSegment(text="请查看图片"),
				user_image,
			]),
			self._image_context_block(),
		])

		raw_messages = gateway.assistant_session.contexts_status.to_list()
		request_messages = gateway._get_request_messages()

		self.assertEqual(
			self._segment_types(request_messages[0]),
			["text", "text"],
		)
		self.assertIn("当前模型不支持图片输入", request_messages[0]["content"][1]["text"])
		self.assertEqual(
			request_messages[2]["content"],
			"第一张图片附件已添加到工具结果上下文。\n当前模型不支持图片输入，附件未发送。",
		)
		self.assertEqual(
			request_messages[3]["content"],
			"第二张图片附件已添加到工具结果上下文。\n当前模型不支持图片输入，附件未发送。",
		)
		self.assertEqual(raw_messages, gateway.assistant_session.contexts_status.to_list())
		self.assertEqual(raw_messages[0]["content"][1]["type"], "image")
		self.assertEqual(raw_messages[2]["content"][1]["type"], "image")

	def test_enabled_image_input_keeps_user_image_message(self):
		gateway = self._gateway([
			UserContext(content=[
				TextSegment(text="请查看图片"),
				ImageBase64Segment(
					data="dXNlci1pbWFnZQ==",
					media_type="image/png",
				),
			]),
		], support_image=True)

		request_messages = gateway._get_request_messages()

		self.assertEqual(self._segment_types(request_messages[0]), ["text", "image"])

	def test_follow_up_user_mode_adapts_supported_images_without_mutating_session(self):
		gateway = self._gateway([
			UserContext(content="请查看图片"),
			self._image_context_block(),
		], mode="follow_up_user", support_image=True)

		raw_messages = gateway.assistant_session.contexts_status.to_list()
		request_messages = gateway._get_request_messages()

		self.assertEqual(
			[message["role"] for message in request_messages],
			["user", "assistant", "tool", "tool", "user"],
		)
		self.assertEqual(
			request_messages[2]["content"],
			"第一张图片附件已添加到工具结果上下文。",
		)
		self.assertEqual(
			request_messages[3]["content"],
			"第二张图片附件已添加到工具结果上下文。",
		)
		self.assertEqual(request_messages[4]["content"], [
			{
				"type": "image_url",
				"image_url": {
					"url": "data:image/png;base64,aW1hZ2UtMQ==",
					"detail": "auto",
				},
			},
			{
				"type": "image_url",
				"image_url": {
					"url": "https://example.com/image-2.webp",
					"detail": "high",
				},
			},
		])
		self.assertEqual(raw_messages, gateway.assistant_session.contexts_status.to_list())
		self.assertEqual(raw_messages[2]["content"][1]["type"], "image")
		self.assertEqual(raw_messages[3]["content"][1]["type"], "image_url")

		request = asyncio.run(gateway.on_build_request())
		self.assertEqual(request.body["messages"], request_messages)

	def test_follow_up_user_mode_keeps_unconvertible_tool_image(self):
		tool_output = ToolOutputContext(
			tool_call_id="call-1",
			content=[
				TextSegment(text="无法转换的图片附件"),
				ImageBase64Segment(
					data="aW52YWxpZA==",
					media_type="text/plain",
				),
			],
		)
		gateway = self._gateway([
			self._tool_calling_context("call-1"),
			tool_output,
		], mode="follow_up_user", support_image=True)

		raw_messages = gateway.assistant_session.contexts_status.to_list()
		request_messages = gateway._get_request_messages()

		self.assertEqual(request_messages, raw_messages)
		self.assertEqual(request_messages[-1]["role"], "tool")
		self.assertEqual(request_messages[-1]["content"][1]["type"], "image")

	def test_follow_up_user_mode_keeps_only_unconvertible_images_in_tool_message(self):
		tool_output = ToolOutputContext(
			tool_call_id="call-1",
			content=[
				TextSegment(text="混合图片附件"),
				ImageBase64Segment(
					data="aW1hZ2UtMQ==",
					media_type="image/png",
				),
				ImageBase64Segment(
					data="aW52YWxpZA==",
					media_type="text/plain",
				),
				ImageUrlSegment(url=""),
			],
		)
		gateway = self._gateway([
			self._tool_calling_context("call-1"),
			tool_output,
		], mode="follow_up_user", support_image=True)

		raw_messages = gateway.assistant_session.contexts_status.to_list()
		request_messages = gateway._get_request_messages()

		self.assertEqual(
			[message["role"] for message in request_messages],
			["assistant", "tool", "user"],
		)
		self.assertEqual(
			self._segment_types(request_messages[1]),
			["text", "image", "image_url"],
		)
		self.assertEqual(
			request_messages[1]["content"][1]["image"]["media_type"],
			"text/plain",
		)
		self.assertEqual(
			request_messages[1]["content"][2]["image_url"]["url"],
			"",
		)
		self.assertEqual(request_messages[2]["content"], [{
			"type": "image_url",
			"image_url": {
				"url": "data:image/png;base64,aW1hZ2UtMQ==",
				"detail": "auto",
			},
		}])
		self.assertEqual(raw_messages, gateway.assistant_session.contexts_status.to_list())

	def test_follow_up_user_mode_does_not_send_disabled_images(self):
		gateway = self._gateway([
			UserContext(content="请查看图片"),
			self._image_context_block(),
		], mode="follow_up_user")

		request_messages = gateway._get_request_messages()

		self.assertEqual(
			[message["role"] for message in request_messages],
			["user", "assistant", "tool", "tool"],
		)
		self.assertIn("当前模型不支持图片输入", request_messages[-1]["content"])
		self.assertFalse(any(
			segment["type"] in {"image", "image_url"}
			for message in request_messages
			if isinstance(message["content"], list)
			for segment in message["content"]
		))

	def test_audio_input_is_filtered_by_capability(self):
		context = UserContext(content=[
			TextSegment(text="请处理音频"),
			AudioInputSegment(data="YXVkaW8=", format="wav"),
		])

		unsupported_gateway = self._gateway([context])
		unsupported_message = unsupported_gateway._get_request_messages()[0]
		self.assertEqual(self._segment_types(unsupported_message), ["text", "text"])
		self.assertIn("当前模型不支持音频输入", unsupported_message["content"][1]["text"])

		supported_gateway = self._gateway([context], support_audio=True)
		supported_message = supported_gateway._get_request_messages()[0]
		self.assertEqual(self._segment_types(supported_message), ["text", "input_audio"])

	def test_explicit_audio_and_video_segments_are_filtered_by_capability(self):
		context = UserContext(content=[
			TextSegment(text="请处理媒体"),
			AudioUrlSegment(url="https://example.com/audio.mp3"),
			VideoInputSegment(data="dmlkZW8="),
			VideoUrlSegment(url="https://example.com/video.mp4"),
		])

		unsupported_gateway = self._gateway([context])
		unsupported_message = unsupported_gateway._get_request_messages()[0]
		self.assertEqual(
			self._segment_types(unsupported_message),
			["text", "text"],
		)
		self.assertIn("当前模型不支持音频、视频输入", unsupported_message["content"][1]["text"])

		supported_gateway = self._gateway(
			[context],
			support_audio=True,
			support_video=True,
		)
		supported_message = supported_gateway._get_request_messages()[0]
		self.assertEqual(
			self._segment_types(supported_message),
			["text", "audio_url", "input_video", "video_url"],
		)
		self.assertEqual(
			supported_message["content"][1]["audio_url"]["url"],
			"https://example.com/audio.mp3",
		)
		self.assertEqual(
			supported_message["content"][2]["input_video"]["data"],
			"dmlkZW8=",
		)
		self.assertEqual(
			supported_message["content"][3]["video_url"]["url"],
			"https://example.com/video.mp4",
		)

	def test_video_input_is_filtered_by_capability(self):
		context = UserContext(content=[
			TextSegment(text="请处理视频"),
			UnknownSegment(
				type="input_video",
				video_url="https://example.com/demo.mp4",
			),
		])

		unsupported_gateway = self._gateway([context])
		unsupported_message = unsupported_gateway._get_request_messages()[0]
		self.assertEqual(self._segment_types(unsupported_message), ["text", "text"])
		self.assertIn("当前模型不支持视频输入", unsupported_message["content"][1]["text"])

		supported_gateway = self._gateway([context], support_video=True)
		supported_message = supported_gateway._get_request_messages()[0]
		self.assertEqual(self._segment_types(supported_message), ["text", "input_video"])

	def test_tool_output_filters_only_unsupported_modalities(self):
		tool_output = ToolOutputContext(
			tool_call_id="call-1",
			content=[
				TextSegment(text="工具结果"),
				ImageBase64Segment(
					data="aW1hZ2U=",
					media_type="image/png",
				),
				AudioInputSegment(data="YXVkaW8="),
				UnknownSegment(
					type="input_video",
					video_url="https://example.com/demo.mp4",
				),
			],
		)
		gateway = self._gateway([
			tool_output,
		], support_image=True, support_video=True)

		request_message = gateway._get_request_messages()[0]

		self.assertEqual(
			self._segment_types(request_message),
			["text", "image", "input_video"],
		)
		self.assertIn("当前模型不支持音频输入", request_message["content"][0]["text"])

	def test_follow_up_user_mode_token_estimate_uses_adapted_messages(self):
		token_tracker = RecordingTokenTracker()
		gateway = self._gateway([
			UserContext(content="请查看图片"),
			self._image_context_block(),
		], mode="follow_up_user", support_image=True, token_tracker=token_tracker)

		self.assertEqual(gateway.estimated_context_tokens, 1)
		self.assertEqual(
			json.loads(token_tracker.contents[0]),
			gateway._get_request_messages(),
		)

	def test_token_cache_is_separated_by_tool_output_image_mode(self):
		token_tracker = RecordingTokenTracker()
		gateway = self._gateway([
			UserContext(content="请查看图片"),
			self._image_context_block(),
		], support_image=True, token_tracker=token_tracker)

		self.assertEqual(gateway.estimated_context_tokens, 1)
		self.assertEqual(token_tracker.estimate_calls, 1)

		gateway.assistant_model_config.tool_output_image_mode = "follow_up_user"

		self.assertEqual(gateway.estimated_context_tokens, 1)
		self.assertEqual(token_tracker.estimate_calls, 2)

	def test_token_cache_is_separated_by_multimodal_capabilities(self):
		token_tracker = RecordingTokenTracker()
		gateway = self._gateway([
			UserContext(content=[
				ImageBase64Segment(
					data="aW1hZ2U=",
					media_type="image/png",
				),
			]),
		], token_tracker=token_tracker)

		self.assertEqual(gateway.estimated_context_tokens, 1)
		self.assertEqual(token_tracker.estimate_calls, 1)

		gateway.assistant_model_config.support_image = True
		self.assertEqual(gateway.estimated_context_tokens, 1)
		self.assertEqual(token_tracker.estimate_calls, 2)

		gateway.assistant_model_config.support_audio = True
		self.assertEqual(gateway.estimated_context_tokens, 1)
		self.assertEqual(token_tracker.estimate_calls, 3)

		gateway.assistant_model_config.support_video = True
		self.assertEqual(gateway.estimated_context_tokens, 1)
		self.assertEqual(token_tracker.estimate_calls, 4)

	def test_image_adapter_uses_aioverse_segment_instances(self):
		tool_output = ToolOutputContext(
			tool_call_id="call-1",
			content=[
				ImageBase64Segment(data="aW1hZ2UtMQ==", media_type="image/png"),
				ImageBase64Segment(data="aW52YWxpZA==", media_type="text/plain"),
				ImageUrlSegment(url=""),
				ImageUrlSegment(url="https://example.com/image.webp"),
			],
		)
		gateway = self._gateway([])

		image_segments = gateway._get_tool_output_image_segments(tool_output)

		self.assertEqual(len(image_segments), 2)
		self.assertTrue(all(isinstance(segment, ImageUrlSegment) for segment in image_segments))
		self.assertEqual(image_segments[0].url, "data:image/png;base64,aW1hZ2UtMQ==")
		self.assertEqual(image_segments[1].url, "https://example.com/image.webp")

	def test_tool_output_image_mode_rejects_unknown_value(self):
		with self.assertRaises(ValidationError):
			AssistantModelConfig(
				api_url="https://example.invalid/v1/chat/completions",
				model_name="demo",
				max_context_length=100000,
				cleanup_threshold=90000,
				tool_output_image_mode="unknown",
			)


	def test_contexts_status_restores_generic_block_context_types(self):
		status = ContextsStatus(contexts=[
			BaseContextsBlock(contexts=[
				SystemContext(content="system"),
				UserContext(content="user"),
			]),
		])

		restored_status = ContextsStatus.model_validate_json(
			status.model_dump_json()
		)
		restored_block = restored_status.contexts[0]

		self.assertIsInstance(restored_block, BaseContextsBlock)
		self.assertIsInstance(restored_block.contexts[0], SystemContext)
		self.assertIsInstance(restored_block.contexts[1], UserContext)
		self.assertEqual(
			restored_status.model_dump(mode="json"),
			status.model_dump(mode="json"),
		)

	def test_multimodal_tool_output_persists_and_requests_after_session_round_trip(self):
		prompt = AssistantPrompt(system_prompt="saved prompt")
		status = ContextsStatus(
			prompt=SystemContext(content=prompt.model_dump_json()),
			memory=SystemContext(content="saved memory"),
			contexts=[
				UserContext(content="read attached images"),
				self._image_context_block(),
			],
		)

		restored_status = ContextsStatus.model_validate_json(
			status.model_dump_json()
		)
		self.assertEqual(
			restored_status.model_dump(mode="json"),
			status.model_dump(mode="json"),
		)
		self.assertIsInstance(restored_status.contexts[0], UserContext)
		self.assertIsInstance(
			restored_status.contexts[1],
			ToolCallingContextsBlock,
		)
		restored_block = restored_status.contexts[1]
		self.assertIsInstance(restored_block.tool_calling, ToolCallingContext)
		self.assertTrue(all(
			isinstance(tool_output, ToolOutputContext)
			for tool_output in restored_block.tool_outputs
		))
		self.assertIsInstance(
			restored_block.tool_outputs[0].content[1],
			ImageBase64Segment,
		)
		self.assertIsInstance(
			restored_block.tool_outputs[1].content[1],
			ImageUrlSegment,
		)

		session = AssistantSession(
			assistant_model_name="demo",
			contexts_status=status,
		)
		fd, path = tempfile.mkstemp(suffix=".json")
		os.close(fd)
		try:
			session.to_file(path, encoding="utf-8")
			restored_session = AssistantSession.from_file(path, encoding="utf-8")
		finally:
			os.remove(path)

		self.assertEqual(
			restored_session.model_dump(mode="json"),
			session.model_dump(mode="json"),
		)
		restored_session_block = restored_session.contexts_status.contexts[1]
		self.assertIsInstance(restored_session_block, ToolCallingContextsBlock)
		self.assertIsInstance(
			restored_session_block.tool_outputs[0].content[1],
			ImageBase64Segment,
		)
		self.assertIsInstance(
			restored_session_block.tool_outputs[1].content[1],
			ImageUrlSegment,
		)
		model_config = AssistantModelConfig(
			api_url="https://example.invalid/v1/chat/completions",
			model_name="demo",
			model_keys=[AssistantKey(key="Bearer test")],
			max_context_length=100000,
			cleanup_threshold=90000,
			support_streaming=False,
			support_image=True,
			tool_output_image_mode="follow_up_user",
		)
		client = RecordingResponseClient()
		gateway = AssistantGateway(
			claw_config=ClawConfig(models_config=[model_config]),
			assistant_session=restored_session,
			assistant_prompt=prompt,
			openai_client=client,
			token_tracker=RecordingTokenTracker(),
		)

		asyncio.run(gateway.round_call())

		self.assertEqual(len(client.requests), 1)
		request_messages = client.requests[0].body["messages"]
		self.assertEqual(
			[message["role"] for message in request_messages],
			["system", "system", "user", "assistant", "tool", "tool", "user"],
		)
		self.assertEqual(
			self._segment_types(request_messages[-1]),
			["image_url", "image_url"],
		)
		self.assertEqual(
			request_messages[-1]["content"][0]["image_url"]["url"],
			"data:image/png;base64,aW1hZ2UtMQ==",
		)
		self.assertEqual(
			request_messages[-1]["content"][1]["image_url"]["url"],
			"https://example.com/image-2.webp",
		)
		self.assertEqual(
			restored_session.contexts_status.contexts[-1].content,
			"request accepted",
		)

if __name__ == "__main__":
	unittest.main()
