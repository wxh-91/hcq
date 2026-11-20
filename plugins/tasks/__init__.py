# -*- coding: utf-8 -*-
"""Tasks 插件（Plugin 3.0）—— 重型任务调度物理隔离单元。

承载三大自主循环/长线调度模块（原 bot_utils/ 迁入）：
  - divine_task  : 神授模式（run_divine_task / is_pending / clear_pending / resume_divine_task）
  - large_task   : 大型任务规划（run_large_task，#大型 前缀触发）
  - task_queue   : 全角星号 ＊ 多任务队列（TaskQueue 单例，串行执行）

接口：本包 re-export 全部公开接口，dz.py 等调用方既可
  `from plugins.tasks.divine_task import ...`（子模块路径），
  也可 `from plugins.tasks import run_divine_task`（包级转发）。
配置：模块内部经 config_center.get_config 读取（DIVINE_MAX_STEPS_PER_ROUND=100、
LARGE_TASK_TOTAL_MAX_STEPS=500 等），中央配置无需迁移。
依赖：ai / admin / plugins.core_utils.scores 均在函数内延迟 import，无循环引用。
"""
from .divine_task import (
    run_divine_task as run_divine_task,
    is_pending as is_pending,
    clear_pending as clear_pending,
    resume_divine_task as resume_divine_task,
)
from .large_task import run_large_task as run_large_task
from .task_queue import (
    split_tasks as split_tasks,
    map_tasks_to_tools as map_tasks_to_tools,
    sort_by_scores as sort_by_scores,
    TaskQueue as TaskQueue,
)
