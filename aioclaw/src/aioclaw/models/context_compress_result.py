# 类型两件套
from pydantic	import BaseModel, ConfigDict, Field

from typing		import Any, List, Optional


class ContextCompressResult(BaseModel):

	model_config = ConfigDict(arbitrary_types_allowed=True)

	# 保留旧字段兼容性；是否达到阈值由 Gateway 判断。
	is_out			: bool	= False
	is_compressed	: bool	= False
	contexts		: Optional[List[Any]] = Field(default=None, exclude=True)
