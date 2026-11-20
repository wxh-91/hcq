# -*- coding: utf-8 -*-
"""WCU 深度优化预留位 —— 任务 2 的起点（本轮只落接口与设计注释，不实现）。

为什么需要这一层
--------------------------------------------------------------------
当前 UI 自动化底座是 pywinauto / Windows UI Automation（UIA）。
它对传统 Win32 / WPF / UWP 控件（计算器、记事本、资源管理器）支持很好，
但对 Electron 系与自绘 UI 支持一般：

  * QQNT（NT 版 QQ）、新版微信、钉钉、飞书客户端，界面本质是 Chromium 渲染的
    DOM，默认不实现完整的 UIA Provider；
  * 在 UIA 眼里往往只看到 ChromiumAccessibleWidget 这类"哑节点"，
    拿不到"好友列表项 / 会话标题 / 消息输入框 / 发送按钮"的语义名称；
  * 于是只能退化成坐标点击或全局快捷键 —— 这与"零信任、绝不点错人"直接冲突。

本轮的处理方式是**安全失败**：通讯软件只在"会话列表/联系人面板"里做名称完全相等
匹配（不使用全局搜索框——QQNT 搜索会弹出独立窗口，结果无法安全确认身份），
匹配不到就报 CONTACT_NOT_FOUND 并中止，绝不用坐标赌一把。

任务 2 的两条正路（在这里实现）
--------------------------------------------------------------------
1. deep_optimize_qq()
   QQNT 是 Electron 应用，理论上有两条可行通道：
     a) 启动参数注入：给 QQ.exe 追加 --remote-debugging-port=9222
        与 --force-renderer-accessibility，让 Electron 暴露 CDP 与可访问性树；
        之后用 HTTP 拿 /json 列表，websocket 连 CDP，
        Runtime.evaluate 直接读 DOM，定位会话项与输入框（比 UIA 稳定一个数量级）。
     b) 协议层注入：Electron 主进程与渲染进程之间走 IPC，
        通过 preload 注入脚本劫持发送函数（风险最高，需严格授权与版本适配）。
   收益：能精确到"第 N 个会话项"，可读未读数、可校验"当前对话对象是否正确"，
        满足零信任要求（发送前先断言 conversation.title == receiver）。
   前置：需要用户显式授权修改 QQ 启动参数；不同版本参数可能被校验，需版本矩阵。

2. deep_optimize_browser()
   本项目已有 interactive_browser（有头 Edge + F12 监听）。
   下一阶段目标是"浏览器书签/历史索引库"：
     a) 只读解析 Edge 的 Bookmarks（JSON）与 History（SQLite，需副本读取，避免锁库）；
     b) 建成结构化索引：标题 / URL / 访问频次 / 最近访问时间 / 域名分组；
     c) 供 AI 用自然语言检索（"上周看过的那个博客"），并与 URL 零信任校验器联动：
        凡是从索引库命中并带用户确认的 URL，才算"用户明确提供"。
   注意：只读、只索引、不上传；历史库读取必须先 copy 到临时目录再打开，
        避免与运行中的 Edge 争锁（Windows 文件锁是硬锁，会直接报 WinError 32）。

与零信任的关系
--------------------------------------------------------------------
深度优化只会增强"定位准确度"，不会放宽任何一条拦截规则：
  * 应用白名单仍在 guards.assert_app_allowed 生效；
  * 通讯发送仍必须 receiver + message，且发送前要断言会话对象；
  * 下载/未知点击、URL 校验逻辑不动。
"""


def deep_optimize_qq() -> dict:
    """【预留】QQNT 深度接管（CDP / 协议层）。当前未实现，调用返回状态字典。"""
    return {
        "implemented": False,
        "module": "deep_optimize_qq",
        "planned": [
            "Electron 启动参数注入（--remote-debugging-port / --force-renderer-accessibility）",
            "CDP Runtime.evaluate 读取会话列表与输入框",
            "发送前断言 conversation.title == receiver（零信任前置）",
            "版本矩阵与参数兼容性校验",
        ],
        "blocking": "需要用户显式授权修改 QQ 启动参数",
    }


def deep_optimize_browser() -> dict:
    """【预留】浏览器书签/历史索引库。当前未实现，调用返回状态字典。"""
    return {
        "implemented": False,
        "module": "deep_optimize_browser",
        "planned": [
            "只读解析 Edge Bookmarks(JSON) 与 History(SQLite 副本)",
            "结构化索引：标题/URL/频次/最近访问/域名分组",
            "自然语言检索接口（供 AI 调用）",
            "与 URL 零信任校验器联动（索引命中 + 用户确认才放行）",
        ],
        "blocking": "历史库需先复制再读，避免与运行中的 Edge 争锁",
    }
