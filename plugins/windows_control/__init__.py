# -*- coding: utf-8 -*-
"""Windows Control Unit (WCU) —— Windows 物理世界操控的唯一指挥单元（Plugin 6.0）。

一句话定位：
    把散落在 ai.py 里的 win_shell / win_ui_automation / interactive_browser
    收拢成"一个单元、一道闸门"，对外工具名保持不变（AI 侧无感迁移）。

零信任四道闸（全部在代码层强制，不依赖提示词自律）：
    1. 应用白名单    guards.assert_app_allowed     → UNAUTHORIZED_APP
    2. 通讯强校验    guards.assert_messenger_action → 只允许 send_message(receiver, message)
    3. 落盘/未知点击  guards.assert_shell_safe / assert_click_safe / assert_browser_actions
    4. URL 校验      url_policy.check_url           → URL_NOT_ALLOWED

模块分工：
    context.py        请求级"用户原话"上下文（ContextVar，按协程隔离）
    policy.py         只读消费 config_center/wcu_policy.json 的白名单与开关（AI 无权写）
    guards.py         四类拦截器（唯一执行闸门）
    url_policy.py     主流域名白名单 + 原话比对
    winenv.py         WSL → Windows 脚本调用封装
    shell_unit.py     win_shell 本体
    ui_unit.py        win_ui_automation 本体（含通讯受控发送）
    browser_unit.py   interactive_browser 本体
    deep_optimize.py  任务 2 预留位（QQNT 协议注入 / 浏览器书签索引库）
"""
from __future__ import annotations

import json
import logging

from aup_core import aup_tool

from . import deep_optimize, policy
from . import context as wcu_context
from .browser_unit import run_browser
from .shell_unit import run_shell
from .ui_unit import run_ui

logger = logging.getLogger("plugins.windows_control")


# ==================== 参数声明（AUP 与 SQLite 共用同一份，避免漂移） ====================
PARAMS_SHELL = {
    "required": ["command"],
    "properties": {
        "command": {"type": "string", "description": "要在 Windows 上执行的命令文本"},
        "shell": {"type": "string", "description": "powershell（默认）或 cmd"},
        "timeout": {"type": "integer", "description": "超时秒数，默认 60，上限 600"},
        "elevated": {"type": "boolean", "description": "是否要求管理员权限（走提权 broker）"},
        "cwd": {"type": "string", "description": "工作目录，可选"},
    },
}

PARAMS_UI = {
    "required": ["action"],
    "properties": {
        "action": {"type": "string",
                   "enum": ["launch", "list_windows", "list_controls", "read", "click",
                            "type", "keys", "close", "wait", "send_message"],
                   "description": "只能取下列之一：launch / list_windows / list_controls / read / "
                                  "click / type / keys / close / wait / send_message"},
        "app_name": {"type": "string", "description": "应用或窗口名，如 计算器 / 记事本 / qq / 微信"},
        "control_name": {"type": "string", "description": "控件名或 automation_id（click/type/read 定位用）"},
        "control_type": {"type": "string", "description": "可选，如 Button / Edit"},
        "text": {"type": "string", "description": "type/keys 要输入的内容"},
        "receiver": {"type": "string", "description": "send_message 专用：收件人（必须精确到好友/群名）"},
        "message": {"type": "string", "description": "send_message 专用：要发送的内容"},
    },
}

PARAMS_BROWSER = {
    "required": ["url"],
    "properties": {
        "url": {"type": "string", "description": "目标网页 URL（http/https），必填"},
        "actions": {"type": "string",
                    "description": "可选 JSON 数组字符串：wait/click/type/press/scroll/screenshot/back/forward/url"},
    },
}

DESC_SHELL = (
    "【WCU·Windows 命令执行器】当主人要求执行 Windows 命令、查看本机系统状态"
    "（CPU/内存/磁盘/进程/服务/注册表/网络）、运行 PowerShell 或 cmd 时调用。"
    "零信任约束：命令若包含下载/安装动作（Invoke-WebRequest、curl、winget install、msiexec 等），"
    "而用户本轮没有明确说“下载/安装”，会被代码层直接拦截（UNAUTHORIZED_DOWNLOAD）。"
    "注意：不要用本工具（curl / Invoke-WebRequest / wget）去抓取或浏览网页内容，"
    "凡是“打开某个网址看看/浏览网页”一律改用 interactive_browser。"
    "参数：command（必填）、shell（powershell|cmd）、timeout（默认60，上限600）、"
    "elevated（是否走提权 broker）、cwd。"
)

