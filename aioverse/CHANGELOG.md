# 0.4.5 更新日志

## 新增

1. `models.segments` 新增 `AudioUrlSegment`，支持 URL 音频输入
2. `models.segments` 新增 `VideoInputSegment` 与 `VideoUrlSegment`，支持 Base64 和 URL 视频输入
3. `BaseContext` 的多模态内容 union 支持显式音频、视频 Segment，并保留 JSON round-trip 类型

---

# 0.4.4 更新日志

## 新增

1. `models.response.delta` 新增 `Delta` 模型，用于 SSE 流式增量数据
2. `models.response.stream_chunk` 新增 `StreamChunk` / `StreamChoice` 模型，用于流式响应解析
3. `models.tool_calling.ToolCalling` 新增可选 `index` 字段，支持流式分片合并
4. `OpenAIClient.call_stream()` 新增流式调用方法，返回 async generator 逐块产出 `StreamChunk`
5. `OpenAIClient._iter_sse_chunks()` 新增 SSE 行解析器，处理 `data:` 行与 `[DONE]` 终止信号
6. `models.tool_calling.ToolCalling` 所有字段添加默认值（`id=""` / `type="function"`），`Function` 字段添加默认值（`name=""` / `arguments=""`）

## 修复

1. `Delta.tool_calls` 类型从 `List[ToolCalling]` 改为 `List[Dict]`，修复流式 tool_calls 分片中 `id`/`type` 仅首个 chunk 存在导致后续 chunk 解析失败、arguments 丢失的问题

## 重构

1. 删除自定义日志系统 `Log.py` 及 `protocols/log_*.py`，全面改用标准库 `logging`
   - 各模块通过 `logger = logging.getLogger(__name__)` 获取全局单例
   - `OpenAIClient` 移除 `async_log` 参数
2. `OpenAIClient` 提取 `_ensure_ready()` / `_build_request()` 方法，`call()` 与 `call_stream()` 共用请求构建逻辑
2. `call_stream()` 扁平化重构，嵌套层级从 6 层降至 3 层

---

# 0.4.3 更新日志

## 新增

1. `models._contexts_status` 新增内部状态管理模块，支持脏标记（dirty flag）缓存机制
   - `flatten_contexts()` 在 dirty 时自动重建缓存
   - 避免频繁 `to_list()` 调用时的重复计算
2. `models.segments` 新增多模态内容模块，包含 `Text`、`ImageUrl`、`AudioInput` 等 Segment 类型
3. `models.blocks` 新增上下文块模块：
   - `ToolCallingBlock` — 工具调用块，包含请求与执行结果，支持 `verify_tool_ids()` 验证完整性
   - `ContextsBlock` — 普通消息块，可包含多条连续 Context
4. `models.assistant_key` 新增 `AssistantKey` 数据模型，支持 `is_enable`、`is_available` 状态管理
5. `models.contexts` 新增 `User`、`ToolCallingContext`、`ToolOutput` 独立上下文类型
6. `models.response` 新增 API 响应体模块：`Response`、`Choice`、`Usage`
7. `protocols` 新增上下文块协议 `ContextsBlockProtocol`，定义 `__iter__`、`__len__`、`append`、`insert`、`delete` 接口
8. `Context.set_token()` 新增 token 设置方法，支持 `exclude=True` 排除序列化

## 重构

1. 全面重构 `base_models` → `models`，所有模型按职能分组为子模块：
   - `models/contexts/` — 对话上下文
   - `models/blocks/` — 上下文块
   - `models/segments/` — 多模态内容
   - `models/response/` — API 响应
2. `OpenAIClient.call()` 参数更新：
   - 新增 `assistant_key` 显式参数，支持多 Key 管理
   - 移除 `body` 注入中的 `tools` 参数合并，交由调用方控制
3. `ContextManager` 内部全面使用 `_ContextsStatus` 管理状态
   - 新增 `__slots__` 优化内存占用
   - `to_file()` / `from_file()` 支持子类重写为异步版本
4. `build_tool_schema_by_doc` 移至 `utils.syntax_sugar`（野路子功能）

## 优化

1. 完善所有 Pydantic 模型的类型注解与字段验证
2. 优化 `Context` 的 `ConfigDict(slots=True)` 内存占用
3. `_ContextsStatus` 使用 `PrivateAttr` 管理内部缓存，避免序列化污染

---

# 0.4.2 更新日志

## 新增

1. 全面更新 `README.md`，补充以下内容：
   - `NullObject` 空对象模式文档
   - 日志系统协议（`LogProtocol` / `LogFormatProtocol` / `LogWriteProtocol`）详细说明
   - 内置日志实现（`AsyncLog` / `SyncLog` / `AsyncWriter` / `SyncWriter` / `LogFormatter`）
   - 上下文块协议 `ContextsBlockProtocol` 接口说明
   - 错误处理 `ResponseCodeError` 使用示例
   - 数据模型一览汇总表
   - CHANGELOG 引用链接

## 优化

1. 完善项目文档结构，提升开发者体验

# 0.4.1 更新日志

## 新增

