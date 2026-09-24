from __future__ import annotations

from ..protocols	import SkillsManagerProtocol

from typing import List, TYPE_CHECKING, Union

if TYPE_CHECKING:
	
	from ..models	import Skill


class SkillsManager(SkillsManagerProtocol):
	
	def __init__(self, skills: List[Skill]):
		
		# 建立索引表，实现 O(1) 查找
		self.skills = {skill.name: skill for skill in skills}
	
	def find(self, *keywords) -> List[Skill]:
		
		"""这个方法更准确的名称应该是 match（匹配）"""
		
		# 已经找到的
		found_skills = []
		
		for skill in self.skills.values():
			
			is_found = any(
				keyword in skill.name or
				keyword in skill.description or
				keyword in skill.content
				for keyword in keywords
			)
			
			if is_found:
				found_skills.append(skill)
		
		return found_skills
	
	def add(self, skill: Skill) -> None:
		
		if skill.name not in self.skills:
			self.skills[skill.name] = skill
	
	def remove(self, skill: Skill) -> None:
		self.skills.pop(skill.name, None)
	
	def get_by_name(self, skill_name: str) -> Union[Skill, None]:
		
		return self.skills.get(skill_name)