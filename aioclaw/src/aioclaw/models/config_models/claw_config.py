from __future__ import annotations

from pydantic	import ConfigDict, Field, model_validator

from .assistant_model_config	import AssistantModelConfig
from .assistant_runtime_config	import AssistantRuntimeConfig
from .base_config				import BaseConfig
from .path_config				import PathConfig
from .skills_directory_config	import SkillsDirectoryConfig

from typing		import Any, List, Optional


class ClawConfig(BaseConfig):

	"""全局运行配置。"""

	model_config = ConfigDict(validate_assignment=True)

	models_config: List[AssistantModelConfig] = Field(default_factory=list)

	context_compression_keep_contexts: int = Field(
		default=4,
		ge=1,
		description="API 压缩时保留的最新顶层上下文数量",
	)
	context_compression_max_tokens: Optional[int] = Field(
		default=2048,
		ge=1,
		description="API 压缩摘要的最大输出 token 数；None 表示不设置上限",
	)

	paths_config	: List[PathConfig]		= Field(default_factory=list)
	skills_config	: SkillsDirectoryConfig	= Field(default_factory=SkillsDirectoryConfig)

	assistant_runtime_config: AssistantRuntimeConfig = Field(
		default_factory=AssistantRuntimeConfig
	)

	@model_validator(mode="before")
	@classmethod
	def validate_context_compression_config(cls, data: Any):
		"""校验上下文压缩配置的类型和取值范围。"""
		if not isinstance(data, dict):
			return data

		keep_contexts = data.get("context_compression_keep_contexts", 4)
		if (
			isinstance(keep_contexts, bool)
			or not isinstance(keep_contexts, int)
			or keep_contexts < 1
		):
			raise ValueError(
				"context_compression_keep_contexts 必须是大于等于 1 的整数"
			)

		max_tokens = data.get("context_compression_max_tokens", 2048)
		if (
			max_tokens is not None
			and (
				isinstance(max_tokens, bool)
				or not isinstance(max_tokens, int)
				or max_tokens < 1
			)
		):
			raise ValueError(
				"context_compression_max_tokens 必须是正整数或 None"
			)

		return data
