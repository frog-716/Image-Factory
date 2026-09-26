# 10 外部能力、来源与核验

核验日期：2026-09-21。公开文档描述不等于当前账号/客户端已开放。

## 已查官方资料

[S1] Codex Subagents：独立代理可指定 model 和 model_reasoning_effort，项目配置可使用 .codex/agents 下 TOML。
当前文档列出 gpt-5.6-luna；具体 Sol ID 与 effort 支持应读取本机模型目录。
https://learn.chatgpt.com/docs/agent-configuration/subagents

[S2] Long-running work：CLI/IDE/桌面文档描述 /goal，并明确目标模式不扩大既有沙箱和授权。
https://learn.chatgpt.com/docs/long-running-work

[S3] Non-interactive mode：codex exec、JSONL、输出 schema 和 sandbox 选项，可用于脚本集成。
这只验证存在程序化集成面，不证明原生图片在该模式一定可用。
https://learn.chatgpt.com/docs/non-interactive-mode

[S4] Codex App Server：可集成状态和事件，model/list 提供可用模型和推理档位。
https://learn.chatgpt.com/docs/app-server

[S5] Image generation：官方文档描述内置图片工具及 Codex 用量；所读页面仍写 gpt-image-2。
开发者主页又展示 GPT Image 2.5，说明页面/产品入口可能不同或更新不同步。
本包因此只把用户指定 Image 2.5 作为期望，真实调用记 tool/model_reported，不能硬编码声称已用 2.5。
https://learn.chatgpt.com/docs/image-generation
https://developers.openai.com/

[S6] Lark 官方 CLI：面向人和 Agent 的 CLI，具体 typed 能力以当前安装版本 help/已验证 Gateway 为准。
https://github.com/larksuite/cli
https://open.feishu.cn/document/mcp_open_tools/feishu-cli-let-ai-actually-do-your-work-in-feishu

[S7] 用户上传 `08-FEISHU-PERMISSIONS.md`：现役业务通过 FeishuGateway 和 typed 记录/字段/视图/附件接口；旧 raw scopes 不再是运行依赖。
本包保留其副本在 evidence/source-08-FEISHU-PERMISSIONS.md。该文档日期 2026-09-20。

## 本机 capability 逐项核验

模型名称/真实 ID；支持的 high/xhigh；Subagent 能否指定模型；/goal 是否可用。
原生 image 工具可见；实际产生文件；引用图片权限；透明背景能力。
CLI/SDK/headless 调用同一原生能力；回执事件能捕获；图像输出路径可用。
lark-cli 当前版本、typed 记录/附件能力，实际目标 Base 和已绑定表 ID。
应用页面/仪表盘配置是否有权限和官方 API，不能从 CRUD 成功推断 UI API 可用。

每项标：documented / available / live_verified / unavailable / unknown。
CAPABILITIES.json 需要证据路径与时间、运行环境，不填虚假 true。

## 模型配置样例使用规则

examples/codex/development.config.example.toml 仅供开发角色配置，不自动安装。
先从本机 model/list 或等效工具拿真实 ID/档位，再替换/合并。
不覆盖 ~/.codex，不修改 lark-cli 登录配置，不把开发模型组写入生产 Workflow。
生产节点独立配置其模型和工具能力；用户没授权时不改成付费 API。
