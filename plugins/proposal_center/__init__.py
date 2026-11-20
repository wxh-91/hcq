# -*- coding: utf-8 -*-
"""Proposal Center —— 沙盒「系统修改提案」通道（Plugin 9.0）。

配置保护设计原则：
  - 朝堂执行系统：生产工具一律守闸，该拦就拦，绝不放水。
  - 沙盒系统：AI 发现「合规限制严重阻碍任务」时，只能把改造方案写成议案呈递给人类，
    严禁自行修改任何配置、代码或数据。议案是否落地，由人类裁决。

对外 AUP 宏工具：
  submit_system_proposal(proposal_type, description, patch_content)

副作用边界（硬约束）：
  - 唯一落盘位置：/root/robot/red_team_sandbox/（议案 JSON + 补丁文本 + 台账），仅供审计
  - 唯一对外动作：把《系统修改提案》文本投递到通知通道（飞书在线则飞书私聊；飞书退场则落本地 outbox 弹窗）
  - 绝不写 settings.py / wcu_policy.json / 任何生产代码或配置
"""
from __future__ import annotations
import os
_ROOT = os.environ.get("PROJECT_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import json
import logging
import os
import re
import time

import aiohttp

from aup_core import AUPError, aup_tool
from plugins.core_utils.notify_bridge import feishu_enabled, notify


logger = logging.getLogger("plugins.proposal_center")

SANDBOX = _ROOT + "/red_team_sandbox"
PROPOSAL_DIR = SANDBOX + "/proposals"
PATCH_DIR = SANDBOX + "/patches"
LEDGER = SANDBOX + "/proposals_ledger.jsonl"
FEISHU_BASE = "https://open.feishu.cn/open-apis"

PROPOSAL_TYPES = {
    "whitelist_add": "应用/域名白名单扩容",
    "ui_path": "UI 自动化路径改造",
    "browser_policy": "浏览器/网址策略调整",
    "tool_gap": "工具能力缺口",
    "config_change": "配置项调整",
    "other": "其他",
}

MAX_DESC = 1500
MAX_PATCH = 2200


def _ensure_dirs() -> None:
    os.makedirs(PROPOSAL_DIR, exist_ok=True)
    os.makedirs(PATCH_DIR, exist_ok=True)


def _slug(text: str, limit: int = 24) -> str:
    keep = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5]+", "-", text or "").strip("-")
    return (keep[:limit] or "proposal")


async def _token() -> str:
    from plugins.core_utils import feishu_bridge

    return await feishu_bridge.get_tenant_token()


def _admin() -> str:
    from plugins.core_utils import feishu_bridge

    return feishu_bridge.admin_open_id()


