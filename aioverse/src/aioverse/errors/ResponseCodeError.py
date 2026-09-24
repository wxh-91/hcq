from typing import Any

class ResponseCodeError(Exception):
	
	"""
	返回码错误
	"""
	
	def __init__(
		self,
		code     : int,
		response : Any = None
	):
		
		self.code     = code
		self.response = response
		super().__init__(f"错误的返回码: {code}")
	
	def __str__(self) -> str: return f"{self.code}: {self.response}"