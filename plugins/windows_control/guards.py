# -*- coding: utf-8 -*-
"""零信任强制拦截器（Zero-Trust Interceptor）。

定位：WCU 的**唯一执行闸门**。三件工具在真正碰 Windows 之前，必须先过这里。
任何一条不通过都抛 AUPError，调用链立即终止，绝不"先做了再说"。

四条物理级防呆规则：
    1. 应用白名单   —— 不在 WIN_ALLOWED_APPS 内 → UNAUTHORIZED_APP
    2. 通讯软件强校验 —— QQ/微信 只允许 send_message(receiver, message)，
                        裸 click/type/keys 全禁，绝不允许盲点"发送"
    3. 落盘拦截     —— 用户没说"下载/安装/保存" → 拦截下载类命令、未知按钮点击
    4. URL 校验     —— 见 url_policy（主流放行，小众必须原话出现）

"用户是否说过" 一律通过 context.get_utterance() 判定；取不到原话即视为"没说"。
"""
import json
import logging
import re

from aup_core import AUPError

from . import context, policy

logger = logging.getLogger("plugins.windows_control.guards")

# ==================== 应用名归一 ====================
# 把 AI 可能写的各种叫法映射到白名单里的规范名；未收录的原样保留（多半会被拦）
APP_ALIASES = {
    "calc": "计算器", "calculator": "计算器", "计算器": "计算器",
    "calculatorapp": "计算器", "microsoftcalculator": "计算器", "windows计算器": "计算器",
    "notepad": "记事本", "记事本": "记事本",
    "wechat": "微信", "weixin": "微信", "微信": "微信",
    "qq": "qq", "qqnt": "qq", "tim": "qq",
    "msedge": "edge", "edge": "edge", "浏览器": "edge",
    "chrome": "chrome", "googlechrome": "chrome", "谷歌浏览器": "chrome",
    "mspaint": "画图", "paint": "画图", "画图": "画图",
    "taskmgr": "任务管理器", "任务管理器": "任务管理器",
    "explorer": "资源管理器", "资源管理器": "资源管理器",
    "cmd": "cmd", "命令提示符": "cmd", "powershell": "powershell",
}

# 通讯软件里允许的"无副作用"动作（不碰消息、不点按钮）
MESSENGER_SAFE_ACTIONS = ("launch", "close", "list_windows", "list_controls", "read", "wait")

# 动作别名：历史工具契约用过 open/start/run，一律归一到 launch（只影响"打开软件"，不涉发送）
ACTION_ALIASES = {
    # 打开/激活窗口
    "open": "launch", "start": "launch", "run": "launch",
    "open_app": "launch", "launch_app": "launch", "activate": "launch", "focus": "launch",
    "activate_window": "launch", "open_window": "launch", "show_window": "launch",
    "bring_to_front": "launch", "switch_window": "launch", "show": "launch",
    "focus_window": "launch", "activate_app": "launch",
    # 列控件
    "list": "list_controls", "inspect": "list_controls", "controls": "list_controls",
    "get_controls": "list_controls", "dump": "list_controls", "dump_controls": "list_controls",
    "get_tree": "list_controls", "dump_tree": "list_controls", "tree": "list_controls",
    "get_tree_text": "list_controls", "screenshot": "list_controls", "capture": "list_controls",
    # 输入
    "type_text": "type", "input": "type", "enter_text": "type",
    "send_keys": "keys", "press": "keys", "hotkey": "keys", "key": "keys",
    # 读取
    "get_text": "read", "text": "read", "get_value": "read", "value": "read",
    # 关闭 / 等待
    "quit": "close", "exit": "close", "shutdown": "close", "sleep": "wait",
    # 通讯
    "send": "send_message", "message": "send_message", "send_msg": "send_message",
}


def normalize_action(action: str) -> str:
    a = (action or "").strip().lower()
    return ACTION_ALIASES.get(a, a)


def normalize_app(name: str) -> str:
    n = (name or "").strip().lower()
    if n.endswith(".exe"):
        n = n[:-4]
    n = n.replace(" ", "").replace("\u3000", "")
    return APP_ALIASES.get(n, n)


def _allowed_keys() -> set:
    return {normalize_app(a) for a in policy.allowed_apps()}


def _messenger_keys() -> set:
    return {normalize_app(a) for a in policy.messenger_apps()}


def is_messenger(app_name: str) -> bool:
    return normalize_app(app_name) in _messenger_keys()


