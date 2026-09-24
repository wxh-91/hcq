# aioverse 🌌

[![Python Version](https://img.shields.io/badge/python-%3E%3D3.11-blue)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-0.4.5-green)]()

基于 `aiohttp` 构建的轻量异步 OpenAI API 请求库。内置**上下文管理**、**工具调用 (Function Calling)**、**流式输出 (SSE)**、**多密钥轮询**与**多模态内容**支持。所有数据模型基于 `Pydantic v2`，开箱即用。

> 设计哲学：**轻量、专注核心、拒绝过度封装**

---

## ✨ 特性

- **纯异步** — 基于 `aiohttp` + `asyncio`，高并发无压力
- **流式调用** — `call_stream()` 支持 SSE 流式响应，async generator 逐块产出
- **工具调用** — 原生支持 Function Calling，工具 Schema 构建语法糖
- **上下文管理** — 对话历史组织、System Prompt 管理、Token 追踪与裁剪，支持块式上下文（`ToolCallingBlock` / `ContextsBlock`）
- **密钥轮询** — `KeyManager` 多 Key 自动轮询，避免单点限流
- **多模态** — 内置 `Segment` 体系：文本、图片、音频和视频输入
- **Pydantic 全栈** — 请求参数、上下文、Schema、响应体全部带类型校验
- **标准日志** — 基于标准库 `logging`，模块级 `logger` 单例
- **空对象模式** — `NullObject` 安全吞掉任何调用，优雅处理可选依赖
- **全链路类型注解** — IDE 补全体验拉满

---

## 📦 安装

```bash
pip install /path/to/aioverse
```

### 依赖

| 包名 | 版本 |
|------|------|
| Python | >= 3.11 |
| aiohttp | >= 3.11 |
| aiofiles | == 25.1.0 |
| orjson | == 3.11.9 |
| pydantic | == 2.13.4 |

---

## 🚀 快速开始

### 基础对话

```python
import aiohttp
import asyncio
from aioverse.OpenAI import OpenAIClient
from aioverse.models import ModelConfig
from aioverse.models.contexts import Prompt, Context
from aioverse.managers import ContextManager

async def main():
    config = ModelConfig(
        model_name="gpt-4o",
        api_url="https://api.openai.com/v1/chat/completions",
        model_keys=["sk-your-key"],
    )

    async with aiohttp.ClientSession() as session:
        client = OpenAIClient(config, session)

        ctx = ContextManager()
        ctx.set_prompt(Prompt(content="你是一个乐于助人的助手。"))
        ctx.add_context(Context(role="user", content="你好，请介绍自己。"))

        response = await client.call(ctx)
        print(response.choices[0].message.content)

asyncio.run(main())
```

### 多密钥自动轮询

```python
config = ModelConfig(
    model_name="gpt-4o",
    api_url="https://api.openai.com/v1/chat/completions",
    model_keys=["sk-key-1", "sk-key-2", "sk-key-3"],
)
```

### 工具调用 (Function Calling)

```python
from aioverse.utils.syntax_sugar import build_tool_schema

# 构建工具 Schema
weather_tool = build_tool_schema(
    tool_name="get_weather",
    tool_description="获取指定城市的当前天气",
    requirements=["location"],
    arguments={
        "location": ("string", "城市名称，如：北京"),
    }
)

# 发送请求（注入工具）
response = await client.call(ctx, body={"tools": [weather_tool.model_dump()]})

# 处理工具调用
if response.choices[0].finish_reason == "tool_calls":
    ctx.add_context(response.choices[0].message)

    for call in response.choices[0].message.tool_calls:
        import json
        args = json.loads(call.function.arguments)
        result = await get_weather(**args)
        ctx.add_context(ToolOutput(content=result, tool_call_id=call.id))

    # 再次请求获取总结
    final = await client.call(ctx)
    print(final.choices[0].message.content)
```

### 多模态内容

```python
from aioverse.models import (
    AudioInputSegment,
    AudioUrlSegment,
    TextSegment,
    UserContext,
    VideoInputSegment,
    VideoUrlSegment,
)

# 文本 + 音频 / 视频混合消息
context = UserContext(content=[
    TextSegment(text="请分析这些媒体内容。"),
    AudioInputSegment(data="...", format="wav"),
    AudioUrlSegment(url="https://example.com/audio.mp3"),
    VideoInputSegment(data="...", format="mp4"),
    VideoUrlSegment(url="https://example.com/video.mp4"),
])
```

`AudioInputSegment` 与 `VideoInputSegment` 接收 Base64 数据；
`AudioUrlSegment` 与 `VideoUrlSegment` 引用远程 URL。四类段都支持可选
`format` 字段。它们只描述请求内容，不负责本地读取、上传、下载、转码或抽帧；
实际端点是否接受对应 `type` 由调用方按模型能力决定。

---

## 📚 API 参考

### OpenAIClient

```python
class OpenAIClient:
    def __init__(
        self,
        model_config: ModelConfig,
        session: aiohttp.ClientSession,
    ): ...

    async def call(
        self,
        context_manager: ContextManager,
        assistant_key: AssistantKey,
        headers: Dict[str, Any] = {},
        params: Dict[str, Any] = {},
        body: Dict[str, Any] = {},
        timeout: int = 90,
    ) -> Response: ...
```

- `context_manager` — 对话上下文，通过 `to_list()` 导出为 messages 格式
- `assistant_key` — API 密钥对象
- `headers` / `params` / `body` — 透传给 `session.post`，可用于注入 `tools`、`temperature` 等参数
- `timeout` — 请求超时时间（秒），默认 90s
- 返回 `Response` 模型（Pydantic），包含 `choices`、`usage`、`id` 等

### ContextManager

对话上下文组织器。内部通过 `_ContextsStatus` 维护状态，支持脏标记（dirty flag）缓存机制。

支持三种上下文类型：

| 类型 | 说明 |
|------|------|
| `Context` | 普通消息（user / assistant / system） |
| `ToolCallingBlock` | 工具调用块，包含请求与执行结果，支持 `verify_tool_ids()` 验证 |
| `ContextsBlock` | 普通消息块，可包含多条连续 Context |

```python
ctx = ContextManager()

# System Prompt
ctx.set_prompt(Prompt(content="你是..."))

# 添加用户消息
ctx.add_context(Context(role="user", content="你好"))

# 添加工具调用上下文
ctx.add_context(ToolCallingContext(tool_calls=[...]))

# 添加工具执行结果
ctx.add_context(ToolOutput(content="结果", tool_call_id="call_xxx"))

# 添加上下文块
ctx.add_context(ToolCallingBlock(tool_calling=..., tool_outputs=[...]))

# 导出为 OpenAI messages 格式
messages = ctx.to_list()     # -> List[Dict]

# Token 管理
ctx.set_token(114514)
print(ctx.token)

# 裁剪最早的一条上下文
ctx.trim()

# 清空（可选保留 Prompt）
ctx.clear(keep_prompt=True)
```

> 💡 `ContextManager` 支持 `to_file()` / `from_file()` 持久化上下文，且可在子类中重写为异步版本。

### _ContextsStatus — 内部状态管理

`ContextManager` 内部通过 `_ContextsStatus` 管理上下文状态，支持脏标记机制：

- 当上下文发生变化时自动标记为 `dirty`
- `flatten_contexts()` 在 dirty 时自动重建缓存
- 避免频繁调用时的重复计算

### ModelConfig

```python
class ModelConfig(BaseModel):
    model_name : str               # 模型名
    model_alias: str | None = None # 别名（默认 = model_name）
    api_url    : str               # API 地址
    model_keys : List[AssistantKey | str]  # 至少 1 个 Key

    token_limit: int = 0           # Token 上限（用于压缩判断）
    max_token  : int = 0           # 最大生成长度

    support_image: bool = False
    support_video: bool = False
    support_audio: bool = False
    support_tool : bool = False
    support_think: bool = False
```

### AssistantKey

```python
class AssistantKey(BaseModel):
    key          : str
    is_enable    : bool = True
    is_available : bool = True
```

### 工具 Schema 构建

#### 方式一：语法糖（推荐）

```python
from aioverse.utils.syntax_sugar import build_tool_schema

tool = build_tool_schema(
    tool_name="calculator",
    tool_description="简单的加减乘除计算器",
    requirements=["a", "b", "op"],
    arguments={
        "a": ("number", "第一个数字"),
        "b": ("number", "第二个数字"),
        "op": ("string", "运算符：+ - * /", "+"),  # 可选默认值
    }
)
```

#### 方式二：原生 Pydantic 模型

```python
from aioverse.models.tool_schema import Tool, Function, Parameters, Argument, _Empty

tool = Tool(
    function=Function(
        name="calculator",
        description="...",
        parameters=Parameters(
            properties={
                "a": Argument(type="number", description="第一个数字"),
                "b": Argument(type="number", description="第二个数字"),
            },
            required=["a", "b"]
        )
    )
)
```

---

## 🧩 扩展机制

### 上下文块协议 (ContextsBlockProtocol)

所有上下文块（`ToolCallingBlock`、`ContextsBlock`）均实现该协议：

```python
class ContextsBlockProtocol(ABC):
    def __iter__(self) -> Iterator[Context]: ...
    def __len__(self) -> int: ...
    def append(self, context: Context): ...
    def insert(self, index: int, context: Context): ...
    def delete(self, index: int): ...
```

`ToolCallingBlock` 额外提供：
- `verify_tool_ids()` — 验证工具调用结果是否完整
- `tool_calling_ids` — 懒加载的 tool_call_id 列表

---

## 📁 项目结构

```
aioverse/
├── src/
│   └── aioverse/
│       ├── OpenAI.py                 # OpenAI API 客户端

│       ├── errors/
│       │   └── ResponseCodeError.py  # API 错误响应
│       ├── managers/
│       │   └── context_manager.py    # 上下文管理器
│       ├── models/
│       │   ├── model_config.py       # 模型配置
│       │   ├── assistant_key.py      # API Key 模型
│       │   ├── _contexts_status.py   # 上下文内部状态
│       │   ├── contexts/             # 对话上下文
│       │   │   ├── base_context.py
│       │   │   ├── prompt.py
│       │   │   ├── user.py
│       │   │   ├── tool_calling_context.py
│       │   │   └── tool_output.py
│       │   ├── blocks/               # 上下文块
│       │   │   ├── contexts_block.py
│       │   │   └── tool_calling_block.py
│       │   ├── segments/             # 多模态内容片段
│       │   │   ├── base_segment.py
│       │   │   ├── text_segment.py
│       │   │   ├── image_url_segment.py
│       │   │   └── audio_segment.py
│       │   ├── response/             # API 响应
│       │   │   ├── response.py
│       │   │   ├── choice.py
│       │   │   └── usage.py
│       │   ├── tool_schema.py        # 工具定义 Schema
│       │   └── tool_calling.py       # 工具调用结果
│       ├── protocols/                # 抽象协议层
│       │   ├── contexts_block_protocol.py
│       │   ├── log_protocol.py
│       │   ├── log_format_protocol.py
│       │   └── log_write_protocol.py
│       └── utils/
│           ├── holder.py             # NullObject
│           └── syntax_sugar.py       # 语法糖
├── pyproject.toml
├── README.md
└── CHANGELOG.md
```

---

## 🛠️ 工具函数

### NullObject — 安全空对象

无论你怎么调用它，它都会安静地返回自身，绝不报错：

```python
from aioverse.utils.holder import NullObject

nope = NullObject()
nope()                          # -> NullObject
nope.log("hello")               # -> NullObject
await nope                      # -> NullObject
await nope.some_method(1, 2, 3) # -> NullObject
```

常用于可选依赖注入的默认值，优雅替代 `if xxx is not None` 检查。

### build_tool_schema — 快速构建工具 Schema

```python
from aioverse.utils.syntax_sugar import build_tool_schema

tool = build_tool_schema(
    tool_name="my_tool",
    tool_description="工具描述",
    requirements=["arg1"],
    arguments={
        "arg1": ("string", "参数说明"),
        "arg2": ("integer", "可选参数", 42),  # 有默认值
    }
)
```

### build_tool_schema_by_doc — 野路子（仅供娱乐）

通过解析函数注释中的 JSON 自动构建 Schema 😋

---

## ❌ 错误处理

| 异常 | 说明 |
|------|------|
| `ResponseCodeError` | API 返回非 200 状态码时抛出，包含 `code` 和 `response` 属性 |

```python
from aioverse.errors import ResponseCodeError

try:
    response = await client.call(ctx)
except ResponseCodeError as e:
    print(f"API 错误: {e.code} - {e.response}")
```

---

## 📋 数据模型一览

| 模块 | 模型 | 说明 |
|------|------|------|
| `models.contexts` | `Context`, `Prompt`, `User`, `ToolCallingContext`, `ToolOutput` | 对话上下文 |
| `models.segments` | `BaseSegment`, `TextSegment`, `ImageUrlSegment`, `AudioInputSegment`, `AudioUrlSegment`, `VideoInputSegment`, `VideoUrlSegment` | 多模态内容片段 |
| `models.blocks` | `ToolCallingBlock`, `ContextsBlock` | 上下文块 |
| `models.response` | `Response`, `Choice`, `Usage` | API 响应体 |
| `models.tool_schema` | `Tool`, `Function`, `Parameters`, `Argument`, `_Empty` | 工具定义 Schema |
| `models.tool_calling` | `ToolCalling`, `Function` | AI 返回的工具调用 |
| `models.model_config` | `ModelConfig` | 模型配置 |
| `models.assistant_key` | `AssistantKey` | API 密钥 |
| `models._contexts_status` | `_ContextsStatus` | ContextManager 内部状态 |

---

## 🔄 更新日志

详见 [CHANGELOG.md](./CHANGELOG.md)

---

## 📄 许可证

MIT License
