# 初始化与真实连接验收

前提：用户已配置 larkcli。先读 AGENTS.md。

A. 发现环境

检查当前工作目录；检查 Python >=3.10、Pillow、已有 lark-cli/larkcli 的真实可执行路径和版本。需要时执行 scripts/setup.sh，只安装项目依赖。执行 `python -m factory doctor`。本机帮助不支持包内命令时记录差异，优先集中适配 factory/lark.py/factory/sync.py 并加测试，不凭空编命令。

B. 确定业务资源

只接用户明确提供的新建专用 Base。通过已有 larkcli 的 URL 解析功能取得真实 Base token；Wiki 先解析其目标。允许只读查询验证链接，不从其他无关 Base 猜一个替代品。用户尚未提供时完成本地演练后停在这一环。

用户明确允许在指定 Base 新增结构后执行：

```bash
python -m factory bind --base-token 实际BaseToken
python -m factory doctor
python -m factory bootstrap
python -m factory bootstrap --apply
python -m factory schema-check
python -m factory setup-views
python -m factory seed-workflows
python -m factory probe-feishu
```

记录真实返回的表 ID、视图 ID、附件探针 SHA256 一致性。不要自动删除自检记录或默认空白表。需要审查权限时，只读当前权限信息；任何权限扩大、身份切换、公开分享都须单独明确授权。

取得审核人的真实 open_id 并由用户确认归属，登记 reviewer_open_ids。可从本人手工建立且不关联图片的“审核人登记”行读取系统创建人，与所选审核人核对一致。不得用显示名猜 ID，不创建真实批准记录。

C. 原生图片工具探针，必须有用户“一次调用”的授权

确认当前会话的实际图片工具或已安装 `$imagegen` Skill 的可用路径。先阅读当前环境的真实说明，确认原生路径，不启用 API。生成一个无品牌简单背景，拿到真实图片文件，记录工具名、结果位置、是否实际报告模型/调用 ID、是否消耗额度。禁止把 Pillow 画图、截图或目录里旧图片当探针。

生成成功后执行：

```bash
python -m factory record-capability --image /真实工具文件.png \
  --evidence /本次工具调用摘要.txt --actual-model 工具实际报告值或not_reported
```

证据摘要不得含访问凭证。无法直接导出文件时，允许用户从实际工具下载后明确提供文件，并注明人工搬运；不能自行发明路径。原生工具不可用时如实停止，不把 API 可用当作原生可用。

D. 留下验收结果

填写 templates/live-acceptance.json 的副本到 var/live/acceptance/。仅勾实际成功项。输出用户可点击的真实 Base 地址、已建立的五个界面名称、仍缺的权限或事实。不要在缺少实际视图地址时拼出假的链接。
