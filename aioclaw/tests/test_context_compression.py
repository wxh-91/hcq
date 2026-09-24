from __future__ import annotations

import json
import asyncio
import os
import tempfile
import unittest
from unittest.mock	import patch

from aioverse.models import (
	Response,
	SystemContext,
	UserContext,
	ToolCallingContext,
	ToolOutputContext,
)

from aioclaw.core import AssistantGateway, Compresser, TokenTracker
from aioclaw.errors import (
	ContextOverflowError,
	UnknownFinishReasonError,
	IncompleteToolCallBlockError,
)
from aioclaw.managers import ToolsManager
from aioclaw.models import (
	AssistantKey,
	AssistantModelConfig,
	AssistantSession,
	ClawConfig,
	ContextsStatus,
	ContextCompressionPrompt,
	ToolCallingContextsBlock,
)
from aioclaw.tools import FileOperationTools


class CharacterTokenTracker:

	def __init__(self):
		self.ratio = 1.0
		self.estimate_calls = 0

	def estimate(self, contents):
		self.estimate_calls += 1
		return sum(len(content) for content in contents)

	def calibrate_ratio(self, guessed, actual):
		...


class FakeResponseClient:

	def __init__(self, *, finish_reason="stop", content="## 状态\n- `/tmp/demo.py` 已读取"):
		self.finish_reason = finish_reason
		self.content = content
		self.requests = []

	async def call(self, *, request):
		self.requests.append(request)

		message = {
			"role": "assistant",
			"content": self.content,
			"reasoning_content": "",
		}

		if self.finish_reason == "tool_calls":
			message["tool_calls"] = [{
				"id": "call-1",
				"type": "function",
				"function": {"name": "read_file", "arguments": "{}"}
			}]

		return Response.model_validate({
			"id": "compression-1",
			"created": 1,
			"model": "demo",
			"object": "chat.completion",
			"choices": [{
				"index": 0,
				"finish_reason": self.finish_reason,
				"message": message,
			}],
			"usage": {
				"completion_tokens": 5,
				"prompt_tokens": 10,
				"total_tokens": 15,
			},
		})


class ObservingResponseClient(FakeResponseClient):

	def __init__(self):
		super().__init__()
		self.gateway = None
		self.is_compresser_during_call = None

	async def call(self, *, request):
		self.is_compresser_during_call = self.gateway.is_compresser
		return await super().call(request=request)


class FailingClient:

	def __init__(self):
		self.gateway = None
		self.is_compresser_during_call = None

	async def call(self, *, request):
		self.is_compresser_during_call = self.gateway.is_compresser
		raise RuntimeError("compression unavailable")


class DropFirstCompresser(Compresser):

	async def _compress(self, contexts, **kwargs):
		return contexts[1:]


class ObservingCompresser(Compresser):

	def __init__(self):
		self.gateway = None
		self.is_compresser_during_compress = None

	async def _compress(self, contexts, **kwargs):
		self.is_compresser_during_compress = self.gateway.is_compresser
		return contexts[1:]


class FailingCompresser(Compresser):

	def __init__(self):
		self.gateway = None
		self.is_compresser_during_compress = None

	async def _compress(self, contexts, **kwargs):
		self.is_compresser_during_compress = self.gateway.is_compresser
		raise RuntimeError("local compression unavailable")


