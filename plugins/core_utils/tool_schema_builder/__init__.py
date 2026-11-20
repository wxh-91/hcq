"""
tool_schema_builder — 构建 OpenAI Tool Call Schema 的工具包

提供类型安全的 Schema 模型定义和链式 Builder，支持 JSON Schema 标准子集：
string / integer / number / boolean / null / object / array / anyOf / oneOf / allOf
"""

from .core import ToolSchemaBuilder

from .models import (
    # 基类
    BaseSchema,
    BaseMultipleSchemas,
    # 简单类型
    StringSchema,
    IntegerSchema,
    NumberSchema,
    BooleanSchema,
    NullSchema,
    # 复合类型
    ObjectSchema,
    ArraySchema,
    # 组合类型
    AnyOfSchema,
    OneOfSchema,
    AllOfSchema,
    # 高阶类型
    FunctionSchema,
    ToolSchema,
    # 类型别名
    SchemaProperties,
    SchemaItems,
)

from .traits import ToSchemaAble

__all__ = [
    # builder
    "ToolSchemaBuilder",
    # traits
    "ToSchemaAble",
    # 基类
    "BaseSchema",
    "BaseMultipleSchemas",
    # 简单类型
    "StringSchema",
    "IntegerSchema",
    "NumberSchema",
    "BooleanSchema",
    "NullSchema",
    # 复合类型
    "ObjectSchema",
    "ArraySchema",
    # 组合类型
    "AnyOfSchema",
    "OneOfSchema",
    "AllOfSchema",
    # 高阶类型
    "FunctionSchema",
    "ToolSchema",
    # 类型别名
    "SchemaProperties",
    "SchemaItems",
]