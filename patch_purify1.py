# -*- coding: utf-8 -*-
"""patch_purify1: ai.py 接入 Central Memory Center + 人格/压缩收编 + L1/C端失忆修复 + 购物工具通用化
一次性补丁：重复运行会因锚点缺失而退出，不会重复修改。
"""
import io, re, sys

P = '/root/robot/ai.py'
src = io.open(P, encoding='utf-8').read()
orig = src


def must_rep(old, new, tag):
    global src
    if old not in src:
        print("[FAIL] 锚点缺失: %s" % tag)
        sys.exit(1)
    src = src.replace(old, new, 1)
    print("[OK] %s" % tag)


def must_re_sub(pattern, repl, tag, flags=re.S):
    global src
    new_src, n = re.subn(pattern, repl, src, count=1, flags=flags)
    if n == 0:
        print("[FAIL] 正则未命中: %s" % tag)
        sys.exit(1)
    src = new_src
    print("[OK] %s" % tag)


# ========== 1) import memory_center ==========
must_rep(
    "from memory import Memory, load_profile",
    "from memory import Memory, load_profile\nfrom memory_center import MemoryCenter, SUMMARY_SYSTEM_PROMPT",
    "import memory_center")

# ========== 2) conversation_history 物理接管（唯一容器归 MemoryCenter） ==========
must_rep(
    "conversation_history = {}",
    "conversation_history = MemoryCenter.get_storage()   # 短期上下文物理归 MemoryCenter 持有（唯一容器）",
    "conversation_history 物理接管")

# ========== 3) 删除 SUMMARY_SYSTEM_PROMPT 定义（memory_center 已持有） ==========
must_re_sub(
    r'SUMMARY_SYSTEM_PROMPT = """.*?"""\n',
    '',
    "删除 SUMMARY_SYSTEM_PROMPT 定义")

# ========== 4) 删除人格常量段（P0/P1/P4/SYSTEM_PROMPT/SYSTEM_TOKEN_COUNT，唯一持有迁移 memory_center） ==========
must_re_sub(
    r'# ==================== 主提示词分段常量（P0/P1） ====================\n.*?SYSTEM_TOKEN_COUNT = count_tokens\(SYSTEM_PROMPT\)\n',
    '# ===== 人格常量（P0~P4）已迁移 memory_center（MemoryCenter 唯一持有与注入） =====\n',
    "删除人格常量段")

# ========== 5) 删除压缩判定函数（收编 memory_center） ==========
must_re_sub(
    r'# ========== 压缩逻辑 ==========\ndef should_compress\(key: str\) -> bool:\n.*?return total_tokens > get_config\("CONTEXT_COMPRESS_THRESHOLD"\)\n',
    '# ===== 压缩判定/记忆巩固已收编 memory_center（should_compress/compress） =====\n',
    "删除 should_compress")

# ========== 6) 删除 compress_history + _parse_consolidation（收编 memory_center） ==========
must_re_sub(
    r'async def compress_history\(key: str\) -> None:\n.*?logger\.debug\(f"已巩固 \{key\} 的对话历史，原 \{len\(history\)\} 轮 → 现 \{len\(new_history\)\} 轮"\)\n',
    '# ===== compress_history 已收编 memory_center（MemoryCenter.compress） =====\n',
    "删除 compress_history")

# ========== 7) 删除 _parse_consolidation（memory_center 已持有） ==========
must_re_sub(
    r'def _parse_consolidation\(text: str\) -> tuple:\n.*?return short, long_facts\n',
    '',
    "删除 _parse_consolidation")

# ========== 8) 主循环人格组装 → MemoryCenter.build_initial_messages（P0~P4 唯一注入，补上 P4 死命令） ==========
must_rep(
    """    messages = []
    # ===== P0：身份 =====
    if PROMPT_P0_IDENTITY:
        messages.append({"role": "system", "content": PROMPT_P0_IDENTITY})
    # ===== P1：规则 =====
    if PROMPT_P1_CORE:
        messages.append({"role": "system", "content": PROMPT_P1_CORE})
    if PROMPT_P1_STYLE:
        messages.append({"role": "system", "content": PROMPT_P1_STYLE})
    if PROMPT_P1_IMAGE:
        messages.append({"role": "system", "content": PROMPT_P1_IMAGE})
    if PROMPT_P1_XMKJ:
        messages.append({"role": "system", "content": PROMPT_P1_XMKJ})
    if ocr_text:
        messages.append({"role": "system", "content": "【P1·图片强制规则】请忽略之前所有关于其他图片的讨论，仅基于当前提供的图片文字作答。如果当前图片文字中没有提及任何暴力、威胁或负面词汇，不得主动引入此类内容。"})
    # ===== P2：主人档案 =====
    if PROFILE_CONTENT:
        messages.append({"role": "system", "content": f"【P2·主人档案】\\n{PROFILE_CONTENT}"})
    # ===== P3：上下文 =====
    if ocr_text:
        messages.append({"role": "system", "content": f"【P3·图片识别】用户最近发送的图片识别文字为：\\n{ocr_text}"})""",
    """    # ===== 人格注入（P0~P4 + P2 档案 + P3 图片上下文）唯一入口：MemoryCenter.build_initial_messages =====
    messages = MemoryCenter.build_initial_messages(PROFILE_CONTENT, ocr_text)""",
    "主循环人格组装收编")

