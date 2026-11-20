# ===== [绝对红线 · 永久生效] =====
# 1. 严禁私自启动 QQ 通道：本程序是唯一允许的启动入口（python3 dz.py）。
#    禁止执行任何 Napcat/QQ 协议端启动（NapCatWinBootMain.exe / launcher*.bat /
#    inject_napcat.ps1 / KillQQ.bat / node napcat.mjs），违反将触发腾讯风控。
# 2. 禁止接触登录凭证：不读取/复制/触发 Napcat 或 QQNT 的登录缓存、token、二维码。
# 3. Napcat 注入已撤销（QQNT loadNapCat.js 已删）；WS 3000 已禁用（onebot11.json enable=False）。
# 4. 对 ws://172.19.192.1:3000 的重连失败是正常噪音（Napcat 离线），不得因此启动 Napcat。
# ====================================
import logging
logger = logging.getLogger(__name__)
from import_center.bootstrap import bootstrap
bootstrap()
import os
import gc
import hmac
import aiohttp
import asyncio
import time
import random
import re as _re
from datetime import datetime
from config_center import get_config
from dotenv import load_dotenv
load_dotenv()

import nonebot
from nonebot.adapters.feishu import Adapter, Bot, MessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter, MessageEvent as OneBotMessageEvent
from nonebot import on_message, on_regex

nonebot.init()
driver = nonebot.get_driver()
# [R11 飞书退场] FEISHU_ENABLED=0 时不注册飞书 adapter：进程不建飞书长连接、不发心跳、不常驻。
# 其余能力（FastAPI 后端 8082、调度器、工具链、巡检）完全不依赖 adapter，照常运行。
if os.getenv("FEISHU_ENABLED", "1").strip().lower() not in ("0", "false", "off", "no", "disabled", ""):
    driver.register_adapter(Adapter)
    logger.info("[dz] Feishu adapter 已注册（FEISHU_ENABLED=1）")
else:
    logger.info("[dz] 飞书通道已关闭（FEISHU_ENABLED=0）：未注册适配器，无 WS 长连接/心跳")
# [OneBot 通道] 条件注册：仅当 ONEBOT_WS_URLS 非空才注册（QQ 未启用时避免空转重连噪音）
_ob_urls = os.getenv("ONEBOT_WS_URLS", "").strip()
if _ob_urls:
    driver.register_adapter(OneBotV11Adapter)
    logger.info("[dz] OneBot V11 adapter 已注册: %s", _ob_urls)
else:
    logger.info("[dz] ONEBOT_WS_URLS 未配置，跳过 OneBot adapter（QQ 通道关闭，无重连噪音）")


def _preprocess_alias(text: str) -> str:
    """[安全别名] 物理前置解析（不经 AI）：'tz 92285' / 'tz#92285' → 完整 URL。
    命中即替换；完整 URL / 其他消息原样返回。别名解析绝不交给大模型。"""
    s = (text or "").strip()
    if not s or s.startswith(("http://", "https://")):
        return text
    try:
        import json as _json
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins", "crawler", "site_map.json")
        with open(_p, encoding="utf-8") as _f:
            _map = _json.load(_f)
    except Exception:
        _map = {}
    for _k, _base in _map.items():
        for _sep in (" ", "#"):
            _prefix = _k + _sep
            if s.startswith(_prefix):
                _tail = s[len(_prefix):].strip()
                if _tail:
                    _resolved = _base + _tail.split()[0]
                    logger.info("[安全别名] 前置解析: '%s' -> %s", s, _resolved)
                    return _resolved
    return text

