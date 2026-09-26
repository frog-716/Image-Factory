# Image-Factory 飞书访问权限最小化

本文件只记录当前 Image-Factory 实际代码路径需要的飞书能力。身份固定为现有 `user`，不包含重新登录、身份切换或扩大授权的建议。

| 实际能力 | 使用功能 | 当前验证 | 当前授权结论 |
|---|---|---|---|
| `+field-list` | `schema-check`、附件字段定位 | 真实 Base 已通过 | 满足 |
| `+view-list` / `+view-create` / `+view-set-*` | 初始化视图与视图配置 | 5 个目标视图已验证；命令为 typed shortcut | 满足 |
| `+record-list` | Workflow、Steps、商品、任务、素材、审核、上架、效果数据读取与幂等查找 | 真实 Base 已通过 | 满足 |
| `+record-get` | 已知记录读取、关系与附件元数据读取 | 真实 Base 已通过 | 满足 |
| `+record-batch-create` | 种子/探针/业务记录创建 | CRUD 探针真实通过 | 满足 |
| `+record-batch-update` | 探针更新、任务/素材/审核状态写回 | CRUD 探针真实通过 | 满足 |
| `+record-delete` | 仅删除唯一标记的探针记录 | CRUD 与附件探针真实通过 | 满足 |
| `+record-upload-attachment` | 素材、生产回执、指标来源文件和附件探针上传 | 附件探针真实通过 | 满足 |
| `+record-download-attachment` | 素材、成图、附件探针下载 | 附件探针真实通过 | 满足 |

## Raw API 收敛结果

业务代码统一通过 `factory/lark.py` 的 `FeishuGateway` 访问 Base。当前代码不再调用 raw records API，也不再保留业务层拼接 `/records`、`/fields` 或 `/views` 的路径。底层 `lark-cli api` 未作为业务访问入口保留；`LarkCLI` 只负责既有 CLI 的 argv 调用与 JSON 解码。

当前 user token 对 raw records GET 实测缺少以下 scope：

- `bitable:app:readonly`
- `bitable:app`
- `base:record:retrieve`

这些 raw scope 不再是本工程当前运行路径所需权限。权限不足时 Gateway 只报告失败并停止，不在运行中申请授权或切换身份。

## 不计入本项目权限需求的能力

- 真实生图能力：本轮未调用，也没有自动 API 回退。
- 商品平台发布、广告投放、外部云盘和通讯录：当前代码不访问。
- `+table-list`：当前配置已保存目标表 ID，业务运行不依赖动态列全量表发现；曾出现网络 TLS timeout，不将其误记为授权通过。

最后核验：2026-09-20；CLI 为现有 `lark-cli 1.0.95`，未更新配置或登录状态。
