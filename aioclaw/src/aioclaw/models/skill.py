# 类型两件套
from pydantic	import BaseModel, ConfigDict
import frontmatter

from typing		import Optional, Literal, Self

# Markdown 解析实现


class Skill(BaseModel):
	
	name		: str
	description	: str
	version		: Optional[str] = "0.1.0"
	content		: str
	
	@classmethod
	def from_markdown(cls, text: str) -> "cls":
		
		markdown = frontmatter.loads(text)
		
		return cls.model_validate({
			"name"			: markdown.get("name"),
			"description"	: markdown.get("description"),
			"version"		: markdown.get("version", "0"),
			"content"		: markdown.content.strip()
		})
	
	@classmethod
	def from_file(
		cls,
		path	: str,
		format	: Literal["markdown", "json"] = "markdown",
		encoding: str = "utf-8"
	) -> Self:

		with open(path, "r", encoding=encoding) as file:
			file_content = file.read()
		
		if format == "json":
			return cls.model_validate_json(file_content)
		else:
			return cls.from_markdown(file_content)
		
		raise RuntimeError(f"未知格式: {format}")