from .base_config import BaseConfig


class AssistantRuntimeConfig(BaseConfig):
	
	max_round	: int = 50
	timeout		: int = 300
	
	@property
	def content(self) -> str:
		return "杂鱼，这个模型有什么好看的呀？"