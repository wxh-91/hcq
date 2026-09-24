from __future__ import annotations

import asyncio
import base64
import unittest

from aioverse.models import (
	ImageBase64Segment,
	TextSegment,
	ToolCallingContext,
	ToolOutputContext,
	UserContext,
)

from aioclaw.core import AssistantGateway, TokenTracker
from aioclaw.models import (
	AssistantKey,
	AssistantModelConfig,
	AssistantPrompt,
	AssistantSession,
	ClawConfig,
	ToolCallingContextsBlock,
)


class TokenTrackerTests(unittest.TestCase):

	@staticmethod
	def _model(model_name, **kwargs):
		return AssistantModelConfig(
			api_url="https://example.invalid/v1/chat/completions",
			model_name=model_name,
			model_keys=[AssistantKey(key="Bearer test")],
			max_context_length=100000,
			cleanup_threshold=90000,
			support_streaming=False,
			**kwargs,
		)

	def _gateway(self, *, token_tracker, contexts, models_config):
		return AssistantGateway(
			claw_config=ClawConfig(models_config=models_config),
			assistant_session=AssistantSession(
				assistant_model_name=models_config[-1].model_name,
				contexts_status={"contexts": contexts},
			),
			assistant_prompt=AssistantPrompt(system_prompt="test prompt"),
			token_tracker=token_tracker,
		)

	@staticmethod
	def _image_context_block(data):
		tool_calling = ToolCallingContext.model_validate({
			"role": "assistant",
			"content": "",
			"reasoning_content": "",
			"tool_calls": [{
				"id": "call-1",
				"type": "function",
				"function": {
					"name": "view_photo",
					"arguments": "{}",
				},
			}],
		})
		return ToolCallingContextsBlock(
			tool_calling=tool_calling,
			tool_outputs=[ToolOutputContext(
				tool_call_id="call-1",
				content=[
					TextSegment(text="图片附件已添加到工具结果上下文。"),
					ImageBase64Segment(
						data=data,
						media_type="image/png",
					),
				],
			)],
		)

	def test_legacy_ratio_calibration_remains_available(self):
		tracker = TokenTracker(calibration_percent=1.0)
		contents = ["legacy calibration"]
		raw_tokens = tracker.estimate_raw(contents)

		tracker.calibrate_ratio(raw_tokens, raw_tokens * 2)

		self.assertEqual(tracker.ratio, 2.0)
		self.assertEqual(tracker.estimate(contents), raw_tokens * 2)

	def test_scoped_calibration_adds_fixed_overhead_without_multiplying_attachment(self):
		tracker = TokenTracker(calibration_percent=1.0)
		scope = "test-provider:test-model"
		text_contents = ["small text request"]
		image_contents = [
			"data:image/png;base64,"
			+ base64.b64encode(bytes(range(256)) * 16).decode(),
		]
		text_tokens = tracker.estimate_raw(text_contents)
		image_tokens = tracker.estimate_raw(image_contents)
		actual_text_tokens = text_tokens + 4500

		tracker.calibrate_estimate(
			guessed=text_tokens,
			actual=actual_text_tokens,
			calibration_scope=scope,
		)

		calibrated_image_tokens = tracker.estimate_with_scope(
			image_contents,
			calibration_scope=scope,
		)
		legacy_image_tokens = int(
			image_tokens * actual_text_tokens / text_tokens
		)

		self.assertGreater(image_tokens, text_tokens)
		self.assertEqual(calibrated_image_tokens, image_tokens + 4500)
		self.assertLess(calibrated_image_tokens, legacy_image_tokens)
		self.assertEqual(
			tracker.estimate_with_scope(
				image_contents,
				calibration_scope="another-provider:test-model",
			),
			image_tokens,
		)

	def test_reset_calibration_clears_scope_and_legacy_ratio(self):
		tracker = TokenTracker(calibration_percent=1.0)
		contents = ["calibration reset"]
		raw_tokens = tracker.estimate_raw(contents)
		scope = "test-provider:test-model"

		tracker.calibrate_ratio(raw_tokens, raw_tokens * 2)
		tracker.calibrate_estimate(
			guessed=raw_tokens,
			actual=raw_tokens * 3,
			calibration_scope=scope,
		)
		tracker.reset_calibration(calibration_scope=scope)

		self.assertEqual(tracker.ratio, 1.0)
		self.assertEqual(
			tracker.get_calibration_summary(calibration_scope=scope),
			(1.0, 0, 0),
		)
		self.assertEqual(
			tracker.estimate_with_scope(
				contents,
				calibration_scope=scope,
			),
			raw_tokens,
		)

	def test_gateway_uses_session_model_before_request_projection(self):
		text_model = self._model("text", support_image=False)
		vision_model = self._model(
			"vision",
			support_image=True,
			tool_output_image_mode="follow_up_user",
		)
		gateway = self._gateway(
			token_tracker=TokenTracker(),
			contexts=[
				UserContext(content="look at this"),
				self._image_context_block("aW1hZ2UtMQ=="),
			],
			models_config=[text_model, vision_model],
		)

		self.assertEqual(gateway.assistant_model_config.model_name, "vision")
		self.assertEqual(
			[message["role"] for message in gateway._get_request_messages()],
			["user", "assistant", "tool", "user"],
		)

	def test_gateway_does_not_multiply_text_usage_ratio_for_tool_image(self):
		tracker = TokenTracker(calibration_percent=1.0)
		vision_model = self._model(
			"vision",
			support_image=True,
			tool_output_image_mode="follow_up_user",
		)
		gateway = self._gateway(
			token_tracker=tracker,
			contexts=[UserContext(content="read the image")],
			models_config=[vision_model],
		)

		asyncio.run(gateway.on_round_initiate())
		text_tokens = gateway._request_raw_estimated_tokens
		self.assertIsNotNone(text_tokens)
		gateway._request_prompt_tokens = text_tokens + 4500
		asyncio.run(gateway.on_round_complete())

		image_data = base64.b64encode(bytes(range(256)) * 16).decode()
		gateway.assistant_session.contexts_status.add_context(
			self._image_context_block(image_data)
		)
		image_tokens = gateway.get_request_raw_estimated_tokens()
		calibrated_image_tokens = gateway.get_request_estimated_tokens()
		legacy_image_tokens = int(
			image_tokens * (text_tokens + 4500) / text_tokens
		)

		self.assertEqual(calibrated_image_tokens, image_tokens + 4500)
		self.assertLess(
			calibrated_image_tokens,
			gateway.effective_context_limit,
		)
		self.assertGreaterEqual(
			legacy_image_tokens,
			gateway.effective_context_limit,
		)

		asyncio.run(gateway.on_round_initiate())
		asyncio.run(gateway.on_round_complete())
		self.assertFalse(gateway.is_round_processing)


if __name__ == "__main__":
	unittest.main()
