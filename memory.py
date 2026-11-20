import json
import os
import threading
import logging
import sqlite3
from datetime import datetime
from dataclasses import dataclass, asdict
from cl import MEMORY_FILE, DEFAULT_ADMIN

# ==================== 长期记忆生命周期参数 ====================
DECAY_HALF_LIFE_DAYS = 30.0    # confidence 半衰期（天）
DORMANT_THRESHOLD = 0.3        # 低于此值进入休眠
FORGET_THRESHOLD = 0.1         # 低于此值进入 fading（待清除）
CLEANUP_GRACE_DAYS = 7         # fading 保持天数，之后物理删除
ACCESS_BOOST = 0.2             # 每次命中时的 confidence 增量
INITIAL_CONFIDENCE = 0.8       # 新记忆初始 confidence
MAX_CONFIDENCE = 1.0


@dataclass
class MemoryRecord:
    """长期记忆的结构化记录"""
    content: str
    created_at: str
    last_accessed_at: str
    access_count: int = 0
    confidence: float = INITIAL_CONFIDENCE
    status: str = "active"      # active / dormant / fading

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryRecord":
        return cls(**d)

    @classmethod
    def from_legacy(cls, content: str, now: str) -> "MemoryRecord":
        """旧格式（纯字符串）迁移"""
        return cls(
            content=content,
            created_at=now,
            last_accessed_at=now,
            access_count=0,
            confidence=INITIAL_CONFIDENCE,
            status="active",
        )

logger = logging.getLogger(__name__)
from config_center import get_config

DEFAULT_MEMORY = {
    "admin_list": [DEFAULT_ADMIN],
    "group_chat_enabled": False,
    "admin_mode": False,
    "news_count": 0,
    "news_history": [],
    "news_enabled": True,
    "last_talk": "",
}

PROFILE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profile.md")


