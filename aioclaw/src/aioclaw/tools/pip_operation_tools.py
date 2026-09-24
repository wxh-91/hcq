from __future__ import annotations

from ..protocols	import ToolsManagerProtocol
from ..utils		import build_tool_schema, kill_async_proc
from .base_tool		import BaseTool

import asyncio
import sys
import shutil


# 安装 pip 包
PipInstallSchema = build_tool_schema(
	tool_name			= "pip_install",
	tool_description	= "使用当前Python解释器安装pip包，确保包安装到正确的环境中而非系统Python",
	arguments			= {
		"package"	: ("string", "要安装的包名，支持pip语法如 'requests' 或 'requests==2.28.0'"),
		"timeout"	: ("integer", "安装超时 单位为秒", 60),
	}
)

# 卸载 pip 包
PipUninstallSchema = build_tool_schema(
	tool_name			= "pip_uninstall",
	tool_description	= "使用当前Python解释器卸载pip包",
	arguments			= {
		"package"	: ("string", "要卸载的包名"),
		"timeout"	: ("integer", "卸载超时 单位为秒", 30),
	}
)

# 列出已安装的 pip 包
PipListSchema = build_tool_schema(
	tool_name			= "pip_list",
	tool_description	= "列出当前Python环境中已安装的pip包",
	arguments			= {
		"timeout"	: ("integer", "查询超时 单位为秒", 15),
	}
)

# 查看 pip 包信息
PipShowSchema = build_tool_schema(
	tool_name			= "pip_show",
	tool_description	= "查看指定pip包的详细信息（版本、依赖、位置等）",
	arguments			= {
		"package"	: ("string", "要查询的包名"),
		"timeout"	: ("integer", "查询超时 单位为秒", 15),
	}
)


# Pip 操作类
class PipOperationTools(BaseTool):

	def __init__(self, *args, **kwargs):
		self._pip = f"uv pip" if shutil.which("uv") else f"{sys.executable} -m pip"

	def register(self, tools_manager: ToolsManagerProtocol):

		super().register(tools_manager)

		tools_manager.register(self.pip_install, PipInstallSchema)
		tools_manager.register(self.pip_uninstall, PipUninstallSchema)
		tools_manager.register(self.pip_list, PipListSchema)
		tools_manager.register(self.pip_show, PipShowSchema)

	async def pip_install(self, package: str, timeout: int = 60) -> str:

		proc = await asyncio.create_subprocess_shell(
			cmd		= f"{self._pip} install {package}",
			stdout	= asyncio.subprocess.PIPE,
			stderr	= asyncio.subprocess.STDOUT
		)

		try:

			output, _	= await asyncio.wait_for(proc.communicate(), timeout=timeout)
			output		= output.decode()

		except asyncio.TimeoutError:
			output = "安装超时"

		finally:
			if proc.returncode is None:
				await kill_async_proc(proc)

		return output

	async def pip_uninstall(self, package: str, timeout: int = 30) -> str:

		proc = await asyncio.create_subprocess_shell(
			cmd		= f"{self._pip} uninstall {package} -y",
			stdout	= asyncio.subprocess.PIPE,
			stderr	= asyncio.subprocess.STDOUT
		)

		try:

			output, _	= await asyncio.wait_for(proc.communicate(), timeout=timeout)
			output		= output.decode()

		except asyncio.TimeoutError:
			output = "卸载超时"

		finally:
			if proc.returncode is None:
				await kill_async_proc(proc)

		return output

	async def pip_list(self, timeout: int = 15) -> str:

		proc = await asyncio.create_subprocess_shell(
			cmd		= f"{self._pip} list",
			stdout	= asyncio.subprocess.PIPE,
			stderr	= asyncio.subprocess.STDOUT
		)

		try:

			output, _	= await asyncio.wait_for(proc.communicate(), timeout=timeout)
			output		= output.decode()

		except asyncio.TimeoutError:
			output = "查询超时"

		finally:
			if proc.returncode is None:
				await kill_async_proc(proc)

		return output

	async def pip_show(self, package: str, timeout: int = 15) -> str:

		proc = await asyncio.create_subprocess_shell(
			cmd		= f"{self._pip} show {package}",
			stdout	= asyncio.subprocess.PIPE,
			stderr	= asyncio.subprocess.STDOUT
		)

		try:

			output, _	= await asyncio.wait_for(proc.communicate(), timeout=timeout)
			output		= output.decode()

		except asyncio.TimeoutError:
			output = "查询超时"

		finally:
			if proc.returncode is None:
				await kill_async_proc(proc)

		return output