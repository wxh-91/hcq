from __future__ import annotations

import unittest

from aioverse.models import Delta

from aioclaw.core.stream_handler import StreamHandler


class StreamHandlerTest(unittest.TestCase):

	def test_first_tool_call_creates_default_function_fields(self):
		handler = StreamHandler()

		handler.merge(Delta(tool_calls=[{
			"index": 0,
			"id": "call-1",
			"type": "function",
		}]))

		tool_call = handler.build_tool_calling_context().tool_calls[0]

		self.assertEqual(tool_call.id, "call-1")
		self.assertEqual(tool_call.type, "function")
		self.assertEqual(tool_call.function.name, "")
		self.assertEqual(tool_call.function.arguments, "")

	def test_tool_call_deltas_preserve_fields_and_append_arguments(self):
		handler = StreamHandler()

		handler.merge(Delta(tool_calls=[{
			"index": 0,
			"id": "call-1",
			"type": "function",
			"function": {
				"name": "read_file",
				"arguments": "{\"path\":",
			},
		}]))
		handler.merge(Delta(tool_calls=[{
			"index": 0,
			"function": {"arguments": "\"/tmp/demo.py\""},
		}]))
		handler.merge(Delta(tool_calls=[{
			"index": 0,
			"id": "",
			"type": "",
			"function": {"name": "", "arguments": "}"},
		}]))

		tool_call = handler.build_tool_calling_context().tool_calls[0]

		self.assertEqual(tool_call.id, "call-1")
		self.assertEqual(tool_call.type, "function")
		self.assertEqual(tool_call.function.name, "read_file")
		self.assertEqual(tool_call.function.arguments, "{\"path\":\"/tmp/demo.py\"}")

	def test_missing_or_empty_function_deltas_do_not_replace_existing_fields(self):
		handler = StreamHandler()

		handler.merge(Delta(tool_calls=[{
			"index": 0,
			"id": "call-1",
			"type": "function",
			"function": {
				"name": "read_file",
				"arguments": "{}",
			},
		}]))
		handler.merge(Delta(tool_calls=[{
			"index": 0,
			"id": "call-2",
			"type": "custom",
		}]))
		handler.merge(Delta(tool_calls=[{
			"index": 0,
			"function": {},
		}]))

		tool_call = handler.build_tool_calling_context().tool_calls[0]

		self.assertEqual(tool_call.id, "call-2")
		self.assertEqual(tool_call.type, "custom")
		self.assertEqual(tool_call.function.name, "read_file")
		self.assertEqual(tool_call.function.arguments, "{}")


if __name__ == "__main__":
	unittest.main()
