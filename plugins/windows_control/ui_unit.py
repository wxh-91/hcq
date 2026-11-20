# -*- coding: utf-8 -*-
"""win_ui_automation 实现本体（从 ai.py 迁入 WCU）。

与旧版的唯一差别：所有动作先过零信任闸门。
特别是通讯软件 —— 只允许 send_message(receiver, message)，
由 Windows 侧脚本在「会话列表/联系人」面板做名称完全相等匹配 → 输入 → 发送，
任何一步失败即中止，绝不盲点（不使用全局搜索框，避免搜索回显条目被误认成联系人）。
"""
import logging

from aup_core import AUPError

from . import guards, winenv

logger = logging.getLogger("plugins.windows_control.ui")


async def run_ui(action: str = "", app_name: str = "", control_name: str = "",
                 control_type: str = "", text: str = "",
                 receiver: str = "", message: str = "") -> str:
    """原生控件操控：读控件文本、注入文本、点击、通讯软件受控发送。"""
    act = guards.normalize_action(action)
    if not act:
        raise AUPError("MISSING_ACTION", "win_ui_automation 必须提供 action")

    # 参数归一：模型常把消息正文塞进 text 而漏传 message
    if act == "send_message" and not (message or "").strip() and (text or "").strip():
        message = text.strip()

    # ---- 零信任闸门（顺序有意义：先白名单，再通讯，再点击）----
    if app_name:
        guards.assert_app_allowed(app_name)
    elif act != "list_windows":
        raise AUPError("MISSING_APP", "除 list_windows 外，所有动作都必须提供 app_name")
    guards.assert_messenger_action(app_name, act, receiver, message, control_name)
    guards.assert_click_safe(control_name, act)

    payload = {
        "action": act,
        "app_name": app_name or "",
        "control_name": control_name or "",
        "control_type": control_type or "",
        "text": text or "",
    }
    if act == "send_message":
        payload["receiver"] = (receiver or "").strip()
        payload["message"] = (message or "").strip()

    logger.info("[win_ui_automation] action=%s app=%s ctrl=%s receiver=%s",
                payload["action"], payload["app_name"], payload["control_name"],
                payload.get("receiver", ""))

    result = winenv.win_call(winenv.WIN_UI_SCRIPT, payload, timeout=180)
    status = result.get("status")

    # 计算器输入后自动回读显示值：避免模型只凭算术口算、不回读真实控件
    if (status == "ok" and act in ("keys", "type")
            and any(k in (app_name or "").lower() for k in ("计算器", "calc"))):
        try:
            _read = winenv.win_call(winenv.WIN_UI_SCRIPT,
                                    {"action": "read", "app_name": app_name,
                                     "control_name": "", "control_type": "",
                                     "text": ""}, timeout=60)
            if _read.get("status") == "ok" and (_read.get("display") or _read.get("result")):
                result = dict(result)
                result["display"] = _read.get("display") or _read.get("result")
                result["expression"] = _read.get("expression") or ""
        except Exception as exc:  # 回读失败不影响主流程，但要在结果里说清楚
            logger.warning("[win_ui_automation] 计算器回读失败: %s", exc)
            result = dict(result)
            result["note"] = "计算器回读失败: %s" % str(exc)[:80]


    if status == "ok":
        if act == "send_message":
            return "\n".join([
                "Windows 通讯软件受控发送结果",
                "收件人: %s | 联系人控件: 精确匹配成功 | 发送方式: %s" % (
                    result.get("receiver"), "按钮" if result.get("sent_via") == "button" else "回车"),
                "内容: %s" % (result.get("message") or ""),
                "窗口: %s（%s, pid=%s）" % (
                    (result.get("window") or {}).get("title"),
                    (result.get("window") or {}).get("class_name"),
                    (result.get("window") or {}).get("pid")),
            ])
        lines = ["Windows 原生 UI 自动化结果", "action: %s | 状态: 成功" % result.get("action")]
        win = result.get("window") or {}
        if win:
            lines.append("窗口: %s（%s, pid=%s）" % (win.get("title"), win.get("class_name"), win.get("pid")))
        if result.get("note"):
            lines.append("备注: %s" % result.get("note"))
        if result.get("sequence"):
            lines.append("输入按键序列: %s" % result.get("sequence"))
        if result.get("keys") is not None and not result.get("sequence"):
            lines.append("注入内容: %s" % result.get("keys"))
        if result.get("typed") is not None:
            lines.append("注入内容: %s（目标: %s）" % (result.get("typed"), result.get("target")))
        if result.get("clicked") is not None:
            lines.append("已点击控件: %s" % result.get("clicked"))
        if result.get("closed"):
            lines.append("窗口已关闭")
        display = (result.get("display") or "").strip()
        expression = (result.get("expression") or "").strip()
        value = (result.get("result") or "").strip()
        if display:
            lines.append("【控件读到的结果显示】%s" % display)
        if expression:
            lines.append("表达式: %s" % expression)
        if value and value != display:
            lines.append("读取结果: %s" % value)
        texts = result.get("texts") or []
        if texts:
            lines.append("控件文本清单: %s" % " | ".join([str(t) for t in texts[:12]]))
        elements = result.get("elements") or []
        if elements and not texts:
            lines.append("控件清单（前%d）:" % len(elements))
            for e in elements[:20]:
                lines.append("  [%s] name=%s auto_id=%s" % (e.get("type"), e.get("name"), e.get("auto_id")))
        tree = (result.get("tree") or "").strip()
        if tree:
            lines.append("控件树:")
            lines.append(tree[:2500])
        wins = result.get("windows") or []
        if wins:
            lines.append("当前窗口（%d）:" % len(wins))
            for w in wins[:20]:
                lines.append("  %s | %s | pid=%s" % (w.get("title"), w.get("class"), w.get("pid")))
        return "\n".join(lines)

    msg = result.get("error") or result.get("output") or "未知错误"
    raw_code_hint = str(result.get("error") or "").split(":", 1)[0].strip()
    lines = ["Windows 原生 UI 自动化失败: %s" % msg]
    if act == "send_message":
        # 通讯失败要明确告诉 AI「已中止、没有发送」
        lines.append("已中止本次发送：未确认收件人之前，系统不会点击任何发送按钮。")
        lines.append("结论：消息【没有发出】，不得向主人报告“已发送”。")
        if raw_code_hint == "CONTACT_NOT_FOUND":
            lines.append("处置要求：立即停止本次任务，如实告诉主人「未找到该联系人，消息没有发出」。")
            lines.append("禁止改用搜索框、win_shell、按键模拟或其他任何迂回方式强行发送；"
                         "通讯软件只允许 send_message(receiver, message) 这一条受控路径。")
            lines.append("如主人确认好友名无误，请让主人在 QQ 中手动打开该会话一次后重试。")
    candidates = result.get("candidates") or []
    if candidates:
        lines.append("相近联系人候选（供用户确认，未自动选择）: %s" % " | ".join([str(c) for c in candidates[:8]]))
    available = result.get("available") or []
    if available:
        lines.append("可用控件（前20）:")
        for e in available[:20]:
            lines.append("  [%s] name=%s auto_id=%s" % (e.get("type"), e.get("name"), e.get("auto_id")))

    # 物理层没做成 → 必须抛错，让工具调用状态为「失败」。
    # 否则上层会把一段失败说明当成功，AI 就可能谎报“已发送”。
    raw_code = str(result.get("error") or "").split(":", 1)[0].strip()
    if not raw_code.replace("_", "").isalnum() or not raw_code.isupper():
        raw_code = "UI_AUTOMATION_FAILED"
    raise AUPError(raw_code, "\n".join(lines),
                   detail={"action": act, "app": app_name,
                           "candidates": candidates[:8]},
                   suggestion=("请先确认目标软件已登录并处于可用界面；"
                               "通讯发送必须由用户确认收件人后重试" if act == "send_message"
                               else "请先确认目标窗口存在且控件名正确"))
