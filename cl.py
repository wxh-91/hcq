import os
from dotenv import load_dotenv
load_dotenv()   # 加载.env

MEMORY_FILE = 'memory.json'
DEFAULT_ADMIN = '1556998894'
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

def get_strategy_config(strategy: str) -> dict | None:
    """获取指定策略的配置"""
    if not MODELS_CONFIG:
        return None
    return MODELS_CONFIG.get("strategies", {}).get(strategy)

def detect_strategy(prompt: str) -> str:
    """根据用户输入检测应该使用哪个策略"""
    if not MODELS_CONFIG:
        return "tool"
    keywords = MODELS_CONFIG.get("strategy_keywords", {})
    tool_keywords = keywords.get("tool", [])
    if any(kw in prompt for kw in tool_keywords):
        return "tool"
    return "chat"
