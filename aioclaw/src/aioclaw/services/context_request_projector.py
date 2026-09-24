from __future__ import annotations

from dataclasses import dataclass

from aioverse.models import (
	BaseContext,
	BaseSegment,
	ImageBase64Segment,
	ImageUrlSegment,
	TextSegment,
	ToolOutputContext,
	UserContext,
)

from typing import Any, Dict, List, Optional, Sequence, Tuple


IMAGE_SEGMENT_TYPES = (ImageBase64Segment, ImageUrlSegment)
AUDIO_SEGMENT_TYPES = {"input_audio", "audio_url"}
VIDEO_SEGMENT_TYPES = {"video", "video_url", "input_video"}
MODALITY_NAMES = {
	"image": "图片",
	"audio": "音频",
	"video": "视频",
}


@dataclass(frozen=True)
class ContextProjectionCapabilities:

	support_image: bool = False
	support_audio: bool = False
	support_video: bool = False
	tool_output_image_mode: str = "tool"


class ContextRequestProjector:

	"""将会话上下文投影为当前模型可以发送的请求消息。"""

	def project(
		self,
		contexts: Sequence[BaseContext],
		capabilities: ContextProjectionCapabilities,
	) -> List[Dict[str, Any]]:

		request_messages = []
		pending_image_segments = []

		for context in contexts:
			segments, unsupported_modalities = self.get_supported_segments(
				context.content,
				capabilities,
			)

			if not isinstance(context, ToolOutputContext):
				self._flush_tool_image_attachments(
					request_messages,
					pending_image_segments,
				)
				request_messages.append(self.get_context_request_message(
					context,
					segments,
					unsupported_modalities,
				))
				continue

			current_images = self.get_follow_up_tool_image_segments(
				context,
				capabilities,
			)
			if not current_images:
				request_messages.append(self.get_context_request_message(
					context,
					segments,
					unsupported_modalities,
				))
				continue

			pending_image_segments.extend(current_images)
			request_messages.append(self.get_tool_output_request_message(
				context,
				segments,
				unsupported_modalities,
				remove_follow_up_images=True,
			))

		self._flush_tool_image_attachments(
			request_messages,
			pending_image_segments,
		)

		return request_messages

	def get_segment_modality(self, segment: BaseSegment) -> Optional[str]:

		if isinstance(segment, IMAGE_SEGMENT_TYPES):
			return "image"

		if segment.type in AUDIO_SEGMENT_TYPES:
			return "audio"

		if segment.type in VIDEO_SEGMENT_TYPES:
			return "video"

		return None

	def get_supported_segments(
		self,
		content: Any,
		capabilities: ContextProjectionCapabilities,
	) -> Tuple[List[BaseSegment], List[str]]:

		supported_segments = []
		unsupported_modalities = []

		if not isinstance(content, list):
			return supported_segments, unsupported_modalities

		for segment in content:
			if not isinstance(segment, BaseSegment):
				continue

			modality = self.get_segment_modality(segment)
			if modality is None:
				supported_segments.append(segment)
				continue

			if getattr(capabilities, f"support_{modality}", False):
				supported_segments.append(segment)
				continue

			if modality not in unsupported_modalities:
				unsupported_modalities.append(modality)

		return supported_segments, unsupported_modalities

	@staticmethod
	def get_unsupported_modalities_text(modalities: List[str]) -> str:

		names = "、".join(MODALITY_NAMES[modality] for modality in modalities)
		return f"当前模型不支持{names}输入，附件未发送。"

	@staticmethod
	def is_convertible_image_segment(segment: BaseSegment) -> bool:

		if isinstance(segment, ImageUrlSegment):
			return bool(segment.url)

		return (
			isinstance(segment, ImageBase64Segment)
			and bool(segment.data)
			and bool(segment.media_type)
			and segment.media_type.startswith("image/")
		)

	@classmethod
	def get_non_follow_up_image_segments(
		cls,
		segments: List[BaseSegment],
	) -> List[BaseSegment]:

		return [
			segment
			for segment in segments
			if not cls.is_convertible_image_segment(segment)
		]

	def get_tool_output_text(
		self,
		segments: List[BaseSegment],
		unsupported_modalities: List[str],
	) -> str:

		texts = [
			segment.text
			for segment in segments
			if isinstance(segment, TextSegment)
		]

		if unsupported_modalities:
			texts.append(
				self.get_unsupported_modalities_text(unsupported_modalities)
			)

		return "\n".join(texts) if texts else "工具返回了多模态附件。"

	@classmethod
	def get_image_url_segments(
		cls,
		segments: List[BaseSegment],
	) -> List[ImageUrlSegment]:

		image_segments = []
		for segment in segments:
			if not cls.is_convertible_image_segment(segment):
				continue

			if isinstance(segment, ImageUrlSegment):
				image_segments.append(segment)
				continue

			image_segments.append(ImageUrlSegment(
				url=f"data:{segment.media_type};base64,{segment.data}",
			))

		return image_segments

	def get_tool_output_image_segments(
		self,
		tool_output: ToolOutputContext,
	) -> List[ImageUrlSegment]:

		"""将工具结果中的 aioverse 图片段转换为标准 image_url 段。"""

		if not isinstance(tool_output.content, list):
			return []

		return self.get_image_url_segments([
			segment
			for segment in tool_output.content
			if isinstance(segment, BaseSegment)
		])

	def get_follow_up_tool_image_segments(
		self,
		tool_output: ToolOutputContext,
		capabilities: ContextProjectionCapabilities,
	) -> List[ImageUrlSegment]:

		if (
			not capabilities.support_image
			or capabilities.tool_output_image_mode != "follow_up_user"
		):
			return []

		return self.get_tool_output_image_segments(tool_output)

	def get_tool_output_request_message(
		self,
		tool_output: ToolOutputContext,
		segments: List[BaseSegment],
		unsupported_modalities: List[str],
		*,
		remove_follow_up_images: bool = False,
	) -> Dict[str, Any]:

		if not remove_follow_up_images and not unsupported_modalities:
			return tool_output.model_dump(mode="json", exclude_none=True)

		if remove_follow_up_images:
			segments = self.get_non_follow_up_image_segments(segments)

		text = self.get_tool_output_text(segments, unsupported_modalities)
		non_text_segments = [
			segment
			for segment in segments
			if not isinstance(segment, TextSegment)
		]
		content = text if not non_text_segments else [
			TextSegment(text=text),
			*non_text_segments,
		]

		return tool_output.model_copy(
			update={"content": content}
		).model_dump(mode="json", exclude_none=True)

	def get_context_request_message(
		self,
		context: BaseContext,
		segments: List[BaseSegment],
		unsupported_modalities: List[str],
	) -> Dict[str, Any]:

		if not unsupported_modalities:
			return context.model_dump(mode="json", exclude_none=True)

		if isinstance(context, ToolOutputContext):
			return self.get_tool_output_request_message(
				context,
				segments,
				unsupported_modalities,
			)

		segments = [
			*segments,
			TextSegment(text=self.get_unsupported_modalities_text(
				unsupported_modalities
			)),
		]
		return context.model_copy(
			update={"content": segments}
		).model_dump(mode="json", exclude_none=True)

	@staticmethod
	def build_tool_image_follow_up_message(
		image_segments: List[ImageUrlSegment],
	) -> Dict[str, Any]:

		"""构造仅用于请求的图片附件上下文，不写回会话。"""

		return UserContext(
			content=image_segments
		).model_dump(mode="json", exclude_none=True)

	@staticmethod
	def _flush_tool_image_attachments(
		request_messages: List[Dict[str, Any]],
		image_segments: List[ImageUrlSegment],
	) -> None:

		if not image_segments:
			return

		request_messages.append(
			ContextRequestProjector.build_tool_image_follow_up_message(
				image_segments
			)
		)
		image_segments.clear()
