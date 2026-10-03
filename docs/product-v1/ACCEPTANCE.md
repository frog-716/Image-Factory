# Product V1 可观察验收条件

> 当前状态（2026-09-27）：下方“当前三张候选／waiting_review／5/6”是旧虚构演示删除前的历史验收快照，不是现役任务。该任务及图片已按用户要求清理；当前没有待审核任务，新的 TOPSTAR 演示尚未跑通，因此 Product V1 **未 PASS**。最新事实见 [Live 实施记录](LIVE-IMPLEMENTATION.md)。

正式范围只有两个用户页面：`待我审核`、`任务与交付`。飞书继续承载业务数据，Workflow Runtime 继续是唯一状态机；Human UI 不承载 Prompt、Workflow、素材库、设置、用户或第二份业务数据库。

## 固定验收条件

1. 用户直接看到当前候选大图，只做通过、退回与可选原因。
2. 浏览器不接收或提交 SHA256、Asset/Run/Workflow ID、审核策略、审核版本、reviewer ID 或 Demo/Production 安全字段。
3. POST 只允许 `decision` 与可选 `reason`；通过、退回均明确确认。
4. Engine 在提交瞬间重验 Runtime、Candidate 本地/远端字节、SHA256、Mode、Policy、任务关系和版本；旧页面拒绝。
5. 审核人来自在线验证的 lark-cli user；飞书系统创建人必须与其一致。
6. Candidate+SHA 固定唯一 operation；双击、刷新、超时不产生第二个有效 Review，未知写入只读协调。
7. 审核只经 FeishuGateway 追加到现有审核表；Runner 只消费 Runtime write journal 已确认的 record。
8. Runtime 完成且 ZIP 回写、下载 SHA256 再验证后，才显示交付入口。

## 历史验收状态（旧 V1 删除前）

| 项目 | 结果 |
| --- | --- |
| 两页 UI / 技术字段隐藏 / 明确确认 | PASS |
| 防重、超时协调、过期拒绝、审计 | 离线故障注入 PASS |
| Runner 可信消费、自动唤醒与 ZIP 下载 | 离线端到端 PASS |
| 当前三张真实候选只读展示 | PASS：3 张，PNG 实际可读 |
| 当前真实审核写入 | 未执行：必须由用户本人点击 |
| Runtime 回归 | `waiting_review`、5/6、Engine Review 写入 0 |

用户点击前的结论是 **READY FOR HUMAN E2E**。只有点击后再验证飞书创建人/审核人、自动绑定、Runtime 推进与 ZIP 下载，才能写最终 Product V1 PASS。
