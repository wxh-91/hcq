from .errors		import ResponseCodeError
from .models		import (
	Response,
	Request,
	BaseContext,
	StreamChunk
)

import logging
import aiohttp
import json

from typing import List, Optional, AsyncIterator

logger = logging.getLogger(__name__)


class OpenAIClient:
	
	def __init__(
		self,
		session		: aiohttp.ClientSession,
		api_url		: Optional[str] = None,
		model_name	: Optional[str] = None
	):
		
		self.api_url 	= api_url
		self.model_name	= model_name
		self.session	= session
	
	
	def _ensure_ready(self) -> None:
		
		if self.model_name is None:
			raise RuntimeError("OpenAIClient 实例必须含有 model_name")
		if self.api_url is None:
			raise RuntimeError("OpenAIClient 实例必须含有 api_url")
	
	def _build_request(
		self,
		context_list	: List[BaseContext],
		assistant_key	: str, *,
		stream			: bool = False
	) -> Request:
		
		request = (
			Request(url=self.api_url)
			.set_header("Authorization", assistant_key)
			.set_header("Content-Type", "application/json")
			.set_body("model", self.model_name)
			.set_body("messages", context_list)
		)
		
		if stream:
			request.set_body("stream", True)
		
		return request
	
	
	async def _iter_sse_chunks(self, response: aiohttp.ClientResponse) -> AsyncIterator[StreamChunk]:
		
		buffer = ""
		
		async for chunk_bytes in response.content.iter_chunked(1024):
			
			buffer += chunk_bytes.decode("utf-8", errors="replace")
			
			while "\n" in buffer:
				
				line, buffer	= buffer.split("\n", 1)
				line			= line.strip()
				
				if not line or not line.startswith("data: "):
					continue
				
				data_str = line[6:]
				
				if data_str == "[DONE]":
					logger.info("流式请求完成 [DONE]")
					return
				
				try:
					yield StreamChunk.model_validate_json(data_str)
				
				except Exception as e:
					logger.warning(f"解析流式数据块失败: {e}, 原始数据: {data_str[:200]}")
					continue
	
	
	async def call(
		self, *,
		context_list	: Optional[List[BaseContext]]	= None,
		assistant_key	: Optional[str]					= None,
		request			: Optional[Request]				= None
	) -> Response:
		
		if request is None:
			
			self._ensure_ready()
			if context_list is None:
				raise RuntimeError("函数必须传入 context_list")
			if assistant_key is None:
				raise RuntimeError("函数必须传入 assistant_key")
			
			request = self._build_request(context_list, assistant_key)
		
		logger.info("参数初始化完成 开始请求AI")
		
		async with self.session.post(
			url		= request.url,
			headers	= request.headers,
			params	= request.params,
			json	= request.body,
			timeout = request.timeout
		) as response:
			response_code = response.status
			response_json = await response.json()
		
		logger.info(f"请求完成: {response_code}")
		
		if response_code != 200:
			raise ResponseCodeError(code=response_code, response=response_json)
		
		return Response.model_validate(response_json)
	
	
	async def call_stream(
		self, *,
		context_list	: Optional[List[BaseContext]]	= None,
		assistant_key	: Optional[str]					= None,
		request			: Optional[Request]				= None
	) -> AsyncIterator[StreamChunk]:
		
		if request is None:
			
			self._ensure_ready()
			
			if context_list is None:
				raise RuntimeError("函数必须传入 context_list")
			if assistant_key is None:
				raise RuntimeError("函数必须传入 assistant_key")
			
			request = self._build_request(context_list, assistant_key, stream=True)
		
		else:
			request.set_body("stream", True)
		
		logger.info("流式参数初始化完成 开始请求AI")
		
		async with self.session.post(
			url		= request.url,
			headers	= request.headers,
			params	= request.params,
			json	= request.body,
			timeout = request.timeout
		) as response:
			
			if response.status != 200:
				response_text = await response.text()
				raise ResponseCodeError(code=response.status, response=response_text)
			
			try:
				async for chunk in self._iter_sse_chunks(response):
					yield chunk
			except GeneratorExit:
				logger.info("流式请求被中断 (stop)")
			else:
				logger.info("流式请求完成 (连接关闭)")