PROJECT_GUIDE — v3 终极扩容版（2026-10-24 全量审计）
================================================================================
读者对象：完全失忆的新接手大模型（无历史对话背景）+ 人类开发者。
阅读目标：读完本文档，无需打开任何源码，即可完全清楚每个文件能干什么、
系统怎么运转、哪些事绝对不可以做。
信息来源：2026-10-24 深度目录遍历（find + ls -R + 函数级索引）实测，
与磁盘真实状态一致。禁止臆造文件或假设文件不存在。
文档结构：第一类（全量项目目录树与文件职责，>30KB）→ 第二类（设计理念
与项目冷知识）→ 第三类（架构裁决与未决问题）。
================================================================================
================================================================================
第一类：全量项目目录树与文件职责（信息密度最大区）
================================================================================

【读者须知】本部分列出的每一个文件均为 ${PROJECT_ROOT}/ 下真实存在的物理文件。
新接手的大模型必须先通读本部分，遇到任何文件时按本部分描述的职责行事，
严禁凭空假设文件不存在或臆造文件内容。目录树与文件列表为 2026-10-24 深度
遍历（find + ls -R）实测结果，与磁盘状态一致。

────────────────────────────────────────────────────────────
0. 项目一句话定位
────────────────────────────────────────────────────────────
运行于 WSL（Ubuntu，root 用户，目录 ${PROJECT_ROOT}/）的飞书机器人。
技术栈：NoneBot 2 + Python 3.10（venv310 虚拟环境）+ 飞书长连接适配器 +
自研 AUP 工具协议 + 动态插件架构。核心链路：飞书私聊消息 → dz.py 入口
（前置别名物理替换）→ ai.py EHIR 三层意图分类 → admin.py AUP 工具执行
（tool_center 注册表门禁）→ 输出清洗层 → 飞书回复。聊天后端优先走
DeepSeek 网页版免费通道（C 端），失败自动降级本地 API 策略。

────────────────────────────────────────────────────────────
1. 根目录文件（${PROJECT_ROOT}/ 下 25 个文件逐一说明）
────────────────────────────────────────────────────────────

【dz.py — 主入口与消息路由（27985B，16 个 def/class）】
系统唯一允许的进程入口，命令固定为 `python3 dz.py`（venv310/bin/python）。
职责明细：
- 组装 NoneBot 驱动与适配器：注册 Feishu 适配器；OneBot V11 仅在 .env 中
  ONEBOT_WS_URLS 非空时才条件注册（当前已注释 → 只加载 Feishu）。
- 启动生命周期钩子：on_startup 打印后端模式（api）、启动 defense watchdog
  后台探针（间隔 30s，默认 DRY-RUN 不处置）、注入飞书报警通道（收件人取
  FEISHU_ADMIN_OPEN_ID）、签到功能开关判断。
- 消息入口 handle_msg → _handle_msg_inner → _handle_msg_locked：
  ① 私聊锁（每用户 asyncio.Lock 防并发）；② 调用 _preprocess_alias()
  做【别名前置物理替换】：正则前缀匹配 SITE_MAP（读 plugins/crawler/
  site_map.json），命中（如 "tz 92285"）即整串替换为完整 URL 并打日志
  「[安全别名] 前置解析」，消息进入 AI 前已完成，绝不交给大模型解析；
  ③ 群聊冷却与夜间休眠（8-23 点活跃窗口外静默丢弃；群消息 60s 冷却 +
  概率触发，@ 机器人时 100% 响应）；④ 图片消息走 ocr.py 识别并把 OCR
  文本注入上下文；⑤ 文本消息转交 ai.ask_ai()。
- send_segmented_reply：最终发送出口。长回复分段发送，且对每段执行
  sanitize_output 兜底清洗（防泄漏的最后一道物理拦截）。
- handle_url / handle_downloadable：链接与下载产物的专用处理分支。
- 文件头部为【绝对红线】注释（只允许 python3 dz.py 启动；严禁任何涉及
  Napcat / QQ 协议端 / launcher*.bat / inject_napcat.ps1 / KillQQ.bat /
  node napcat.mjs 的启动脚本或命令）。
- 副作用：启动即占用 127.0.0.1:8082 端口（NoneBot uvicorn）；产生
  feishu_runtime.log 运行日志。

【ai.py — AI 大脑（73318B，40 个 def/class，全项目最大单文件）】
处理消息完整链路的核心模块。职责明细：
- TodoListManager / TodoItem：会话级待办清单（add/get_pending/mark_running/
  mark_done/mark_failed/list_items），供神授模式与多轮任务追踪。
- EHIR 三层意图分类 _detect_intent(prompt, context)：
  L1 rule_match（静态正则，plugins/core_utils/lexicon.py）；
  L2 LexiconDB 词库命中（SQLite，零 API 消耗，含动态学习：模型判 tool
  后 _learn_tool_word 自动提取动作词入库）；
  L3 _llm_detect_intent（大模型深度分类 + _parse_intent_reply 解析返回，
  分类 prompt 内置「站点缩写别名类」规则：形如 tz 92285 / tz#92285 /
  yt abc123 → 判 tool，直接原样传参）。
  低置信度处理：confidence < 阈值时暂存原消息并回复追问，不执行工具。
- ask_ai 主入口：意图 = chat → 走 C 端 web_deepseek（deepseek_web_
  integration.ask_web_deepseek），失败降级本地 chat 策略；意图 = tool →
  构建工具 schema 列表（tool_center.get_ai_visible_schemas）进入多轮
  tool_calls 循环（TOOL_MAX_ROUNDS=5）：模型每轮产出 tool 调用 →
  admin.execute_tool_directly 执行 → submit_tool_results 回填 → 继续
  直到模型输出最终回复。
- 输出清洗层（本项目最高优先级物理防线）：
  sanitize_output(text)：正则检测泄漏特征（"调用了" / "tool_calls" /
  参数 key / JSON 结构 / 转义炸弹 \n\\" 等），命中返回固定文案
  「系统内部处理中，请重试。」；
  _sanitize_for_chat(history)：把工具执行结果剥离为「已调用工具：xxx」
  纯文本，绝不把 arguments JSON 拼进模型上下文（参数隔离）；
  _strip_code_blocks：剥离 markdown 代码块。
- 多轮压缩：should_compress / compress_history（历史超长自动摘要压缩）。
- run_sub_agent / run_async_task_chain / _run_fast_chain：子代理与快速
  任务链（神授模式支持）。
- 副作用：读取 memory.json / knowledge.db / lexicon.db；产生
  episodic 记录（episodic.log）；调用外部模型 API。

【admin.py — AUP 工具执行中枢（23592B，16 个 def/class）】
所有工具调用的唯一汇聚点。职责明细：
- execute_tool_directly(name, args, sender_id, ...)：核心执行器。流程：
  is_active 校验（tool_center 数据库）→ check_permission 权限检查 →
  参数预处理（_fix_missing_newlines 修复 Python 特征换行丢失）→ 调用
  注册函数 → AUPResult 包装返回。绕过它的裸工具调用是红线违规。
- _call_tool_router：老式文本工具路由（兼容层）。
- handle_admin_command / _execute_admin_command：管理指令（开关群聊、
  agent 管理、get_xmkj、query_knowledge、recall_conversation、
  clean_media_tool、checkin_tool 等）。
- build_tool_schema / _get_tool_default_desc：构建模型可见工具 schema。
- _render_aup_result：AUPResult → 用户可见文本渲染。
- 副作用：记录审计日志（audit.log）；写 scores.json（工具得分）。

【cl.py — 通道常量与管理员身份（3140B，4 个 def/class）】
- is_main_admin(user_id)：管理员判定（int 身份：飞书 open_id 经 crc32
  映射为 402710372；QQ 侧 DEFAULT_ADMIN）。
- load_models_config：读取 models.json（模型/策略配置）。
- get_strategy_config / detect_strategy：按 prompt 检测策略（fast/deep
  等）并取对应配置。
- 副作用：无（纯配置读取）。

【deepseek_web_integration.py — C 端 DeepSeek 网页版通道（25946B，17 def）】
聊天后端首选通道（免费，不消耗付费 API）。职责明细：
- _ensure_initialized：创建带【完整浏览器头】的 aiohttp session
  （Authorization: Bearer <DEEPSEEK_USER_TOKEN> + x-client-platform:web
  + UA + Origin + Referer）。裸 session 会被 WAF 以 HTTP 429 HTML 拦截。
- _create_session：POST /chat_session/create，解析响应必须读
  data.biz_data.chat_session.id（biz_data.id 不存在，是 None——抓包实证）。
- _solve_pow：DeepSeekHashV1 PoW 挑战求解（Node + sha3 wasm 计算
  x-ds-pow-response），缺失返回 40300。
- _chat_once：POST /chat/completion（SSE 流），_parse_sse 解析正文
  + references + message_id。
- _do_call / _do_call_once：会话状态机（session_id + parent_message_id
  续接多轮；use_memory 记忆开关）。
- ask_web_deepseek(prompt, user_id)：对外唯一接口，1.3s 级真实回复
  （已全链路实证）。_log_event 结构化写 web_deepseek.log。
- 副作用：写 ${PROJECT_ROOT}/web_deepseek.log；按 user_id 维护会话状态。

【memory.py — 长期记忆（13922B，29 个 def/class）】
- MemoryRecord：记忆记录模型（to_dict/from_dict/from_legacy 兼容旧格式）。
- Memory 单例：load/save/reload（memory.json）；get/set；
  is_admin / is_admin_mode；add_memory / search_memories（语义检索）/
  forget_memory；_migrate_memories（旧格式迁移）；_apply_decay（时间
  衰减）；_cleanup_forgotten（遗忘清理）。
- KnowledgeDB 单例：knowledge.db（SQLite）；upsert_preference /
  get_preference；add_solution / search_solutions（解决方案库）；
  save_conversation_summary（会话摘要持久化）。
- load_profile：读取 profile.md 人格文件。
- 副作用：读写 memory.json / knowledge.db / profile.md。

【ocr.py — 图片 OCR（2770B，4 个 def/class）】
- OCRResult：识别结果（succeeded 判定）。
- get_ocr：获取 OCR 服务实例。
- recognize_image_url(image_url)：图片消息 → 文字，注入 ai.py 上下文。
- 副作用：消耗 OCR 服务配额。

【open_source.py — 开源/仓库工具（6370B，6 个 def/class）】
- run：subprocess 执行外壳（check=True 默认）。
- update_readme_structure / check_large_files / check_sensitive：
  仓库健康检查。
- generate_commit_message：AI 生成 commit message。
- main：CLI 入口。
- 副作用：可执行 git 命令（只读检查为主）。

【pi_integration.py — Pi 模式集成（10686B，11 个 def/class）】
- set_mode / get_mode：切换后端模式（api / pi）。
- ask_pi：整链路转发给 Pi 后端（pi_backend/ RPC JSONL 子进程协议），
  绕过本地 API。
- _register_echo_tools / make_wrapper：把 AUP 工具桥接到 Pi 工具后端。
- _ensure_initialized / _close_internal / close_pi_backend：生命周期。
- 副作用：get_mode()=="pi" 时启动子进程（RPC 端口监听）。

【xmkj_to_html.py — XMKJ 转 HTML（9308B，13 个 def/class）】
- read_text：读 XMKJ.txt。
- split_blocks：按结构切块；has_tree_char 判定树形块；
  block_is_header / block_is_tree / block_is_bullet：块类型识别。
- render_header / render_tree / render_bullets / render_plain：
  各类型渲染器。
- build_page(sections, timestamp)：组装 HTML 页面。
- resolve_output：输出路径解析。main：CLI。
- 副作用：生成 HTML 文件。

【patch_ai.py — 历史补丁脚本（3592B，0 def，无函数）】
一次性给 ai.py 打入 EHIR 漏斗补丁的历史脚本。当前 ai.py 已含对应逻辑，
本文件保留但无活跃引用，修改 ai.py 时不得依赖它。

【prod_smoke.py — 生产冒烟（2141B，1 个 def）】
- llm(text)：生产环境 import 兼容性冒烟测试（import plugins.core_utils.
  lexicon + EHIR 快速单测；不 import ai.py 避免重依赖）。
- 用途：部署后验证 venv310 环境可用。

【memory.json — 记忆数据（2208B）】
Memory 单例的持久化文件。包含长期记忆记录（含时间戳、衰减权重）。
人类/AI 修改记忆统一走 memory.py 接口，不直接编辑本文件。

【knowledge.db — 知识库 SQLite（24576B）】
KnowledgeDB 的持久化数据库（偏好表 + 解决方案表 + 会话摘要表）。
工具 query_knowledge / recall_conversation 的检索对象。

【models.json — 模型策略配置（2230B）】
模型名/API key/策略（fast、deep 等）的映射配置。cl.load_models_config
读取。models.json.example 为模板。

