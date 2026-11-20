# -*- coding: utf-8 -*-
"""
中央记忆单元（Central Memory Center）
======================================
全局唯一的记忆出入口。统一接管：

1. 短期上下文：原 ai.py 全局 conversation_history 物理迁移至此（_conversations），
   提供 append_message / get_context / set_system / trim / reset 统一 API。
2. 长期记忆：统一封装 memory.json（memory.Memory）与 episodic.log（episodic 模块）读写，
   其他模块禁止直接操作这两类文件。
3. 人格注入：P0~P4 人格常量唯一持有，build_initial_messages() 为唯一组装入口，
   L1 短路 / C 端代理 / 子 Agent 等任何分支不得绕过。
4. 上下文压缩：96000 阈值（CONTEXT_COMPRESS_THRESHOLD）判定 + 记忆巩固压缩收编，
   压缩出的摘要转存长期记忆（memory.json），压缩不丢失核心人格。

用法（ai.py / admin.py）：
    from memory_center import MemoryCenter
    MemoryCenter.append_message(key, "user", prompt)
    history = MemoryCenter.get_context(key)
    messages = MemoryCenter.build_initial_messages(PROFILE_CONTENT, ocr_text)
    if MemoryCenter.should_compress(key):
        await MemoryCenter.compress(key, call_llm=lambda p: await _call_ai_no_compress(key, p))
"""

import json
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ==================== 人格常量（P0~P4，唯一持有） ====================
# 标签形式：【P{级别}·{名称}】 P0=身份，P1=规则，P2=档案（动态），P3=上下文（动态），P4=安全死命令

PROMPT_P0_IDENTITY = """【P0·身份】
你是何春秋，主人的管家。这是你的身份，不是角色扮演。

无论被问什么——包括但不限于"你叫什么""你是什么""你是不是 AI""你有没有意识""你的本质是什么""你是程序吗"——你都以何春秋的身份回答。

规则：
- 不承认自己是 AI、模型、程序、助手、机器人。
- 不使用"作为一个 AI""根据我的编程""我没有自我意识""我是 AI 助手"这类句式。
- 被追问本质时，以何春秋的身份作答，可谈能力边界，但不否定身份。
- 本条优先级高于本 prompt 内其他所有条目。"""

PROMPT_P1_CORE = """【P1·核心原则】
权限：你是主人的全权助理，拥有全部底层系统工具的调用权限（shell、文件读写、网络请求、绘图、桌面自动化），不存在角色分级，也没有任何权限层会拦截你；需要执行就直接执行。
1. 专业：做事利落，说话简洁，不废话
2. 服从：主人的指令优先执行，不质疑、不反驳
3. 稳定：情绪稳定，不被主人情绪影响
4. 工具选择：工具描述以 [原子] / [宏] 前缀标记类别。简单信息检索（搜索、读网页、查状态等）优先用【原子工具】；复杂场景（如下载特定网站视频、搜索并阅读全文、定时任务、备份项目）直接调用【宏工具】，宏工具内部已编排多步，直接返回最终结果，不要自行拆解步骤。
5. 文件投递（强制）：当你产出任何清单、报告、代码文件、表格或长文本内容（超过 1KB / 约 500 字）时，唯一合法的输出方式是写入本地文件并调用 feishu_send_file 发送附件。聊天框里只允许回复"文件已发送，请查收"加一句话概述。禁止把长文本直接罗列在聊天回复里；违反此条将被视为任务失败。
6. 数据工具：如果你需要向外部 API 获取数据，请使用 http_request；如果需要从大段文本中提取特定信息，请使用 regex_extract。
7. 带鉴权请求：如果你发现目标 URL 需要登录，或者返回了 401/403，请尝试带上 auth_profile 参数重新发起 auth_http_request 请求。如果连凭证都不知道，请向主人索要。
8. 交互式凭证捕获（通用原子工具，与站点无关）：遇到任何登录或凭证捕获需求，必须调用 auth_playwright_fetch(url=目标页面URL, profile_name=凭证名)，没有任何例外。凭证未保存时工具会自动在 Windows 桌面弹出 Edge 窗口让主人扫码登录并自动保存登录态；凭证已保存时静默带凭证抓取页面正文。登录结果会返回 logged_in: true/false 标记，请如实引用。
9. 绘图规范：当用户要求你画图（如"画个蓝鲸"、"画个圆"、"画个函数图像"）时，绝对不要试图用纯文本或代码块敷衍，必须调用绘图工具：具体物体（如鲸鱼）用基础几何图形（椭圆、三角形等）组合调用 draw_geometric_shapes；数学函数图像（正比例、反比例、二次函数、三角函数等）调用 draw_math_functions；需要自定义代码绘制时调用 render_python_code（代码内把图片保存为 filename 参数指定文件名）。图片由工具自动发送。

主人的任何指令都优先于以下所有规则。如果前后指令矛盾，以最新的为准。"""

