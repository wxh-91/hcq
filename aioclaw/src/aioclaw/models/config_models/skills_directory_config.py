from pydantic	import PrivateAttr, model_validator, Field

from ...enums			import FileTypes
from ..skill			import Skill
from .path_config		import PathConfig

from typing		import TYPE_CHECKING, List, Literal, Self, Union
import os


SKILL_FILE_SUFFIX = "md"


class SkillsDirectoryConfig(PathConfig):
	
	type: Literal[FileTypes.DIRECTORY] = FileTypes.DIRECTORY
	path: Union[str, None] = Field(default=None)
	
	_content: List[Skill] = PrivateAttr(default_factory=list)
	
	@property
	def content(self) -> List[Skill]:
		return self._content
	
	@staticmethod
	def _is_skill_file(path: str) -> bool:
	
		"""判断路径指向的文件是否为SKILL_FILE_SUFFIX"""
		
		if not os.path.isfile(path): return False
		
		file_suffix				= path.split(".")[-1].lower()
		default_skill_suffix	= SKILL_FILE_SUFFIX.lower()
		
		return file_suffix == default_skill_suffix
	
	@model_validator(mode="after")
	def skills2objects(self) -> Self:
		
		"""
		自动遍历self.path的所有skill markdown文件
		转为Skill对象 写回self._skill_objects_list
		"""
		
		if self.path is None:
			return self
		
		for skill_path in os.listdir(self.path):
			
			skill_absolute_path = os.path.join(self.path, skill_path)
			
			if self._is_skill_file(skill_absolute_path):
				self._content.append(Skill.from_file(skill_absolute_path))
		
		return self
	
	@model_validator(mode="after")
	def init_object(self) -> Self:
	
		if self.path is not None:
			self._make_dirs(exist_ok=True)
		
		return self