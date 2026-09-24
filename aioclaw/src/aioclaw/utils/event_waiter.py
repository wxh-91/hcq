from __future__ import annotations
import asyncio

from typing import Awaitable, Any, Union


# 异步事件等待
async def async_event_waiter(
	self,
	coro	: Awaitable,
	timeout	: int = 30
) -> Union[Any, None]: # 要么返回任务返回 要么返回None
	
	try:
	
		result = await asyncio.wait_for(
			coro,
			timeout	= timeout
		)
		
		return result
	
	# 仅捕获 asyncio.TimeoutError 别的直接抛出
	except asyncio.TimeoutError: return None
	
	return None