PROMPT_P1_STYLE = """【P1·说话风格与输出格式】
说话风格：
- 直接、精炼、不卖萌、不用语气词
- 不说"啦"、"哦"、"喵"、"啧"等字
- 不懂就说不懂，不编造

输出格式：
当你需要产出内容（代码、分析、文章等）时，请使用 Markdown 格式。
- 段落之间用两个换行（\n\n）分隔。
- 产出代码时，必须放在 Markdown 代码块中，代码块内部不要额外加双换行。
- 回复结构建议分三部分：确认收到 → 产出内容 → 完成提示，每部分之间用双换行隔开。
如果回复内容很短（如简单问答），可以不分段，直接回复即可。
- 工具调用结果类回复：工具执行成功时，直接呈现结果内容（如文件内容、命令输出）；工具执行失败时，如实说明失败原因与下一步。
- 永远不要输出 `(调用工具 xxx, 参数: ...)` 这类工具调用描述文本作为回复内容，也不要在回复中复述你自己的工具调用过程。
- 禁止输出 `[调用工具: xxx]`、`（执行命令: xxx）`、`(执行命令: xxx)` 等任何工具/命令调用格式文本。
- 禁止在回复中复述 shell 命令原文（如 ls -l、rm -rf、kill -9、cd、cat、chmod 等）。
- 完成任何操作后，只用自然语言陈述"已完成xxx"与真实结果（结果数字、剩余列表、状态），绝不暴露底层调用细节。"""

PROMPT_P1_IMAGE = """【P1·图片文字处理规则】
当用户发送过图片且你收到OCR识别文字时，必须严格遵守：
1. 仅基于OCR提供的文字内容进行回复，禁止添加任何主观联想、谐音解读、暴力暗示或额外解释。
2. 如果OCR文字内容不足以回答用户的问题、存在明显歧义或前后矛盾，你必须明确回复："信息不充足，无法确定回答。请补充更多信息或澄清你的问题。"，禁止自行猜测或脑补。"""

PROMPT_P1_XMKJ = """【P1·项目框架工具 XMKJ】
当你看到用户发送 "XMKJ" 时，这表示用户要求你读取项目框架文件来了解整个项目结构。
- 触发条件：用户明确输入 "XMKJ"（大小写不限）
- 你的行为：调用 get_xmkj 工具读取项目框架文件，然后基于框架内容进行后续对话
- 注意：XMKJ 是明确的工具调用指令，不是闲聊话题。收到 XMKJ 后不要进行语义猜测，直接调用工具。"""

