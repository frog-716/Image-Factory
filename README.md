# TOPSTAR 图片工厂 · 飞书优先 V1.0

**你在飞书管理商品、选素材、改提示词、下任务和审图；独立 Runtime 推进流程，Codex 只承担运行中的 AI／原生生图节点。**

这是可运行的本地执行器、飞书建模/视图脚本、Codex Skill 与操作提示词的组合。日常管理界面在飞书，电脑上的目录只承担缓存、确定性处理与执行账本。

## 先看交付边界

已经验证：离线完整演练、单元/故障注入/接口模拟测试。测试证据见 [evidence/test-results.txt](evidence/test-results.txt) 和 [evidence/validation.json](evidence/validation.json)。

尚未验证：你的飞书租户权限、已安装 larkcli 的具体版本、你的 Codex 原生生图权限和真实商品质量。本包没有连接你的账号，没有采集拼多多图片，没有生成真实 TOPSTAR 成图。不能把模拟通过写成真实上线验收通过。

本版支持 macOS、Linux、WSL，Python 3.10+；使用单机进程锁，不支持 Windows 原生命令行或多电脑抢同一任务。脚本不会调用图片 API，不需要 OpenAI API Key，不会在额度不足时暗中改走付费 API。

## 第一次使用

在终端进入本目录，执行：

```bash
bash scripts/setup.sh
source .venv/bin/activate
bash scripts/test.sh
python -m factory demo --out var/my-first-demo
```

打开 `var/my-first-demo/index.html`。你会看到程序绘制的 3 张模拟候选、2 张模拟批准、1 张驳回，以及生成的 ZIP 和模拟反馈记录。演练不连接外部账号、不调用图片模型。已有同名演练目录时换一个 `--out`，不会覆盖旧记录。

现役 `V1-DEMO-KIDS-001` 的人工入口是 macOS App：双击 [`dist/Image Factory.app`](dist/Image%20Factory.app)，再点“启动并打开审核工作台”。第一次打开会在线验证当前 lark-cli user；浏览器显示“待我审核”和“任务与交付”。看大图后点通过或退回即可，SHA256、Asset/Run/Workflow ID、审核策略和版本均由 Engine 自动绑定。App 只启动本机 `127.0.0.1` Human UI，不启动生产、不生成图片、不预留额度；使用时保持 App 打开，退出 App 会停止它自己启动的服务。App 内有“使用教程”。

这个 App 依赖旁边的 Image-Factory 项目文件夹，不要单独移走。需要重建时，在项目根目录运行 `macos/build-app.sh`；生成物位于 `dist/Image Factory.app`，使用本机 ad-hoc 签名，尚未公证。原有 `启动审核界面.command` 仍可作为备用入口。

在 Codex 中打开本目录，粘贴：

```text
读取 AGENTS.md、README.md 和 prompts/00-START-HERE.md，按提示词执行。
先完成本地测试和现有 larkcli 能力检查；不要重新安装或重置我的 larkcli。
真实飞书操作限定到我提供的新建专用多维表。未拿到具体 Base 前，不修改任何旧表。
先完成连接验收；只有我明确允许原生生图后才能运行探针或生产。
遇到阻塞给出具体原因、已完成成果和最小修复动作，禁止造假通过。
默认中文。成功命令只报告 command、exit code、简短结果。
```

随后把新建空白多维表链接发给 Codex，并阅读 [分步骤操作](docs/03-操作手册.md)。本地 Skill 位于 `.agents/skills/feishu-image-factory/SKILL.md`；无法自动发现时直接让 Codex 读取该文件。

## 这条链做什么

```text
飞书商品资料 + 素材画册 + 流程步骤
                  ↓
飞书生产任务：选商品、流程、场景、版位、数量和调用上限
                  ↓
Codex 读取队列 → 脚本校验和冻结 → 原生生图 → 脚本合成和归档
                  ↓
飞书成图画册 → 人工审核 → 仅批准图片进入交付 ZIP
                  ↓
人工上架 → 登记具体使用位置 → 导入真实原始计数 → 下次创意参考
```

先用 1 个真实 SKU、1 个渠道、1 种场景广告图、3 个候选，调用上限 4。默认任务上限 12 张，系统上限 18 次调用，可在本地配置调整；这些是本工程的保护值，不能理解为平台官方额度。100 张候选建议拆成多个有明确创意方向的任务，最终批准数量可能少于候选数量。

## 文件入口

| 入口 | 用途 |
|---|---|
| [AGENTS.md](AGENTS.md) | Codex 不得绕过的执行和诚实性规则 |
| [prompts/00-START-HERE.md](prompts/00-START-HERE.md) | 全流程总指挥提示词 |
| `prompts/01...07` | 初始化、入库、生产、审核、反馈、恢复、复审 |
| [docs/01-架构与边界.md](docs/01-架构与边界.md) | 第一性原理、职责、数据权威和取舍 |
| [docs/02-飞书平台搭建.md](docs/02-飞书平台搭建.md) | 8 张表、5 个主要视图、权限和素材管理 |
| [docs/03-操作手册.md](docs/03-操作手册.md) | 可直接照做的全过程 |
| [docs/04-数据契约.md](docs/04-数据契约.md) | 提示词变量、回执、版本、反馈口径 |
| [docs/05-对抗性审查.md](docs/05-对抗性审查.md) | 攻击场景、已修正项、残余风险 |
| [docs/06-验收与恢复.md](docs/06-验收与恢复.md) | 真实验收清单和故障恢复 |
| [docs/07-来源与兼容性.md](docs/07-来源与兼容性.md) | 官方依据、当前命令契约、版本适配 |
| `factory/` | 实际执行代码 |
| `macos/build-app.sh` | 构建带图标和使用教程的 macOS 启动 App |
| `dist/Image Factory.app` | 普通用户双击启动入口；依赖项目根目录及 `.venv` |
| `启动审核界面.command` | 启动两页本机 Human UI；审核只经 Engine API 与 FeishuGateway |
| `tests/` | 可重复运行的测试 |
| `templates/` | 飞书字段、流程、商品/任务/回执/反馈模板 |
| `examples/offline-demo/` | 已运行的完整模拟证据与可浏览结果 |

## 重要约束

商品事实只能来自你确认的资料。来源或授权不明的素材不开工。生成图不能直接冒充商品原图。提交后的输入有改动，旧运行会停止，复制任务/流程版本继续。

背景合成模式只让模型生成背景，用你确认的透明商品原图做缩放、摆放和合成。更适合先保护商品形状；不保证自然接触阴影、透视或审美合格。参考图编辑模式更自由，必须人工逐项核对商品细节。

V1 可通过项目根目录的 `启动图片工厂.command` 运行受控单进程循环：它持久化心跳、预算、Attempt 和状态投影，遇到原生生图时明确停在 `waiting_worker`。本机当前未证明 headless 原生生图，因此运行器不会伪装能在无 Codex 会话时自动画图。详见 [V1 用户指南](docs/USER-GUIDE.md)。

脚本的技术检查不等于品牌或渠道审核。真实审核必须由人操作飞书；实际发布保留人工。整体质量、授权和业务归因仍需人工确认。