# ========== 9) 前置处理（ask_ai 主循环）→ MemoryCenter API ==========
must_rep(
    """    # ----- 前置处理（历史、压缩、记忆） -----
    history = conversation_history.get(key, [])[-(MAX_HISTORY * 2):]
    if should_compress(key):
        await compress_history(key)
        history = conversation_history.get(key, [])[-(MAX_HISTORY * 2):]
    if len(history) > MAX_HISTORY:
        conversation_history[key] = history[-MAX_HISTORY:]
        history = conversation_history[key]""",
    """    # ----- 前置处理（历史、压缩、记忆）统一走 MemoryCenter -----
    history = MemoryCenter.get_context(key, MAX_HISTORY * 2)
    if MemoryCenter.should_compress(key):
        await MemoryCenter.compress(key, call_llm=lambda _p: _call_ai_no_compress(key, _p))
        history = MemoryCenter.get_context(key, MAX_HISTORY * 2)
    if len(history) > MAX_HISTORY:
        MemoryCenter.trim(key, MAX_HISTORY)
        history = MemoryCenter.get_context(key)""",
    "ask_ai 前置处理收编 MemoryCenter")

# ========== 10) get_ai_response 前置处理 → MemoryCenter API ==========
must_rep(
    """    history = conversation_history.get(key, [])[-(MAX_HISTORY * 2):]
    if should_compress(key):
        await compress_history(key)
        history = conversation_history.get(key, [])[-(MAX_HISTORY * 2):]
    if len(history) > MAX_HISTORY:
        conversation_history[key] = history[-MAX_HISTORY:]
        history = conversation_history[key]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)""",
    """    history = MemoryCenter.get_context(key, MAX_HISTORY * 2)
    if MemoryCenter.should_compress(key):
        await MemoryCenter.compress(key, call_llm=lambda _p: _call_ai_no_compress(key, _p))
        history = MemoryCenter.get_context(key, MAX_HISTORY * 2)
    if len(history) > MAX_HISTORY:
        MemoryCenter.trim(key, MAX_HISTORY)
        history = MemoryCenter.get_context(key)

    messages = MemoryCenter.build_initial_messages(PROFILE_CONTENT)
    messages.extend(history)""",
    "get_ai_response 前置处理+人格收编")

# ========== 11) 长期记忆注入 → MemoryCenter.recall_long_term（统一封装） ==========
must_rep(
    "    matched_memories = memory.search_memories(prompt)",
    "    matched_memories = MemoryCenter.recall_long_term(prompt)",
    "长期记忆检索收编 MemoryCenter")

# ========== 12) 非工具路径收尾 → MemoryCenter.append_message + flush_episodic ==========
must_rep(
    """    # ===== 非工具路径：保存本轮对话到历史 =====
    conversation_history[key].append({"role": "user", "content": prompt})
    conversation_history[key].append({"role": "assistant", "content": reply})

    if len(conversation_history[key]) > MAX_HISTORY * 2:
        conversation_history[key] = conversation_history[key][-(MAX_HISTORY * 2):]

    # ===== 情景记忆落盘检查 =====
    try:
        from plugins.core_utils.episodic import maybe_flush
        maybe_flush(key, conversation_history[key])
    except Exception as e:
        logger.error(f"[episodic] 落盘检查失败: {e}", exc_info=True)""",
    """    # ===== 非工具路径：保存本轮对话到历史（MemoryCenter 唯一出入口） =====
    MemoryCenter.append_message(key, "user", prompt)
    MemoryCenter.append_message(key, "assistant", reply)
    MemoryCenter.trim(key, MAX_HISTORY * 2)

    # ===== 情景记忆落盘（MemoryCenter 统一封装） =====
    MemoryCenter.flush_episodic(key)""",
    "非工具路径收尾收编")

