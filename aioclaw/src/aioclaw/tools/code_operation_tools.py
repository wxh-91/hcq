from __future__ import annotations

from ..protocols	import ToolsManagerProtocol
from ..utils		import build_tool_schema, kill_async_proc
from .base_tool		import BaseTool

import asyncio
import traceback
import tempfile
import sys


TEMP_DICTIONARY = tempfile.gettempdir()


# 执行 Python 代码
PythonRunnerSchema = build_tool_schema(
	tool_name			= "python_runner",
	tool_description	= "运行python代码",
	arguments			= {
		"code"			: ("string", "需要运行的代码"),
		"timeout"		: ("integer", "运行超时 单位为秒", 10),
		"work_directory": (["string", "null"], "工作目录 默认为临时文件目录", None),
		"output_limit"	: ("integer", "输出长度限制 超过则后续部分使用'...'截断", 800)
	}
)

# 执行 Shell 指令
BashRunnerSchema = build_tool_schema(
	tool_name			= "bash_runner",
	tool_description	= "运行bash指令，支持管道",
	arguments			= {
		"command"		: ("string", "需要运行的指令"),
		"timeout"		: ("integer", "运行超时 单位为秒", 10),
		"work_directory": (["string", "null"], "工作目录 默认为临时文件目录", None),
		"output_limit"	: ("integer", "输出长度限制 超过则后续部分使用'...'截断", 800)
	}
)


class CodeOperationTools(BaseTool):
	
	def register(self, tools_manager: ToolsManagerProtocol):
		
		super().register(tools_manager)
		
		tools_manager.register(self.python_runner, PythonRunnerSchema)
		tools_manager.register(self.bash_runner, BashRunnerSchema)
		
	async def python_runner(
		self,
		code			: str,
		work_directory	: Optional[str]	= None,
		timeout			: int			= 10,
		output_limit	: int			= 800
	) -> str:
	
		"""执行 Python 代码。"""
		
		proc = await asyncio.create_subprocess_exec(
			sys.executable, "-c", code,
			cwd		= work_directory or TEMP_DICTIONARY,
			stdout	= asyncio.subprocess.PIPE,
			stderr	= asyncio.subprocess.STDOUT
		)
		
		try:
			
			output, _	= await asyncio.wait_for(proc.communicate(), timeout=timeout)
			output		= output.decode()
		
		except asyncio.TimeoutError:
			output = "代码执行超时"
		
		finally:
			if proc.returncode is None:
				await kill_async_proc(proc)
		
		if len(output) > output_limit:
			output = f"{output[:output_limit]}..."
				
		return output
	
	async def bash_runner(
		self,
		command			: str,
		timeout			: int			= 10,
		work_directory	: Optional[str]	= None,
		output_limit	: int			= 800
	) -> str:
		
		"""执行终端指令。"""
		
		proc = await asyncio.create_subprocess_shell(
			cmd		= command,
			cwd		= work_directory or TEMP_DICTIONARY,
			stdout	= asyncio.subprocess.PIPE,
			stderr	= asyncio.subprocess.STDOUT
		)
		
		try:
			
			output, _	= await asyncio.wait_for(proc.communicate(), timeout=timeout)
			output		= output.decode()
		
		except asyncio.TimeoutError:
			output = "代码执行超时"
		
		finally:
			if proc.returncode is None:
				await kill_async_proc(proc)
		
		if len(output) > output_limit:
			output = f"{output[:output_limit]}..."
				
		return output