def utterance_has(keywords) -> bool:
    """用户本轮原话是否出现任一关键词（取不到原话即 False）。"""
    u = (context.get_utterance() or "").lower()
    if not u:
        return False
    return any(str(k).lower() in u for k in keywords if str(k).strip())


# ==================== 规则 1：应用白名单 ====================
def assert_app_allowed(app_name: str) -> str:
    """校验应用名并返回规范名；不在白名单直接拦。"""
    if not policy.zero_trust_mode():
        return normalize_app(app_name)
    key = normalize_app(app_name)
    if not key:
        raise AUPError("MISSING_APP", "必须提供 app_name（要操作哪个软件/窗口）")
    allowed = _allowed_keys()
    if key not in allowed:
        raise AUPError(
            "UNAUTHORIZED_APP",
            "应用「%s」不在 WIN_ALLOWED_APPS 白名单内，已拦截。当前允许：%s。"
            "AI 无权自行扩权：如确认需要，请调用 submit_system_proposal 向主人呈递"
            "《系统修改提案》，由主人在 config_center/wcu_policy.json 手工增加后重启生效。"
            % (app_name, ", ".join(sorted(allowed))),
        )
    return key


# ==================== 规则 2：通讯软件强校验 ====================
def assert_messenger_action(app_name: str, action: str,
                            receiver: str = "", message: str = "",
                            control_name: str = "") -> None:
    """QQ/微信：只放行只读动作与 send_message(receiver, message)。"""
    if not is_messenger(app_name):
        return
    if not policy.zero_trust_mode():
        return
    act = normalize_action(action)
    if act in MESSENGER_SAFE_ACTIONS:
        return
    if act == "send_message":
        if not (receiver or "").strip():
            raise AUPError(
                "MISSING_RECEIVER",
                "通讯软件必须提供 receiver（明确到具体好友/群）。不指定收件人的消息一律不发送。",
            )
        if not (message or "").strip():
            raise AUPError("MISSING_MESSAGE", "通讯软件必须提供 message（要发送的内容）。")
        if "发送" in (control_name or ""):
            raise AUPError(
                "BLIND_SEND_FORBIDDEN",
                "禁止直接点击「发送」控件。发送必须走 action=send_message，由系统先精确匹配联系人再发送。",
            )
        return
    raise AUPError(
        "MESSENGER_ACTION_FORBIDDEN",
        "QQ/微信 不允许执行 action=%s。发送消息必须使用 action=send_message 并显式给出 receiver 与 message；"
        "系统禁止对通讯软件做裸点击/裸注入，防止点错人。"
        "允许的动作：launch（打开软件，可用别名 open）/ close / list_windows / list_controls / read / wait / send_message。"
        % (action or "(空)"),
    )


# ==================== 规则 3：落盘 / 未知按钮 ====================
_SHELL_DOWNLOAD_RE = re.compile(
    r"(invoke-webrequest|\biwr\b|start-bitstransfer|bitsadmin|\bcurl(\.exe)?\b|\bwget(\.exe)?\b|"
    r"msiexec|winget\s+(install|add)|choco\s+install|scoop\s+install|pip\s+install|"
    r"npm\s+(install|i)\b|\bdownloadfile\b|certutil\s+-urlcache|expand-archive)",
    re.I,
)

# 通讯软件命令行通道（QQ/微信）——整条封死
_MESSENGER_SHELL_RE = re.compile(
    r"(qq\.exe|qqnt|\\tencent\\qq|微信\.exe|wechat\.exe|weixin\.exe)", re.I)



# ==================== 规则 5：命令行启动拦截（UNAUTHORIZED_STARTUP） ====================
# 白名单原先只管「能否操控」，本规则补上「能否拉起进程」——
# win_shell 的 Start-Process / start / explorer.exe 是真实存在的旁路。
# 判定：解析出启动目标 → 白名单内放行；白名单外且用户原话未点名 → 拦截。
_LAUNCH_HEADS = (
    re.compile(r"\bStart-Process\b", re.I),
    re.compile(r"\bsaps\b", re.I),                       # Start-Process 的 PowerShell 别名
    re.compile(r"\bstart\b(?![\w-])", re.I),             # cmd 内建 start（可出现在 cmd /c 之后）
    re.compile(r"\bexplorer\.exe\b", re.I),
    re.compile(r"\bInvoke-Item\b", re.I),
    re.compile(r"\bProcess\]::Start\s*\(", re.I),
    re.compile(r"\.(?:Run|Exec|ShellExecute)\s*\(", re.I),  # WScript.Shell / Shell.Application 的 COM 启动
    re.compile(r"\bwmic\b[^\r\n;|]*\bprocess\b[^\r\n;|]*\bcreate\b", re.I),
    re.compile(r"\bschtasks(?:\.exe)?\b[^\r\n;|]*?\s/create\b", re.I),  # 计划任务延迟拉起
)

