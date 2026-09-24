from pydantic	import (
	BaseModel,
	Field,
	ConfigDict
)

from .contexts_status	import ContextsStatus
from ..enums			import ThinkingEfforts, ThinkingModes

from typing	import Optional, Dict, Any, Self, Union
from uuid	import uuid4, UUID


class AssistantSession(BaseModel):

	model_config = ConfigDict(validate_assignment=True)

	session_uuid: Union[UUID, str] = Field(default_factory=uuid4)

	assistant_model_name	: str				= Field(..., description="该会话使用的AI模型名称")
	contexts_status			: ContextsStatus	= Field(default_factory=ContextsStatus, description="上下文状态")
	assistant_think_mode	: ThinkingModes		= Field(default=ThinkingModes.ENABLED, description="思考是否开启")
	assistant_think_effort	: ThinkingEfforts	= Field(default=ThinkingEfforts.MAX, description="思考强度")


	# ----- 设置方法 -----
	def set_model_name(self, name: str):
		self.assistant_model_name = name
	def set_think_mode(self, mode: ThinkingModes):
		self.assistant_think_mode = mode
	def set_think_effort(self, effort: ThinkingEfforts):
		self.assistant_think_effort = effort


	# 持久化
	def to_file(self, path: str, **kwargs):
		with open(path, mode="w", **kwargs) as file:
			file.write(self.model_dump_json())

	@classmethod
	def from_file(cls, path: str, **kwargs) -> Self:
		with open(path, **kwargs) as file:
			data = file.read()
		return cls.model_validate_json(data)