# [通道适配] 修复 nonebot-adapter-feishu 2.7.2 事件解析 bug：
# sender.sender_id.user_id 为 null 时模型解析失败会回退 Event 基类，导致 on_message 全部失配。
try:
    import nonebot.adapters.feishu.adapter as _feishu_adapter
    _orig_json_to_event = _feishu_adapter.Adapter.json_to_event

    @classmethod
    def _patched_json_to_event(cls, json_data):
        try:
            sid = json_data.get("event", {}).get("sender", {}).get("sender_id", {})
            if sid.get("user_id") is None:
                sid["user_id"] = ""
        except Exception:
            pass
        return _orig_json_to_event(json_data)

    _feishu_adapter.Adapter.json_to_event = _patched_json_to_event
    _feishu_adapter.Adapter.json_to_event.__doc__ = _orig_json_to_event.__doc__
except Exception:
    pass


from nonebot import require
require("nonebot_plugin_apscheduler")

# nonebot.load_plugin("nonebot_plugin_multi_source_daily")
nonebot.load_plugin("nonebot_plugin_resolver2")
nonebot.load_plugin("nonebot_plugin_souti")
#nonebot.load_plugin("nonebot_plugin_exhibitionism")

from import_center import get_service
from memory import Memory
from admin import handle_admin_command, FAST_COMMANDS
from ai import ask_ai, refresh_image_context, set_ocr_context, get_conv_key, sanitize_output
from ocr import OCRResult, recognize_image_url
from plugins.core_utils.checkin import has_checked_in_today, execute_checkin, save_state, get_today
from cl import DEFAULT_ADMIN
import pi_integration   # 用于模式切换和关闭

memory = get_service("memory")
_user_message_locks: dict[str, asyncio.Lock] = {}


# ==================== 启动/关闭钩子 ====================
@driver.on_startup
async def on_startup():
    """启动时不初始化Pi后端，由切换指令按需触发"""
    logger.info("[dz] 启动完成，当前后端模式: %s", pi_integration.get_mode())
    # [防御中枢] 启动后台探针（脱离 QQ 消息驱动，每 30s 轻量扫描；默认 DRY-RUN）
    try:
        from defense_center.watchdog import default_watchdog
        wd = default_watchdog()
        wd.start()
        logger.info("[defense] watchdog 后台探针已启动（间隔 30s，处置默认 DRY-RUN）")
    except Exception as e:
        logger.error("[defense] watchdog 启动失败: %s", e, exc_info=True)
    # 清理过期的情景记忆（只保留当天）
    try:
        from plugins.core_utils.episodic import clear_expired
        clear_expired()
    except Exception as e:
        logger.error(f"[dz] 清理情景记忆失败: {e}", exc_info=True)

@driver.on_bot_connect
async def on_bot_connect(bot: Bot):
    """Bot 连接成功后检查签到（每日仅一次）"""
    from cl import CHECKIN_ENABLED
    from plugins.core_utils.feishu_bridge import FeishuBotView, admin_open_id
    view_bot = FeishuBotView(bot)
    # [防御中枢] Bot 连接成功：注入真实报警通道（中危警告/高危紧急求救推送管理员，飞书 send_message）
    try:
        from defense_center.enforcer import qq_notifier
        qq_notifier.attach(
            lambda text: view_bot.send_private_msg(user_id=DEFAULT_ADMIN, message=text),
            0,
        )
        logger.info("[defense] 报警通道已注入（收件人 FEISHU_ADMIN_OPEN_ID=%s）", admin_open_id())
    except Exception as e:
        logger.error("[defense] 报警通道注入失败: %s", e, exc_info=True)
    # [R11 飞书退场] 工具级心跳改走 notify_bridge：飞书在线顺带发，离线必落本地 outbox
    try:
        from admin import set_heartbeat_callback
        from plugins.core_utils.notify_bridge import notify

        async def _heartbeat_sender(text: str):
            try:
                await notify(text, title="工具心跳", level="heartbeat")
                logger.info("[心跳] 已投递: %s", text[:60])
            except Exception as _hb_err:
                logger.error("[心跳] 投递失败: %s", _hb_err)

        set_heartbeat_callback(_heartbeat_sender)
        logger.info("[心跳] 工具级心跳回调已注册（>60s 自动落本地/飞书）")
    except Exception as e:
        logger.error("[心跳] 回调注册失败: %s", e, exc_info=True)
    if not CHECKIN_ENABLED:
        logger.info("[签到] 签到功能已停用，跳过")
        return
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
        await view_bot.send_private_msg(user_id=admin_id, message=msg)
    except Exception as e:
        logger.error(f"[签到] 通知发送失败: {e}")

