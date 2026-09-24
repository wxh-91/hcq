from .base_context	import BaseContext, Field
from ...enums		import Roles

from typing	import Literal, Optional


class SystemContext(BaseContext):
	role				: Literal[Roles.SYSTEM] = Roles.SYSTEM
	reasoning_content	: Optional[str] = Field(default=None, exclude=True)