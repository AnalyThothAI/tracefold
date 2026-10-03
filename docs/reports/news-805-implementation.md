# #805 方案 A：实现与认证流程

[Issue #805](https://github.com/AnalyThothAI/tracefold/issues/805) · [PR #808](https://github.com/AnalyThothAI/tracefold/pull/808) · [推送认证批 1](news-805-certification.md) · [News](../modules/news.md) · [标注规范](../modules/news-reader-labeling.md) · [契约](../CONTRACTS.md)

本页说明读者判断的代码、离线流程和运行时接缝。真实认证的数字见[推送认证批 1](news-805-certification.md)。
owner 发布审阅已写入运行时文件（`release_ready=true`，审阅与豁免证据见 #805）；部署按[切换与回滚](../OPERATIONS.md#news-reader-switch)执行。

## 当前状态

| 后端 | 数据 / 系数 | 推送切线 / 重点切线 | 状态 |
| --- | --- | --- | --- |
| native | 2,500 条 guide v5 代理样本拟合，m* = 2 | 0.372 / null | `certified`（仅推送），owner 发布审阅已写入 |
| generated | 未重问，零系数占位 | null / null | `uncalibrated`，回答只进信息流 |

- 资格表：`recap_or_old_period` 可推（owner 决定：附在当期公司报道后的最新财报行推送，纯回顾由低影响留在信息流）。
- 重点：`key_cut = null` 的推送认证有效，此时没有任何命题成为重点。重点精度认证留到下一轮（批 1 中候选重点区
  只有 56 个故事，owner 重点精度约 0.41）。
- 交易范围（D12–D14）只写在标注规范里，运行时不执行；改读者问题会使证书失效，下一轮把范围写进问题并重新认证。
- 新标注使用 guide v6；证书绑定拟合和 owner 标签实际使用的 guide v5，运行时文件记录该版本。

## 读者判断

模型在同一次 JEV 请求中独立回答报道类型、增量影响、是否应优先看到和核心锚点，冻结输入仍为 v3。
资格质量 e、影响尾部 m 和 held 输入推送 logistic，打断概率 i 与 e 输入重点 logistic；锚点不乘入分数。
native/generated 分开拟合，m* 在较早故事的折外预测中选择。计划冻结四题分布、概率、认证状态、当时切线与校准身份；
`reader_ineligible` 与概率不足、未认证分开展示，旧记录缺字段返回 null。

[运行时校准文件](../../tracefold/news/notifications/reader_calibration.json)由 `ReaderPolicy.load()` 加载，
文件字节摘要和资格表进入策略身份。生效还要求当前问题身份、资格表、生产组合判断器、实际作答适配器、served model
与审阅记录都匹配；任一不符按未认证处理，模型评分只进信息流。

## 离线流程

1. **导出**：[导出器](../../scripts/export_news_reader_cases.py)只接受 `TRACEFOLD_READONLY_DSN`，在只读
   repeatable-read 快照中按原采用 update、已送正文和回执恢复冻结 `ReaderInput`。每条带上命题引用的证据原文
   `source_texts`（每段至多 2,500 字，读者模型看不到）。输入来源按记录的判断器程序（`plan.reader_identity`）定：
   该程序有任一输入能被当前代码逐位复原，其全部输入都必须复原，否则报漂移；没有任何输入能复原的程序（reader v3
   之前的渲染）从同一 update 和正文重建，标为 `rebuilt`，不算摘要核验。`--census` 保留窗口内全部命题决定。
2. **代理标注**：`label_news_reader annotate` 用当前规范盲标，材料含 `source_text`，标签带 `repeat`（判重）。
3. **拟合**：`eval_news_reader assemble` 合并冻结输入、代理标签和重问日志；`fit` 冻结时间/故事切分并拟合候选。
   候选记录拟合标签的规范版本，认证的 owner 标签必须同版。
4. **冻结选择**：`owner-sample` 在完整 census 上建立边界之后的独立故事框，用冻结候选给每个代表打分，
   在看标签前按分数分为推送区、重点区和其余三层，按层给定样本量（可全查）和种子抽样；
   `--frozen-selection` 改为校验别处冻结的选择（重新计算故事框和全部分数）。
5. **owner 盲标**：`prepare-owner` 生成公开材料与私有 manifest；owner 只回答推送和重点并确认或推翻代理判重，
   `import-owner --proxy …` 从代理标签补齐类型和锚点并记录来源。
6. **认证**：`certify` 重建候选、故事框、分数和选择后计算精度下界（见下），写持久 holdout ledger。
7. **报告与运行时**：`report` 渲染证书；[导出桥接](../../scripts/export_news_reader_calibration.py)写出待审阅
   运行时文件，没有证书的后端保持零系数占位，不能直接覆盖仓库中的生产文件。

完整命令见[推送认证批 1 的复现一节](news-805-certification.md#复现)。`reask_news_models reader` 与
`label_news_reader annotate` 会真实调用模型，需单独授权。

## 认证数学

每条切线 c 选中的集合 S_c = {资格通过 ∧ p ≥ c} 由冻结分数精确已知。按冻结分层，N_h 为 S_c 在第 h 层的数量，
n_h 为其中 owner 已标注的数量，k_h 为 owner 判为推送（或重点）的数量。下界为
Σ N_h · CP(k_h, n_h, δ/H) / Σ N_h，H 为 N_h > 0 的层数，有选中量但无标注的层计 0；只有一层且全查时
就是普通的单侧 Clopper–Pearson。推送与重点各用 δ = 0.05（每后端合计 0.1）。

切线序列取候选冻结序列中不低于选择时冻结的最松切线的部分，从第一个选中量达到最小数（推送 150、重点 60）的
切线开始，由严到松，首次失败即停；通过还需同样数量的 owner 标注独立故事。重点只在推送通过后、在推送切线
之内检验，误差再按推送序列中可能被选中的切线数平均分配。

## 发布门槛与审阅

证书的 `release_gates` 包括精度、概率抽样、成对区分度、回答覆盖、类型误判、端到端召回、影响含糊和成对 p90
延迟，以及卡片日量回放、日量目标或豁免、owner 审阅、留出使用审阅四项外部证据。桥接加 `--review` 才激活：
审阅记录逐项绑定 `dataset_sha256`、`candidate_identity`、`holdout_identity`、证书 canonical 摘要、报告字节摘要、
生产组合判断器身份、`reviewed_by` 与含时区的 `reviewed_at`；四项外部证据各需 `passed=true`、`evidence_ref` 和
`evidence_sha256`；测得不为 true 的门槛必须列入 `waived_gates`，写明理由与证据，精度与概率抽样不可豁免。
运行时文件冻结 `review_ref` 与审阅记录的摘要。批 1 中各门槛的测量结果见[认证报告](news-805-certification.md#其余测得门槛)。

上线按[切换与对称回滚](../OPERATIONS.md#news-reader-switch)执行，无 schema 迁移；上线后的复核是独立证据。
