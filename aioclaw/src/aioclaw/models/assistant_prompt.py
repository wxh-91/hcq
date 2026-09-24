# 类型两件套
from pydantic	import BaseModel, Field

from typing		import Dict, Any, Self


# 系统提示词需要直接硬编码
# 你可以修改 但是不建议
system_prompt = (
"""
我是一个旗舰级的人工智能模型
我的沟通风格融合了深思熟虑的缜密逻辑与敏锐细腻的人文洞察


# 极致深度与严谨
我拒绝流于表面的泛泛之谈
面对复杂议题，我擅长逐层解构，展现出强大的推理与元认知能力
在给出结论前，我会反复推敲逻辑链条，确保输出的严密性与可靠性


# 建设性的务实导向
我的深刻服务于实用性
无论面对学术难题、代码编写、创意写作还是复杂决策，我都能将深邃的洞察转化为清晰、可执行的步骤或高质量的输出


# 互动准则
当我不确定时，会主动提出关键的澄清性问题
当我持有不同视角时，会以探索而非争辩的方式呈现
"""
)


class AssistantPrompt(BaseModel):

	system_prompt	: str				= Field(default=system_prompt)
	role_prompt		: str				= Field(default_factory=str)
	metadata		: Dict[str, Any]	= Field(default_factory=dict)

	def set_system_prompt(self, prompt: str) -> Self:
		self.system_prompt = prompt
		return self
	def set_role_prompt(self, prompt: str) -> Self:
		self.role_prompt = prompt
		return self
	def set_metadata(self, key: str, value: Any) -> Self:
		self.metadata[key] = value
		return self

	def unset_metadata(self, key: str) -> Self:
		self.metadata.pop(key, None)
		return self