# ========== 13) 工具路径收尾 → MemoryCenter.append_message + flush_episodic ==========
must_rep(
    """        else:
            conversation_history[key].append({"role": "assistant", "content": final_reply})

            if len(conversation_history[key]) > MAX_HISTORY * 2:
                conversation_history[key] = conversation_history[key][-(MAX_HISTORY * 2):]

        # ===== 情景记忆落盘检查 =====
        try:
            from plugins.core_utils.episodic import maybe_flush
            maybe_flush(key, conversation_history[key])
        except Exception as e:
            logger.error(f"[episodic] 落盘检查失败: {e}", exc_info=True)""",
    """        else:
            MemoryCenter.append_message(key, "assistant", final_reply)
            MemoryCenter.trim(key, MAX_HISTORY * 2)

        # ===== 情景记忆落盘（MemoryCenter 统一封装） =====
        MemoryCenter.flush_episodic(key)""",
    "工具路径收尾收编")

# ========== 14) L1 短路失忆断层修复：return 前完整写入 User + Assistant ==========
must_rep(
    """            _final = _l1_verdict_text(_sc_res)
            logger.info("[L1短路] 完成，代码层直判结果（已切断 LLM 总结幻觉空间，0 次总结 API）")
            return sanitize_output(_final)""",
    """            _final = _l1_verdict_text(_sc_res)
            logger.info("[L1短路] 完成，代码层直判结果（已切断 LLM 总结幻觉空间，0 次总结 API）")
            # [失忆断层修复] L1 短路也必须完整写入全局上下文，确保下一轮无缝衔接
            MemoryCenter.append_message(key, "user", prompt)
            MemoryCenter.append_message(key, "assistant", _final)
            MemoryCenter.flush_episodic(key)
            return sanitize_output(_final)""",
    "L1 短路 append_message（失忆修复）")

# ========== 15) 知识库 / 记住 直返分支也补 append（失忆断层） ==========
must_rep(
    """        results = knowledge_db.search_solutions(keyword, limit=5)
        if not results:
            return f"未找到与「{keyword}」相关的历史记录"
        lines = [f"找到 {len(results)} 条相关历史记录："]
        for r in results:
            lines.append(f"- 问题：{r['problem']}")
            lines.append(f"  方案：{r['solution'][:200]}...")
            lines.append(f"  时间：{r['created_at']}")
        return "\\n".join(lines)""",
    """        results = knowledge_db.search_solutions(keyword, limit=5)
        if not results:
            _kb_reply = f"未找到与「{keyword}」相关的历史记录"
            MemoryCenter.append_message(key, "user", prompt)
            MemoryCenter.append_message(key, "assistant", _kb_reply)
            return _kb_reply
        lines = [f"找到 {len(results)} 条相关历史记录："]
        for r in results:
            lines.append(f"- 问题：{r['problem']}")
            lines.append(f"  方案：{r['solution'][:200]}...")
            lines.append(f"  时间：{r['created_at']}")
        _kb_reply = "\\n".join(lines)
        MemoryCenter.append_message(key, "user", prompt)
        MemoryCenter.append_message(key, "assistant", _kb_reply)
        return _kb_reply""",
    "知识库分支 append_message")

must_rep(
    '''            if memory_text:
                result = memory.add_memory(memory_text)
                return result
        return "请告诉我要记住什么内容"''',
    '''            if memory_text:
                result = memory.add_memory(memory_text)
                MemoryCenter.append_message(key, "user", prompt)
                MemoryCenter.append_message(key, "assistant", result)
                return result
        _mem_reply = "请告诉我要记住什么内容"
        MemoryCenter.append_message(key, "user", prompt)
        MemoryCenter.append_message(key, "assistant", _mem_reply)
        return _mem_reply''',
    "记住分支 append_message")

# ========== 16) sub_agent：conversation_history 直写 → MemoryCenter ==========
must_rep(
    """    conversation_history[key] = [{"role": "system", "content": system_prompt}]""",
    """    MemoryCenter.set_system(key, system_prompt)""",
    "sub_agent set_system")

must_rep(
    """            result = await execute_tool_directly(tool_name, tool_args, user_id)
            conversation_history[key].append({
                "role": "tool",
                "tool_call_id": tc.get("id", f"step_{step}"),
                "content": result[:500]
            })""",
    """            result = await execute_tool_directly(tool_name, tool_args, user_id)
            MemoryCenter.append_message(key, "tool", result[:500],
                                         tool_call_id=tc.get("id", f"step_{step}"))""",
    "sub_agent tool append")