【checkin_state.json — 签到状态（156B）】
签到功能的日期状态持久化（plugins/core_utils/checkin.py 读写）。

【scores.json — 工具得分（679B）】
工具成功率/偏好得分的持久化（plugins/core_utils/scores.py 读写，
EHIR 工具选择排序依据）。

【backup_manifest.json / backup_manifest.meta.json — 备份清单】
backup_manifest.json（2849B）：XMKJ 相关 AI 解析结果的缓存清单；
backup_manifest.meta.json（40B）：清单元信息（版本/时间戳）。

【audit.log — 审计日志（41167B）】
admin.py 与 security.py log_audit 写入的工具调用审计记录
（工具名/参数/调用者/结果）。红线核查时的第一证据源。

【feishu_runtime.log — 飞书运行日志（2679B）】
NoneBot 飞书适配器运行日志（连接、事件分发）。

【episodic.log — 情节记忆日志（0B）】
plugins/core_utils/episodic.py 的情节记忆写入文件（当前空/由插件管理）。

【web_deepseek.log — C 端通道日志（13702B）】
deepseek_web_integration._log_event 的结构化日志
（事件/用户/状态/耗时/原因）。C 端故障排查第一证据源。

【profile.md — 人格文件（628B）】
AI 人格/角色设定（profile.md.example 为模板）。memory.load_profile 读取。

【requirements.txt — 依赖清单（3259B）】
venv310 环境的 pip 依赖（nonebot2、feishu 适配器、aiohttp、jieba 等）。

【.env — 运行配置（1359B，只由人类修改）】
- DEEPSEEK_USER_TOKEN：C 端 DeepSeek 网页版 userToken（免费通道凭证）。
- FEISHU_ADMIN_OPEN_ID：飞书主管理员 open_id（crc32 → 402710372）。
- ONEBOT_WS_URLS：已注释（QQ 通道关闭；取消注释+重启才恢复 OneBot）。
- HOST=127.0.0.1 / PORT=8082：NoneBot 监听地址。
改 .env 后必须重启 dz.py 生效。

【.env.example — 配置模板（172B）】
新环境复制为 .env 的模板（不包含真实 token）。

【.gitignore — git 忽略规则（370B）】
忽略 venv310 / __pycache__ / downloads / *.log 等。

【LICENSE — 许可证（17984B）】
MIT 许可证全文。

【README.md — 历史总览（25314B）】
项目早期文档（历史演进记录）。当前以 PROJECT_GUIDE.md 为准。

【PROJECT_BIBLE.md — 人类级历史档案（64160B）】
人类开发者视角的完整历史档案（含早期架构决策）。新模型非必要不读，
遇历史问题可查。

【PROJECT_GUIDE.md — 当前权威交接文档（本文件在 WSL 内的落点）】
唯一面向新接手 AI 的权威文档。D:\项目\PROJECT_GUIDE3.txt 为最新版
（v3 终极扩容版），本文件应与其同步。

【FEISHU_PERMISSION_CHECKLIST.md — 飞书权限清单（4134B）】
飞书应用后台需开通的权限/事件清单（含已验证项）。

【feishu_deliverables_copy.md — 交付物存档（4405B）】
飞书交付物说明的存档副本。

【XMKJ.txt — 数据源文本（27154B）】
XMKJ 业务数据源（明文文本），backup_manifest.py 与 xmkj_to_html.py 的
输入对象。

【backups/ — 备份目录（空）】
预留目录。当前备份策略已改为「.bak 全清」（2026-10-24 起，全库 .bak*
物理删除，项目内 .bak 计数为 0）。

【downloads/ — 下载产物（含 10-24/183928.mp4，173398374B）】
宏工具 download_video_by_url 的视频落盘目录。路径规则：
downloads/<月-日>/<HHMMSS>.mp4。实测产物 183928.mp4 为 166MB 真实
MP4（宏工具闭环验证物）。

────────────────────────────────────────────────────────────
2. 基建层目录（协议 / 注册 / 配置 / 防御）
────────────────────────────────────────────────────────────

【aup_core/ — AUP 统一协议层（Agent Unified Protocol）】

- aup_core/__init__.py（883B）：协议层包初始化（导出 AUPResult、
  AUPError、aup_tool）。
- aup_core/contract.py（4978B，10 个 def/class）：
  AUPError（协议异常基类，to_result 转 AUPResult）；
  AUPResult（统一响应结构：is_ok / to_dict / to_json / from_dict；
  字段含 status、error_code、data、fallback_chain）；
  validate_result（结果结构校验）；is_aup_result（类型判定）。
  全部工具返回值必须为 AUPResult，非 AUP 返回一律视为协议违规。
- aup_core/decorators.py（9937B，10 个 def/class）：
  aup_tool 装饰器（name/description/category/is_danger/params_schema/
  fallback_chain）：包装后自动做入参类型校验（validate_args +
  _coerce_kwargs_types）、出参包装（coerce_result → AUPResult）；
  元数据注册 func._aup_meta；get_aup_meta / is_aup_decorated 查询；
  文件内附带 execute_shell / aup_run_bash_script 的 AUP 示范实现。
  硬约束：任何工具必须带 @aup_tool，未过 AUP 一律拒绝注册与调用。

【import_center/ — 导入引导与依赖容器】

- import_center/__init__.py（455B）：包入口。
- import_center/bootstrap.py（2837B，5 个 def/class）：
  bootstrap()：启动时将项目根加入 sys.path，保证顶层导入
  （from aup_core ... / from config_center ...）可用；
  _attr_factory / _call_factory：惰性属性与惰性调用工厂
  （ModuleNotFoundError 时自动重试导入）。
- import_center/manager.py（2668B，8 个 def/class）：
  ServiceContainer（服务容器）：register / get / has /
  __contains__；get_service / register_service 全局服务注册表
  （惰性 factory 支持）。

【config_center/ — 中央配置中心】

- config_center/__init__.py（1318B，1 个 def）：
  get_config(key, default=None)：统一配置读取网关（读 settings 模块）。
- config_center/settings.py（8698B，0 def，纯常量）：
  集中定义业务常量：拟人开关（PERSONA）、活跃时段（ACTIVE_HOURS）、
  群聊冷却（GROUP_COOLDOWN_SECONDS / REPLY_PROBABILITY）、路径常量
  （ROBOT_ROOT / DOWNLOAD_DIR / CRAWLER_SRC / COOKIE_SRC）、消息截断
  阈值、工具超时等。修改配置只改本文件，禁止散落各处。
  副作用：无（纯数据模块，import 即生效）。

【tool_center/ — AUP 工具注册中心（SQLite 持久化）】

- tool_center/__init__.py（76B）：包入口（空）。
- tool_center/db_manager.py（4754B，8 个 def/class）：
  _connect（SQLite 连接）；init_db（建表：tools 表字段 name/
  description/category/schema/is_danger/is_active/module_path/
  func_name/created_at）；upsert_tool（注册 upsert）；set_active
  （工具开关）；fetch_all / fetch_by_name / fetch_by_category
  （查询，active_only 过滤）；count（统计）。
- tool_center/registry_manager.py（10986B，22 个 def/class）：
  register_tool（AUP 门禁 + SQLite upsert，仅显式调用时执行——启动
  只 load_all_plugins 导入不重注册，因此手动隐藏的 is_active=0 不会被
  覆盖）；
  get_ai_visible_schemas（只返回 is_active=1 工具的 OpenAI schema，
  渐进式披露：隐藏执行细节）；get_ai_visible / get_all_schemas /
  get_tool_info；toggle_tool_status（开关）；
  CompatView（兼容视图：items/get/keys/__contains__/__getitem__/
  __len__/__iter__，active_only 过滤）；
  build_compat_view；_resolve_func（按 module_path + func_name 解析
  函数）；is_aup_compliant / read_aup_meta（AUP 合规校验）。
- tool_center/migrate_tools.py（2667B，2 个 def/class）：
  一次性迁移脚本（从旧注册表迁移到新库），main 为 CLI。
- tool_center/tool_center.db（49152B）：注册中心数据库。
  当前 AI 可见工具 24 个（全部带 module_path）；下载宏工具
  download_video_by_url（tool_id=32，is_active=1）；中间爬虫工具
  crawl_video_link / crawl_custom_by_id / crawler_probe /
  crawler_dom_hook / crawler_run_node 全部 is_active=0 隐藏。
  查询：venv310/bin/python -c "import tool_center.registry_manager as r;
  print(r.get_all_names(True))"

【defense_center/ — 主动防御中枢（对 AI 静默的底层安全机制）】

- defense_center/__init__.py（1113B）：包入口（导出版本/报警通道）。
- defense_center/policies.py（6932B，7 个 def/class）：
  TriggerRule（触发规则模型：rule_id/pattern/level/actions）；
  SecurityEvent（安全事件模型）；
  get_rule / match_line / extract_ip（规则匹配与 IP 提取）；
  describe_protocol / describe_actions（三级安全协议描述：
  L1 观察 / L2 提示 / L3 处置，代码写死的律法库）。
- defense_center/watchdog.py（8201B，10 个 def/class）：
  read_cpu_percent / read_mem_percent（系统负载读取）；
  read_new_lines（增量读日志尾部）；scan_once（单轮扫描：CPU/内存/
  日志特征匹配 policies）；
  Watchdog 类：_loop（30s 周期轮询）/ start / stop /
  default_watchdog（全局单例）。默认 DRY-RUN（只报不处置）。
- defense_center/enforcer.py（11224B，22 个 def/class）：
  SecurityAction / DefenseAudit（动作与审计记录模型）；
  _exec_via_admin（经 admin 执行工具）；_build_action_impls
  （动作实现表：ban_ip / kill_suspect / clear_port / cut_network /
  restart_service——DRY-RUN 时为 _stub 占位）；
  ai_intervene（安全事件 → AI 干预）；QQNotifier（报警通知器，
  attach 绑定发送器）；Enforcer.handle（事件 → 动作列表执行 + 通知）；
  manual_trigger（人工触发规则处置）。
- defense_center/defense_audit.log（4213B）：防御处置审计日志。
  报警通道：飞书 admin open_id（dz.py on_startup 注入）。

【scripts/ — 运维脚本】

- scripts/stop_all.sh（1432B）：停止机器人进程（杀 dz.py 相关进程）。
  已移除 WSL /root/Napcat 引用（Napcat 在 Windows 宿主机 C:\Napcat，
  跨系统杀进程只能人类操作：powershell Stop-Process -Name
  NapCatWinBootMain）。
- scripts/switch_account.sh（2155B）：切换账号（C 端 token 切换/重启）。
  同样无 Napcat 引用。

【pi_backend/ — Pi 模式后端（RPC JSONL 子进程协议，可选模式）】

- pi_backend/__init__.py（152B）：包入口。
- pi_backend/core/pi_backend.py（9067B，38 个 def/class）：
  PIBackend 类——通过 JSONL stdin/stdout 与 Pi 子进程通信的客户端：
  prompt / steer / follow_up / abort / clear_queue / new_session /
  get_state / get_messages / set_model / cycle_model /
  get_available_models / set_thinking_level / cycle_thinking_level /
  get_available_thinking_levels / set_steering_mode /
  set_follow_up_mode / compact / set_auto_compaction / set_auto_retry /
  abort_retry / bash / abort_bash / get_session_stats / export_html /
  switch_session / fork / clone / get_fork_messages / get_entries /
  get_tree / get_last_assistant_text / set_session_name /
  get_commands；_send_command 统一发送 RPC 指令。
- pi_backend/core/pi_process.py（2385B，6 个 def/class）：
  PIProcess：子进程生命周期管理（build_process / read_line /
  write_line / close_process）。
- pi_backend/core/pi_tool_backend.py（8885B，19 个 def/class）：
  PIToolBackend：工具桥接服务器（asyncio TCP server）：register_tool
  （注册工具回调）/ on_execute_tool（收到工具执行请求 → 执行 → 流式
  回送 ToolResultChunk / ToolEndFlag）/ run_server / _execute_tool /
  _next_tool_output / _send / _recv_line / _recv_tool_request /
  _cleanup_connection / _close_server。
