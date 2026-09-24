from __future__ import annotations

from abc	import ABC, abstractmethod
from typing	import Optional, List, TYPE_CHECKING


if TYPE_CHECKING:
	
	from ..models import Skill


class SkillsManagerProtocol(ABC):
	
	@abstractmethod
	def find(self, keyword: str) -> List[Skill]:
		
		"""通过关键字查找技能"""
		...
	
	@abstractmethod
	def add(self, skill: Skill) -> None:
		
		"""添加技能"""
		...
	
	@abstractmethod
	def remove(self, skill: Skill) -> None:
		
		"""移除技能"""
		...