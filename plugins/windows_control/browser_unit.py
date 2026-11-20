# -*- coding: utf-8 -*-
"""interactive_browser 实现本体（从 ai.py 迁入 WCU）。

与旧版的差别：入口处先过 URL 零信任校验器，动作列表里出现下载/高风险点击也要过闸。
"""
import json
import logging
import subprocess

from aup_core import AUPError

from . import guards, url_policy, winenv

logger = logging.getLogger("plugins.windows_control.browser")


def _normalize_actions(actions) -> str:
    """把 actions 归一成 JSON 数组字符串；无法解析时返回 "[]"（用脚本默认动作）。"""
    if actions is None:
        return "[]"
    if isinstance(actions, (list, dict)):
        return json.dumps(actions, ensure_ascii=False)
    raw = str(actions).strip()
    if not raw:
        return "[]"
    try:
        parsed = json.loads(raw)
    except Exception:
        logger.warning("[interactive_browser] actions 不是 JSON，按默认动作执行: %s", raw[:60])
        return "[]"
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return "[]"
    return json.dumps(parsed, ensure_ascii=False)


async def run_browser(url: str = "", actions: str = "") -> str:
    if not url:
        raise AUPError("MISSING_URL", "interactive_browser 必须提供目标网页 URL")

    # 规则 4：URL 零信任校验（主流放行 / 小众必须在用户原话里出现）
    url_policy.check_url(url)
    # actions 归一：模型偶尔会传自然语言或数组，能解析就转成 JSON 字符串，
    # 解析不出就当作"未指定动作"（走脚本默认 goto/wait/screenshot/get_text），
    # 不能因此让整次开页失败。
    actions_arg = _normalize_actions(actions)
    # 规则 3：动作列表里的下载/高风险点击
    guards.assert_browser_actions(actions_arg)

    logger.info("[interactive_browser] 启动 url=%s actions=%s", url, actions_arg[:120])
    try:
        proc = subprocess.run(
            [winenv.WIN_PY, winenv.WIN_BROWSER_SCRIPT, url, actions_arg],
            capture_output=True, text=True, timeout=320,
        )
    except Exception as exc:
        logger.error("[interactive_browser] 执行异常: %s", exc)
        return "有头浏览器执行失败: %s" % str(exc)[:200]

    out = (proc.stdout or "").strip()
    if not out:
        return "有头浏览器无输出（rc=%s）：%s" % (proc.returncode, (proc.stderr or "")[:200])
    try:
        result = json.loads(out.splitlines()[-1])
    except Exception:
        result = {"status": "raw", "output": out[-500:]}

    if result.get("status") != "ok":
        return "有头浏览器状态: %s | %s" % (
            result.get("status", "unknown"), result.get("message", out[-300:]))

    lines = [
        "页面: %s | URL: %s" % (result.get("title", ""), result.get("url", url)),
        "截图: %s" % (result.get("screenshot") or result.get("screenshot_path") or ""),
        "动作: %s" % (" → ".join(result.get("actions_done", []))),
    ]
    dom = ((result.get("dom_text") or result.get("page_content") or "") or "").strip()
    if dom:
        lines.append("页面内容（节选）:")
        lines.append(dom[:3000])

    dev = result.get("devtools_report") or {}
    net = dev.get("network") or []
    if net:
        lines.append("【F12 Network 接口监听（%d 条）】" % len(net))
        for e in net[:10]:
            extra = ""
            if e.get("post_data"):
                extra += " post=%s" % (e["post_data"] or "")[:80]
            if e.get("response_json"):
                extra += " resp=%s" % (e["response_json"] or "")[:80]
            if e.get("request_headers"):
                keys = ",".join(list(e["request_headers"].keys())[:4])
                if keys:
                    extra += " hdr=%s" % keys
            lines.append("  [%s %s] %s%s" % (e.get("method") or "GET", e.get("status"),
                                             (e.get("url") or "")[:150], extra))
    console = dev.get("console") or []
    if console:
        lines.append("【F12 Console 监听（%d 条）】" % len(console))
        for c in console[:5]:
            lines.append("  [%s] %s" % (c.get("type", "log"), (c.get("text") or "")[:200]))
    storage = dev.get("storage") or {}
    cookies = storage.get("cookies") or []
    local = storage.get("localStorage") or {}
    if cookies or local:
        lines.append("【F12 Storage 采集】")
        if cookies:
            lines.append("  cookies(%d): %s" % (len(cookies),
                                                ", ".join((c.get("name") or "?") for c in cookies[:10])))
        if local:
            lines.append("  localStorage(%d): %s" % (len(local), ", ".join(list(local.keys())[:10])))
    if result.get("message"):
        lines.append(result["message"])
    return "\n".join(lines)
