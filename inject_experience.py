# -*- coding: utf-8 -*-
"""SOP 批量注入脚本（人类录入）。

用法：
    python inject_experience.py seed_sops.json
    python inject_experience.py seed_sops.json --db ${PROJECT_ROOT}/plugins/core_utils/experience.db

JSON 格式（数组，或 {"sops": [...]}）：
[
  {
    "trigger_text": "把 B 站视频链接下载并转成 MP3 音频",
    "trigger_keywords": ["B站", "下载", "MP3", "音频", "bv"],
    "tool_chain": ["download_audio_mp3"],
    "category": "macro",
    "action_steps": [
      {
        "tool": "download_audio_mp3",
        "args_brief": {"url": "B站 BV 号或视频链接", "output_name": "可选，输出文件名"},
        "note": "yt-dlp 抽取音频并转 MP3，落 downloads 目录，返回 MP3 路径"
      }
    ],
    "source": "human"
  }
]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import experience_bank as bank


def load_records(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        records = data.get("sops") or data.get("experiences") or []
    else:
        records = data
    if not isinstance(records, list):
        raise ValueError("JSON 顶层必须是数组或含 sops 数组的对象")
    return records


def validate(rec: dict) -> None:
    if not str(rec.get("trigger_text", "")).strip():
        raise ValueError("缺少 trigger_text")
    chain = rec.get("tool_chain") or []
    if not isinstance(chain, list) or not chain:
        raise ValueError("缺少 tool_chain（非空数组）")
    steps = rec.get("action_steps")
    if steps is None:
        rec["action_steps"] = [{"tool": t, "args_brief": {}, "note": ""} for t in chain]


def main() -> int:
    ap = argparse.ArgumentParser(description="批量注入人类 SOP 到经验库")
    ap.add_argument("json_path", help="SOP JSON 文件路径")
    ap.add_argument("--db", help="覆盖 experience.db 路径（可选）")
    args = ap.parse_args()

    if args.db:
        bank.DB_PATH = args.db

    records = load_records(args.json_path)
    for rec in records:
        validate(rec)
    ids = bank.inject_sops(records)

    print(f"已注入 {len(ids)} 条 SOP：{ids}")
    print("---- 当前库内 SOP ----")
    for s in bank.list_sops():
        print(f"#{s['id']} [{s['source']}/{s['category']}] {s['trigger_text']}")
        print(f"     工具链: {' -> '.join(s['tool_chain'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
