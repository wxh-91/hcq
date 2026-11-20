# -*- coding: utf-8 -*-
"""中央控制台（Central Config Center）统一读取网关。

设计硬约束：
- 本包只提供【读取】能力，不提供任何【写入/修改】能力。
- 配置基线的唯一人工修改通道：开发者进入 WSL，用 nano 手动编辑 settings.py。
- 严禁任何工具/脚本/装饰器改写 settings.py 与 wcu_policy.json（已 chattr +i 锁定）。

Console 热更新通道（运行时覆盖层，不回写锁定文件）：
- ${PROJECT_ROOT}/config_center/console_overlay.json 由网关 /api/config 写入；
- get_config 每次调用优先读取覆盖层，命中即返回覆盖值，下次业务调用即生效；
- 覆盖层缺失/损坏时自动回落到 settings.py 基线。

用法：
    from config_center import get_config
    threshold = get_config("CONTEXT_COMPRESS_THRESHOLD")
"""

import json
from pathlib import Path

from . import settings  # noqa: E402

__all__ = ["get_config", "settings"]

_OVERLAY_PATH = Path(__file__).with_name("console_overlay.json")


def _read_overlay() -> dict:
    try:
        if _OVERLAY_PATH.exists():
            data = json.loads(_OVERLAY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def get_config(key: str, default=None):
    """极简读取网关：覆盖层优先，未命中再取 settings 模块基线。

    无缓存：覆盖层文件每次现读，保证 Console 拨动开关后热更新即时生效。
    """
    overlay = _read_overlay()
    if key in overlay:
        return overlay[key]
    return getattr(settings, key, default)
