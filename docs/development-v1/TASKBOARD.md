# V1 开发任务板

| 阶段 | Owner | 验收 | 当前状态 |
|---|---|---|---|
| B0 基线/备份/能力 | Sol | 源码、账本、验收、Base 快照可读可校验 | 完成 |
| B1 原生节点 | Luna Runtime + Sol 复核 | request/receipt、实际字节、失败证据 | live 完成；5/6 调用有完整账本 |
| B2 独立 Runtime | Luna Runtime + Sol 集成 | 原子预算、单实例、重启、outbox、心跳 | L1 live 运行；L2 未验证 |
| B3 Schema/Prompt/审核 | Sol | 16 项精确增量、组件选择、demo-v2 | live 迁移和隔离种子完成 |
| B4 童鞋图片链 | Luna Image + Sol 复核 | alpha、白底、3 独立背景、3 合成、lineage | live 完成；待真人审图结论 |
| B5 审核/ZIP/回流 | Sol | 人审后自动交付、round-trip SHA、模拟指标 | 8 附件 live 往返通过；等待人审后收尾 |
| B6 对抗审查 | Sol | 144 项全量、实备份 ledger smoke、边界报告 | 迁移/附件/生图 live 已验；人审/ZIP/回流待验 |
| B7 交接 | Sol | 启停状态入口、六动作指南、精确 live 命令 | 完成 |

共享约束：开发子代理未写 live 飞书或调用图片；live 写入和五次原生调用均由根执行者按 Runtime 预算完成。真人审核仍不可代办。
