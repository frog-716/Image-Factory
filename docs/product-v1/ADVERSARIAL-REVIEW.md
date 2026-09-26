# Product V1 对抗性审查

## 结论

两页 Human UI 的关键安全路径已实现并通过离线故障注入；真实候选已完成只读展示验收。最终 live PASS 仍等待用户本人点击后的闭环证据。

| 攻击/故障 | 防护 | 结果 |
| --- | --- | --- |
| 前端伪造身份、SHA、Run、Policy、Version、Mode | POST 仅 decision/reason；Engine 重建可信上下文 | PASS |
| 跨站请求本机 API | 127.0.0.1、Host/Origin、随机入口、HttpOnly SameSite cookie、CSRF | PASS |
| token 泄露内部 ID | 随机内存 capability，30 分钟 TTL | PASS |
| 旧页面审核已变化图片 | 提交前重验 Runtime、本地文件、飞书元数据及下载附件 SHA | PASS |
| 双击/刷新/响应丢失 | 前端锁 + Runtime write journal + 唯一远端标题协调 | PASS |
| 已确认后改点另一结论 | 固定 operation 冲突，不新增有效 Review | PASS |
| bot/他人代审 | `auth status --verify` 只接受 user；写后创建人=审核人=启动身份 | PASS |
| 浏览器伪造 Demo/Production 安全字段 | 浏览器无这些字段，Engine 从冻结 Mode/Policy 设置 | PASS |
| 手填飞书审核绕过 Engine | Runner 仅接受 journal 已确认 record ID | PASS |
| 历史审核存在 | 保留；新 Review 用 max(version)+1，不覆盖 | PASS |
| ZIP 替换 | 下载前从飞书回读并比对 Runtime export SHA256 | PASS |
| 重复唤醒 Runner | 复用现有 runner process lock；冲突只记 audit | PASS（单机） |

## 残余风险

- 本机单用户认证不能隔离同一 OS 账号下的恶意进程。
- 飞书管理员仍可修改底表；不一致会阻断，但不能阻止管理员本身。
- 单机 Runtime 不保证跨电脑 exactly-once。
- 导出后撤回不能收回已下载或外部投放文件。
- 审美、真实性、授权和渠道合规仍由人判断。
- 飞书/lark-cli 漂移或网络故障可能进入待协调状态；系统不会盲重试。
