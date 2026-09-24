# 0.2.3 更新日志

## 新增

1. 请求期多模态适配支持 aioverse 的 URL 音频、Base64 视频和 URL 视频 Segment
2. 保留旧 `UnknownSegment` 视频类型的兼容识别
3. 新增音频、视频 Segment 的能力过滤回归测试

## 依赖

1. `aioverse` 最低版本提升至 `0.4.5`

---

# 0.2.2 更新日志

## 新增

1. `ContextCompressionMixin` 上下文压缩能力
   - 支持软阈值 API Markdown Memory 压缩
   - 支持可选本地 `Compresser` 硬阈值保护
   - 压缩失败、无收益或响应异常时保留原会话并回滚
2. `ClawConfig` 新增上下文压缩配置
   - `context_compression_keep_contexts`
   - `context_compression_max_tokens`
3. 新增上下文压缩、Gateway 生命周期和 `StreamHandler` 回归测试

## 文档

1. 更新 README 的安装、配置、调用链、工具、上下文压缩和开发验证说明
2. 补充 `CONTEXT_COMPRESSION_DESIGN.md` 设计记录

---

# 0.2.1 更新日志

## 新增

1. `core.stream_handler` 新增 `StreamHandler` 流式增量处理器
   - `merge(delta)` — 合并 SSE 增量（content / reasoning_content / tool_calls 分片拼接）
   - `flush()` — 从缓存构建完整 `AssistantOutput`
   - `reset()` — 清空本轮缓存
   - `build_tool_calling_context()` — 从累积的 tool_calls 构建 `ToolCallingContext`
2. `AssistantGateway` 新增流式支持：
   - `on_stream_chunk(chunk)` — 流式数据块事件钩子
   - `on_stream_request()` — 流式请求路径（可被子类重写）
   - `on_common_request()` — 非流式请求路径（可被子类重写）
   - `stream_handler` 懒加载 property + `set_stream_handler()` setter
3. `AssistantModelConfig` 新增 `support_streaming` 字段，默认 `True`，支持按模型控制流式开关
4. `BaseContextsBlock._clean_legacy_fields()` 新增旧数据兼容验证器，自动剥离误混入的 `content` / `reasoning_content` / `role` 字段

## 重构

1. `AssistantGateway.round_call()` 拆分为 `on_stream_request()` / `on_common_request()`，流式与非流式路径独立
2. 流式增量缓存（`_pending_content` / `_pending_reasoning` / `_pending_tool_calls`）从 Gateway 迁移至 `StreamHandler`
3. `_merge_delta()` / `_reset_pending()` / `_flush_pending()` 从 Gateway 移除，委托给 `StreamHandler`
4. `StreamHandler.merge()` 拆分为 `_merge_tool_call` / `_find_tool_call` / `_update_tool_call` / `_add_tool_call`，最大嵌套 3 层
5. `build_tool_calling_context()` 改为 `ToolCallingModel.model_validate()` 列表推导
6. 所有 `print()` 改为 `logging.getLogger(__name__)` 全局单例日志

## 修复

1. `BaseContextsBlock` 新增 `_clean_legacy_fields` 模型验证器，兼容旧版本 sessions.json 中 `ToolCallingContextsBlock` 顶层混入 `content` / `reasoning_content` 导致的 `extra_forbidden` 错误

---

# 0.2.0 更新日志

## 新增

1. 初始发布 — 基于 aioverse 构建的 AI Agent 框架
2. `AssistantGateway` 事件驱动网关架构
3. 工具集：文件操作、代码执行、网络请求、Pip 管理等
4. `AssistantSession` 会话管理，支持序列化/反序列化
5. `ToolCallingContextsBlock` 工具调用上下文块
6. `PydanticModelsFactory` 工厂模式
7. `ToolsManager` / `KeysManager` 管理器
