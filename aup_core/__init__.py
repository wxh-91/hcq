# -*- coding: utf-8 -*-
"""AUP 统一协议层（Agent Unified Protocol）。

- contract.AUPResult / AUPError：统一标准响应结构
- decorators.aup_tool：工具自动入参/出参校验 + 元数据自动注册
- decorators.AUP_REGISTRY：元数据池（注册中心读取点）

用法：
    from aup_core import AUPResult, AUPError, aup_tool
"""
from __future__ import annotations

from .contract import (
    AUPError,
    AUPResult,
    AUP_MANDATORY_FIELDS,
    STATUS_ERROR,
    STATUS_OK,
    is_aup_result,
    validate_result,
)
from .decorators import (
    AUP_REGISTRY,
    aup_tool,
    get_aup_meta,
    is_aup_decorated,
)

__all__ = [
    "AUPResult",
    "AUPError",
    "AUP_MANDATORY_FIELDS",
    "STATUS_OK",
    "STATUS_ERROR",
    "validate_result",
    "is_aup_result",
    "aup_tool",
    "AUP_REGISTRY",
    "get_aup_meta",
    "is_aup_decorated",
]