1. `utils.syntax_sugar`新增`build_tool_schema`方法，返回`models.tool_schema.Tool`对象

## 移除

1. `utils.syntax_sugar`移除上下文构建语法糖(函数名叫啥我忘了)，因为现在上下文基于pydantic，可直接通过内置方法快速转换，无需手动实现语法糖

# 0.4.0更新日志

## 新增

1. 新增 `models.tool_schema` 模块，包含 `Argument`、`Parameters`、`Function`、`Tool` 等 Pydantic 数据模型，用于定义 AI 可调用的工具结构
2. 新增 `models.tool_call_response` 模块，包含 `Function`、`ToolCall` 等 Pydantic 数据模型，用于处理 AI 返回的工具调用请求
3. 新增 `models.contexts.AssistantToolCalls`，专门处理 AI 返回的工具调用上下文（原 `ToolCalls` 改名）
4. 新增 `models.contexts.ToolExecuteResult`，用于构建工具执行结果上下文，支持 `tool_call_id` 关联
5. 新增 `managers.tool_manager.ToolManager`，提供工具注册、执行、序列化一体化管理
   - `register(func, tool)`：自动通过函数名注册工具
   - `tool_executer(tool_calls)`：安全执行工具调用，同步/异步函数自动适配，错误捕获并返回字符串
   - `to_list()`：导出为 OpenAI 标准 tools 格式
6. 新增 `utils.syntax_sugar.build_contexts`，支持将 OpenAI 格式的字典列表快速转为 `ContextManager`
7. 所有 Pydantic 模型新增 `to_dict()` 方法，统一序列化出口
8. 新增`models.ModelConfig`，用于配置模型，后续可能扩展`max_token`之类的可选参数

## 更改

1. `models.tool` 重命名为 `models.tool_schema`，语义更清晰（定义工具结构 vs 处理工具调用）
2. `models.tool_call` 重命名为 `models.tool_call_response`，语义更清晰（AI 返回的调用请求）
3. `models.contexts.ToolCalls` 重命名为 `AssistantToolCalls`，避免与 `tool_call_response` 模块混淆
4. `managers.context_manager` 修复方法名大小写：`hasPrompt()` → `has_prompt()`
5. `OpenAIClient.set_key_manager` 修复变量名：`keyManager` → `key_manager`
6. `models.contents.Segment.__len__` 修复变量名：`len(data)` → `len(self.data)`
7. `models.contexts.Context.to_dict` 修复循环变量判断：`isinstance(self.content, Segment)` → `isinstance(content, Segment)`
8. `models.contexts.AssistantToolCalls.to_dict` 修复未定义变量：`tool_calls` → `self.tool_calls`
9. `models.tool_schema.Parameters.to_dict` 修复字典遍历：`for name, arg in self.properties` → `for name, arg in self.properties.items()`
10. `utils.syntax_sugar.build_contexts` 修复不存在的类方法：`Context.from_dict()` → `Context(**context)`
11. `managers.tool_manager` 补充缺失的类型导入：`List`、`Any`
12. `OpenAIClient`的初始化传参的显式传入`api_url`等，更改为传入`models.ModelConfig`，便于扩展
13. `aioverse.types`的大部分类均基于pydantic重构并移动到`aioverse.models`里

## 删除

1. 删除 `models.tool_call.ToolCallsArray` 类，功能由 `List[ToolCall]` 直接替代，简化接口

# 0.3.3更新日志

## 说明

1. 感觉原来的项目垃圾太多了，我的设计理念就是一个`简单`, `轻量`的openai请求库，所以给垃圾全删了，仅保留核心功能

## 新增

1. `aioverse.managers.ContextManager`新增`getList`方法，返回实例的`self._contexts`

## 更改

1. `aioverse.types.Context`的`content`不再强行绑定`ContentArray`，只要该实例支持`toString`或`toList`都行，兜底`str()`
2. `OpenAI.py`基本移除所有的驼峰命名，`OpenAIClient`的`chatCompletion`方法改名为`call`
3. 项目的所有驼峰命名基本已经转为蛇形命名
4. `ContentArray`模块迁移至`models/`内，用pydantic用于类型验证

## 移除

1. 移除`AITools`模块(没用，且需要提示词支持)
2. 移除`Typing.py`(`AITools`模块的依赖，跟着移除)
3. 移除`ExceptionHandler`模块，请自行解决错误处理(因为原本的就没有搞好 没法预料所有的错误)

# 0.3.2更新日志

## Log.py (优化`get_log`方法逻辑)
1. 添加内部变量`_global_logs`，用于存储日志实例
2. `get_log`方法传入**完全相同**的参数时
  - `_global_logs`存在相同参数的键->**直接返回**对应日志实例
  - 不存在则**创建**并**记录**

## OpenAI.py
1. 删除`safeRequest`方法
2. `OpenAIClient.chatCompletion`的参数`contextManager`改名为`context_manager`

