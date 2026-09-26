---
name: feishu-image-factory
description: 在本工程内使用已配置的 larkcli、Codex 原生图片工具和 Python 脚本，建立与运行飞书电商图片生产流程。适用初始化、授权素材入库、提交任务、原生生图、人工审图、ZIP 交付、指标回填和中断恢复。不用于自动发广告、绕过额度或未授权 API 生图。
---

# 飞书图片工厂

从当前工作目录向上找到同时含 `AGENTS.md`、`factory/`、`prompts/` 的项目根目录，只读该工程必要内容。所有脚本从根目录执行。

先读 AGENTS.md。首次执行读 README.md 和 prompts/00-START-HERE.md；日常按阶段读取对应单个提示词，不把所有文档每轮塞进上下文。

初始化：prompts/01-SETUP.md。素材入库：02-INGEST.md。生成：03-PRODUCE.md。人工审核/交付：04-REVIEW-DELIVER.md。反馈：05-PUBLISH-FEEDBACK.md。恢复：06-RESUME.md。代码变更后：07-RED-TEAM.md。

固定调用边界：先 `python -m factory start-slot` 成功并保存飞书检查点，再调用当前会话真实原生图片工具一次，最后 `stage-result` 接收真实文件和回执。普通脚本无法自行假设宿主内置工具的调用方式。原生工具不可用时停在能力缺口，不改走 API。

任务由飞书业务记录驱动；冻结输入和执行状态由脚本控制。严禁手工改 SQLite、冻结 JSON 或伪造能力文件绕过校验。未完成不能写完成，模拟数据不能写真实。

人工审核由人在飞书创建，本工具只读取。脚本不会把取消、退款、自动上架或第三方投放当作允许的副作用。
