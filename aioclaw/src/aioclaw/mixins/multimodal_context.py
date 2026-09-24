from __future__ import annotations

from aioverse.models import (
	BaseContext,
	BaseSegment,
	ImageUrlSegment,
	ToolOutputContext,
)

from ..protocols import MultimodalContextHostProtocol
from ..services import (
	ContextProjectionCapabilities,
	ContextRequestProjector,
)

from typing import Any, Dict, List, Optional, Tuple, cast


class MultimodalContextMixin:

	"""将宿主状态适配到无状态的请求上下文投影服务。"""

	def __init__(
		self,
		*,
		context_request_projector: Optional[ContextRequestProjector] = None,
		**kwargs,
	):
		super().__init__(**kwargs)
		self._context_request_projector = context_request_projector

	@property
	def context_request_projector(self) -> ContextRequestProjector:

		if getattr(self, "_context_request_projector", None) is None:
			self._context_request_projector = ContextRequestProjector()

		return self._context_request_projector

	def _get_projection_host(self) -> MultimodalContextHostProtocol:
		return cast(MultimodalContextHostProtocol, self)

	def _get_projection_capabilities(self) -> ContextProjectionCapabilities:

		config = self._get_projection_host().assistant_model_config
		return ContextProjectionCapabilities(
			support_image=getattr(config, "support_image", False),
			support_audio=getattr(config, "support_audio", False),
			support_video=getattr(config, "support_video", False),
			tool_output_image_mode=getattr(
				config,
				"tool_output_image_mode",
				"tool",
			),
		)

	def _get_segment_modality(self, segment: BaseSegment) -> Optional[str]:
		return self.context_request_projector.get_segment_modality(segment)

	def _get_supported_segments(
		self,
		content: Any,
	) -> Tuple[List[BaseSegment], List[str]]:
		return self.context_request_projector.get_supported_segments(
			content,
			self._get_projection_capabilities(),
		)

	@staticmethod
	def _get_unsupported_modalities_text(modalities: List[str]) -> str:
		return ContextRequestProjector.get_unsupported_modalities_text(modalities)

	@staticmethod
	def _is_convertible_image_segment(segment: BaseSegment) -> bool:
		return ContextRequestProjector.is_convertible_image_segment(segment)

	@classmethod
	def _get_non_follow_up_image_segments(
		cls,
		segments: List[BaseSegment],
	) -> List[BaseSegment]:
		return ContextRequestProjector.get_non_follow_up_image_segments(segments)

	def _get_tool_output_text(
		self,
		segments: List[BaseSegment],
		unsupported_modalities: List[str],
	) -> str:
		return self.context_request_projector.get_tool_output_text(
			segments,
			unsupported_modalities,
		)

	@classmethod
	def _get_image_url_segments(
		cls,
		segments: List[BaseSegment],
	) -> List[ImageUrlSegment]:
		return ContextRequestProjector.get_image_url_segments(segments)

	def _get_tool_output_image_segments(
		self,
		tool_output: ToolOutputContext,
	) -> List[ImageUrlSegment]:
		return self.context_request_projector.get_tool_output_image_segments(
			tool_output
		)

	def _get_follow_up_tool_image_segments(
		self,
		tool_output: ToolOutputContext,
	) -> List[ImageUrlSegment]:
		return self.context_request_projector.get_follow_up_tool_image_segments(
			tool_output,
			self._get_projection_capabilities(),
		)

	def _get_tool_output_request_message(
		self,
		tool_output: ToolOutputContext,
		segments: List[BaseSegment],
		unsupported_modalities: List[str],
		*,
		remove_follow_up_images: bool = False,
	) -> Dict[str, Any]:
		return self.context_request_projector.get_tool_output_request_message(
			tool_output,
			segments,
			unsupported_modalities,
			remove_follow_up_images=remove_follow_up_images,
		)

	def _get_context_request_message(
		self,
		context: BaseContext,
		segments: List[BaseSegment],
		unsupported_modalities: List[str],
	) -> Dict[str, Any]:
		return self.context_request_projector.get_context_request_message(
			context,
			segments,
			unsupported_modalities,
		)

	@staticmethod
	def _build_tool_image_follow_up_message(
		image_segments: List[ImageUrlSegment],
	) -> Dict[str, Any]:
		return ContextRequestProjector.build_tool_image_follow_up_message(
			image_segments
		)

	def _flush_tool_image_attachments(
		self,
		request_messages: List[Dict[str, Any]],
		image_segments: List[ImageUrlSegment],
	) -> None:
		ContextRequestProjector._flush_tool_image_attachments(
			request_messages,
			image_segments,
		)

	def _get_request_messages(self) -> List[Dict[str, Any]]:

		"""返回按当前模型能力投影后的请求上下文，不修改会话原数据。"""

		host = self._get_projection_host()
		contexts = host.assistant_session.contexts_status.flatten_contexts()
		return self.context_request_projector.project(
			contexts,
			self._get_projection_capabilities(),
		)
