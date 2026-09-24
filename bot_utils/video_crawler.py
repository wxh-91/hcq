import asyncio
import os
import shutil
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# ===== 配置 =====
CRAWLER_SRC = "/root/robot"              # Node 脚本源目录
DOWNLOAD_DIR = "/root/robot/downloads"   # 最终 mp4 存放目录
COOKIE_SRC = "/root/cookies.json"        # cookie 备份路径


async def _run_node(script_path: str, args: list, cwd: str, timeout: int) -> tuple:
    """执行 Node 脚本，返回 (returncode, stdout, stderr)"""
    proc = await asyncio.create_subprocess_exec(
        "node", script_path, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"超时（{timeout}s）"
    return proc.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


async def _refresh_cookie(work_dir: str) -> bool:
    """在源目录执行 diag.js 刷新 cookie（cwd 指向工作目录，输出文件落在那）"""
    diag_src = os.path.join(CRAWLER_SRC, "diag.js")

    logger.info("[crawler] 开始刷新 cookie（需 90 秒）...")
    rc, out, err = await _run_node(diag_src, [], work_dir, timeout=180)
    if rc != 0:
        logger.error(f"[crawler] diag.js 失败: rc={rc}, err={err[-500:]}")
        return False

    new_cookie = os.path.join(work_dir, "cookies.json")
    if os.path.exists(new_cookie):
        shutil.copy(new_cookie, COOKIE_SRC)

    logger.info("[crawler] cookie 刷新完成")
    return True

async def crawl_video(video_url: str) -> dict:
    """
    端到端爬取新站点视频。
    返回：{"success": bool, "mp4_path": str, "error": str}
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work_dir = f"/root/robot/crawl_work_{timestamp}"
    os.makedirs(work_dir, exist_ok=True)

    try:
        # 准备 cookie 到工作目录（脚本从 cwd 读取）
        cookie_in_work = os.path.join(work_dir, "cookies.json")
        if os.path.exists(COOKIE_SRC):
            shutil.copy(COOKIE_SRC, cookie_in_work)

        # 用绝对路径执行源目录下的脚本，cwd=工作目录（输出落到工作目录）
        fetch_path = os.path.join(CRAWLER_SRC, "fetch_by_id.js")
        rc, out, err = await _run_node(fetch_path, [video_url], work_dir, timeout=1800)

        # 检测 cookie 过期
        if rc != 0 and ("接口非 200" in out or "cookie" in out.lower() or "401" in out):
            logger.warning("[crawler] 疑似 cookie 过期，尝试刷新...")
            if await _refresh_cookie(work_dir):
                rc, out, err = await _run_node(fetch_path, [video_url], work_dir, timeout=1800)

        if rc != 0:
            return {
                "success": False,
                "error": f"fetch_by_id.js 失败 (rc={rc})\nstdout: {out[-3000:]}\nstderr: {err[-3000:]}",
                "mp4_path": "",
            }

        ts_path = os.path.join(work_dir, "video.ts")
        if not os.path.exists(ts_path):
            return {"success": False, "error": "video.ts 未生成", "mp4_path": ""}

        # ffmpeg 转 mp4
        now = datetime.now()
        date_dir = os.path.join(DOWNLOAD_DIR, now.strftime("%m-%d"))
        os.makedirs(date_dir, exist_ok=True)
        mp4_name = now.strftime("%H%M%S") + ".mp4"
        mp4_path = os.path.join(date_dir, mp4_name)

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", ts_path, "-c", "copy", mp4_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, ff_err = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"success": False, "error": "ffmpeg 超时", "mp4_path": ""}

        if proc.returncode != 0 or not os.path.exists(mp4_path):
            return {
                "success": False,
                "error": f"ffmpeg 失败: {ff_err.decode('utf-8', 'replace')[-500:]}",
                "mp4_path": "",
            }

        return {"success": True, "mp4_path": mp4_path, "error": ""}

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