DESC_UI = (
    "【WCU·Windows 原生 UI 自动化】不截图，直接用 UI Automation 操控桌面软件。"
    "零信任约束（三条，违反即报错中止）："
    "① 应用必须在 WIN_ALLOWED_APPS 白名单内（计算器/记事本/qq/微信/edge/chrome），否则 UNAUTHORIZED_APP；"
    "被 UNAUTHORIZED_APP 拦截时不要放弃、也不要绕路：如确有需要，调用 submit_system_proposal "
    "呈递《系统修改提案》，请主人裁决是否加入 config_center/wcu_policy.json（AI 无权自行修改）；"
    "② 通讯软件（QQ/微信）禁止 click/type/keys 裸操作，必须用 action=send_message 并提供 "
    "receiver（具体好友名）与 message（内容），系统会先搜索并精确匹配联系人，找不到就报 "
    "CONTACT_NOT_FOUND 并中止，绝不乱点“发送”；"
    "收到 CONTACT_NOT_FOUND 时必须立刻停止并向主人如实说明“未发送”，"
    "严禁改用搜索框、win_shell、按键模拟或其他方式绕行；"
    "③ click 必须给出明确的 control_name，未知按钮直接拦（BLIND_CLICK）。"
    "标准流程（计算器）：action=launch（app_name=计算器）→ action=keys（text=123*456=）→ action=read。"
    "动作：launch / list_windows / list_controls / read / click / type / keys / close / wait / send_message。"
    "【给好友发消息的正确用法】主人说“给某某发某内容”时，直接调用一次 "
    "action=send_message app_name=QQ(或微信) receiver=某某 message=内容；"
    "不要先 launch、不要先 list_controls、不要试探其他 action、不要用 win_shell 起 QQ。"
)

DESC_BROWSER = (
    "【WCU·有头浏览器接管】当主人说「帮我去某网站看看/打开网页/浏览一下/打开某链接」时调用。"
    "Windows 桌面弹出真实 Edge 窗口（headless=False），按 actions 顺序执行，返回截图、DOM 文本与 F12 报告。"
    "零信任约束：URL 必须是 http/https；主流大站（淘宝/天猫/京东/拼多多/百度/B站/知乎/微博/抖音/小红书等）"
    "直接放行；小众域名必须能在用户本轮原话中找到，否则报 "
    "「目标网址未在白名单内，且未在指令中明确提供，请提供确切 URL」。"
    "要操作桌面软件（计算器/记事本/微信/QQ）请改用 win_ui_automation；"
    "不要用 win_shell 的 curl 抓网页，浏览一律走本工具。"
    "参数：url（必填）、actions（可选 JSON 数组字符串）。"
    "【正确用法】只想“打开看看”时只传 url 即可，脚本默认会打开页面、等待加载、截图并取文本；"
    "不要把中文描述（如“打开页面”）塞进 actions，也不要先用 win_shell start msedge 开窗。"
)


# ==================== 对外工具：win_shell ====================
@aup_tool(name="win_shell", description=DESC_SHELL, category="system",
          is_danger=False, params_schema=PARAMS_SHELL, version="1.1.0")
async def win_shell(command: str = "", shell: str = "powershell", timeout: int = 60,
                    elevated: bool = False, cwd: str = "") -> str:
    return await run_shell(command, shell, timeout, elevated, cwd)


# ==================== 对外工具：win_ui_automation ====================
@aup_tool(name="win_ui_automation", description=DESC_UI, category="system",
          is_danger=False, params_schema=PARAMS_UI, version="1.1.0")
async def win_ui_automation(action: str = "", app_name: str = "", control_name: str = "",
                            control_type: str = "", text: str = "",
                            receiver: str = "", message: str = "") -> str:
    return await run_ui(action, app_name, control_name, control_type, text, receiver, message)


# ==================== 对外工具：interactive_browser ====================
@aup_tool(name="interactive_browser", description=DESC_BROWSER, category="network",
          is_danger=False, params_schema=PARAMS_BROWSER, version="1.1.0")
async def interactive_browser(url: str = "", actions: str = "") -> str:
    return await run_browser(url, actions)


# ==================== 注册入库（tool_center.db） ====================
_TOOLS_META = (
    ("win_shell", win_shell, "system", DESC_SHELL, PARAMS_SHELL),
    ("win_ui_automation", win_ui_automation, "system", DESC_UI, PARAMS_UI),
    ("interactive_browser", interactive_browser, "network", DESC_BROWSER, PARAMS_BROWSER),
)


def _schema_json(name: str, desc: str, params: dict) -> str:
    """按 tool_center 既有格式生成 function schema。"""
    return json.dumps({"type": "function", "function": {
        "name": name, "description": desc, "parameters": params,
    }}, ensure_ascii=False)


try:
    from tool_center.registry_manager import register_tool as _register_tool

    for _name, _fn, _cat, _desc, _params in _TOOLS_META:
        _register_tool(_name, _fn, description=_desc, category=_cat,
                       schema_json=_schema_json(_name, _desc, _params),
                       is_danger=False, is_active=True, need_confirm=False,
                       params_desc=_params,
                       module_path="plugins.windows_control", func_name=_name)
    logger.info("[WCU] 三件工具已注册入库（含参数 schema）：%s",
                ", ".join(m[0] for m in _TOOLS_META))
except Exception as _reg_err:  # SQLite 不可用也不挡加载（AUP_REGISTRY 已在内存）
    logger.warning("[WCU] 注册入库失败: %s", _reg_err)

logger.info("[WCU] 已加载，零信任开关=%s，应用白名单=%s",
            policy.zero_trust_mode(), ", ".join(sorted(policy.allowed_apps())))


__all__ = [
    "win_shell", "win_ui_automation", "interactive_browser",
    "wcu_context", "deep_optimize", "policy", "run_shell", "run_ui", "run_browser",
]
