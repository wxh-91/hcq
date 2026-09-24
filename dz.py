import logging
logger = logging.getLogger(__name__)
import aiohttp
import asyncio
from dotenv import load_dotenv
load_dotenv()

import nonebot
from nonebot.adapters.onebot.v11 import Adapter, Bot, MessageEvent, Message, MessageSegment
from nonebot import on_message, on_regex

nonebot.init()
driver = nonebot.get_driver()
driver.register_adapter(Adapter)

from nonebot import require
require("nonebot_plugin_apscheduler")

# nonebot.load_plugin("nonebot_plugin_multi_source_daily")
nonebot.load_plugin("nonebot_plugin_resolver2")
nonebot.load_plugin("nonebot_plugin_souti")
#nonebot.load_plugin("nonebot_plugin_exhibitionism")

from memory import Memory
from admin import handle_admin_command, FAST_COMMANDS
from ai import ask_ai, refresh_image_context, set_ocr_context, get_conv_key
from ocr import OCRResult, recognize_image_url
from bot_utils.checkin import has_checked_in_today, execute_checkin, save_state, get_today
from cl import DEFAULT_ADMIN
import pi_integration   # 用于模式切换和关闭

memory = Memory()
_user_message_locks: dict[str, asyncio.Lock] = {}


# ==================== 启动/关闭钩子 ====================
@driver.on_startup
async def on_startup():
    """启动时不初始化Pi后端，由切换指令按需触发"""
    logger.info("[dz] 启动完成，当前后端模式: %s", pi_integration.get_mode())
    # 清理过期的情景记忆（只保留当天）
    try:
        from bot_utils.episodic import clear_expired
        clear_expired()
    except Exception as e:
        logger.error(f"[dz] 清理情景记忆失败: {e}", exc_info=True)

@driver.on_bot_connect
async def on_bot_connect(bot: Bot):
    """Bot 连接成功后检查签到（每日仅一次）"""
    if has_checked_in_today():
        logger.info("[签到] 今日已签到，跳过")
        return

    logger.info("[签到] 今日首次启动，执行签到...")
    result = await execute_checkin()

    try:
        admin_id = int(DEFAULT_ADMIN)
    except (ValueError, TypeError):
        logger.error("[签到] 无法解析管理员ID，跳过通知")
        return

    if result.get("success"):
        save_state({
            "last_checkin_date": get_today(),
            "last_checkin_time": result.get("time", ""),
            "award": result.get("award", 0),
            "total": result.get("total", 0),
            "quota": result.get("quota", 0),
            "success": True,
        })
        msg = (
            f"{get_today()} {result.get('time', '')}\n"
            f"签到成功，获得额度：{result.get('award', 0)}\n"
            f"累计签到：{result.get('total', 0)} 天\n"
            f"累计总额度：{result.get('quota', 0)}"
        )
        logger.info(f"[签到] 成功，获得 {result.get('award', 0)} 额度")
    else:
        msg = f"签到失败：{result.get('error', '未知错误')}"
        logger.error(f"[签到] 失败：{result.get('error')}")

    try:
        await bot.send_private_msg(user_id=admin_id, message=msg)
    except Exception as e:
        logger.error(f"[签到] 通知发送失败: {e}")

@driver.on_shutdown
async def on_shutdown():
    """关闭时释放Pi后端资源（若已初始化）"""
    try:
        await pi_integration.close_pi_backend()
        logger.info("[dz] Pi后端资源已释放")
    except Exception as e:
        logger.error("[dz] 关闭Pi后端时出错: %s", e, exc_info=True)


# ==================== 网页截图（无改动） ====================
url_matcher = on_regex(r"^https?://", priority=1, block=True)

@url_matcher.handle()
async def handle_url(bot: Bot, event: MessageEvent):
    url = event.get_plaintext().strip()
    if "b23.tv" in url or "bilibili.com" in url:
        await url_matcher.skip()
        return
    try:
        from nonebot_plugin_htmlrender import get_new_page
        async with get_new_page() as page:
            await page.goto(url, timeout=30000)
            img = await page.screenshot(full_page=True)
            await bot.send(event, MessageSegment.image(img))
    except Exception as exc:
        await bot.send(event, Message(f"截图失败: {exc}"))

