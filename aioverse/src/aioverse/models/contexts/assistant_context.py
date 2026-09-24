from .base_context	import BaseContext
from ...enums		import Roles

from typing	import Literal


class AssistantContext(BaseContext):
	role: Literal[Roles.ASSISTANT] = Roles.ASSISTANT