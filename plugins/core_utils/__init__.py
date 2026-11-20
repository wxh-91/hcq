# -*- coding: utf-8 -*-
"""core_utils —— 全局辅助工具插件（Plugin 4.0，原 bot_utils/ 迁移而来）。

承载全项目共享的基础设施（按功能归组，均为普通模块，非 AUP 工具）：
  - 记忆/词库 : episodic.py（对话记忆）、lexicon.py + lexicon.db（EHIR 动态词库）
  - 权限/安全 : security.py（权限判定）、security_scanner.py（输出清洗）
  - 通道桥接 : feishu_bridge.py（飞书事件/发送兼容层）
  - AUP 工具  : aup_tools.py（execute_shell / run_bash_script 标杆实现，db 注册指向本包）
  - 其余      : scores.py / media.py / checkin.py / downloader.py / admin_gh.py /
               backup_manifest.py / tool_schema_builder（schema 构建）

说明：本包为普通辅助包（不注册 AUP 工具），由 load_all_plugins() 动态导入；
词库/记忆等持久化文件（lexicon.db）随包迁移，路径经 __file__ 动态解析。
"""
