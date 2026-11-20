# 飞书权限补齐清单（Feishu Permission Checklist）—— v2 对照截图 + 实测更新

> 生成方式：**源码反推**（dz.py / enforcer.py）对照飞书官方权限文档 + **人类后台截图 5 张（2026-10-23 23:09）** + **API 探测实测（feishu_probe.py，凭证已写入 .env）**。
> 应用状态：何春秋（用户 771947 的组织），**已启用、当前修改均已发布**，事件订阅方式已选「使用长连接接收事件」。

---

## 一、权限现状（截图核对）与补齐清单

### ✅ 已开通（无需再勾）

| 权限 | 说明 |
|---|---|
| `im:message` | 获取与发送单聊、群组消息（旧版基础） |
| `im:message.p2p_msg.readonly` | 读取用户发给机器人的单聊消息（**旧版点号**；新版为 `im:message.p2p_msg:readonly`） |
| `im:message:readonly` | 获取单聊、群组消息（旧版） |
| `im:message:send_as_bot` | **以应用的身份发消息**（发送单聊/群聊、防御中枢报警、签到通知的核心权限） |

### ❌ 待补齐（后台「权限管理 → 开通权限」勾选）

| # | 权限标识（精确勾选） | 为什么需要（源码依据） |
|---|---|---|
| 1 | `im:message.group_at_msg:readonly` | **接收群聊中 @ 机器人的消息**（dz.py 群聊分支 `chat_type=="group"`；飞书群聊必须@机器人） |
| 2 | `im:resource` | **获取消息中的图片/文件资源**（dz.py `handle_image_segments` OCR 图片、`handle_downloadable` 文件下载；bridge 已实现资源下载链路，缺此权限会失败） |
| 3 | `contact:contact.base:readonly` | **获取用户基本信息**（API 探测实测报错：`users/me` 需 `contact:contact.base:readonly`（或 access_as_app/readonly 系列任一）——**注意：不是我最初清单里的 `contact:user.base:readonly`，以实测报错为准**） |

> 说明：单聊接收已满足（旧版 `.readonly` 已开通，事件 `im.message.receive_v1` 接受旧版单聊权限）。若担心新旧兼容，可顺手补新式 `im:message.p2p_msg:readonly`（冒号版），非必需。

## 二、事件订阅（必须操作，当前缺失）

| # | 操作 | 位置 | 当前状态 |
|---|---|---|---|
| 1 | **添加事件 `im.message.receive_v1`（接收消息）** | 开发配置 → 事件与回调 → 事件配置 → 已添加事件 → 添加事件 | ⚠️ **暂无数据（未添加！）** |
| 2 | 订阅方式 = **使用长连接接收事件** | 同一页面 → 订阅方式 | ✅ 已选（推荐标签） |

> 不添加该事件，机器人永远收不到任何消息——这是当前**最优先的缺口**。

## 三、版本发布

✅ **当前修改均已发布**（截图 230914 确认）——补完权限/事件后，**再次创建版本并发布**即可生效（权限与事件的新增也需发布）。

## 四、API 探测实证结果（真实输出，2026-10-23 23:2x）

```
[token]    HTTP 200  code=0  → tenant_access_token 获取成功（凭证有效 ✓）
[users/me] HTTP 400  code=99991672
   msg: Access denied. One of the following scopes is required:
        [contact:contact.base:readonly, contact:contact:access_as_app,
         contact:contact:readonly, contact:contact:readonly_as_app]
   ⇒ 缺 contact 权限（对应上表 #3）
```

- 发送测试（im:message:send_as_bot）：**权限已开通**（截图确认）；需 open_id 才能发——users/me 被 contact 权限挡住，**请私聊机器人一条消息**，我即可从日志提取你的 open_id 完成发送实证（或补 contact 权限后自动获取）。

## 五、管理员 open_id（兼容占位已就绪）

- `FEISHU_ADMIN_OPEN_ID` 未配置时，防御中枢/签到推送会记录日志占位（不发送）；
- 私聊机器人后从日志提取，或直接告知，我写入 `.env`。

## 六、操作顺序建议

1. 后台补勾：`im:message.group_at_msg:readonly` + `im:resource` + `contact:contact.base:readonly`
2. 事件配置：添加 `im.message.receive_v1`（长连接已选，勿改）
3. **创建版本并发布**
4. 私聊机器人一句「你好」
5. 告诉我重启主程序（或我这边启动验证）
