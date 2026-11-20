# -*- coding: utf-8 -*-
"""EHIR 极端混合意图路由 —— 词库管理 + 三层漏斗纯逻辑模块（无重依赖，仅标准库）。

位置：/root/robot/bot_utils/lexicon.py
职责：
  L1 静态正则预判（极速拦截，0 API）
  L2 用户专属动态词库（SQLite 持久化，命中 0 API）
  L3 触发条件判断（长消息 / 因果连接词 → 强制大模型介入）
  学习机制：大模型判 tool 后提取核心动作词入库（黑话学习："扬了"、"跑一下"）

ai.py 的 _detect_intent 通过本模块组装漏斗；验证脚本直接 import 本模块做本地验证。
大模型介入（L3）的云端调用仍走 ai.py 原有 safe_api_request / AUP 边界，本模块不触碰。
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path
from typing import Optional, Tuple

# ============ 数据库 ============
_DB_PATH = Path(__file__).resolve().parent / "lexicon.db"
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lexicon (
    word      TEXT PRIMARY KEY,
    intent    TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0,
    last_hit  TEXT,
    source    TEXT NOT NULL DEFAULT 'learned'
);
CREATE INDEX IF NOT EXISTS idx_lexicon_intent ON lexicon(intent);
"""


class LexiconDB:
    """动态词库：SQLite 持久化，线程安全。"""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else _DB_PATH
        with _lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def learn(self, word: str, intent: str, source: str = "learned") -> bool:
        """入库（INSERT OR IGNORE；已存在则累计 source 不重置）。返回是否新增。"""
        word = (word or "").strip()
        if len(word) < 2 or len(word) > 6:
            return False
        with _lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT 1 FROM lexicon WHERE word = ?", (word,)
                )
                exists = cur.fetchone() is not None
                if not exists:
                    conn.execute(
                        "INSERT INTO lexicon(word, intent, hit_count, last_hit, source) "
                        "VALUES(?, ?, 0, datetime('now','localtime'), ?)",
                        (word, intent, source),
                    )
                conn.commit()
                return not exists
            finally:
                conn.close()

    def match(self, text: str) -> Optional[Tuple[str, str]]:
        """在文本中查找命中的词条，返回 (word, intent)；无命中返回 None。
        按命中次数优先 + 词长降序（长词优先，避免"一下"误吞"跑一下"）。"""
        if not text or not text.strip():
            return None
        with _lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT word, intent FROM lexicon "
                    "WHERE ? LIKE '%' || word || '%' "
                    "ORDER BY hit_count DESC, length(word) DESC LIMIT 1",
                    (text,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                conn.execute(
                    "UPDATE lexicon SET hit_count = hit_count + 1, "
                    "last_hit = datetime('now','localtime') WHERE word = ?",
                    (row["word"],),
                )
                conn.commit()
                return row["word"], row["intent"]
            finally:
                conn.close()

    def stats(self) -> dict:
        with _lock:
            conn = self._connect()
            try:
                total = conn.execute("SELECT COUNT(*) AS c FROM lexicon").fetchone()["c"]
                by_intent = {
                    r["intent"]: r["c"]
                    for r in conn.execute(
                        "SELECT intent, COUNT(*) AS c FROM lexicon GROUP BY intent"
                    )
                }
                top = [
                    (r["word"], r["intent"], r["hit_count"])
                    for r in conn.execute(
                        "SELECT word, intent, hit_count FROM lexicon "
                        "ORDER BY hit_count DESC LIMIT 10"
                    )
                ]
                return {"total_words": total, "by_intent": by_intent, "top_words": top}
            finally:
                conn.close()


# ============ L1 静态正则规则表 ============
# 说明：以"开头强指令"为主（任务书口径），避免过度拦截写作/问答类消息。
_TOOL_PREFIX = (
    r"^(?:"
    r"执行|运行|跑一下|打开|删除|删掉|删|爬取|爬一下|备份|下载|签到|"
    r"重启|清理|写文件|读文件|替换|打包|压缩|解压|"
    r"添加管理员|删管理员|查管理员|查看管理员|君权神授|神授|"
    r"用\S*(shell|命令|脚本).*(执行|运行)|关闭.*(群聊|开关)|开启.*(群聊|开关)"
    r")"
)

_CHAT_PREFIX = (
    r"^(?:"
    r"你好|您好|谢谢|感谢|再见|拜拜|晚安|早安|哈哈|嗯嗯|在吗|"
    r"你是谁|你叫什么|讲个笑话|推荐一部|写一首|翻译一下|解释一下|"
    r"你怎么看|你觉得|夸夸|帮我想|起个名|随便聊聊|聊聊天|"
    r"请问\S*(是什么|什么意思|怎么办)"
    r")"
)

# 定时任务强特征：命中即 tool（物理拦截，绝不让进 C 端通道）
_SCHEDULE_PATTERNS = [
    r"(?:每天|明天|今天|后天|下周|周[一二三四五六日])?\s*\d{1,2}\s*(?:点|时|[:：])",
    r"提醒我",
    r"定时",
    r"闹钟",
    r"发送",
]

# 商城/凭证/数据获取强特征：命中即 tool（物理拦截，杠绝分类器漂移后的追问死锁）
_COMMERCE_TOOL_PATTERNS = [
    r"拼多多",
    r"淘宝",
    r"天猫",
    r"美团",
    r"京东",
    r"闲鱼",
    r"没存凭证",
    r"未存凭证",
    r"没有凭证",
    r"获取数据",
    r"抓取数据",
    r"采集数据",
    r"获取.{0,8}(?:价格|销量|订单|商品|数据)",
    r"抓.{0,8}(?:价格|销量|订单|商品|数据)",
]

# web 强特征（可出现在句中，但须整体偏查询）
_WEB_PATTERNS = [
    r"搜(?:一下|索)?",
    r"查一下?\s*(?:天气|新闻|行情|汇率|股价|最新|实时|排名|价格)",
    r"(?:最新|实时)(?:消息|资讯|动态|行情)",
    r"帮我查查",
    r"百度一下",
    r"上网(?:搜|查|找)",
    r"热搜",
    r"今日(?:新闻|热点|头条)",
]

_TOOL_STRONG = [
    # 明确的系统查询 / 文件 / 网络动作（非开头也可命中，但必须含强动作词）
    r"(?:查|看|查一下|看看)\s*(?:系统状态|CPU|内存|磁盘|占用|进程|端口|日志)",
    r"(?:用|执行)\s*(?:execute_shell|bash|python|命令)",
    r"(?:抓|取|下载).*(?:视频|m3u8|mp4|链接)",
    r"(?:写|创建|新建).*(?:文件|脚本)",
]

# ============ L3 因果触发 ============
_CAUSAL_PATTERNS = [
    r"先.{0,8}(?:再|然后)",
    r"如果.{0,12}就",
    r"因为.{0,12}(?:所以|就)",
    r"然后",
    r"接着",
    r"顺便",
    r"同时",
    r"以及再",
    r"边.{0,4}边",
    r"先.{0,6}后",
]

LLM_ROUTE_MIN_LEN = 20  # 消息长度阈值（字）


def should_llm_route(text: str) -> Tuple[bool, str]:
    """L3 触发判断：消息较长 或 含因果连接词 → 必须大模型深度分析。"""
    text = (text or "").strip()
    if len(text) > LLM_ROUTE_MIN_LEN:
        return True, "长消息(%d字>%d)" % (len(text), LLM_ROUTE_MIN_LEN)
    for pat in _CAUSAL_PATTERNS:
        if re.search(pat, text):
            return True, "因果连接词[%s]" % pat
    return False, ""


def rule_match(text: str) -> Optional[Tuple[str, str, float]]:
    """L1 静态正则预判。返回 (label, reason, confidence) 或 None。"""
    text = (text or "").strip()
    if not text:
        return None
    for pat in _SCHEDULE_PATTERNS:
        if re.search(pat, text):
            return "tool", "L1正则:定时任务[%s]" % pat, 0.95
    for pat in _COMMERCE_TOOL_PATTERNS:
        if re.search(pat, text):
            return "tool", "L1正则:商城/凭证[%s]" % pat, 0.95
    if re.match(_TOOL_PREFIX, text):
        return "tool", "L1正则:开头指令", 0.95
    if re.match(_CHAT_PREFIX, text):
        return "chat", "L1正则:问候/闲聊", 0.95
    for pat in _TOOL_STRONG:
        if re.search(pat, text):
            return "tool", "L1正则:%s" % pat, 0.9
    for pat in _WEB_PATTERNS:
        if re.search(pat, text):
            return "web", "L1正则:%s" % pat, 0.85
    return None


# ============ 学习机制：提取核心动作词 ============
# 标准动作词不学（太常见且已由 L1 覆盖）；只学用户特有黑话（"扬了"、"跑一下"）
_STOP_WORDS = {
    "执行", "运行", "查看", "删除", "删掉", "打开", "下载", "爬取", "备份",
    "签到", "写入", "读取", "搜索", "查询", "添加", "重启", "清理", "检查",
    "统计", "发送", "获取", "更新", "替换", "修改", "创建", "列出", "关闭",
    "开启", "停止", "开始", "打包", "压缩", "解压", "启动", "安装", "卸载",
    "扫描", "监控", "记录", "输出", "打印", "保存", "恢复", "登录", "退出",
    "确认", "取消", "撤回", "转发", "翻译", "解释", "推荐", "帮助", "测试",
}

_ACTION_SUFFIX = re.compile(r"^[\u4e00-\u9fa5]{1,3}(?:了|一下|一哈|起来|掉|完|好|一嘴|一记)$")

# 标准动作词前缀：全句扫描时跳过（这些词已由 L1 覆盖，无需学习）
_STOP_PREFIX = ("查看", "删除", "删掉", "执行", "打开", "运行", "下载", "爬取",
                "备份", "签到", "写入", "读取", "搜索", "查询", "添加", "重启",
                "清理", "检查", "统计", "发送", "获取", "更新", "替换", "修改",
                "创建", "列出", "关闭", "开启", "停止", "开始", "打包", "压缩",
                "解压", "启动", "安装", "卸载", "测试", "登录", "退出")

# 通用单字动词黑名单：太泛，学了会抢命中（"查一下/看一下"应留给 L1/L3）
_SINGLE_VERB_BLACKLIST = {
    "查", "看", "找", "弄", "搞", "写", "读", "删", "打", "开", "关", "放",
    "拿", "用", "说", "问", "想", "给", "要", "去", "来", "干", "做", "改",
    "建", "加", "减", "算", "走", "翻", "调", "取", "发", "传", "收", "存",
    "报", "批", "审", "测", "试", "记",
}


def extract_action_word(text: str) -> Optional[str]:
    """从消息中提取用户特有动作词（用于入库学习）。

    启发式（真实设计取舍，从右往左）：
      定位句尾动词后缀（了/一下/一哈/起来/掉/完/好），取紧邻的 1 字动词。
      "把那个日志扬了" → 后缀"了"，动词"扬" → "扬了"
      "把脚本跑一下"   → 后缀"一下"，动词"跑" → "跑一下"
      单字动词在黑名单（查/看/找…泛词）→ 拒绝学习（留给 L1/L3）；
      标准动作词（L1 已覆盖）→ 拒绝学习。
    实测教训：从左往右贪婪会吞宾语（"日志扬了"/"志扬了"），词条无法在变体句命中。
    """
    text = (text or "").strip()
    if not text:
        return None
    m = re.search(r"(了|一下|一哈|起来|掉|完|好)$", text)
    if not m:
        return None
    suffix = m.group(1)
    vi = m.start(1) - 1  # 动词候选位置（后缀前一字符）
    if vi < 0:
        return None
    verb = text[vi]
    if not (0x4E00 <= ord(verb) <= 0x9FFF):
        return None
    if verb in _SINGLE_VERB_BLACKLIST:
        return None
    word = verb + suffix
    if word in _STOP_WORDS or word.startswith(_STOP_PREFIX):
        return None
    return word


# ============ 三层漏斗（供验证脚本与 ai.py 共用） ============
class FunnelRoute:
    __slots__ = ("label", "reason", "confidence", "layer", "word", "llm_called")

    def __init__(self, label, reason, confidence, layer, word=None, llm_called=False):
        self.label = label
        self.reason = reason
        self.confidence = confidence
        self.layer = layer
        self.word = word
        self.llm_called = llm_called

    def to_dict(self):
        return {
            "label": self.label, "reason": self.reason, "confidence": self.confidence,
            "layer": self.layer, "word": self.word, "llm_called": self.llm_called,
        }


def funnel_route(text: str, llm_classify=None, lexicon: Optional[LexiconDB] = None) -> FunnelRoute:
    """三层漏斗纯逻辑（不依赖 ai.py）。

    llm_classify(text) -> "tool"|"chat"|"web"：由调用方注入（ai.py 传 safe_api_request 包装；
    验证脚本传 mock）。若为 None，L3 直接返回 chat 并标记未调用（仅测试用）。
    """
    # L1：静态正则（极速拦截）
    hit = rule_match(text)
    if hit:
        return FunnelRoute(hit[0], hit[1], hit[2], layer="L1")

    # L3 触发检查：长消息 / 因果词 → 跳过词库，强制大模型
    trigger, why = should_llm_route(text)
    if trigger:
        label = llm_classify(text) if llm_classify else "chat"
        if label == "tool":
            word = extract_action_word(text)
            if word and lexicon is not None:
                lexicon.learn(word, "tool")
        return FunnelRoute(label, "L3:%s" % why, 1.0 if llm_classify else 0.5,
                           layer="L3", llm_called=bool(llm_classify))

    # L2：动态词库（命中 0 API）
    if lexicon is not None:
        m = lexicon.match(text)
        if m:
            word, intent = m
            return FunnelRoute(intent, "L2词库命中[%s]" % word, 0.9, layer="L2", word=word)

    # 兜底 L3：大模型深度分析
    label = llm_classify(text) if llm_classify else "chat"
    if label == "tool":
        word = extract_action_word(text)
        if word and lexicon is not None:
            lexicon.learn(word, "tool")
    return FunnelRoute(label, "L3兜底", 1.0 if llm_classify else 0.5,
                       layer="L3", llm_called=bool(llm_classify))
