from __future__ import annotations

from typing import Any, Dict, List, Optional
from typing_extensions import Self

from ..models import BaseSchema, ObjectSchema, FunctionSchema, ToolSchema


class ToolSchemaBuilder:
	
	def __init__(self) -> None:	
		
		self._parameters_dict		: Optional[Dict[str, BaseSchema]]	= None
		self._required_parameters	: Optional[List[str]]				= None
		
		self._parameters_schema	: Optional[ObjectSchema]	= None
		self._function_schema	: Optional[FunctionSchema]	= None
		self._tool_schema		: Optional[ToolSchema]		= None
	
	
	@property
	def parameters_dict(self) -> Dict[str, BaseSchema]:
		
		if self._parameters_dict is None:
			self._parameters_dict = dict()
		
		return self._parameters_dict
	
	@property
	def required_parameters(self) -> List[str]:
		
		if self._required_parameters is None:
			self._required_parameters = list()
		
		return self._required_parameters
	
	
	def set_parameter(
		self,
		parameter_name	: str,
		parameter_schema: BaseSchema
	) -> Self:
		
		"""设置参数名对应的 schema"""
		
		self.parameters_dict[parameter_name] = parameter_schema
		
		return self
	
	def set_required(self, parameter_name: str) -> Self:
		
		"""设置必须的参数"""
		
		if parameter_name not in self.required_parameters:
			self.required_parameters.append(parameter_name)
		
		return self
		
	
	def build_parameters(self) -> Self:
		
		# 设置了必须参数才验证
		if self._required_parameters is not None:
			for required in self.required_parameters:	
				if required not in self.parameters_dict:
					raise RuntimeError(f"parameters 缺少 {required}")
		
		self._parameters_schema = ObjectSchema(properties=self.parameters_dict, required=self._required_parameters)
		return self
	
	def build_function(self, name: str) -> Self:
		
		if self._parameters_schema is None:
			raise RuntimeError("不可跳过build_parameters")
		
		self._function_schema = FunctionSchema(name=name, parameters=self._parameters_schema)
		return self
	
	def build_tool(self) -> Self:
		
		if self._function_schema is None:
			raise RuntimeError("不可跳过build_function")
		
		self._tool_schema = ToolSchema(function=self._function_schema)
		return self
	
	def to_schema(self) -> Dict[str, Any]:
		
		if self._tool_schema is None:
			raise RuntimeError("不可跳过build_tool")
		
		return self._tool_schema.to_schema()
