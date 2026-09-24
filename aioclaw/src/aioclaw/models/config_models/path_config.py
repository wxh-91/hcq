from pydantic	import PrivateAttr, model_validator, field_validator

from .base_config	import BaseConfig
from ...enums		import FileTypes

from typing		import Optional, Self, Literal, Union
import os


class PathConfig(BaseConfig):
	
	path: str
	type: FileTypes
	
	_content: Optional[str] = PrivateAttr(default=None)
	
	@property
	def content(self) -> Union[str, None]:
		return self._content
	
	def _update_content(self, content: str):
		self._content = content
	
	def _read_file(self, **kwargs) -> None:
		
		"""读取之后直接_update_content 不返回"""
		
		with open(self.path, **kwargs) as file:
			self._update_content(file.read())
	
	def _make_dirs(self, **kwargs) -> None:
		
		"""创建文件夹"""
		os.makedirs(self.path, **kwargs)
	
	@model_validator(mode="after")
	def init_object(self) -> Self:
	
		if self.type == FileTypes.FILE:
			self._read_file(encoding="utf-8")
		else:
			self._make_dirs(exist_ok=True)
		
		return self
	
	@field_validator("type", mode="before")
	@classmethod
	def _coerce_str_to_enum(cls, value):
		
		if isinstance(value, str):
			return FileTypes(value)
		
		return value