def load_profile() -> str:
    """读取 profile.md，返回全文（去掉首尾空白）。文件缺失或读取失败返回空字符串。"""
    try:
        with open(PROFILE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.warning(f"profile.md 不存在: {PROFILE_FILE}")
        return ""
    except IOError as e:
        logger.error(f"读取 profile.md 失败: {e}", exc_info=True)
        return ""


class Memory:
    _instance = None
    _file_lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._data = None
        return cls._instance
    
    def load(self):
        if self._data is not None:
            return self._data
        with self._file_lock:
            try:
                with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                data = DEFAULT_MEMORY.copy()
            # 补全缺失字段
            for k, v in DEFAULT_MEMORY.items():
                if k not in data:
                    data[k] = v
            self._data = data
            return data
    
    def save(self, data=None):
        if data is None:
            data = self._data
        if data is None:
            data = DEFAULT_MEMORY.copy()
        with self._file_lock:
            tmp_file = MEMORY_FILE + '.tmp'
            try:
                with open(tmp_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_file, MEMORY_FILE)
                self._data = data
            except Exception as e:
                # 写入失败时清理临时文件
                if os.path.exists(tmp_file):
                    try:
                        os.remove(tmp_file)
                    except OSError:
                        pass
                logger.error(f"保存 memory 数据失败: {e}", exc_info=True)
                raise RuntimeError(f"保存数据失败: {str(e)}")

    def reload(self):
        self._data = None
        return self.load()
    
    def get(self, key, default=None):
        return self.load().get(key, default)
    
    def set(self, key, value):
        data = self.load()
        data[key] = value
        # 保护管理员列表
        if key == "admin_list":
            if DEFAULT_ADMIN not in value:
                value.append(DEFAULT_ADMIN)
                data[key] = value
        self.save(data)
    
    def is_admin(self, user_id: str) -> bool:
        admins = self.get('admin_list', [])
        if not admins:
            return str(user_id) == DEFAULT_ADMIN
        return str(user_id) in admins
    
    def is_admin_mode(self) -> bool:
        return self.get('admin_mode', False)

    # ========== 长期记忆生命周期辅助方法 ==========
    def _now_str(self) -> str:
        return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    def _migrate_memories(self, raw_list: list) -> list:
        """兼容旧格式：纯字符串 → MemoryRecord 字典；已是字典则补全字段"""
        now = self._now_str()
        result = []
        for item in raw_list:
            if isinstance(item, str):
                result.append(MemoryRecord.from_legacy(item, now).to_dict())
            elif isinstance(item, dict) and "content" in item:
                item.setdefault("created_at", now)
                item.setdefault("last_accessed_at", now)
                item.setdefault("access_count", 0)
                item.setdefault("confidence", INITIAL_CONFIDENCE)
                item.setdefault("status", "active")
                result.append(item)
        return result

    def _apply_decay(self, records: list) -> list:
        """对每条记录应用指数时间衰减，并更新 status"""
        now = datetime.now()
        for r in records:
            try:
                last = datetime.fromisoformat(r["last_accessed_at"])
            except (ValueError, TypeError, KeyError):
                last = now
            days = (now - last).total_seconds() / 86400.0
            if days <= 0:
                continue
            factor = 0.5 ** (days / DECAY_HALF_LIFE_DAYS)
            r["confidence"] = round(r.get("confidence", INITIAL_CONFIDENCE) * factor, 4)
            if r["confidence"] < FORGET_THRESHOLD:
                r["status"] = "fading"
            elif r["confidence"] < DORMANT_THRESHOLD:
                r["status"] = "dormant"
            else:
                r["status"] = "active"
        return records

    def _cleanup_forgotten(self, records: list) -> list:
        """物理删除 fading 状态且超过 grace 期的记录"""
        now = datetime.now()
        kept = []
        for r in records:
            if r.get("status") == "fading":
                try:
                    last = datetime.fromisoformat(r["last_accessed_at"])
                    if (now - last).days > CLEANUP_GRACE_DAYS:
                        continue
                except (ValueError, TypeError, KeyError):
                    pass
            kept.append(r)
        return kept

    # ========== 长期记忆相关 ==========
    def add_memory(self, memory_text: str) -> str:
        """新增一条记忆。不再 FIFO 淘汰，由生命周期管理存留。"""
        raw = self.get('memories', [])
        records = self._migrate_memories(raw)
        records = self._apply_decay(records)
        records = self._cleanup_forgotten(records)
        now = self._now_str()
        records.append(MemoryRecord(
            content=memory_text,
            created_at=now,
            last_accessed_at=now,
        ).to_dict())
        # 中央控制台：长期记忆上限截断（保留最近 N 条）
        _max_records = get_config("MAX_MEMORY_RECORDS", 100)
        if len(records) > _max_records:
            records = records[-_max_records:]
        self.set('memories', records)
        return f"已记住：{memory_text}"

    def search_memories(self, query: str) -> list[str]:
        """关键词匹配，返回命中的记忆内容。命中时强化（access_count+1, confidence+0.2）。"""
        raw = self.get('memories', [])
        if not raw:
            return []
        records = self._migrate_memories(raw)
        records = self._apply_decay(records)
        records = self._cleanup_forgotten(records)

        import re
        keywords = re.split(r'[\s，。、！？,;.!?\s]+', query)
        keywords = [kw for kw in keywords if len(kw) >= 2]
        if not keywords:
            self.set('memories', records)
            return []

        now_str = self._now_str()
        matched = []
        for r in records:
            content = r.get("content", "")
            for kw in keywords:
                if kw in content:
                    # 强化
                    r["access_count"] = r.get("access_count", 0) + 1
                    r["last_accessed_at"] = now_str
                    r["confidence"] = min(
                        MAX_CONFIDENCE,
                        r.get("confidence", INITIAL_CONFIDENCE) + ACCESS_BOOST
                    )
                    if r.get("status") in ("dormant", "fading"):
                        r["status"] = "active"
                    matched.append(r)
                    break

        self.set('memories', records)
        # 遗忘曲线排序：confidence 降序，同分按 access_count 降序（权重最高者最先进入 AI 视野）
        matched.sort(
            key=lambda r: (r.get("confidence", 0.0), r.get("access_count", 0)),
            reverse=True,
        )
        return [r.get("content", "") for r in matched]

    def forget_memory(self, keyword: str) -> str:
        """删除包含关键词的记忆（模糊匹配）"""
        raw = self.get('memories', [])
        if not raw:
            return "当前没有记忆可删除"
        records = self._migrate_memories(raw)
        original_len = len(records)
        new_records = [r for r in records if keyword not in r.get("content", "")]
        if len(new_records) == original_len:
            return f"未找到包含「{keyword}」的记忆"
        self.set('memories', new_records)
        return f"已删除包含「{keyword}」的记忆"

class KnowledgeDB:
    _instance = None
    _db_path = "knowledge.db"

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_db()
        return cls._instance

    def _init_db(self):
        """初始化数据库，创建三张表"""
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.cursor()

            # 表1：用户偏好
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_preferences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT UNIQUE NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # 表2：解决方案历史
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS solution_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    problem TEXT NOT NULL,
                    solution TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # 表3：对话摘要
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS conversation_summary (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
            logger.info("知识库数据库初始化完成")

    def upsert_preference(self, key: str, value: str) -> None:
        """插入或更新用户偏好"""
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO user_preferences (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            ''', (key, value))
            conn.commit()

    def get_preference(self, key: str) -> str | None:
        """获取用户偏好"""
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM user_preferences WHERE key = ?', (key,))
            row = cursor.fetchone()
            return row[0] if row else None

    def add_solution(self, problem: str, solution: str) -> None:
        """添加解决方案"""
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO solution_history (problem, solution, created_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (problem, solution))
            conn.commit()

    def search_solutions(self, keyword: str, limit: int = 5) -> list[dict]:
        """搜索历史解决方案（关键词匹配）"""
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT problem, solution, created_at
                FROM solution_history
                WHERE problem LIKE ? OR solution LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
            ''', (f'%{keyword}%', f'%{keyword}%', limit))
            rows = cursor.fetchall()
            return [{"problem": r[0], "solution": r[1], "created_at": r[2]} for r in rows]

    def save_conversation_summary(self, session_id: str, summary: str) -> None:
        """保存对话摘要"""
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO conversation_summary (session_id, summary, created_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (session_id, summary))
            conn.commit()

knowledge_db = KnowledgeDB()
