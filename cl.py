import os
from dotenv import load_dotenv
load_dotenv()   # 加载.env
CHECKIN_ENABLED = os.getenv("CHECKIN_ENABLED", "true").strip().lower() in ("true", "1", "yes", "on")
MEMORY_FILE = 'memory.json'
DEFAULT_ADMIN = '1556998894'
# [Feishu Admin] 飞书端主管理员 open_id（.env 的 FEISHU_ADMIN_OPEN_ID 注入）。
# 与 DEFAULT_ADMIN 构成双端主管理员映射：任一端身份均拥有主管理员权限（含高危工具）。
FEISHU_ADMIN_OPEN_ID = os.getenv("FEISHU_ADMIN_OPEN_ID", "") or ""


def is_main_admin(user_id) -> bool:
    """统一主管理员判定：QQ（DEFAULT_ADMIN）或飞书（FEISHU_ADMIN_OPEN_ID）。

    兼容飞书桥接层的 open_id↔int 稳定映射（crc32）：
    FeishuEventView.user_id 是有意设计为 open_id 的 crc32 int，
    业务层拿到的 user_id 可能是 int（如 402710372），需反查回 open_id 再比对。
    """
    uid = str(user_id)
    if uid == str(DEFAULT_ADMIN):
        return True
    if FEISHU_ADMIN_OPEN_ID and uid == str(FEISHU_ADMIN_OPEN_ID):
        return True
    if FEISHU_ADMIN_OPEN_ID:
        try:
            from plugins.core_utils.feishu_bridge import resolve_open_id
            oid = resolve_open_id(uid)
            if oid and oid == str(FEISHU_ADMIN_OPEN_ID):
                return True
        except Exception:
            pass
    return False
# 本地沙盒 / DSH 桌面通道会话前缀（与 dz.py _TEST_SANDBOX_KEY 对齐）。
# 该通道仅绑定 127.0.0.1，是本机操作者自己的控制面板；
# 远程飞书 / QQ 发送者身份永远不会带此前缀。本地会话在危险工具上视同主管理员。
LOCAL_SANDBOX_PREFIX = "test_sandbox_123_"


def is_local_sandbox_session(user_id) -> bool:
    """判定是否为本机沙盒 / DSH 桌面通道会话（本地回环，操作者即机主）。"""
    return str(user_id or "").startswith(LOCAL_SANDBOX_PREFIX)


NEWAPI_BASE = os.getenv("NEWAPI_BASE")

# 读取多个 API Key，支持 NEWAPI_KEY_1, NEWAPI_KEY_2, ...
NEWAPI_KEYS = []
idx = 1
while True:
    key = os.getenv(f"NEWAPI_KEY_{idx}")
    if not key:
        break
    NEWAPI_KEYS.append(key)
    idx += 1

# 如果未配置多 Key，回退到单 Key
if not NEWAPI_KEYS:
    single_key = os.getenv("NEWAPI_KEY")
    if single_key:
        NEWAPI_KEYS = [single_key]
    else:
        raise ValueError("未配置任何 API Key，请设置 NEWAPI_KEY 或 NEWAPI_KEY_1")

MODEL_NAME = os.getenv("MODEL_NAME", "deepseek-v4-flash")
BACKUP_BASE = os.getenv("BACKUP_BASE")
BACKUP_KEY = os.getenv("BACKUP_KEY")
# 当前使用的 Key 索引（全局状态）
CURRENT_KEY_INDEX = 0
KEY_FAILURE_COUNT = {}  # key -> 连续失败次数
KEY_COOLDOWN_UNTIL = {} # key -> 冷却到期时间戳

import json

MODELS_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "models.json")

def load_models_config():
    try:
        with open(MODELS_CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[WARN] 加载 models.json 失败: {e}，使用默认配置")
        return None

MODELS_CONFIG = load_models_config()

# ==================== R18 · 模型解析层（唯一的路由依据是模型名） ====================

def _models_table() -> dict:
    """models.json 里的 models 表；老结构（strategies 版）时返回空。"""
    if not MODELS_CONFIG:
        return {}
    return MODELS_CONFIG.get("models") or {}


def _alias_table() -> dict:
    """role_aliases 兼容表：历史调用点写的是岗位名，这里只做「岗位名 -> 真实模型」的翻译。"""
    if not MODELS_CONFIG:
        return {}
    return {str(k).strip().lower(): str(v) for k, v in (MODELS_CONFIG.get("role_aliases") or {}).items()}


# 没显式指定模型时用的默认模型（前端没传 model 才会走到这里）
DEFAULT_MODEL = str((MODELS_CONFIG or {}).get("default_model") or MODEL_NAME)


def list_models() -> list:
    """当前可选的模型名列表（前端 /v1/models 与日志取证用）。"""
    return list(_models_table().keys())


def resolve_model(name: str) -> dict | None:
    """把「真实模型名 / 历史岗位别名」解析成一份可直接发请求的模型配置。

    顺序：真实模型名精确匹配 -> 大小写不敏感匹配 -> role_aliases 兼容表 -> 老 strategies 结构。
    全部落空返回 None：调用方必须如实报错，禁止静默换模型。
    """
    if not name:
        return None
    key = str(name).strip()
    models = _models_table()
    if key in models:
        cfg = dict(models[key])
        cfg.setdefault("id", key)
        cfg.setdefault("model", key)
        return cfg
    low = key.lower()
    for model_key, value in models.items():
        if model_key.lower() == low:
            cfg = dict(value)
            cfg.setdefault("id", model_key)
            cfg.setdefault("model", model_key)
            return cfg
    alias_target = _alias_table().get(low)
    if alias_target:
        return resolve_model(alias_target)
    # 兼容老结构（models.json 还是 strategies 版）
    if MODELS_CONFIG and MODELS_CONFIG.get("strategies"):
        legacy = MODELS_CONFIG["strategies"].get(low)
        if legacy:
            cfg = dict(legacy)
            cfg.setdefault("id", low)
            cfg.setdefault("model", cfg.get("model", low))
            return cfg
    return None


def get_strategy_config(name: str) -> dict | None:
    """历史入口（名字保留以兼容 divine_task / large_task 等旧调用点）：
    等价于 resolve_model，返回结构仍是 model / base_url / api_keys / timeout。"""
    cfg = resolve_model(name)
    if not cfg:
        return None
    cfg.setdefault("model", cfg.get("id"))
    return cfg


def detect_strategy(prompt: str) -> str:
    """[已废弃 R18] 自动策略分类彻底下线，恒返回空字符串。

    保留函数只为兼容历史 import；任何调用方都不得据此选择模型。
    """
    return ""
