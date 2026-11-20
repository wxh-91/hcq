# -*- coding: utf-8 -*-
"""patch_purify3: ai.py 剩余购物硬编码净化 + admin.py 记忆收编"""
import io, re, sys

# ========== ai.py ==========
PA = '/root/robot/ai.py'
src = io.open(PA, encoding='utf-8').read()


def must_rep(old, new, tag):
    global src
    if old not in src:
        print("[FAIL] ai.py 锚点缺失: %s" % tag)
        sys.exit(1)
    src = src.replace(old, new, 1)
    print("[OK] ai.py: %s" % tag)


# 1) gui_shopping_assistant @_aup_tool 注册描述 → 通用动作+URL
must_rep(
    '''        description="【购物下单专用】当用户要求买/购买/下单/加购物车/去结算/停在支付页面等购物操作时，必须调用本工具。半自动购物助手：在 Windows 宿主弹出真实 Edge 窗口，自动完成搜索→加购物车→去结算，停在订单确认页截图并交由人类扫码支付。AI 绝不触碰支付密码或最终付款。参数：platform(jd/meituan，可省略，默认 jd)、query(商品关键词)、quantity(数量,默认1)。",
        category="shopping",
        is_danger=True,
        params_schema={
            "required": ["query"],
            "properties": {
                "platform": {"type": "string", "description": "平台名：jd 或 meituan，省略时默认 jd"},
                "query": {"type": "string", "description": "要购买的商品关键词，如：可乐"},
                "quantity": {"type": "integer", "description": "购买数量，默认 1"}
            },
        },''',
    '''        description="【购物下单专用】当用户要求买/购买/下单/加购物车/去结算/停在支付页面等购物操作时，必须调用本工具。半自动购物助手：接受通用动作 search/add_to_cart/checkout 与目标站点 URL，Windows 宿主有头 Edge 打开 URL，页面定位（搜索框/加购/结算按钮）由 DOM 文本判断，绝不写死任何平台选择器；自动执行动作并停在订单确认页截图，交由人类扫码支付；AI 绝不碰支付密码/最终付款。参数：action(通用动作：search/add_to_cart/checkout，默认 search)、url(目标购物站点URL，必填)、query(商品关键词)、quantity(数量,默认1)。",
        category="shopping",
        is_danger=True,
        params_schema={
            "required": ["url"],
            "properties": {
                "action": {"type": "string", "description": "通用动作：search / add_to_cart / checkout，默认 search"},
                "url": {"type": "string", "description": "目标购物站点 URL（必填），页面定位由 DOM 文本判断"},
                "query": {"type": "string", "description": "要购买的商品关键词，如：可乐"},
                "quantity": {"type": "integer", "description": "购买数量，默认 1"}
            },
        },''',
    "@_aup_tool 通用描述+params_schema")

# 2) C 端代理降级段：platform="jd" 硬编码 → 站点注册表匹配 URL + 通用动作
must_rep(
    """    if tool_name == "gui_shopping_login" and _looks_like_shopping(user_prompt):
        _q = _extract_shopping_query(user_prompt)
        logger.info("[购物路由] 拦截误选 gui_shopping_login → 重定向 gui_shopping_assistant (query=%s)", _q)
        tool_name = "gui_shopping_assistant"
        args = {"platform": "jd", "query": _q}""",
    """    if tool_name == "gui_shopping_login" and _looks_like_shopping(user_prompt):
        _q = _extract_shopping_query(user_prompt)
        _site_url = ""
        for _site, (_url, _prof) in _L1_SITES.items():
            if _site in (user_prompt or ""):
                _site_url = _url
                break
        logger.info("[购物路由] 拦截误选 gui_shopping_login → 重定向 gui_shopping_assistant (query=%s url=%s)", _q, _site_url)
        tool_name = "gui_shopping_assistant"
        args = {"action": "search", "url": _site_url, "query": _q}""",
    "降级段通用化（站点注册表 URL）")

# 3) gui_shopping_login @_aup_tool 描述 → 通用凭证/已废弃（防平台清单写死）
must_rep(
    '''        description="【京东扫码登录专用】仅在两种情况下调用本工具：①主人明确发送“帮我扫码登录京东”/“登录京东”/“扫码登录”等登录指令；②gui_shopping_assistant 返回 login_expired 提示登录失效后，主人确认重新登录。常规购物请求不要预先调用本工具（gui_shopping_assistant 自带登录态检测与强制清空购物车）。调用后弹出 Windows Edge 窗口打开京东登录页，等待主人手机扫码登录，登录态自动保存到 shop_profile（等待最长 5 分钟）。",''',
    '''        description="【已废弃，勿调用】任何登录/存凭证意图一律使用通用凭证工具 auth_playwright_fetch(url=目标URL, profile_name=凭证名)，与站点无关。本工具（gui_shopping_login）仅保留兼容：仅在历史场景触发时弹出 Edge 窗口等待扫码（登录态自动保存到 shop_profile，等待最长 5 分钟）。常规购物请求不要预先调用本工具。",''',
    "gui_shopping_login 通用/废弃描述")

io.open(PA, 'w', encoding='utf-8').write(src)
print("AI_PATCH3_DONE")

# ========== admin.py ==========
PB = '/root/robot/admin.py'
srcb = io.open(PB, encoding='utf-8').read()


def must_rep_b(old, new, tag):
    global srcb
    if old not in srcb:
        print("[FAIL] admin.py 锚点缺失: %s" % tag)
        sys.exit(1)
    srcb = srcb.replace(old, new, 1)
    print("[OK] admin.py: %s" % tag)


# admin: episodic recall 收编 MemoryCenter（统一长期记忆出入口）
must_rep_b(
    "from plugins.core_utils.episodic import recall",
    "from memory_center import MemoryCenter   # 长期记忆统一出入口（episodic.log 不再直接操作）",
    "import MemoryCenter")

must_rep_b(
    """    results = recall(keyword, limit=20)""",
    """    results = MemoryCenter.recall_long_term(keyword, limit=20)""",
    "recall → MemoryCenter.recall_long_term")

io.open(PB, 'w', encoding='utf-8').write(srcb)
print("ADMIN_PATCH3_DONE")
