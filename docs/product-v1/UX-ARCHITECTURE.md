# Product V1 人类操作层架构

## 正式边界

Product V1 是本机回环地址上的极薄 Human UI，不是第二套业务后台。它只有 `待我审核`、`任务与交付` 两页。商品、素材、Prompt、Workflow、任务、资产、反馈和运营数据仍在飞书；SQLite Workflow Runtime 仍是唯一状态机；Codex 只作为 Runtime 可调用的 Producer 节点。

```text
浏览器（仅 decision / reason）
  → loopback Human UI（session + CSRF + opaque capability）
  → HumanReviewEngine
  → Runtime 当前 Candidate / Policy / write journal / audit
  → FeishuGateway → 飞书审核事件
  → Runner 只读 Engine-confirmed Review → ZIP 交付
```

Human UI 没有第二数据库。短期页面 token 与 session 只在内存；幂等、未知写入和审计复用现有 `runtime_writes`、`runtime_events`。

## 受信审核

启动时通过 `lark-cli auth status --json --verify` 取得 ready/valid 的 user open_id；bot、过期会话或缺少 open_id 都拒绝启动。提交时 Engine 重验 Runtime 仍在 `human_review`，本地 Candidate SHA、飞书任务/资产关系与元数据、远端附件下载 SHA、冻结 Mode/Policy，并以全部历史 Review 的最大版本加一。写后必须读回并证明系统创建人=审核人=启动身份。

Demo/Production 检查位完全由冻结 Mode/Policy 决定；浏览器不可见、不可提交。

## 幂等、Runner 与交付

同一 Run+Candidate+SHA 只有一个 Engine operation。confirmed 的同决定重复请求返回既有结果，不同决定冲突；pending/unknown 只按唯一飞书标题读回协调，不重发。第一次确认后异步调用现有 `runner_service.run_once`；process lock 防止与现役 Runner 并发。当前步骤已经是 review，自动唤醒不会派发新的图片节点。

Runner 只读取 write journal 已确认的飞书 record。Runtime 完成后，任务页才签发短期下载 capability；每次下载都重新从飞书取 ZIP 并与 Runtime export SHA256 对比。

## 本机安全

- 仅监听 `127.0.0.1`；随机入口 token 换取 HttpOnly SameSite cookie；POST 还需 CSRF。
- 校验 Host/Origin，设置 CSP、frame deny、no-sniff、no-referrer、no-store。
- review/delivery capability 随机且按 TTL 清理；服务重启后旧页面失效。
- 前端没有 reviewer 输入、账号系统、远端公开地址或任何内部业务 ID。

该方案是当前单机单用户的最小安全边界，不声称防御同一 OS 用户下的恶意进程。