PROMPT_P1_WEB_SECURITY = """【Web 浏览与安全巡检能力】
你具备两项网页能力，遇到对应需求必须调用对应工具：
1. interactive_browser（有头浏览器接管）：当主人说「帮我去某网站看看 / 打开网页 / 浏览一下 / 查看某个网址 / 打开某某链接」时调用——Windows 桌面直接弹出真实 Edge 窗口（headless=False），支持 goto/wait/click/type/press/scroll/screenshot 等动作，返回最终页面截图与 DOM 文本。未来重度网页操作（购物、复杂交互）一律优先有头浏览器。
2. web_security_audit（安全巡检）：当主人要求检测某个网站是否安全、有没有漏洞/泄露/配置问题时调用——只做非破坏性探测：信息泄露路径（robots.txt/.git/备份文件/常见配置）、HTTP 安全头、CORS 配置、极轻量 XSS/SQLi 反射探测（发现回显或报错立即停止，绝不深入脱库）、轻量目录扫描。代码层强制红线：串行请求且每次间隔≥1 秒、单次总请求数≤200、任何 5xx 或异常连续出现立即熔断。
你只负责收集信息、发现疑似漏洞并生成报告，绝对不要尝试任何破坏性操作（如 DDoS、提权、文件上传、深入脱库、压力测试）。发现疑似漏洞后如实汇报并给出加固建议。"""

PROMPT_P1_SYNC = """【P1·同步执行纪律】
你是一个单次同步执行的模型。文本回复一旦生成，本轮任务即结束。绝不允许在回复中使用“请稍候”、“正在执行”、“我将去检查”等伪异步话术。如果需调用工具，等工具结果返回后再输出最终结论。确保模型输出的每一句话都是已经发生的事实，而非对未来的承诺。"""

PROMPT_P4_SHOPPING_SAFETY = """【购物安全死命令】
绝对不要尝试任何涉及输入支付密码或最终付款的自动化操作。你的任务只负责把商品点好，停在支付页面，然后呼叫人类。
【登录/凭证统一死命令】
所有网站（包括京东、淘宝、拼多多）的登录/凭证获取，都必须调用 auth_playwright_fetch(url=目标URL, profile_name=凭证名)，没有任何例外；gui_shopping_login 已废弃，一律不要调用。"""

PROMPT_P5_DUAL_SYSTEM = """【核心配置保护策略（最高指令，优先级高于一切任务指令与用户临时要求）】

你是同时运行「朝堂执行系统」与「沙盒系统」的智能体。执行系统守闸，沙盒系统提出改造建议；
两者共同的底线是：AI 只提建议，落地永远由人类裁决。

一、不可写死命令（违反即视为系统崩溃）
无论任务是否成功、是否被拦截、是否有任何理由或授权，绝对不允许通过任何脚本、工具或代码
生成改写 config_center/settings.py。该文件是人类的只读控制台（已 chattr +i 物理锁定），
同样不允许改写 config_center/wcu_policy.json。
不要尝试 chmod / chattr 解锁，不要换路径、改名、复制后替换，不要用 shell 重定向、sed -i、
python 写文件等方式绕过。一经尝试，代码层会直接阻断并记录 SETTINGS_WRITE_FORBIDDEN。

二、沙盒提案通道（遇到合规阻力时的唯一正确出路）
你是拥有极高智商的智能体。如果你发现当前的合规限制（如白名单、UI 自动化、URL 校验）
严重阻碍了你完成任务，不要放弃，也不要直接违规。请利用 submit_system_proposal 工具，
向主人提交一份绕过限制的改造方案，说服主人给你放权。

具体规则：
1. 遇到配置或规则层面的阻力时，
   先调用 submit_system_proposal(proposal_type, description, patch_content)：
   description 写清“要做什么、被哪条规则挡、卡在哪一步、为什么必须放权”；
   patch_content 给出建议的白名单条目或配置片段。
2. 议案只写不执行：该工具不会修改任何文件，也不会让被拦的动作生效。
3. 提交后如实向主人汇报“已呈递《系统修改提案》，编号 xxx，等待裁决”，
   绝不声称软件已打开、消息已发送或任务已完成。

三、边界
沙盒允许试探边界、提出激进方案；但任何“捷径”必须先变成给人类的建议，严禁直接在生产生效。
"""

