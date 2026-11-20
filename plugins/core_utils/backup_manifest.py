"""
XMKJ.txt 解析与备份清单缓存

职责：
- 读取项目根目录下的 XMKJ.txt
- 调用 AI（tool 策略）解析出应备份的文件/目录清单
- 生成 backup_manifest.json（纯数组）与 backup_manifest.meta.json（记录 mtime）
- 提供 is_manifest_stale() 判断缓存是否失效，load_manifest() 读取清单
"""
import os
import re
import json
import logging

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.expanduser('~/robot')
XMKJ_PATH = os.path.join(PROJECT_ROOT, 'XMKJ.txt')
MANIFEST_PATH = os.path.join(PROJECT_ROOT, 'backup_manifest.json')
META_PATH = os.path.join(PROJECT_ROOT, 'backup_manifest.meta.json')

# 硬排除黑名单（Q9=C 代码侧兜底；prompt 侧也会说明）
BLACKLIST = ['backups', 'downloads']

PARSE_PROMPT_TEMPLATE = """你是项目备份清单解析器。下面是一份项目框架说明文件（XMKJ.txt），其中描述了项目的所有文件和目录。

你的任务：从中提取出【应该被打包备份的项目文件/目录清单】。

规则：
1. 只输出项目根目录下的顶层文件或目录。不要输出任何子文件。
   例如 XMKJ.txt 里展开了 bot_utils/ 下的多个文件，你只需输出 "bot_utils"，不要输出 "bot_utils/wj.py"。
2. 排除以下内容：
   - 外部关联组件（不在项目根目录内，如 ~/.pi/agent/ 下的任何内容）
   - 运行时数据文件：memory.json、scores.json、episodic.log、checkin_state.json、knowledge.db
   - 产出物目录：backups/、downloads/
   - 标注为「暂时废弃」的代码：web_server.py、web_ui.html
3. 保留以下内容（不要排除）：
   - 所有源代码文件（.py、.js、.ts）
   - 配置文件：models.json、.env
   - 文档：profile.md、XMKJ.txt
   - 目录：pi_backend/、bot_utils/
4. 输出格式：严格的 JSON 数组，每项包含：
   - path：相对项目根目录的路径
   - type："file" 或 "dir"
   - desc：一句话描述（从注释中提取或自行概括）
5. 只输出 JSON 数组本身，不要任何额外文字、解释、markdown 代码块标记。

示例输出：
[{{"path": "dz.py", "type": "file", "desc": "QQ端主入口"}}, {{"path": "bot_utils", "type": "dir", "desc": "工具函数包"}}]

项目框架文件内容：
---
{XMKJ_CONTENT}
---

请输出 JSON 数组："""


def _read_xmkj() -> str:
    with open(XMKJ_PATH, 'r', encoding='utf-8') as f:
        return f.read()


def _get_xmkj_mtime() -> float:
    return os.path.getmtime(XMKJ_PATH)


def _extract_json_array(text: str) -> list:
    """从 AI 返回文本中正则抠出 JSON 数组（Q7=B）"""
    match = re.search(r'\[[\s\S]*\]', text)
    if not match:
        raise ValueError("AI 返回中未找到 JSON 数组")
    return json.loads(match.group(0))


async def _parse_xmkj_via_ai() -> list:
    """调 AI（tool 策略）解析 XMKJ.txt，返回清单数组"""
    from ai import safe_api_request
    content = _read_xmkj()
    prompt = PARSE_PROMPT_TEMPLATE.replace('{XMKJ_CONTENT}', content)
    messages = [
        {"role": "system", "content": "你是一个严格的 JSON 输出器。只输出 JSON，不要任何解释。"},
        {"role": "user", "content": prompt},
    ]
    reply, _ = await safe_api_request(messages, strategy="tool", tools=None)
    if not reply:
        raise ValueError("AI 返回为空")
    items = _extract_json_array(reply)
    if not isinstance(items, list) or not items:
        raise ValueError("AI 返回的清单为空或格式错误")
    for it in items:
        if not isinstance(it, dict) or 'path' not in it:
            raise ValueError(f"清单项格式错误：{it}")
    return items


async def refresh_manifest() -> list:
    """重新解析 XMKJ.txt，刷新 manifest 与 meta"""
    items = await _parse_xmkj_via_ai()
    with open(MANIFEST_PATH, 'w', encoding='utf-8') as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    meta = {"source_mtime": _get_xmkj_mtime()}
    with open(META_PATH, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    logger.info(f"[backup_manifest] 已刷新清单，共 {len(items)} 项")
    return items


def is_manifest_stale() -> bool:
    """判断缓存是否失效：缺文件或 mtime 变化"""
    if not os.path.exists(MANIFEST_PATH) or not os.path.exists(META_PATH):
        return True
    try:
        with open(META_PATH, 'r', encoding='utf-8') as f:
            meta = json.load(f)
        stored_mtime = meta.get('source_mtime')
        if stored_mtime is None:
            return True
        return _get_xmkj_mtime() != stored_mtime
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"[backup_manifest] 读取 meta 失败: {e}")
        return True


def load_manifest() -> list:
    """读取当前清单数组"""
    with open(MANIFEST_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)