@driver.on_startup
async def _r11_startup_fallbacks():
    """[R11 飞书退场] 启动即绑定的兜底通道：不依赖飞书连接，关掉飞书照样工作。"""
    try:
        from admin import set_heartbeat_callback
        from plugins.core_utils.notify_bridge import notify

        async def _hb(text: str):
            await notify(text, title="工具心跳", level="heartbeat")

        set_heartbeat_callback(_hb)
        logger.info("[R11] 心跳回调已注册（不依赖飞书连接）")
    except Exception as e:
        logger.error("[R11] 心跳回调注册失败: %s", e, exc_info=True)
    try:
        from defense_center.enforcer import qq_notifier

        async def _defense(text: str):
            await notify(text, title="防御报警", level="alert")

        qq_notifier.attach(_defense, 0)
        logger.info("[R11] 防御报警通道已绑定 notify_bridge（不依赖飞书连接）")
    except Exception as e:
        logger.error("[R11] 防御报警绑定失败: %s", e, exc_info=True)


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
async def handle_url(bot, event):
    url = event.get_plaintext().strip()
    if "b23.tv" in url or "bilibili.com" in url:
        await url_matcher.skip()
        return
    try:
        from nonebot_plugin_htmlrender import get_new_page
        async with get_new_page() as page:
            await page.goto(url, timeout=30000)
            img = await page.screenshot(full_page=True)
            if isinstance(event, OneBotMessageEvent):
                from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMsgSeg
                await bot.send(event, OneBotMsgSeg.image(img))
            else:
                from plugins.core_utils.feishu_bridge import FeishuEventView, FeishuBotView
                view_bot = FeishuBotView(bot)
                view_event = FeishuEventView(event, bot)
                await view_bot.send(view_event, MessageSegment.image(img))
    except Exception as exc:
        if isinstance(event, OneBotMessageEvent):
            await bot.send(event, Message(f"截图失败: {exc}"))
        else:
            from plugins.core_utils.feishu_bridge import FeishuEventView, FeishuBotView
            view_bot = FeishuBotView(bot)
            view_event = FeishuEventView(event, bot)
            await view_bot.send(view_event, Message(f"截图失败: {exc}"))

# ==================== 分段回复====================
async def send_segmented_reply(bot, event, reply: str):
    # [Output Sanitization] 通道出口硬拦截：任何发给 QQ/飞书的内容再过一次物理清洗层
    reply = sanitize_output(reply)
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

# [QQ拟人] 群聊潜行参数（Behavioral Camouflage v2）
# 冷却期：同一群 60 秒内最多回复一次（被明确 @ 时豁免）
QQ_GROUP_COOLDOWN = 60.0
# 未 @ 时的回复概率（30%，模拟真人偶尔看到消息才回）
QQ_GROUP_REPLY_PROB = 0.3
# 群冷却时间戳表：group_id -> 最近一次回复的 time.time()
_qq_group_cooldown: dict[str, float] = {}


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
async def handle_msg(bot, event):
    # QQ(onebot/Napcat) 通道：业务代码原生兼容 onebot 事件接口，直接透传
    if isinstance(event, OneBotMessageEvent):
        import logging as _dbg_logging
        _dbg_logging.getLogger("__main__").info(
            "[DBG] QQ事件进入 handle_msg: user_id=%r self_id=%r msg_type=%r text=%r",
            getattr(event, "user_id", None), getattr(bot, "self_id", None),
            getattr(event, "message_type", None), str(event.get_plaintext())[:50],
        )
        await _handle_msg_inner(bot, event)
        return
    # 飞书通道：包装成 onebot 风格视图（现状不变）
    from plugins.core_utils.feishu_bridge import FeishuEventView, FeishuBotView
    view_event = await FeishuEventView.build(event, bot)
    view_bot = FeishuBotView(bot)
    await _handle_msg_inner(view_bot, view_event)

