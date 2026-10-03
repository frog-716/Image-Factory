# Image Factory V1 恢复交接

> 当前状态（2026-09-27）：本文件以下关于 `V1-DEMO-KIDS-001`、5/6 额度及 waiting_review 的操作是**历史快照，不能作为现役恢复指令执行**。该虚构任务和图片已按用户要求清理，当前 `var/live` 没有活跃任务。请先看 [当前实施记录](../product-v1/LIVE-IMPLEMENTATION.md)；不要重新 seed 或启动旧任务。

新会话先读：根 `AGENTS.md` → `Image-Factory-Build-Kit-v3/START-CODEX.md` → 本目录 `STATUS.json`、`BASELINE.json`、`REUSE-MAP.md`、`TASKBOARD.md`。

## 2026-09-21 历史快照（恢复前必须重新核对）

- 运行：`V1-DEMO-KIDS-001`
- Runtime：`waiting_review`
- 图片调用：5/6，剩余 1；无 unknown job
- 飞书：8 个 V1 素材已写入且附件往返 SHA256 通过
- 人审：outdoor / indoor / studio 三条 demo-v2 审核均未创建
- L1：支持独立 loop、heartbeat、stop；L2 headless native image 未验证

不得重新迁移、重新 seed、重新初始化授权或重新生图。恢复时只读：

```bash
.venv/bin/python -m factory --config config.local.json --state var/live runner-status --run V1-DEMO-KIDS-001
.venv/bin/python -m factory --config config.local.json --state var/live schema-check
```

不要根据旧心跳自行重启生产。先只读对账 Runtime、飞书候选和审核状态；当前用户要求保持待审 Runtime、不代审、不重画。生产重启必须另有明确授权。

## 人审与自动收尾

真人双击 `dist/Image Factory.app`，打开“待我审核”，逐张看大图并明确点“通过”或“退回”；退回原因可选。前端不填写或显示 SHA256、Run ID、审核版本等技术字段。Engine API 根据当前可信 Candidate、SHA256、Runtime、Policy 和审核人身份创建有效记录，经 FeishuGateway 写入飞书；Runner 只能消费 Engine 确认的审核。Codex 不得代点、直接写表或用底表记录绕过 Engine。

三张图都经本人审核、Runner 健康并继续后，“任务与交付”提供仅含批准候选的 ZIP；若全部驳回则明确 `no_delivery`，不创建空 ZIP。之后才可能写入隔离的 Demo Usage 与模拟指标。具体见 [用户指南](../USER-GUIDE.md) 和 [审核提示词](../../prompts/04-REVIEW-DELIVER.md)。

停止和状态入口：

```bash
.venv/bin/python -m factory --config config.local.json --state var/live runner-stop --run V1-DEMO-KIDS-001
.venv/bin/python -m factory --config config.local.json --state var/live runner-status --run V1-DEMO-KIDS-001
```

根目录的“启动/暂停/查看状态” `.command` 是同等入口。RUN-001/002/003 是历史保护对象，不得更新。
