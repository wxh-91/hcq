from __future__ import annotations
from pydantic	import model_validator, Field

from ...enums		import FileTypes
from .path_config	import PathConfig

from typing		import Optional, Self, Literal


class EnvConfig(PathConfig):
	
	path	: Optional[str] = Field(default=None) # 不必须
	type	: Literal[FileTypes.FILE, FileTypes.ENV] # 不能为文件夹
	
	env_content	: Optional[str] = Field(default=None)
	
	@property
	def content(self) -> Optional[str]:
		return env_content
	
	def _update_content(self, content: str):
		
		"""重写 使其指向 env_content"""
		
		self.env_content = content
	
	@model_validator(mode="after")
	def check_format(self) -> Self:
		
		"""
		确保不会让self.type是FileTypes.file时缺失path
		"""
		
		if self.type == FileTypes.FILE and self.path is None:
			raise RuntimeError(f"type为 {self.type} 时缺失'path'字段")
		
		return self