import json
import os
import threading
import logging
import sqlite3
from datetime import datetime
from cl import MEMORY_FILE, DEFAULT_ADMIN

logger = logging.getLogger(__name__)

DEFAULT_MEMORY = {
    "admin_list": [DEFAULT_ADMIN],
    "group_chat_enabled": False,
    "admin_mode": False,
    "news_count": 0,
    "news_history": [],
    "news_enabled": True,
    "last_talk": "",
}

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
    # ========== 长期记忆相关 ==========
    def add_memory(self, memory_text: str) -> str:
        """新增一条记忆，超出100条则淘汰最早（FIFO）"""
        memories = self.get('memories', [])
        memories.append(memory_text)
        if len(memories) > 100:
            removed = memories.pop(0)
        self.set('memories', memories)
        return f"已记住：{memory_text}"

    def search_memories(self, query: str) -> list[str]:
        """关键词匹配，返回命中的记忆列表"""
        memories = self.get('memories', [])
        if not memories:
            return []
        # 分词：按空格和常见标点分割
        import re
        keywords = re.split(r'[\s，。、！？,;.!?\s]+', query)
        keywords = [kw for kw in keywords if len(kw) >= 2]  # 过滤单字词
        if not keywords:
            return []
        matched = []
        for mem in memories:
            for kw in keywords:
                if kw in mem:
                    matched.append(mem)
                    break
        return matched

    def forget_memory(self, keyword: str) -> str:
        """删除包含关键词的记忆（模糊匹配）"""
        memories = self.get('memories', [])
        if not memories:
            return "当前没有记忆可删除"
        # 备份原列表，用于判断是否删除成功
        original_len = len(memories)
        new_memories = [mem for mem in memories if keyword not in mem]
        if len(new_memories) == original_len:
            return f"未找到包含「{keyword}」的记忆"
        self.set('memories', new_memories)
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
