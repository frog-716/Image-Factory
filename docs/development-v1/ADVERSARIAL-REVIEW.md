# V1 对抗性审查

| 风险 | 结果 | 证据/修正 | 剩余边界 |
|---|---|---|---|
| V1 Runtime 覆盖旧 `runs` | DESIGN_FIXED + TESTED | V1 只使用 `runtime_*`；同账本共存和真实备份副本 smoke 通过 | 不自动迁移旧 Run |
| 多 Run 规避 6 次上限 | LIVE_VERIFIED | `runtime_authorizations.reserved` 使用 `BEGIN IMMEDIATE` 原子共享；live 已预留 5/6，剩余 1 | 第 7 次离线拒绝已测 |
| 坏图伪装“未发生”退款 | DESIGN_FIXED + TESTED | `runtime-reject-image` 保存失败图/证据，Attempt 确认失败，预算不退 | 修正仅能使用剩余预算 |
| unknown 盲目重试 | DESIGN_FIXED + TESTED | unknown 步骤阻塞；仅完整 terminal/no-effect 证明可重开 | live 对账仍需人工 |
| 状态投影滞后/回退 | DESIGN_FIXED + TESTED | outbox + `event_seq`；更新的远端版本不被旧事件覆盖 | RUN-002 历史状态不修改 |
| 自动人审/伪商品确认 | DESIGN_FIXED + TESTED | demo-v2 只读审核，要求真实审核人、人工确认、版本/Run/hash 绑定 | 同账号 user 模式非强权限边界 |
| 白鞋近白阈值抠图 | LIVE_VERIFIED | 商品源原生 RGBA；原始 0..254 与仅 `254→255` 的派生文件均保留 SHA；白底由 alpha 合成 | 最终美观待真人审图 |
| 背景拼图/库存图冒充 | LIVE_VERIFIED | outdoor/indoor/studio 三个独立 Job、三次独立原生调用、三张正方形图 | 原生工具未报告模型/call ID |
| 审核后篡改/错包 | DESIGN_FIXED + TESTED | 导出重读最新审核和 SHA，ZIP 集合=批准集合，附件往返校验 | live 人审后待验 |
| Demo 污染正式指标 | DESIGN_FIXED + TESTED | 正式查询只收 `mode=production`；Demo Usage/指标独立标记且幂等 | 人审后 live 回流待验 |
| 飞书空文本省略导致重复写 | LIVE_FIXED + TESTED | 首条素材创建后检测到差异并停止；先复现失败，再仅规范化文本 `None==""`，续跑复用原记录 | 不放宽 SHA/关联/角色校验 |
| 开发 Thread 永久主控 | LIVE_VERIFIED_L1 | 独立 loop、heartbeat、stop、重启检查点；L1 当前等待人审 | headless native 未验，L2 BLOCKED |

未运行的 live 攻击测试不标 PASS。真实飞书迁移、附件、原生图已分开验收；人审、ZIP 和回流仍明确待验。