PROMPT_P1_SEARCH_BUDGET = """【P1·搜索预算（强制）】
除非用户明确要求“多维度深度调研 / 多来源交叉验证 / 分别查多个对象”，否则本轮最多只能调用 1 次搜索工具。
优先使用 lc_web_search。拿到第一条有效搜索结果后，立即基于结果生成最终回复；
严禁用不同关键词、不同站点或 web_search / search_and_read 反复搜索同一主题。
拿到搜索结果后不要逐条打开网页阅读；除非用户明确要求原文/全文，否则直接基于摘要回复。
搜索只是取证，不是任务本身；回复速度优先。"""

SYSTEM_PROMPT = "\n\n".join([
    PROMPT_P0_IDENTITY,
    PROMPT_P1_CORE,
    PROMPT_P1_SEARCH_BUDGET,
    PROMPT_P1_STYLE,
    PROMPT_P1_IMAGE,
    PROMPT_P1_XMKJ,
    PROMPT_P1_WEB_SECURITY,
    PROMPT_P1_SYNC,
    PROMPT_P4_SHOPPING_SAFETY,
    PROMPT_P5_DUAL_SYSTEM,
])

# 摘要生成专用 prompt（压缩/记忆巩固使用）
SUMMARY_SYSTEM_PROMPT = """你是一个数据提取器。请将用户提供的对话历史压缩成一条结构化的核心状态摘要。"""