class ContextCompressionTests(unittest.TestCase):

	def _gateway(
		self,
		contexts,
		*,
		client=None,
		compresser=None,
		context_compression_prompt=None,
		context_compression_keep_contexts=4,
		max_context_length=100000,
		cleanup_threshold=1,
		reserved_completion_tokens=0,
		context_safety_margin=0,
		context_compression_max_tokens=2048,
		support_tool=False,
		token_tracker=None,
	):
		model_config = AssistantModelConfig(
			api_url="https://example.invalid/v1/chat/completions",
			model_name="demo",
			model_keys=[AssistantKey(key="Bearer test")],
			max_context_length=max_context_length,
			cleanup_threshold=cleanup_threshold,
			reserved_completion_tokens=reserved_completion_tokens,
			context_safety_margin=context_safety_margin,
			support_tool=support_tool,
			support_streaming=False,
		)
		claw_config = ClawConfig(
			models_config=[model_config],
			context_compression_keep_contexts=context_compression_keep_contexts,
			context_compression_max_tokens=context_compression_max_tokens,
		)
		session = AssistantSession(
			assistant_model_name="demo",
			contexts_status=ContextsStatus(contexts=contexts),
		)
		return AssistantGateway(
			claw_config=claw_config,
			assistant_session=session,
			openai_client=client,
			token_tracker=CharacterTokenTracker() if token_tracker is None else token_tracker,
			tools_manager=(
				self._tools_manager()
				if support_tool
				else None
			),
			compresser=compresser,
			context_compression_prompt=context_compression_prompt,
		)

	@staticmethod
	def _tools_manager():
		manager = ToolsManager()
		FileOperationTools().register(manager)
		return manager

	def test_gateway_input_adds_context_through_gateway_hook(self):
		gateway = self._gateway([])
		context = UserContext(content="input through gateway")

		asyncio.run(gateway.input(context))

		self.assertEqual(
			gateway.assistant_session.contexts_status.contexts,
			[context]
		)

	def test_prompt_is_not_json_encoded_twice(self):
		status = ContextsStatus()
		status.set_prompt(SystemContext(content="prompt"))
		status.add_context(UserContext(content="hello"))

		self.assertEqual(status.to_list(), [
			{"role": "system", "content": "prompt"},
			{"role": "user", "content": "hello"},
		])

	def test_threshold_properties_use_cached_estimate(self):
		gateway = self._gateway([
			UserContext(content="hello"),
			UserContext(content="world"),
		])
		tracker = gateway.token_tracker

		first = gateway.estimated_context_tokens
		second = gateway.estimated_context_tokens

		self.assertEqual(first, second)
		self.assertEqual(tracker.estimate_calls, 1)
		self.assertEqual(tracker.ratio, 1.0)
		self.assertTrue(gateway.is_context_cleanup_required)
		self.assertFalse(gateway.is_context_overflow)

		gateway.assistant_session.contexts_status.add_context(
			UserContext(content="new context")
		)
		gateway.estimated_context_tokens
		self.assertEqual(tracker.estimate_calls, 2)

	def test_effective_context_limit_reserves_output_and_margin(self):
		gateway = self._gateway(
			[],
			max_context_length=1000,
			cleanup_threshold=700,
			reserved_completion_tokens=200,
			context_safety_margin=50,
		)

		self.assertEqual(gateway.effective_context_limit, 750)
		self.assertFalse(gateway.is_context_overflow)

	def test_context_compression_max_tokens_requires_positive_integer(self):
		for value in (0, -1, 1.5, "2", True):
			with self.assertRaises(ValueError):
				ClawConfig(context_compression_max_tokens=value)

		config = ClawConfig(context_compression_max_tokens=None)
		self.assertIsNone(config.context_compression_max_tokens)

	def test_claw_config_compression_settings_validate_assignment(self):
		config = ClawConfig()
		config.context_compression_keep_contexts = 2
		config.context_compression_max_tokens = None

		self.assertEqual(config.context_compression_keep_contexts, 2)
		self.assertIsNone(config.context_compression_max_tokens)

		with self.assertRaises(ValueError):
			config.context_compression_keep_contexts = 0
		with self.assertRaises(ValueError):
			config.context_compression_max_tokens = False

	def test_context_compression_mixin_initializes_custom_prompt(self):
		prompt = ContextCompressionPrompt(
			system_prompt="custom compression system",
			payload_prefix="custom payload prefix",
			memory_prefix="# custom memory",
		)
		client = FakeResponseClient(content="## 状态\n- 已压缩")
		gateway = self._gateway(
			[
				UserContext(content="a" * 2000),
				UserContext(content="b" * 2000),
				UserContext(content="c" * 2000),
				UserContext(content="d" * 2000),
			],
			client=client,
			context_compression_prompt=prompt,
			context_compression_keep_contexts=2,
		)

		self.assertIs(gateway.context_compression_prompt, prompt)
		self.assertEqual(gateway.context_compression_keep_contexts, 2)
		self.assertEqual(
			gateway.claw_config.context_compression_keep_contexts,
			2,
		)
		self.assertTrue(asyncio.run(gateway.compress_contexts()))

		request = client.requests[0]
		self.assertEqual(
			request.body["messages"][0]["content"],
			"custom compression system"
		)
		self.assertTrue(
			request.body["messages"][1]["content"].startswith(
				"custom payload prefix"
			)
		)
		self.assertTrue(
			gateway.assistant_session.contexts_status.memory.content.startswith(
				"# custom memory"
			)
		)
		self.assertEqual(len(gateway.assistant_session.contexts_status.contexts), 2)

	def test_compression_messages_use_aioverse_context_models(self):
		gateway = self._gateway([
			UserContext(content="old"),
			UserContext(content="recent"),
		])
		contexts = gateway.assistant_session.contexts_status.contexts[:1]
		messages = gateway._build_compression_messages(contexts)
		messages_data = gateway._build_compression_messages_data(contexts)
		request = asyncio.run(gateway.on_build_compression_request(contexts))

		self.assertIsInstance(messages[0], SystemContext)
		self.assertIsInstance(messages[1], UserContext)
		self.assertEqual(
			[
				context.model_dump(mode="json", exclude_none=True)
				for context in messages
			],
			messages_data,
		)
		self.assertEqual(request.body["messages"], messages_data)

		with patch.object(
			gateway.token_tracker,
			"estimate",
			wraps=gateway.token_tracker.estimate,
		) as estimate:
			gateway._estimate_compression_input_tokens(contexts)

		estimated_body = json.loads(estimate.call_args.args[0][0])
		self.assertEqual(estimated_body["messages"], messages_data)

	def test_api_compression_sets_flag_during_call_and_logs_start(self):
		client = ObservingResponseClient()
		gateway = self._gateway(
			[
				UserContext(content="a" * 2000),
				UserContext(content="b" * 2000),
			],
			client=client,
		)
		client.gateway = gateway

		with self.assertLogs(
			"aioclaw.mixins.context_compression",
			level="INFO",
		) as logs:
			self.assertTrue(asyncio.run(gateway.compress_contexts()))

		self.assertTrue(client.is_compresser_during_call)
		self.assertFalse(gateway.is_compresser)
		self.assertTrue(
			any("开始上下文压缩" in message for message in logs.output)
		)

	def test_api_compression_resets_flag_after_failure(self):
		client = FailingClient()
		gateway = self._gateway(
			[
				UserContext(content="a" * 100),
				UserContext(content="b" * 100),
			],
			client=client,
		)
		client.gateway = gateway

		with self.assertRaises(RuntimeError):
			asyncio.run(gateway.compress_contexts())

		self.assertTrue(client.is_compresser_during_call)
		self.assertFalse(gateway.is_compresser)

	def test_context_compression_keep_contexts_requires_positive_integer(self):
		for value in (0, -1, 1.5, "2", True):
			with self.assertRaises(ValueError):
				ClawConfig(context_compression_keep_contexts=value)

	def test_gateway_reads_compression_settings_from_claw_config(self):
		gateway = self._gateway(
			[],
			context_compression_keep_contexts=2,
			context_compression_max_tokens=1024,
		)

		self.assertEqual(gateway.context_compression_keep_contexts, 2)
		self.assertEqual(gateway.context_compression_max_tokens, 1024)

		gateway.set_context_compression_keep_contexts(3)
		gateway.set_context_compression_max_tokens(None)

		self.assertEqual(
			gateway.claw_config.context_compression_keep_contexts,
			3,
		)
		self.assertIsNone(gateway.claw_config.context_compression_max_tokens)

	def test_default_context_compression_prompt_is_multiline(self):
		prompt = ContextCompressionPrompt()

		self.assertGreater(len(prompt.system_prompt.splitlines()), 1)
		self.assertTrue(prompt.payload_prefix)
		self.assertTrue(prompt.memory_prefix)

	def test_gateway_context_hook_runs_without_a_base_gateway(self):
		gateway = self._gateway([], cleanup_threshold=100000)

		asyncio.run(gateway.on_prepare_context_before_request())
		self.assertIsNotNone(gateway.get_request_estimated_tokens())
		self.assertFalse(gateway.is_round_processing)

	def test_tool_calling_block_is_selected_as_one_context_item(self):
		tool_calling = ToolCallingContext.model_validate({
			"role": "assistant",
			"content": "",
			"reasoning_content": "",
			"tool_calls": [{
				"id": "call-1",
				"type": "function",
				"function": {"name": "read_file", "arguments": "{}"},
			}],
		})
		tool_block = ToolCallingContextsBlock(
			tool_calling=tool_calling,
			tool_outputs=[ToolOutputContext(tool_call_id="call-1", content="ok")],
		)
		gateway = self._gateway([
			UserContext(content="old"),
			tool_block,
			UserContext(content="recent"),
		], context_compression_keep_contexts=1)

		to_compress, to_keep = asyncio.run(
			gateway.on_select_contexts_for_compression()
		)
		self.assertEqual(to_compress, [gateway.assistant_session.contexts_status.contexts[0], tool_block])
		self.assertEqual(to_keep, [gateway.assistant_session.contexts_status.contexts[-1]])

	def test_compression_input_overflow_does_not_send_request(self):
		client = FakeResponseClient()
		gateway = self._gateway(
			[UserContext(content="a" * 1000), UserContext(content="b" * 1000)],
			client=client,
			max_context_length=200,
			context_compression_max_tokens=50,
		)
		status = gateway.assistant_session.contexts_status
		before = status.model_dump(mode="json")

		with self.assertRaises(ContextOverflowError):
			asyncio.run(gateway.compress_contexts())

		self.assertEqual(client.requests, [])
		self.assertEqual(status.model_dump(mode="json"), before)

	def test_api_compression_uses_direct_non_stream_request(self):
		client = FakeResponseClient()
		gateway = self._gateway(
			[
				UserContext(content="a" * 2000),
				UserContext(content="b" * 2000),
				UserContext(content="c" * 2000),
				UserContext(content="d" * 2000),
				UserContext(content="e" * 2000),
			],
			client=client,
			support_tool=True,
		)

		self.assertTrue(asyncio.run(gateway.compress_contexts()))
		request = client.requests[0]
		self.assertFalse(request.body["stream"])
		self.assertNotIn("tools", request.body)
		self.assertNotIn("tool_choice", request.body)
		self.assertEqual(request.body["max_tokens"], 2048)
		self.assertIsNotNone(gateway.assistant_session.contexts_status.memory)
		self.assertEqual(len(gateway.assistant_session.contexts_status.contexts), 4)

	def test_round_initiate_runs_compression_before_request(self):
		client = FakeResponseClient()
		gateway = self._gateway(
			[
				UserContext(content=letter * 2000)
				for letter in ("a", "b", "c", "d", "e")
			],
			client=client,
		)

		asyncio.run(gateway.on_round_initiate())
		self.assertTrue(gateway.is_round_processing)
		self.assertEqual(len(client.requests), 1)
		self.assertIsNotNone(gateway.assistant_session.contexts_status.memory)
		asyncio.run(gateway.on_round_complete())
		self.assertFalse(gateway.is_round_processing)

	def test_generator_timestamps_track_full_lifecycle(self):
		gateway = self._gateway([])
		gateway.set_generator_complete_timestamp(10.0)

		with patch("aioclaw.core.assistant_gateway.time.time", side_effect=[100.0, 105.5, 105.5]):
			asyncio.run(gateway.on_generator_initiate())

			self.assertEqual(gateway.generator_start_timestamp, 100.0)
			self.assertIsNone(gateway.generator_complete_timestamp)
			self.assertEqual(gateway.generator_elapsed_seconds, 5.5)

			asyncio.run(gateway.on_generator_end())

		self.assertEqual(gateway.generator_complete_timestamp, 105.5)
		self.assertEqual(gateway.generator_elapsed_seconds, 5.5)
		self.assertFalse(gateway.is_generator_processing)

	def test_local_compresser_receives_copy_and_writes_atomically(self):
		gateway = self._gateway(
			[
				UserContext(content="a" * 1000),
				UserContext(content="b" * 1000),
			],
			compresser=DropFirstCompresser(),
		)
		old_tokens = gateway.estimated_context_tokens

		self.assertTrue(asyncio.run(gateway.on_local_context_compress()))
		self.assertEqual(len(gateway.assistant_session.contexts_status.contexts), 1)
		self.assertLess(gateway.estimated_context_tokens, old_tokens)

	def test_compression_resets_token_calibration(self):
		tracker = TokenTracker(calibration_percent=1.0)
		gateway = self._gateway(
			[
				UserContext(content="a" * 1000),
				UserContext(content="b" * 1000),
			],
			compresser=DropFirstCompresser(),
			token_tracker=tracker,
		)
		tools_json = gateway._get_context_token_tools_json()
		scope = gateway._get_token_calibration_scope(tools_json)
		old_raw_tokens = gateway.get_request_raw_estimated_tokens()

		tracker.calibrate_estimate(
			guessed=old_raw_tokens,
			actual=old_raw_tokens * 3,
			calibration_scope=scope,
		)
		self.assertGreater(
			gateway.estimated_context_tokens,
			old_raw_tokens,
		)

		self.assertTrue(asyncio.run(gateway.on_local_context_compress()))

		new_raw_tokens = gateway.get_request_raw_estimated_tokens()
		self.assertEqual(gateway.estimated_context_tokens, new_raw_tokens)
		self.assertEqual(
			tracker.get_calibration_summary(calibration_scope=scope),
			(1.0, 0, 0),
		)

	def test_api_compression_resets_token_calibration(self):
		tracker = TokenTracker(calibration_percent=1.0)
		client = FakeResponseClient(content="## 状态\n- 压缩完成")
		gateway = self._gateway(
			[
				UserContext(content=letter * 1000)
				for letter in ("a", "b", "c", "d", "e")
			],
			client=client,
			context_compression_keep_contexts=2,
			token_tracker=tracker,
		)
		tools_json = gateway._get_context_token_tools_json()
		scope = gateway._get_token_calibration_scope(tools_json)
		old_raw_tokens = gateway.get_request_raw_estimated_tokens()

		tracker.calibrate_estimate(
			guessed=old_raw_tokens,
			actual=old_raw_tokens * 3,
			calibration_scope=scope,
		)
		self.assertGreater(
			gateway.estimated_context_tokens,
			old_raw_tokens,
		)

		self.assertTrue(asyncio.run(gateway.compress_contexts()))

		new_raw_tokens = gateway.get_request_raw_estimated_tokens()
		self.assertEqual(gateway.estimated_context_tokens, new_raw_tokens)
		self.assertEqual(
			tracker.get_calibration_summary(calibration_scope=scope),
			(1.0, 0, 0),
		)

	def test_local_compresser_sets_and_resets_flag(self):
		compresser = ObservingCompresser()
		gateway = self._gateway(
			[
				UserContext(content="a" * 1000),
				UserContext(content="b" * 1000),
			],
			compresser=compresser,
		)
		compresser.gateway = gateway

		self.assertTrue(asyncio.run(gateway.on_local_context_compress()))
		self.assertTrue(compresser.is_compresser_during_compress)
		self.assertFalse(gateway.is_compresser)

	def test_local_compresser_resets_flag_after_failure(self):
		compresser = FailingCompresser()
		gateway = self._gateway(
			[
				UserContext(content="a" * 100),
				UserContext(content="b" * 100),
			],
			compresser=compresser,
		)
		compresser.gateway = gateway

		with self.assertRaises(RuntimeError):
			asyncio.run(gateway.on_local_context_compress())

		self.assertTrue(compresser.is_compresser_during_compress)
		self.assertFalse(gateway.is_compresser)

	def test_failed_api_compression_preserves_session(self):
		client = FailingClient()
		gateway = self._gateway(
			[
				UserContext(content="a" * 100),
				UserContext(content="b" * 100),
			],
			client=client,
		)
		client.gateway = gateway
		status = gateway.assistant_session.contexts_status
		before = status.model_dump(mode="json")

		asyncio.run(gateway.on_prepare_context_before_request())

		self.assertEqual(status.model_dump(mode="json"), before)
		self.assertTrue(client.is_compresser_during_call)
		self.assertFalse(gateway.is_compresser)

	def test_length_finish_reason_preserves_truncated_text(self):
		gateway = self._gateway([])
		response = Response.model_validate({
			"id": "response-1",
			"created": 1,
			"model": "demo",
			"object": "chat.completion",
			"choices": [{
				"index": 0,
				"finish_reason": "length",
				"message": {
					"role": "assistant",
					"content": "截断文本",
					"reasoning_content": "",
				},
			}],
		})

		asyncio.run(gateway.on_response(response))
		self.assertEqual(
			gateway.assistant_session.contexts_status.contexts[-1].content,
			"截断文本",
		)

	def test_length_finish_reason_with_tool_calls_is_rejected(self):
		gateway = self._gateway([])
		response = Response.model_validate({
			"id": "response-2",
			"created": 1,
			"model": "demo",
			"object": "chat.completion",
			"choices": [{
				"index": 0,
				"finish_reason": "length",
				"message": {
					"role": "assistant",
					"content": "",
					"reasoning_content": "",
					"tool_calls": [{
						"id": "call-1",
						"type": "function",
						"function": {"name": "read_file", "arguments": "{"},
					}],
				},
			}],
		})

		with self.assertRaises(IncompleteToolCallBlockError):
			asyncio.run(gateway.on_response(response))

	def test_tool_call_response_is_rejected(self):
		gateway = self._gateway(
			[
				UserContext(content="a" * 100),
				UserContext(content="b" * 100),
			],
			client=FakeResponseClient(finish_reason="tool_calls"),
		)
		status = gateway.assistant_session.contexts_status
		before = status.model_dump(mode="json")

		with self.assertRaises(UnknownFinishReasonError):
			asyncio.run(gateway.compress_contexts())

		self.assertEqual(status.model_dump(mode="json"), before)

	def test_hard_overflow_does_not_call_api_without_local_compressor(self):
		client = FakeResponseClient()
		gateway = self._gateway(
			[
				UserContext(content="a" * 100),
				UserContext(content="b" * 100),
			],
			client=client,
			max_context_length=150,
		)

		with self.assertRaises(ContextOverflowError):
			asyncio.run(gateway.on_prepare_context_before_request())

		self.assertEqual(client.requests, [])

	def test_memory_persists_through_session_round_trip(self):
		gateway = self._gateway(
			[
				UserContext(content="first-" + "a" * 1000),
				UserContext(content="second-" + "b" * 1000),
				UserContext(content="third-" + "c" * 1000),
				UserContext(content="fourth-" + "d" * 1000),
				UserContext(content="fifth-" + "e" * 1000),
			],
			client=FakeResponseClient(content="## 事实\n- `value` 已确认"),
		)
		self.assertTrue(asyncio.run(gateway.compress_contexts()))

		fd, path = tempfile.mkstemp(suffix=".json")
		os.close(fd)
		try:
			gateway.assistant_session.to_file(path, encoding="utf-8")
			restored = AssistantSession.from_file(path, encoding="utf-8")
			self.assertIsNotNone(restored.contexts_status.memory)
			self.assertIn("会话历史记忆", restored.contexts_status.memory.content)
		finally:
			os.remove(path)

	def test_cleanup_threshold_default_is_integer(self):
		config = AssistantModelConfig(
			api_url="x",
			model_name="x",
			max_context_length=10,
		)
		self.assertEqual(config.cleanup_threshold, 7)

		minimum_config = AssistantModelConfig(
			api_url="x",
			model_name="x",
			max_context_length=1,
		)
		self.assertEqual(minimum_config.cleanup_threshold, 1)


if __name__ == "__main__":
	unittest.main()
