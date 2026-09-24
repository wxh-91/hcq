from __future__ import annotations
from asyncio	import Event as AsyncioEvent
from typing		import Set, Any


class ValueNotifier:
	
	@property
	def _value_waiters(self) -> Set[AsyncioEvent]:
		
		if not hasattr(self, "__value_waiters"):
			setattr(self, "__value_waiters", set())
		
		return getattr(self, "__value_waiters")
	
	def change(self, name: str, value: Any) -> None:
		
		setattr(self, name, value)
		
		for waiter in self._value_waiters:
			waiter.set()
		
		self._value_waiters.clear()
	
	async def wait_change(self) -> True:
		
		event = AsyncioEvent()
		self._value_waiters.add(event)
		
		return await event.wait()