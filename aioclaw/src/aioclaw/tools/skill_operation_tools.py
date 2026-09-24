from __future__ import annotations
import orjson

from ..models		import AssistantPrompt
from ..protocols	import ToolsManagerProtocol, SkillsManagerProtocol
from ..utils		import build_tool_schema
from .base_tool		import BaseTool

from typing import Dict, Optional


# 找技能
FindSkillsSchema = build_tool_schema(
	tool_name			= "find_skills",
	tool_description	= "查找多个技能 返回一个技能名与技能描述的映射表",
	arguments			= {
		"keywords": ("string", "查找关键词 多个关键词使用空格分割")
	}
)

# 读技能
ReadSkillSchema = build_tool_schema(
	tool_name			= "read_skill",
	tool_description	= "读取(学习)一个技能",
	arguments			= {
		"skill_name"	: ("string", "技能名"),
		"length"		: ("integer", "读取的长度"),
		"offset"		: ("integer", "读取偏移量 默认从头开始", 0),
		"learn_skill"	: ("boolean", "是否学习技能", False)
	}
)


# 技能工具
class SkillOperationTools(BaseTool):
	
	def __init__(
		self,
		skills_manager_instance	: Optional[SkillsManagerProtocol]	= None,
		assistant_prompt		: Optional[AssistantPrompt]			= None,
		*args,
		**kwargs
	):
		
		super().__init__(*args, **kwargs)
		
		self.skills_manager_instance	= skills_manager_instance
		self.assistant_prompt			= assistant_prompt
	
	def register(self, tools_manager: ToolsManagerProtocol):
		
		super().register(tools_manager)
		
		tools_manager.register(self.find_skills, FindSkillsSchema)
		tools_manager.register(self.read_skill, ReadSkillSchema)
	
	def find_skills(
		self,
		keywords: str
	) -> str:
		
		if self.skills_manager_instance is None:
			
			return "技能管理器实例未注入"
		
		skills = self.skills_manager_instance.find(keywords)
		
		skills_format = {
			skill.name: skill.description
			for skill in skills
		}
		
		return orjson.dumps(skills_format).decode()
	
	def read_skill(
		self,
		skill_name	: str,
		length		: int,
		offset		: int	= 0,
		learn_skill	: bool	= False
	) -> str:
		
		if self.skills_manager_instance is None: return f"技能管理器实例未注入"
		
		# 找这个技能
		skill = self.skills_manager_instance.get_by_name(skill_name)
		
		if skill is None: return f"没有 {skill_name} 这个技能"
		
		# 需要学习技能
		if learn_skill is True:
			
			if self.assistant_prompt is None: return f"提示词管理器未注入"
			
			# 将技能注入assistant_prompt的skills
			self.assistant_prompt.learned_skills[skill.name] = skill.description
		
		return skill.content[offset: offset + length]