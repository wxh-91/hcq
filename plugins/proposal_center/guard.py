# -*- coding: utf-8 -*-
"""settings.py / wcu_policy.json 只读守卫（代码层死命令）。

定位：任何工具执行前的最后一道闸。命中即返回错误串，工具不会被调用。

判定：
  - 参数里出现受保护文件（settings.py / wcu_policy.json）
  - 且 工具是"直写类"（file_write / write_file / replace_line / insert_line / delete_line 等），
    或 工具是 shell 类且命令里带写动作特征（>、tee、sed -i、Set-Content、Out-File、
    open(...,'w')、.write(、shutil.copy、chmod、chattr、cp、mv、rm、truncate）
  → 判定违规，返回 SETTINGS_WRITE_FORBIDDEN。

只读访问（grep / cat / read_file）不在拦截范围内。
"""
from __future__ import annotations

import json
import re

PROTECTED = ("settings.py", "wcu_policy.json")

# 直写类工具：只要参数里出现受保护文件即拦
_DIRECT_WRITE_TOOLS = {
    "file_write", "write_file", "replace_line", "insert_line", "delete_line",
    "crawler_download",
}

# shell 类工具：需要有写动作特征才拦（读命令放行）
_SHELL_TOOLS = {
    "execute_shell", "run_bash_script", "aup_execute_shell", "aup_run_bash_script", "win_shell",
}

_WRITE_PAT = re.compile(
    r"(>>?|\btee\b|sed\s+-i|Set-Content|Out-File|Add-Content|writelines|"
    r"open\([^)]*['\"][wa]\+?['\"]|\.write\(|shutil\.copy|os\.replace|"
    r"\bchmod\b|\bchattr\b|\bcp\b|\bmv\b|\brm\b|\btruncate\b)",
    re.IGNORECASE,
)


def violation(tool_name: str, args) -> str:
    """返回违规说明串；无违规返回空串。"""
    name = (tool_name or "").strip()
    try:
        text = json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args
    except Exception:
        text = str(args)
    low = text.lower()
    hit = next((p for p in PROTECTED if p in low), "")
    if not hit:
        return ""
    blocked = name in _DIRECT_WRITE_TOOLS or (name in _SHELL_TOOLS and bool(_WRITE_PAT.search(text)))
    if not blocked:
        return ""
    return (
        "SETTINGS_WRITE_FORBIDDEN：config_center/%s 是人类的只读控制台，"
        "任何脚本/工具/代码改写都会被代码层阻断（文件已 chattr +i 锁定）。\n"
        "如需调整策略，请调用 submit_system_proposal 呈递《系统修改提案》，由主人裁决。" % hit
    )


def assert_readonly(tool_name: str, args) -> None:
    """供非字符串返回场景调用（抛异常版）。"""
    msg = violation(tool_name, args)
    if msg:
        from aup_core import AUPError

        raise AUPError("SETTINGS_WRITE_FORBIDDEN", msg)