# ==================== 分段回复====================
async def send_segmented_reply(bot, event, reply: str):
    if not reply:
        reply = "（无内容）"
    MAX_LEN = 2000
    if len(reply) <= MAX_LEN:
        await bot.send(event, Message(reply))
        return

    import re
    code_pattern = re.compile(r'(```[\s\S]*?```)', re.MULTILINE)
    last_end = 0
    segments = []
    for match in code_pattern.finditer(reply):
        start, end = match.span()
        if start > last_end:
            segments.append(('text', reply[last_end:start]))
        segments.append(('code', match.group()))
        last_end = end
    if last_end < len(reply):
        segments.append(('text', reply[last_end:]))

    final_parts = []
    current = ""
    for seg_type, seg_text in segments:
        if seg_type == 'code':
            if len(current) + len(seg_text) <= MAX_LEN:
                current += seg_text
            else:
                if current:
                    final_parts.append(current.strip())
                current = seg_text
        else:
            lines = seg_text.splitlines(keepends=True)
            for line in lines:
                if len(current) + len(line) <= MAX_LEN:
                    current += line
                else:
                    if current:
                        final_parts.append(current.strip())
                    current = line
    if current:
        final_parts.append(current.strip())

    for i, part in enumerate(final_parts[:5]):
        if part:
            await bot.send(event, Message(part.strip()))
            await asyncio.sleep(0.3)
    if len(final_parts) > 5:
        remaining = "".join(final_parts[5:])
        if remaining.strip():
            await bot.send(event, Message(remaining.strip()))

# ==================== OCR辅助 ====================
def _ocr_failure_message(results: list[OCRResult]) -> str:
    failures = [result.error for result in results if result.error]
    if not failures:
        return "OCR识别失败：未能提取图片文字，请重新发送清晰图片或直接补充文字。"
    unique_failures = list(dict.fromkeys(failures))
    reason = "；".join(unique_failures[:3])
    if len(unique_failures) > 3:
        reason += "；其他图片处理失败"
    return f"OCR识别失败：{reason}。请重新发送清晰图片或直接补充文字。"

def _get_user_message_lock(user_id: str) -> asyncio.Lock:
    return _user_message_locks.setdefault(user_id, asyncio.Lock())

async def handle_image_segments(bot: Bot, event: MessageEvent, key: str) -> tuple[bool, bool]:
    image_urls = [
        segment.data.get("url")
        for segment in event.message
        if segment.type == "image"
    ]
    if not image_urls:
        return False, False

    refresh_image_context(key)
    raw_results = await asyncio.gather(
        *(recognize_image_url(image_url) for image_url in image_urls),
        return_exceptions=True,
    )
    results = []
    for result in raw_results:
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            results.append(OCRResult(error="图片处理异常，请重试"))
        else:
            results.append(result)

    recognized_text = [result.text for result in results if result.succeeded]
    if recognized_text:
        logger.debug("OCR 识别到文字: %s...", recognized_text[0][:50])
        set_ocr_context(key, "\n\n".join(recognized_text))

    failures = [result for result in results if not result.succeeded]
    if failures:
        await bot.send(event, Message(_ocr_failure_message(failures)))

    return True, bool(recognized_text)

# ==================== 核心消息处理器 ====================
msg_handler = on_message(priority=99, block=False)

@msg_handler.handle()
async def handle_msg(bot: Bot, event: MessageEvent):
    if event.user_id == bot.self_id:
        return
    async with _get_user_message_lock(str(event.user_id)):
        await _handle_msg_locked(bot, event)

