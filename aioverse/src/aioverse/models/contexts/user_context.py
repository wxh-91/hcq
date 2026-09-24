from .base_context	import BaseContext, Field
from ...enums		import Roles

from typing	import Literal, Optional


class UserContext(BaseContext):
	role				: Literal[Roles.USER] = Roles.USER
	reasoning_content	: Optional[str] = Field(default=None, exclude=True)