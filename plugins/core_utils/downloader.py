import os
_ROOT = os.environ.get("PROJECT_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import json
import os
import tempfile
import re
import shutil
import logging
import secrets
import ssl
import time
import subprocess
import urllib.request
from datetime import datetime
from urllib.parse import urlparse

from aup_core import aup_tool
from plugins.core_utils import file_delivery, trust, async_outbox
from memory_center import MemoryCenter

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# ---------------------------------------------------------------------------
# 常量与任务状态
# ---------------------------------------------------------------------------
DOWNLOAD_DIR = _ROOT + "/downloads"
TASK_STATE_FILE = os.path.join(DOWNLOAD_DIR, "m3u8_tasks.json")
# 清单分段数超过该阈值时转入后台任务，不在同步请求内等待
ASYNC_SEG_THRESHOLD = 60
SYNC_TIMEOUT = 240          # 同步加密流内部超时（低于 ask_ai 全局 300s）
BG_TIMEOUT = 7200          # 后台任务超时（2 小时；525 段单线程实测约 3.75s/段需 33 分钟以上）
MAX_RETAINED_TASKS = 50

_TASKS: dict = {}
_BG_TASKS: set = set()


# ---------------------------------------------------------------------------
# 任务状态持久化
# ---------------------------------------------------------------------------
def _load_tasks() -> None:
    if _TASKS:
        return
    try:
        if os.path.isfile(TASK_STATE_FILE):
            with open(TASK_STATE_FILE, encoding="utf-8") as f:
                _TASKS.update(json.load(f))
        # 启动对账：进程重启后，未完成任务的 worker 已随进程消失，
        # 不能永久停留在 running/queued，标记为中断失败。
        changed = False
        for rec in _TASKS.values():
            if rec.get("status") in ("running", "queued"):
                rec["status"] = "failed"
                rec["error"] = "后端重启，后台任务中断"
                changed = True
        if changed:
            _save_tasks()
    except Exception as e:
        logger.warning("[DOWNLOADER] 任务状态读取失败: %s", e)


def _save_tasks() -> None:
    try:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        # 只保留最近 N 条，避免状态文件无限增长
        if len(_TASKS) > MAX_RETAINED_TASKS:
            keep = sorted(
                _TASKS.values(), key=lambda r: r.get("updated", ""), reverse=True
            )[:MAX_RETAINED_TASKS]
            _TASKS.clear()
            for r in keep:
                _TASKS[r["task_id"]] = r
        tmp = TASK_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_TASKS, f, ensure_ascii=False, indent=1)
        os.replace(tmp, TASK_STATE_FILE)
    except Exception as e:
        logger.warning("[DOWNLOADER] 任务状态落盘失败: %s", e)


def _new_task_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"dl_{stamp}_{secrets.token_hex(3)}"


def _unique_dest(stem: str) -> str:
    base = os.path.join(DOWNLOAD_DIR, f"{stem}.mp4")
    n = 1
    while os.path.exists(base):
        base = os.path.join(DOWNLOAD_DIR, f"{stem}_{n}.mp4")
        n += 1
    return base

def _runtime_identity() -> dict:
    """抓取本轮请求的归属与会话标识（后台 Worker 脱离请求上下文后回推用）。"""
    owner = file_delivery.current_owner() or ""
    sender = trust.runtime_sender() or ""
    sid = sender
    m = re.search(r"dsh_(.+)$", sender)
    if m:
        sid = m.group(1)
    return {"owner_ref": owner, "session_key": sender, "client_sid": sid}


# ---------------------------------------------------------------------------
# 清单拉取与解析（Python 侧完成，避免再启一个易挂的检测子进程）
# ---------------------------------------------------------------------------
def _fetch_playlist(url: str, referer: str, timeout: int = 15) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": referer or "",
        },
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read().decode("utf-8", "replace")


def _parse_playlist(text: str, url: str) -> dict:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    segs = []
    key_line = ""
    base = url.rsplit("/", 1)[0] + "/"
    for ln in lines:
        if ln.startswith("#EXT-X-KEY"):
            key_line = ln
        elif not ln.startswith("#"):
            segs.append(ln if ln.startswith("http") else base + ln.lstrip("./"))
    return {"has_key": bool(key_line), "segments": segs, "key_line": key_line}


