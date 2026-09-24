from __future__ import annotations

import asyncio
import json
import unittest

from aioverse.models import (
	ImageBase64Segment,
	TextSegment,
	ToolCallingContext,
	ToolOutputContext,
	UserContext,
)

from aioclaw.services import (
	ContextProjectionCapabilities,
	ContextRequestProjector,
	ToolExecutor,
	ToolRegistry,
)
from aioclaw.utils import build_tool_schema


class ExtractedServicesTests(unittest.TestCase):

	@staticmethod
	def _tool_call(tool_name: str, arguments: str):
		return ToolCallingContext.model_validate({
			"role": "assistant",
			"content": "",
			"reasoning_content": "",
			"tool_calls": [{
				"id": "call-1",
				"type": "function",
				"function": {
					"name": tool_name,
					"arguments": arguments,
				},
			}],
		}).tool_calls[0]

	def test_tool_registry_and_executor_are_independent_of_tools_manager(self):
		registry = ToolRegistry()
		executor = ToolExecutor(timeout=2)

		def add(left: int, right: int) -> int:
			return left + right

		registry.register(
			add,
			build_tool_schema("add", "add numbers", {
				"left": ("integer", "left number"),
				"right": ("integer", "right number"),
			}),
		)

		output = asyncio.run(executor.execute(
			self._tool_call("add", json.dumps({"left": 2, "right": 3})),
			registry.get,
		))

		self.assertIsInstance(output, ToolOutputContext)
		self.assertEqual(output.content, "5")
		self.assertEqual(len(registry.to_list()), 1)

	def test_tool_executor_preserves_multimodal_results(self):
		registry = ToolRegistry()
		executor = ToolExecutor()
		segment = ImageBase64Segment(data="aGVsbG8=", media_type="image/png")

		async def get_image():
			return [TextSegment(text="image"), segment]

		registry.register(
			get_image,
			build_tool_schema("get_image", "get image", {}),
		)

		output = asyncio.run(executor.execute(
			self._tool_call("get_image", "{}"),
			registry.get,
		))

		self.assertEqual(output.content, [TextSegment(text="image"), segment])

	def test_context_request_projector_has_no_gateway_dependency(self):
		projector = ContextRequestProjector()
		contexts = [
			UserContext(content=[
				TextSegment(text="look"),
				ImageBase64Segment(
					data="aW1hZ2U=",
					media_type="image/png",
				),
			]),
			ToolOutputContext(
				tool_call_id="call-1",
				content=[
					TextSegment(text="tool image"),
					ImageBase64Segment(
						data="aW1hZ2U=",
						media_type="image/png",
					),
				],
			),
		]

		messages = projector.project(
			contexts,
			ContextProjectionCapabilities(
				support_image=True,
				tool_output_image_mode="follow_up_user",
			),
		)

		self.assertEqual([message["role"] for message in messages], [
			"user",
			"tool",
			"user",
		])
		self.assertEqual(messages[-1]["content"][0]["type"], "image_url")
		self.assertEqual(messages[-1]["content"][0]["image_url"]["url"], (
			"data:image/png;base64,aW1hZ2U="
		))


if __name__ == "__main__":
	unittest.main()
