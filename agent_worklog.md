# agent_worklog — 服务器 GitHub 访问优化记录

- 日期：2026-10-06（完成收尾）
- 服务器环境：WSL Ubuntu（本机），项目路径 /root/robot，远端 https://github.com/WXH-91/hcq.git
- 目标：改善服务器到 GitHub 的连接稳定性
- 约束：仅修改 `~/.gitconfig` 与 `~/.ssh/config`；不修改项目内 .py、.env、models.json（实际未改动任何项目文件）

---

## 一、诊断结果

| 检查项 | 命令 | 结果 |
|---|---|---|
| ICMP 连通 | `ping -c 3 github.com` | 0% 丢包；RTT min/avg/max = 76.682 / 77.700 / 79.220 ms |
| HTTPS 连通 | `curl -I -m 10 https://github.com` | HTTP/2 200，1 秒内返回，未超时 |
| DNS 配置 | `cat /etc/resolv.conf` | 仅一个 nameserver：`8.8.8.8` |
| DNS 稳定性 | 连续 5 次 `getent hosts github.com` | 5/5 成功，稳定解析到 20.205.243.166 |
| Git HTTPS 基线 | `git ls-remote origin HEAD` | 成功，HEAD = 872e9f4eba4f491e9c6a151fa4295078528f6583 |
| SSH 端口 22 | `ssh -T git@github.com`（测试时） | TCP 可达（返回 Permission denied，说明已连上仅缺密钥） |
| 代理环境变量 | `env \| grep -i proxy` | 无 |

**结论**：诊断快照时刻全链路正常（丢包 0、HTTPS 200、DNS 稳定），问题属于**间歇性**不稳定。常见诱因：境内到 GitHub 的路由抖动、8.8.8.8 在境内偶发丢包/污染。因此按方案 A（SSH）+ 方案 C（HTTP 超时兜底）组合加固。

---

## 二、方案尝试记录

### 方案 A：配置 Git SSH 连接（优先）✅ 已完成

1. 生成 Ed25519 SSH 密钥（无口令，注释 wxh-server@github）
   - 命令：`ssh-keygen -t ed25519 -N '' -C 'wxh-server@github' -f /root/.ssh/id_ed25519`
   - 结果：成功生成 `~/.ssh/id_ed25519`（私钥）与 `~/.ssh/id_ed25519.pub`（公钥）
2. 写入 `~/.ssh/config`
   - 内容：Host github.com / HostName github.com / User git / IdentityFile ~/.ssh/id_ed25519 / IdentitiesOnly yes / ServerAliveInterval 60（保活，缓解间歇断连）
   - 权限：.ssh 700、config 600
3. 公钥交付用户（WXH-91），用户手动添加到 GitHub（Settings → SSH and GPG keys → New SSH key，Authentication Key）
4. 收尾验证（用户确认添加后执行）：

```
$ ssh -T git@github.com
Hi wxh-91! You've successfully authenticated, but GitHub does not provide shell access.

$ git ls-remote git@github.com:WXH-91/hcq.git HEAD
872e9f4eba4f491e9c6a151fa4295078528f6583    HEAD
```

5. 在 `~/.gitconfig` 追加 URL 改写规则，使项目 HTTPS 远端自动走 SSH：

```ini
[url "git@github.com:WXH-91/"]
    insteadOf = https://github.com/WXH-91/
```

6. 生效验证：`GIT_TRACE=1 git -C /root/robot ls-remote origin HEAD` 显示实际调用 `ssh git@github.com 'git-upload-pack WXH-91/hcq.git'`；`git -C /root/robot ls-remote origin HEAD` 正常返回 HEAD 872e9f4。

**公钥（已添加）**：
```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIME+TGPl4kN8yrxrXeju/mymD4K1Jj/fPyEUAL+4lZoR wxh-server@github
```

### 方案 B：Git 配置代理（备用）— 跳过

- 询问用户是否有可用 HTTP/SOCKS5 代理 → 回复**无可用代理** → 跳过，不配置任何代理。

### 方案 C：调整 Git HTTP 超时参数（兜底）✅ 核查完成

- 检查现有 `~/.gitconfig` 的 http 段：
  - `http.postBuffer = 524288000`（500MB，上传缓冲区充足）
  - `http.lowSpeedLimit = 0`（已禁用低速率判定）
  - `http.lowSpeedTime = 999999`（低速率持续时间阈值≈无限）
- 结论：低速率自动断开（默认 1000 字节/s 持续 30s 即中止）已被禁用，等效于"永不因慢速超时中断"，方案 C 实际已处于最宽松配置，无需再改。

---

## 三、变更清单（最终）

| 文件 | 操作 |
|---|---|
| `~/.ssh/id_ed25519` / `~/.ssh/id_ed25519.pub` | 新建（方案 A 要求生成密钥） |
| `~/.ssh/config` | 新建，配置 github.com 主机与保活参数 |
| `~/.gitconfig` | 新增 `[url "git@github.com:WXH-91/"] insteadOf = https://github.com/WXH-91/`（本次唯一改动） |
| `/etc/resolv.conf` | 未修改（超出允许范围，仅建议，见下） |
| `/root/robot/*` | 未改动任何项目文件 |

---

## 四、结论与后续建议

1. **最终采用方案：方案 A（SSH）**——SSH 认证通过、`git ls-remote` 正常、项目已无缝切换 SSH 拉取；SSH 走 22 端口 + 保活参数，比 HTTPS 更抗间歇抖动。
2. **方案 C 已处于最宽松配置**（低速率中断已禁用），作为 HTTP 兜底无需改动；方案 B 因无代理跳过。
3. **DNS 建议（未执行，超出授权范围）**：当前唯一 nameserver 是 8.8.8.8，境内网络下偶发丢包是"间歇性不稳定"的常见根因。建议后续改为 `223.5.5.5`（阿里）+ `119.29.29.29`（腾讯）双 DNS（手动编辑 /etc/resolv.conf）。
4. 若未来遇到单次操作超时，可临时加长超时：`git -c http.connectTimeout=30 -c http.lowSpeedLimit=0 ...`（当前全局已是最宽松）。