# ---------------------------------------------------------------------------
# 加密流脚本（带分段超时/重试、断点续传、进度上报）
# ---------------------------------------------------------------------------
TPL = r'''#!/bin/bash
set -u
WORK_DIR="__WORK_DIR__"
cd "$WORK_DIR" || exit 1
rm -rf anime_temp; mkdir -p anime_temp || exit 1
cd anime_temp || exit 1
M3U8_URL="__URL__"
REFERER="__REFERER__"
OUTPUT_NAME="__OUTPUT_NAME__"
PROGRESS="__PROGRESS__"
LOGF="__LOGF__"
fail() { echo "FAILED:$1" >> "$LOGF"; echo "❌ $1" >> "$LOGF"; exit "${2:-1}"; }
echo "0 0" > "$PROGRESS"
echo "📥 下载 playlist..." >> "$LOGF"
curl -s -f --connect-timeout 10 --max-time 20 --retry 2 --retry-delay 1 \
  -H "Referer: $REFERER" -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)" \
  "$M3U8_URL" -o playlist.m3u8 || fail "playlist 下载失败"
KEY_LINE=$(grep -m1 '#EXT-X-KEY' playlist.m3u8)
[ -z "$KEY_LINE" ] && fail "未找到 #EXT-X-KEY"
KEY_URI=$(printf '%s' "$KEY_LINE" | grep -o 'URI="[^"]*"' | sed 's/URI="//;s/"//')
case "$KEY_URI" in http*) ;; *) KEY_URI="$(dirname "$M3U8_URL")/$KEY_URI" ;; esac
IV=$(printf '%s' "$KEY_LINE" | grep -o 'IV=0x[0-9a-fA-F]*' | sed 's/IV=//')
IV_HEX="${IV#0x}"
[ -z "$IV_HEX" ] && IV_HEX="00000000000000000000000000000000"
curl -s -f --connect-timeout 10 --max-time 20 -H "Referer: $REFERER" "$KEY_URI" -o key.bin || fail "密钥下载失败"
[ -s key.bin ] || fail "密钥为空"
KEY_HEX=$(xxd -p -c 256 key.bin | tr -d '\n')
BASE_URL=$(dirname "$M3U8_URL")/
grep -v '^#' playlist.m3u8 | grep -v '^[[:space:]]*$' > ts_raw.txt
while IFS= read -r line; do
  case "$line" in http*) echo "$line" ;; *) echo "${BASE_URL}${line#./}" ;; esac
done < ts_raw.txt > ts_list.txt
TOTAL=$(wc -l < ts_list.txt)
[ "$TOTAL" -eq 0 ] && fail "清单为空"
OK=0; BAD=0; idx=0
: > filelist.txt; : > bad_list.txt
while IFS= read -r ts_url; do
  idx=$((idx+1))
  raw=$(printf '%s' "$ts_url" | sed 's/?.*//')
  filename=$(basename "$raw"); base="${filename%.*}"
  enc="$base.enc"; dec="$base.ts"
  if [ -s "$dec" ] && ffprobe -v error -select_streams v:0 -show_entries stream=codec_type -of csv=p=0 "$dec" 2>/dev/null | grep -q video; then
    OK=$((OK+1)); printf '%s\n' "$dec" >> filelist.txt
    echo "$((OK+BAD)) $TOTAL" > "$PROGRESS"; continue
  fi
  got=0
  for try in 1 2 3; do
    if curl -s -f --connect-timeout 10 --max-time 20 --retry 1 -H "Referer: $REFERER" -H "User-Agent: Mozilla/5.0" "$ts_url" -o "$enc"; then
      sz=$(stat -c%s "$enc" 2>/dev/null || echo 0)
      [ "$sz" -gt 200 ] && { got=1; break; }
    fi
    sleep 1
  done
  if [ "$got" -eq 0 ]; then
    echo "⚠️ 分段下载失败: $filename" >> "$LOGF"
    printf '%s\n' "$filename" >> bad_list.txt
    BAD=$((BAD+1)); rm -f "$enc"
    echo "$((OK+BAD)) $TOTAL" > "$PROGRESS"; continue
  fi
  openssl aes-128-cbc -d -in "$enc" -out "$dec" -K "$KEY_HEX" -iv "$IV_HEX" 2>/dev/null
  if ! ffprobe -v error -select_streams v:0 -show_entries stream=codec_type -of csv=p=0 "$dec" 2>/dev/null | grep -q video; then
    SEQ_IV=$(printf '%032x' $((idx-1)))
    openssl aes-128-cbc -d -in "$enc" -out "$dec" -K "$KEY_HEX" -iv "$SEQ_IV" 2>/dev/null
  fi
  if [ -s "$dec" ] && ffprobe -v error -select_streams v:0 -show_entries stream=codec_type -of csv=p=0 "$dec" 2>/dev/null | grep -q video; then
    OK=$((OK+1)); printf '%s\n' "$dec" >> filelist.txt
  else
    echo "⚠️ 分段解密/校验失败: $filename" >> "$LOGF"
    printf '%s\n' "$filename" >> bad_list.txt
    BAD=$((BAD+1)); rm -f "$dec"
  fi
  rm -f "$enc"
  echo "$((OK+BAD)) $TOTAL" > "$PROGRESS"
done < ts_list.txt

if [ "$BAD" -gt 0 ]; then
  echo "❌ 切片完整性校验未通过：$OK/$TOTAL，缺失 $(wc -l < bad_list.txt) 段，已拒绝合并" >> "$LOGF"
  echo "INCOMPLETE:$OK/$TOTAL" >> "$LOGF"
  rm -f ./*.ts ./*.enc filelist.txt bad_list.txt playlist.m3u8 ts_raw.txt ts_list.txt key.bin
  exit 2
fi

: > concat.txt
while IFS= read -r f; do printf "file '%s'\n" "$f" >> concat.txt; done < filelist.txt
FINAL_FILE="$WORK_DIR/$OUTPUT_NAME.mp4"
if ! ffmpeg -y -f concat -safe 0 -i concat.txt -c copy "$FINAL_FILE" >> "$LOGF" 2>&1; then
  { echo "#EXTM3U"; echo "#EXT-X-VERSION:3"; echo "#EXT-X-TARGETDURATION:10"; echo "#EXT-X-MEDIA-SEQUENCE:0"
    while IFS= read -r f; do echo "#EXTINF:10.0,"; echo "$f"; done < filelist.txt
    echo "#EXT-X-ENDLIST"; } > decrypted.m3u8
  ffmpeg -y -allowed_extensions ALL -i decrypted.m3u8 -c copy "$FINAL_FILE" >> "$LOGF" 2>&1 || true
fi
if [ -s "$FINAL_FILE" ] && ffprobe -v error -show_entries format=duration -of csv=p=0 "$FINAL_FILE" 2>/dev/null | grep -qE '^[0-9]'; then
  echo "SUCCESS:$FINAL_FILE" >> "$LOGF"
  rm -f ./*.ts ./*.enc concat.txt decrypted.m3u8 filelist.txt bad_list.txt playlist.m3u8 ts_raw.txt ts_list.txt key.bin
  echo "CLEANUP:anime_temp 剩余条目 $(ls -A | wc -l) 个" >> "$LOGF"
else
  echo "❌ 合并/转码失败" >> "$LOGF"; exit 1
fi
'''


