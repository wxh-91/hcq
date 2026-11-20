# -*- coding: utf-8 -*-
"""AUP 装饰器：@aup_tool 自动完成入参/出参校验，并自动注册元数据池。

任何工具函数只要加上 @aup_tool：
  1. 入参校验：params_schema 声明的 required 参数缺失 / 类型不符 -> AUPError
  2. 执行包装：异常统一收敛为 AUPError（含降级链路记录）
  3. 出参校验：返回值强制符合 AUPResult 五字段结构，否则 CONTRACT_VIOLATION
  4. 自动注册：元数据写入 AUP_REGISTRY（注册中心从此处读取）
"""
from __future__ import annotations

import asyncio
import functools
import json
import inspect
import re
from typing import Any, Callable, Dict, List, Optional

from .contract import (
    AUPError,
    AUPResult,
    ERROR_CODE_OK,
    STATUS_ERROR,
    STATUS_OK,
    validate_result,
)

# 元数据池：name -> meta dict
# 注册中心（tool_center/registry_manager.py）通过 get_aup_meta / func._aup_meta 读取
AUP_REGISTRY: Dict[str, dict] = {}

# params_schema 的类型白名单（JSON Schema type 子集）
_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": (list, tuple),
    "object": dict,
    "any": object,
}


def _type_ok(value: Any, type_name: str) -> bool:
    if type_name == "any":
        return True
    expect = _TYPE_MAP.get(type_name, object)
    if expect is bool:
        # bool 是 int 子类，需显式排除
        return isinstance(value, bool)
    if expect is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(expect, tuple):
        return any(isinstance(value, t) and not isinstance(value, bool) for t in expect)
    return isinstance(value, expect)


def validate_args(params_schema: Optional[dict], args: tuple, kwargs: dict) -> List[str]:
    """入参校验：required 缺失 / 类型不符。返回违规清单（空=通过）。"""
    problems: List[str] = []
    if not params_schema:
        return problems
    required = list(params_schema.get("required") or [])
    properties = params_schema.get("properties") or {}
    # 命名参数视图：位置参数按函数签名对齐（装饰器内无法取 fn 形参名，此处用顺序对齐调用方传入的 kwargs）
    present = set(kwargs.keys())
    for i, v in enumerate(args):
        _ = i, v  # 位置参数由调用方显式命名后走 kwargs；纯位置调用时仅做类型校验
    for r in required:
        if r not in present:
            problems.append("missing required argument: %r" % r)
    for name, p in properties.items():
        if name not in kwargs:
            continue
        # [Fix HTTP Request Type Mismatch] None = 可选参数未传（函数默认值），
        # 不做类型校验；否则 admin 层填充的 headers=None / json_body=None 会
        # 因 isinstance(None, dict)=False 被误判 type mismatch
        if kwargs[name] is None:
            continue
        type_name = p.get("type", "any")
        if not _type_ok(kwargs[name], type_name):
            problems.append(
                "argument %r: type mismatch, expected %r got %s"
                % (name, type_name, type(kwargs[name]).__name__)
            )
    return problems


def _coerce_kwargs_types(params_schema: Optional[dict], kwargs: dict) -> dict:
    """LLM 通病修复：schema 声明数字/布尔但模型传字符串 → 自动转换（宽松兼容）。

    模型经常把整数参数（如 timeout=15000）输出为字符串（"15000"）或把布尔输出为
    "true"/"false"。校验前先按 schema 声明做一次安全转换，避免误伤 INVALID_ARGUMENT；
    转换失败则保留原值，交给校验器按类型不符处理。
    """
    if not params_schema:
        return kwargs
    out = dict(kwargs)
    for name, p in (params_schema.get("properties") or {}).items():
        if name not in out:
            continue
        t = p.get("type")
        v = out[name]
        if t == "integer":
            if isinstance(v, str):
                sv = v.strip()
                if re.fullmatch(r"[+-]?\d+", sv):
                    out[name] = int(sv)
                elif re.fullmatch(r"[+-]?\d+\.\d+", sv):
                    out[name] = int(float(sv))
            elif isinstance(v, float) and v.is_integer():
                out[name] = int(v)
        elif t == "number":
            if isinstance(v, str):
                sv = v.strip()
                try:
                    out[name] = float(sv)
                except ValueError:
                    pass
        elif t == "boolean":
            if isinstance(v, str):
                low = v.strip().lower()
                if low in ("true", "1", "yes", "on"):
                    out[name] = True
                elif low in ("false", "0", "no", "off"):
                    out[name] = False
        # [Fix HTTP Request Type Mismatch] dict/list 兼容：大模型常把 headers/json_body
        # 传成 JSON 字符串，此处自动 json.loads 转换；失败保留原值交校验器判错
        elif t in ("object", "array"):
            if isinstance(v, str):
                sv = v.strip()
                if sv:
                    try:
                        out[name] = json.loads(sv)
                    except (json.JSONDecodeError, ValueError):
                        pass
    return out


