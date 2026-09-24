from __future__ import annotations
from enum	import Enum


class FileTypes(str, Enum):
	
	FILE		: str = "file"
	DIRECTORY	: str = "directory"
	ENV			: str = "env"