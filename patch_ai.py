# -*- coding: utf-8 -*-
"""给 /root/robot/ai.py 打入 EHIR 漏斗补丁：
1) 原 _detect_intent 改名 _llm_detect_intent（逻辑不动）
2) 在其前插入新的三层漏斗版 _detect_intent + _learn_tool_word
先备份 ai.py.bak_ehir，再做精确字符串替换；失败即中止不写回。
"""
import shutil
import sys

AIPY = "/root/robot/ai.py"
BAK = "/root/robot/ai.py.bak_ehir"

with open(AIPY, "r", encoding="utf-8") as f:
    src = f.read()

OLD = (
    "async def _detect_intent(prompt: str, context_messages: list = None) -> dict:\n"
    "    \"\"\"\n"
    "    用 chat 策略模型判断用户意图。\n"
)

NEW_FUNNEL = (
    "async def _detect_intent(prompt: str, context_messages: list = None) -> dict:\n"
    "    \"\"\"EHIR 三层漏斗入口：\n"
    "    L1 静态正则预判（极速拦截，0 API）→ L3 触发检查（长消息/因果词强制大模型）\n"
    "    → L2 动态词库（SQLite 命中，0 API）→ L3 大模型兜底 + 学习入库。\n"
    "    返回 dict：{\"label\": \"tool|chat|web\", \"reason\": str, \"confidence\": float}\n"
    "    \"\"\"\n"
    "    from plugins.core_utils.lexicon import LexiconDB, rule_match, should_llm_route\n"
    "\n"
    "    # L1：静态正则（极速拦截）\n"
    "    hit = rule_match(prompt)\n"
    "    if hit:\n"
    "        return {\"label\": hit[0], \"reason\": hit[1], \"confidence\": hit[2]}\n"
    "\n"
    "    # L3 触发检查：长消息 / 因果连接词 → 跳过词库，强制大模型深度分析\n"
    "    trigger, why = should_llm_route(prompt)\n"
    "    if trigger:\n"
    "        result = await _llm_detect_intent(prompt, context_messages)\n"
    "        if result[\"label\"] == \"tool\":\n"
    "            _learn_tool_word(prompt)\n"
    "        return result\n"
    "\n"
    "    # L2：动态词库（命中 0 API）\n"
    "    lex = LexiconDB()\n"
    "    m = lex.match(prompt)\n"
    "    if m:\n"
    "        word, intent = m\n"
    "        return {\"label\": intent, \"reason\": \"词库命中[%s]\" % word, \"confidence\": 0.9}\n"
    "\n"
    "    # 兜底 L3：大模型深度分析 + 学习\n"
    "    result = await _llm_detect_intent(prompt, context_messages)\n"
    "    if result[\"label\"] == \"tool\":\n"
    "        _learn_tool_word(prompt)\n"
    "    return result\n"
    "\n"
    "\n"
    "def _learn_tool_word(prompt: str) -> None:\n"
    "    \"\"\"大模型判定 tool 后：提取用户特有动作词入库（黑话学习）。\"\"\"\n"
    "    try:\n"
    "        from plugins.core_utils.lexicon import LexiconDB, extract_action_word\n"
    "        word = extract_action_word(prompt)\n"
    "        if word:\n"
    "            LexiconDB().learn(word, \"tool\")\n"
    "            logger.info(\"[EHIR] 学习新动作词: %s\", word)\n"
    "    except Exception as e:\n"
    "        logger.warning(\"[EHIR] 学习失败: %s\", e)\n"
    "\n"
    "\n"
    "async def _llm_detect_intent(prompt: str, context_messages: list = None) -> dict:\n"
    "    \"\"\"\n"
    "    用 chat 策略模型判断用户意图。\n"
)

if OLD not in src:
    print("ERROR: 锚点未找到，未做任何修改")
    sys.exit(1)

# 备份
shutil.copy2(AIPY, BAK)

new_src = src.replace(OLD, NEW_FUNNEL, 1)

# 语法自检：compile 后再写回
try:
    compile(new_src, AIPY, "exec")
except SyntaxError as e:
    print("ERROR: 补丁后语法错误，回滚: %s" % e)
    sys.exit(2)

with open(AIPY, "w", encoding="utf-8") as f:
    f.write(new_src)

print("PATCH-OK")
print("新 _detect_intent 行数:", len(NEW_FUNNEL.splitlines()))
