---
name: image-factory-build
description: Develop and validate Image Factory V1 in the existing project. Use for implementation, integration and adversarial review, not production task orchestration.
---

# Image Factory 开发 Skill

仅开发时使用。读取本包 START-CODEX.md 和 docs/07-DEVELOPMENT-PLAN.md。
Sol/Luna 是开发协作角色；成品必须由独立 Runtime 推进。
不覆盖现役 AGENTS、Gateway 或历史数据，真实接口从当前代码读取。
按阶段契约做实现、测试、回执证据、对抗审查和交接，按授权边界持续推进。
此文件随包提供作可选项目级 Skill；目录嵌套在启动包内时不会自动成为项目根 Skill。
需要安装时，先检查项目 `.agents/skills/` 已有内容，用独立名称合并；不能覆盖既有技能。
