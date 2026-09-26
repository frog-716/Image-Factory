# Image Factory V1 开发报告

## 结论

V1 核心已建成，并通过离线、真实飞书和真实原生生图三层验收。`V1-DEMO-KIDS-001` 已产生透明商品源、白底展示图、三张独立背景和三张候选，8 个单版本素材已在飞书完成附件 SHA 往返校验。Runtime 当前为 `waiting_review`；ZIP 和模拟指标必须等真人完成 demo-v2 审核后才能生成。

## 建成内容

- Runtime：冻结计划、依赖顺序、原子预算、重启恢复、unknown/明确失败分流、审核快照和 outbox。
- 图片链：真 RGBA 童鞋源、白底派生、outdoor/indoor/studio 独立背景和三候选固定几何合成。
- 飞书：16 项只增不改 schema 迁移、V1 命名空间种子、提示词组件、demo-v2 人审、附件 SHA 往返和状态版本投影。
- 交付：只导出最新通过图片，全驳回 `no_delivery`，Demo Usage/模拟指标幂等回流，正式统计显式排除。
- 运行入口：`启动图片工厂.command`、`暂停图片工厂.command`、`查看图片工厂状态.command`。

## 离线验收

| command | exit code | 简短结果 |
|---|---:|---|
| `PATH="$PWD/.venv/bin:$PATH" bash scripts/test.sh` | 0 | 144 tests + 离线 Demo passed |
| 备份 ledger 可写副本上初始化 Runtime | 0 | 旧 `runs` 0→0，`runtime_*` 成功建立，原备份 SHA 不变 |
| `zsh -n` 三个 `.command` | 0 | 语法通过 |

## 真实飞书验收

应用 V1 增量迁移后，8 表 schema-check 通过。已写入隔离的 `V1-DEMO-KIDS` 商品、流程、4 个提示词组件、任务和 8 个素材；每个附件均上传后重新下载并核对 SHA256。RUN-001/002/003 只读回查保持原状态和错误摘要。迁移期间 link 参数不兼容 `800010701` 已先确认字段未落地，再通过受控 effect 对账恢复；素材空槽位的飞书空文本省略差异也已补失败测试后修复。

## 真实生图验收

共预留并实际调用 5/6 次：第一次商品源虽为 RGBA，但无 `alpha=255`，已保存失败文件和证据且不退款；第二次编辑输出仍以 254 为最大 alpha，因此保留原文件/SHA，并仅做 `254→255` 的确定性 alpha 归一后通过严格合同；其余三次分别独立生成 outdoor/indoor/studio 正方形背景并一次通过。工具没有报告实际模型名或调用 ID，均如实记录为 `not_reported`。无 API 回退，剩余 1 次未使用。

三张候选已由人眼做生成阶段检查，主体一致、无文字/Logo/人物、场景独立；这不等于人工审核通过。

## 已知限制

- L1 Runtime 可独立轮询/恢复；L2 headless native Worker 未实测。
- 单机、单执行器；不支持跨机 exactly-once。
- 真实审核只能由人在飞书完成；当前正等待三条 demo-v2 审核。审核后 runner 才能验证 ZIP 和模拟指标回流。
- 本轮不发布、不投放、不使用付费 API 回退。