def coerce_result(raw: Any, tool_name: str, chain: List[str]) -> AUPResult:
    """出参收口：任何返回值 -> AUPResult（非 AUP 返回自动包装 / 自动校验）。"""
    if isinstance(raw, AUPResult):
        problems = validate_result(raw.to_dict())
        if problems:
            raise AUPError(
                "CONTRACT_VIOLATION",
                "%s 返回的 AUPResult 结构不完整" % tool_name,
                detail={"problems": problems},
            )
        return raw
    if isinstance(raw, dict):
        problems = validate_result(raw)
        if problems:
            raise AUPError(
                "CONTRACT_VIOLATION",
                "%s 返回 dict 缺少 AUP 强制字段" % tool_name,
                detail={"problems": problems},
            )
        raw.setdefault("fallback_chain", chain)
        return AUPResult.from_dict(raw)
    # 裸返回值（str/int/...）自动包装
    return AUPResult(status=STATUS_OK, data=raw, error_code=ERROR_CODE_OK,
                     fallback_chain=chain)


def aup_tool(
    name: Optional[str] = None,
    *,
    description: str = "",
    category: str = "general",
    is_danger: bool = False,
    params_schema: Optional[dict] = None,
    version: str = "1.0.0",
    need_confirm: bool = False,
    suggestion_hint: str = "",
):
    """AUP 工具装饰器。用法：

        @aup_tool(
            name="execute_shell",
            description="执行任意 Shell 命令（高危）",
            category="system",
            is_danger=True,
            params_schema={"required": ["cmd"], "properties": {"cmd": {"type": "string"}}},
        )
        async def execute_shell(cmd: str) -> ...:
            ...
    """

    def deco(fn: Callable) -> Callable:
        tool_name = name or fn.__name__
        meta = {
            "name": tool_name,
            "description": description or (inspect.getdoc(fn) or "").strip().split("\n")[0],
            "category": category,
            "is_danger": is_danger,
            "params_schema": params_schema or {},
            "version": version,
            "need_confirm": need_confirm,
            "module_path": fn.__module__,
            "func_name": fn.__name__,
            "aup": True,
        }

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> AUPResult:
            chain: List[str] = [f"{tool_name}:enter"]
            # 0) 位置参数按函数签名映射为命名参数（入参校验与调用统一走 kwargs）
            bound_names = list(inspect.signature(fn).parameters.keys())
            merged: Dict[str, Any] = {}
            for i, v in enumerate(args):
                if i < len(bound_names):
                    merged[bound_names[i]] = v
            merged.update(kwargs)  # 显式命名优先
            # [Parameter Normalization] LLM 多包一层 args/kwargs → 智能解包：
            # 仅当 merged 的所有键都落在 {args, kwargs} 内（纯包裹形态）才提取内层真实参数。
            # 内层为 dict 直接合并；为 JSON 字符串则解析后合并。正常传参不受影响。
            _wrap_keys = set(merged.keys())
            if _wrap_keys and _wrap_keys <= {"args", "kwargs"}:
                _inner = {}
                for _wk in ("args", "kwargs"):
                    if _wk not in merged:
                        continue
                    _v = merged[_wk]
                    if isinstance(_v, dict):
                        _inner.update(_v)
                    elif isinstance(_v, str):
                        try:
                            _jv = json.loads(_v)
                            if isinstance(_jv, dict):
                                _inner.update(_jv)
                        except Exception:
                            pass
                if _inner:
                    merged = _inner
            # LLM 宽松兼容：数字/布尔字段若收到字符串 → 自动转换（模型常把 15000 传成 "15000"）
            merged = _coerce_kwargs_types(params_schema, merged)
            # 1) 入参校验
            violations = validate_args(params_schema, (), merged)
            if violations:
                chain.append(f"{tool_name}:validate:rejected")
                raise AUPError(
                    "INVALID_ARGUMENT",
                    "入参校验失败",
                    detail={"problems": violations},
                    fallback_chain=chain,
                    suggestion="请按 %s 的参数 Schema 补齐参数" % tool_name,
                )
            chain.append(f"{tool_name}:validate:ok")
            # 2) 执行（async 原样 await，sync 走线程池）
            try:
                if inspect.iscoroutinefunction(fn):
                    raw = await fn(**merged)
                else:
                    raw = await asyncio.to_thread(fn, **merged)
                chain.append(f"{tool_name}:primary:ok")
            except AUPError as e:
                # 工具内部已抛 AUPError：若自带链路则保留（工具内部降级记录优先），
                # 否则补齐 wrapper 前缀（入参校验等 wrapper 层错误）
                if not e.fallback_chain:
                    e.fallback_chain = list(chain[:-1])
                raise
            except Exception as e:
                chain.append(f"{tool_name}:primary:exception:{type(e).__name__}")
                raise AUPError(
                    "TOOL_EXECUTION_FAILED",
                    "%s 执行失败: %s" % (tool_name, e),
                    fallback_chain=chain,
                    suggestion=suggestion_hint or "请检查入参或稍后重试",
                ) from e
            # 3) 出参校验
            return coerce_result(raw, tool_name, chain)

        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        # 注册中心读取点：func._aup_meta
        wrapper._aup_meta = meta
        AUP_REGISTRY[tool_name] = meta
        return wrapper

    return deco


def get_aup_meta(obj: Any) -> Optional[dict]:
    """读取 AUP 元数据：装饰器函数（._aup_meta）或注册池（name）。"""
    if callable(obj) and hasattr(obj, "_aup_meta"):
        return obj._aup_meta
    if isinstance(obj, str):
        return AUP_REGISTRY.get(obj)
    return None


def is_aup_decorated(obj: Any) -> bool:
    """判定一个函数是否带 @aup_tool 装饰器。"""
    return callable(obj) and hasattr(obj, "_aup_meta")
