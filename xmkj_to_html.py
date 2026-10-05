#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xmkj_to_html.py —— 项目框架.txt → 项目框架.html 半自动转换脚本
================================================================

用法（WSL 内，使用默认路径）：
    cd /root/robot
    python3 xmkj_to_html.py

或手动指定路径：
    python3 xmkj_to_html.py --input /path/to/项目框架.txt \
                            --output /path/to/项目框架.html

行为约定：
1. 纯确定性转换：只做结构识别与排版，不改写、不省略任何原文内容。
2. 绝不覆盖已有文件：默认输出 /mnt/d/项目/项目框架.html 若已存在
   （包括只读的“风格参考”同名文件），自动改存为 项目框架1.html、
   项目框架2.html ……（数字递增，取第一个不存在的名字）。
3. 输出为自包含 HTML（CSS 内嵌），无需联网即可打开。
4. txt 编码自动识别：UTF-8（含 BOM）→ UTF-8 → GB18030。
"""

import argparse
import datetime
import html
import os
import re
import sys

DEFAULT_INPUT = "/mnt/d/项目/项目框架.txt"
DEFAULT_OUTPUT = "/mnt/d/项目/项目框架.html"

CSS = """\
* { box-sizing: border-box; }
body {
    margin: 0;
    background: #f8fafc;
    color: #1e293b;
    font-family: "PingFang SC", "Microsoft YaHei", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    min-height: 100vh;
}
.container { max-width: 1024px; margin: 0 auto; padding: 2.5rem 1.25rem 3rem; }
header { margin-bottom: 2.5rem; border-bottom: 2px solid #6366f1; padding-bottom: 1rem; }
h1 { margin: 0; font-size: 1.875rem; font-weight: 700; color: #4338ca; }
.subtitle { color: #64748b; margin-top: .5rem; font-size: .9rem; }
section { margin-bottom: 2rem; }
h2 {
    font-size: 1.25rem; font-weight: 600; color: #4f46e5;
    margin: 0 0 .75rem; padding-left: .6rem; border-left: 4px solid #a5b4fc;
}
.section-note {
    color: #64748b; font-size: .9rem;
    margin: -.5rem 0 .75rem;
}
pre.tree-line {
    background: #1e293b; color: #e2e8f0;
    border-radius: 8px; padding: 1rem;
    overflow-x: auto;
    font-family: Consolas, Monaco, "Courier New", monospace;
    font-size: 13px; line-height: 1.6; white-space: pre; margin: 0;
}
.bullet-card {
    background: #fff; border-radius: 12px;
    box-shadow: 0 1px 3px rgba(15, 23, 42, .08);
    padding: 1.25rem 1.5rem;
}
.bullet-card ul { list-style: none; margin: 0; padding: 0; }
.bullet-card li {
    border-left: 3px solid #c7d2fe;
    padding: .35rem 0 .35rem .9rem;
    margin-bottom: .55rem;
}
.bullet-card li:last-child { margin-bottom: 0; }
.item-main { line-height: 1.75; }
.item-sub {
    color: #64748b; margin-top: .25rem;
    padding-left: 1rem; border-left: 1px dashed #e2e8f0; line-height: 1.7;
}
.plain p {
    background: #fff; border-radius: 8px;
    padding: 1rem; line-height: 1.75; margin: 0;
}
footer {
    margin-top: 3rem; padding-top: 1rem;
    border-top: 1px solid #e2e8f0;
    color: #94a3b8; font-size: .875rem;
}
"""


def read_text(path):
    """按 UTF-8(BOM) → UTF-8 → GB18030 顺序尝试读取，返回文本。"""
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError("无法识别的文件编码: %s" % path)


HEADER_RE = re.compile(r"^【[^】]+】")


def split_blocks(text):
    """按空行把全文切成若干非空行块；【章节标题】行始终独立成块。"""
    blocks, cur = [], []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if cur:
                blocks.append(cur)
                cur = []
            continue
        if HEADER_RE.match(stripped):
            if cur:
                blocks.append(cur)
                cur = []
            blocks.append([line])
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)
    return blocks


def has_tree_char(line):
    return any(ch in line for ch in "├└│┌┐")


def block_is_header(block):
    """单个【标题】（允许尾部带说明文字）行，视为章节标题。"""
    if len(block) == 1:
        return HEADER_RE.match(block[0].strip()) is not None
    return False


def block_is_tree(block):
    """目录树块：含树形字符，或「根路径 + 树形/续行」结构。"""
    if any(has_tree_char(line) for line in block):
        return True
    first = block[0].strip()
    if first.endswith("/") and len(block) > 1:
        return all(
            not line.strip()
            or has_tree_char(line)
            or line.strip().startswith("#")
            for line in block[1:]
        )
    return False


def block_is_bullet(block):
    """要点列表块：存在以「- 」开头的行。"""
    return any(line.startswith("- ") or line.startswith("-\t") for line in block)


def render_header(block):
    """【标题】（尾部可带说明）→ h2 + 可选说明行，保留全部原文内容。"""
    stripped = block[0].strip()
    m = HEADER_RE.match(stripped)
    title = m.group(0)[1:-1].strip()
    note = stripped[m.end():].strip()
    out = '<h2>%s</h2>' % html.escape(title)
    if note:
        out += '<p class="section-note">%s</p>' % html.escape(note)
    return '<section>%s</section>' % out


def render_tree(block):
    root = block[0].strip()
    body = "\n".join(html.escape(line) for line in block)
    return (
        '<section><h2>%s</h2>'
        '<pre class="tree-line">%s</pre></section>'
    ) % (html.escape(root), body)


def render_bullets(block):
    items = []
    cur = None
    for line in block:
        if line.startswith("- ") or line.startswith("-\t"):
            if cur is not None:
                items.append(cur)
            cur = {"main": line[2:].strip(), "sub": []}
        elif line[:1] in (" ", "\t") and cur is not None:
            cur["sub"].append(line.strip())
        else:
            # 理论上不会出现的散行，就近归入当前条目
            if cur is None:
                cur = {"main": line.strip(), "sub": []}
            else:
                cur["sub"].append(line.strip())
    if cur is not None:
        items.append(cur)

    lis = []
    for it in items:
        inner = '<div class="item-main">%s</div>' % html.escape(it["main"])
        if it["sub"]:
            sub = "".join(
                '<div class="item-sub">%s</div>' % html.escape(s) for s in it["sub"]
            )
            inner += sub
        lis.append("<li>%s</li>" % inner)
    return '<section><div class="bullet-card"><ul>%s</ul></div></section>' % "".join(lis)


def render_plain(block):
    body = "<br>".join(html.escape(line) for line in block)
    return '<section class="plain"><p>%s</p></section>' % body


def build_page(sections, timestamp):
    body = "\n".join(sections)
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>项目框架文档（自动生成）</title>
<style>
%s
</style>
</head>
<body>
<div class="container">
<header>
<h1>项目框架文档</h1>
<p class="subtitle">由 xmkj_to_html.py 自动生成 · %s</p>
</header>
%s
<footer>项目框架文档 · 自动生成于 %s</footer>
</div>
</body>
</html>
""" % (CSS, timestamp, body, timestamp)


def resolve_output(base):
    """若目标已存在，返回带数字后缀的第一个可用文件名，绝不覆盖已有文件。"""
    if not os.path.exists(base):
        return base
    stem, ext = os.path.splitext(base)
    n = 1
    while True:
        candidate = "%s%d%s" % (stem, n, ext)
        if not os.path.exists(candidate):
            return candidate
        n += 1


def main():
    parser = argparse.ArgumentParser(
        description="项目框架.txt → 项目框架.html 半自动转换脚本"
    )
    parser.add_argument("--input", default=DEFAULT_INPUT,
                        help="输入 txt 路径（默认 %s）" % DEFAULT_INPUT)
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="输出 html 路径（默认 %s）" % DEFAULT_OUTPUT)
    args = parser.parse_args()

    src = args.input
    if not os.path.isfile(src):
        print("[错误] 找不到输入文件: %s" % src, file=sys.stderr)
        print("请确认文件存在，或使用 --input 指定正确路径。", file=sys.stderr)
        sys.exit(1)

    text = read_text(src)
    blocks = split_blocks(text)

    sections = []
    for block in blocks:
        if block_is_header(block):
            sections.append(render_header(block))
        elif block_is_tree(block):
            sections.append(render_tree(block))
        elif block_is_bullet(block):
            sections.append(render_bullets(block))
        else:
            sections.append(render_plain(block))

    out = resolve_output(args.output)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    page = build_page(sections, timestamp)

    with open(out, "w", encoding="utf-8") as f:
        f.write(page)

    print("[OK] 输入: %s" % src)
    print("[OK] 输出: %s" % out)
    print("[OK] 生成完成，共 %d 个区块" % len(sections))


if __name__ == "__main__":
    main()