async def _handle_msg_locked(bot: Bot, event: MessageEvent):
    plain_text = event.get_plaintext().strip()

    chat_type = "private" if event.message_type == "private" else "group"
    group_id = str(event.group_id) if chat_type == "group" else ""
    key = get_conv_key(str(event.user_id), chat_type, group_id)

    # ---------- 群聊特殊处理 ----------
    if chat_type == "group":
        if not memory.get("group_chat_enabled", False):
            return
        if not memory.is_admin(str(event.user_id)):
            await bot.send(event, Message("群聊功能未开启或无权限"))
            return

    # ---------- 文件处理（无改动） ----------
    for seg in event.message:
        if seg.type == "file":
            file_info = seg.data
            downloadable = {
                "file_name": file_info.get("file", "unknown"),
                "file_size": int(file_info.get("file_size", 0)),
                "file_id": file_info.get("file_id"),
                "file_url": None,
            }
            result = await handle_downloadable(downloadable, plain_text, event, bot, chat_type, group_id)
            if result:
                return
            return

    # ---------- 图片处理（无改动） ----------
    has_images, has_ocr_text = await handle_image_segments(bot, event, key)
    if has_images and not has_ocr_text and not plain_text:
        return
    if has_images and not has_ocr_text and plain_text:
        refresh_image_context(key)
        logger.debug("OCR失败，但用户有文字输入，继续处理文字: %s...", plain_text[:30])

    # ---------- 提取正文 ----------
    user_input = plain_text
    if user_input:
        memory.set("last_talk", user_input.lower())

    # ---------- 群聊前缀处理 ----------
    if chat_type == "group":
        if not (user_input.startswith("！") or user_input.startswith("!")):
            return
        if user_input.startswith("！"):
            user_input = user_input[1:].strip()
        else:
            user_input = user_input[1:].strip()
        if not user_input:
            return

    # ---------- 切换指令检测（私聊和群聊通用） ----------
    # 私聊：以@开头，群聊：已经去掉前缀，直接比较
    # 定义切换指令为 "切换到API" 或 "切换到pi"（不区分大小写）
    switch_cmd = user_input.lower()
    if switch_cmd in ("切换到api", "切换到pi"):
        target_mode = "api" if "api" in switch_cmd else "pi"
        try:
            await pi_integration.set_mode(target_mode)
            reply = f"后端模式已切换为: {target_mode.upper()}"
        except Exception as e:
            reply = f"切换失败: {str(e)}"
        await send_segmented_reply(bot, event, reply)
        return

    # ---------- 私聊逻辑 ----------
    if chat_type == "private":
        # === 优先级最高：模式切换指令（@切换到api / @切换到pi） ===
        # 注意：必须保留 @ 前缀，因为用户习惯用 @ 触发
        if user_input.startswith("@") and user_input[1:].strip().startswith("切换到"):
            # 提取目标模式
            raw = user_input[1:].strip()
            if "api" in raw.lower():
                target_mode = "api"
            elif "pi" in raw.lower():
                target_mode = "pi"
            else:
                # 不支持的切换目标，直接走 AI
                pass
            try:
                await pi_integration.set_mode(target_mode)
                reply = f"后端模式已切换为: {target_mode.upper()}"
            except Exception as e:
                reply = f"切换失败: {str(e)}"
            await send_segmented_reply(bot, event, reply)
            return

        # ---------- 爬取指令（新站点，异步） ----------
        if user_input.startswith("爬取"):
            url = user_input[2:].strip()
            if not url:
                await send_segmented_reply(bot, event, "请提供视频 URL，格式：爬取 <url>")
                return

            await send_segmented_reply(bot, event, f"收到，正在下载：{url}")

            async def _background_crawl():
                from bot_utils.video_crawler import crawl_video
                from cl import DEFAULT_ADMIN
                try:
                    result = await crawl_video(url)
                    admin_id = int(DEFAULT_ADMIN)
                    if result["success"]:
                        msg = f"爬取完成\n保存路径：{result['mp4_path']}"
                    else:
                        msg = f"爬取失败：{result['error']}"
                    await bot.send_private_msg(user_id=admin_id, message=msg)
                except Exception as e:
                    logger.error(f"[爬取] 后台任务异常: {e}", exc_info=True)

            asyncio.create_task(_background_crawl())
            return

        # ---------- 多任务（全角星号分隔） ----------
        if "＊" in user_input:
            from bot_utils.task_queue import TaskQueue
            await send_segmented_reply(bot, event, "收到，任务已加入队列")
            asyncio.create_task(
                TaskQueue().submit(user_input, str(event.user_id), chat_type, group_id)
            )
            return

        # 其他 @ 开头的硬指令（FAST_COMMANDS）
        if user_input.startswith("@"):
            cmd = user_input[1:].strip()
            if cmd in FAST_COMMANDS:
                func = FAST_COMMANDS[cmd]
                if asyncio.iscoroutinefunction(func):
                    result = await func()
                else:
                    result = func()
                await send_segmented_reply(bot, event, result if isinstance(result, str) else str(result))
                return
            else:
                # 未知的 @ 指令，去掉 @ 后当作普通消息
                user_input = cmd

        # 普通消息走 AI（ask_ai 内部根据当前模式选择后端）
        reply = await ask_ai(str(event.user_id), user_input, chat_type, group_id)
        await send_segmented_reply(bot, event, reply)
        return
    # ---------- 群聊逻辑 ----------
    if chat_type == "group":
        # 管理员命令（工具调用）
        admin_result = await handle_admin_command(user_input, str(event.user_id), force_tool_route=True)
        if admin_result is not None:
            await send_segmented_reply(bot, event, admin_result)
            return

        # 群聊对话（若开启）
        if memory.get("group_chat_enabled", False):
            reply = await ask_ai(str(event.user_id), user_input, chat_type, group_id)
            await send_segmented_reply(bot, event, reply)
            return
        return

    # ---------- 默认（理论上不会到这里） ----------
    reply = await ask_ai(str(event.user_id), user_input, chat_type, group_id)
    await send_segmented_reply(bot, event, reply)