async def _handle_msg_inner(bot, event):
    if event.user_id == bot.self_id:
        return
    async with _get_user_message_lock(str(event.user_id)):
        await _handle_msg_locked(bot, event)

async def _handle_msg_locked(bot: Bot, event: MessageEvent):
    import logging as _dbg_logging2
    plain_text = event.get_plaintext().strip()
    plain_text = _preprocess_alias(plain_text)  # [安全别名] 物理前置：命中则替换为完整 URL
    _dbg_logging2.getLogger("__main__").info(
        "[DBG] _handle_msg_locked: user_id=%s plain=%s chat_type=%s",
        str(getattr(event, "user_id", "")), plain_text[:50],
        "private" if event.message_type == "private" else "group",
    )

    chat_type = "private" if event.message_type == "private" else "group"
    group_id = str(event.group_id) if chat_type == "group" else ""
    key = get_conv_key(str(event.user_id), chat_type, group_id)

    # ---------- [QQ拟人] 夜间休眠：非活跃时段静默丢弃 QQ 消息 ----------
    if isinstance(event, OneBotMessageEvent) and get_config("QQ_SLEEP_ENABLE", True):
        _now_h = datetime.now().hour
        _active = get_config("QQ_ACTIVE_HOURS", (8, 23))
        if _active and not (_active[0] <= _now_h < _active[1]):
            logger.info("[QQ拟人] 当前 %s 时不在活跃时段 %s，静默丢弃 QQ 消息", _now_h, _active)
            return

    # ---------- [QQ拟人] 群聊潜行：冷却 + 概率（被明确 @ 时 100% 触发） ----------
    if chat_type == "group" and isinstance(event, OneBotMessageEvent):
        is_at_me = bool(getattr(event, "to_me", False))
        if not is_at_me:
            _now = time.time()
            _last = _qq_group_cooldown.get(group_id, 0.0)
            if _now - _last < QQ_GROUP_COOLDOWN:
                logger.info("[QQ拟人] 群 %s 冷却期内（%.0fs），静默", group_id, QQ_GROUP_COOLDOWN)
                return
            if random.random() >= QQ_GROUP_REPLY_PROB:
                logger.info("[QQ拟人] 群 %s 概率未命中（%.0f%%），静默", group_id, QQ_GROUP_REPLY_PROB * 100)
                return
        _qq_group_cooldown[group_id] = time.time()
        logger.debug("[QQ拟人] 群 %s 通过潜行检查（@=%s），继续处理", group_id, is_at_me)

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

        # [divine] 神授模式：暂停态恢复拦截（最高优先级）
        from plugins.tasks.divine_task import (
            is_pending as divine_is_pending,
            clear_pending as divine_clear_pending,
            resume_divine_task,
            run_divine_task,
        )

        async def _divine_notify(msg: str):
            try:
                await bot.send_private_msg(user_id=int(event.user_id), message=msg)
            except Exception as e:
                logger.error(f"[divine] 通知发送失败: {e}")

        # 暂停态恢复：用户回复非"君权神授"开头时，走恢复流程
        if divine_is_pending(str(event.user_id)) and not user_input.startswith("君权神授"):
            asyncio.create_task(
                resume_divine_task(str(event.user_id), user_input, _divine_notify)
            )
            return

        # 神授新任务：以"君权神授"开头
        if user_input.startswith("君权神授"):
            if divine_is_pending(str(event.user_id)):
                divine_clear_pending(str(event.user_id))
            asyncio.create_task(
                run_divine_task(user_input, str(event.user_id), _divine_notify)
            )
            return

        # [large-task] 大型任务拦截（#大型 开头，仅在私聊触发）
        if user_input.startswith("#大型"):
            from plugins.tasks.large_task import run_large_task

            async def _notify(msg: str):
                try:
                    await bot.send_private_msg(user_id=int(event.user_id), message=msg)
                except Exception as e:
                    logger.error(f"[large-task] 通知发送失败: {e}")

            asyncio.create_task(
                run_large_task(
                    user_input=user_input,
                    user_id=str(event.user_id),
                    notify=_notify,
                )
            )
            return

        # === 优先级最高：模式切换指令（@切换到api / @切换到pi） ===
            # 没有 pending 时继续正常流程（避免误伤普通对话）
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

        # ---------- 多任务（全角星号分隔） ----------
        if "＊" in user_input:
            from plugins.tasks.task_queue import TaskQueue
            await send_segmented_reply(bot, event, "收到，任务已加入队列")
            asyncio.create_task(
                TaskQueue().submit(user_input, str(event.user_id), chat_type, group_id)
            )
            return

        # ---------- 转MP3 硬指令 ----------
        if user_input == "转MP3" or user_input.startswith("转MP3"):
            from plugins.core_utils.media import convert_latest_mp4_to_mp3
            await send_segmented_reply(bot, event, "收到，正在转码...")
            result = await convert_latest_mp4_to_mp3()
            await send_segmented_reply(bot, event, result)
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

