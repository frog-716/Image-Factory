# Image Factory V1 复用与增量修改图

| 现役正本 | 处理 | V1 增量 | 验收 |
|---|---|---|---|
| `factory/lark.py::FeishuGateway` | 原样保留为唯一飞书访问层 | Runtime 的投影/附件写入只能经 Gateway | 不出现 raw `/records` 业务调用；typed 读回 |
| `factory/state.py` 与 `var/live/ledger.sqlite3` | 保留同一 SQLite 账本 | 在同一数据库增加 Run/StepRun/Attempt/授权预算/outbox 表；不新建第二个业务账本 | 重启恢复、原子 claim、unknown 不重试 |
| `factory/engine.py` | 保留旧生产路径与 RUN 档案兼容 | V1 Runtime 包装节点执行；旧 RUN-001/002/003 不迁移、不改写 | 旧回归全通过；旧记录只读不变 |
| `factory/demo_live.py` | 保留 RUN-002 legacy Demo 解释 | 新增 `demo-v2` 审核策略，独立使用 Demo 视觉与仅演示字段 | legacy 不变；V1 不要求虚假商业合规 |
| `factory/schema.py` | 保留 8 表结构 | 仅增模式、命名空间、Prompt 组件选择、Demo 审核、资产角色等字段 | `schema-plan` 精确 dry-run，迁移后 readback |
| `factory/sync.py` | 保留 bootstrap/view/metrics | 新增只读 schema diff；正式指标改为显式 `mode=production` | 未标模式不进入正式统计 |
| `factory/media.py` | 保留旧合成兼容 | V1 图片链使用独立强 alpha 契约与冻结几何 | 白鞋不按近白阈值抠图；三背景独立 |
| Demo Export / Metrics | 保留现役幂等与 ZIP round-trip | V1 复用同一附件链路、追加 mode/review policy | ZIP 精确等于批准集合；二次导入 existing |
| `Image-Factory-Build-Kit-v3/reference/` | 只作契约参考 | 按模块吸收，不整目录覆盖 | 当前工程接口与历史测试继续成立 |

## 明确不做

- 不重复 bootstrap，不重置 lark-cli，不切换 user/bot 身份。
- 不删除/覆盖 RUN-001/002/003、旧资产、旧审核或旧交付。
- 不把 Codex 开发对话、Sol 或 Luna 变成运行期状态机。
- 不增加付费 API、网页 Cookie、自动审核、自动发布或投放。
