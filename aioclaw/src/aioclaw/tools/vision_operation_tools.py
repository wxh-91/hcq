from __future__ import annotations

import aiofiles

from ..protocols	import ToolsManagerProtocol
from ..utils		import build_tool_schema
from .base_tool		import BaseTool

from aioverse.models	import ImageBase64Segment, TextSegment

from base64	import b64encode
from typing	import List, Optional, Union

import os


MAX_IMAGE_BYTES = 5 * 1024 * 1024


ViewPhotoSchema = build_tool_schema(
	tool_name			= "view_photo",
	tool_description	= "查看一个本地图片，仅多模态模型支持；支持 PNG、JPEG、GIF、WebP，单个文件最大 5 MiB",
	arguments			= {
		"file_path": ("string", "图片路径")
	}
)


class VisionOperationTools(BaseTool):

	def register(self, tools_manager: ToolsManagerProtocol):

		super().register(tools_manager)

		tools_manager.register(self.view_photo, ViewPhotoSchema)

	def _get_media_type(self, file_bytes: bytes) -> Optional[str]:

		"""根据文件内容识别支持的图片 MIME 类型。"""

		if file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
			return "image/png"

		if file_bytes.startswith(b"\xff\xd8\xff"):
			return "image/jpeg"

		if file_bytes.startswith((b"GIF87a", b"GIF89a")):
			return "image/gif"

		if file_bytes.startswith(b"RIFF") and file_bytes[8:12] == b"WEBP":
			return "image/webp"

		return None

	async def _read_image_file(self, file_path: str) -> Union[bytes, str]:

		"""读取受大小限制的图片文件。"""

		try:
			file_size = os.path.getsize(file_path)

		except OSError as exception:
			return f"读取 {file_path} 失败: {type(exception).__name__}: {exception}"

		if file_size > MAX_IMAGE_BYTES:
			return f"{file_path} 超过图片大小限制 {MAX_IMAGE_BYTES // 1024 // 1024} MiB"

		try:
			async with aiofiles.open(file_path, "rb") as file:
				file_bytes = await file.read(MAX_IMAGE_BYTES + 1)

		except OSError as exception:
			return f"读取 {file_path} 失败: {type(exception).__name__}: {exception}"

		if len(file_bytes) > MAX_IMAGE_BYTES:
			return f"{file_path} 超过图片大小限制 {MAX_IMAGE_BYTES // 1024 // 1024} MiB"

		return file_bytes

	async def view_photo(self, file_path: str) -> Union[List[Union[TextSegment, ImageBase64Segment]], str]:

		"""读取本地图片并返回工具结果中的说明和多模态图片内容。"""

		if not isinstance(file_path, str) or not file_path:
			return "file_path 必须是非空字符串"

		if not os.path.isfile(file_path):
			return f"{file_path} 不是一个文件或不存在"

		file_bytes = await self._read_image_file(file_path)
		if isinstance(file_bytes, str):
			return file_bytes

		if (media_type := self._get_media_type(file_bytes)) is None:
			return f"{file_path} 不是支持的图片格式，仅支持 PNG、JPEG、GIF、WebP"

		p_segment	= ImageBase64Segment(
			data		= b64encode(file_bytes).decode("utf-8"),
			media_type	= media_type,
		)
		t_segment	= TextSegment(text="图片附件已添加到工具结果上下文，请结合附件继续完成任务")

		return [t_segment, p_segment]
