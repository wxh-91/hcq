import asyncio
import os
import tempfile
import re
import shutil
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

async def download_m3u8(url: str, referer: str = None, output_name: str = "video") -> str:
    """
    根据 m3u8 链接下载视频，自动检测是否加密：
    - 有 #EXT-X-KEY → 走解密分批下载 (bash脚本)
    - 无 KEY → 先用 yt-dlp，失败则降级到 FFmpeg
    """
    required_bins = ["curl", "openssl", "ffmpeg"]
    if not hasattr(download_m3u8, "_checked"):
        missing = [b for b in required_bins if not shutil.which(b)]
        if missing:
            return f"缺少系统依赖：{', '.join(missing)}，请安装后重试"
        download_m3u8._checked = True

    logger.info(f"[DOWNLOADER] 开始下载: url={url}, referer={referer}, output_name={output_name}")

    if not referer:
        parsed = urlparse(url)
        referer = f"{parsed.scheme}://{parsed.netloc}/"
        logger.info(f"[DOWNLOADER] 自动补全 referer: {referer}")

    output_name = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fa5]', '_', output_name)
    work_dir = tempfile.mkdtemp(prefix="m3u8_dl_")
    logger.info(f"[DOWNLOADER] 临时工作目录: {work_dir}")

    # 检测是否有加密
    check_script = f'''#!/bin/bash
curl -s -f -H "Referer: {referer}" -H "User-Agent: Mozilla/5.0" "{url}" | grep '#EXT-X-KEY' | head -1
'''
    check_path = os.path.join(work_dir, "check.sh")
    with open(check_path, "w") as f:
        f.write(check_script)
    os.chmod(check_path, 0o755)
    logger.info("[DOWNLOADER] 开始检测是否加密...")

    proc = await asyncio.create_subprocess_shell(
        f"bash {check_path}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=work_dir
    )
    stdout, _ = await proc.communicate()
    has_key = stdout.decode().strip() != ""
    logger.info(f"[DOWNLOADER] 加密检测结果: has_key={has_key}")

    # ===== 无加密：先试 yt-dlp，失败后降级 FFmpeg =====
    if not has_key:
        dest = os.path.join(os.path.expanduser("~"), f"{output_name}.mp4")
        counter = 1
        while os.path.exists(dest):
            name, ext = os.path.splitext(f"{output_name}.mp4")
            dest = os.path.join(os.path.expanduser("~"), f"{name}_{counter}{ext}")
            counter += 1
        logger.info(f"[DOWNLOADER] 目标文件: {dest}")

        # ---------- 尝试 yt-dlp ----------
        yt_cmd = (
            f'yt-dlp -o "{dest}" --referer "{referer}" '
            f'--user-agent "Mozilla/5.0" --no-check-certificate --no-playlist --no-cookies "{url}"'
        )
        logger.info(f"[DOWNLOADER] yt-dlp 命令: {yt_cmd}")
        proc = await asyncio.create_subprocess_shell(
            yt_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("[DOWNLOADER] yt-dlp 超时，降级到 FFmpeg")
            # 降级到 FFmpeg
        else:
            output = stdout.decode().strip() or stderr.decode().strip()
            logger.info(f"[DOWNLOADER] yt-dlp 输出长度: {len(output)}")
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                shutil.rmtree(work_dir, ignore_errors=True)
                return f"视频下载完成 (yt-dlp)！\n保存路径：{dest}\n日志：\n{output[-300:]}"
            else:
                logger.warning("[DOWNLOADER] yt-dlp 未生成有效文件，降级到 FFmpeg")

        # ---------- 降级到 FFmpeg ----------
        logger.info("[DOWNLOADER] 使用 FFmpeg 下载")
        ffmpeg_cmd = (
            f'ffmpeg -y -multiple_requests 1 '
            f'-headers "Referer: {referer}\\r\\nUser-Agent: Mozilla/5.0\\r\\n" '
            f'-i "{url}" -c copy "{dest}"'
        )
        logger.info(f"[DOWNLOADER] FFmpeg 命令: {ffmpeg_cmd}")

        proc = await asyncio.create_subprocess_shell(
            ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            shutil.rmtree(work_dir, ignore_errors=True)
            return "下载超时（超过 5 分钟），请检查链接或网络"

        output = stdout.decode().strip() or stderr.decode().strip()
        logger.info(f"[DOWNLOADER] FFmpeg 输出长度: {len(output)}")

        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            shutil.rmtree(work_dir, ignore_errors=True)
            return f"视频下载完成 (FFmpeg)！\n保存路径：{dest}\n执行日志：\n{output[-300:]}"
        else:
            shutil.rmtree(work_dir, ignore_errors=True)
            return f"所有下载方式均失败，请检查链接是否有效。\n输出：\n{output[-500:]}"

    # ===== 有加密：走原有 bash 脚本（解密 + 合并） =====
    logger.info("[DOWNLOADER] 进入加密流下载模式")
    SCRIPT_TEMPLATE = '''#!/bin/bash
set -e
WORK_DIR="{work_dir}"
cd "$WORK_DIR"
rm -rf anime_temp && mkdir -p anime_temp && cd anime_temp
M3U8_URL="{url}"
REFERER="{referer}"
OUTPUT_NAME="{output_name}"
BATCH_SIZE=50
echo "📥 下载 playlist..."
curl -s -f -H "Referer: $REFERER" -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)" "$M3U8_URL" -o playlist.m3u8
KEY_LINE=$(grep '#EXT-X-KEY' playlist.m3u8 | head -1)
if [ -z "$KEY_LINE" ]; then echo "❌ 未找到 #EXT-X-KEY"; exit 1; fi
KEY_URI=$(echo "$KEY_LINE" | grep -o 'URI="[^"]*"' | sed 's/URI="//;s/"//')
if [[ ! "$KEY_URI" =~ ^http ]]; then KEY_URI="$(dirname "$M3U8_URL")/$KEY_URI"; fi
IV=$(echo "$KEY_LINE" | grep -o 'IV=0x[0-9a-fA-F]*' | sed 's/IV=//')
if [ -z "$IV" ]; then IV="0x00000000000000000000000000000000"; fi
curl -s -f -H "Referer: $REFERER" "$KEY_URI" -o key.bin
if [ ! -s key.bin ]; then echo "❌ 密钥下载失败"; exit 1; fi
KEY_HEX=$(xxd -p -c 256 key.bin | tr -d '\\n')
BASE_URL=$(dirname "$M3U8_URL")/
grep -v '^#' playlist.m3u8 | grep -v '^$' | while read line; do
    if [[ ! "$line" =~ ^http ]]; then echo "${{BASE_URL}}${{line#./}}"; else echo "$line"; fi
done > ts_list.txt
total=$(wc -l < ts_list.txt)
batch=1; start=1; part_files=""
while [ $start -le $total ]; do
    end=$((start + BATCH_SIZE - 1)); [ $end -gt $total ] && end=$total
    echo "📦 处理第 $batch 批 ($start ~ $end)"
    sed -n "${{start}},${{end}}p" ts_list.txt > batch_list.txt
    > decrypted.m3u8
    echo "#EXTM3U" >> decrypted.m3u8
    echo "#EXT-X-VERSION:3" >> decrypted.m3u8
    echo "#EXT-X-TARGETDURATION:10" >> decrypted.m3u8
    while read ts_url; do
        filename=$(basename "$ts_url")
        enc_file="${{filename%.ts}}_enc.ts"
        dec_file="${{filename%.ts}}_dec.ts"
        curl -s -f -H "Referer: $REFERER" -H "User-Agent: Mozilla/5.0" "$ts_url" -o "$enc_file" || continue
        IV_HEX=${{IV#0x}}
        openssl aes-128-cbc -d -in "$enc_file" -out "$dec_file" -K "$KEY_HEX" -iv "$IV_HEX" 2>/dev/null
        if [ -s "$dec_file" ]; then
            echo "#EXTINF:10.0," >> decrypted.m3u8
            echo "$dec_file" >> decrypted.m3u8
        else
            echo "⚠️ 解密失败: $filename"
        fi
        rm -f "$enc_file"
    done < batch_list.txt
    echo "#EXT-X-ENDLIST" >> decrypted.m3u8
    part_name=$(printf "part_%03d.mp4" $batch)
    if ffmpeg -y -allowed_extensions ALL -i decrypted.m3u8 -c copy "$part_name" 2>/dev/null; then
        if [ -f "$part_name" ] && [ -s "$part_name" ]; then part_files="$part_files $part_name"; fi
    fi
    rm -f batch_list.txt decrypted.m3u8 *_dec.ts
    start=$((end + 1)); batch=$((batch + 1))
done
if [ -n "$part_files" ]; then
    > concat_list.txt
    for f in $part_files; do echo "file '$PWD/$f'" >> concat_list.txt; done
    FINAL_FILE="$WORK_DIR/$OUTPUT_NAME.mp4"
    if ffmpeg -y -f concat -safe 0 -i concat_list.txt -c copy "$FINAL_FILE" 2>/dev/null; then
        echo "SUCCESS:$FINAL_FILE"
    else
        ffmpeg -y -f concat -safe 0 -i concat_list.txt -c:v libx264 -c:a aac -preset fast "$FINAL_FILE"
        echo "SUCCESS:$FINAL_FILE"
    fi
    rm -f $part_files concat_list.txt
else
    echo "❌ 没有成功合并任何批次"
    exit 1
fi
'''

    script_content = SCRIPT_TEMPLATE.format(
        work_dir=work_dir,
        url=url,
        referer=referer,
        output_name=output_name
    )

    script_path = os.path.join(work_dir, "download.sh")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script_content)
    os.chmod(script_path, 0o755)
    logger.info("[DOWNLOADER] 加密下载脚本已生成")

    proc = await asyncio.create_subprocess_shell(
        f"bash {script_path}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=work_dir
    )
    logger.info("[DOWNLOADER] 加密下载进程已启动，等待完成...")
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        shutil.rmtree(work_dir, ignore_errors=True)
        return "下载超时（超过 10 分钟），请检查链接或网络"

    output = stdout.decode().strip() or stderr.decode().strip()
    final_file = None
    for line in output.splitlines():
        if line.startswith("SUCCESS:"):
            final_file = line.split(":", 1)[1]
            break

    if final_file and os.path.exists(final_file):
        home = os.path.expanduser("~")
        dest = os.path.join(home, f"{output_name}.mp4")
        counter = 1
        while os.path.exists(dest):
            name, ext = os.path.splitext(f"{output_name}.mp4")
            dest = os.path.join(home, f"{name}_{counter}{ext}")
            counter += 1
        os.rename(final_file, dest)
        shutil.rmtree(work_dir, ignore_errors=True)
        return f"视频下载完成 (加密流)！\n保存路径：{dest}\n执行日志（尾部）：\n{output[-300:]}"
    else:
        shutil.rmtree(work_dir, ignore_errors=True)
        return f"加密流下载失败，请检查链接是否有效。\n完整输出：\n{output[-500:]}"