# 只有这些扩展名算"可执行目标"；start.txt 之类不当作启动目标，避免误伤
_EXEC_EXTS = (".exe", ".bat", ".cmd", ".com", ".lnk", ".msc", ".ps1", ".vbs", ".js", ".msi")
_TOKEN_RE = re.compile(r'"([^"]*)"|\'([^\']*)\'|([^\s]+)')

# shell 宿主本身：win_shell 已是受控执行环境，不对它再做启动拦截
_SHELL_HOSTS = {"cmd", "powershell", "pwsh", "bash", "sh", "conhost", "wsl"}


def _tokens(s: str):
    return [a or b or c for a, b, c in _TOKEN_RE.findall(s or "")]


def _looks_like_program(t: str) -> bool:
    """粗判 token 是否是"程序目标"，排除 start.txt 这类普通文件名。"""
    low = (t or "").strip().strip("\"'").lower()
    if not low:
        return False
    if low.endswith(_EXEC_EXTS):
        return True
    # 裸程序名（start mspaint / wmic create mspaint）不含点、不含路径
    return "." not in low


def _first_target(rest: str) -> str:
    """从启动命令尾串里取第一个"非选项"token 作为目标。"""
    for t in _tokens(rest):
        t = t.strip().strip("\"'")
        if not t or t.startswith(("-", "/", "$", "(")):
            continue
        if not _looks_like_program(t):
            continue
        return t
    return ""


def _encoded_payloads(command: str):
    """解析 powershell -EncodedCommand / -enc 里的 base64 载荷（拦截 base64 绕行）。"""
    import base64

    outs = []
    for m in re.finditer(r"-(?:encodedcommand|enc|e)\s+([A-Za-z0-9+/=]{8,})", command or "", re.I):
        blob = m.group(1)
        for enc in ("utf-16-le", "utf-8"):
            try:
                text = base64.b64decode(blob).decode(enc, "ignore")
            except Exception:
                continue
            if text and text.isprintable():
                outs.append(text)
                break
    return outs


def startup_targets(command: str):
    """解析命令里所有启动目标（原样 token）。"""
    cmd = command or ""
    targets = []
    for head in _LAUNCH_HEADS:
        for m in head.finditer(cmd):
            rest = re.split(r"[;|&\n]", cmd[m.end():], maxsplit=1)[0]
            if "schtasks" in m.group(0).lower():
                # 计划任务的真实目标在 /tr 参数里
                tr = re.search(r"/tr\s+(\"([^\"]+)\"|'([^']+)'|([^\s]+))", rest, re.I)
                if tr:
                    targets.append(tr.group(2) or tr.group(3) or tr.group(4) or "")
                continue
            tgt = _first_target(rest)
            if tgt:
                targets.append(tgt)
    targets.extend(
        m.group(1)
        for m in re.finditer(
            r"(?:^|[;&|]\s*|&\s*)[\"']?((?:[^\"'\s;|&]*[\\/])?[^\"'\s;|&]+\.exe)", cmd, re.I
        )
    )
    for payload in _encoded_payloads(cmd):
        targets.extend(startup_targets(payload))
    # 去重（同一目标可能被多条正则命中）
    seen, uniq = set(), []
    for t in targets:
        k = _basename(t).lower()
        if t and k not in seen:
            seen.add(k)
            uniq.append(t)
    return uniq


def _basename(target: str) -> str:
    name = (target or "").strip().strip("\"'")
    name = re.split(r"[\\/]", name)[-1]
    return name.strip()


def _utterance_mentions(target: str) -> bool:
    """用户本轮原话是否点名了这个程序（含中文别名与去 .exe 写法）。"""
    u = (context.get_utterance() or "").lower()
    if not u:
        return False
    base = _basename(target).lower()
    stem = base[:-4] if base.endswith(".exe") else base
    keys = {base, stem}
    canon = normalize_app(stem)
    keys.add(canon)
    return any(k and k in u for k in keys)


