import os
_ROOT = os.environ.get("PROJECT_ROOT") or os.path.dirname(os.path.abspath(__file__))

# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, _ROOT)
from memory_center import MemoryCenter, SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT
import os

mc = MemoryCenter()
mc.append_message("smoke1", "user", "测试消息1")
mc.append_message("smoke1", "assistant", "回复1")
h = mc.get_context("smoke1")
print("CONTEXT_LEN", len(h), h[0]["role"], h[1]["role"])
msgs = mc.build_initial_messages("主人档案测试")
print("PERSONA_BLOCKS", len(msgs), [m["content"][:14] for m in msgs])
print("SYS_LEN", len(SYSTEM_PROMPT), "SUMMARY", bool(SUMMARY_SYSTEM_PROMPT))
print("COMPRESS_CHECK", mc.should_compress("smoke1"))
print("MC_IMPORT_OK")
