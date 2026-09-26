# Image Factory V1 · 开发启动包 v3

**现在用 Sol 高 + Luna 极高建系统；以后由独立流程引擎运行系统，Codex 只是其中的 AI 节点。**

本包交给现有 Image-Factory 项目的开发 Codex 使用。它包含开发契约、任务依赖、参考代码、测试、配置样例和交付标准。它不覆盖旧工程，也不声称已经在你的电脑部署完成。

## 你只读这里

1. 将本文件夹放在现有 `Image-Factory` 根目录，保留本文件夹这一层。
2. 在 Codex 打开原项目，开一个新的开发对话，选 Sol、高。
3. 使用对话中的启动指令，或让 Codex 读取 [START-CODEX.md](START-CODEX.md)。
4. 开发过程不用逐条粘贴阶段提示词。到真正需要你授权或审图时再介入。

完整小白教程：[START-HERE.md](START-HERE.md)。

## 建成后的使用方式

打开飞书：**选童鞋和场景 → 提交任务 → 看图并审核 → 取 ZIP**。
本地运行器提前启动，读取飞书任务并记录每一步。Codex 只领一个节点任务，做完交回结果。关闭开发对话后，运行器仍能读状态、等待、恢复和继续。

## 包内地图

| 目录或文件 | 谁读 | 用途 |
|---|---|---|
| `START-CODEX.md` | Sol | 一次启动开发，含目标、边界、验收 |
| `docs/01-PRODUCT-AND-BOUNDARIES.md` | Sol | 开发期/运行期职责分开 |
| `docs/02-REUSE-AND-MIGRATION.md` | Sol + Luna | 保留现有飞书链路、旧 Demo 和运行档案 |
| `docs/03-RUNTIME-AND-NODES.md` | 工程执行者 | 独立引擎、任务回执、恢复和状态 |
| `docs/04-FEISHU-AND-HUMAN-UX.md` | 工程执行者 | 飞书点选、审图、四入口与少操作 |
| `docs/05-ASSETS-PROMPTS-DEMO.md` | 工程执行者 | 童鞋透明原图、白底图、背景、真实 Demo |
| `docs/06-METRICS-AND-TRACE.md` | 工程执行者 | 指标口径、文件血缘、成本和归因限制 |
| `docs/07-DEVELOPMENT-PLAN.md` | Sol | 任务图、Subagent 分工、阶段验收 |
| `docs/08-ADVERSARIAL-REVIEW.md` | 独立复核者 | 风险、已修复项、尚待 live 验证项 |
| `docs/09-ACCEPTANCE-AND-HANDOVER.md` | Sol + 你 | 建成标准，禁止以测试条数代替产品验收 |
| `docs/10-CAPABILITIES-AND-SOURCES.md` | Sol | 官方资料与本机能力差异 |
| `prompts/` | Sol / Luna / 运行期 Worker | 各阶段提示词，角色明确分开 |
| `templates/` | 工程执行者 | 授权、回执、字段映射、指标等 JSON 模板 |
| `examples/` | 工程执行者 | Workflow、模型配置、界面草图 |
| `reference/` | 工程执行者 | 可运行的独立引擎参考与离线测试 |
| `evidence/` | 你 / Sol | 本包实际执行的离线验证证据 |

## 已验证与未验证

本包的离线参考代码确实运行并测试。具体数目见 `evidence/reference-tests.txt`。
这些测试不等于你现有工程的 113 项测试，也不替代现有回归。
本包没有读取你电脑上最新源码、没有修改飞书、没有调用你账号中的原生生图，也没有生成新童鞋样片。
真实童鞋素材生成是开发团队必须完成的 live 验收任务，详细提示词已提供。

参考代码用于说明可测试的核心契约；不要把它整目录覆盖到现有 `factory/`。
真实 Gateway、Codex 原生生图适配、身份认证、常驻运行器和飞书界面仍由开发 Codex 在现有项目中集成实现。

本包不附带旧广告拼图或办公椅素材。测试中的小图片只是算法生成的色块测试文件，不属于电商示例图。