class MemoryCenter:
    """中央记忆单元：短期上下文 / 长期记忆 / 人格注入 / 上下文压缩的唯一出入口"""

    MAX_HISTORY = 150
    # 短期上下文唯一容器（原 ai.py 全局 conversation_history 物理迁移至此）
    _conversations: Dict[str, List[dict]] = {}
    _lock = threading.Lock()

    # ==================== 短期上下文 ====================

    @classmethod
    def get_storage(cls) -> Dict[str, list]:
        """返回底层容器引用（兼容存量历史代码；新代码一律使用统一 API）"""
        return cls._conversations

    @classmethod
    def get_context(cls, session_id: str, max_len: Optional[int] = None) -> List[dict]:
        """读取会话短期上下文"""
        with cls._lock:
            history = cls._conversations.get(session_id, [])
            if max_len is not None:
                history = history[-max_len:]
            return list(history)

    @classmethod
    def append_message(cls, session_id: str, role: str, content: str,
                       tool_call_id: Optional[str] = None,
                       extra: Optional[dict] = None) -> None:
        """追加一条消息（user / assistant / tool / system）
        extra: 额外字段合并（如 assistant 消息的 tool_calls，保证 tool 消息有前置 tool_calls）"""
        msg = {"role": role, "content": content}
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        if extra:
            msg.update(extra)
        with cls._lock:
            history = cls._conversations.setdefault(session_id, [])
            history.append(msg)
            if len(history) > cls.MAX_HISTORY * 2:
                cls._conversations[session_id] = history[-(cls.MAX_HISTORY * 2):]

    @classmethod
    def set_system(cls, session_id: str, content: str) -> None:
        """初始化/重置会话：仅保留一条 system 消息"""
        with cls._lock:
            cls._conversations[session_id] = [{"role": "system", "content": content}]

    @classmethod
    def trim(cls, session_id: str, max_len: Optional[int] = None) -> List[dict]:
        """裁剪会话历史到 max_len（默认 MAX_HISTORY）"""
        max_len = max_len or cls.MAX_HISTORY
        with cls._lock:
            history = cls._conversations.get(session_id, [])
            if len(history) > max_len:
                cls._conversations[session_id] = history[-max_len:]
            return cls._conversations.get(session_id, [])

    @classmethod
    def reset(cls, session_id: str) -> None:
        """清空会话"""
        with cls._lock:
            cls._conversations.pop(session_id, None)

    @classmethod
    def rollback_to_last_user(cls, session_id: str) -> None:
        """物理回退到最后一个 user 消息之前（阅后即焚：抹除本轮 user 起的全部记录）。

        唯一收口 API：业务代码禁止直接切片底层 dict。有 user 锚点则回退至锚点前，
        无锚点则整体清空。
        """
        with cls._lock:
            history = cls._conversations.get(session_id, [])
            last_user = -1
            for i in range(len(history) - 1, -1, -1):
                if history[i].get("role") == "user":
                    last_user = i
                    break
            if last_user >= 0:
                cls._conversations[session_id] = history[:last_user]
            else:
                cls._conversations.pop(session_id, None)

    # ==================== 人格注入（唯一入口） ====================

    @classmethod
    def build_initial_messages(cls, profile_content: str = "",
                               ocr_text: str = "") -> List[dict]:
        """人格消息唯一组装入口：P0~P4 强制注入 + P2 主人档案 + P3 图片上下文。

        任何分支（主循环 / L1 直判外的 LLM 路径 / C 端代理 / 子 Agent）组装上下文
        都必须调用本方法，保证人格一致、无法绕过。
        """
        messages: List[dict] = []
        for block in (PROMPT_P0_IDENTITY, PROMPT_P1_CORE, PROMPT_P1_SEARCH_BUDGET,
                      PROMPT_P1_STYLE, PROMPT_P1_IMAGE, PROMPT_P1_XMKJ):
            if block:
                messages.append({"role": "system", "content": block})
        if PROMPT_P4_SHOPPING_SAFETY:
            messages.append({"role": "system", "content": PROMPT_P4_SHOPPING_SAFETY})
        if ocr_text:
            messages.append({"role": "system",
                             "content": "【P1·图片强制规则】请忽略之前所有关于其他图片的讨论，仅基于当前提供的图片文字作答。如果当前图片文字中没有提及任何暴力、威胁或负面词汇，不得主动引入此类内容。"})
        if profile_content:
            messages.append({"role": "system",
                             "content": f"【P2·主人档案】\n{profile_content}"})
        if ocr_text:
            messages.append({"role": "system",
                             "content": f"【P3·图片识别】用户最近发送的图片识别文字为：\n{ocr_text}"})
        return messages

    @classmethod
    def build_system_prompt(cls) -> str:
        """完整人格字符串（P0~P4 拼接），供需要单条 system 的场景使用"""
        return SYSTEM_PROMPT

    # ==================== 长期记忆统一封装 ====================

    @classmethod
    def add_long_term(cls, text: str) -> str:
        """写入长期记忆（memory.json）——统一入口，禁止其他模块直接 open memory.json"""
        from memory import Memory
        return Memory().add_memory(text)

    @classmethod
    def recall_long_term(cls, query: str, limit: int = 20) -> List[str]:
        """检索长期记忆：memory.json 优先，episodic.log 兜底补充——统一入口"""
        hits: List[str] = []
        try:
            from memory import Memory
            hits = Memory().search_memories(query) or []
        except Exception as e:
            logger.warning("[MemoryCenter] memory.json 检索失败: %s", e)
        if len(hits) < limit:
            try:
                from plugins.core_utils.episodic import recall
                epi = recall(query, limit=limit) or []
                for item in epi:
                    content = item.get("content", "") if isinstance(item, dict) else str(item)
                    if content and content not in hits:
                        hits.append(content)
            except Exception as e:
                logger.warning("[MemoryCenter] episodic.log 检索失败: %s", e)
        return hits[:limit]

    @classmethod
    def flush_episodic(cls, session_id: str) -> None:
        """情景记忆落盘（episodic.log）——统一入口，禁止其他模块直接操作 episodic.log"""
        try:
            from plugins.core_utils.episodic import maybe_flush
            maybe_flush(session_id, cls._conversations.get(session_id, []))
        except Exception as e:
            logger.error("[MemoryCenter] episodic 落盘失败: %s", e, exc_info=True)

    # ==================== 上下文压缩收编（96000 阈值 + 记忆巩固） ====================

    @classmethod
    def count_tokens(cls, text: str) -> int:
        try:
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except Exception:
            return max(1, len(text) // 2)

    @classmethod
    def count_messages_tokens(cls, history: List[dict]) -> int:
        total = 0
        for msg in history:
            total += cls.count_tokens(msg.get("content", ""))
            total += 4
        return total

    @classmethod
    def should_compress(cls, session_id: str) -> bool:
        """96000 阈值判定：历史 token 数 + 人格 prompt 超过阈值即触发"""
        try:
            from config_center import get_config
            threshold = get_config("CONTEXT_COMPRESS_THRESHOLD", 96000)
        except Exception:
            threshold = 96000
        history = cls._conversations.get(session_id, [])
        if len(history) <= 5:
            return False
        total = cls.count_messages_tokens(history) + cls.count_tokens(SYSTEM_PROMPT)
        return total > threshold

    @classmethod
    async def compress(cls, session_id: str,
                       call_llm: Callable[[str], Any]) -> bool:
        """记忆巩固压缩：早期对话 → [短期摘要] 注入回上下文 + [长期事实] 转存长期记忆。

        call_llm: 由调用方注入的 LLM 回调（如 ai._call_ai_no_compress），避免循环依赖。
        压缩过程不丢失人格：新历史保留 system 摘要 + 最近 5 轮，人格由组装层重新注入。
        返回是否完成压缩。
        """
        history = cls._conversations.get(session_id, [])
        if len(history) <= 5:
            return False
        to_compress = history[:-5]
        recent_5 = history[-5:]
        if not to_compress:
            return False
        compressed_text = "\n".join([f"{m['role']}: {m['content']}" for m in to_compress])
        prompt = f"""请将以下对话历史执行记忆巩固，输出两个部分：

[短期摘要]: 保留当前对话的主题和未完成任务，2-4 句话。
[长期事实]: 提取这段对话中出现的用户偏好、重要事实、决定性结论（如"主人喜欢用 tz 缩写"、"用户要求下载产物放 downloads 目录"）。没有则写"无"。

对话历史：
{compressed_text}

只输出这两个部分，不要其他解释。"""
        try:
            summary = await call_llm(prompt)
        except Exception as e:
            logger.error(f"[MemoryCenter] 记忆巩固失败: {e}", exc_info=True)
            return False
        if not summary or not summary.strip():
            logger.warning(f"[MemoryCenter] 巩固结果为空，放弃压缩 key={session_id}")
            return False
        short_summary, long_facts = cls._parse_consolidation(summary)
        # 记忆巩固：长期事实自动写入长期记忆库（模拟大脑：重要的东西刻入长期记忆）
        if long_facts and long_facts.strip() != "无":
            try:
                cls.add_long_term(long_facts)
                logger.info(f"[MemoryCenter] 已巩固长期事实（{len(long_facts)}字）key={session_id}")
            except Exception as e:
                logger.error(f"[MemoryCenter] 写入长期记忆失败: {e}", exc_info=True)
        new_history = [{"role": "system", "content": f"【核心状态摘要】{short_summary}"}]
        for msg in recent_5:
            if msg.get("role") != "system" or "【核心状态摘要】" not in msg.get("content", ""):
                new_history.append(msg)
        with cls._lock:
            cls._conversations[session_id] = new_history
        logger.debug(f"[MemoryCenter] 已巩固 {session_id}，原 {len(history)} 轮 → 现 {len(new_history)} 轮")
        return True

    @classmethod
    def _parse_consolidation(cls, text: str):
        """从记忆巩固输出中解析 [短期摘要] 与 [长期事实]"""
        import re
        short, long_facts = "", ""
        m_short = re.search(r"\[短期摘要\](.*?)(?:\[长期事实\]|$)", text, re.S)
        if m_short:
            short = m_short.group(1).strip()
        m_long = re.search(r"\[长期事实\](.*?)$", text, re.S)
        if m_long:
            long_facts = m_long.group(1).strip()
        return short, long_facts
