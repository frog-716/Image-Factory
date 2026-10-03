# 项目地图

Image Factory 用飞书保存业务数据，用本机 Engine 和流程引擎执行任务；Codex 只在流程要求时充当 AI／原生生图节点。人工只在本机 Human UI 看图、审核和领取交付。

## 一条主流程

飞书表单提交业务需求 → 受信接单检查明确确认和本地授权 → Engine 冻结商品、素材、流程和任务输入 → Runner 推进确定性步骤 → 需要生图时由获授权的 Codex 调原生图片工具 → Engine 校验并回写飞书 → 人在 Human UI 审核 → 只有批准的图进入 ZIP。

飞书是业务数据平台；Engine 是唯一业务规则和状态机；本机 `var/` 保存被忽略的运行账本、缓存和临时文件。不要把 `var/`、`config.local.json` 或 `.env` 加入仓库。

## 主要代码在哪里

| 位置 | 作用 |
|---|---|
| `factory/cli.py`、`factory/__main__.py` | 命令行入口；先看 `python -m factory --help`。 |
| `factory/engine.py` | 既有 Engine：校验、冻结、审核、交付和恢复等业务规则。 |
| `factory/form_intake_service.py` | 受信读取飞书任务表单；明确确认和本地授权缺一不可，只负责接单排队。 |
| `factory/runtime.py`、`factory/runtime_runner.py` | 独立 Runtime 的任务、预算、派发、结果登记和本地确定性步骤。 |
| `factory/runner_service.py`、`factory/v1_live.py` | 推进 Runtime，并通过 FeishuGateway 对账/投影运行结果。 |
| `factory/lark.py` | 飞书读写和附件接口的边界；接口差异收敛在这里。 |
| `factory/human_ui_server.py`、`factory/human_ui_static/` | 本机两页“待我审核／任务与交付”；审核只走 Engine API。 |
| `factory/image_pipeline.py` | 图片格式校验、白底图与候选图的确定性处理。 |
| `factory/state.py`、`factory/model.py`、`factory/schema.py`、`factory/sync.py` | 本地状态、数据模型、飞书字段契约和同步适配。 |
| `factory/metrics.py`、`factory/human_ops.py` | 指标整理及人工业务操作辅助。 |
| `templates/` | 飞书表结构、Workflow、商品/任务/素材和回执模板。 |
| `tests/` | 离线单元、接口模拟、故障和端到端测试。 |

## 文档与入口

- `README.md`：安装、边界和首次使用。
- `docs/01-架构与边界.md`：角色、业务边界和运行流程。
- `docs/04-数据契约.md`：字段、冻结、审核和回执约定。
- `docs/03-操作手册.md`、`docs/USER-GUIDE.md`：人工使用说明。
- `docs/05-对抗性审查.md`、`docs/06-验收与恢复.md`：安全检查和恢复方法。
- `docs/product-v1/`：Human UI 与产品验收记录。
- `docs/research/`：来源核验、图像工作流和研究记录。
- `assets/topstar-public/`：公开收录的 TOPSTAR 商品参考图；不是生产素材库，也不证明品牌授权。
- `macos/`、`启动审核界面.command`：macOS App 构建材料和本机审核入口。
- `scripts/setup.sh`、`scripts/test.sh`：本机环境准备与测试。

## 开始排查时

1. 看 `AGENTS.md` 和 `README.md`，先明确运行、授权和数据边界。
2. 看相关 `docs/`，再读对应 `factory/` 模块及其 `tests/`。
3. 本地完整测试运行 `bash scripts/test.sh`；真实飞书和真实原生生图验收要分开记录。
4. 要查当前 Runtime，按 CLI 帮助使用只读状态命令；不要仅凭旧文档或聊天记录重新派发任务。
