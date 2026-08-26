# Tracefold News GEPA 生产级 Gold Dataset 设计研究

日期：2026-08-26

研究范围：当前 `News Agent` 的人工 Review 数据如何变成可供 DSPy GEPA 使用、可审计且不会泄漏的生产级 Gold Dataset。

结论状态：设计建议，不改变当前 Reader Contract、Review v4、Metric v4、GEPA 或 release gate。

落地跟踪：[Issue #245](https://github.com/AnalyThothAI/tracefold/issues/245)。

本文用三个前缀区分证据层级：

- **外部事实**：来自 DSPy/GEPA 官方文档或源码、原始论文、平台官方文档、NIST 等一手来源。
- **仓库事实**：来自 Tracefold 当前提交 `78682409281ace85af1a5264e60069d6dd719318` 的代码、合同或 Issue 证据。
- **建议/推论**：根据以上事实为 Tracefold 做出的工程判断；不是 DSPy 或 GEPA 的通用硬性要求。

## 结论先行

1. **现在已有的是高价值的“候选标注池”，还不是 Gold Dataset。** 已推送、被拦截、模型 drop、Gate suppress、restatement leak 都是需要覆盖的真实生产样本；但模型 draft、Event 行数或 24 小时数量本身都不能充当 Gold。Gold 的最小单位应是经过人工确认的独立事实簇，而不是媒体条数。
2. **Codex/LLM 可以当“助教”，不能在当前信任合同下冒充最终人工。** 它适合起草 Review v4、建议事实簇边、找 rubric 自相矛盾、排出高风险队列；最终 `accepted news_review_v4` 仍需人读证据、修正并明确接受。模型独立产出的数据只能叫 `silver/proposal`。
3. **不要把所有标为重复的 Event 删除。** 必须先区分三种重复：队列/任务身份重复应为零；同一事实的媒体复述应折叠为一个统计簇；但至少保留代表性的 `restatement` 负例，否则 GEPA 无法学习或评估“不要重复推送”。`progression` 是同一 storyline 的新变化，通常应是新的事实簇，不能和纯复述一起删除。
4. **24 小时适合作为每日采集批次或 smoke test，不足以代表生产分布。** 正式 development 应跨多个已经关闭的自然日；train 与 GEPA selection 必须按事实簇、按时间隔离，未来 validation 再使用优化结束之后的新时间窗。
5. **采用成熟流程，但暂不引入第二套真相。** ReviewDesk/PostgreSQL 继续作为唯一 accepted truth 和 Freeze 输入。Label Studio/Argilla 只有在多人并行、盲审分配和一致性报表成为真实瓶颈时，才作为可替换的标注 UI/工作流投影；它们的 `ground truth` 标记不能直接进入 GEPA。
6. **当前合同已经覆盖了最难的治理部分，但缺少多人标注运营层。** 已有：版本化 rubric、证据引用、显式 owner、exact expected、append-only acceptance、事实簇 split、readiness、预算和未来 release 阶段。缺少：独立双标记录、裁决记录、标注指南版本/校准集、pre-acceptance 事实簇工作台、按字段一致性统计，以及防止同一事实簇在 GEPA 中按媒体成员数过度加权的明确上限。

## 0. 当前生产数据盘点

本节只报告结构化数量，不复制新闻正文、模型 reasoning 或凭证。审计时的运行配置来自 operator-owned `~/.tracefold/config.yaml`，CLI 只返回了路径和已配置布尔值。

### 0.1 活跃 cohort 的 ReviewDesk 覆盖

2026-08-26 13:52:49 CST 的只读 coverage 查询显示：

| 窗口参数 | replayable | received | reviewed | accepted | 说明 |
| --- | ---: | ---: | ---: | ---: | --- |
| 24h | 555 | 232 | 0 | 0 | 当前 active cohort |
| 72h | 555 | 232 | 0 | 0 | 与 24h 相同，不能视为 3 个自然日 |

24h 与 72h 数量相同并不是“过去三天分布稳定”，而是当前 cohort 的可用数据都落在最近 24 小时内。正式 profile 需要真实跨越至少 3 个 settled natural days，扩大 `--hours` 参数不会制造缺少的日历覆盖。

当前 queue strata 包含 21 个 delivered、35 个 model-drop，以及多类 macro/regional/throttled controls；这些是抽样层，不等同于最终 Gold label。尤其不能把 `delivered` 当 `should_push`，也不能把“被拦截”自动当 `should_hold`。

### 0.2 已生成但尚未被接受的 proposal pool

已经落盘的 `tracefold.news.review_draft_batch.v2` 批次 SHA 为 `df221003f08fdcf916850ef1dfa12849535150a789a55439a691531db3b79003`：

- 437 个 draft、437 个唯一 task、0 个 draft error；
- action proposal：`should_push=165`、`should_hold=166`、`must_hold=106`；
- novelty proposal：`new_fact=224`、`progression=113`、`restatement=100`；
- draft confidence：最小 0.72、中位数 0.85、最大 0.95；
- 126 个高风险优先复核 task，其中 `must_action=106`、`restatement_sent=19`、`factual_failure=1`、`low_confidence=1`，理由可以重叠；
- accepted Gold 仍为 0。

因此，437 不是 GEPA dataset 的 N。它既包含尚未人工确认的标签，也尚未按 connected fact cluster 折叠；当前最多只能称为覆盖 555 个 replayable tasks 中 437 个的 pre-Gold 库存。进入 Freeze 的真实 N 要在事实簇确认、独立复核、裁决和 ReviewDesk acceptance 后重新计算。

这也回答“能否由 Codex 制造高标准数据集”：**Codex 可以把 437 条 proposal 收敛成结构一致、证据齐全、按风险排序的待裁决集合；没有独立复核和最终接受权限时，它不能单独把这批数据升级为生产 Gold。**

## 1. GEPA 到底需要什么数据

### 1.1 Trainset、valset 的真实语义

**外部事实。** Tracefold 锁定的是 `dspy==3.3.0`。该版本源码要求 `trainset` 非空；`trainset` 用于反思式更新，`valset` 用于追踪 Pareto 分数和选择候选。若不传 `valset`，DSPy 会复用 `trainset`，并明确警告这适合对给定任务过拟合的 inference-time scaling，不适合证明对未见任务的泛化；官方建议保留“足以代表下游分布的最小 valset”，同时尽可能增大 trainset。[DSPy 3.3.0 GEPA 源码](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/teleprompt/gepa/gepa.py#L499-L581)

**外部事实。** 用户引用的 Trusted Monitor 教程先构建 `dspy.Example`，再明确切成 200 条 train 和 100 条 validation；它传入独立的 `trainset`/`valset`，指标同时返回 scalar score 和自然语言 feedback。[Trusted Monitor 数据切分](https://dspy.ai/tutorials/gepa_trusted_monitor/#apps-dataset)、[Trusted Monitor comparative metric](https://dspy.ai/tutorials/gepa_trusted_monitor/#comparative-metric)

**建议/推论。** 教程里的随机切分不能直接照抄到新闻流。新闻是有时间顺序、同一事实会被多家媒体反复报道的非独立数据；随机行切分会把同一事实放到两侧，使 selection 看见 train 中的答案。Tracefold 当前按事实簇和时间做 70/30 split，方向正确，应继续作为唯一 GEPA development split。

### 1.2 Metric 不只是一个分数

**外部事实。** GEPA 可以接受普通 float metric，但只有 `Prediction/ScoreWithFeedback(score, feedback)` 才会把具体失败原因交给 reflection LM；否则反思只看到泛化的分数说明。对多 Predictor 程序，DSPy 还会提供 `pred_name` 和该 Predictor 的 `pred_trace`，让反馈路由到可修复的组件。[DSPy GEPA in-depth](https://github.com/stanfordnlp/dspy/blob/main/docs/docs/diving-deeper/gepa-in-depth.md#1-the-metric-is-the-feedback-channel--return-predictionscore-feedback-not-a-bare-float)、[DSPy 3.3.0 GEPA 源码](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/teleprompt/gepa/gepa.py#L63-L102)

**外部事实。** GEPA 论文的核心也不是“拿标签训练权重”，而是从系统 trajectory 和文本反馈中反思失败、提出文本参数变体，并在 Pareto frontier 上搜索；论文报告的是跨六项任务的样本效率结果，而不是一个对所有业务通用的数据量门槛。[GEPA 原始论文](https://arxiv.org/abs/2507.19457)

**仓库事实。** Tracefold Metric v4 已把最终 reader action、结构化 TradeRelevance、semantics/novelty 和 ReaderCard 分开计分，并将 feedback 路由给 EventSemantics 或 ReaderCard；失败维度没有 exact Gold 时不进入该维度分母。[Tracefold CONTRACTS](https://github.com/AnalyThothAI/tracefold/blob/78682409281ace85af1a5264e60069d6dd719318/docs/CONTRACTS.md#L958-L986)

**建议/推论。** 因此数据集制作的优化目标不是笼统的“少推送”，而是：

- distinct、必须及时到达的事实不能被漏掉；
- 弱相关、背景、已复述的事实应 hold；
- action 之外的方向、传导、意外程度和 reader value 要有 exact Gold；
- feedback 只能指向 Prompt 真能修的错误，Gate、retrieval、policy、delivery 缺陷必须排除出 GEPA target。

如果只用“是否已经推送”当 label，GEPA 最容易学到的是复刻现有系统，而不是修正现有系统。

### 1.3 数据量和调用成本是联动的

**外部事实。** DSPy 要求 `auto`、`max_full_evals`、`max_metric_calls` 三选一；`max_full_evals` 会按 `len(trainset) + len(valset)` 换算 metric-call 预算。GEPA 会多次全量评估 valset，reflection LM 成本也可能超过 task LM。[DSPy 3.3.0 budget 源码](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/teleprompt/gepa/gepa.py#L301-L357)、[DSPy GEPA cost 说明](https://github.com/stanfordnlp/dspy/blob/main/docs/docs/diving-deeper/gepa-in-depth.md#12-reflection-lm-cost-dominates-if-youre-not-careful)

**仓库事实。** Tracefold 每个 metric call 固定执行两个 Predictor，并让 readiness 在花钱前给出 task/judge call envelope。[Objective readiness](https://github.com/AnalyThothAI/tracefold/blob/78682409281ace85af1a5264e60069d6dd719318/src/tracefold/news/learning/objective.py#L974-L1043)

**建议/推论。** “Gold 越多越好”不能脱离预算讨论。先增加事实簇覆盖，再用 readiness 的 selection 大小计算第一轮 full evaluation、task calls、judge ceiling 和总成本；不得因为标了 500 条，就用不足以完成一次 selection evaluation 的预算启动 GEPA。

## 2. 正确的样本单位：事实簇，不是 Event 行

### 2.1 三层去重

| 层级 | 例子 | 正确处理 | 是否计作独立 N |
| --- | --- | --- | --- |
| 身份重复 | 同一 `task_id + task_version` 被分页重复返回 | 直接拒绝；模型调用和 Review 都只能发生一次 | 否 |
| 同事实复述 | 多家媒体转述同一决定、同一数字、同一状态变化 | 连接为一个 `connected fact cluster`；保留代表性 restatement case | 一个簇 |
| 同 storyline 的新进展 | “谈判开始”之后“协议签署” | 标为 `progression`，通常建立新事实簇，同时保留 storyline 关联 | 是 |

**仓库事实。** #242/#243 已修复 ReviewDesk 队列的分页身份重复、稀疏 strata 截断和窗口漂移。部署后的 24 小时审计为 466 行、466 个唯一 task、零重复；这解决的是第一层，不会自动解决语义事实重复。[Issue #242 deployment evidence](https://github.com/AnalyThothAI/tracefold/issues/242#issuecomment-5421124621)

**仓库事实。** Review v4 要求 `restatement` 必须填写 `duplicate_of`，并禁止其它 novelty 类型填写它。[Review v4 novelty contract](https://github.com/AnalyThothAI/tracefold/blob/78682409281ace85af1a5264e60069d6dd719318/src/tracefold/news/review/desk.py#L253-L265)

**仓库事实。** Freeze 时现有实现会把 accepted `duplicate_of` 连通分量、相同 source fact 和 deterministic normalized-text fallback 折叠成一个 `news_fact_cluster_v1`；指向窗外同一 prior 的复述也会进入同一分量。[connected fact cluster 实现](https://github.com/AnalyThothAI/tracefold/blob/78682409281ace85af1a5264e60069d6dd719318/src/tracefold/news/learning/projection.py#L401-L449)

### 2.2 推荐的事实簇判定规则

**建议/推论。** 每个节点是一个唯一、当前版本的 Review task。按下列证据从强到弱建立候选边：

1. 同一 task identity：身份重复，删除额外副本；
2. accepted `restatement.duplicate_of`：确定边；
3. 相同 immutable source/focus-fact identity：确定边；
4. exact normalized fact key：确定边，但必须抽查误合并；
5. embedding/LLM 判断“表达同一原子事实”：只能是候选边，需人工确认；
6. 仅共享实体、storyline 或主题：不是重复边。

对连通分量执行两项 QA：

- 大簇检查：成员异常多时，防止一个错误边把不同 progression 串成一个簇；
- 时间跨度检查：相距很久的成员若仍称 restatement，确认它是在复述旧事实，而不是新状态变化。

### 2.3 不要把 duplicate 全部排掉

**仓库事实。** Tracefold 把“已知复述却到达读者”作为 hard gate，并另行报告 accepted restatement 的 retrieval recall；没有 restatement cases 就无法评估重复拦截是否真的改善。[Objective retrieval receipt](https://github.com/AnalyThothAI/tracefold/blob/78682409281ace85af1a5264e60069d6dd719318/src/tracefold/news/learning/objective.py#L322-L359)、[Metric hard gates](https://github.com/AnalyThothAI/tracefold/blob/78682409281ace85af1a5264e60069d6dd719318/docs/CONTRACTS.md#L958-L978)

**建议/推论。** 每个事实簇建议最多选：

- 1 个最早、证据最完整的 canonical fact；
- 1 个最容易误推的 restatement；
- 如确有新的状态变化，再选 1 个 progression，但它应是独立事实簇。

这不是修改当前 Freeze 算法的硬编码要求，而是标注抽样规则。原始成员继续留在 PostgreSQL 供审计；统计、抽样和 CI 以 cluster 为单位。需要特别注意：当前 GEPA 接收 split 内的全部 case，而不是每簇自动只取一个代表，因此同簇成员过多仍可能在优化选择中被按 case 数加权。这是现有 contract 的一个缺口，应在正式大规模运行前通过“每簇成员上限/cluster-weighted selection”单独立 Issue 处理，不能靠删光 restatement 掩盖。

## 3. 从过去 24 小时/多日数据构造语料

### 3.1 先固定总体，再抽样

**仓库事实。** 当前 queue 提供 deterministic strata 和 `sampling_probability`，包含 trade-relevance 定向层、critical、throttled、delivered、model_drop、gate_suppress 和 random control；Event queue 的 cursor 会固定首屏 upper bound。[ReviewDesk selection](https://github.com/AnalyThothAI/tracefold/blob/78682409281ace85af1a5264e60069d6dd719318/src/tracefold/news/review/desk.py#L1957-L2022)、[ReviewDesk queue contract](https://github.com/AnalyThothAI/tracefold/blob/78682409281ace85af1a5264e60069d6dd719318/docs/CONTRACTS.md#L767-L789)

**建议/推论。** 每个标注批次先写死以下 manifest，再打开任何结果：

```text
from_ms / to_ms（已关闭并越过 settlement grace）
active bundle / Program / policy / runtime-model cohort
Review task version、rubric version、reader contract version
queue sampler/selection version
包含的 strata 和每层计划 cluster 数
排除原因及数量
```

不要用“最近 24 小时”这个移动查询先标一部分、过几小时再继续；要将 24 小时转换为固定毫秒区间。#225 已证明 closed window、Freeze SHA 和一致 identity 是可执行的。[Issue #225 operator pilot](https://github.com/AnalyThothAI/tracefold/issues/225#issuecomment-5413507138)

### 3.2 两块面板：代表性 + 挑战性

**建议/推论。** 一个高标准 development corpus 同时需要：

**Representative panel（用于 selection/基线解释）**

- 按事实簇而不是 Event 行抽样；
- 覆盖 push 与 hold、不同交易时段、工作日/周末、宏观/加密/美股/自选边界；
- 保留 queue sampling probability，报告被抽总体和入选总体；
- 不按事后价格筛选 should-push；高波动只能做 discovery strata。

**Challenge panel（主要用于 GEPA train）**

- `must_push` 漏推、`must_hold` 误推；
- model_drop 与 delivered 的边界案例；
- 已推送的 restatement、被正确拦截的 restatement；
- exact TradeRelevance 错误；
- 明确可由 `triage_prompt` 修复的 target；
- stable 正确的 control，防止 GEPA 通过“全部更激进/全部更保守”取巧。

挑战集可以有意富集失败，selection 和未来 validation 不能只由失败组成。DSPy 官方把 valset 用于候选选择，因此 valset 的分布错误会直接选择错误 Prompt，而不只是让报告难看。

### 3.3 时间和泄漏

**外部事实。** 官方模型选择文档把“同一 group 不跨 train/test”和“时间序列不能用未来训练、过去评估”视为不同但都必要的约束；普通随机 split 会产生过于乐观的结果。[scikit-learn GroupKFold](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.GroupKFold.html)、[TimeSeriesSplit](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html)、[data leakage 指南](https://scikit-learn.org/stable/common_pitfalls.html#data-leakage)

**仓库事实。** Tracefold 已按 cluster 的 latest Event 时间排序，较早 70% 为 train，较晚 30% 为 development-selection；簇不拆分，双方都必须有 safety、positive、negative、novelty，否则 readiness fail closed。[Tracefold honest split](https://github.com/AnalyThothAI/tracefold/blob/78682409281ace85af1a5264e60069d6dd719318/src/tracefold/news/learning/objective.py#L246-L329)

**建议/推论。** 需要三层而不是两层：

```text
过去多日 frozen development
  ├─ earlier 70% cluster-time train：GEPA 反思
  └─ later 30% cluster-time development-selection：选择 candidate

优化结束后的新时间窗
  └─ future temporal validation：完全未被 GEPA 看见
```

`development-selection` 不是 final holdout。任何人根据它手动改 rubric、事实簇或 Prompt 后，都已“看过答案”，必须另开 future window。

## 4. LLM/Codex 能否充当人工标注者

### 4.1 可以做什么

**外部事实。** 模型标注表现高度依赖任务。一个 PNAS 原始研究在若干文本分类任务中发现 ChatGPT 能超过其使用的 crowd-worker 基准；后续 PNAS Nexus 原始研究在不同数据和任务上则发现表现不稳定，并建议对 substantive annotation 保持谨慎。[Gilardi et al., 2023](https://doi.org/10.1073/pnas.2305016120)、[Törnberg, 2025](https://doi.org/10.1093/pnasnexus/pgaf069)

**外部事实。** LLM-as-a-judge 还存在 position bias 等系统性偏差，不能因为输出解释得很像专家就假定其是独立裁判。[Judging the Judges 原始论文](https://arxiv.org/abs/2406.07791)

**建议/推论。** Codex/LLM 可以承担：

- 按 Review v4 起草 proposal；
- 根据 immutable evidence 给出证据引用候选；
- 提议 `duplicate_of` 和事实簇候选边；
- 检查 `fail` 是否缺 expected、owner 是否和实际故障面冲突；
- 找双标分歧、生成裁决清单；
- 对 accepted corpus 做结构、覆盖、泄漏和异常簇审计。

### 4.2 不能做什么

**仓库事实。** Tracefold 明确把 model draft 定义为文件 proposal；只有显式、append-only 的 review submission 与 acceptance receipt 才是真相。#225 也明确禁止自动接受 DeepSeek 生成的 owner 或 gold。[Draft/acceptance contract](https://github.com/AnalyThothAI/tracefold/blob/78682409281ace85af1a5264e60069d6dd719318/docs/CONTRACTS.md#L1142-L1152)、[Issue #225](https://github.com/AnalyThothAI/tracefold/issues/225)

**建议/推论。** 以下行为会破坏生产 Gold：

- Codex 看完自己的 draft 后直接批量 `accept-drafts`；
- 用同一 DeepSeek 同时当 drafter、Gold reviewer、metric judge 和 GEPA reflection；
- 把模型置信度当作 label correctness；
- 对模型不好判断的案例默认为 stable 正确；
- 用模型投票代替人的事实和产品边界裁决。

根本风险是循环论证：模型提出标签，GEPA 针对这些标签优化，再由同族模型判断优化成功。LLM-only 数据可以保留为 `silver`，用于发现候选、成本预估和 reviewer 预填；不得进入 current trust root。

## 5. 生产级人机标注流程

### 5.1 角色分离

**建议/推论。** 推荐四个逻辑角色；人数不足时同一人可以在不同时间承担，但记录中不能混成一个动作：

1. `Drafter`：LLM 生成 proposal，不可接受；
2. `Primary reviewer`：逐证据独立作答；
3. `Second reviewer`：对所有 target/safety/restatement 和随机 controls 盲审，不看第一人的选择；
4. `Adjudicator`：只处理分歧，给出最终 rubric 和 guideline change。

模型 draft 会造成锚定风险，因此至少一名 reviewer 应在校准集和审计子集上看不到 suggestion。其结果用来衡量“LLM 辅助节省了多少时间”以及“是否改变了人的 label 分布”，而不是默认模型总能提高质量。

### 5.2 双审比例

**建议/推论。** 第一版可执行规则：

- 100% 双审：`must_push`、`must_hold`、factual failure、GEPA target、restatement/duplicate_of、external miss；
- 100% 双审：任何显式 `first_bad_owner=triage_prompt`；
- 至少 20% 随机双审：普通 stable-correct controls，按 strata 分层；
- 0% 自动接受：LLM draft；
- 全部分歧必须裁决，不能用 majority vote 跳过证据。

**外部事实。** 原始 annotation disagreement 研究建议将分歧作为任务定义或指南含糊的信号，经过概念对齐、独立标注和 disagreement resolution，而不是只把分歧平均掉。[Interrater Disagreement Resolution](https://aclanthology.org/2021.humeval-1.15/)

### 5.3 一致性指标和项目门槛

**外部事实。** Cohen's kappa 是两名标注者对 nominal labels 的经典 chance-corrected agreement；Krippendorff's alpha 可处理多标注者、缺失值和不同测量尺度。它们衡量的是标注过程可复现性，不替代事实正确性。[Cohen, 1960](https://doi.org/10.1177/001316446002000104)、[Krippendorff reliability resources](https://www.asc.upenn.edu/krippendorffs-alpha-reliability)

**建议/推论。** 以下是 Tracefold 自定义 release gate，不是外部标准声称的通用阈值：

- 校准批次：先对 30–50 个独立事实簇全量双标；
- `should_push`、novelty、owner：pre-adjudication raw exact agreement ≥ 90%，且 alpha/kappa ≥ 0.80；
- typed expected：在双方都认为 applicable 的字段上 exact agreement ≥ 95%；
- safety、target 和 `duplicate_of`：裁决后 100% 唯一、无 unresolved；
- 同时报告 confusion matrix、每字段 N 和 label prevalence；分布极偏时，不用高 raw agreement 掩盖低 alpha，也不用不稳定 alpha 掩盖高风险分歧；
- 低于门槛：暂停新接受、修订 guideline、重新校准并回看受影响 batch。

### 5.4 单条 Gold 的硬门槛

一条 case 只有同时满足以下条件才可进入 Frozen Dataset：

- 当前 active cohort，当前 `news_review_v4` 和 Reader Contract；
- closed window、evidence/context 可重放、task version 未 supersede；
- task identity 唯一；
- `should_push`、novelty 已决定；restatement 有有效 `duplicate_of`；
- 任一 `fail` 都有 evidence refs；
- 任一 scored typed failure 都有 exact `expected`；
- GEPA target 的 `first_bad_owner=triage_prompt` 是人显式填写，不是系统推导；
- safety/target/duplicate 已完成双审和裁决；
- 最终 acceptance 只通过 ReviewDesk 写入 PostgreSQL receipt。

**仓库事实。** Review v4 已在 schema 中强制 factual label、push 的 timeliness、fail 的 evidence ref，以及 expected 只能对应 failed dimension。[EventRubricSubmission validator](https://github.com/AnalyThothAI/tracefold/blob/78682409281ace85af1a5264e60069d6dd719318/src/tracefold/news/review/desk.py#L321-L364)

### 5.5 整批数据说明书

**外部事实。** Datasheets for Datasets 建议随数据记录创建动机、组成、采集过程、预处理、推荐用途、分发和维护方式，使使用者能判断数据是否适合目标任务，而不是只拿到一组 rows。[Datasheets for Datasets](https://arxiv.org/abs/1803.09010)

**建议/推论。** 每个 frozen Tracefold dataset 除机器可验的 SHA/identity 外，还应附一个版本化 datasheet，至少说明：

- 来源窗口、settlement grace、active cohort 和抽样总体；
- Event/case/connected-cluster 数量及各 strata/action/novelty 分布；
- duplicate/progression 聚类规则、人工确认率和异常大簇；
- drafter/reviewer/adjudicator 角色、指南版本、双审比例和 agreement；
- included target/control、excluded owner 和 exact-Gold coverage；
- 已知偏差、允许用途、禁止用途以及 future holdout 边界。

Datasheet 是 Freeze 的解释层，不是另一套可修改标签；其中的计数必须能从 frozen artifact 与 acceptance receipts 复算。

## 6. Tracefold 现有合同覆盖与缺口

| 能力 | 当前状态 | 判断 |
| --- | --- | --- |
| 唯一 accepted truth | PostgreSQL judgment + acceptance receipt，append-only | 已覆盖，必须保留 |
| 任务版本/幂等 | `task_id`、SHA task version、Idempotency-Key | 已覆盖 |
| 证据隔离 | task-scoped immutable evidence；market reaction 接受前隐藏 | 已覆盖 |
| Rubric | Review v4 action、15 dimensions、novelty、owner、evidence、exact expected | 已覆盖 |
| 模型 proposal 与 Gold 分离 | draft 写文件，不自动成为 review | 已覆盖 |
| Queue 分层与抽样率 | deterministic strata + sampling probability | 已覆盖 |
| 事实簇 | accepted duplicate/source/text 连通分量 | Freeze 已覆盖；标注前工作台不足 |
| Train/selection 防泄漏 | 70/30 cluster-time split，coverage fail closed | 已覆盖 |
| Target 权限 | 只有人显式 `triage_prompt` + 可验证 gold 才是 target | 已覆盖 |
| 数据准备解释 | readiness 零模型调用，target/control/excluded 和 blocker | 已覆盖 |
| 多人独立标注 | acceptance 当前是一次最终动作，没有独立 response/adjudication ledger | **缺口** |
| 标注指南/校准版本 | Reader Contract/rubric 有版本；具体判例手册和校准集无独立版本 | **缺口** |
| IAA/审计抽样报表 | 无按 reviewer/字段的 agreement、disagreement、adjudication 指标 | **缺口** |
| Pre-acceptance fact clustering | LLM/embedding 候选边和人工确认没有一等工作流 | **缺口** |
| Cluster-weighted GEPA | split 不跨簇，但 train/val 仍包含簇内全部 case | **缺口/需测量** |
| 最终 untouched temporal test | release profile 有 future validation/shadow/canary | 已覆盖，但必须实际积累新数据 |

**仓库事实。** 正式 development profile 当前要求至少 30 个 boundary clusters、100 个 retention clusters、50 个 negative clusters、3 个自然日、3 个 strata 且必须有 safety。boundary 与 retention 在计数实现中互斥，因此仅满足前两项就至少需要 130 个独立 cluster。[release profile](https://github.com/AnalyThothAI/tracefold/blob/78682409281ace85af1a5264e60069d6dd719318/src/tracefold/news/learning/profile.py#L22-L40)、[dataset counts](https://github.com/AnalyThothAI/tracefold/blob/78682409281ace85af1a5264e60069d6dd719318/src/tracefold/news/learning/dataset.py#L594-L640)

## 7. Label Studio / Argilla：成熟方案还是第二套真相

### 7.1 它们真正能补什么

**外部事实。** Label Studio 支持把模型 prediction 作为只读 pre-annotation，再由人复制/修改为 annotation；review assignment、overlap、ground-truth 标记和 agreement 工作流主要位于付费产品层级。[Label Studio pre-annotations](https://labelstud.io/guide/predictions.html)、[Label Studio feature matrix](https://labelstud.io/guide/label_studio_compare#quality-workflows)

**外部事实。** Argilla 把 model `Suggestion` 与 user `Response` 明确分开，一个问题可有多个用户 response，并可用 `min_submitted` 配置 overlap；还提供相似度检索和按 suggestion/response 的筛选。[Argilla Response](https://docs.argilla.io/latest/reference/argilla/records/responses/)、[Argilla overlap distribution](https://docs.argilla.io/latest/reference/argilla/settings/task_distribution/)、[Argilla annotation UI](https://docs.argilla.io/latest/how_to_guides/annotate/)

### 7.2 推荐决策

**建议/推论。** 当前不建议让 Label Studio 或 Argilla 成为数据真相，也不建议现在为 1–2 名 reviewer 建设双向同步。原因不是这些产品不成熟，而是 Tracefold 的 task version、evidence SHA、Program/policy cohort、acceptance receipt 和 Freeze 身份都已经由 PostgreSQL 约束；把平台的 mutable annotation/ground-truth 再同步回来，会引入第二个版本、第二种并发语义和第二套删除/修改权限。

现在采用：

```text
ReviewDesk/PostgreSQL = authoritative evidence + accepted truth + Freeze source
LLM batch file         = proposal/silver
Notebook/report        = read-only QA projection
```

满足以下任一条件后再做平台 spike（项目门槛，不是产品官方门槛）：

- 同时有 3 名以上 reviewer，需要自动盲分配/overlap；
- 每周需要人工处理 300 个以上独立 cluster 或 backlog 超过 1,000；
- 20% 审计样本、裁决和按字段 agreement 已无法用 ReviewDesk 导出稳定完成；
- 需要 SSO/RBAC、正式 reviewer performance 和审计报表。

若接入，平台只能是可重建 UI：

1. 从 ReviewDesk 导出 `task_id + task_version + evidence projection`；
2. 模型 draft 导入为 suggestion，绝不导入为 response/ground truth；
3. 两名 reviewer 独立 response；
4. adjudicator 生成最终 Review v4；
5. 通过 ReviewDesk API 的 If-Match/Idempotency-Key 提交；
6. PostgreSQL 返回 acceptance receipt 才算完成；stale task 必须拒绝；
7. Freeze 永远不读 Label Studio/Argilla DB。

选择倾向：若只是 News 文本 rubric、suggestion/response/overlap，Argilla 的概念映射更直接；若未来需要多模态、复杂区域标注或企业级 reviewer 管理，Label Studio 更广。两者都不能替代 Tracefold 的 domain truth。

## 8. 样本规模、阶段门槛与停止规则

GEPA/DSPy 官方没有给“新闻 Agent 至少 N 条”的通用阈值。下面的数字分成仓库硬门槛与 Tracefold 规划建议，不能混写。

| 阶段 | 独立事实簇规模 | 时间 | 能声明什么 |
| --- | ---: | --- | --- |
| Rubric 校准 | 30–50，全量双标 | 可来自若干 closed batch | 标注指南是否可复现；不能跑 release claim |
| S0 smoke | 8–12；#225 实际 6 簇跑通 | 关闭窄窗 | 工作流物理可运行；不能声明 uplift |
| **正式 contract floor** | **至少 130**，并满足 boundary≥30、retention≥100、negative≥50、safety、strata≥3 | ≥3 自然日 | 只表示 development profile 计数合格 |
| Production v1 建议 | 150–250；其中自然 verified Prompt targets 目标≥30、controls≥100 | ≥7 个 settled 自然日 | 可做有意义的 bounded GEPA/offline candidate discovery |
| 稳健 production 建议 | 400–600，按 cluster-time 和 strata 达到停止规则 | 14–28 日，覆盖不同时段/周末 | 可开始估计稳定收益；仍需 future validation |
| Future validation | 当前合同：≥24 h、≥200 eligible Events、计划 50/至少 30 primary clusters、最多 100 人工 judgments | 必须在优化之后 | 检验未见时间窗；之后仍需 shadow/canary |

**建议/推论。** 400–600 是规划范围，不是魔法数字。真正的停止规则应是：

- 每个 required stratum 和 action label 都达到预注册最小 cluster 数；
- 新增一周数据不再大幅改变 label/owner/target 分布；
- cluster-bootstrap 95% CI 已达到预注册精度，candidate improvement 的区间不跨 0；
- safety critical regressions 为 0；
- 不靠增加同一大新闻的媒体成员缩窄 CI。

如果没有自然出现足够的 Prompt-owned target，不要把 retrieval/Gate/policy 错误改名成 Prompt target 来凑 30；正确终态是“没有足够优化机会，保持 Stable”。

## 9. 成本模型

### 9.1 人工标注

**建议/推论。** 用 cluster 而不是 Event 估算工时：

```text
primary_hours
  = primary_clusters × median_primary_minutes / 60

second_pass_hours
  = double_review_clusters × median_second_minutes / 60

adjudication_hours
  = disagreements × median_adjudication_minutes / 60
```

举例（仅为容量规划，必须用首个 30–50 簇校准批次替换假设）：若 200 个簇 primary 每簇 6 分钟，100 个高风险簇 second pass 每簇 5 分钟，20 个分歧每个裁决 10 分钟，总计约 31.7 人时。事实簇预折叠对成本的影响通常比换标注平台更直接。

### 9.2 模型和 GEPA

**建议/推论。** 成本表必须至少包含：

- draft calls / schema failures / unique tasks；
- GEPA metric calls；
- task calls（Tracefold = `2 × metric calls`）；
- reflection calls；
- metric-judge ceiling 和实际 calls；
- provider known cost 与 unpriced-call 安全计费；
- wall clock 和失败重跑成本。

先跑 readiness，再设预算。扩大 selection set 会提高初次和周期性 full evaluation 的固定成本；不能为了省钱让 train 和 val 重合，也不能截断同一 frozen corpus 后仍声称与 baseline 同数据集。

### 9.3 平台成本

外部标注平台的真实总成本还包括部署、升级、备份、用户/RBAC、数据脱敏、schema adapter、stale-task reconciliation 和双向同步事故。当前团队如果仍由少数 operator 审核，这些成本高于 UI 收益；达到第 7 节规模门槛再比较 self-host Argilla、Label Studio Enterprise 与 ReviewDesk 原生扩展。

## 10. 推荐的端到端 SOP

```text
1. 固定 closed 多日窗口和 active cohort manifest
2. 读取 ReviewDesk 全量分页；验证 task identity 唯一
3. LLM 起草 rubric + 候选 duplicate edges（proposal only）
4. 人工建立/确认事实簇；按簇抽 representative + challenge panels
5. Primary review
6. Safety/target/restatement 全量 Second review + controls 20% audit
7. 分歧裁决；记录 guideline change 和 pre-adjudication agreement
8. 仅通过 ReviewDesk 接受最终 Review v4
9. Freeze；检查 cluster/count/cohort/rubric/profile SHA
10. readiness；若 insufficient，按 blocker 补自然证据，不加模型预算
11. 同一 dataset 的 compile_live baseline
12. bounded GEPA；保存 NO_OP/REJECTED/ADVANCE terminal report
13. ADVANCE 也不能上线：进入未来 temporal validation、blind pairwise、shadow、canary
```

NIST AI RMF 要求记录测试集、指标、TEVV 方法、数据代表性和 human-AI 监督职责；上述角色分离、版本 manifest 和 acceptance receipt 与这一治理方向一致。[NIST AI RMF Core](https://airc.nist.gov/airmf-resources/airmf/5-sec-core/)

## 11. 下一步的最小落地建议

1. 将当前多日 proposal pool 视为待审库存，不执行模型自动接受。
2. 先抽 30–50 个独立事实簇做 blind calibration：必须同时有 delivered、model_drop、Gate suppress、正确/错误 restatement、must_push/must_hold。
3. 产出一个版本化 `News Review v4 Annotation Handbook`，至少包含 20 个裁决后的边界例，绑定 rubric/reader-contract SHA。
4. 在不改 truth 的前提下做一个 read-only QA notebook：事实簇大小、label/owner/expected 覆盖、双标 agreement、split 时间图、泄漏检查、预算 envelope。
5. 单独开 Issue 决定两项缺口：
   - durable independent annotations/adjudication ledger；
   - GEPA selection 是否需要 cluster weighting 或每簇 case 上限。
6. 达到正式 profile 和 readiness `ready` 后，才运行新的 Baseline vs GEPA；#225 的 6 簇 NO_OP 只能作为工作流证据，不能估计“705 条会减少多少”。

最终判断：**要用成熟的数据工程与标注治理方法，但不需要立即购买或部署成熟标注平台。先把 ReviewDesk 的 single-truth 之上补齐校准、双审、裁决、agreement 和事实簇权重；LLM 负责提速，人负责授权。**