# ==================== 文件下载处理（无改动） ====================
async def handle_downloadable(downloadable: dict, user_input: str, event, bot, chat_type: str, group_id: str):
    file_name = downloadable.get("file_name", "未知文件")
    file_id = downloadable.get("file_id")

    if not file_id:
        await bot.send(event, Message("无法获取文件ID"))
        return True

    try:
        if chat_type == "group":
            file_info = await bot.call_api("get_group_file_url", group_id=group_id, file_id=file_id)
        else:
            file_info = await bot.call_api("get_private_file_url", file_id=file_id)
        logger.debug("get_private_file_url 返回: %s", file_info)
        file_url = file_info.get("url")
        if not file_url:
            await bot.send(event, Message("未获取到下载链接"))
            return True

        async with aiohttp.ClientSession() as session:
            async with session.get(file_url) as resp:
                if resp.status != 200:
                    await bot.send(event, Message(f"下载失败: HTTP {resp.status}"))
                    return True
                content_bytes = await resp.read()

        try:
            content = content_bytes.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            await bot.send(event, Message("文件不是纯文本格式（UTF-8 解码失败）"))
            return True

        if len(content) > 10000:
            content = content[:10000] + "\n\n... (内容过长，已截断)"

        if user_input:
            prompt = f"用户上传了文件：{file_name}\n文件内容：\n```\n{content}\n```\n用户附加指令：{user_input}\n请根据文件内容回应用户。"
        else:
            prompt = f"用户上传了文件：{file_name}\n文件内容：\n```\n{content}\n```\n请先总结这个文件的核心功能，然后主动询问用户希望如何处理这个文件（比如修改、分析、执行等）。"
        reply = await ask_ai(str(event.user_id), prompt, chat_type, group_id)
        await send_segmented_reply(bot, event, reply)
        return True

    except aiohttp.ClientError as e:
        logger.error("下载文件失败: %s", e, exc_info=True)
        await bot.send(event, Message(f"下载文件失败: {str(e)}"))
        return True
    except UnicodeDecodeError as e:
        logger.error("文件解码失败: %s", e, exc_info=True)
        await bot.send(event, Message("文件不是纯文本格式（UTF-8 解码失败）"))
        return True
    except asyncio.TimeoutError as e:
        logger.error("下载超时: %s", e, exc_info=True)
        await bot.send(event, Message("下载超时，请稍后重试"))
        return True
    except Exception as e:
        logger.error("处理文件异常: %s", e, exc_info=True)
        await bot.send(event, Message(f"处理文件失败: {str(e)}"))
        return True

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    nonebot.run()
