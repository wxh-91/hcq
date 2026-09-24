from __future__ import annotations

from pydantic	import BaseModel, model_serializer, Field, ConfigDict
from typing		import Any, List, Optional, Dict, Union


EmptyObject = object()

# 蛇形 → 驼峰 字段名映射
_FIELD_ALIAS_MAP = {
	"any_of"				: "anyOf",
	"one_of"				: "oneOf",
	"all_of"				: "allOf",
	"min_length"			: "minLength",
	"max_length"			: "maxLength",
	"exclusive_minimum"		: "exclusiveMinimum",
	"exclusive_maximum"		: "exclusiveMaximum",
	"multiple_of"			: "multipleOf",
}


class Argument(BaseModel):
	
	"""工具参数 Schema 定义，支持 JSON Schema 常用特性。"""
	
	model_config = ConfigDict(populate_by_name=True)
	
	# ---- 基础字段 ----
	type		: Union[str, List[str]]
	description	: str
	default		: Any = EmptyObject
	
	# ---- 枚举与组合约束 ----
	enum		: Optional[List[Any]]				= None
	any_of		: Optional[List[Dict[str, Any]]]	= Field(default=None, alias="anyOf")
	one_of		: Optional[List[Dict[str, Any]]]	= Field(default=None, alias="oneOf")
	all_of		: Optional[List[Dict[str, Any]]]	= Field(default=None, alias="allOf")
	
	# ---- 数组相关 ----
	items		: Optional[Dict[str, Any]]			= None
	
	# ---- 字符串约束 ----
	min_length	: Optional[int]						= Field(default=None, alias="minLength")
	max_length	: Optional[int]						= Field(default=None, alias="maxLength")
	pattern		: Optional[str]						= None
	format		: Optional[str]						= None
	
	# ---- 数值约束 ----
	minimum				: Optional[float]			= None
	maximum				: Optional[float]			= None
	exclusive_minimum	: Optional[float]			= Field(default=None, alias="exclusiveMinimum")
	exclusive_maximum	: Optional[float]			= Field(default=None, alias="exclusiveMaximum")
	multiple_of			: Optional[float]			= Field(default=None, alias="multipleOf")
	
	# ---- 嵌套对象 ----
	properties			: Optional[Dict[str, Argument]]	= None
	required			: Optional[List[str]]			= None
	
	@model_serializer(mode="wrap")
	def serialize(self, handler) -> Dict[str, Any]:
		
		"""序列化时移除 None 值，并将蛇形字段名转换为驼峰形式。"""
		
		data = handler(self)
		
		# 处理 default
		if self.default is not EmptyObject:
			data["default"] = self.default
		else:
			data.pop("default", None)
		
		# 移除所有 None 值字段
		keys_to_remove = [
			k for k, v in data.items()
			if v is None and k not in ("type", "description")
		]
		for k in keys_to_remove:
			data.pop(k)
		
		# 蛇形 → 驼峰 字段名转换
		for snake, camel in _FIELD_ALIAS_MAP.items():
			if snake in data:
				data[camel] = data.pop(snake)
		
		return data
	
	def set_default(self, value: Any):
		
		"""设置默认值"""
		
		self.default = value
	
	def is_required(self) -> bool:
		
		"""判断该参数是否必填"""
		
		return self.default is EmptyObject
