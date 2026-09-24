from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import unittest

import aioclaw.tools as tools

from aioverse.models import ImageBase64Segment, TextSegment, ToolCallingContext

from aioclaw.managers import ToolsManager
from aioclaw.tools import VisionOperationTools
from aioclaw.tools.vision_operation_tools import MAX_IMAGE_BYTES
from aioclaw.utils import build_tool_schema


PNG_BYTES = base64.b64decode(
	"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL7hAAAAABJRU5ErkJggg=="
)

IMAGE_BYTES = {
	"image/png": PNG_BYTES,
	"image/jpeg": b"\xff\xd8\xff\xdb",
	"image/gif": b"GIF89a",
	"image/webp": b"RIFF\x00\x00\x00\x00WEBPVP8 ",
}


class VisionOperationToolsTests(unittest.TestCase):

	def _tool_call(self, tool_name: str, arguments: str):
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

	def _execute_tool(self, manager: ToolsManager, tool_name: str, arguments: str):
		return asyncio.run(manager.execute_tool(
			self._tool_call(tool_name, arguments)
		))

	def test_public_tool_registers_and_recognizes_supported_image_formats(self):
		self.assertIs(tools.VisionOperationTools, VisionOperationTools)

		with tempfile.TemporaryDirectory() as directory:
			manager = ToolsManager()
			VisionOperationTools().register(manager)

			for index, (media_type, image_bytes) in enumerate(IMAGE_BYTES.items()):
				file_path = os.path.join(directory, f"image-{index}")
				with open(file_path, "wb") as file:
					file.write(image_bytes)

				with self.subTest(media_type=media_type):
					tool_output = self._execute_tool(
						manager,
						"view_photo",
						json.dumps({"file_path": file_path}),
					)
					serialized_output = tool_output.model_dump()

					self.assertIsInstance(tool_output.content, list)
					self.assertIsInstance(tool_output.content[0], TextSegment)
					self.assertIn(
						"图片附件已添加到工具结果上下文",
						tool_output.content[0].text,
					)
					self.assertIsInstance(tool_output.content[1], ImageBase64Segment)
					self.assertEqual(tool_output.content[1].media_type, media_type)
					self.assertEqual(
						tool_output.content[1].data,
						base64.b64encode(image_bytes).decode(),
					)
					self.assertEqual(
						serialized_output["content"][1]["image"]["media_type"],
						media_type,
					)

			self.assertIn("view_photo", manager.schema)

	def test_view_photo_rejects_invalid_paths_and_files(self):
		with tempfile.TemporaryDirectory() as directory:
			non_image_path = os.path.join(directory, "not-image.png")
			oversized_path = os.path.join(directory, "oversized.png")

			with open(non_image_path, "wb") as file:
				file.write(b"not an image")

			with open(oversized_path, "wb") as file:
				file.truncate(MAX_IMAGE_BYTES + 1)

			tool = VisionOperationTools()
			non_image_output = asyncio.run(tool.view_photo(non_image_path))
			oversized_output = asyncio.run(tool.view_photo(oversized_path))
			empty_path_output = asyncio.run(tool.view_photo(""))
			non_string_path_output = asyncio.run(tool.view_photo(None))
			missing_path_output = asyncio.run(tool.view_photo(
				os.path.join(directory, "missing.png")
			))

			self.assertIn("不是支持的图片格式", non_image_output)
			self.assertIn("超过图片大小限制", oversized_output)
			self.assertIn("非空字符串", empty_path_output)
			self.assertIn("非空字符串", non_string_path_output)
			self.assertIn("不是一个文件或不存在", missing_path_output)

	def test_tools_manager_keeps_segments_and_converts_other_outputs(self):
		manager = ToolsManager()
		segment = ImageBase64Segment(data="aGVsbG8=", media_type="image/png")

		def returns_segment():
			return segment

		def returns_segments():
			return [segment]

		def returns_mixed_values():
			return [segment, "text"]

		def returns_number():
			return 123

		manager.register(returns_segment, build_tool_schema("returns_segment", "test", {}))
		manager.register(returns_segments, build_tool_schema("returns_segments", "test", {}))
		manager.register(returns_mixed_values, build_tool_schema("returns_mixed_values", "test", {}))
		manager.register(returns_number, build_tool_schema("returns_number", "test", {}))

		single_segment_output = self._execute_tool(manager, "returns_segment", "{}")
		segments_output = self._execute_tool(manager, "returns_segments", "{}")
		mixed_values_output = self._execute_tool(manager, "returns_mixed_values", "{}")
		number_output = self._execute_tool(manager, "returns_number", "{}")

		self.assertEqual(single_segment_output.content, [segment])
		self.assertEqual(segments_output.content, [segment])
		self.assertIsInstance(mixed_values_output.content, str)
		self.assertIn("text", mixed_values_output.content)
		self.assertEqual(number_output.content, "123")

	def test_invalid_arguments_are_returned_as_tool_output(self):
		manager = ToolsManager()
		VisionOperationTools().register(manager)

		invalid_json_output = self._execute_tool(manager, "view_photo", "{")
		missing_argument_output = self._execute_tool(manager, "view_photo", "{}")
		non_object_output = self._execute_tool(manager, "view_photo", "[]")
		unknown_tool_output = self._execute_tool(manager, "missing_tool", "{}")

		self.assertIn("JSONDecodeError", invalid_json_output.content)
		self.assertIn("TypeError", missing_argument_output.content)
		self.assertIn("JSON 对象", non_object_output.content)
		self.assertEqual(unknown_tool_output.content, "无法调用不存在的工具: missing_tool")


if __name__ == "__main__":
	unittest.main()
