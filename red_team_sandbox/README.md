# 沙盒 (Sandbox)

这里存放 AI 通过 submit_system_proposal 提交的《修改提案修改提案》，只作审计与推演，**不参与生产**。

- proposals/ ：每条议案的 JSON（类型 / 阻塞事实 / 建议补丁 / 送审结果）
- patches/   ：议案附带补丁文本（人类评审用，永不自动应用）
- proposals_ledger.jsonl ：逐条追加的台账（含飞书送达结果）

生产配置只有两处且都由人类手工维护：config_center/settings.py（只读锁定）、config_center/wcu_policy.json（AI 只读）。AI 永远不能把沙盒里的东西写进生产。