def assert_startup_allowed(command: str) -> None:
    """命令行拉起进程的白名单校验（只放行白名单内应用或用户点名过的程序）。"""
    if not policy.zero_trust_mode():
        return
    targets = startup_targets(command)
    for t in targets:
        base = _basename(t)
        if not base:
            continue
        stem = base[:-4] if base.lower().endswith(".exe") else base
        if stem.lower() in _SHELL_HOSTS:
            continue
        key = normalize_app(stem)
        allowed = _allowed_keys()
        if key in allowed:
            continue
        if _utterance_mentions(t):
            logger.info("[UNAUTHORIZED_STARTUP] 用户原话点名「%s」，放行启动", base)
            continue
        raise AUPError(
            "UNAUTHORIZED_STARTUP",
            "命令行启动「%s」已被拦截：它不在 allowed_apps 白名单内，且你本轮原话没有点名要求启动它。"
            "白名单只允许启动：%s。\n"
            "AI 无权自行拉起进程：如确需，请调用 submit_system_proposal 呈递《系统修改提案》，"
            "申请把该程序加入 config_center/wcu_policy.json 的 allowed_apps，由主人裁决后重启生效。"
            % (base, ", ".join(sorted(allowed))),
        )


def assert_shell_safe(command: str) -> None:
    """命令里出现下载/安装动作，但用户没说"下载/安装" → 拦截。"""
    if not policy.zero_trust_mode():
        return
    # 通讯软件：命令行这条通道整体封死，只能走 send_message 受控入口
    if _MESSENGER_SHELL_RE.search(command or ""):
        raise AUPError(
            "MESSENGER_SHELL_FORBIDDEN",
            "禁止用 win_shell 启动或操控 QQ/微信（命中：%s）。通讯软件只允许 "
            "win_ui_automation 的 action=send_message(receiver, message) 受控入口；"
            "若该入口返回 CONTACT_NOT_FOUND，必须立即停止并如实告知主人「未发送」，"
            "不得换命令行或按键模拟绕行。" % _MESSENGER_SHELL_RE.search(command).group(0),
        )
    if _SHELL_DOWNLOAD_RE.search(command or "") and not utterance_has(policy.download_keywords()):
        raise AUPError(
            "UNAUTHORIZED_DOWNLOAD",
            "命令包含下载/安装动作，但用户本轮没有明确要求下载或安装，已拦截。"
            "请先让用户明确说明要下载/安装什么。",
        )
    # 规则 5：命令行启动 — 白名单外的程序一律拦（用户点名过的除外）
    assert_startup_allowed(command)


def assert_click_safe(control_name: str, action: str) -> None:
    """点击必须指名控件；命中高风险词且用户没授权 → 拦截。"""
    if not policy.zero_trust_mode():
        return
    if (action or "").strip().lower() != "click":
        return
    cname = (control_name or "").strip()
    if not cname:
        raise AUPError(
            "BLIND_CLICK",
            "点击动作必须提供明确的 control_name，禁止盲点未知按钮。请先 action=list_controls 确认控件名。",
        )
    risk = [w for w in policy.click_keywords() if w and w in cname.lower()]
    if risk and not utterance_has(risk):
        raise AUPError(
            "UNAUTHORIZED_CLICK",
            "控件「%s」属于高风险操作（命中：%s），但用户本轮没有明确要求，已拦截。"
            "如确需点击，请让用户明确说出要做的动作。" % (cname, ", ".join(risk)),
        )


def assert_browser_actions(actions: str) -> None:
    """浏览器动作列表里出现下载/高风险点击，同样要用户授权。"""
    if not policy.zero_trust_mode() or not (actions or "").strip():
        return
    try:
        acts = json.loads(actions)
        if isinstance(acts, dict):
            acts = [acts]
        if not isinstance(acts, list):
            acts = []
    except Exception:
        raise AUPError("BAD_ACTIONS", "actions 必须是 JSON 数组字符串，例如 [{'type':'wait','s':3}]")
    for item in acts:
        if not isinstance(item, dict):
            continue
        t = str(item.get("type") or "").strip().lower()
        if t in ("download", "save", "savenetwork", "download_file"):
            if not utterance_has(policy.download_keywords()):
                raise AUPError(
                    "UNAUTHORIZED_DOWNLOAD",
                    "浏览动作包含下载（%s），但用户本轮没有明确要求下载，已拦截。" % t,
                )
        if t == "click":
            blob = "%s %s" % (item.get("selector") or "", item.get("text") or "")
            risk = [w for w in policy.download_keywords() if w and w in blob.lower()]
            if risk and not utterance_has(risk):
                raise AUPError(
                    "UNAUTHORIZED_CLICK",
                    "点击目标「%s」疑似下载/安装入口，但用户本轮没有明确要求，已拦截。" % blob.strip()[:80],
                )