## 新增
1. 为`Content`新增`from_dict`方法，可以通过符合以下格式的字典转为实例
```python伪代码
{
	"type": <类型>,
	<类型>: <数据>
}
```
2. 为`Context`新增`from_dict`方法，可以将符合以下格式的字典转为Context实例 (`content`的值将被直接传入`Context`实例化，无论是`list`/`str`/`dict`/...) 完全透传
```python伪代码
{
	"role": <角色>,
	"content": <内容>,
	"token": <token数> # 这不是必须
}
```
3. 新增`utils/`文件夹，用于存放辅助工具
4. 新增`utils/syntax_sugar.py`，语法糖函数
5. `utils/syntax_sugar.py`新增`build_contexts`函数，用于将OpenAI标准上下文格式转为ContextManager
6. `aioverse.types.context`新增`to_raw_dict`方法，返回带有`token`, `content`, `role`完整信息的字典

## Future
1. 将更多使用驼峰命名的函数名/参数改为蛇形命名 (写java写傻了)
2. 可能会支持流式输出，等我去研究研究OpenAI流式输出格式先😋😋


# 0.3.1更新日志

## Log.py
1. `getLog`函数改名为`get_log`
2. `asyncWriter`与`syncWriter`添加`flush`参数，默认值`False`，当传入`True`时 强行写入缓冲区内容
```python
flush: bool = False
```

## OpenAI.py
1. `OpenAIClient`的`apiUrl`改名为`api_url`

# 0.3.0更新日志

## OpenAI.py的OpenAIClient类
1. 返回类型由`str`改为`Item`，添加了**可扩展性**
2. 支持返回`token`, `model`, `request_id`, `reasoning`
  - `token`: token使用量
  - `model`: 使用的模型
  - `request_id`: 请求id
  - `reasoning`: 思维链
3. 优化请求参数构建逻辑，`headers`, `params`, `body`均采用`**`解包

## managers.ContextManager
1. 属性`self._context`更名为`self._contexts`
2. 添加`_token`属性
  - 可在实例化时通过`token`可选参数传入
  ```python
  ContextManager(token=114514)
  ```
  - 也可以在实例化后通过实例的`setToken`方法传入
  ```python
  ContextManager().setToken(114514)
  ```
3. 新增使用装饰器`property`的`token`方法
  1. 当`self._token`小于等于`0`->通过遍历`self._contexts`并相加后*1.3
  2. 当`self._token`大于`0`->直接返回`self._token`
4. `isOut()`方法更改
  - 参数`maxToken`改名为`max_token`
  - 使用`self.token`函数来获取token用量
5. `clear()`方法更新
  - 添加`keep_prompt`(`bool`)参数，可选是否保留上下文


# 0.2.4更新日志
- 为`Context`类添加了一个`token`参数
  - 默认值为`None`
  - 当值不为默认值时->`len(Context)`返回的是`self.token`的值
  - 当值为默认值时->`len(Context)`返回`self.content`的`__len__`方法返回值
  - 这是为了后续自定义上下文占用token预留了**接口**，增加**灵活性**


# 0.2.3更新日志

## Item优化
1. 增加` __slots__ `属性，优化性能占用
2. ` __getattr__ `与` __setattr__ `使用`self._mapping`字典代理
3. `toDict()`返回一个**浅复制**的字典
4. `toDict()`与`toString()`支持动态属性


# 0.2.2更新日志
1. 为一些实例添加了` __slots__ `属性，**优化内存使用**
2. 将`ContentArray`的`addContent'`方法独立出2个方法
  - `addContent`: 仅支持使用`Content`实例添加
  - `addData`: **自动**通过传入参数构建`Content`对象，通过调用`addContent`方法添加


# 0.2.1更新日志
- 让`types.content_array.ContentArray.addContent`支持使用`types.content.Content`作为参数传入
  - 具体可查看`aioverse/types/content_array.py`


# 0.2.0更新日志

## **兼容多模态**
- 添加`Content`, `ContentArray`等抽象类以支持多模态
  - 目前关系`Content`->`ContentArray`->`Context`->`ContextManager`
- **`Content` 更新**
  - `upload_type`: 上传的类型
  - `upload_data`: 上传的数据
- **`ContentArray` 更新**
  - 可**链式调用**添加`Content`
  - toList() 方法输出类似
  ```json
  [
    {"type": "text", "text": "请描述这张图片"},
    {"type": "image_url", "image_url": "xxx.com/picture.png"}
  ]
  ```
- **`Context` 更新**
  - 支持`ContentArray`与`str`**混合使用**
- **`ContextManager` 修复**
  - 修复一些token统计的bug

- 为`Content`, `ContentArray`, `Context`均**添加了` __len__ `**，便于**token统计**

## 更改**文件关系**
  1. 原`model/`被**弃用**
  2. **结构体**分类为`types/`
  3. **错误**分类为`errors/`
  4. **协议**分类为`protocols/`

## 协议弃用
- `KeyManagerProtocol`, `ContextManagerProtocol`等协议被弃用
  - 纯占空间**作用不大**
  - 简单的类**无需**继承协议
  - 所有依赖`KeyManagerProtocol`与`ContextManagerProtocol`的脚本
    - `KeyManagerProtocol`**替换**为`KeyManager`
    - `ContextManagerProtocol`**替换**为`ContextManager`