# -*- coding: utf-8 -*-
"""WCU 配置桥：读取 config_center/wcu_policy.json 的白名单与开关（AI 只读）。

为什么单独一层：
    settings.py 是人类手改的中央控制台（文件头明确禁止 AI 自动改写，且已锁定只读），
    白名单这类"会频繁被建议调整"的策略因此搬到独立的 wcu_policy.json。
    AI 只能读，不能写；任何调整必须由人类手工编辑该 JSON，或经沙盒修改提案由人类裁决。

读取顺序：
    1. config_center/wcu_policy.json（唯一权威源，按 mtime 热加载）
    2. settings.py 遗留变量（仅兼容旧部署；正常情况下 settings.py 已不再包含这些键）
    3. 代码兜底默认值（刻意按"最严"口径）

任何一层缺失都不影响服务启动；缺失时一律按最严处理，绝不放开拦截。
"""
import os
_ROOT = os.environ.get("PROJECT_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import logging
import os


logger = logging.getLogger("plugins.windows_control.policy")

# ---- 代码级兜底（JSON 与 settings 都缺项时使用）----
DEFAULT_ALLOWED_APPS = "计算器, 记事本, qq, 微信, edge, chrome"
DEFAULT_MESSENGER_APPS = "qq, 微信"
DEFAULT_MAINSTREAM_DOMAINS = (
    "taobao.com, tmall.com, jd.com, pinduoduo.com, pdd.com, yangkeduo.com, "
    "baidu.com, bilibili.com, zhihu.com, weibo.com, douyin.com, "
    "xiaohongshu.com, qq.com, tencent.com, 163.com, sohu.com, sina.com.cn, "
    "youku.com, iqiyi.com, aliyun.com, alibaba.com, 1688.com, csdn.net, "
    "github.com, gitee.com, google.com, bing.com, microsoft.com, apple.com, "
    "localhost, 127.0.0.1"
)
DEFAULT_DOWNLOAD_KEYWORDS = "下载, 安装, 保存, 导出, 另存为, 存到"
DEFAULT_CLICK_KEYWORDS = "下载, 保存, 安装, 购买, 支付, 付款, 下单, 删除, 卸载, 格式化"

POLICY_JSON = _ROOT + "/config_center/wcu_policy.json"

_cache = {"mtime": None, "data": None}


def _load_json() -> dict:
    """按 mtime 热加载 wcu_policy.json；解析失败按空配置处理（走最严兜底）。"""
    try:
        st = os.stat(POLICY_JSON)
    except OSError:
        return {}
    if _cache["mtime"] == st.st_mtime and _cache["data"] is not None:
        return _cache["data"]
    try:
        with open(POLICY_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception as e:
        logger.warning("[policy] wcu_policy.json 解析失败，回落到最严默认: %s", e)
        data = {}
    _cache["mtime"] = st.st_mtime
    _cache["data"] = data
    return data


def _settings_module():
    try:
        from config_center import settings  # 只读导入，绝不触碰写路径
        return settings
    except Exception:
        return None


def _raw(name: str, json_key: str, default):
    data = _load_json()
    if json_key in data:
        return data[json_key]
    mod = _settings_module()
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return default


def _split_items(value) -> tuple:
    """把 "a, b, c" / ["a","b"] / ("a","b") 统一成去空的小写元组。"""
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        items = [str(v) for v in value]
    else:
        items = str(value).replace("，", ",").replace("、", ",").split(",")
    return tuple(x.strip().lower() for x in items if str(x).strip())


def allowed_apps() -> tuple:
    return _split_items(_raw("WIN_ALLOWED_APPS", "allowed_apps", DEFAULT_ALLOWED_APPS))


def messenger_apps() -> tuple:
    return _split_items(_raw("WIN_MESSENGER_APPS", "messenger_apps", DEFAULT_MESSENGER_APPS))


def mainstream_domains() -> tuple:
    return _split_items(_raw("BROWSER_MAINSTREAM_DOMAINS", "mainstream_domains",
                             DEFAULT_MAINSTREAM_DOMAINS))


def zero_trust_mode() -> bool:
    return bool(_raw("WIN_ZERO_TRUST_MODE", "zero_trust_mode", True))


def download_keywords() -> tuple:
    return _split_items(_raw("WIN_DOWNLOAD_KEYWORDS", "download_keywords",
                             DEFAULT_DOWNLOAD_KEYWORDS))


def click_keywords() -> tuple:
    return _split_items(_raw("WIN_CLICK_RISK_KEYWORDS", "click_risk_keywords",
                             DEFAULT_CLICK_KEYWORDS))


def snapshot() -> dict:
    """给日志/自检用的配置快照。"""
    return {
        "source": POLICY_JSON if os.path.exists(POLICY_JSON) else "(fallback) settings.py",
        "zero_trust": zero_trust_mode(),
        "allowed_apps": list(allowed_apps()),
        "messenger_apps": list(messenger_apps()),
        "mainstream_domains": list(mainstream_domains()),
        "download_keywords": list(download_keywords()),
        "click_risk_keywords": list(click_keywords()),
    }
