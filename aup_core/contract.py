# -*- coding: utf-8 -*-
"""AUP 核心协议：AUPResult / AUPError / 统一响应结构校验。

AUP（Agent Unified Protocol）定义所有生产工具、测试单元、注册中心之间
的统一通信契约。任何工具返回结果必须携带五个字段：
    status          "ok" | "error"
    data            业务数据（status=ok 时非空）
    error_code       0 / "0" 表示成功；非 0 为错误码
    fallback_chain   本次执行的降级链路记录（多备用方案逐条追加）
    suggestion       给上层 AI/调用者的建议文案（可空字符串）
"""
from __future__ import annotations

import dataclasses
import json
from typing import Any, List, Optional, Tuple, Union

# 统一响应结构的强制字段（契约硬校验点）
AUP_MANDATORY_FIELDS: Tuple[str, ...] = (
    "status", "data", "error_code", "fallback_chain", "suggestion",
)

STATUS_OK: str = "ok"
STATUS_ERROR: str = "error"
ERROR_CODE_OK: str = "0"


class AUPError(Exception):
    """AUP 协议级异常：工具内部抛出的所有错误统一收敛为此类。

    Attributes:
        error_code:      机器可读错误码（如 INVALID_ARGUMENT / TOOL_EXECUTION_FAILED）
        message:         人读错误信息
        detail:          结构化细节（参数名、来源等）
        fallback_chain:  降级链路记录（每条 = "<tool>:<stage>:<note>"）
        suggestion:      给上层 AI 的处置建议
    """

    def __init__(
        self,
        error_code: str,
        message: str = "",
        *,
        detail: Optional[dict] = None,
        fallback_chain: Optional[List[str]] = None,
        suggestion: str = "",
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.message = message or error_code
        self.detail = detail or {}
        self.fallback_chain = fallback_chain or []
        self.suggestion = suggestion

    def to_result(self) -> "AUPResult":
        """异常 -> AUPResult（保持链路信息不丢）。"""
        return AUPResult(
            status=STATUS_ERROR,
            data=None,
            error_code=self.error_code,
            fallback_chain=list(self.fallback_chain),
            suggestion=self.suggestion,
        )


@dataclasses.dataclass
class AUPResult:
    """统一标准响应结构。所有工具函数（经 @aup_tool）返回本类型。"""

    status: str = STATUS_OK
    data: Any = None
    error_code: str = ERROR_CODE_OK
    fallback_chain: List[str] = dataclasses.field(default_factory=list)
    suggestion: str = ""

    # ---- 便捷断言 ----
    def is_ok(self) -> bool:
        return self.status == STATUS_OK

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "data": self.data,
            "error_code": self.error_code,
            "fallback_chain": list(self.fallback_chain),
            "suggestion": self.suggestion,
        }

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kw)

    @classmethod
    def from_dict(cls, d: dict) -> "AUPResult":
        problems = validate_result(d)
        if problems:
            raise AUPError(
                "CONTRACT_VIOLATION",
                "返回结构不符合 AUP 协议",
                detail={"problems": problems},
            )
        return cls(
            status=d.get("status", STATUS_ERROR),
            data=d.get("data"),
            error_code=str(d.get("error_code", "1")),
            fallback_chain=list(d.get("fallback_chain") or []),
            suggestion=d.get("suggestion", ""),
        )


def validate_result(obj: Any) -> List[str]:
    """校验任意返回值是否符合 AUP 统一响应结构。

    返回违规清单；空清单 = 合规。这是测试单元契约测试与注册中心门禁
    共用的唯一校验入口。
    """
    problems: List[str] = []
    if isinstance(obj, AUPResult):
        obj = obj.to_dict()
    if not isinstance(obj, dict):
        return ["result must be dict or AUPResult instance, got %r" % type(obj).__name__]
    for f in AUP_MANDATORY_FIELDS:
        if f not in obj:
            problems.append("missing mandatory field: %s" % f)
    if "status" in obj and obj["status"] not in (STATUS_OK, STATUS_ERROR):
        problems.append("status must be 'ok' or 'error', got %r" % obj["status"])
    if (
        "status" in obj
        and obj["status"] == STATUS_OK
        and "error_code" in obj
        and obj["error_code"] not in (0, "0", "")
    ):
        problems.append("ok result must carry zero error_code, got %r" % obj["error_code"])
    if "fallback_chain" in obj and not isinstance(obj["fallback_chain"], list):
        problems.append("fallback_chain must be a list")
    return problems


def is_aup_result(obj: Any) -> bool:
    """快速判定：返回值是否 AUP 合规（不抛异常版）。"""
    return not validate_result(obj)
