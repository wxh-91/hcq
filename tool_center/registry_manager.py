"""工具注册网关：对外统一的注册与拉取入口（AUP 协议化）。

视角约定：
- AI 视角：只暴露工具编号与简介，用于意图匹配（渐进式披露由此网关控制）。
- 人类视角：按名字检索定位。
- AUP 门禁：只允许注册带有 @aup_tool 装饰器（或完全符合 AUP 协议描述）的工具。
  不合规工具 -> ValueError 拒绝，无法进入 tool_center。

元数据一律落 SQLite；进程内仅缓存函数引用（函数对象不可序列化，
按 module_path + func_name 可在进程重启后自动恢复）。
"""
from __future__ import annotations

import importlib
import json
from typing import Callable, Iterator, Optional

from . import db_manager as _db

# AUP 协议层
from aup_core import get_aup_meta, validate_result, AUP_MANDATORY_FIELDS  # noqa: E402

_FUNCS: dict[str, Callable] = {}


def _resolve_func(name: str, module_path: str, func_name: str) -> Optional[Callable]:
    if name in _FUNCS:
        return _FUNCS[name]
    if module_path and func_name:
        try:
            mod = importlib.import_module(module_path)
            fn = getattr(mod, func_name)
            _FUNCS[name] = fn
            return fn
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# AUP 合规判定（注册中心门禁的唯一入口）
# ---------------------------------------------------------------------------
def is_aup_compliant(func: Optional[Callable] = None, meta: Optional[dict] = None) -> bool:
    """判定一个工具是否 AUP 合规。

    两条合规路径：
      A. 函数带 @aup_tool 装饰器（_aup_meta 存在）
      B. 传入的元数据携带 aup 协议描述（"aup": True / "aup_desc" 含 mandatory_fields）
    """
    if func is not None and hasattr(func, "_aup_meta"):
        return True
    if meta:
        if meta.get("aup") is True:
            return True
        desc = meta.get("aup_desc")
        if isinstance(desc, dict):
            fields = desc.get("mandatory_fields")
            if fields and all(f in AUP_MANDATORY_FIELDS for f in fields):
                return True
    return False


def read_aup_meta(func: Callable) -> Optional[dict]:
    """注册中心读取 @aup_tool 装饰器元数据的真实过程。

    返回元数据池（AUP_REGISTRY）中的完整 meta，含 name/description/category/
    is_danger/params_schema/module_path/func_name/version/aup 标记。
    """
    return get_aup_meta(func)


# ---------------------------------------------------------------------------
# 注册（AUP 强制门禁）
# ---------------------------------------------------------------------------
def register_tool(
    name: str,
    func: Optional[Callable] = None,
    *,
    description: str = "",
    category: str = "general",
    schema_json: str = "{}",
    is_danger: bool = False,
    is_active: bool = True,
    need_confirm: bool = False,
    params_desc: Optional[dict] = None,
    roles: Optional[list] = None,
    module_path: Optional[str] = None,
    func_name: Optional[str] = None,
    aup_desc: Optional[dict] = None,
    aup_required: bool = True,
) -> int:
    """注册一个工具：元数据写入 SQLite，函数引用挂载到进程内存。返回 tool_id。

    AUP 门禁（aup_required=True 默认强制）：
        - func 带 @aup_tool -> 自动提取 AUP 元数据填充注册信息
        - 或显式传入 aup_desc（AUP 协议描述）-> 校验通过后注册
        - 均不满足 -> ValueError("AUP 不合规...")，拒绝注册
    """
    aup_meta = None
    if func is not None and hasattr(func, "_aup_meta"):
        aup_meta = func._aup_meta
    if aup_required and not is_aup_compliant(func, {"aup_desc": aup_desc} if aup_desc else None):
        raise ValueError(
            "AUP 不合规：工具 %r 缺少 @aup_tool 装饰器元数据，且未提供 aup_desc 协议描述"
            % name
        )
    # 装饰器元数据自动接管（标注"aup"来源）
    if aup_meta:
        description = description or aup_meta.get("description", "")
        category = category if category != "general" else aup_meta.get("category", "general")
        is_danger = is_danger or bool(aup_meta.get("is_danger"))
        need_confirm = need_confirm or bool(aup_meta.get("need_confirm"))
        params_desc = params_desc or aup_meta.get("params_schema")
        if module_path is None:
            module_path = aup_meta.get("module_path")
        if func_name is None:
            func_name = aup_meta.get("func_name")

    if func is not None:
        if module_path is None:
            module_path = func.__module__
        if func_name is None:
            func_name = func.__name__
        _FUNCS[name] = func
    _db.init_db()
    return _db.upsert_tool(
        {
            "name": name,
            "description": description,
            "category": category,
            "schema_json": schema_json,
            "is_danger": is_danger,
            "is_active": is_active,
            "need_confirm": need_confirm,
            "params_desc": params_desc or {},
            "roles": roles or ["admin", "member"],
            "module_path": module_path or "",
            "func_name": func_name or "",
        }
    )


