# Image Factory V1 恢复交接

新会话先读：根 `AGENTS.md` → `Image-Factory-Build-Kit-v3/START-CODEX.md` → 本目录 `STATUS.json`、`BASELINE.json`、`REUSE-MAP.md`、`TASKBOARD.md`。

## 当前真实状态

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

若 runner 未运行，可启动：

```bash
.venv/bin/python -m factory --config config.local.json --state var/live runner-loop --run V1-DEMO-KIDS-001 --interval 60
```

## 人审与自动收尾

真人必须在飞书为三张候选分别创建审核记录，并填写：

- 图片资产：对应候选
- 目标SHA256：候选记录当前 SHA256
- 结论：通过 / 驳回
- 审核人：当前真实审核人
- 人工确认：勾选
- Demo视觉检查：勾选（仅“通过”时必须）
- 仅限演示使用：勾选（仅“通过”时必须）
- 审核策略版本：`demo-v2`
- 运行ID：`V1-DEMO-KIDS-001`
- 审核版本：`1`
- 原因：非空

系统只读审核表，绝不代建或代勾选。三条回执齐全后，runner 会导出仅含最新批准候选的 ZIP；若全部驳回则明确 `no_delivery`，不创建空 ZIP。随后才写入隔离的 Demo Usage 与模拟指标，并将运行投影为完成。

停止和状态入口：

```bash
.venv/bin/python -m factory --config config.local.json --state var/live runner-stop --run V1-DEMO-KIDS-001
.venv/bin/python -m factory --config config.local.json --state var/live runner-status --run V1-DEMO-KIDS-001
```

根目录的“启动/暂停/查看状态” `.command` 是同等入口。RUN-001/002/003 是历史保护对象，不得更新。
