# -*- coding: utf-8 -*-
"""Plugin 4.0 —— 动态插件加载器。

约定：plugins/ 下每个含 __init__.py 的子目录视为一个插件包；
插件包在导入时经 @aup_tool 将内部工具注册到 AUP_REGISTRY（或经 tool_center.db
的 module_path 动态解析）。主程序启动仅调用 load_all_plugins() 一次，
扫描、导入、容错、日志全部收敛在此，消灭物理硬编码引用。

加载顺序：按目录名字典序（core_utils 先于 crawler/system_ops/tasks，
保证全局辅助先就绪）；单个插件失败仅记录，不中断其余插件与主程序。
"""
import importlib
import logging
import os
import sys

logger = logging.getLogger("plugins.loader")


def _plugin_names() -> list:
    root = os.path.dirname(os.path.abspath(__file__))
    names = []
    for name in sorted(os.listdir(root)):
        if name.startswith("__") or name.startswith("."):
            continue
        pkg_dir = os.path.join(root, name)
        if os.path.isdir(pkg_dir) and os.path.exists(os.path.join(pkg_dir, "__init__.py")):
            names.append(name)
    return names


def load_all_plugins() -> list:
    """扫描 plugins/ 下所有含 __init__.py 的子包并动态导入。

    返回：成功加载的插件名列表（按字典序）。失败插件记录 warning 不抛出。
    """
    root = os.path.dirname(os.path.abspath(__file__))
    loaded: list = []
    failed: list = []
    for name in sorted(os.listdir(root)):
        if name.startswith("__") or name.startswith("."):
            continue
        pkg_dir = os.path.join(root, name)
        if not os.path.isdir(pkg_dir):
            continue
        if not os.path.exists(os.path.join(pkg_dir, "__init__.py")):
            continue
        try:
            importlib.import_module("plugins.%s" % name)
            loaded.append(name)
        except Exception as e:
            logger.error("[plugins.loader] 插件 %s 加载失败: %s", name, e, exc_info=True)
            failed.append(name)
    if loaded:
        logger.info("成功加载插件：%s", ", ".join(loaded))
    if failed:
        logger.warning("插件加载失败：%s", ", ".join(failed))
    return loaded


def reload_all_plugins() -> dict:
    """真正重载 plugins/*：清空模块缓存与函数引用后重新导入并重新注册 AUP 工具。

    返回 {"loaded": [...], "failed": [...], "count": n}，供 8082 调试指令台回显。
    """
    try:
        from tool_center import registry_manager
        registry_manager._FUNCS.clear()
    except Exception as exc:
        logger.warning("[plugins.reload] 清空工具函数缓存失败: %s", exc)
    names = _plugin_names()
    for name in names:
        prefix = "plugins.%s" % name
        for mod in list(sys.modules):
            if mod == prefix or mod.startswith(prefix + "."):
                sys.modules.pop(mod, None)
    loaded, failed = [], []
    for name in names:
        mod_name = "plugins.%s" % name
        try:
            importlib.import_module(mod_name)
            loaded.append(name)
        except Exception as exc:
            logger.error("[plugins.reload] 插件 %s 重载失败: %s", name, exc, exc_info=True)
            failed.append(name)
    logger.info("[plugins.reload] loaded=%s failed=%s", loaded, failed)
    return {"loaded": loaded, "failed": failed, "count": len(loaded)}