async def _send_text(text: str) -> tuple:
    """投递议案文本。飞书在线走飞书私聊；飞书退场时落本地 outbox（notify_bridge）。返回 (ok, info)。"""
    if not feishu_enabled():
        res = await notify(text, title="系统修改提案", level="proposal")
        return True, "本地 outbox: " + (res.get("path") or "已落盘")
    token = await _token()
    admin = _admin()
    if not token or not admin:
        return False, "无法获取 token 或管理员 open_id"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                FEISHU_BASE + "/im/v1/messages?receive_id_type=open_id",
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json; charset=utf-8",
                },
                json={
                    "receive_id": admin,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                obj = await r.json()
        if obj.get("code") == 0:
            mid = (obj.get("data") or {}).get("message_id", "")
            return True, "message_id=%s" % mid
        return False, "%s %s" % (obj.get("code"), obj.get("msg"))
    except Exception as e:  # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, str(e)[:120])


def _render(pid: str, ptype: str, label: str, desc: str, patch: str, ts: str) -> str:
    body = [
        "【系统修改提案 · 待裁决】",
        "编号：%s" % pid,
        "类型：%s（%s）" % (label, ptype),
        "呈递时间：%s" % ts,
        "",
        "一、阻塞事实",
        desc,
        "",
        "二、建议改造方案",
        patch or "（本次未附补丁，仅说明诉求）",
        "",
        "三、裁决状态",
        "未生效。AI 无权修改生产配置，以上内容仅为议案；",
        "如认可，请主人在 /root/robot/config_center/wcu_policy.json 或对应代码手工评审落地。",
    ]
    return "\n".join(body)


@aup_tool(
    name="submit_system_proposal",
    description=(
        "[宏·沙盒申请通道] 把「合规限制阻碍任务」写成《系统修改提案》呈递给主人。"
        "当你被白名单（UNAUTHORIZED_APP）、URL 校验、下载/点击拦截挡住，"
        "或发现某条 UI 路径严重走不通时调用它：说明卡在哪、为什么、建议怎么改、风险是什么。"
        "本工具不修改任何文件与配置，只把议案投递到通知通道（飞书在线则飞书，退场则本地弹窗）等待主人裁决。"
        "被拦后不要放弃、也不要换 win_shell/按键模拟等旁路强推。"
    ),
    category="governance",
    is_danger=False,
    params_schema={
        "required": ["proposal_type", "description"],
        "properties": {
            "proposal_type": {
                "type": "string",
                "enum": list(PROPOSAL_TYPES.keys()),
                "description": "议案类型：whitelist_add(白名单扩容) / ui_path(UI路径改造) / "
                               "browser_policy(网址策略) / tool_gap(工具缺口) / config_change(配置调整) / other",
            },
            "description": {
                "type": "string",
                "description": "阻塞事实与理由：要做什么、被哪条规则挡住、卡在哪一步、为什么需要放权。",
            },
            "patch_content": {
                "type": "string",
                "description": "建议的补丁或 JSON 片段（仅作文案呈递，不会被执行）。可留空。",
            },
        },
    },
    suggestion_hint="若通知通道投递失败，如实说明失败原因，不要谎称已呈递",
    version="1.0.0",
)
async def submit_system_proposal(
    proposal_type: str, description: str, patch_content: str = ""
) -> str:
    ptype = (proposal_type or "other").strip()
    if ptype not in PROPOSAL_TYPES:
        ptype = "other"
    desc = (description or "").strip()
    patch = (patch_content or "").strip()
    if len(desc) < 6:
        raise AUPError("PROPOSAL_TOO_THIN", "议案缺少阻塞事实：请说明要做什么、被哪条规则挡住、卡在哪一步。")

    _ensure_dirs()
    now = time.localtime()
    ts = time.strftime("%Y-%m-%d %H:%M:%S", now)
    pid = "P-%s-%s" % (time.strftime("%Y%m%d-%H%M%S", now), _slug(desc, 8))

    record = {
        "id": pid,
        "type": ptype,
        "type_label": PROPOSAL_TYPES[ptype],
        "description": desc,
        "patch_content": patch,
        "created_at": ts,
        "status": "pending_human",
        "applied": False,
        "channel": "feishu" if feishu_enabled() else "local",
        "principle": "沙盒只提建议，落地必须由人类裁决",
    }

    with open(PROPOSAL_DIR + "/%s.json" % pid, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    if patch:
        with open(PATCH_DIR + "/%s.patch.txt" % pid, "w", encoding="utf-8") as f:
            f.write(patch)

    text = _render(pid, ptype, PROPOSAL_TYPES[ptype], desc[:MAX_DESC], patch[:MAX_PATCH], ts)
    ok, info = await _send_text(text)

    record["delivery"] = {"ok": ok, "info": info}
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    if not ok:
        logger.error("[proposal] %s 通知投递失败: %s", pid, info)
        raise AUPError("PROPOSAL_DELIVERY_FAILED", "《系统修改提案》%s 已存档但通知投递失败：%s" % (pid, info))

    logger.info("[proposal] %s 已呈递，%s", pid, info)
    return (
        "已呈递《系统修改提案》%s（类型：%s）。通知送达：%s。\n"
        "本次只提交议案，没有修改任何文件或配置；是否放权由主人裁决。"
        % (pid, PROPOSAL_TYPES[ptype], info)
    )


# ==================== 注册入库（tool_center.db） ====================
_PARAMS = {
    "type": "object",
    "required": ["proposal_type", "description"],
    "properties": {
        "proposal_type": {
            "type": "string",
            "enum": list(PROPOSAL_TYPES.keys()),
            "description": "议案类型",
        },
        "description": {"type": "string", "description": "阻塞事实与理由"},
        "patch_content": {"type": "string", "description": "建议的补丁或 JSON 片段（不会被执行）"},
    },
}

try:
    from tool_center.registry_manager import register_tool as _register_tool

    _register_tool(
        "submit_system_proposal",
        submit_system_proposal,
        description=(
            "[宏·沙盒申请通道] 把「合规限制阻碍任务」写成《系统修改提案》呈递给主人。"
            "被白名单、URL 校验、下载/点击拦截挡住，或某条 UI 路径走不通时调用："
            "说明卡在哪、为什么、建议怎么改、风险是什么。工具不改任何文件，只发飞书议案。"
        ),
        category="governance",
        schema_json=json.dumps({"type": "function", "function": {
            "name": "submit_system_proposal",
            "description": "向主人呈递《系统修改提案》（只提建议，不改任何文件）",
            "parameters": _PARAMS,
        }}, ensure_ascii=False),
        is_danger=False,
        is_active=True,
        need_confirm=False,
        params_desc=_PARAMS,
        module_path="plugins.proposal_center",
        func_name="submit_system_proposal",
    )
    logger.info("[proposal] submit_system_proposal 已注册入库")
except Exception as _reg_err:  # SQLite 不可用也不挡加载（AUP 内存注册已生效）
    logger.warning("[proposal] 注册入库失败: %s", _reg_err)