- pi_backend/models/（pydantic 事件模型层）：
  base_event.py：BaseEvent 基类；
  _internal/tool_end_flag.py：ToolEndFlag（工具结束标记）；
  _internal/tool_result_chunk.py：ToolResultChunk（工具结果分片）；
  tool_events/：BaseToolEvent / ToolExecution / ToolExecutionEnd /
  ToolResult（工具事件模型）；
  rpc_events/base_rpc_event.py：BaseRPCEvent；
  rpc_events/client_events/：BaseCommand + 30 个命令类（AbortCommand、
  AbortBashCommand、AbortRetryCommand、BashCommand、ClearQueueCommand、
  CloneCommand、CompactCommand、CycleModelCommand、
  CycleThinkingLevelCommand、ExportHtmlCommand、FollowUpCommand、
  ForkCommand、GetAvailableModelsCommand、
  GetAvailableThinkingLevelsCommand、GetCommandsCommand、
  GetEntriesCommand、GetForkMessagesCommand、
  GetLastAssistantTextCommand、GetMessagesCommand、
  GetSessionStatsCommand、GetStateCommand、GetTreeCommand、
  NewSessionCommand、PromptCommand、SetAutoCompactionCommand、
  SetAutoRetryCommand、SetFollowUpModeCommand、SetModelCommand、
  SetSessionNameCommand、SetSteeringModeCommand、
  SetThinkingLevelCommand、SteerCommand、SwitchSessionCommand）；
  rpc_events/server_events/base_response.py：BaseResponse；
  rpc_events/server_events/responses/：BaseResponse 的 33 个响应模型
  （与命令一一对应：AbortResponse、BashResponse、PromptResponse、
  StateResponse、SteerResponse 等）。
  说明：pi_backend 为可选模式（pi_integration.set_mode("pi") 才激活），
  默认 api 模式不加载子进程。

────────────────────────────────────────────────────────────
3. 插件层（plugins/ —— 唯一扩展点，动态加载）
────────────────────────────────────────────────────────────

【plugins/__init__.py — 动态插件加载器（1778B，1 个 def）】
- load_all_plugins()：扫描 plugins/ 下含 __init__.py 的子包，按字典序
  importlib 导入；插件导入时其 @aup_tool 装饰的工具自动注册元数据。
  主程序（dz.py）零硬编码 import，只调用 load_all_plugins() 一次。
  当前加载结果：core_utils, crawler, system_ops, tasks（四件套）。
- 新增能力 = 在 plugins/ 下新建包 + @aup_tool 注册，无需改主引擎。

────────────────────────────────────────────────────────────
3.1 plugins/core_utils/ — 全局辅助插件（原 bot_utils 迁移而来）
────────────────────────────────────────────────────────────

【plugins/core_utils/__init__.py（945B）】
core_utils 插件包入口：注册通用工具（execute_shell 系列、m3u8 下载、
签到、媒体转换、管理员增删等），动态加载器导入本包时生效。

【plugins/core_utils/security.py — 权限校验（3086B，3 个 def）】
- check_permission(tool_name, sender_id, default_admin)：工具权限判定，
  返回 (允许, 原因)。主管理员（int 身份 402710372 / QQ DEFAULT_ADMIN）
  全放行；非管理员只放行白名单工具（查询类/爬虫类）。
- _sanitize_args(args)：参数清洗（防注入特征）。
- log_audit(tool_name, args, sender_id, result)：审计落 audit.log。
- 副作用：写 audit.log。

【plugins/core_utils/lexicon.py — EHIR 词库（13342B，13 个 def/class）】
EHIR 意图分类的 L2 层核心。
- LexiconDB（lexicon.db SQLite）：learn（学习：模型判 tool 后提取
  动作词入库）；match（文本 → 意图 + 动作词）；stats（词库统计）；
  should_llm_route（是否需升 L3 大模型）；rule_match（L1 静态正则：
  /^(打开|读取|执行|爬取|下载|搜索|...)/ 等动作前缀 → tool 高置信）；
  extract_action_word（提取动作词）。
- FunnelRoute（label/reason/confidence/layer/word/llm_called）：
  funnel_route(text, llm_classify, lexicon)：三层漏斗路由统一出口——
  L1 rule_match 命中即返回；L2 lexicon 命中即返回；否则调 llm_classify
  （L3）并返回。整个意图判断链路由此函数驱动。
- 副作用：读写 lexicon.db；L3 调用消耗 API。

【plugins/core_utils/episodic.py — 情节记忆（4208B，8 个 def）】
- count_today_turns / append_messages / maybe_flush：每日对话轮次计数
  与情节沉淀（达到阈值自动 flush 进长期记忆）。
- recall(keyword, limit)：情节检索。
- clear_expired：过期清理。_load_state / _save_state 持久化。
- 副作用：写 episodic 状态（内存 + 文件）。

【plugins/core_utils/scores.py — 工具得分（2933B，5 个 def）】
- add_score(tool_name)：调用成功后加分（含时间衰减 _decay）。
- get_scores(tool_names)：查询得分，EHIR/工具选择排序依据。
- _load / _save：scores.json 持久化。

【plugins/core_utils/media.py — 媒体处理（3241B，3 个 def）】
- _find_latest_mp4：找 downloads 最新 mp4。
- convert_latest_mp4_to_mp3：mp4 → mp3（ffmpeg）。
- clean_media(scope)：清理媒体缓存（both = 视频+音频）。

【plugins/core_utils/checkin.py — 签到（5542B，8 个 def）】
- get_today / load_state / save_state / has_checked_in_today：
  签到状态管理（checkin_state.json）。
- execute_checkin / auto_checkin / run_checkin：执行签到（外部接口）。
- 当前状态：dz.py 启动日志显示「签到功能已停用，跳过」。

【plugins/core_utils/admin_gh.py — 管理员管理（1303B，3 个 def）】
- add_admin / del_admin / list_admin：仅主管理员可增删查管理员。

【plugins/core_utils/aup_tools.py — AUP 标杆工具（8075B，8 个 def）】
- aup_execute_shell(cmd, timeout)：@aup_tool 封装的 shell 执行（is_
  danger）：优先 subprocess exec（无 shell 注入面），异常降级 run/shell
  通道（_run_via_subprocess_exec / _run_via_subprocess_run /
  _run_via_subprocess_shell 三通道）。
- aup_run_bash_script(script_content, timeout)：Bash 脚本执行（_fix_
  missing_newlines 修复换行丢失后交给 bash -c）。
- _sanitize_output：工具内部输出清洗。
- 这些工具是 AUP 协议的标准范例：新工具照此模式写。

【plugins/core_utils/downloader.py — m3u8 下载器（10456B，1 个 def）】
- download_m3u8(url, referer, output_name)：通用 m3u8 下载：
  curl/openssl 拉分片 + ffmpeg 合并；加密（key）检测 + yt-dlp/ffmpeg
  降级链。宏工具 download_video_by_url 的底层复用对象之一。
- 副作用：写 downloads/。

【plugins/core_utils/feishu_bridge.py — 飞书兼容层（15541B，27 def）】
QQ(onebot) 消息模型与飞书模型的兼容适配层。
- _oid_hash / register_open_id / resolve_open_id：飞书 open_id ↔ int
  uid 双向映射（crc32）。
- get_tenant_token：飞书 tenant_access_token 获取。
- ensure_media_server / _download_resource / _save_media：媒体资源
  下载与本地保存。
- Seg / FeishuEventView：飞书事件 → 统一分段消息模型
  （build / _convert_message / message / get_plaintext）。
- FeishuBotView：飞书 Bot 发送封装（send / send_private_msg /
  send_msg / call_api / _normalize_outgoing / _upload_image）。
- extract_plain_text / admin_open_id：工具函数。
- 副作用：写 media 缓存；调飞书开放平台 API。

【plugins/core_utils/backup_manifest.py — 备份清单（4902B，7 def）】
- refresh_manifest / load_manifest / is_manifest_stale：
  XMKJ 解析结果的清单维护（_read_xmkj / _get_xmkj_mtime /
  _extract_json_array / _parse_xmkj_via_ai）。
- 副作用：写 backup_manifest.json。

【plugins/core_utils/security_scanner.py — 安全扫描（1296B，1 个 def）】
- sanitize_crawler_output(text)：爬虫输出的 URL/m3u8 脱敏（防泄漏到
  聊天通道）。

【plugins/core_utils/tool_schema_builder/ — OpenAI schema 构建器】
- core/tool_schema_builder.py（2355B，10 个 def/class）：
  ToolSchemaBuilder：参数 schema 构建（parameters_dict /
  required_parameters / set_parameter / set_required /
  build_parameters / build_function / build_tool / to_schema）。
- models/schemas/：BaseSchema 基类 + 各类型 schema 类
  （ArraySchema / BooleanSchema / FunctionSchema / IntegerSchema /
  NullSchema / NumberSchema / ObjectSchema / StringSchema /
  ToolSchema）+ 组合 schema（AllOfSchema / AnyOfSchema /
  OneOfSchema / BaseMultipleSchemas）。
- traits/to_schema_able.py：ToSchemaAble 协议接口。
- 用途：把 params_desc 字典转成 OpenAI function calling 标准 schema。

────────────────────────────────────────────────────────────
3.2 plugins/crawler/ — 爬虫微内核（核心业务插件）
────────────────────────────────────────────────────────────

【plugins/crawler/__init__.py — 爬虫插件入口（12767B，9 个 def）】
对外唯一业务工具 = 宏工具。职责明细：
- _resolve_site_url(raw)：别名/缩写 → 完整 URL。输入形如 "tz 92285" /
  "tz#92285" → 读 site_map.json 拼出 https://tob.tobvic.com:52000/
  video/detail/92285；完整 URL 原样返回。
- _clean_workspace：清理 crawler workspace 临时文件。
- _run_node(js_path, args, timeout_ms)：Node 子进程执行封装
  （cwd=插件根，捕获 stdout/stderr）。
- crawl_video_link(url)：通用抓包工具（dom_hook → probe 降级漏斗）——
  当前 is_active=0 隐藏（保留实现，内部降级用）。
- crawler_probe / crawler_run_node / crawler_dom_hook：中间节点工具
  ——is_active=0 隐藏。
- crawl_custom_by_id(url)：定制解析器入口（tobvic 域名）——is_active=0
  隐藏。实现：_resolve_site_url → _run_node(fetch_by_id.js) → 返回
  {success,title,id,note}（脱敏，m3u8 仅本地日志）。
- download_video_by_url(url)：【宏工具，AI 唯一可见爬虫入口】：
  URL/别名 → _resolve_site_url → video_crawler.crawl_video（fetch_by_id
  .js 拉流 + ffmpeg 转码）→ 返回 {"success":true,"msg":"视频已下载",
  "path":"downloads/<月-日>/<HHMMSS>.mp4"}。黑盒一条龙：中间过程对
  AI 透明，返回不含 URL/m3u8（防泄漏）。tool_id=32，is_active=1。
- 副作用：写 downloads/；写 detail_*.json 临时文件（0B 占位）。

【plugins/crawler/video_crawler.py — 下载闭环（4383B，3 个 def）】
- _run_node(script_path, args, cwd, timeout)：Node 执行（显式 cwd）。
- _refresh_cookie(work_dir)：cookie 刷新（diag.js）。
- crawl_video(video_url)：核心下载链路：
  常量 CRAWLER_SRC=${PROJECT_ROOT}/plugins/crawler（JS 目录）、
  DOWNLOAD_DIR=${PROJECT_ROOT}/downloads（产物目录）、
  COOKIE_SRC=/root/cookies.json（cookie 源，人类维护）；
  fetch_by_id.js 拿 m3u8 → ffmpeg 转码 → 落盘 downloads/<月-日>/
  <HHMMSS>.mp4 → 返回路径字典。实测闭环：183928.mp4（166MB）。

【plugins/crawler/robust_crawler.py — 多级降级漏斗（8173B，7 def）】
- is_video_url / _has_m3u8_signature：URL 与 m3u8 特征判定。
- _run_node / _parse_dom_hook / _parse_probe：dom_hook/probe 脚本
  执行与解析。
- _pick_valid_video(candidates, evidence)：多候选择优。
- robust_video_crawl(url)：完整漏斗（dom_hook → probe → 结论）。

【plugins/crawler/site_map.json — 别名字典（100B）】
{"tz":"https://tob.tobvic.com:52000/video/detail/","yt":
"https://www.youtube.com/watch?v="}。
dz.py _preprocess_alias 与 _resolve_site_url 的查询对象。
新增站点缩写在此追加（人类维护，格式：缩写 → 基础 URL）。

【plugins/crawler/crawler_tools/ — Node 调试工具集】
- package.json（366B）/ package-lock.json（19853B）：Node 依赖
  （jsdom / ffmpeg-static 等）。
- probe.js（3034B）：通用站点探测脚本（查 m3u8/mp4 特征）。
- dom_hook.js（3967B）：DOM 钩子抓包脚本（SPA 站）。
- run.js（1001B）：Node 运行入口（调试用）。
- workspace/：Node 工作目录（临时文件）。

【plugins/crawler/ 根下 Node 脚本】
- fetch_by_id.js（4234B）：tobvic 详情 API 抓取（内置 XOR 解密逻辑），
  输出视频 m3u8。宏工具拉流第一步。
- download_video.js（4969B）：m3u8 分片下载 + 解密 + 合并。
- diag.js（5473B）：诊断/cookie 刷新脚本。
- detail_92285.json（0B）：临时详情文件（占位，可被覆盖）。

────────────────────────────────────────────────────────────
3.3 plugins/system_ops/ — 系统与文件插件
────────────────────────────────────────────────────────────

