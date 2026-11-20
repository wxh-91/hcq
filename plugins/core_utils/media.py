import os
_ROOT = os.environ.get("PROJECT_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import os
import shutil
import logging
from pathlib import Path


logger = logging.getLogger(__name__)

BILI_CACHE = Path("/root/.cache/nonebot2/nonebot_plugin_resolver2")
ROBOT_ROOT = Path(_ROOT)
DOWNLOADS = ROBOT_ROOT / "downloads"


def _find_latest_mp4() -> Path | None:
    """在 B站插件缓存目录中找最新的 .mp4 文件"""
    if not BILI_CACHE.exists():
        return None
    mp4_files = list(BILI_CACHE.glob("*.mp4"))
    if not mp4_files:
        return None
    return max(mp4_files, key=lambda p: p.stat().st_mtime)


async def convert_latest_mp4_to_mp3() -> str:
    """
    自动找 B站缓存里最新的 mp4，转为 mp3 存到 /root/robot/，
    成功与否都删除原 mp4。
    """
    mp4_path = _find_latest_mp4()
    if mp4_path is None:
        return "缓存目录里没有找到 mp4 文件"

    mp3_name = mp4_path.stem + ".mp3"
    mp3_path = ROBOT_ROOT / mp3_name

    cmd = [
        "ffmpeg", "-y",
        "-i", str(mp4_path),
        "-vn",
        "-acodec", "libmp3lame",
        "-q:a", "2",
        str(mp3_path)
    ]
    logger.info(f"[media] 开始转码: {mp4_path} -> {mp3_path}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    # 无论成功与否，删除原 mp4
    try:
        mp4_path.unlink()
        logger.info(f"[media] 已删除原文件: {mp4_path}")
    except Exception as e:
        logger.warning(f"[media] 删除原文件失败: {e}")

    if proc.returncode != 0:
        err = stderr.decode("utf-8", "replace")[-500:]
        logger.error(f"[media] ffmpeg 转码失败: {err}")
        return f"转码失败：{err}"

    if not mp3_path.exists():
        return "转码失败：未生成 mp3 文件"

    return f"转码完成：{mp3_path}"


def clean_media(scope: str) -> str:
    """
    清理 mp4/mp3 文件。
    scope 可选：
        "downloads" —— 清空 downloads 目录下所有内容
        "robot"     —— 删除 /root/robot/ 下所有 .mp4 和 .mp3
        "both"      —— 两者都做
    """
    messages = []

    if scope in ("downloads", "both"):
        if DOWNLOADS.exists():
            count = 0
            for child in DOWNLOADS.iterdir():
                try:
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                    count += 1
                except Exception as e:
                    logger.warning(f"[media] 删除 {child} 失败: {e}")
            messages.append(f"downloads 目录已清空（{count} 项）")
        else:
            messages.append("downloads 目录不存在")

    if scope in ("robot", "both"):
        count = 0
        for pattern in ("*.mp4", "*.mp3"):
            for f in ROBOT_ROOT.glob(pattern):
                try:
                    f.unlink()
                    count += 1
                except Exception as e:
                    logger.warning(f"[media] 删除 {f} 失败: {e}")
        messages.append(f"robot 根目录已删除 mp4/mp3（{count} 个文件）")

    return "\n".join(messages)
