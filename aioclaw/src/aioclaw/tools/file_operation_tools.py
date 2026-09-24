from __future__ import annotations
import aiofiles
import asyncstdlib
import orjson

from ..protocols	import ToolsManagerProtocol
from ..utils		import build_tool_schema
from .base_tool		import BaseTool

from typing		import Optional
import os
import shutil


# 写文件
WriteFileSchema = build_tool_schema(
	tool_name			= "write_file",
	tool_description	= "将内容写入到文件",
	arguments			= {
		"file_path"	: ("string", "文件路径"),
		"content"	: ("string", "具体写入内容"),
		"encoding"	: ("string", "编码方式", "utf-8"),
		"mode"		: ("string", "写入模式 w代表覆盖 a代表追加", "w")
	}
)

# 复制文件
CopyFileSchema = build_tool_schema(
	tool_name			= "copy_full_file",
	tool_description	= "复制完整文件",
	arguments			= {
		"file_path": ("string", "待复制文件的路径"),
		"target_path": ("string", "目标文件或路径")
	}
)

# 删除文件
DeleteFileSchema = build_tool_schema(
	tool_name			= "delete_file",
	tool_description	= "删除一个文件",
	arguments			= {
		"file_path": ("string", "目标文件路径")
	}
)

# 搜索目录内容
ScanDirectorySchema = build_tool_schema(
	tool_name			= "scan_directory",
	tool_description	= "获取单个文件夹里面的文件/文件夹",
	arguments			= {
		"directory_path": ("string", "目标文件夹路径")
	}
)

# 在文件里面找东西
FindInFileSchema = build_tool_schema(
	tool_name			= "find_in_file",
	tool_description	= "在文件中查找带有关键词的行",
	arguments			= {
		"file_path"	: ("string", "文件路径"),
		"keywords"	: ("string", "关键词组 使用空格分割"),
		"encoding"	: ("string", "编码方式", "utf-8")
	}
)

# 创建目录
CreateDirectorySchema = build_tool_schema(
	tool_name			= "create_directory",
	tool_description	= "创建一个目录 如果父目录不存在也会一并创建",
	arguments			= {
		"directory_path": ("string", "要创建的目录路径 支持多级目录 使用/分割 如: a/b/c")
	}
)

# 读取一个文件
ReadFileSchema = build_tool_schema(
	tool_name			= "read_file",
	tool_description	= "读取一个文件",
	arguments			= {
		"file_path"		: ("string", "目标文件路径"),
		"encoding"		: ("string", "编码方式", "utf-8"),
		"offset_line"	: ("integer", "起始行号 1=第一行", 1),
		"limit_line"	: (["integer", "null"], "读取行数 默认不限制", None),
		"output_limit"	: ("integer", "输出长度限制 超过则后续部分使用'...'截断", 800)
	}
)


# 文件操作类
class FileOperationTools(BaseTool):

	def register(self, tools_manager: ToolsManagerProtocol):

		super().register(tools_manager)

		tools_manager.register(self.write_file, WriteFileSchema)
		tools_manager.register(self.copy_full_file, CopyFileSchema)
		tools_manager.register(self.delete_file, DeleteFileSchema)
		tools_manager.register(self.scan_directory, ScanDirectorySchema)
		tools_manager.register(self.find_in_file, FindInFileSchema)
		tools_manager.register(self.create_directory, CreateDirectorySchema)
		tools_manager.register(self.read_file, ReadFileSchema)

	async def read_file(
		self,
		file_path	: str,
		encoding	: str			= "utf-8",
		offset_line	: int			= 1,
		limit_line	: Optional[int]	= None,
		output_limit: int			= 800
	) -> str:

		if not os.path.isfile(file_path):
			return f"{file_path} 不存在"

		async with aiofiles.open(file_path, encoding=encoding) as file:

			lines = []
			async for index, line in asyncstdlib.enumerate(file, start=1):

				if index < offset_line:
					continue

				lines.append(line)

				if limit_line is not None and len(lines) >= limit_line:
					break

		content = "".join(lines)

		if len(content) > output_limit:
			content = f"{content[:output_limit]}..."

		return content

	async def write_file(self, file_path: str, content: str, mode: str = "w", **kwargs) -> str:

		"""写入文件"""

		# 限制模式
		if mode not in ("w", "a"):
			return f"不支持 {mode} 模式"

		# 默认用w是因为content只能为字符串 而字符串不能写入二进制 支持wb也没用
		async with aiofiles.open(file_path, mode, **kwargs) as file:
			write_length = await file.write(content)

		return (
			f"{file_path} 写入完成 "
			f"输入字符数: {len(content)} "
			f"写入字符数: {write_length}"
		)

	def copy_full_file(self, file_path: str, target_path: str) -> str:

		"""复制一整个文件"""

		if not os.path.exists(file_path):
			return f"{file_path} 不存在"

		shutil.copy(file_path, target_path)

		return (
			f"复制完成 ({file_path} -> {target_path}) "
			f"{file_path} 大小: {os.path.getsize(file_path)} bytes "
			f"{target_path} 大小: {os.path.getsize(target_path)} bytes"
		)

	def delete_file(self, file_path: str) -> str:

		"""删除文件"""

		if not os.path.exists(file_path):
			return f"{file_path} 不存在"

		os.remove(file_path)

		return f"{file_path} 删除成功"

	def scan_directory(self, directory_path: str, link_char: str = ",") -> str:

		if not os.path.isdir(directory_path):
			return f"{directory_path} 不存在"

		files = os.listdir(directory_path)

		return link_char.join(files) if files else f"该文件夹没有任何文件"

	def create_directory(self, directory_path: str) -> str:

		directory_path = directory_path.replace("/", os.sep)

		if os.path.isdir(directory_path):
			return f"{directory_path} 目录已存在"

		os.makedirs(directory_path, exist_ok=True)

		return f"{directory_path} 目录创建完成"

	async def find_in_file(self, file_path: str, keywords: str, encoding: str = "utf-8") -> str:
		
		"""在文件中查找内容"""

		if not os.path.isfile(file_path):
			return f"{file_path} 不存在"

		if not (keywords := keywords.strip()):
			return f"至少需要一个关键词"

		async with aiofiles.open(file_path, "r", encoding=encoding) as file:

			found_lines = {
				f"Line {index}": line
				async for index, line in asyncstdlib.enumerate(file, start=1)
				if any(keyword in line for keyword in keywords.split(" "))
			}

		return orjson.dumps(found_lines).decode()