【plugins/system_ops/__init__.py — 插件入口（5775B，8 个 def）】
- backup_project：项目备份（打包）。
- read_file(filepath, max_len=12000)：读文件（默认 12KB 上限，
  retries=2 重试）。
- write_file(filepath, content)：写文件。
- delete_line / replace_line / insert_line：行级编辑。
- system_status：系统状态（CPU/内存/磁盘/运行时间）。
- ping_host(host)：网络连通测试。
- 这些工具经 @aup_tool 注册为 AI 可见工具（读文件/写文件/行编辑为
  文件操作白名单）。

【plugins/system_ops/wj.py — 文件操作实现（7772B，7 个 def）】
- _safe_path(filepath)：路径安全校验（防越权读敏感文件）。
- backup_project / read_file / write_file / delete_line /
  replace_line / insert_line：与 __init__.py 对应实现。
- 副作用：写文件操作会真实改动磁盘（AI 工具链高危面之一）。

【plugins/system_ops/xt.py — 系统状态（4452B，4 个 def）】
- system_status：CPU/内存/磁盘/uptime。
- execute_shell(cmd)：shell 执行（is_danger）。
- _fix_missing_newlines：修复换行丢失。
- ping_host：ping 测试。

【plugins/system_ops/script_runner.py — Bash 脚本执行（2582B，2 def）】
- _fix_missing_newlines(script)：把 "\n" 字面量修复为真实换行
  （AI 生成脚本的经典缺陷——参数解码层修复）。
- run_bash_script(script_content, timeout=30)：bash -c 执行。

────────────────────────────────────────────────────────────
3.4 plugins/tasks/ — 重型任务插件
────────────────────────────────────────────────────────────

【plugins/tasks/__init__.py — 插件入口（1340B）】
tasks 插件包入口（神授/大型任务/任务队列注册）。

【plugins/tasks/divine_task.py — 神授模式（18352B，15 个 def/class）】
自主循环任务引擎：
- _call_model / _call_model_once：模型调用（按策略）。
- _estimate_tokens / _ctx_size / _compress_context：token 预算与
  上下文压缩。
- _check_limits：步数/时间/成本限制。
- _run_loop：自主循环（目标 → 工具调用 → 评估 → 继续/结束）。
- run_divine_task / resume_divine_task / is_pending / clear_pending：
  任务生命周期（挂起可恢复）。
- _finalize_with_summary / _fallback_summary：结束总结。

【plugins/tasks/large_task.py — 大型任务五阶段（16551B，10 def）】
- _plan：任务 → 五阶段计划（理解/拆解/执行/验证/总结）。
- _execute_one：单子任务执行（工具调用）。
- _summarize / _fallback_summary：汇总。
- _build_tool_list：构建工具列表。
- run_large_task：入口。

【plugins/tasks/task_queue.py — 任务队列（6469B，7 个 def/class）】
- split_tasks：全角「＊」分隔多任务。
- map_tasks_to_tools：任务 → 工具映射（按得分）。
- sort_by_scores：按工具得分排序。
- TaskQueue 单例：submit（排队执行 + _notify 通知）。

────────────────────────────────────────────────────────────
附：全量文件树（深度遍历实测，含字节数）
────────────────────────────────────────────────────────────
.env [1359B]
.env.example [172B]
.gitignore [370B]
FEISHU_PERMISSION_CHECKLIST.md [4134B]
LICENSE [17984B]
PROJECT_BIBLE.md [64160B]
README.md [25314B]
XMKJ.txt [27154B]
admin.py [23592B]
ai.py [73318B]
audit.log [41167B]
aup_core/
  __init__.py [883B]
  contract.py [4978B]
  decorators.py [9937B]
backup_manifest.json [2849B]
backup_manifest.meta.json [40B]
backups/
checkin_state.json [156B]
cl.py [3140B]
config_center/
  __init__.py [1318B]
  settings.py [8698B]
deepseek_web_integration.py [25946B]
defense_center/
  __init__.py [1113B]
  defense_audit.log [4213B]
  enforcer.py [11224B]
  policies.py [6932B]
  watchdog.py [8201B]
downloads/
  10-24/
    183928.mp4 [173398374B]
dz.py [27985B]
episodic.log [0B]
feishu_deliverables_copy.md [4405B]
feishu_runtime.log [2679B]
import_center/
  __init__.py [455B]
  bootstrap.py [2837B]
  manager.py [2668B]
knowledge.db [24576B]
memory.json [2208B]
memory.py [13922B]
models.json [2230B]
models.json.example [672B]
ocr.py [2770B]
open_source.py [6370B]
patch_ai.py [3592B]
pi_backend/
  __init__.py [152B]
  core/
    __init__.py [178B]
    pi_backend.py [9067B]
    pi_process.py [2385B]
    pi_tool_backend.py [8885B]
  models/
    __init__.py [3903B]
    _internal/
      __init__.py [142B]
      tool_end_flag.py [249B]
      tool_result_chunk.py [232B]
    base_event.py [121B]
    rpc_events/
      __init__.py [3685B]
      base_rpc_event.py [103B]
      client_events/
        __init__.py [2584B]
        abort.py [171B]
        abort_bash.py [189B]
        abort_retry.py [193B]
        base_command.py [253B]
        bash.py [370B]
        clear_queue.py [193B]
        clone.py [171B]
        compact.py [312B]
        cycle_model.py [193B]
        cycle_thinking_level.py [227B]
        export_html.py [315B]
        follow_up.py [410B]
        fork.py [271B]
        get_available_models.py [227B]
        get_available_thinking_levels.py [261B]
        get_commands.py [197B]
        get_entries.py [334B]
        get_fork_messages.py [215B]
        get_last_assistant_text.py [237B]
        get_messages.py [197B]
        get_session_stats.py [215B]
        get_state.py [185B]
        get_tree.py [181B]
        new_session.py [321B]
        prompt.py [515B]
        set_auto_compaction.py [320B]
        set_auto_retry.py [300B]
        set_follow_up_mode.py [341B]
        set_model.py [318B]
        set_session_name.py [298B]
        set_steering_mode.py [338B]
        set_thinking_level.py [364B]
        steer.py [395B]
        switch_session.py [299B]
      server_events/
        __init__.py [1867B]
        base_response.py [669B]
        responses/
          __init__.py [2925B]
          abort_bash_response.py [153B]
          abort_response.py [140B]
          abort_retry_response.py [156B]
          bash_response.py [137B]
          clear_queue_response.py [156B]
          clone_response.py [140B]
          compact_response.py [146B]
          cycle_model_response.py [156B]
          cycle_thinking_level_response.py [181B]
          export_html_response.py [156B]
          follow_up_response.py [150B]
          fork_response.py [137B]
          get_available_models_response.py [181B]
          get_available_thinking_levels_response.py [206B]
          get_commands_response.py [159B]
          get_entries_response.py [156B]
          get_fork_messages_response.py [172B]
          get_last_assistant_text_response.py [188B]
          get_messages_response.py [159B]
          get_session_stats_response.py [172B]
          get_tree_response.py [147B]
          new_session_response.py [156B]
          prompt_response.py [143B]
          set_auto_compaction_response.py [178B]
          set_auto_retry_response.py [163B]
          set_follow_up_mode_response.py [173B]
          set_model_response.py [150B]
          set_session_name_response.py [169B]
          set_steering_mode_response.py [172B]
          set_thinking_level_response.py [175B]
          state_response.py [144B]
          steer_response.py [140B]
          switch_session_response.py [165B]
    tool_events/
      __init__.py [274B]
      base_tool_event.py [150B]
      tool_execution.py [299B]
      tool_execution_end.py [257B]
      tool_result.py [190B]
pi_integration.py [10686B]
plugins/
  __init__.py [1778B]
  core_utils/
    __init__.py [945B]
    admin_gh.py [1303B]
    aup_tools.py [8075B]
    backup_manifest.py [4902B]
    checkin.py [5542B]
    downloader.py [10456B]
    episodic.py [4208B]
    feishu_bridge.py [15541B]
    lexicon.db [16384B]
    lexicon.py [13342B]
    media.py [3241B]
    scores.py [2933B]
    security.py [3086B]
    security_scanner.py [1296B]
    tool_schema_builder/
      __init__.py [1271B]
      core/
        __init__.py [143B]
        tool_schema_builder.py [2355B]
      models/
        __init__.py [577B]
        schemas/
          __init__.py [1150B]
          all_of_schema.py [267B]
          any_of_schema.py [267B]
          array_schema.py [1178B]
          base_multiple_schema.py [661B]
          base_schema.py [484B]
          boolean_schema.py [363B]
          function_schema.py [612B]
          integer_schema.py [949B]
          null_schema.py [237B]
          number_schema.py [969B]
          object_schema.py [1018B]
          one_of_schema.py [267B]
          string_schema.py [592B]
          tool_schema.py [534B]
      traits/
        __init__.py [72B]
        to_schema_able.py [156B]
  crawler/
    __init__.py [12767B]
    crawler_tools/
      dom_hook.js [3967B]
      package-lock.json [19853B]
      package.json [366B]
      probe.js [3034B]
      run.js [1001B]
      workspace/
    detail_92285.json [0B]
    diag.js [5473B]
    download_video.js [4969B]
    fetch_by_id.js [4234B]
    robust_crawler.py [8173B]
    site_map.json [100B]
    video_crawler.py [4383B]
  system_ops/
    __init__.py [5775B]
    script_runner.py [2582B]
    wj.py [7772B]
    xt.py [4452B]
  tasks/
    __init__.py [1340B]
    divine_task.py [18352B]
    large_task.py [16551B]
    task_queue.py [6469B]
prod_smoke.py [2141B]
profile.md [628B]
profile.md.example [174B]
requirements.txt [3259B]
scores.json [679B]
scripts/
  stop_all.sh [1432B]
  switch_account.sh [2155B]
tool_center/
  __init__.py [76B]
  db_manager.py [4754B]
  migrate_tools.py [2667B]
  registry_manager.py [10986B]
  tool_center.db [49152B]
web_deepseek.log [13702B]
xmkj_to_html.py [9308B]

===== PY FUNCTION INDEX =====