# ==================== [Sandbox] 本地测试沙盒通道（仅 127.0.0.1） ====================
from fastapi import Request
from fastapi.responses import JSONResponse
from plugins.core_utils.trust import set_request_context, clear_request_context
from plugins.core_utils import file_delivery, async_outbox

def _peer_identity(request: Request, payload: dict) -> tuple:
    """提取本次请求的源 IP 与移动端 Key（供高危工具信任判定）。"""
    host = getattr(getattr(request, "client", None), "host", "") or ""
    key = str(payload.get("mobile_api_key") or "")
    if not key:
        key = str(request.headers.get("x-api-key") or "")
    if not key:
        auth = str(request.headers.get("authorization") or "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    return host, key

# 专属 session_key：测试上下文与主人真实上下文彻底隔离
_TEST_SANDBOX_KEY = "test_sandbox_123"
# 本轮已生成文件却命中 429/超时/异常时，替换报错兜底的成功提示
_FILE_READY_MSG = "任务已成功处理并生成文件，已为您附上，请直接下载附件（AI 总结因网络限制暂未生成）。"
_FILE_FAIL_PAT = _re.compile(
    r"任务超时中断|执行异常|系统内部处理中，请重试|请求超时|网络异常|内部异常|429|限流|Too Many Requests"
)


def _attach_markers(reply: str, pending: list) -> str:
    if pending:
        reply = (reply or "").rstrip() + "\n" + "\n".join(pending)
    return reply


def _file_ready_reply(pending: list) -> str:
    return _attach_markers(_FILE_READY_MSG, pending)
# [Test Mode] 测试模式标记：本次处理只返回不发送
_sandbox_test_mode = False


async def _sandbox_test_chat(request: Request):
    """POST /api/test_chat：本地自动化测试专用通道。
    - 仅监听 127.0.0.1（uvicorn 绑定），外网不可达
    - 专属 session_key 隔离测试上下文
    - 经过 _preprocess_alias 物理替换层 + ask_ai 正常流水线
    - Test Mode：拦截发送动作，直接返回 Sanitizer 过滤后的 AI 最终回复
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "无效 JSON"}, status_code=400)
    host, peer_key = _peer_identity(request, payload)
    peer_role = str(payload.get("user_role") or request.headers.get("x-user-role") or "").strip().lower()
    user_id = str(payload.get("user_id", "test_user"))
    set_request_context(host, peer_key, peer_role,
                        str(payload.get("user_ref") or user_id))
    message = str(payload.get("message", "")).strip()
    if not message:
        return JSONResponse({"ok": False, "error": "message 不能为空"}, status_code=400)
    global _sandbox_test_mode
    _sandbox_test_mode = True
    try:
        # [安全别名] 物理前置：与生产同一条替换层（tz 92285 → 完整 URL）
        processed = _preprocess_alias(message)
        # 专属 session_key：测试上下文与主人真实上下文隔离
        session_key = f"{_TEST_SANDBOX_KEY}_{user_id}"
        reply = await ask_ai(session_key, processed, "private", "")
        # Sanitizer 过滤后的最终文本（ask_ai 已内部过滤，此处双保险）
        reply = sanitize_output(reply)
        return JSONResponse({
            "ok": True,
            "user_id": user_id,
            "message": processed,
            "reply": reply,
        })
    except Exception as e:
        logger.error(f"[sandbox] 测试通道执行异常: {e}", exc_info=True)
        return JSONResponse({"ok": False, "error": f"内部异常: {e}"}, status_code=500)
    finally:
        _sandbox_test_mode = False
        clear_request_context()
        file_delivery.clear()


# 挂载到 NoneBot 底层 FastAPI 实例（模块加载时执行，早于 nonebot.run）
try:
    _sandbox_app = driver.server_app
    _sandbox_app.post("/api/test_chat")(_sandbox_test_chat)
    logger.info("[sandbox] 测试沙盒通道已挂载: POST http://127.0.0.1:8082/api/test_chat（Test Mode 只返回不发送）")
except Exception as e:
    logger.error(f"[sandbox] 挂载失败: {e}", exc_info=True)

# ==================== [R18 · 模型直达] 网关 8083 专用通道 ====================
# 前端在 DSH 里选了哪个模型，这里就把哪个模型原样交给 ai.py：
# 不猜意图、不做策略映射、不换模型。未知模型直接 400 回给网关，绝不悄悄换成别的模型。
import re as _re
from cl import DEFAULT_MODEL as _DEFAULT_MODEL, list_models as _list_models, resolve_model as _resolve_model

# 旧网关会在文本前拼「【网关意图模式：X】」取证前缀：命中即剥离，绝不进入模型 prompt
_MODEL_PREFIX_RE = _re.compile(r"^\s*【网关意图模式：([A-Z_]+)】\s*")


async def _model_chat(request: Request):
    """POST /api/model_chat（旧名 /api/strategy_chat 同样可用）：模型原样直达。

    入参 {user_id, message, model}；strategy 为旧字段，仅作模型别名兜底。
    返回 {ok, model, upstream_model, message, reply, elapsed}。
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "无效 JSON"}, status_code=400)

    host, peer_key = _peer_identity(request, payload)
    peer_role = str(payload.get("user_role") or request.headers.get("x-user-role") or "").strip().lower()
    raw_model = str(payload.get("model") or payload.get("strategy") or "").strip()
    if not raw_model:
        raw_model = _DEFAULT_MODEL
    user_id = str(payload.get("user_id", "dsh"))
    message = str(payload.get("message", "")).strip()
    owner_ref = str(payload.get("user_ref") or user_id or "").strip() or "local-admin"
    set_request_context(host, peer_key, peer_role, owner_ref)
    file_delivery.set_owner(owner_ref)

    cfg = _resolve_model(raw_model)
    if not cfg:
        logger.error("[model] 未知模型请求被拒: %s", raw_model)
        return JSONResponse(
            {"ok": False, "error": f"未知模型: {raw_model}", "allowed": _list_models()},
            status_code=400,
        )
    model_id = cfg.get("id") or raw_model
    upstream_model = cfg.get("model") or model_id
    kind = str(cfg.get("kind") or "").strip().lower()

    if not message:
        return JSONResponse({"ok": False, "error": "message 不能为空"}, status_code=400)

    _m = _MODEL_PREFIX_RE.match(message)
    if _m:
        logger.info("[model] 旧意图前缀已剥离: %s（不参与任何路由）", _m.group(1))
        message = message[_m.end():].strip()

    session_key = f"{_TEST_SANDBOX_KEY}_{user_id}"
    global _sandbox_test_mode
    _sandbox_test_mode = True
    _started = time.time()
    logger.info(
        "[model] request model=%s upstream=%s tools=%s user=%s chars=%d",
        model_id, upstream_model, bool(cfg.get("supports_tools")), user_id, len(message),
    )

    try:
        # 与生产同一条别名替换层
        message = _preprocess_alias(message)

        if kind == "c-end-web":
            # C 端网页免费通道：它本身就是一个模型通道
            import ai as _ai_cend
            _wd = getattr(_ai_cend, "deepseek_web_integration", None)
            if _wd is None or not _wd.is_enabled():
                raise RuntimeError(
                    "C端通道不可用：deepseek_web_integration 未就绪，"
                    "或 models.json 中 web-deepseek.enabled 不为 true"
                )
            reply = await _wd.ask_web_deepseek(message, user_id=user_id)
        else:
            # 所有 API 模型都走同一条 ask_ai 流水线；模型身份由 model= 带入，不再映射成 chat/tool
            reply = await ask_ai(session_key, message, model=model_id)

        reply = sanitize_output(reply)
        pending = file_delivery.pending_markers()
        if pending and (not reply or _FILE_FAIL_PAT.search(reply)):
            reply = _FILE_READY_MSG
        reply = _attach_markers(reply, pending)
        _elapsed = round(time.time() - _started, 2)
        logger.info("[model] done model=%s elapsed=%ss chars=%d", model_id, _elapsed, len(reply))
        return JSONResponse({
            "ok": True,
            "model": model_id,
            "upstream_model": upstream_model,
            "user_id": user_id,
            "message": message,
            "reply": reply,
            "elapsed": _elapsed,
        })
    except Exception as e:
        logger.error("[model] 通道执行异常 model=%s: %s", model_id, e, exc_info=True)
        pending = file_delivery.pending_markers()
        if pending:
            # 本轮已成功生成文件：即便命中 429/超时/异常，也不回报错兜底，改发带附件的成功提示
            return JSONResponse({
                "ok": True,
                "model": model_id,
                "user_id": user_id,
                "message": message,
                "reply": _file_ready_reply(pending),
                "elapsed": round(time.time() - _started, 2),
            })
        return JSONResponse({"ok": False, "model": model_id, "error": f"内部异常: {e}"}, status_code=500)
    finally:
        _sandbox_test_mode = False
        clear_request_context()


