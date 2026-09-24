from __future__ import annotations

from ..protocols	import ToolsManagerProtocol
from ..utils		import build_tool_schema
from .base_tool		import BaseTool

from typing			import Optional, Dict, Any
from fake_useragent	import UserAgent

import httpx
import trafilatura


# 访问 URL 静态内容
FetchUrlSchema = build_tool_schema(
	tool_name			= "fetch_url",
	tool_description	= "访问url的静态内容",
	arguments			= {
		"url"					: ("string", "目标url"),
		"method"				: ("string", "使用的请求方式 包括但不限于get和post"),
		"content"				: ("string", "请求体的内容 默认空", ""),
		"headers"				: ("object", "请求头 传入则与系统默认的并合", {}),
		"encoding"				: ("string", "编码方式", "utf-8"),
		"max_response_length"	: ("integer", "最大返回字符数", 800),
		"timeout"				: ("integer", "访问超时时间", 10)
	}
)


class NetworkOperationTools(BaseTool):
			
	def register(self, tools_manager: ToolsManagerProtocol):
		
		super().register(tools_manager)
		
		tools_manager.register(self.fetch_url, FetchUrlSchema)
	
	# 辅助方法：解析 HTML
	def _html2markdown(self, html: str, **kwargs) -> Any:
		
		response_markdown	= trafilatura.extract(
			filecontent		= html,
			output_format	= "markdown",
			include_tables	= True, # 保留表格
			deduplicate		= True, # 去除重复文本
			no_fallback		= False,# 启用备用提取策略
			**kwargs
		)
		
		return response_markdown if response_markdown else "Empty Response"
	
	async def fetch_url(
		self,
		url		: str,
		method	: str,
		content	: str							= "",
		headers	: Union[Dict[str, Any], None]	= None,
		encoding: str							= "utf-8",
		max_response_length: int				= 800,
		timeout	: int							= 10
	) -> str:
		
		# 请求头处理
		headers = {
			"User-Agent": UserAgent().random,
			**(headers if headers else {})
		}
		
		# 校验请求参数
		if content: 
			content = content.encode(encoding)
		
		async with httpx.AsyncClient(
			timeout			= timeout,
			follow_redirects= True
		) as client:
			
			response = await client.request(
				method	= method,
				url		= url,
				headers	= headers,
				content	= content
			)
		
		response_code	= response.status_code
		response_html	= response.content.decode(encoding)
		content_type	= response.headers.get("Content-Type", "")
		
		if "application/json" in content_type:
			result = response_html
		elif "text/html" in content_type:
			result = self._html2markdown(response_html)
		else:
			result = response_html
		
		if len(result) > max_response_length:
			result = f"{result[:max_response_length]}..."
		
		return f"{response_code}: {result}"