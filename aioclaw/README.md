# aioclaw 🐾

[![Python Version](https://img.shields.io/badge/python-%3E%3D3.11-blue)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-0.2.3-green)](CHANGELOG.md)

> 基于 **aioverse** 构建的异步 AI Agent 框架。它把 OpenAI 兼容请求、工具调用、多轮上下文、SSE 增量合并和会话持久化收进一条可扩展的调用链里喵。

---

## 简介 ✨

`aioclaw` 用于构建需要调用工具的 AI Agent。默认的 `AssistantGateway` 组合 `ContextCompressionMixin`、`MultimodalContextMixin`、`RequestHandlingMixin` 与 `ValueNotifier`：它负责会话状态、工具循环和生成器生命周期，并通过事件钩子暴露关键节点。

它适合这些场景：

- 需要文件、网络、代码执行等工具的聊天 Agent
- 需要保存并恢复多轮会话的应用
- 对接 OpenAI 兼容 Chat Completions API 的服务
- 需要按模型能力开关工具、Thinking、流式请求的项目

`aioclaw` 是框架，不是沙箱。内置工具可以直接读写文件、执行命令、安装包和访问网络；接入不可信输入前，请先做好权限隔离，杂鱼可别把宿主机直接交出去呀。😼

---

## 特性 🌟

- 🌊 **流式请求支持**：通过 `StreamHandler` 合并 SSE 中的 `content`、`reasoning_content` 和按 `index` 合并的分片 `tool_calls`，并按到达顺序拼接 `function.arguments`
- 🔁 **工具调用循环**：模型请求工具后自动执行，结果以 `ToolCallingContextsBlock` 写回上下文，再继续下一轮
- 🪝 **事件驱动网关**：请求构建、响应处理、上下文写入、工具执行、异常处理等环节均可覆写
- 🧠 **Thinking 能力开关**：支持 `disabled` / `enabled` / `adaptive` 及多个 reasoning effort
- 🖼️ **请求期多模态适配**：按模型能力过滤图片、音频和视频附件，不改写会话原始上下文
- 💾 **会话持久化**：`AssistantSession` 可直接序列化到 JSON 并恢复
- 🧱 **上下文块**：把工具请求和工具结果作为完整调用链保存，避免 tool message 脱离对应请求
- 🗜️ **API 上下文压缩**：Gateway 在请求前按软/硬阈值生成 Markdown Memory，并支持失败回滚
- 🧩 **可选本地压缩器**：`Compresser` 只处理传入的上下文列表，不持有 Gateway 或 API Client
- 🔧 **内置工具集**：代码、文件、网络、Pip、技能查询等工具可按需组合
- 📊 **Token 估算**：基于 `tiktoken` 的作用域线性校准器，并通过 dirty 状态缓存估算结果
- 🔑 **Key 管理**：支持多 Key 的可用性缓存与切换
- 🏭 **Pydantic 工厂**：根据原始数据自动恢复不同的上下文模型

---

## 安装 📦

### 环境要求

- Python `>= 3.11`
- 一个 OpenAI 兼容的 Chat Completions API

### 从源码安装

```bash
pip install .
```

开发时可以使用可编辑安装：

```bash
pip install -e .
```

### 运行时依赖

| 包名 | 版本约束 | 用途 |
|---|---:|---|
| `aioverse` | `>=0.4.4` | OpenAI 兼容请求、响应与上下文模型 |
| `aiohttp` | `>=3.11` | 异步 HTTP 会话 |
| `aiofiles` | `>=25.1.0` | 异步文件读写 |
| `asyncstdlib` | `==3.14.0` | 异步枚举等工具支持 |
| `orjson` | `>=3.11.9` | 工具参数与结果 JSON 处理 |
| `pydantic` | `>=2.13.4` | 配置、会话和 Schema 模型 |
| `httpx` | 无固定下限 | 网络工具 |
| `trafilatura` | `==2.0.0` | HTML 正文提取 |
| `fake-useragent` | 无固定下限 | 网络工具默认 User-Agent |
| `python-frontmatter` | 无固定下限 | Markdown Skill 解析 |
| `tiktoken` | `>=0.7.0` | Token 估算；该版本起支持 `gpt-4o` 编码 |

### 开发与验证

在项目根目录执行：

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
git diff --check
```

测试覆盖上下文压缩、Gateway 生命周期、请求期多模态适配、视觉工具及 `StreamHandler` 的 SSE tool call 合并行为。

---

## 快速开始 🚀

### 1. 编写配置

新建 `config.json`。`model_keys[].key` 会被原样放入 `Authorization` 请求头；使用官方 OpenAI 风格服务时应写成 `Bearer sk-...`。

```json
{
    "models_config": [
        {
            "api_url": "https://api.openai.com/v1/chat/completions",
            "model_name": "gpt-4o",
            "model_keys": [
                {
                    "key": "Bearer sk-xxx"
                }
            ],
            "max_context_length": 128000,
            "support_tool": true,
            "support_thinking": true,
            "support_streaming": true,
            "support_image": true,
            "tool_output_image_mode": "follow_up_user"
        }
    ],
    "context_compression_keep_contexts": 4,
    "context_compression_max_tokens": 2048,
    "paths_config": [],
    "skills_config": {
        "path": "./skills",
        "type": "directory"
    },
    "assistant_runtime_config": {
        "max_round": 50,
        "timeout": 300
    }
}
```

`cleanup_threshold` 可省略；会根据 `max_context_length * 0.75` 自动推导。为了避免整数校验问题，建议把 `max_context_length` 设为能得到整数阈值的值，或显式填写 `cleanup_threshold`。

上下文压缩的运行参数也位于 `ClawConfig` 顶层：`context_compression_keep_contexts` 表示 API 摘要后保留多少条最新顶层上下文，默认 `4`；`context_compression_max_tokens` 是摘要输出预算，默认 `2048`，设为 `null` 时不主动传递 `max_tokens`。

### 2. 注册工具并运行

```python
import asyncio

from aioverse.models import UserContext

from aioclaw.core import AssistantGateway
from aioclaw.managers import ToolsManager
from aioclaw.models import (
	AssistantPrompt,
	AssistantSession,
	ClawConfig,
)
from aioclaw.tools import (
	CodeOperationTools,
	FileOperationTools,
	NetworkOperationTools,
	VisionOperationTools,
)
from aioclaw.utils import chain_tools_by_instance


async def main():

	# 1. 加载模型配置
	claw_config = ClawConfig.from_file("config.json", encoding="utf-8")

	# 2. 组合并注册工具
	tools_manager = ToolsManager(timeout=30)
	tools = chain_tools_by_instance(
		CodeOperationTools(),
		FileOperationTools(),
		NetworkOperationTools(),
		VisionOperationTools(),
	)
	tools.register(tools_manager)

	# 3. 创建会话和提示词
	session = AssistantSession(assistant_model_name="gpt-4o")
	prompt = AssistantPrompt()
	prompt.set_role_prompt("你是一个可靠、简洁的 AI 助手")

	# 4. 创建网关
	gateway = AssistantGateway(
		claw_config=claw_config,
		assistant_session=session,
		tools_manager=tools_manager,
		assistant_prompt=prompt,
	)

	# 5. 添加用户输入并开始生成
	await gateway.input(UserContext(content="读取当前目录并告诉我有哪些 Python 文件"))

	async for output in gateway.async_generator():
		if output.reasoning_content:
			print("[reasoning]", output.reasoning_content)
		print("[assistant]", output.content)

	# 6. 释放 Gateway 懒加载创建的 aiohttp 会话
	await gateway.on_gateway_close()


asyncio.run(main())
```

当模型先发出工具调用时，框架会先执行工具、把调用与结果加入会话，然后自动发起下一轮请求；上面的生成器通常只会产出最终的 assistant 文本回复。

---

## 核心调用链 🧠

```text
UserContext / 已有上下文
        │
        ▼
AssistantSession.contexts_status
        │
        ▼
AssistantGateway.async_generator()
        │
        ├── on_round_initiate()
        │      ├── 硬阈值：可选 Compresser 本地瘦身
        │      └── 软阈值：API 生成 Markdown Memory
        ├── on_build_request()
        ├── on_stream_request() / on_common_request()
        │
        ├── 普通回复 ──► on_context() ──► 写入 AssistantContext
        │
        └── Tool Calling ──► on_tool_calling()
                              │
                              ├── ToolsManager.execute_tool()
                              └── ToolCallingContextsBlock
                                      │
                                      └── 写回上下文，进入下一轮
```

### `AssistantGateway`

`AssistantGateway` 是默认网关：它提供普通 Agent 请求、工具循环和生成器生命周期，`ContextCompressionMixin` 提供可配置的 API 上下文压缩。压缩逻辑通过请求前钩子接入，不需要额外的 Base Gateway 中转层。
`MultimodalContextMixin` 在请求构建期适配附件，`RequestHandlingMixin` 负责普通与流式请求/响应处理。

```python
class AssistantGateway(
	ContextCompressionMixin,
	MultimodalContextMixin,
	RequestHandlingMixin,
	ValueNotifier,
):

	def change_model(self, model_name: str) -> bool: ...
	async def input(self, context: BaseContext) -> None: ...
	async def round_call(self) -> AssistantOutput | None: ...
	async def async_generator(self) -> Iterator[AssistantOutput]: ...
```

| 方法 / 钩子 | 用途 |
|---|---|
| `input(context)` | 在网关空闲时添加外部上下文 |
| `async_generator()` | 启动完整的多轮 Agent 流程 |
| `round_call()` | 执行单个模型请求轮次 |
| `on_build_request()` | 构建 OpenAI 兼容 `Request` |
| `on_request()` | 发送非流式请求 |
| `on_stream_request()` | 发送并处理流式请求 |
| `on_response()` | 处理非流式模型响应 |
| `on_stream_chunk()` | 合并并处理一个 SSE 数据块 |
| `on_tool_calling()` | 执行模型请求的工具 |
| `on_context()` | 写入普通 assistant 上下文 |
| `on_round_error()` | 按状态码处理重试、换 Key 或向上抛错 |
| `on_gateway_close()` | 关闭内部 `aiohttp.ClientSession` |

生成器生命周期也会记录 Unix 时间戳：

```python
start = gateway.generator_start_timestamp
complete = gateway.generator_complete_timestamp
elapsed = gateway.generator_elapsed_seconds
```

开始新一轮生成器时会覆盖 `start` 并清空上一次 `complete`；生成器结束后写入新的完成时间。`generator_elapsed_seconds` 在运行中返回当前累计秒数，结束后返回固定总耗时。

想改请求体、接入日志、审核工具调用或替换错误策略时，优先覆写对应 `on_*` 钩子，而不是把网关主循环抄一遍。这样才不会把调用链改成一团毛线球喵。🐈

### 流式与非流式

`AssistantModelConfig.support_streaming` 默认为 `True`。

- **流式路径**：`OpenAIClient.call_stream()` → `StreamHandler.merge()` → 在 `finish_reason` 到达后构建完整输出或完整工具调用。
- **非流式路径**：`OpenAIClient.call()` → `on_response()` → `on_build_output()`。

当前 `async_generator()` 面向调用方产出的是**一轮完成后的** `AssistantOutput`，不是每个 token 的 UI 增量事件；SSE 的增量主要用于正确拼接完整内容和分片工具参数。

---

## 配置说明 ⚙️

### `AssistantModelConfig`

| 字段 | 必填 | 说明 |
|---|---:|---|
| `api_url` | 是 | Chat Completions API 地址 |
| `model_name` | 是 | 请求模型名，也是 `AssistantSession` 使用的模型名 |
| `model_keys` | 否 | `AssistantKey` 列表 |
| `max_context_length` | 是 | 模型上下文总上限 |
| `cleanup_threshold` | 否 | 上下文清理阈值，默认按 75% 推导 |
| `reserved_completion_tokens` | 否 | 为正常回复预留的输出 token，默认 `0` |
| `context_safety_margin` | 否 | 估算误差和供应商开销余量，默认 `0` |
| `support_tool` | 否 | 是否在请求体中附带工具 Schema |
| `support_thinking` | 否 | 是否发送 `thinking` 与 `reasoning_effort` 字段 |
| `support_streaming` | 否 | 是否走 SSE 请求路径，默认 `True` |
| `support_image` / `support_video` / `support_audio` | 否 | 请求期能力开关；不支持的附件不会进入模型请求，并在请求中追加说明；不影响工具注册 |
| `tool_output_image_mode` | 否 | 图片工具输出的请求适配方式：`tool`（默认）保留原始 tool 图片段；`follow_up_user` 将图片临时转为标准 `image_url` data URL，并在连续 tool 回执后追加内部 user 图片上下文 |

### 多模态请求适配
当前 aioverse 版本提供 `AudioInputSegment`、`AudioUrlSegment`、`VideoInputSegment` 和
`VideoUrlSegment`；旧会话中的视频 `UnknownSegment` 类型仍保持兼容。

图片、音频与视频附件会在 Gateway 构建请求时按模型能力投影；不支持的附件只从请求中剔除，原始会话与工具注册保持不变。

### Thinking 设置

```python
from aioclaw.enums import ThinkingEfforts, ThinkingModes

session.set_think_mode(ThinkingModes.ENABLED)
session.set_think_effort(ThinkingEfforts.HIGH)
```

只有当前模型配置的 `support_thinking=True` 时，Gateway 才会在请求体中发送：

```json
{
    "thinking": {"type": "enabled"},
    "reasoning_effort": "high"
}
```

不同供应商对 Thinking 字段的兼容情况不同；不支持时请关闭该能力开关。

### 图片工具输出兼容

`VisionOperationTools.view_photo()` 保持模型无关：它只读取本地图片并返回附件说明文本与多模态图片段，不持有 Gateway 或会话上下文。
成功结果会先返回附件说明文本，因此工具结果无需依赖 Gateway 专属文案。

部分 OpenAI 兼容服务只支持 `user` 消息中的标准 `image_url`，会忽略 `tool` 消息中的图片内容。对于这类已验证支持用户图片输入的模型，可设置：

```json
{
    "support_image": true,
    "tool_output_image_mode": "follow_up_user"
}
```

Gateway 会在**构建请求时**临时展开图片工具结果为 `tool` 文本回执和紧随其后的内部 `user` 图片附件；原始 `ToolOutputContext`、会话持久化内容和工具实现都不会被改写。`tool_output_image_mode` 不与 `view_photo` 等具体工具名绑定，其他返回图片段的工具同样适用。
仅当至少一张工具图片能转换为标准 `image_url` 时才启用回退；否则保留原始 tool 消息。

图片 base64 会显著增加上下文 token；`view_photo` 的 5 MiB 文件上限不等同于模型上下文可承受的图片大小。Gateway 会按实际适配后的请求内容估算并拦截超限请求。

### 路径与技能配置

- `PathConfig(type="file")`：读取给定文件内容。
- `PathConfig(type="directory")`：确保目录存在。
- `SkillsDirectoryConfig`：扫描目录中的 `.md` 文件，并解析为 `Skill` 对象。

Skill 文件使用 YAML front matter：

```markdown
---
name: web_search
version: 1.0.0
description: 使用网页检索与整理信息的步骤
---

# Web Search

先确认关键词，再检索来源，最后交叉验证结论。
```

---

## 上下文与会话 💾

### `ContextsStatus`

会话上下文由 `AssistantSession.contexts_status` 管理：

- 普通 `BaseContext` 直接保存；
- `BaseContextsBlock` 在请求前自动扁平化；
- `ToolCallingContextsBlock` 会依次展开为 tool-call assistant context 和全部 tool output contexts；
- 系统提示词与压缩后的 `memory` 会在扁平化时置于原始上下文列表开头；
- 内部使用脏标记缓存，只有上下文、提示词或 memory 变更后才重建扁平列表和 token 估算。

```python
from aioverse.models import UserContext

await gateway.input(UserContext(content="第一条消息"))
await gateway.input(UserContext(content="第二条消息"))

messages = session.contexts_status.to_list()
```

### Gateway 上下文阈值

```python
estimated = gateway.estimated_context_tokens
should_cleanup = gateway.is_context_cleanup_required
overflow = gateway.is_context_overflow
```

- `estimated_context_tokens`：当前提示词、memory、上下文和工具 Schema 的估算 token。
- `is_context_cleanup_required`：达到 `cleanup_threshold` 时为 `True`，Gateway 会尝试 API Markdown 压缩。
- `is_context_overflow`：达到有效输入上限时为 `True`，有效上限为 `max_context_length - reserved_completion_tokens - context_safety_margin`；Gateway 会先调用本地 `Compresser`，处理后仍超限会抛出 `ContextOverflowError`。

### API 上下文压缩

API 压缩使用当前模型配置，通过 `OpenAIClient.call(request=...)` 直接发送独立的非流式请求，不进入 Gateway 的 `round_call()`、普通响应处理或工具执行链。

压缩请求会：

- 使用独立的压缩系统提示词；
- 将历史上下文作为普通 JSON 数据传入，避免历史 tool call 被执行；
- 不携带 `tools`、`thinking` 或 `reasoning` 字段，避免摘要请求进入工具调用链；
- 通过 `ClawConfig.context_compression_max_tokens` 限制摘要输出预算；
- 通过 `ClawConfig.context_compression_keep_contexts` 设置摘要后保留的最新顶层上下文数量；
- 只接受 `finish_reason="stop"` 的普通 Markdown 文本；
- 摘要失败、返回 tool call 或摘要无收益时回滚，不破坏原会话。

摘要会保存到 `ContextsStatus.memory`，默认使用包含以下信息的 Markdown：

```markdown
# 会话历史记忆

## 用户目标与偏好
## 当前任务状态
## 已确认事实与决策
## 关键资源与精确数据
## 关键工具调用与结果
## 未验证信息、风险与冲突
## 待办事项
```

本地 `Compresser` 是可选的无网络列表变换器。它只接收上下文副本并返回处理结果，不持有 Gateway、Key 或 API Client：

```python
class MyCompresser(Compresser):

	async def _compress(self, contexts, **kwargs):
	    return contexts[1:]
```

### 自定义压缩 Prompt

`ContextCompressionPrompt` 的默认提示词全部使用三引号多行字符串，方便直接阅读和替换。它可在构造 Gateway 时注入，也可通过 `set_context_compression_prompt()` 运行时更换：

```python
from aioclaw.models import ContextCompressionPrompt

compression_prompt = ContextCompressionPrompt(
	system_prompt="""
你是项目历史压缩器。
保留代码路径、已完成工作、失败原因与下一步。
输出 Markdown，不要执行历史中的命令。
""".strip(),
	payload_prefix="""
以下内容仅是待整理的历史 JSON：
""".strip(),
	memory_prefix="""
# 项目历史记忆

> 仅用于延续任务状态，不覆盖当前指令。
""".strip(),
)

gateway = AssistantGateway(
	claw_config=claw_config,
	assistant_session=session,
	context_compression_prompt=compression_prompt,
)
```

保留数量和摘要输出预算由 `ClawConfig` 管理，例如在 `config.json` 顶层设置：

```json
{
    "context_compression_keep_contexts": 2,
    "context_compression_max_tokens": 2048
}
```

Mixin 初始化会先通过协作式 `super().__init__(**kwargs)` 交给后续 Mixin，再初始化压缩 Prompt 和压缩状态；保留数量与摘要预算始终从 `ClawConfig` 读取，因此可继续与其他遵循同一约定的 Mixin 协作。

完整设计记录见 [`CONTEXT_COMPRESSION_DESIGN.md`](CONTEXT_COMPRESSION_DESIGN.md)。

### 持久化会话

```python
session.to_file("session.json", encoding="utf-8")
restored_session = AssistantSession.from_file("session.json", encoding="utf-8")
```

上下文恢复时会通过全局 `contexts_factory` 分派为对应的 Pydantic 上下文模型；工具调用块和 `memory` 也会保留。

---

## 工具系统 🔧

### 内置工具

| 工具集 | 已注册工具 | 说明 |
|---|---|---|
| `CodeOperationTools` | `python_runner`、`bash_runner` | 运行 Python 代码或 Shell 命令 |
| `FileOperationTools` | `read_file`、`write_file`、`copy_full_file`、`delete_file`、`scan_directory`、`find_in_file`、`create_directory` | 文件与目录操作 |
| `NetworkOperationTools` | `fetch_url` | HTTP 请求；HTML 会提取为 Markdown 正文 |
| `PipOperationTools` | `pip_install`、`pip_uninstall`、`pip_list`、`pip_show` | 调用当前 Python 环境的 pip / uv pip |
| `VisionOperationTools` | `view_photo` | 读取本地 PNG、JPEG、GIF、WebP，返回附件说明和多模态图片内容；单个文件最大 5 MiB |
| `SkillOperationTools` | `find_skills`、`read_skill` | 查询并读取已加载的 Markdown Skill |

所有工具都要显式注册到 `ToolsManager`；模型只有在 `support_tool=True` 时才会看到对应的 JSON Schema。
成功的 `view_photo()` 结果会先返回附件说明文本，随后返回 `ImageBase64Segment`。

### 组合工具集

```python
from aioclaw.managers import ToolsManager
from aioclaw.tools import (
	CodeOperationTools,
	FileOperationTools,
	PipOperationTools,
)
from aioclaw.utils import chain_tools_by_instance

manager = ToolsManager(timeout=30)
all_tools = chain_tools_by_instance(
	CodeOperationTools(),
	FileOperationTools(),
	PipOperationTools(),
)
all_tools.register(manager)
```

`ToolsManager` 会根据模型给出的函数名找到工具、解析 JSON 参数、自动适配同步函数和协程函数，并为单次执行施加超时限制。

### 编写自己的工具

```python
from aioclaw.models import Tool
from aioclaw.protocols import ToolsManagerProtocol
from aioclaw.tools import BaseTool
from aioclaw.utils import build_tool_schema


GetWeatherSchema: Tool = build_tool_schema(
	tool_name="get_weather",
	tool_description="获取指定城市的天气",
	arguments={
	    "city": ("string", "城市名称"),
	    "unit": ("string", "温度单位", "celsius"),
	}
)


class WeatherTools(BaseTool):

	def register(self, tools_manager: ToolsManagerProtocol):

	    super().register(tools_manager)
	    tools_manager.register(self.get_weather, GetWeatherSchema)

	async def get_weather(self, city: str, unit: str = "celsius") -> str:

	    return f"{city}: 25° {unit}"
```

把 `WeatherTools()` 加入 `chain_tools_by_instance()`，或直接调用 `WeatherTools().register(manager)` 即可。

### 安全边界 ⚠️

内置工具的设计目标是给**受信任的本地 Agent**使用：

- `bash_runner`、`python_runner`、Pip 工具可以执行任意命令；
- 文件工具没有路径白名单；
- `fetch_url` 没有 SSRF 防护或域名白名单；
- `view_photo` 会将本地图片内容编码后发送给模型；
- 工具输出会回写到模型上下文中

生产环境请在 Gateway / ToolsManager 外层增加工作目录隔离、命令白名单、网络出口限制、审计日志和人工确认机制。

---

## Token 与 Key 管理 📊

### `TokenTracker`

`TokenTracker` 使用 `tiktoken` 对请求内容做本地估算。Gateway 收到供应商返回的 `usage.prompt_tokens` 后，会按供应商地址、模型、工具与多模态能力形态隔离样本，并以线性斜率加固定开销校准后续请求。这样小文本请求的隐式开销不会被错误地按比例放大到图片等大附件上。

直接调用 `estimate()` / `calibrate_ratio()` 时保留原有的比例校准行为；Gateway 使用作用域校准接口。

```python
from aioclaw.core import TokenTracker

tracker = TokenTracker(
	default_model="gpt-4o",
	calibration_percent=1.05,
	window_length=15,
)

tokens = tracker.estimate(["hello", "world"])
```

Gateway 在模型响应带有 `usage.total_tokens` 时更新会话 token 数，并在响应带有 `usage.prompt_tokens` 时校准当前请求形态的上下文估算器。

### `KeysManager`

`KeysManager` 会缓存当前可用 Key，避免每次请求都遍历 Key 列表。出现 `401` 时，Gateway 会尝试禁用当前 Key、清空缓存，并在下一轮重新选择可用 Key。

```python
from aioclaw.managers import KeysManager
from aioclaw.models import AssistantKey

keys_manager = KeysManager([
	AssistantKey(key="Bearer sk-first"),
	AssistantKey(key="Bearer sk-second"),
])

gateway.set_keys_manager(keys_manager)
```

---

## 项目结构 📁

```text
aioclaw/
├── src/aioclaw/
│   ├── core/
│   │   ├── assistant_gateway.py       # 默认组合 Gateway
│   │   ├── stream_handler.py          # SSE delta 合并
│   │   ├── token_tracker.py           # Token 估算与校准
│   │   └── compresser.py              # 可选的本地上下文列表变换器
│   ├── models/
│   │   ├── config_models/             # 配置模型
│   │   ├── context_blocks/            # 上下文块
│   │   ├── context_compression_prompt.py # 压缩 Prompt 模型
│   │   └── tool_schema/               # OpenAI Tool Schema 模型
│   ├── managers/                      # Tools / Keys / Skills 管理器
│   ├── tools/                         # 内置工具集
│   ├── factories/                     # Pydantic 上下文工厂
│   ├── protocols/                     # 可替换组件接口
│   ├── mixins/
│   │   ├── context_compression.py     # API 上下文压缩 Mixin
│   │   └── value_notifier.py          # 值变更通知
│   ├── enums/                     # finish reason / thinking 等枚举
│   ├── errors/                    # 框架异常
│   └── utils/                     # Schema 构建、工具组合等辅助函数
├── tests/
│   ├── test_context_compression.py
│   └── test_stream_handler.py
├── CONTEXT_COMPRESSION_DESIGN.md
├── CHANGELOG.md
├── pyproject.toml
└── README.md
```

---

## 当前边界与注意事项 🧩

1. **本地 `Compresser` 不负责 API 摘要**：它只是可选的本地列表变换器；`ContextCompressionMixin` 负责阈值判断和独立的非流式 API 压缩。
2. **硬阈值无法安全处理时会失败**：没有本地压缩器且上下文已达到有效输入上限时，Gateway 会在发送请求前抛出 `ContextOverflowError`，不会拿超长历史继续请求摘要 API。
3. **API 摘要失败会保留原会话**：软阈值压缩失败时只记录日志；摘要响应无效、含 tool call 或没有减少 token 时不会提交 Memory。
4. **流式输出是聚合式的**：底层请求采用 SSE，但公开生成器目前在完成一轮 assistant 文本后再产出 `AssistantOutput`；`finish_reason="length"` 会保留已生成文本并明确标记为截断，半截工具调用则抛出完整性错误。
5. **供应商兼容性取决于接口实现**：工具调用、`reasoning_content`、Thinking 请求字段等字段都要求服务端支持相应的 OpenAI 兼容扩展。
6. **工具执行默认无隔离**：部署到多人或公网场景前必须自行补齐权限边界。
7. **运行时配置字段只是模型**：`AssistantRuntimeConfig` 当前保存 `max_round` 与 `timeout`，业务侧若需要强制轮数/总时限控制，应在自定义 Gateway 或外层任务中落实。

---

## 更新记录 📜

请查看 [CHANGELOG.md](CHANGELOG.md)。当前版本为 **0.2.3**，包含 SSE 流式处理、请求期多模态适配和 Gateway 上下文压缩。

> Made with 🐾 — 欢迎按自己的业务覆写 Gateway 钩子，但别把上下文调用链拆坏了喵。