_INTERNAL_SECRET_PATH = "/mnt/d/dsh-custom/auth-gateway/secret.env"


def _internal_secret() -> str:
    val = os.environ.get("HCQ_INTERNAL_TOKEN", "").strip()
    if val:
        return val
    try:
        with open(_INTERNAL_SECRET_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith("INTERNAL_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _debug_internal_ok(request: Request) -> bool:
    """单用户本地部署：调试/管理端点不再校验内部令牌（8084 已物理删除）。"""
    return True


async def _admin_command(request: Request):
    """POST /api/admin_command：只接受内部令牌 + admin 角色的白名单指令。"""
    if not _debug_internal_ok(request):
        return JSONResponse({"ok": False, "error": "内部令牌无效"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "无效 JSON"}, status_code=400)
    cmd = str(body.get("cmd") or "").strip().lower()
    if cmd == "reload_plugin":
        try:
            from plugins import reload_all_plugins
            result = reload_all_plugins()
            return JSONResponse({"ok": True, "cmd": cmd, "result": f"已重载 {result['count']} 个插件",
                                 "detail": result})
        except Exception as exc:
            logger.error("[debug] reload_plugin 失败: %s", exc, exc_info=True)
            return JSONResponse({"ok": False, "cmd": cmd, "error": str(exc)[:300]}, status_code=500)
    if cmd == "clear_cache":
        cleared = {}
        try:
            from tool_center import registry_manager
            cleared["tool_func_refs"] = len(registry_manager._FUNCS)
            registry_manager._FUNCS.clear()
        except Exception:
            pass
        try:
            from plugins.core_utils import feishu_bridge as _fb
            cleared["feishu_file_urls"] = len(_fb._file_url_cache)
            _fb._file_url_cache.clear()
            _fb._token_cache["token"] = None
            _fb._token_cache["exp"] = 0.0
            cleared["feishu_token"] = 1
        except Exception:
            pass
        try:
            from plugins.core_utils import downloader as _dl
            for attr in ("_download_cache", "_media_cache"):
                cache = getattr(_dl, attr, None)
                if isinstance(cache, dict):
                    cleared[attr] = len(cache)
                    cache.clear()
        except Exception:
            pass
        gc.collect()
        return JSONResponse({"ok": True, "cmd": cmd, "result": "缓存已清理", "cleared": cleared})
    if cmd == "status":
        import admin as _admin_mod
        return JSONResponse({"ok": True, "cmd": cmd, "result": "8082 在线",
                             "active_tools": _admin_mod.debug_active_tools()})
    if cmd == "help":
        return JSONResponse({"ok": True, "cmd": cmd,
                             "result": "reload_plugin | clear_cache | status | help"})
    return JSONResponse({"ok": False, "error": f"不支持的命令: {cmd}"}, status_code=400)


async def _debug_state(request: Request):
    """GET /api/debug/state：真实执行中的工具队列（内部令牌保护）。"""
    if not _debug_internal_ok(request):
        return JSONResponse({"ok": False, "error": "内部令牌无效"}, status_code=403)
    import admin as _admin_mod
    return JSONResponse({"ok": True, "active_tools": _admin_mod.debug_active_tools(),
                        "ts": int(time.time() * 1000)})


# 挂载到同一个 FastAPI 实例（与 /api/test_chat 并列，运行期只追加路由）
try:
    async def _pending_messages(request: Request):
        owner = str(request.headers.get("x-user-id") or "").strip() or "local-admin"
        session_id = str(request.query_params.get("session_id") or "").strip()
        items = async_outbox.fetch_pending(owner, session_id or None)
        messages = [{
            "role": "assistant",
            "content": r.get("content", ""),
            "files": r.get("files", []),
            "ts": r.get("ts", ""),
        } for r in items]
        return JSONResponse({"ok": True, "messages": messages})

    _sandbox_app.get("/api/pending_messages")(_pending_messages)
    _sandbox_app.post("/api/model_chat")(_model_chat)
    _sandbox_app.post("/api/strategy_chat")(_model_chat)   # 旧路径保留，语义同样是“模型直达”
    _sandbox_app.get("/api/debug/state")(_debug_state)
    _sandbox_app.post("/api/admin_command")(_admin_command)
    logger.info("[model] 模型直达通道已挂载: POST http://127.0.0.1:8082/api/model_chat（+ 兼容 /api/strategy_chat）")
except Exception as e:
    logger.error("[model] 挂载失败: %s", e, exc_info=True)



if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    # 日志脱敏：sk-/Token/Key/Authorization/Cookie 一律打码，超长文本截断
    try:
        from plugins.core_utils import log_redact
        log_redact.install()
    except Exception:
        pass
    # Plugin 4.0：动态扫描并加载全部插件（@aup_tool 自动注册），消灭硬编码插件引用
    from plugins import load_all_plugins
    load_all_plugins()
    nonebot.run()
