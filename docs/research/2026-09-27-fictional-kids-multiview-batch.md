# 虚构童鞋多角度 Demo 素材批次（2026-09-27）

本批次只为 Image Factory 工作台演练准备**虚构、无品牌**童鞋参考图。输入是本项目此前原生生成的虚构透明商品源，SHA256 为 `8b1a67558990dc2012f847a68e540cddc2d39ad096c21208fca0f89af8cf394c`；**未使用**公开收集的 Topstar 图片作生成输入。图片不能冒充 Topstar 实物，也不表示商品结构或功能已核实。

用户明确授权新的独立批次最多 6 次。账本在 `var/fictional-assets-20260927/ledger.sqlite3`，授权 `AUTH-FICTIONAL-ASSET-20260927-6` 的 `FICTIONAL-ASSET-LAB` 作用域共预留 **6/6**；没有使用原有 `V1-DEMO-KIDS-001` 最后 1 次额度。每次调用前均有独立派发单，原生输出、输入快照、哈希与工具证据保存在相应 `runtime-jobs/<job_id>/`。图片工具未报告具体模型或 provider call ID，记为 `not_reported`，不宣称已验证某个具体模型版本。

来源核验的精确边界：本次参考图哈希与现役 Demo 原生 `product-source` 文件一致，操作记录可相互核对。创建批次时的旧账本文字 `previously_generated_fictional_demo` 是操作者提供的来源声明，初始化函数当时**没有独立核验上游 Runtime**；后续代码已将这类字段改为中性的 `caller_supplied_local_reference`，但不回写已冻结的批次计划。

| 调用 | 结果 | 画面 | 文件 | SHA256 |
| --- | --- | --- | --- | --- |
| 1 | 拒收，仍计费 | 侧偏三分之四；RGBA 最高 alpha=254，不满足透明源契约 | [拒收原图](../../var/fictional-assets-20260927/runtime-jobs/job_4a90b0332ca6fa68709c524694fd/failed/output.png) | `c4e253215e209145c98d14403390a912c9bf1cd5944822bd094c84185fce3f0d` |
| 2 | 接收 | 白底侧面双鞋 | [侧面图](../../var/fictional-assets-20260927/runtime-jobs/job_846687d60fb3a0a7babd10bc4079/output/catalog_image.png) | `8fe9f6ad50b6a125dc4299706d89b9386de34202a51b27004b0dd4193422cd35` |
| 3 | 接收 | 白底正面双鞋 | [正面图](../../var/fictional-assets-20260927/runtime-jobs/job_352fb5ca44516ef053f613f66adc/output/catalog_image.png) | `762c5cc5c94df8471488c12340806876a98944cc958d612e03fec0b7c1f0a64b` |
| 4 | 接收 | 白底背面双鞋 | [背面图](../../var/fictional-assets-20260927/runtime-jobs/job_483ad8b9383b3e2e62d451680dbf/output/catalog_image.png) | `54e7b2f534267a76f86af968c1359d5ee914e5ee35915837200d002d23c73f7a` |
| 5 | 接收 | 白底单鞋鞋底 | [鞋底图](../../var/fictional-assets-20260927/runtime-jobs/job_927ead7d2df7ded08b2065088bc1/output/catalog_image.png) | `eb198f35815c202df897728a69077107d5c16a111a74da12e9c508d926688265` |
| 6 | 接收 | 白底俯视双鞋 | [俯视图](../../var/fictional-assets-20260927/runtime-jobs/job_cb678c89c8e7c6ea004dc002ebff/output/catalog_image.png) | `de41a91c65b180b6243f744770ba9e56f115a00b1a28bfb43a9b82e4ff544751` |

第 2–6 张是电商风格的**多角度参考图**，不是通过人工审核的最终交付，也不是独立的透明商品原图。跨角度颜色和外观大体一致，但生成式模型可能虚构背面、鞋底等不可从原输入验证的细节；正式商品必须由品牌/商品负责人核对实物和授权。首张不合格图片不经后处理冒充合格原生透明图。

便于本机查看的五图副本：[全量多角度 ZIP](../../var/fictional-assets-20260927/fictional-kids-catalog-views.zip)。逐张原始接收记录和哈希仍以账本及上表路径为准，ZIP 本身不是最终审核交付。

## 对抗性视觉筛选

“通过文件契约”只表示图片能解码、有真实原生调用记录，**不等于适合做商品事实或最终交付**。将五张图与冻结的虚构源图交叉看，白色网布、浅蓝包边、宽魔术贴、白鞋带、蓝色鞋口和厚底这些大特征较一致；后跟、鞋底的真实结构无法从源图验证。

| 角度 | 选择 | 具体判断 |
| --- | --- | --- |
| 侧面 | 保留作 Demo 视觉参考 | 材质和蓝白分区连贯，但仍有三分之四视角，不是严格正侧面；画面顶部空白偏多。 |
| 正面 | 保留作 Demo 视觉参考 | 鞋头、绑带和鞋带清晰，双鞋近乎镜像；可用于界面、候选对比，不可据此断言真实左右脚结构。 |
| 背面 | 保留作 Demo 视觉参考 | 蓝色鞋跟与提环连续，构图清楚；后跟细节由模型推断，不能作为商品实拍证据。 |
| 鞋底 | **不纳入精选参考** | 纹路和防滑块是模型虚构的具体结构，源图没有可核对的鞋底正面；放入可复用素材库会诱发虚假商品细节。 |
| 俯视 | 保留作 Demo 视觉参考 | 鞋面网布、鞋带、绑带和鞋口容易检查；两鞋过于对称，正式 SKU 仍需实物核验。 |

[精选四角度 ZIP](../../var/fictional-assets-20260927/selected-reference-views.zip)仅供**隔离的虚构 Demo 知识参考**；没有作为飞书“商品原图”或已审核候选写入。当前 Base 的素材类型缺少准确的“AI 生成商品角度参考图”语义，强行套用“商品原图／风格参考／候选”会混淆可生产输入、视觉研究和人工批准。入飞书前应明确该分类与可用边界，并由人核对商品事实和授权。

本批次未写入飞书，也未投放、发布或代替人工审核。`FICTIONAL-ASSET-20260927-CATALOG` 本地状态为 `completed`，其待投影 outbox 仅属隔离素材账本，不代表飞书已同步。现役 `V1-DEMO-KIDS-001` 仍在 `waiting_review`，5/6 次旧授权已用、1 次未用。工作台真实页面可只读显示 3 张待审核图；完整真人审核→ZIP→指标闭环仍需用户自行审核，不能把离线 mock 测试当作真实端到端完成。