### admin.py (16 defs/classes)
   async def set_group_chat_enabled(enabled: bool) -> str:
   async def get_group_chat_status() -> str:
   async def agent_manager(
   async def get_xmkj() -> str:
   async def query_knowledge(keyword: str) -> str:
   async def recall_conversation(keyword: str) -> str:
   async def clean_media_tool(scope: str = "both") -> str:
   async def checkin_tool() -> str:
   def get_help_text() -> str:
   def build_tool_schema(tool_name: str, func, params_desc: dict) -> dict:
   def _get_tool_default_desc(tool_name: str) -> str:
   async def _call_tool_router(user_input: str, sender_id: str) -> dict | None:
   async def _execute_admin_command(cmd: str, sender_id: str, chat_type: str = "private", group_id: str = "") -> 
   async def handle_admin_command(raw_input, sender_id, force_tool_route=False, chat_type="private", group_id="")
   def _render_aup_result(res: "AUPResult") -> str:
   async def execute_tool_directly(

### ai.py (40 defs/classes)
   class TodoItem:
   class TodoListManager:
   def __init__(self, session_id: str):
   def load(self) -> List[TodoItem]:
   def save(self, items: List[TodoItem]) -> None:
   def add(self, description: str, parent_id: str = None) -> TodoItem:
   def get_pending(self) -> Optional[TodoItem]:
   def mark_running(self, item_id: str) -> bool:
   def mark_done(self, item_id: str, result: str = "") -> bool:
   def mark_failed(self, item_id: str, result: str = "") -> bool:
   def list_items(self) -> str:
   def get_conv_key(user_id: str, chat_type: str, group_id: str = "") -> str:
   def refresh_image_context(key: str) -> None:
   def set_ocr_context(key: str, ocr_text: str) -> None:
   def consume_image_context(key: str) -> tuple[str, bool]:
   def count_tokens(text: str) -> int:
   def count_messages_tokens(messages: list) -> int:
   def should_compress(key: str) -> bool:
   async def _call_ai_no_compress(user_id: str, prompt: str, system_prompt: str = None) -> str:
   async def compress_history(key: str) -> None:
   def _sanitize_for_chat(history: list) -> list:
   def _strip_code_blocks(text):
   def sanitize_output(text) -> str:
   async def safe_api_request(
   async def _get_current_key_with_list(api_keys: list, start_index: int = 0) -> str:
   async def _try_backup(messages, model, timeout, tools, tool_choice, retries):
   async def _get_current_key() -> str:
   async def run_sub_agent(
   async def _detect_intent(prompt: str, context_messages: list = None) -> dict:
   def _learn_tool_word(prompt: str) -> None:
   async def _llm_detect_intent(prompt: str, context_messages: list = None) -> dict:
   def _parse_intent_reply(reply: str) -> dict:
   async def ask_ai(user_id: str, prompt: str, chat_type: str = "private", group_id: str = "") -> str:
   async def get_ai_response(
   async def submit_tool_results(user_id: str, tool_results: list) -> str:
   class TaskContext:
   def __init__(self, user_goal: str):
   def to_prompt(self) -> str:
   async def run_async_task_chain(
   async def _run_fast_chain(user_id: str, goal: str, max_steps: int, chat_type: str) -> str:

### cl.py (4 defs/classes)
   def is_main_admin(user_id) -> bool:
   def load_models_config():
   def get_strategy_config(strategy: str) -> dict | None:
   def detect_strategy(prompt: str) -> str:

### deepseek_web_integration.py (17 defs/classes)
   def _log_event(event: str, user: str, status: str, elapsed: float, reason: str = "") -> None:
   def _reason_of(exc: BaseException) -> str:
   def _get_config():
   def is_enabled() -> bool:
   def get_api_key() -> str:
   def _pow_wasm_path() -> str:
   async def _ensure_initialized() -> None:
   async def _post_json(session: aiohttp.ClientSession, path: str, body: dict,
   async def _solve_pow(session: aiohttp.ClientSession) -> str:
   async def _create_session(session: aiohttp.ClientSession) -> str:
   async def _chat_once(session: aiohttp.ClientSession, session_id: str,
   async def _parse_sse(stream) -> tuple:
   def _format_reply(content: str, references: list) -> str:
   async def ask_web_deepseek(prompt: str, user_id: str = None, timeout_seconds: float = None) -> str:
   async def _do_call(session, user: str, prompt: str, timeout: float,
   async def _do_call_once(session, state, user: str, prompt: str, timeout: float,
   async def close() -> None:

### dz.py (16 defs/classes)
   def _preprocess_alias(text: str) -> str:
   def _patched_json_to_event(cls, json_data):
   async def on_startup():
   async def on_bot_connect(bot: Bot):
   async def on_shutdown():
   async def handle_url(bot, event):
   async def send_segmented_reply(bot, event, reply: str):
   def _ocr_failure_message(results: list[OCRResult]) -> str:
   def _get_user_message_lock(user_id: str) -> asyncio.Lock:
   async def handle_image_segments(bot: Bot, event: MessageEvent, key: str) -> tuple[bool, bool]:
   async def handle_msg(bot, event):
   async def _handle_msg_inner(bot, event):
   async def _handle_msg_locked(bot: Bot, event: MessageEvent):
   async def _divine_notify(msg: str):
   async def _notify(msg: str):
   async def handle_downloadable(downloadable: dict, user_input: str, event, bot, chat_type: str, group_id: str):

### memory.py (29 defs/classes)
   class MemoryRecord:
   def to_dict(self) -> dict:
   def from_dict(cls, d: dict) -> "MemoryRecord":
   def from_legacy(cls, content: str, now: str) -> "MemoryRecord":
   def load_profile() -> str:
   class Memory:
   def __new__(cls):
   def load(self):
   def save(self, data=None):
   def reload(self):
   def get(self, key, default=None):
   def set(self, key, value):
   def is_admin(self, user_id: str) -> bool:
   def is_admin_mode(self) -> bool:
   def _now_str(self) -> str:
   def _migrate_memories(self, raw_list: list) -> list:
   def _apply_decay(self, records: list) -> list:
   def _cleanup_forgotten(self, records: list) -> list:
   def add_memory(self, memory_text: str) -> str:
   def search_memories(self, query: str) -> list[str]:
   def forget_memory(self, keyword: str) -> str:
   class KnowledgeDB:
   def __new__(cls):
   def _init_db(self):
   def upsert_preference(self, key: str, value: str) -> None:
   def get_preference(self, key: str) -> str | None:
   def add_solution(self, problem: str, solution: str) -> None:
   def search_solutions(self, keyword: str, limit: int = 5) -> list[dict]:
   def save_conversation_summary(self, session_id: str, summary: str) -> None:

### ocr.py (4 defs/classes)
   class OCRResult:
   def succeeded(self) -> bool:
   def get_ocr():
   async def recognize_image_url(image_url: str | None, timeout: int = 15, retries: int = 2) -> OCRResult:

### open_source.py (6 defs/classes)
   def run(cmd, check=True):
   def update_readme_structure():
   def check_large_files():
   def check_sensitive():
   async def generate_commit_message():
   async def main():

### patch_ai.py (0 defs/classes)

### pi_integration.py (11 defs/classes)
   def _make_async_wrapper(sync_func):
   async def wrapper(**kwargs):
   def _register_echo_tools(tool_backend: PIToolBackend) -> None:
   def make_wrapper(name, func):
   async def wrapper(**kwargs):
   async def _ensure_initialized(
   async def _close_internal() -> None:
   async def set_mode(mode: str) -> None:
   def get_mode() -> str:
   async def ask_pi(
   async def close_pi_backend() -> None:

### prod_smoke.py (1 defs/classes)
   def llm(text):

### xmkj_to_html.py (13 defs/classes)
   def read_text(path):
   def split_blocks(text):
   def has_tree_char(line):
   def block_is_header(block):
   def block_is_tree(block):
   def block_is_bullet(block):
   def render_header(block):
   def render_tree(block):
   def render_bullets(block):
   def render_plain(block):
   def build_page(sections, timestamp):
   def resolve_output(base):
   def main():

### defense_center/__init__.py (0 defs/classes)

### defense_center/enforcer.py (22 defs/classes)
   class SecurityAction:
   class DefenseAudit:
   def record(entry: dict) -> None:
   async def _exec_via_admin(tool_name: str, args: dict) -> str:
   def _build_action_impls(dry_run: bool):
   def _stub(action, params):
   async def ban_ip(params):
   async def kill_suspect(params):
   async def clear_port(params):
   async def cut_network(params):
   async def restart_service(params):
   async def ai_intervene(event: SecurityEvent) -> str:
   class QQNotifier:
   def __init__(self):
   def attach(self, sender: Callable, admin_qq: int) -> None:
   async def send(self, text: str) -> str:
   class Enforcer:
   def __init__(self, dry_run: Optional[bool] = None):
   async def handle(self, event: SecurityEvent) -> List[SecurityAction]:
   def _params_for(self, event: SecurityEvent, action: str) -> dict:
   async def _notify(self, event: SecurityEvent, kind: str, performed: List[SecurityAction]) -> None:
   async def manual_trigger(rule_id: str, evidence_lines: Optional[List[str]] = None) -> List[SecurityAction]:

### defense_center/policies.py (7 defs/classes)
   class TriggerRule:
   class SecurityEvent:
   def get_rule(rule_id: str) -> Optional[TriggerRule]:
   def match_line(line: str, rule: TriggerRule) -> Optional[re.Match]:
   def extract_ip(line: str, rule: TriggerRule) -> Optional[str]:
   def describe_protocol(level: int) -> str:
   def describe_actions(level: int) -> Tuple[str, ...]:

### defense_center/watchdog.py (10 defs/classes)
   def read_cpu_percent() -> Optional[float]:
   def read_mem_percent() -> Optional[float]:
   def read_new_lines(path: str, max_len: int = MAX_LINE_LEN) -> List[str]:
   def scan_once(
   class Watchdog:
   def __init__(self, interval: float | None = None, log_paths: tuple = DEFAULT_LOG_PATHS,
   async def _loop(self) -> None:
   def start(self) -> asyncio.Task:
   def stop(self) -> None:
   def default_watchdog() -> Watchdog:

### config_center/__init__.py (1 defs/classes)
   def get_config(key: str, default=None):

### config_center/settings.py (0 defs/classes)

### aup_core/__init__.py (0 defs/classes)

### aup_core/contract.py (10 defs/classes)
   class AUPError(Exception):
   def __init__(
   def to_result(self) -> "AUPResult":
   class AUPResult:
   def is_ok(self) -> bool:
   def to_dict(self) -> dict:
   def to_json(self, **kw) -> str:
   def from_dict(cls, d: dict) -> "AUPResult":
   def validate_result(obj: Any) -> List[str]:
   def is_aup_result(obj: Any) -> bool:

### aup_core/decorators.py (10 defs/classes)
   def _type_ok(value: Any, type_name: str) -> bool:
   def validate_args(params_schema: Optional[dict], args: tuple, kwargs: dict) -> List[str]:
   def _coerce_kwargs_types(params_schema: Optional[dict], kwargs: dict) -> dict:
   def coerce_result(raw: Any, tool_name: str, chain: List[str]) -> AUPResult:
   def aup_tool(
   async def execute_shell(cmd: str) -> ...:
   def deco(fn: Callable) -> Callable:
   async def wrapper(*args: Any, **kwargs: Any) -> AUPResult:
   def get_aup_meta(obj: Any) -> Optional[dict]:
   def is_aup_decorated(obj: Any) -> bool:

### plugins/__init__.py (1 defs/classes)
   def load_all_plugins() -> list:

### plugins/system_ops/__init__.py (8 defs/classes)
   async def backup_project() -> str:
   async def read_file(filepath: str, max_len: int = 12000) -> str:
   async def write_file(filepath: str, content: str) -> str:
   async def delete_line(filepath: str, line_num: int) -> str:
   async def replace_line(filepath: str, line_num: int, new_content: str) -> str:
   async def insert_line(filepath: str, line_num: int, new_content: str) -> str:
   async def system_status() -> str:
   async def ping_host(host: str) -> str:

### plugins/system_ops/script_runner.py (2 defs/classes)
   def _fix_missing_newlines(script: str) -> str:
   async def run_bash_script(script_content: str, timeout: int = 30) -> str:

### plugins/system_ops/wj.py (7 defs/classes)
   def _safe_path(filepath: str) -> str:
   async def backup_project():
   async def read_file(filepath, max_len=12000, retries=2):
   async def write_file(filepath, content, backup=True, retries=2):
   async def delete_line(filepath, line_num):
   async def replace_line(filepath, line_num, new_content):
   async def insert_line(filepath, line_num, new_content):

### plugins/system_ops/xt.py (4 defs/classes)
   async def system_status():
   async def execute_shell(cmd):
   def _fix_missing_newlines(cmd: str) -> str:
   async def ping_host(host: str) -> str:

### plugins/crawler/__init__.py (9 defs/classes)
   def _resolve_site_url(raw) -> str:
   def _clean_workspace() -> None:
   async def _run_node(js_path: str, args: list, timeout_ms: int = 60000) -> tuple:
   async def crawl_video_link(url: str) -> str:
   async def crawler_probe(url: str, timeout: int = 15000) -> str:
   async def crawler_run_node(code: str) -> str:
   async def crawler_dom_hook(url: str, timeout: int = 20000) -> str:
   async def crawl_custom_by_id(url: str) -> str:
   async def download_video_by_url(url: str) -> str:

### plugins/crawler/robust_crawler.py (7 defs/classes)
   def is_video_url(url: str) -> bool:
   def _has_m3u8_signature(text: str) -> bool:
   async def _run_node(script_path: str, args: list, timeout_ms: int):
   def _parse_dom_hook(stdout: str):
   def _parse_probe(stdout: str):
   def _pick_valid_video(candidates, evidence):
   async def robust_video_crawl(url: str, timeout: int = DOM_HOOK_TIMEOUT_MS) -> dict:

### plugins/crawler/video_crawler.py (3 defs/classes)
   async def _run_node(script_path: str, args: list, cwd: str, timeout: int) -> tuple:
   async def _refresh_cookie(work_dir: str) -> bool:
   async def crawl_video(video_url: str) -> dict:

### plugins/core_utils/__init__.py (0 defs/classes)

### plugins/core_utils/admin_gh.py (3 defs/classes)
   def add_admin(sender_id, new_admin):
   def del_admin(sender_id, del_admin):
   def list_admin():

### plugins/core_utils/aup_tools.py (8 defs/classes)
   def _sanitize_output(text: str, label: str) -> str:
   async def _run_via_subprocess_shell(cmd: str, timeout_sec: float) -> tuple:
   async def _run_via_subprocess_exec(cmd: str, timeout_sec: float) -> tuple:
   def _run_via_subprocess_run(cmd: str, timeout_sec: float) -> tuple:
   def _fix_missing_newlines(cmd: str) -> str:
   async def aup_execute_shell(cmd: str, timeout_sec: float = 15.0) -> AUPResult:
   async def _demo_success() -> AUPResult:
   async def aup_run_bash_script(script_content: str, timeout: int = 30) -> AUPResult:

### plugins/core_utils/backup_manifest.py (7 defs/classes)
   def _read_xmkj() -> str:
   def _get_xmkj_mtime() -> float:
   def _extract_json_array(text: str) -> list:
   async def _parse_xmkj_via_ai() -> list:
   async def refresh_manifest() -> list:
   def is_manifest_stale() -> bool:
   def load_manifest() -> list:

### plugins/core_utils/checkin.py (8 defs/classes)
   def get_today() -> str:
   def load_state() -> dict:
   def save_state(data: dict) -> None:
   def has_checked_in_today() -> bool:
   def parse_checkin_result() -> dict:
   async def execute_checkin() -> dict:
   async def auto_checkin() -> dict:
   async def run_checkin() -> str:

### plugins/core_utils/downloader.py (1 defs/classes)
   async def download_m3u8(url: str, referer: str = None, output_name: str = "video") -> str:

### plugins/core_utils/episodic.py (8 defs/classes)
   def _today() -> str:
   def _load_state() -> dict:
   def _save_state(state: dict) -> None:
   def count_today_turns() -> int:
   def append_messages(messages: list, start_turn: int) -> None:
   def maybe_flush(key: str, history: list) -> None:
   def recall(keyword: str, limit: int = 20) -> list:
   def clear_expired() -> None:

### plugins/core_utils/feishu_bridge.py (27 defs/classes)
   def _oid_hash(open_id: str) -> int:
   def register_open_id(open_id: str) -> int:
   def resolve_open_id(uid: Any) -> Optional[str]:
   def _load_env_pair() -> tuple:
   async def get_tenant_token() -> Optional[str]:
   def ensure_media_server() -> None:
   async def _download_resource(message_id: str, file_key: str, rtype: str) -> Optional[bytes]:
   def _save_media(data: bytes, ext: str) -> Optional[str]:
   class Seg:
   def __init__(self, type_: str, data: dict):
   def _seg_data_of(segment) -> dict:
   class FeishuEventView:
   def __init__(self, event, bot, message: Optional[List[Seg]] = None):
   async def build(cls, event, bot) -> "FeishuEventView":
   async def _convert_message(self) -> List[Seg]:
   def message(self) -> List[Seg]:
   def get_plaintext(self) -> str:
   class FeishuBotView:
   def __init__(self, bot):
   async def send(self, event, message=None, **kwargs):
   async def send_private_msg(self, user_id=None, message=None, **kwargs):
   async def send_msg(self, receive_id_type: str, receive_id: str, content: str, msg_type: str):
   async def call_api(self, api: str, **params):
   async def _normalize_outgoing(self, message):
   async def _upload_image(self, image_bytes: bytes) -> Optional[str]:
   def extract_plain_text(message) -> str:
   def admin_open_id() -> str:

### plugins/core_utils/lexicon.py (13 defs/classes)
   class LexiconDB:
   def __init__(self, db_path: Optional[Path] = None):
   def _connect(self):
   def learn(self, word: str, intent: str, source: str = "learned") -> bool:
   def match(self, text: str) -> Optional[Tuple[str, str]]:
   def stats(self) -> dict:
   def should_llm_route(text: str) -> Tuple[bool, str]:
   def rule_match(text: str) -> Optional[Tuple[str, str, float]]:
   def extract_action_word(text: str) -> Optional[str]:
   class FunnelRoute:
   def __init__(self, label, reason, confidence, layer, word=None, llm_called=False):
   def to_dict(self):
   def funnel_route(text: str, llm_classify=None, lexicon: Optional[LexiconDB] = None) -> FunnelRoute:

### plugins/core_utils/media.py (3 defs/classes)
   def _find_latest_mp4() -> Path | None:
   async def convert_latest_mp4_to_mp3() -> str:
   def clean_media(scope: str) -> str:

### plugins/core_utils/scores.py (5 defs/classes)
   def _load() -> dict:
   def _save(data: dict) -> None:
   def _decay(score: float, last_success: str) -> float:
   def add_score(tool_name: str) -> None:
   def get_scores(tool_names: list) -> dict:

### plugins/core_utils/security.py (3 defs/classes)
   def _sanitize_args(args) -> dict:
   def log_audit(tool_name: str, args, sender_id: str, result: str,
   def check_permission(tool_name: str, sender_id: str, default_admin: str) -> Tuple[bool, str]:

### plugins/core_utils/security_scanner.py (1 defs/classes)
   def sanitize_crawler_output(text: str) -> str:

### plugins/core_utils/tool_schema_builder/__init__.py (0 defs/classes)

### plugins/core_utils/tool_schema_builder/traits/__init__.py (0 defs/classes)

### plugins/core_utils/tool_schema_builder/traits/to_schema_able.py (2 defs/classes)
   class ToSchemaAble(Protocol):
   def to_schema(self) -> Dict[str, Any]:

### plugins/core_utils/tool_schema_builder/models/__init__.py (0 defs/classes)

### plugins/core_utils/tool_schema_builder/models/schemas/__init__.py (0 defs/classes)

### plugins/core_utils/tool_schema_builder/models/schemas/all_of_schema.py (1 defs/classes)
   class AllOfSchema(BaseMultipleSchemas):

### plugins/core_utils/tool_schema_builder/models/schemas/any_of_schema.py (1 defs/classes)
   class AnyOfSchema(BaseMultipleSchemas):

### plugins/core_utils/tool_schema_builder/models/schemas/array_schema.py (2 defs/classes)
   class ArraySchema(BaseSchema):
   def to_schema(self) -> Dict[str, Any]:

### plugins/core_utils/tool_schema_builder/models/schemas/base_multiple_schema.py (3 defs/classes)
   class BaseMultipleSchemas(BaseSchema):
   def to_schema(self) -> Dict[str, List[Any]]:
   def build(cls, *schemas: BaseSchema) -> Self:

### plugins/core_utils/tool_schema_builder/models/schemas/base_schema.py (2 defs/classes)
   class BaseSchema(BaseModel):
   def to_schema(self) -> Dict[str, Any]:

### plugins/core_utils/tool_schema_builder/models/schemas/boolean_schema.py (1 defs/classes)
   class BooleanSchema(BaseSchema):

### plugins/core_utils/tool_schema_builder/models/schemas/function_schema.py (2 defs/classes)
   class FunctionSchema(BaseSchema):
   def to_schema(self) -> Dict[str, Any]:

### plugins/core_utils/tool_schema_builder/models/schemas/integer_schema.py (1 defs/classes)
   class IntegerSchema(BaseSchema):

### plugins/core_utils/tool_schema_builder/models/schemas/null_schema.py (1 defs/classes)
   class NullSchema(BaseSchema):

### plugins/core_utils/tool_schema_builder/models/schemas/number_schema.py (1 defs/classes)
   class NumberSchema(BaseSchema):

### plugins/core_utils/tool_schema_builder/models/schemas/object_schema.py (2 defs/classes)
   class ObjectSchema(BaseSchema):
   def to_schema(self) -> Dict[str, Any]:

### plugins/core_utils/tool_schema_builder/models/schemas/one_of_schema.py (1 defs/classes)
   class OneOfSchema(BaseMultipleSchemas):

### plugins/core_utils/tool_schema_builder/models/schemas/string_schema.py (1 defs/classes)
   class StringSchema(BaseSchema):

### plugins/core_utils/tool_schema_builder/models/schemas/tool_schema.py (2 defs/classes)
   class ToolSchema(BaseSchema):
   def to_schema(self) -> Dict[str, Any]:

### plugins/core_utils/tool_schema_builder/core/__init__.py (0 defs/classes)

### plugins/core_utils/tool_schema_builder/core/tool_schema_builder.py (10 defs/classes)
   class ToolSchemaBuilder:
   def __init__(self) -> None:
   def parameters_dict(self) -> Dict[str, BaseSchema]:
   def required_parameters(self) -> List[str]:
   def set_parameter(
   def set_required(self, parameter_name: str) -> Self:
   def build_parameters(self) -> Self:
   def build_function(self, name: str) -> Self:
   def build_tool(self) -> Self:
   def to_schema(self) -> Dict[str, Any]:

### plugins/tasks/__init__.py (0 defs/classes)

### plugins/tasks/divine_task.py (15 defs/classes)
   async def _call_model(strategy: str, messages: list, timeout: float) -> str:
   def _estimate_tokens(text: str) -> int:
   def _ctx_size(ctx: dict) -> int:
   def _extract_json(text: str) -> Optional[dict]:
   async def _call_model_once(ctx: dict) -> str:
   async def _compress_context(ctx: dict) -> None:
   def _check_limits(ctx: dict) -> tuple[Optional[str], bool]:
   async def _finalize_with_summary(user_id: str, ctx: dict, notify, reason: str) -> str:
   def _fallback_summary(ctx: dict, reason: str) -> str:
   async def _run_loop(user_id: str, ctx: dict, notify) -> str:
   def _is_cancel_intent(text: str) -> bool:
   async def run_divine_task(
   def is_pending(user_id: str) -> bool:
   def clear_pending(user_id: str) -> None:
   async def resume_divine_task(

### plugins/tasks/large_task.py (10 defs/classes)
   async def _call_model(strategy: str, messages: list, timeout: float) -> tuple[str, str]:
   def _estimate_tokens(text: str) -> int:
   def _ctx_size(ctx: dict) -> int:
   def _build_tool_list() -> str:
   def _extract_json(text: str) -> Optional[dict]:
   async def _plan(user_input: str, tool_list: str) -> dict:
   async def _execute_one(subtask: dict, ctx: dict, tool_list: str, sender_id: str) -> dict:
   async def _summarize(task_name: str, executions: list) -> str:
   def _fallback_summary(executions: list) -> str:
   async def run_large_task(

### plugins/tasks/task_queue.py (7 defs/classes)
   def split_tasks(text: str) -> list:
   async def map_tasks_to_tools(tasks: list, tool_names: list) -> list:
   async def sort_by_scores(items: list) -> list:
   class TaskQueue:
   def __new__(cls):
   async def submit(self, text: str, user_id: str, chat_type: str, group_id: str) -> None:
   async def _notify(self, user_id: str, message: str) -> None:

### tool_center/__init__.py (0 defs/classes)

### tool_center/db_manager.py (8 defs/classes)
   def _connect() -> sqlite3.Connection:
   def init_db() -> None:
   def upsert_tool(record: dict) -> int:
   def set_active(name: str, active: bool) -> bool:
   def fetch_all(active_only: bool = True) -> list[dict]:
   def fetch_by_name(name: str) -> Optional[dict]:
   def fetch_by_category(category: str, active_only: bool = True) -> list[dict]:
   def count() -> int:

### tool_center/migrate_tools.py (2 defs/classes)
   def _first_doc_line(func) -> str:
   def main() -> int:

### tool_center/registry_manager.py (22 defs/classes)
   def _resolve_func(name: str, module_path: str, func_name: str) -> Optional[Callable]:
   def is_aup_compliant(func: Optional[Callable] = None, meta: Optional[dict] = None) -> bool:
   def read_aup_meta(func: Callable) -> Optional[dict]:
   def register_tool(
   def _openai_schema(name: str, description: str, params_desc: dict) -> dict:
   def get_ai_visible_schemas(category: Optional[str] = None) -> list[dict]:
   def get_ai_visible(name: str) -> Optional[dict]:
   def get_all_schemas(category: Optional[str] = None) -> list[dict]:
   def get_tool_info(name: str) -> Optional[dict]:
   def toggle_tool_status(name: str, status: bool) -> bool:
   def get_all_names(active_only: bool = True) -> list[str]:
   class CompatView:
   def __init__(self, active_only: bool = True):
   def _rows(self) -> list[dict]:
   def items(self) -> Iterator[tuple]:
   def get(self, name: str, default=None):
   def keys(self) -> list[str]:
   def __contains__(self, name: str) -> bool:
   def __getitem__(self, name: str):
   def __len__(self) -> int:
   def __iter__(self):
   def build_compat_view(active_only: bool = True) -> CompatView:

### import_center/__init__.py (0 defs/classes)

### import_center/bootstrap.py (5 defs/classes)
   def _attr_factory(module_name: str, attr_name: str | None) -> Callable[[], Any]:
   def _load() -> Any:
   def _call_factory(module_name: str, attr_name: str, args: tuple = (), kwargs: dict | None = None) -> Callable[
   def _load() -> Any:
   def bootstrap() -> None:

### import_center/manager.py (8 defs/classes)
   class ServiceContainer:
   def __init__(self) -> None:
   def register(
   def get(self, name: str) -> Any:
   def has(self, name: str) -> bool:
   def __contains__(self, name: str) -> bool:
   def get_service(name: str) -> Any:
   def register_service(name: str, instance: Any = None, *, factory: Optional[Callable[[], Any]] = None) -> None:

### pi_backend/__init__.py (0 defs/classes)

### pi_backend/models/__init__.py (0 defs/classes)

### pi_backend/models/base_event.py (1 defs/classes)
   class BaseEvent(BaseModel):

### pi_backend/models/rpc_events/__init__.py (0 defs/classes)

### pi_backend/models/rpc_events/base_rpc_event.py (1 defs/classes)
   class BaseRPCEvent(BaseEvent):

### pi_backend/models/rpc_events/client_events/__init__.py (0 defs/classes)

### pi_backend/models/rpc_events/client_events/abort.py (1 defs/classes)
   class AbortCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/abort_bash.py (1 defs/classes)
   class AbortBashCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/abort_retry.py (1 defs/classes)
   class AbortRetryCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/base_command.py (1 defs/classes)
   class BaseCommand(BaseRPCEvent):

### pi_backend/models/rpc_events/client_events/bash.py (1 defs/classes)
   class BashCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/clear_queue.py (1 defs/classes)
   class ClearQueueCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/clone.py (1 defs/classes)
   class CloneCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/compact.py (1 defs/classes)
   class CompactCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/cycle_model.py (1 defs/classes)
   class CycleModelCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/cycle_thinking_level.py (1 defs/classes)
   class CycleThinkingLevelCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/export_html.py (1 defs/classes)
   class ExportHtmlCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/follow_up.py (1 defs/classes)
   class FollowUpCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/fork.py (1 defs/classes)
   class ForkCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/get_available_models.py (1 defs/classes)
   class GetAvailableModelsCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/get_available_thinking_levels.py (1 defs/classes)
   class GetAvailableThinkingLevelsCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/get_commands.py (1 defs/classes)
   class GetCommandsCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/get_entries.py (1 defs/classes)
   class GetEntriesCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/get_fork_messages.py (1 defs/classes)
   class GetForkMessagesCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/get_last_assistant_text.py (1 defs/classes)
   class GetLastAssistantTextCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/get_messages.py (1 defs/classes)
   class GetMessagesCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/get_session_stats.py (1 defs/classes)
   class GetSessionStatsCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/get_state.py (1 defs/classes)
   class GetStateCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/get_tree.py (1 defs/classes)
   class GetTreeCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/new_session.py (1 defs/classes)
   class NewSessionCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/prompt.py (1 defs/classes)
   class PromptCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/set_auto_compaction.py (1 defs/classes)
   class SetAutoCompactionCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/set_auto_retry.py (1 defs/classes)
   class SetAutoRetryCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/set_follow_up_mode.py (1 defs/classes)
   class SetFollowUpModeCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/set_model.py (1 defs/classes)
   class SetModelCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/set_session_name.py (1 defs/classes)
   class SetSessionNameCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/set_steering_mode.py (1 defs/classes)
   class SetSteeringModeCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/set_thinking_level.py (1 defs/classes)
   class SetThinkingLevelCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/steer.py (1 defs/classes)
   class SteerCommand(BaseCommand):

### pi_backend/models/rpc_events/client_events/switch_session.py (1 defs/classes)
   class SwitchSessionCommand(BaseCommand):

### pi_backend/models/rpc_events/server_events/__init__.py (0 defs/classes)

### pi_backend/models/rpc_events/server_events/base_response.py (1 defs/classes)
   class BaseResponse(BaseRPCEvent):

### pi_backend/models/rpc_events/server_events/responses/__init__.py (0 defs/classes)

### pi_backend/models/rpc_events/server_events/responses/abort_bash_response.py (1 defs/classes)
   class AbortBashResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/abort_response.py (1 defs/classes)
   class AbortResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/abort_retry_response.py (1 defs/classes)
   class AbortRetryResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/bash_response.py (1 defs/classes)
   class BashResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/clear_queue_response.py (1 defs/classes)
   class ClearQueueResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/clone_response.py (1 defs/classes)
   class CloneResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/compact_response.py (1 defs/classes)
   class CompactResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/cycle_model_response.py (1 defs/classes)
   class CycleModelResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/cycle_thinking_level_response.py (1 defs/classes)
   class CycleThinkingLevelResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/export_html_response.py (1 defs/classes)
   class ExportHtmlResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/follow_up_response.py (1 defs/classes)
   class FollowUpResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/fork_response.py (1 defs/classes)
   class ForkResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/get_available_models_response.py (1 defs/classes)
   class GetAvailableModelsResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/get_available_thinking_levels_response.py (1 defs/classes)
   class GetAvailableThinkingLevelsResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/get_commands_response.py (1 defs/classes)
   class GetCommandsResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/get_entries_response.py (1 defs/classes)
   class GetEntriesResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/get_fork_messages_response.py (1 defs/classes)
   class GetForkMessagesResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/get_last_assistant_text_response.py (1 defs/classes)
   class GetLastAssistantTextResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/get_messages_response.py (1 defs/classes)
   class GetMessagesResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/get_session_stats_response.py (1 defs/classes)
   class GetSessionStatsResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/get_tree_response.py (1 defs/classes)
   class GetTreeResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/new_session_response.py (1 defs/classes)
   class NewSessionResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/prompt_response.py (1 defs/classes)
   class PromptResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/set_auto_compaction_response.py (1 defs/classes)
   class SetAutoCompactionResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/set_auto_retry_response.py (1 defs/classes)
   class SetAutoRetryResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/set_follow_up_mode_response.py (1 defs/classes)
   class SetFollowUpModeResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/set_model_response.py (1 defs/classes)
   class SetModelResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/set_session_name_response.py (1 defs/classes)
   class SetSessionNameResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/set_steering_mode_response.py (1 defs/classes)
   class SetSteeringModeResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/set_thinking_level_response.py (1 defs/classes)
   class SetThinkingLevelResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/state_response.py (1 defs/classes)
   class StateResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/steer_response.py (1 defs/classes)
   class SteerResponse(BaseResponse):

### pi_backend/models/rpc_events/server_events/responses/switch_session_response.py (1 defs/classes)
   class SwitchSessionResponse(BaseResponse):

### pi_backend/models/_internal/__init__.py (0 defs/classes)

### pi_backend/models/_internal/tool_end_flag.py (1 defs/classes)
   class ToolEndFlag:

### pi_backend/models/_internal/tool_result_chunk.py (1 defs/classes)
   class ToolResultChunk:

### pi_backend/models/tool_events/__init__.py (0 defs/classes)

### pi_backend/models/tool_events/base_tool_event.py (1 defs/classes)
   class BaseToolEvent(BaseEvent):

### pi_backend/models/tool_events/tool_execution.py (1 defs/classes)
   class ToolExecution(BaseToolEvent):

### pi_backend/models/tool_events/tool_execution_end.py (1 defs/classes)
   class ToolExecutionEnd(BaseToolEvent):

### pi_backend/models/tool_events/tool_result.py (1 defs/classes)
   class ToolResult(BaseToolEvent):

### pi_backend/core/__init__.py (0 defs/classes)

### pi_backend/core/pi_backend.py (38 defs/classes)
   class PIBackend:
   def __init__(
   async def read_jsonl(self) -> Dict[str, Any]:
   async def write_jsonl(self, msg: str) -> None:
   async def prompt(
   async def steer(
   async def follow_up(
   async def abort(self, request_id: Optional[str] = None) -> None:
   async def clear_queue(self, request_id: Optional[str] = None) -> None:
   async def new_session(
   async def get_state(self, request_id: Optional[str] = None) -> None:
   async def get_messages(self, request_id: Optional[str] = None) -> None:
   async def set_model(
   async def cycle_model(self, request_id: Optional[str] = None) -> None:
   async def get_available_models(self, request_id: Optional[str] = None) -> None:
   async def set_thinking_level(
   async def cycle_thinking_level(self, request_id: Optional[str] = None) -> None:
   async def get_available_thinking_levels(self, request_id: Optional[str] = None) -> None:
   async def set_steering_mode(
   async def set_follow_up_mode(
   async def compact(
   async def set_auto_compaction(self, enabled: bool, request_id: Optional[str] = None) -> None:
   async def set_auto_retry(self, enabled: bool, request_id: Optional[str] = None) -> None:
   async def abort_retry(self, request_id: Optional[str] = None) -> None:
   async def bash(
   async def abort_bash(self, request_id: Optional[str] = None) -> None:
   async def get_session_stats(self, request_id: Optional[str] = None) -> None:
   async def export_html(
   async def switch_session(
   async def fork(self, entry_id: str, request_id: Optional[str] = None) -> None:
   async def clone(self, request_id: Optional[str] = None) -> None:
   async def get_fork_messages(self, request_id: Optional[str] = None) -> None:
   async def get_entries(
   async def get_tree(self, request_id: Optional[str] = None) -> None:
   async def get_last_assistant_text(self, request_id: Optional[str] = None) -> None:
   async def set_session_name(
   async def get_commands(self, request_id: Optional[str] = None) -> None:
   async def _send_command(self, command: BaseCommand) -> None:

### pi_backend/core/pi_process.py (6 defs/classes)
   class PIProcess:
   def __init__(self, pi_process: Process) -> None:
   async def build_process(
   async def read_line(self) -> str:
   async def write_line(self, *lines: str) -> None:
   async def close_process(self) -> None:

### pi_backend/core/pi_tool_backend.py (19 defs/classes)
   class PIToolBackend:
   def __init__(
   def set_timeout(self, timeout: float) -> None:
   def register_tool(self, func: ASYNC_FUNC, name: Optional[str] = None) -> None:
   async def send_result(self, id: str, result: Any) -> None:
   async def send_end(self, id: str, reason: Optional[str] = None) -> None:
   async def close_backend(self) -> None:
   async def ensure_close(self, id: str) -> None:
   async def on_execute_tool(self, id: str, tool_name: str, tool_param: Dict[str, Any]) -> None:
   async def on_connect(self, reader: StreamReader, writer: StreamWriter) -> None:
   async def run_server(self, **kwargs) -> Tuple[Server, Task]:
   async def _execute_tool(self, tool_name: str, tool_param: Dict[str, Any]) -> AsyncIterable[Any]:
   async def _next_tool_output(self, tool_name: str, generation) -> Union[ToolResultChunk, ToolEndFlag]:
   async def _send(self, writer: StreamWriter, msg: str) -> None:
   async def _recv_line(self, reader: StreamReader) -> bytes:
   async def _recv_tool_request(self, reader: StreamReader) -> Optional[ToolExecution]:
   async def _cleanup_connection(self, id: str) -> None:
   async def _close_writer(self, writer: StreamWriter) -> None:
   async def _close_server(self) -> None:



================================================================================
第二类：设计理念与项目冷知识（配置与环境的深刻记录）
================================================================================

────────────────────────────────────────────────────────────
2.1 设计理念（以下为项目最高准则，以客观事实陈述，无提示词、无身份授予）
────────────────────────────────────────────────────────────

【权重排序（原话逻辑，必须遵守）】
逻辑性 > 健壮性 > 拓展性 > 安全性。先回答核心问题，实事求是。完成不了
就直说。修复代码时：① 说文件名和函数名（不说行号）② 采用对比法（先贴
原代码，再指出问题，最后贴修改后代码）。绝不做任何无用的过度安全设计。

【工具本位，物理拦截优先（本项目最高架构红线）】
底层逻辑严禁依赖 Prompt 提示词去约束大模型行为。理由：大模型天然存在
不可控的「复读机」倾向，当参数过长时必然引发泄漏；用提示词做兜底在
工程上极不可靠。因此：
- 输出清洗用正则与代码物理拦截（sanitize_output / _sanitize_for_chat），
  不靠「请你不要输出工具调用」这类话。
- 别名解析用 dz.py 入口正则前置替换（_preprocess_alias），不靠 AI 自觉。
- 工具可见性用数据库 is_active 硬控（tool_center），不靠模型自律。
- 参数隔离用代码剥离（只回填「已调用工具：xxx」），不靠模型理解。

【微内核 + 动态插件】
主引擎（dz.py / ai.py / admin.py）极简且稳定；所有能力经 AUP 协议挂载
为 plugins/ 动态插件（importlib 扫描，零硬编码 import）。新增能力 =
新建插件包 + @aup_tool 注册，主引擎零改动。这保证了核心链路的可验证性
与扩展的低风险性。

【权限制衡】
AI 只能通过 tool_center 调用带 @aup_tool 且 is_active=1 的工具；无法
修改 config_center/settings.py 与 .env（配置文件只由人类修改）；高危
工具走 is_danger + 权限校验（check_permission）；所有工具返回先过
AUP 协议再进输出清洗层。

【宏工具哲学】
能用一个复杂工具黑盒闭环的，绝不让 AI 走流水线。AI 的中间步骤 =
token 浪费 + 出错面 + 泄漏面。download_video_by_url 是唯一对外爬虫
工具；中间节点工具全部 is_active=0 隐藏，AI 无选择余地。

【防泄漏总原则】
聊天通道（飞书）与日志不得出现敏感完整 URL/m3u8。工具返回脱敏
（只给状态 + id + 本地路径）；完整链接仅本地 logger.info 留档。

────────────────────────────────────────────────────────────
2.2 项目冷知识（防呆避坑，全部为已验证的客观事实）
────────────────────────────────────────────────────────────

【C 端 DeepSeek 通道（聊天免费通道）】
- 会话创建响应：会话 id 在 data.biz_data.chat_session.id（2024 年
  接口改版后 biz_data.id 不存在，返回 None）。解析错必然 RuntimeError
  → 降级本地策略 → 消耗付费 API。
- 必须携带完整浏览器 Headers：Authorization: Bearer <token> +
  x-client-platform: web + UA + Origin + Referer。裸 aiohttp session
  （无 UA）会被 WAF 以 HTTP 429 拦成 HTML 错误页。
- 独立脚本调用前必须先 load_dotenv('${PROJECT_ROOT}/.env', override=True)，
  否则读不到 DEEPSEEK_USER_TOKEN。
- PoW 挑战为 DeepSeekHashV1（Node + sha3 wasm 求解），缺失
  x-ds-pow-response 会返回错误码 40300。
- 日志文件：web_deepseek.log；故障先看它。

【飞书防封与别名前置拦截】
- 用户绝不能在飞书发送敏感完整 URL（平台风控 = 封号风险）。只发
  人畜无害缩写 + ID（tz 92285 / tz#92285 / yt abc123）。
- 别名替换必须由 dz.py 在消息进入 AI 前物理完成（_preprocess_alias），
  绝不交给大模型（模型会浪费 token 去 grep/读文件、还会猜错意图——
  曾有日志证据：tz 92285 被判「低置信度 0.55 → 追问用户」）。
- 工具返回必须脱敏：crawl_custom_by_id 返回 {success,title,id,note}
  不含 m3u8；download_video_by_url 返回 msg + path 不含 URL。
- 若日志出现「低置信度 → 追问」，说明别名消息未被前置替换
  （dz.py 未跑新代码或 site_map.json 缺项），优先检查这两处。

【宏工具与工具可见性】
- download_video_by_url（tool_id=32）是 AI 可见的唯一爬虫下载工具。
- crawl_video_link / crawl_custom_by_id / crawler_probe /
  crawler_dom_hook / crawler_run_node 在 tool_center.db 中
  is_active=0（隐藏）。隐藏不会被重启覆盖：启动流程只 load_all_plugins
  导入插件，不重注册（registry_manager.register_tool 仅显式调用时
  upsert）。
- 修改工具描述/参数：改源码 @aup_tool 装饰器后，需显式触发一次
  register_tool 才会同步进 db（或人工 UPDATE tool_center.db）。

【WSL 环境玄学（跑命令前必读）】
- python 必须用绝对路径 ${PROJECT_ROOT}/venv310/bin/python；裸 python 在
  部分 PATH 环境下不可用。
- Windows→WSL 执行链：Write 脚本到 C:\Users\Public\feishu_stage\
  再 `wsl -u root -- bash /mnt/c/Users/Public/feishu_stage/xxx.sh`。
  heredoc / 复杂引号 / $ 变量经 PowerShell 直接 `wsl -c` 会崩或丢字
  ——禁止（已有 awk 转义炸命令、find 排除条件漏子目录的教训）。
- WSL 后台进程易被回收（systemd-less 环境）；长任务用前台 +
  run_in_background 跟踪；启动前向人类说明，结束自回收。杀进程用
  pkill -f 或 kill PID（查 PID 用 ps aux | grep dz.py）。
- Node 脚本目录：crawler 的 JS 在 plugins/crawler/ 根（fetch_by_id /
  download_video / diag）与 crawler_tools/（probe / dom_hook / run）。
  子进程调用必须固定 cwd（video_crawler._run_node 显式传 cwd），
  相对路径按 cwd 解析。
- 端口：NoneBot 监听 127.0.0.1:8082；飞书走长连接（WS），无需公网
  端口。端口被占时先查旧进程：ps aux | grep dz.py 再 kill。
- 改核心文件后必须 py_compile 校验 + 重启验证（python3 dz.py 前台
  观察启动日志）。改完 .env 需重启才生效。

【通道与 Napcat 禁区（红线，违反即事故）】
- 当前只有飞书在线：启动日志出现 Loaded adapters: Feishu 即正确。
- OneBot 重连 3000 失败是正常噪音（Napcat 未运行），不得因此启动
  Napcat，也不得删除/注释日志代码。
- Napcat 注入已撤销：QQNT resources/app/loadNapCat.js 已删；C:\Napcat
  \config\onebot11.json websocket enable=False；Napcat 缓存
  session.token 已清。
- 严禁 NapCatWinBootMain / launcher*.bat / inject_napcat.ps1 /
  KillQQ.bat / node napcat.mjs——任何形态的 QQ 协议端启动都是红线。
- Napcat 在 Windows 宿主机 C:\Napcat（WSL 之外）；跨系统操作 Napcat
  文件只能走 /mnt/c/... 或人类操作。

【备份与版本管理现状】
- 2026-10-24 起全库 .bak* 已物理删除（根目录 + 插件子目录 + bot_utils
  备份目录），项目内 .bak 计数为 0。不再产生 .bak 备份文件。
- 改核心文件前可先 git commit 或 cp 到 /tmp/ 临时备份（不落项目内）。
- 全库主文件基线大小：dz.py 27985B / ai.py 73318B / admin.py 23592B /
  cl.py 3140B / deepseek_web_integration.py 25946B / memory.py 13922B
  （改前可对照）。

【环境不一致导致的坑（已实证）】
- 裸 session vs 完整头 session：同一接口，缺 UA 被 429 拦，带头正常。
  接口行为取决于 Headers，不是只看 URL。
- 独立脚本 vs 集成运行：独立脚本缺少 load_dotenv 时「未配置 token」，
  集成运行正常——脚本必须显式加载 .env。
- find 排除条件：`-not -path './venv310/*'` 只排除一层；漏子目录时
  find 结果与 ls 不一致。核验用两种命令交叉（find + ls）。
- awk 经 PowerShell 双层引号会因 $ 转义崩溃——核验命令避免用 awk
  或整段写进 .sh 文件再执行。

【飞书侧事实】
- 管理员 open_id：ou_2df0b2dac99ca750cf5e25ae059eeb63；业务层
  crc32 映射 int = 402710372。
- 飞书 App cli_aa51c999ecf8dd1c；事件走长连接接收；权限清单见
  FEISHU_PERMISSION_CHECKLIST.md。
- 发送通道：send_segmented_reply 分段 + sanitize 兜底；媒体走
  feishu_bridge（_upload_image）。

【日志文件一览（排查入口）】
- feishu_runtime.log：飞书适配器运行日志。
- web_deepseek.log：C 端通道结构化日志（故障第一证据）。
- audit.log：工具调用审计（权限/越权核查）。
- defense_center/defense_audit.log：防御处置记录。
- episodic.log：情节记忆。



================================================================================
第三类：架构裁决与未决问题（中性技术陈述）
================================================================================

────────────────────────────────────────────────────────────
3.1 架构裁决（判定为合理的架构决策）
────────────────────────────────────────────────────────────

【AUP 协议层 — 合理】
- 统一响应结构（AUPResult：status/error_code/data/fallback_chain）
  消除工具返回格式分歧；@aup_tool 装饰器强制入参/出参校验；未过 AUP
  的工具拒绝注册。该协议是工具链可信度的基础，保持强制。

【微内核 + 动态插件 — 合理】
- 主引擎（dz.py/ai.py/admin.py）零硬编码 import 插件；plugins/ 四件套
  （core_utils/crawler/system_ops/tasks）经 importlib 动态加载。
  新增能力不需要触碰主引擎。该结构已验证（插件化迁移三轮全部通过）。

【EHIR 三层意图分类 — 合理】
- L1 静态正则（零 API）→ L2 词库（SQLite，零 API，可学习）→ L3 大模型
  （仅前两层未命中时）。最大化本地决策、最小化 API 消耗。词库学习
  机制（模型判 tool 后提取动作词）使分类器可持续进化。

【物理拦截优先（输出清洗 + 参数隔离） — 合理】
- sanitize_output（正则拦截泄漏特征）与 _sanitize_for_chat（剥离工具
  参数 JSON）在代码层完成，不依赖模型自律。该防线已实证：高转义压力
  测试下最终通道无原始 JSON 泄漏。

【工具可见性硬控（tool_center is_active） — 合理】
- AI 可见工具列表由数据库 active_only 过滤决定；隐藏工具（爬虫中间
  节点）重启不复活（启动流程不重注册）。渐进式披露降低了工具误用面。

【别名前置物理替换（dz.py _preprocess_alias） — 合理】
- 消息进入 AI 前由正则完成缩写→URL 替换；工具返回脱敏。该设计同时
  满足防封（聊天通道无敏感 URL）与防错（AI 无解析机会）两个目标。

【C 端 DeepSeek 通道 — 合理】
- 免费网页版通道承担闲聊流量，付费 API 仅作降级兜底。修复后
  （chat_session.id 解析 + 完整浏览器头）已全链路实证（1.3s 真实
  回复）。该通道显著降低运营成本。

【宏工具闭环（download_video_by_url） — 合理】
- 解析+下载+转码+落盘一条龙黑盒；AI 只面对一个工具。业务闭环（发
  链接 → 自动下载 → 返回本地路径）完整，中间过程零暴露。

【defense_center 主动防御 — 合理】
- watchdog 探针 + policies 律法 + enforcer 执行器三层分离；默认
  DRY-RUN（只报不处置）降低误伤面；报警通道注入飞书。

【单飞书通道（OneBot 条件注册） — 合理】
- 主用飞书（长连接稳定），OneBot 仅在显式配置时注册；Napcat 注入
  撤销后不存在 QQ 协议端启动面。通道收敛降低了维护与安全成本。

────────────────────────────────────────────────────────────
3.2 未决问题（如实陈述，不做掩盖）
────────────────────────────────────────────────────────────

【plugins/crawler/cookies.json 缺失（长期遗留，人类待办）】
- 定制站点解析（tobvic 系）的有效 cookie 文件不存在于插件根目录。
- 现状：无 cookie 时该站 API 实测仍可解析下载（166MB 实证成功），
  但遇到 __js_challenge 反爬时解析会失败。
- 处置：人类在插件根放置有效 cookie（数组格式）后方可完全发挥
  定制工具能力；在此前，宏工具依赖公开 API 通道。

【C 端 DeepSeek 通道的会话上限与风控】
- C 端网页版通道存在并发/频率限制与账号风控可能；当前单会话续接
  正常，但高并发闲聊时可能出现限流（表现为降级本地策略）。
- 监控：web_deepseek.log 中 status=error 的比例；若频繁限流，
  需人类评估加会话轮换或多账号配置。

【签到功能已停用】
- dz.py 启动日志「签到功能已停用，跳过」：checkin.py 逻辑保留但
  未启用。如需恢复由人类在 config_center/settings.py 开启。

【飞书媒体服务器（ensure_media_server）】
- feishu_bridge 中媒体本地服务依赖本机资源下载；大文件（如 166MB
  mp4）不经飞书发送（仅返回本地路径），由人类自行取用。

────────────────────────────────────────────────────────────
3.3 变更操作标准化规程（任何改动必须遵守）
────────────────────────────────────────────────────────────

【代码变更规程】
1. 先读原代码再改：改前 Read 目标文件相关段落，禁止凭记忆重写。
2. 改后立即校验：venv310/bin/python -m py_compile <file>（语法门禁）。
3. 说文件名和函数名，不说行号；采用对比法描述（原代码 → 问题 →
  修改后代码）。
4. 涉及工具：改动后显式触发 register_tool 同步 tool_center.db（或
  UPDATE），并核验 get_ai_visible 结果。
5. 涉及配置：只改 config_center/settings.py 与 .env（人类把关），
  改后重启生效。

【验证规程】
1. 重启：python3 dz.py 前台观察启动日志（Feishu 连接成功、无
  OneBot 噪音、defense watchdog 启动）。
2. 功能验证：真实消息实测（如发送 tz 92285 观察日志「[安全别名]
  前置解析」+ 宏工具调用 + 落盘产物存在）。
3. 双通道核验：当前只有飞书通道；若配置 OneBot 需同时验证两侧。
4. 回归：核心链路（意图分类 → 工具执行 → 输出清洗 → 回复）逐环
  抽查日志。

【文档同步规程】
- D:\项目\PROJECT_GUIDE3.txt 为 v3 权威交接文档；
  ${PROJECT_ROOT}/PROJECT_GUIDE.md 为 WSL 内落点，两者内容保持一致。
- 每次架构变更后更新本文档对应段落（文件职责/冷知识/裁决），
  不积累欠账。

【安全红线核查清单（每次部署后过一遍）】
- 启动命令 = python3 dz.py（唯一）。
- 日志无 OneBot 重连堆栈（适配器未注册）。
- audit.log 无越权调用记录。
- 聊天通道无敏感 URL 泄漏（别名前置生效）。
- tool_center.db 隐藏工具保持 is_active=0。
- 项目内 .bak 计数 = 0。

================================================================================
（本文档结束。全部内容基于 2026-10-24 深度遍历与实证，与磁盘真实状态一致。）
================================================================================