# ---------------------------------------------------------------------------
# 渐进式工具披露（AI 视角：只返回 id/name/description/schema，隐藏执行细节）
# ---------------------------------------------------------------------------
def _openai_schema(name: str, description: str, params_desc: dict) -> dict:
    """把 AUP params_schema 映射为 OpenAI function calling schema。"""
    props = {}
    for pname, p in (params_desc.get("properties") or {}).items():
        ptype = p.get("type", "string")
        props[pname] = {"type": ptype, "description": p.get("description", "")}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": list(params_desc.get("required") or []),
            },
        },
    }


def get_ai_visible_schemas(category: Optional[str] = None) -> list[dict]:
    """渐进式披露：对 AI 只返回 编号/名字/简介/参数Schema，隐藏执行细节。

    返回: [{"id": tool_id, "name": ..., "description": ..., "schema": {...}}]
    不含 module_path / func_name / is_danger / need_confirm / params_desc 等内部信息。
    """
    rows = _db.fetch_by_category(category) if category else _db.fetch_all(active_only=True)
    out = []
    for r in rows:
        try:
            params_desc = json.loads(r.get("params_desc_json") or "{}")
        except json.JSONDecodeError:
            params_desc = {}
        out.append(
            {
                "id": r.get("tool_id"),
                "name": r.get("name"),
                "description": r.get("description"),
                "schema": _openai_schema(r.get("name", ""), r.get("description", ""), params_desc),
            }
        )
    return out


def get_ai_visible(name: str) -> Optional[dict]:
    """单工具的渐进式披露（AI 视角）。"""
    row = _db.fetch_by_name(name)
    if not row:
        return None
    try:
        params_desc = json.loads(row.get("params_desc_json") or "{}")
    except json.JSONDecodeError:
        params_desc = {}
    return {
        "id": row.get("tool_id"),
        "name": row.get("name"),
        "description": row.get("description"),
        "schema": _openai_schema(row.get("name", ""), row.get("description", ""), params_desc),
    }


# ---------------------------------------------------------------------------
# 存量接口（人类/系统视角，保留 100% 行为）
# ---------------------------------------------------------------------------
def get_all_schemas(category: Optional[str] = None) -> list[dict]:
    """返回全部（或指定分类）的 OpenAI 格式 Schema 列表，用于工具路由披露。"""
    rows = _db.fetch_by_category(category) if category else _db.fetch_all(active_only=True)
    out = []
    for r in rows:
        raw = r.get("schema_json") or ""
        if not raw:
            continue
        try:
            schema = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(schema, dict):
            out.append(schema)
    return out


def get_tool_info(name: str) -> Optional[dict]:
    """按名字返回完整配置（含可执行函数引用）。未知/停用返回 None。"""
    row = _db.fetch_by_name(name)
    if not row:
        return None
    info = dict(row)
    try:
        info["params_desc"] = json.loads(info.pop("params_desc_json", "{}"))
    except json.JSONDecodeError:
        info["params_desc"] = {}
    try:
        info["roles"] = json.loads(info.pop("roles_json", "[]") or "[]")
    except json.JSONDecodeError:
        info["roles"] = []
    if not isinstance(info.get("roles"), list):
        info["roles"] = []
    info["func"] = _resolve_func(name, row.get("module_path", ""), row.get("func_name", ""))
    return info


def toggle_tool_status(name: str, status: bool) -> bool:
    """启用或停用某个工具（灰度/下线）。"""
    _db.init_db()
    return _db.set_active(name, bool(status))


def get_all_names(active_only: bool = True) -> list[str]:
    rows = _db.fetch_all(active_only=active_only)
    return [r["name"] for r in rows]


# ---------- 兼容视图：对外保持 dict 形态（结构不变，数据来自 SQLite） ----------

class CompatView:
    """dict-like 只读视图，供存量调用方（items/get/__contains__/keys/__getitem__）使用。"""

    def __init__(self, active_only: bool = True):
        self._active_only = active_only

    def _rows(self) -> list[dict]:
        return _db.fetch_all(active_only=self._active_only)

    def items(self) -> Iterator[tuple]:
        for r in self._rows():
            info = dict(r)
            try:
                info["params_desc"] = json.loads(info.pop("params_desc_json", "{}"))
            except json.JSONDecodeError:
                info["params_desc"] = {}
            try:
                info["roles"] = json.loads(info.pop("roles_json", "[]") or "[]")
            except json.JSONDecodeError:
                info["roles"] = []
            if not isinstance(info.get("roles"), list):
                info["roles"] = []
            info["func"] = _resolve_func(r["name"], r.get("module_path", ""), r.get("func_name", ""))
            if info["func"] is None:
                continue
            yield r["name"], info

    def get(self, name: str, default=None):
        info = get_tool_info(name)
        if info is None:
            return default
        if self._active_only and not info.get("is_active", True):
            return default
        return info

    def keys(self) -> list[str]:
        return [r["name"] for r in self._rows()]

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None

    def __getitem__(self, name: str):
        info = self.get(name)
        if info is None:
            raise KeyError(name)
        return info

    def __len__(self) -> int:
        return len(self._rows())

    def __iter__(self):
        return iter(self.keys())


def build_compat_view(active_only: bool = True) -> CompatView:
    _db.init_db()
    return CompatView(active_only=active_only)
