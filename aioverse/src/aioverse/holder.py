# 这俩函数就是啥也不干的
async def async_null(*args, **kwargs): pass
def sync_null(*args, **kwargs): pass


class NullObject:
	
	"""
	没人感觉这个类很涩吗
	无论你对她NullObject()()
	还是NullObject().log()
	还是await NullObject()
	她对你都是一如既往地爱你
	她也不说话
	就让你一直调她
	"""
	
	def __await__(self):
		async def _null():
			return self
		return _null().__await__()
	
	def __call__(self, *args, **kwargs):
		return self
	
	def __getattr__(self, name: str):
		return self


null_object = NullObject()