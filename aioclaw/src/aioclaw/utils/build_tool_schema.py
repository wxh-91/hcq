from __future__ import annotations

from ..models import (
	Tool,
	Function,
	Parameters,
	Argument,
	EmptyObject,
)

from typing import Dict, Any, Optional, Union, List


# ============================================================
# ArgDef 参数定义格式:
#
#   基础格式（向后兼容）:
#     (类型, 描述)              → 必填，无默认值
#     (类型, 描述, 默认值)      → 可选，有默认值
#
#   高级格式（新增）:
#     (规格字典, 描述)          → 必填，字典可包含 type/enum/anyOf/oneOf/... 等
#     (规格字典, 描述, 默认值)  → 可选
#
#   规格字典支持的字段（均可选）:
#     type, enum, anyOf, oneOf, allOf, items,
#     minLength, maxLength, pattern, format,
#     minimum, maximum, exclusiveMinimum, exclusiveMaximum, multipleOf,
#     properties, required
# ============================================================

ArgDef = Union[
	tuple[str, str],					# (类型, 描述)
	tuple[str, str, Any],				# (类型, 描述, 默认值)
	tuple[Dict[str, Any], str],			# (规格字典, 描述)
	tuple[Dict[str, Any], str, Any],	# (规格字典, 描述, 默认值)
]


def _parse_arg_def(arg: ArgDef) -> Dict[str, Any]:
	
	"""将 ArgDef 统一解析为 Argument 构造参数"""
	
	if isinstance(arg[0], dict):
		spec		= dict(arg[0])
		description	= arg[1]
		default		= arg[2] if len(arg) >= 3 else EmptyObject
		
		# 如果使用了 anyOf / oneOf / allOf，则不需要默认的 type
		has_composition = any(
			spec.get(k) for k in ("anyOf", "oneOf", "allOf")
		)
		
		if "type" not in spec and not has_composition:
			spec["type"] = "string"
		
		result = {
			"description"	: description,
			"default"		: default,
		}
		
		# 合并 spec 中的字段（type 可能在 spec 中，也可能没有）
		if "type" in spec:
			result["type"] = spec.pop("type")
		else:
			result["type"] = "string"  # 兜底
		
		result.update(spec)
		
		return result
	else:
		return {
			"type"			: arg[0],
			"description"	: arg[1],
			"default"		: arg[2] if len(arg) >= 3 else EmptyObject,
		}


def get_requirements_by_arguments(arguments: Dict[str, ArgDef]) -> List[str]:
	
	"""提取必填参数名列表"""
	
	requirements = [
		name for name, arg in arguments.items()
		if len(arg) < 3
	]
	
	return requirements


def build_tool_schema(
	tool_name			: str,
	tool_description	: str,
	arguments			: Dict[str, ArgDef]
) -> Tool:
	
	"""
	快速构建工具 Schema
	
	基础示例:
		build_tool_schema(
			tool_name			= "get_weather",
			tool_description	= "获取指定城市的天气",
			arguments			= {
				"city"		: ("string", "城市名称"),
				"unit"		: ("string", "温度单位", "celsius"),
			}
		)
	
	高级示例 (anyOf / enum):
		build_tool_schema(
			tool_name			= "search",
			tool_description	= "搜索",
			arguments			= {
				"query"		: ("string", "搜索关键词"),
				"sort"		: ({"type": "string", "enum": ["asc", "desc"]}, "排序方式", "desc"),
				"value"		: ({"anyOf": [{"type":"string"}, {"type":"number"}]}, "值"),
			}
		)
	
	工具名称:			工具名称
	工具描述:	工具描述
	参数定义:			参数定义字典
	返回:					Tool Schema 对象
	"""
	
	properties = {
		name: Argument(**_parse_arg_def(arg))
		for name, arg in arguments.items()
	}
	
	parameters = Parameters(
		properties	= properties,
		required	= get_requirements_by_arguments(arguments)
	)
	
	function = Function(
		name		= tool_name,
		description	= tool_description,
		parameters	= parameters
	)

	return Tool(function=function)
