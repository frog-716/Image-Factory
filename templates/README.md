# 模板使用

`product-input.json`、`asset-input.json`、`task-input.json` 是实际业务字段的空模板，确认项均为 false，不能直接提交。它们没有真实 TOPSTAR 商品事实。由 Codex读取真实资料、用户确认后填入实际值；附件必须用上传链路。

`workflow-background.json` 是已实现的默认流程。`workflow-reference-edit.json` 是未发布草案，可运行 `python -m factory seed-workflows --template templates/workflow-reference-edit.json` 添加后在飞书人工确认再发布。

`feishu-schema.json` 是字段契约。`metrics.csv` 是规范化反馈 CSV 的表头。`native-receipt.example.json` 和 `native-tool-evidence.txt` 仅说明结构，禁止冒充实际回执。

`live-acceptance.json` 复制到运行目录逐项填写，未完成项目必须保持 false。
