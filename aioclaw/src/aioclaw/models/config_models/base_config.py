from pydantic	import BaseModel, PrivateAttr

from typing		import Any, Self


class BaseConfig(BaseModel):
	
	_content: Any = PrivateAttr(default=None) # 防止被替换
	
	@property
	def content(self) -> Any:
		return self._content
	
	@classmethod
	def from_file(cls, path: str, **kwargs) -> Self:
		
		with open(path, **kwargs) as file:
			
			config_json = file.read()
		
		return cls.model_validate_json(config_json)