def _build_encrypted_script(work_dir: str, url: str, referer: str,
                             output_name: str, progress_file: str,
                             log_file: str) -> str:
    """下载脚本：逐段下载+解密+ffprobe 校验；切片不齐严禁合并；成功后删中间文件。"""
    return (TPL.replace("__WORK_DIR__", work_dir)
               .replace("__URL__", str(url).replace('"', '\\"'))
               .replace("__REFERER__", str(referer).replace('"', '\\"'))
               .replace("__OUTPUT_NAME__", str(output_name))
               .replace("__PROGRESS__", progress_file)
               .replace("__LOGF__", log_file))


def _read_progress(path: str) -> tuple:
    try:
        with open(path, encoding="utf-8") as f:
            d, t = f.read().split()
        return int(d), int(t)
    except Exception:
        return 0, 0


def _tail_file(path: str, n: int = 40) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def _parse_success(output: str):
    for line in output.splitlines():
        if line.startswith("SUCCESS:"):
            return line.split(":", 1)[1].strip()
    return None


async def _reap(proc) -> None:
    if proc is not None and proc.returncode is None:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 后台任务 worker
# ---------------------------------------------------------------------------
async def _bg_worker(task_id: str) -> None:
    rec = _TASKS.get(task_id)
    if not rec:
        return
    work_dir = os.path.join(DOWNLOAD_DIR, "m3u8", task_id)
    os.makedirs(work_dir, exist_ok=True)
    progress = os.path.join(work_dir, "progress.txt")
    logf = os.path.join(work_dir, "worker.log")
    open(logf, "w", encoding="utf-8").close()

    script = _build_encrypted_script(
        work_dir, rec["url"], rec["referer"], rec["output_name"], progress, logf
    )
    sp = os.path.join(work_dir, "download.sh")
    with open(sp, "w", encoding="utf-8") as f:
        f.write(script)
    os.chmod(sp, 0o755)

    proc = None
    logf_handle = None
    started = time.monotonic()
    try:
        rec["status"] = "running"
        rec["updated"] = datetime.now().isoformat(timespec="seconds")
        _save_tasks()

        logf_handle = open(logf, "a", encoding="utf-8")
        proc = await asyncio.create_subprocess_shell(
            f"bash {sp}",
            stdout=logf_handle,
            stderr=subprocess.STDOUT,
            cwd=work_dir,
        )
        # 定时轮询进度文件与日志文件，不依赖 stdout 行回调
        while proc.returncode is None:
            await asyncio.sleep(2)
            d, t = _read_progress(progress)
            rec["done"], rec["total"] = d, t
            rec["log_tail"] = _tail_file(logf)
            rec["updated"] = datetime.now().isoformat(timespec="seconds")
            _save_tasks()
            if time.monotonic() - started > BG_TIMEOUT:
                await _reap(proc)
                raise RuntimeError("后台下载超时（%d 分钟）" % (BG_TIMEOUT // 60))

        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            await _reap(proc)
            raise RuntimeError("后台进程退出等待超时")

        if proc.returncode != 0:
            _tail = _tail_file(logf)
            if proc.returncode == 2 or "INCOMPLETE:" in _tail:
                _inc = ""
                for _l in _tail.splitlines():
                    if _l.startswith("INCOMPLETE:"):
                        _inc = _l.split(":", 1)[1].strip()
                        break
                raise RuntimeError("切片不完整（%s），已拒绝合并并清理临时文件" % (_inc or "存在缺失段"))
            raise RuntimeError(f"脚本退出码 {proc.returncode}")

        rec["log_tail"] = _tail_file(logf)
        final = _parse_success(rec.get("log_tail", ""))
        if not final or not os.path.exists(final):
            raise RuntimeError("未生成最终视频文件")

        dest = _unique_dest(rec["output_name"])
        shutil.move(final, dest)
        rec["status"] = "done"
        rec["result_path"] = dest
        rec["error"] = ""
        rec["updated"] = datetime.now().isoformat(timespec="seconds")
        _save_tasks()

        from plugins.core_utils import notify_bridge
        await notify_bridge.notify(
            f"后台视频下载完成：{dest}", title="下载完成", level="info"
        )

        # 后台产物闭环：登记受控附件 → 写回记忆会话 → 入待发消息箱，前端轮询自动插卡
        try:
            ident = {
                "owner_ref": rec.get("owner_ref", ""),
                "session_key": rec.get("session_key", ""),
                "client_sid": rec.get("client_sid", ""),
            }
            if ident["owner_ref"]:
                file_delivery.set_owner(ident["owner_ref"])
            frec = file_delivery.register_file(dest, name=os.path.basename(dest),
                                              mime="video/mp4")
            files = []
            marker_txt = ""
            if frec:
                marker_txt = file_delivery.marker(frec)
                files.append({"id": frec["file_id"], "name": frec["name"],
                              "size": frec["size"], "mime": frec.get("mime", "video/mp4")})
            msg_text = "后台视频下载完成，文件已附上，请直接下载。"
            if marker_txt:
                msg_text += "\n" + marker_txt
            if ident["session_key"]:
                MemoryCenter.append_message(f"private_{ident['session_key']}",
                                            "assistant", msg_text)
            async_outbox.enqueue(ident["owner_ref"], ident["session_key"],
                                  ident["client_sid"], msg_text, files)
        except Exception as _push_err:
            logger.warning("[DOWNLOADER] 后台产物回传异常: %s", _push_err)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
    except Exception as e:
        rec["status"] = "failed"
        rec["error"] = str(e)[:300]
        rec["updated"] = datetime.now().isoformat(timespec="seconds")
        _save_tasks()
        logger.warning("[DOWNLOADER] 后台任务 %s 失败: %s", task_id, e)
        try:
            from plugins.core_utils import notify_bridge
            await notify_bridge.notify(
                f"后台视频下载失败：{task_id} {e}", title="下载失败", level="warning"
            )
        except Exception:
            pass
    finally:
        # 零垃圾：失败/中断也清掉工作目录（日志尾巴已存进任务记录）
        shutil.rmtree(work_dir, ignore_errors=True)
        await _reap(proc)
        if logf_handle is not None:
            try:
                logf_handle.close()
            except Exception:
                pass
        _BG_TASKS.discard(asyncio.current_task())


# ---------------------------------------------------------------------------
# 同步加密流（短清单）
# ---------------------------------------------------------------------------
async def _run_encrypted_sync(url: str, referer: str, output_name: str) -> str:
    work_dir = tempfile.mkdtemp(prefix="m3u8_dl_")
    progress = os.path.join(work_dir, "progress.txt")
    logf = os.path.join(work_dir, "worker.log")
    script = _build_encrypted_script(
        work_dir, url, referer, output_name, progress, logf
    )
    sp = os.path.join(work_dir, "download.sh")
    with open(sp, "w", encoding="utf-8") as f:
        f.write(script)
    os.chmod(sp, 0o755)

    proc = None
    try:
        proc = await asyncio.create_subprocess_shell(
            f"bash {sp}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=SYNC_TIMEOUT
            )
        except asyncio.TimeoutError:
            await _reap(proc)
            shutil.rmtree(work_dir, ignore_errors=True)
            raise RuntimeError("下载超时（4 分钟），请改用后台任务或检查网络")

        output = (stdout.decode("utf-8", "replace")
                  or stderr.decode("utf-8", "replace")).strip()
        logtxt = _tail_file(logf, 200)
        final = _parse_success(logtxt) or _parse_success(output)
        if not final or not os.path.exists(final):
            shutil.rmtree(work_dir, ignore_errors=True)
            raise RuntimeError("加密流下载失败：" + ((logtxt or output)[-300:] or "脚本无输出"))

        dest = _unique_dest(output_name)
        shutil.move(final, dest)
        return dest
    finally:
        await _reap(proc)
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 同步无加密流：yt-dlp 优先，失败降级 FFmpeg
# ---------------------------------------------------------------------------
async def _run_plain_sync(url: str, referer: str, output_name: str) -> str:
    work_dir = tempfile.mkdtemp(prefix="m3u8_dl_")
    dest = _unique_dest(output_name)
    proc = None
    try:
        yt_cmd = (
            f'yt-dlp -o "{dest}" --referer "{referer}" '
            f'--user-agent "Mozilla/5.0" --no-check-certificate --no-playlist --no-cookies "{url}"'
        )
        logger.info("[DOWNLOADER] yt-dlp 命令: %s", yt_cmd)
        proc = await asyncio.create_subprocess_shell(
            yt_cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=work_dir,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            await _reap(proc)
            logger.warning("[DOWNLOADER] yt-dlp 超时，降级到 FFmpeg")
            proc = None
        else:
            output = stdout.decode("utf-8", "replace") or stderr.decode("utf-8", "replace")
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                shutil.rmtree(work_dir, ignore_errors=True)
                return dest
            logger.warning("[DOWNLOADER] yt-dlp 未生成有效文件，降级到 FFmpeg")

        ffmpeg_cmd = (
            f'ffmpeg -y -multiple_requests 1 '
            f'-headers "Referer: {referer}\\r\\nUser-Agent: Mozilla/5.0\\r\\n" '
            f'-i "{url}" -c copy "{dest}"'
        )
        logger.info("[DOWNLOADER] FFmpeg 命令: %s", ffmpeg_cmd)
        proc = await asyncio.create_subprocess_shell(
            ffmpeg_cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=work_dir,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=SYNC_TIMEOUT)
        except asyncio.TimeoutError:
            await _reap(proc)
            shutil.rmtree(work_dir, ignore_errors=True)
            raise RuntimeError("下载超时（4 分钟），请检查链接或网络")
        output = stdout.decode("utf-8", "replace") or stderr.decode("utf-8", "replace")

        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            shutil.rmtree(work_dir, ignore_errors=True)
            return dest
        shutil.rmtree(work_dir, ignore_errors=True)
        raise RuntimeError("所有下载方式均失败：" + output[-300:])
    finally:
        await _reap(proc)
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 对外工具入口
# ---------------------------------------------------------------------------
async def download_m3u8(url: str, referer: str = None, output_name: str = "video") -> str:
    """根据 m3u8 链接下载视频：加密流自动解密合并；长清单转入后台任务。"""
    required_bins = ["curl", "openssl", "ffmpeg"]
    if not hasattr(download_m3u8, "_checked"):
        missing = [b for b in required_bins if not shutil.which(b)]
        if missing:
            return f"缺少系统依赖：{', '.join(missing)}，请安装后重试"
        download_m3u8._checked = True

    logger.info("[DOWNLOADER] 开始下载: url=%s, referer=%s, output_name=%s",
                url, referer, output_name)

    if not referer:
        parsed = urlparse(url)
        referer = f"{parsed.scheme}://{parsed.netloc}/"
        logger.info("[DOWNLOADER] 自动补全 referer: %s", referer)

    output_name = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fa5]', '_', output_name)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    _load_tasks()

    # 拉取并解析清单；失败时退到直连兜底（yt-dlp/ffmpeg 自行处理）
    try:
        text = _fetch_playlist(url, referer)
        meta = _parse_playlist(text, url)
    except Exception as e:
        logger.warning("[DOWNLOADER] 清单解析失败，转直连兜底: %s", e)
        meta = {"has_key": False, "segments": []}

    # 长加密清单 → 后台任务，不在同步请求内等待
    if meta["has_key"] and len(meta["segments"]) > ASYNC_SEG_THRESHOLD:
        task_id = _new_task_id()
        _TASKS[task_id] = {
            "task_id": task_id,
            "url": url,
            "referer": referer,
            "output_name": output_name,
            "owner_ref": _runtime_identity()["owner_ref"],
            "session_key": _runtime_identity()["session_key"],
            "client_sid": _runtime_identity()["client_sid"],
            "status": "queued",
            "total": len(meta["segments"]),
            "done": 0,
            "result_path": "",
            "error": "",
            "log_tail": "",
            "created": datetime.now().isoformat(timespec="seconds"),
            "updated": datetime.now().isoformat(timespec="seconds"),
        }
        _save_tasks()
        task = asyncio.create_task(_bg_worker(task_id))
        _BG_TASKS.add(task)
        return json.dumps({
            "success": True,
            "async": True,
            "task_id": task_id,
            "status": "queued",
            "message": f"清单含 {len(meta['segments'])} 段，已转入后台下载；完成后通知，"
                       f"可用 download_task_status 查询进度。",
        }, ensure_ascii=False)

    if meta["has_key"]:
        dest = await _run_encrypted_sync(url, referer, output_name)
        return _video_success_payload(dest)

    dest = await _run_plain_sync(url, referer, output_name)
    return _video_success_payload(dest)


def _video_success_payload(dest: str) -> str:
    """同步视频产物：登记受控附件并回带 [FILE:] 标记的 JSON。"""
    frec = file_delivery.register_file(dest, name=os.path.basename(dest),
                                      mime="video/mp4")
    payload = {"success": True, "path": dest, "size_bytes": os.path.getsize(dest)}
    if frec:
        payload["file_id"] = frec["file_id"]
        payload["file_name"] = frec["name"]
        payload["marker"] = file_delivery.marker(frec)
    return json.dumps(payload, ensure_ascii=False)


# ===== [R19] 视频/音频链接 -> 本地 MP3 闭环（B站 BV 链接首选） =====
import glob as _glob

DOWNLOAD_MP3_DIR = DOWNLOAD_DIR


def _find_yt_dlp() -> str:
    for cand in (
        "/usr/local/bin/yt-dlp",
        "/home/wxh/robot/venv310/bin/yt-dlp",
        shutil.which("yt-dlp") or "",
    ):
        if cand and os.path.exists(cand):
            return cand
    return "yt-dlp"


_BILI_ID_RE = re.compile(r"(BV[0-9A-Za-z]+)|av(\d+)")


def _extract_bili_id(url: str) -> str:
    m = _BILI_ID_RE.search(url or "")
    if not m:
        return ""
    if m.group(1):
        return m.group(1)
    return f"av{m.group(2)}" if m.group(2) else ""


def _clean_filename_stem(title) -> str:
    """清洗非法文件名字符，保留中文 / 字母 / 数字 / 下划线 / 点 / 短横。"""
    if title is None:
        return ""
    stem = str(title).strip()
    stem = re.sub(r"[\x00-\x1f\x7f]", "", stem)
    stem = re.sub(r'[\\/:*?"<>|%]', "_", stem)
    stem = re.sub(r"\s+", " ", stem)
    stem = re.sub(r"_+", "_", stem)
    stem = stem.strip(" ._")
    if len(stem) > 80:
        stem = stem[:80].rstrip(" ._")
    return stem


def _bili_api_title(url: str) -> str:
    """优先走 B站 view API 取权威标题；任何异常都返回空串交给下一级兜底。"""
    bv = re.search(r"(BV[0-9A-Za-z]+)", url or "")
    aid = re.search(r"av(\d+)", url or "")
    if not bv and not aid:
        return ""
    try:
        if bv:
            api = f"https://api.bilibili.com/x/web-interface/view?bvid={bv.group(1)}"
        else:
            api = f"https://api.bilibili.com/x/web-interface/view?aid={aid.group(1)}"
        req = urllib.request.Request(api, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
        })
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        if data.get("code") == 0:
            return str((data.get("data") or {}).get("title") or "").strip()
    except Exception as e:
        logger.warning("[MP3] B站 API 取标题失败: %s", e)
    return ""


def _yt_dlp_title(url: str, yt: str) -> str:
    """yt-dlp dump-json 兜底取标题。"""
    try:
        proc = subprocess.run(
            [yt, "--skip-download", "--dump-json", "--no-playlist",
             "--no-check-certificate", "--no-cookies", url],
            capture_output=True, text=True, timeout=30,
        )
        lines = (proc.stdout or "").strip().splitlines()
        if lines:
            meta = json.loads(lines[-1])
            return str(meta.get("title") or "").strip()
    except Exception as e:
        logger.warning("[MP3] yt-dlp 取标题失败: %s", e)
    return ""


@aup_tool(
    name="download_audio_mp3",
    description="[音频提取闭环] 把视频/音频链接（B站 BV 链接、视频页 URL 等）下载并提取为 MP3 音频文件，保存到本地 downloads 目录并返回 MP3 路径。当主人要求「下载这个B站视频并转成 MP3/音频」时，首选直接调用本工具，无需再手动拼 yt-dlp/ffmpeg 命令。",
    category="media",
    is_danger=False,
    params_schema={
        "required": ["url"],
        "properties": {
            "url": {"type": "string", "description": "视频/音频页面 URL（如 B站 BV 链接）"},
            "output_name": {"type": "string", "description": "可选：输出文件名（不含扩展名），留空用视频标题"},
        },
    },
    suggestion_hint="音频提取失败：检查链接可达性、ffmpeg/yt-dlp 是否可用",
)
async def download_audio_mp3(url: str, output_name: str = None) -> str:
    os.makedirs(DOWNLOAD_MP3_DIR, exist_ok=True)
    yt = _find_yt_dlp()
    before = {os.path.basename(p): os.path.getmtime(p)
              for p in _glob.glob(os.path.join(DOWNLOAD_MP3_DIR, "*.mp3"))}
    # [R20] 标题解析优先级：显式 output_name -> B站 view API -> yt-dlp dump-json -> BV/avid 兜底
    if output_name:
        stem = _clean_filename_stem(output_name)
    else:
        stem = _clean_filename_stem(_bili_api_title(url) or _yt_dlp_title(url, yt))
    if not stem:
        stem = _extract_bili_id(url) or "audio"
    out_tmpl = os.path.join(DOWNLOAD_MP3_DIR, stem + ".%(ext)s")
    cmd = [yt, "-x", "--audio-format", "mp3", "--audio-quality", "0",
           "--no-playlist", "--no-check-certificate", "-o", out_tmpl, url]
    logger.info("[MP3] yt-dlp 命令: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=DOWNLOAD_MP3_DIR,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return json.dumps({"success": False, "error": "音频提取超时（180s）"}, ensure_ascii=False)
    out_tail = (stdout.decode("utf-8", "replace") + stderr.decode("utf-8", "replace")).strip()[-600:]
    mp3s = [p for p in _glob.glob(os.path.join(DOWNLOAD_MP3_DIR, "*.mp3"))
            if os.path.getsize(p) > 0]
    changed = [p for p in mp3s if os.path.basename(p) not in before
               or os.path.getmtime(p) > before[os.path.basename(p)]]
    # 防幻觉：yt-dlp 非零退出，或本轮没有任何新增/改写的 MP3 时，
    # 绝不回退复用下载目录里的旧 MP3 并误报成功（避免旧文件被当成新产物发卡）。
    if proc.returncode != 0 or not changed:
        return json.dumps(
            {"success": False, "error": "音频提取失败，未生成新的 MP3",
             "log_tail": out_tail[-300:]}, ensure_ascii=False)
    target = max(changed, key=os.path.getmtime)
    # 标题缺失/被解析成 None 时，回退用 BV/avid 作为文件名
    _m = re.search(r"(BV[0-9A-Za-z]+)", url or "")
    _stem = os.path.splitext(os.path.basename(target))[0]
    if not _stem or _stem.strip().lower() in ("none", "null", "nan", "未知"):
        _fallback = _m.group(1) if _m else "audio"
        dest = os.path.join(DOWNLOAD_MP3_DIR, f"{_fallback}.mp3")
        _c = 1
        while os.path.exists(dest) and os.path.abspath(dest) != os.path.abspath(target):
            dest = os.path.join(DOWNLOAD_MP3_DIR, f"{_fallback}_{_c}.mp3")
            _c += 1
        if os.path.abspath(dest) != os.path.abspath(target):
            shutil.move(target, dest)
            target = dest
    size = os.path.getsize(target)
    rec = file_delivery.register_file(target, name=os.path.basename(target), mime="audio/mpeg")
    payload = {
        "success": True,
        "path": target,
        "size_bytes": size,
        "size_kb": round(size / 1024, 1),
        "log_tail": out_tail[-300:],
    }
    if rec:
        payload["file_id"] = rec["file_id"]
        payload["file_name"] = rec["name"]
        payload["marker"] = file_delivery.marker(rec)
    return json.dumps(payload, ensure_ascii=False)


# ===== 后台下载任务状态查询工具 =====
@aup_tool(
    name="download_task_status",
    description="查询后台 m3u8 视频下载任务的进度、结果路径或失败原因。参数：task_id（下载返回的任务编号）。",
    category="network",
    is_danger=False,
    params_schema={
        "required": ["task_id"],
        "properties": {
            "task_id": {"type": "string", "description": "下载任务编号（download_m3u8 返回的 task_id）"},
        },
    },
)
async def download_task_status(task_id: str) -> str:
    _load_tasks()
    rec = _TASKS.get(task_id)
    if not rec:
        return json.dumps({"success": False, "error": "任务不存在或已被清理"}, ensure_ascii=False)
    pct = round(rec["done"] / rec["total"] * 100, 1) if rec.get("total") else 0.0
    return json.dumps({
        "success": True,
        "task_id": task_id,
        "status": rec["status"],
        "done": rec.get("done", 0),
        "total": rec.get("total", 0),
        "progress_pct": pct,
        "result_path": rec.get("result_path", ""),
        "error": rec.get("error", ""),
        "log_tail": rec.get("log_tail", "")[-300:],
    }, ensure_ascii=False)


try:
    from tool_center.registry_manager import register_tool as _register_mp3
    _register_mp3("download_audio_mp3", download_audio_mp3, category="media")
    logger.info("[download_audio_mp3] 已注册入库（SQLite）")
except Exception as _reg_mp3_err:
    logger.warning("[download_audio_mp3] 注册入库失败: %s", _reg_mp3_err)

try:
    from tool_center.registry_manager import register_tool as _register_status
    _register_status(
        "download_task_status",
        download_task_status,
        description="查询后台 m3u8 视频下载任务的进度与结果。",
        category="network",
    )
    logger.info("[download_task_status] 已注册入库（SQLite）")
except Exception as _reg_status_err:
    logger.warning("[download_task_status] 注册入库失败: %s", _reg_status_err)


# ===========================================================================
# 通用文件下载（原子工具，不做多步编排）
# 设计口径：只做「公网 http(s) 直链 → 本地 downloads/」这一件事，
#          与命令执行完全解耦，因此不设 is_danger，但阻断内网/回环地址防 SSRF。
# ===========================================================================
import ipaddress as _ipaddress
from urllib.parse import urlsplit as _urlsplit


_MAX_FILE_BYTES = 200 * 1024 * 1024
_DL_TIMEOUT = 120


def _safe_output_name(url: str, output_name: str) -> str:
    """文件名净化：去掉路径分隔与非法字符，只保留单层文件名。"""
    base = output_name or os.path.basename(_urlsplit(url).path) or "download"
    base = os.path.basename(base)
    base = re.sub(r"[^\w\u4e00-\u9fa5.\-]", "_", base).strip("._")
    return (base or "download")[:120]


_EXT_BY_TYPE = {
    "application/pdf": ".pdf",
    "text/html": ".html",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/json": ".json",
    "application/zip": ".zip",
    "application/octet-stream": ".bin",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "video/mp4": ".mp4",
}


def _ext_from_content_type(content_type: str) -> str:
    """按 Content-Type 推断扩展名；推断不出返回空串（保持无扩展名）。"""
    ct = str(content_type or "").split(";")[0].strip().lower()
    return _EXT_BY_TYPE.get(ct, "")


def _guard_public_url(url: str) -> str:
    """只允许公网 http(s) 直链，阻断回环/内网/保留地址（防 SSRF）。"""
    parsed = _urlsplit(str(url or "").strip())
    if parsed.scheme not in ("http", "https"):
        return "仅支持 http/https 直链"
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return "URL 缺少主机名"
    if host in ("localhost", "localhost.localdomain"):
        return "不允许访问本机回环地址"
    try:
        ip = _ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved):
        return "不允许访问内网/保留地址"
    return ""


def _download_sync(url: str, dest: str) -> dict:
    """同步下载到 dest，返回 {ok, size, content_type, error}。"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) HCQ/1.0",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=_DL_TIMEOUT) as resp:
        ctype = resp.headers.get("Content-Type", "")
        total = 0
        tmp = dest + ".part"
        with open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_FILE_BYTES:
                    fh.close()
                    os.remove(tmp)
                    return {"ok": False, "error": "文件超过 200MB 上限，已中止"}
                fh.write(chunk)
        os.replace(tmp, dest)
    return {"ok": True, "size": total, "content_type": ctype}


@aup_tool(
    name="download_file",
    description=(
        "[原子] 文件下载：把任意 http(s) 直链文件保存到服务器本地 downloads/ 目录，返回本地路径与文件大小。"
        "适用于 PDF、图片、压缩包、文档、音频等常规文件下载；"
        "影视站点页面（需解析 video ID 或 m3u8 清单）请改用 download_video_by_url / crawl_video_link。"
    ),
    category="network",
    is_danger=False,
    params_schema={
        "required": ["url"],
        "properties": {
            "url": {"type": "string", "description": "文件的 http(s) 直链"},
            "output_name": {"type": "string", "description": "保存文件名（可选，缺省从 URL 推断）"},
        },
    },
    suggestion_hint="下载失败：先确认链接可直接访问（非登录页、非网盘跳转页），再重试",
)
async def download_file(url: str, output_name: str = "") -> str:
    url = str(url or "").strip()
    guard = _guard_public_url(url)
    if guard:
        return json.dumps({"success": False, "error": guard}, ensure_ascii=False)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    name = _safe_output_name(url, output_name)
    dest = os.path.join(DOWNLOAD_DIR, name)
    if os.path.exists(dest):
        stem, ext = os.path.splitext(name)
        dest = os.path.join(DOWNLOAD_DIR, "%s_%s%s" % (stem, datetime.now().strftime("%H%M%S"), ext))
    try:
        result = await asyncio.to_thread(_download_sync, url, dest)
    except Exception as e:
        logger.warning("[DOWNLOAD_FILE] 下载失败 url=%s err=%s", url, e)
        return json.dumps({"success": False, "error": "下载失败：%s" % (str(e)[:180] or type(e).__name__)},
                          ensure_ascii=False)
    if not result.get("ok"):
        return json.dumps({"success": False, "error": result.get("error", "下载失败")}, ensure_ascii=False)
    # 无扩展名时按 Content-Type 补全，避免落成打不开的 "download" 文件
    if not os.path.splitext(dest)[1]:
        _ext = _ext_from_content_type(result.get("content_type", ""))
        if _ext:
            try:
                os.replace(dest, dest + _ext)
                dest = dest + _ext
            except OSError:
                pass
    logger.info("[DOWNLOAD_FILE] 完成 url=%s -> %s (%d bytes)", url, dest, result.get("size", 0))
    return json.dumps({
        "success": True,
        "path": dest,
        "size": result.get("size", 0),
        "content_type": result.get("content_type", ""),
        "message": "文件已保存到本地：%s（%.1f KB）" % (dest, result.get("size", 0) / 1024.0),
    }, ensure_ascii=False)


try:
    from tool_center.registry_manager import register_tool as _register_dlfile
    _register_dlfile("download_file", download_file, category="network")
    logger.info("[download_file] 已注册入库（SQLite）")
except Exception as _reg_dlfile_err:
    logger.warning("[download_file] 注册入库失败: %s", _reg_dlfile_err)
