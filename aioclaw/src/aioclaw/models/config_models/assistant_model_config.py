from pydantic import model_validator, Field

from ..assistant_key import AssistantKey
from .base_config import BaseConfig

from typing import List, Optional, Dict, Any, Literal


class AssistantModelConfig(BaseConfig):

	"""单个模型的连接、能力和上下文预算配置。"""

	api_url		: str
	model_name	: str
	model_keys	: List[AssistantKey] = Field(default_factory=list)

	max_context_length			: int = Field(..., description="模型明确的上下文长度")
	cleanup_threshold			: int = Field(..., description="触发清理逻辑的上下文长度")
	reserved_completion_tokens	: int = Field(default=0, ge=0, description="为正常模型输出预留的 token 数")
	context_safety_margin		: int = Field(default=0, ge=0, description="上下文估算误差与供应商开销的安全余量")

	support_thinking	: bool = Field(default=False, description="是否支持设置思考")
	support_tool		: bool = Field(default=False, description="是否支持工具调用")
	support_streaming	: bool = Field(default=True, description="是否支持流式输出")
	support_image		: bool = Field(default=False, description="是否支持图片理解")
	support_video		: bool = Field(default=False, description="是否支持视频理解")
	support_audio		: bool = Field(default=False, description="是否支持音频理解")

	tool_output_image_mode: Literal["tool", "follow_up_user"] = Field(default="tool", description="图片工具输出的请求适配方式")


	@model_validator(mode="before")
	@classmethod
	def init_cleanup_threshold(cls, data: Dict[str, Any]):

		max_context_length	= data.get("max_context_length")
		cleanup_threshold	= data.get("cleanup_threshold")

		if max_context_length is None:
			raise RuntimeError("上下文长度必须设置")

		if max_context_length < 1:
			raise ValueError("max_context_length 必须是正整数")

		if cleanup_threshold is None:
			data["cleanup_threshold"] = max(1, int(max_context_length * 0.75))

		return data

	@model_validator(mode="after")
	def validate_context_budget(self):

		if self.cleanup_threshold < 1:
			raise ValueError("cleanup_threshold 必须是正整数")

		if self.cleanup_threshold > self.max_context_length:
			raise ValueError("cleanup_threshold 不能超过 max_context_length")

		if self.reserved_completion_tokens + self.context_safety_margin >= self.max_context_length:
			raise ValueError("reserved_completion_tokens + context_safety_margin 必须小于 max_context_length")

		return self
