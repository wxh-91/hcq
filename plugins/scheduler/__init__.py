# -*- coding: utf-8 -*-
"""Scheduler 插件（Plugin 7.0 · Chronos）—— 持久化多模式定时任务中枢。

基于 APScheduler 自建 AsyncIOScheduler：
  - JobStore：SQLAlchemyJobStore（sqlite:////root/robot/scheduler.db），进程重启自动恢复未执行任务。
  - 触发器：cron（循环）/ date（一次性绝对时间）/ interval（固定间隔）三种模式。
  - 到点经 notify_bridge 投递「【定时提醒】任务描述」（本地 outbox + Windows 弹窗；飞书在线时顺带飞书）。
防呆红线：任务总数上限 10 个；trigger 参数非法直接拒绝；成功返回 task_id。
"""
from __future__ import annotations
import os
_ROOT = os.environ.get("PROJECT_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import json
import logging
import uuid
from datetime import datetime

from aup_core import aup_tool

logger = logging.getLogger("plugins.scheduler")

MAX_JOBS = 10
SCHEDULER_DB = _ROOT + "/scheduler.db"


def _ok(data) -> str:
    return json.dumps(data, ensure_ascii=False)


def _err(msg: str) -> str:
    return json.dumps({"success": False, "error": msg}, ensure_ascii=False)


# ============ 持久化调度器（Chronos 核心基建） ============
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
import os


_jobstores = {
    "default": SQLAlchemyJobStore(url="sqlite:///%s" % SCHEDULER_DB)
}
_scheduler = AsyncIOScheduler(jobstores=_jobstores, timezone="Asia/Shanghai")


async def _start_scheduler():
    """NoneBot 启动时启动调度器；SQLAlchemyJobStore 自动从 db 恢复未执行任务。"""
    if not _scheduler.running:
        _scheduler.start()
        jobs = _scheduler.get_jobs()
        logger.info("[scheduler] 调度器已启动（持久化 db=%s），恢复任务 %d 个", SCHEDULER_DB, len(jobs))
        for j in jobs:
            logger.info("[scheduler]   - 恢复 %s | %s | %s", j.id, j.trigger, j.args)


async def _stop_scheduler():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[scheduler] 调度器已关闭")


def _hook_lifecycle():
    try:
        from nonebot import get_driver

        driver = get_driver()
        driver.on_startup(_start_scheduler)
        driver.on_shutdown(_stop_scheduler)
        logger.info("[scheduler] 生命周期钩子已注册（startup/shutdown）")
    except Exception as e:
        logger.error("[scheduler] 生命周期钩子注册失败: %s", type(e).__name__)


_hook_lifecycle()


async def _notify_user(text: str) -> None:
    """R11-notify-bridge：到点提醒统一走 notify_bridge（飞书可选 + 本地 outbox 兜底）。

    关掉飞书后提醒仍然落盘，进程重启后 SQLAlchemyJobStore 恢复的任务照常触发。
    函数名与所在模块保持不变 —— job_state 里存的是 `plugins.scheduler:_notify_user`，
    改名会让已持久化的任务无法恢复。
    """
    try:
        from plugins.core_utils.notify_bridge import notify

        await notify(text, title="定时提醒", level="reminder")
    except Exception as e:
        logger.error("[scheduler] 提醒投递失败: %s", type(e).__name__)


def _job_count() -> int:
    try:
        return len(_scheduler.get_jobs())
    except Exception:
        return 0


def _build_trigger(
    trigger_type: str,
    cron_expr: str = "",
    run_date: str = "",
    interval_hours: int = 0,
    interval_minutes: int = 0,
    interval_seconds: int = 0,
):
    """按 trigger_type 构造 APScheduler Trigger；参数非法抛 ValueError。"""
    tt = (trigger_type or "").strip().lower()
    if tt == "cron":
        cron_expr = (cron_expr or "").strip()
        if not cron_expr:
            raise ValueError("trigger_type=cron 必须提供 cron_expr（如 0 8 * * *）")
        from apscheduler.triggers.cron import CronTrigger

        return CronTrigger.from_crontab(cron_expr)
    if tt == "date":
        run_date = (run_date or "").strip()
        if not run_date:
            raise ValueError("trigger_type=date 必须提供 run_date（如 2026-10-26 12:00:00）")
        from apscheduler.triggers.date import DateTrigger

        try:
            dt = datetime.fromisoformat(run_date.replace(" ", "T"))
        except ValueError:
            dt = datetime.strptime(run_date, "%Y-%m-%d %H:%M:%S")
        return DateTrigger(run_date=dt)
    if tt == "interval":
        if not (interval_hours or interval_minutes or interval_seconds):
            raise ValueError("trigger_type=interval 必须提供至少一个 >0 的间隔参数（interval_hours/interval_minutes/interval_seconds）")
        from apscheduler.triggers.interval import IntervalTrigger

        return IntervalTrigger(
            hours=int(interval_hours or 0),
            minutes=int(interval_minutes or 0),
            seconds=int(interval_seconds or 0),
        )
    raise ValueError("trigger_type 只能是 cron / date / interval 三选一（收到：%s）" % trigger_type)


@aup_tool(
    name="schedule_task",
    description="定时任务注册（Chronos 多模式）。trigger_type 只能是 cron / date / interval 三选一："
                "cron=循环任务（同时传 cron_expr 五段式，如 \"0 8 * * *\" 每天早上8点）；"
                "date=一次性绝对时间（同时传 run_date，如 \"2026-10-26 12:00:00\"，到点执行一次后自动移除）；"
                "interval=固定间隔循环（同时传 interval_hours/interval_minutes/interval_seconds 之一，如每隔2小时传 interval_hours=2；interval 任务跨天持续运行属正常现象，用户说「每隔/每XX小时/每XX分钟」时直接用 interval 模式，不要被「今天/明天/仅今天」等日期限定词干扰而放弃调用）。"
                "到点经本地通知器投递「【定时提醒】+任务描述」（写入本地 outbox 并弹 Windows 卡片；飞书已退场）。任务上限 10 个。"
                "注意：当前系统时间已注入模型上下文，涉及「今天/明天/半小时后」等相对时间请基于当前时间推导出绝对时间后使用 date 模式（传 run_date），不要用 cron 模拟单次任务。",
    category="system",
    is_danger=False,
    params_schema={
        "required": ["task_desc", "trigger_type"],
        "properties": {
            "task_desc": {"type": "string", "description": "任务描述，如 提醒我喝水"},
            "trigger_type": {
                "type": "string",
                "enum": ["cron", "date", "interval"],
                "description": "触发器模式三选一：cron=循环任务，date=一次性绝对时间，interval=固定间隔",
            },
            "cron_expr": {
                "type": "string",
                "description": "trigger_type=cron 时必填：五段式 Cron，如 0 8 * * *（每天早上8点）",
            },
            "run_date": {
                "type": "string",
                "description": "trigger_type=date 时必填：一次性绝对时间，格式 2026-10-26 12:00:00",
            },
            "interval_hours": {"type": "integer", "description": "trigger_type=interval 时的间隔小时数，如每隔2小时填 2"},
            "interval_minutes": {"type": "integer", "description": "trigger_type=interval 时的间隔分钟数"},
            "interval_seconds": {"type": "integer", "description": "trigger_type=interval 时的间隔秒数"},
        },
    },
    suggestion_hint="trigger 参数非法、任务超上限时直接说明原因；相对时间基于当前时间推导",
)
async def schedule_task(
    task_desc: str,
    trigger_type: str,
    cron_expr: str = "",
    run_date: str = "",
    interval_hours: int = 0,
    interval_minutes: int = 0,
    interval_seconds: int = 0,
) -> str:
    try:
        trigger = _build_trigger(
            trigger_type,
            cron_expr=cron_expr,
            run_date=run_date,
            interval_hours=interval_hours,
            interval_minutes=interval_minutes,
            interval_seconds=interval_seconds,
        )
    except ValueError as e:
        return _err("触发器参数无效：%s" % e)
    if _job_count() >= MAX_JOBS:
        return _err("定时任务已达上限 %d 个，请先取消部分任务再注册" % MAX_JOBS)
    task_id = "sched_%s" % uuid.uuid4().hex[:8]
    text = "【定时提醒】%s" % task_desc
    try:
        _scheduler.add_job(
            _notify_user,
            trigger=trigger,
            args=[text],
            id=task_id,
            replace_existing=True,
        )
    except Exception as e:
        return _err("任务注册失败：%s" % type(e).__name__)
    logger.info(
        "[scheduler] 已注册任务 %s：%s | trigger=%s | cron=%s run_date=%s interval=%s/%s/%s",
        task_id, task_desc, trigger_type, cron_expr, run_date, interval_hours, interval_minutes, interval_seconds,
    )
    return _ok(
        {
            "success": True,
            "task_id": task_id,
            "msg": "定时任务已注册（持久化到 %s，重启自动恢复）" % SCHEDULER_DB,
            "desc": task_desc,
            "trigger_type": trigger_type,
            "run_date": run_date,
            "cron_expr": cron_expr,
            "interval": [interval_hours, interval_minutes, interval_seconds],
        }
    )


@aup_tool(
    name="cancel_scheduled_task",
    description="取消指定定时任务：传入 schedule_task 返回的 task_id 即可取消（同步从持久化库移除，APScheduler remove_job 双端生效）。任务不存在时返回错误。删除成功后必须继续调用 list_scheduled_tasks 展示剩余任务列表，证明确实删除。",
    category="system",
    is_danger=False,
    params_schema={
        "required": ["task_id"],
        "properties": {"task_id": {"type": "string", "description": "要取消的定时任务 ID（如 sched_ab12cd34）"}},
    },
)
async def cancel_scheduled_task(task_id: str) -> str:
    try:
        _scheduler.remove_job(task_id)
    except Exception:
        # 内存移除失败：兜底从持久化库删除（防"DB 有、内存无"场景的假删除）
        try:
            import sqlite3 as _sq
            con = _sq.connect(SCHEDULER_DB)
            cur = con.execute("DELETE FROM apscheduler_jobs WHERE id=?", (task_id,))
            con.commit()
            removed_db = cur.rowcount > 0
            con.close()
        except Exception:
            removed_db = False
        if not removed_db:
            return _err("任务不存在或已取消：%s" % task_id)
        logger.warning("[scheduler] 任务 %s 内存移除失败，已从持久化库兜底删除（防假删除）", task_id)
    # 删除成功后附剩余任务列表（双保险：即使 AI 忘调 list_scheduled_tasks，结果也已含剩余清单）
    try:
        jobs = _scheduler.get_jobs()
        remaining = [j.id for j in jobs] if jobs else []
    except Exception:
        remaining = []
    if not remaining:
        # get_jobs 为空/调度器未运行时回退查持久化库，保证"剩余 N 个"始终准确
        try:
            import sqlite3 as _sq
            con2 = _sq.connect(SCHEDULER_DB)
            remaining = [r[0] for r in con2.execute("SELECT id FROM apscheduler_jobs").fetchall()]
            con2.close()
        except Exception:
            remaining = []
    logger.info("[scheduler] 已取消任务 %s，剩余 %d 个", task_id, len(remaining))
    return _ok({"success": True, "msg": "任务已取消（已从持久化库移除），剩余 %d 个定时任务" % len(remaining), "task_id": task_id, "remaining_tasks": remaining})


@aup_tool(
    name="list_scheduled_tasks",
    description="列出当前所有定时任务（task_id 与下次触发时间）。取消/删除定时任务后必须调用本工具展示剩余任务列表，证明确实删除。",
    category="system",
    is_danger=False,
    params_schema={"required": [], "properties": {}},
)
async def list_scheduled_tasks() -> str:
    """列出剩余定时任务：优先内存 scheduler.get_jobs()，未启动时回退查 scheduler.db。"""
    try:
        jobs = _scheduler.get_jobs()
    except Exception:
        jobs = []
    if not jobs:
        # 回退：从持久化库读
        try:
            import sqlite3 as _sq
            con = _sq.connect(SCHEDULER_DB)
            rows = con.execute("SELECT id FROM apscheduler_jobs").fetchall()
            if rows:
                return _ok({"success": True, "msg": "当前剩余 %d 个定时任务（持久化库）" % len(rows), "tasks": [{"task_id": r[0]} for r in rows]})
            return _ok({"success": True, "msg": "当前没有任何定时任务（剩余 0 个）", "tasks": []})
        except Exception as e:
            return _err("列出定时任务失败: %s" % e)
    tasks = [{"task_id": j.id, "next_run": str(j.next_run_time) if j.next_run_time else None, "name": (j.name or "")[:60]} for j in jobs]
    return _ok({"success": True, "msg": "当前剩余 %d 个定时任务" % len(tasks), "tasks": tasks})
