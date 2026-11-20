# -*- coding: utf-8 -*-
"""patch_purify2: 修复 ai.py 残留人格引用（PROMPT_P0/SYSTEM_PROMPT 已迁移 memory_center）"""
import io, re, sys

P = '/root/robot/ai.py'
src = io.open(P, encoding='utf-8').read()


def must_rep(old, new, tag):
    global src
    if old not in src:
        print("[FAIL] 锚点缺失: %s" % tag)
        sys.exit(1)
    src = src.replace(old, new, 1)
    print("[OK] %s" % tag)


# 1) submit_tool_results final_messages 人格组装 → MemoryCenter 唯一入口（含 P4 死命令）
must_rep(
    """        final_messages = []
        # ===== P0：身份 =====
        if PROMPT_P0_IDENTITY:
            final_messages.append({"role": "system", "content": PROMPT_P0_IDENTITY})
        # ===== P1：规则 =====
        if PROMPT_P1_CORE:
            final_messages.append({"role": "system", "content": PROMPT_P1_CORE})
        if PROMPT_P1_STYLE:
            final_messages.append({"role": "system", "content": PROMPT_P1_STYLE})
        if PROMPT_P1_IMAGE:
            final_messages.append({"role": "system", "content": PROMPT_P1_IMAGE})
        if PROMPT_P1_XMKJ:
            final_messages.append({"role": "system", "content": PROMPT_P1_XMKJ})
        # ===== P2：主人档案 =====
        if PROFILE_CONTENT:
            final_messages.append({"role": "system", "content": f"【P2·主人档案】\\n{PROFILE_CONTENT}"})""",
    """        # ===== 人格注入（P0~P4 + P2 档案）唯一入口：MemoryCenter.build_initial_messages =====
        final_messages = MemoryCenter.build_initial_messages(PROFILE_CONTENT)""",
    "submit_tool_results 人格组装收编")

# 2) submit_tool_results 单条 system → MemoryCenter.build_system_prompt()
must_rep(
    """    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)""",
    """    messages = [{"role": "system", "content": MemoryCenter.build_system_prompt()}]
    messages.extend(history)""",
    "submit_tool_results 单条人格收编")

io.open(P, 'w', encoding='utf-8').write(src)
print("PATCH2_DONE")
