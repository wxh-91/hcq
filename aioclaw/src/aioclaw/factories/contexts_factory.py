from __future__ import annotations
from .pydantic_models_factory	import PydanticModelsFactory
from aioverse.models	import (

	BaseContext,
	SystemContext,
	ToolCallingContext,
	ToolOutputContext,
	UserContext,
	AssistantContext
)

from typing	import Any


class ContextsFactory(PydanticModelsFactory):

	"""此处逻辑无需更改。"""
	...


# 全局单例 自动注册
# 注意注册顺序
# ToolCallingContext 的角色也是 assistant，所以要放在 AssistantContext 之前
contexts_factory = ContextsFactory()
contexts_factory.register(ToolCallingContext, 1)
contexts_factory.register(ToolOutputContext, 2)
contexts_factory.register(SystemContext, 3)
contexts_factory.register(AssistantContext, 4)
contexts_factory.register(UserContext, 5)
contexts_factory.register(BaseContext, 6)


def restore_context_data(data: Any) -> Any:

	"""将持久化的普通上下文数据恢复为具体 aioverse 模型。"""

	if not isinstance(data, dict):
		return data

	if any(key in data for key in ("tool_calling", "tool_outputs", "contexts")):
		return data

	context = contexts_factory.dispatcher(data)
	return data if context is None else context
