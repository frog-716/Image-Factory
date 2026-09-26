# 03 独立 Runtime 与节点契约

## 调度权

Runtime 做：读取已提交任务、冻结配置、校验输入、排队、预算预留、发单、收回执、推进、等待审核、导出和同步。
Codex 节点做：一个限定任务，返回结构化数据/真实文件，不选择下一节点，不自行导出，不改业务审核。
飞书工作流不得和 Runtime 同时写相同状态。

## V1 进程

`runner`：本地单进程循环；一次只处理一个 live 写入，持有 OS 排他锁/现有 DB 锁。
`worker adapter`：按节点类型启动已验证工具，限定工作目录和输出，不读取飞书凭证。
`gateway`：复用现役 FeishuGateway，负责一切远端写入。
`status projector`：将单个运行器的权威结果投影到飞书，失败记待同步，不重新生图。

自定义节点只从固定注册表选择。飞书字段中的 Python、Shell、URL 指令一律视为数据。

## 三层持久记录

Run：业务任务、mode、category、workflow_hash、素材/模板快照、授权、预算、状态。
StepRun：节点类型、依赖、输入哈希、输出摘要、状态、时间。
Attempt：某次外部调用的 request_hash、attempt_id、worker_handle、证据、消耗、终态。

发送之前先写 Attempt 和占用预算。即便进程退出，磁盘上仍能知道正在等待哪一单。
回执需匹配 run/step/attempt/request_hash，并以实际文件复算 hash，不能仅信“成功”消息。
既有 task/run 不变时，配置修改必须新建 revision；不允许修改已派发请求。

## 状态

执行状态：queued / running / waiting_worker / waiting_review / blocked / delivery_pending_sync / delivered / completed_demo / no_delivery / cancelled。
候选审核状态独立：pending / approved / rejected / revoked。

一张批准与一张驳回是候选结论；整个任务完成导出后应显示“已交付”。
Demo 指标结束后显示“演示完成”。真实经营反馈可以持续回流，不要求有指标才算交付完成。
全部驳回时显示“无可交付图片”，不得创建空 ZIP 冒充完成。

## unknown 的正确含义

超时只表示客户端不知道结果。立即读到空附件只证明“当前未观察到绑定”，不证明上传从未发生。
必须区分：upload_started、upload_receipt_seen、bind_observed、download_verified；不把 upload 和 bind 混成成功。
如果 typed CLI 内部封装三阶段，Gateway 可维持一个公开方法，但保留阶段诊断与回执证据。
先有界只读协调。确认已成功就复用；确认原执行结束且不会产生晚到副作用才可标记可重试失败。
仅有“空 GET”无法充分证明时，保留 unresolved；不清账后直接给新 operation 再发一次。
不能取得严格幂等保证的外部系统，只承诺防重复与证据协调，不宣称全链 exactly-once。

## Codex 原生节点

优先验证本机 `codex exec` 或 App Server 的实际能力。
官方有非交互运行和 App Server，不等于本机 native image 在这些入口也可用。
先 help/模型目录/配置只读核实，再以本次授权的第一张童鞋源图验证真实调用和结果落盘，避免额外空耗。
存在 image_gen 名称只证明工具可见，不能标记“真实输出/无人值守已通过”。
无法无交互调用时状态为 `BLOCKED_NATIVE_AUTOMATION`，不通过模拟器、网页点击脚本或付费 API 绕开。
有交互执行窗口时可以先完成 L1，但须注明产品自动化条件未满足。

## 最小 Worker 请求与结果

请求：job_id、run_id、step_id、attempt_id、mode、request_hash、输入文件清单/hash、视觉提示词、允许输出角色、预算、工作根目录。
结果：job_id、request_hash、status、origin、实际 tool/model 或 not_reported、真实调用证据引用、输出文件与 hash/bytes。
工作文件必须位于隔离 job 目录，拒绝 `../`、符号链接逃逸、任意 URL 拉取和意外附件。
只收允许文件类型与数量，完整解码与像素上限检查。

文本 JSON 验证不等于可信证明。运行器应直接抓取调用事件、文件与日志，校验 worker_handle，而不是让 Worker 自己签署“native=true”。
本参考代码演示契约、预算和协调；身份鉴别、进程隔离、调度循环、网络适配必须在目标项目实现。