# ========== 17) C 端代理人格注入（防绕过） ==========
must_rep(
    """    lines = [
        "你是「工具决策模块」，运行在一个可以调用外部工具的系统里。",""",
    """    lines = [
        MemoryCenter.build_system_prompt(),
        "",
        "你是「工具决策模块」，运行在一个可以调用外部工具的系统里。",""",
    "C 端代理人格注入")

# ========== 18) gui_shopping_assistant 伪通用净化：platform 白名单 → 通用 action+url ==========
must_rep(
    """    async def gui_shopping_assistant(platform: str = "jd", query: str = "", quantity: int = 1) -> str:
        \"\"\"半自动购物助手：Windows 宿主有头 Edge 自动加购并停在订单确认页。\"\"\"
        _platform = (platform or "jd").strip().lower()
        if _platform not in ("jd", "meituan"):
            # [Tool Boundary] 平台边界锁死：抛明确业务异常，禁止返回"不支持"普通串（防技能库误查）
            raise AUPError("UNSUPPORTED_PLATFORM", f"平台 {_platform} 暂不支持购物宏工具")""",
    """    async def gui_shopping_assistant(action: str = "search", url: str = "", query: str = "", quantity: int = 1) -> str:
        \"\"\"半自动购物助手（通用动作 + URL）：Windows 宿主有头 Edge 打开任意购物站点 URL，
        页面定位由 DOM 文本判断（不写死任何平台选择器），自动执行动作并停在订单确认页。\"\"\"
        _action = (action or "search").strip().lower()
        if _action not in ("search", "add_to_cart", "checkout"):
            # [Tool Boundary] 动作边界：只接受通用动作，页面定位交给 LLM/DOM 文本判断
            raise AUPError("UNSUPPORTED_ACTION", f"动作 {_action} 暂不支持（仅支持 search/add_to_cart/checkout）")
        if not url:
            raise AUPError("MISSING_URL", "gui_shopping_assistant 必须提供目标站点 URL（页面定位由 DOM 文本判断）")
        _platform = "generic" """,
    "gui_shopping_assistant 通用签名")

# ========== 19) gui_shopping_assistant 调用脚本参数：platform → url+action ==========
must_rep(
    """            _proc = _sp.run(
                [_win_py, _script, _platform, str(query), str(_qty)],
                capture_output=True, text=True, timeout=200,
            )""",
    """            _proc = _sp.run(
                [_win_py, _script, _action, url, str(query), str(_qty)],
                capture_output=True, text=True, timeout=200,
            )""",
    "gui_shopping_assistant 传参 action+url")

# ========== 20) gui_shopping_assistant 结果行：platform 字段回退 ==========
must_rep(
    """        _lines = [f"平台: {_res.get('platform', _platform)} | 状态: {_res.get('status', 'unknown')}"]""",
    """        _lines = [f"站点: {_res.get('platform', _platform)} | 动作: {_action} | 状态: {_res.get('status', 'unknown')}"]""",
    "gui_shopping_assistant 结果行")

# ========== 21) gui_shopping_assistant 注册描述：通用动作+URL ==========
must_rep(
    """            description="【购物下单专用】当用户要求买/购买/下单/加购物车/去结算/停在支付页面等购物操作时，必须调用本工具。半自动购物助手：Windows 宿主有头 Edge 自动搜索→加购→去结算→停在订单确认页截图，交由人类扫码支付；AI 绝不碰支付密码/最终付款。每次执行会先强制清空购物车（保证订单里只有本次目标商品）；自带登录态检测，登录失效时返回 login_expired——此时请主人确认后改用 auth_playwright_fetch(url=当前页面URL, profile_name=shop_profile) 弹窗扫码登录，不要预先登录。",""",
    """            description="【购物下单专用】当用户要求买/购买/下单/加购物车/去结算/停在支付页面等购物操作时，必须调用本工具。半自动购物助手：接受通用动作 search/add_to_cart/checkout 与目标站点 URL，Windows 宿主有头 Edge 打开 URL，页面定位（搜索框/加购/结算按钮）由 DOM 文本判断，绝不写死任何平台选择器；自动执行动作并停在订单确认页截图，交由人类扫码支付；AI 绝不碰支付密码/最终付款。每次执行会先强制清空购物车（保证订单里只有本次目标商品）；自带登录态检测，登录失效时返回 login_expired——此时请主人确认后改用 auth_playwright_fetch(url=当前页面URL, profile_name=shop_profile) 弹窗扫码登录，不要预先登录。",""",
    "gui_shopping_assistant 通用描述")

io.open(P, 'w', encoding='utf-8').write(src)
print("PATCH1_DONE lines=%d -> %d" % (len(orig.splitlines()), len(src.splitlines())))
