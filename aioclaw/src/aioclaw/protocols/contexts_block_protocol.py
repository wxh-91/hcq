from __future__ import annotations

from pydantic	import BaseModel

from aioverse.models import BaseContext

from abc		import ABC, abstractmethod
from typing		import Optional, List, Iterator, TYPE_CHECKING


if TYPE_CHECKING:
	...
	

class ContextsBlockProtocol(ABC, BaseModel):
	
	@abstractmethod
	def __iter__(self) -> Iterator[BaseContext]: ...
	
	@abstractmethod
	def __len__(self) -> int: ...
	
	@abstractmethod
	def delete(self, index: int): ...
	
	@abstractmethod
	def insert(self, index: int, BaseContext: BaseContext): ...
	
	@abstractmethod
	def append(self, BaseContext: BaseContext): ...