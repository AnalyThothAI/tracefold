# DSPy / GEPA 是否适合优化 Tracefold News Agent

> **决策状态（2026-08-22）**：本报告保留为采用前研究，不再代表实施决策。Operator 已在
> [#129](https://github.com/AnalyThothAI/tracefold/issues/129) 明确选择 D 型双 Predictor DSPy Program hard cut，
> 并授权将 Prompt-era 学习证据降为只读审计、从 `program_v1` epoch 重新积累。下文的 bounded-GEPA / 等待
> #121 建议因此被 supersede；引用的外部证据和风险分析仍保留供复盘。

日期：2026-08-21
结论类型：架构与采用决策，不是实施授权

## 0. 结论先行

**今天不应把 DSPy 接入 News 运行时，也不应在 #121 完成前启动自动 Prompt 搜索。**

以后可以采用，但边界应非常窄：把 standalone GEPA（或薄封装的 DSPy GEPA）放在离线、人工触发的
`NewsOptimizationWorkbench` 中，只负责提出 Prompt candidate；最终只导出普通纯文本 Prompt，继续由
Tracefold 现有 `CandidateEvaluator`、未来时间留出集、盲评、shadow、单臂 canary 和人工批准决定是否发布。

这不是保守地拒绝新框架，而是把它放在真正有 **Leverage** 的 **Seam**：

```text
冻结的 development evidence
        -> 离线候选搜索
        -> CandidateManifest
        -> Tracefold 原有发布证据链
```

不应采用的目标是：

```text
RabbitMQ -> Consumer -> DSPy Agent/ReAct -> decide() -> Delivery
```

当前 News 在线核心不是需要工具、规划或多步推理的 agent loop，而是“一个结构化语义阶段 + 确定性政策”。
DSPy 的主要价值来自用 metric 编译 instruction、demo 或模型权重，而不是把该语义阶段包进另一个运行框架。
DSPy 官方选择指南也把 optimizer 的可调旋钮归为 instruction、demo、weights，并明确优化需要先有可靠 program
与 metric；编译本身可能产生显著模型费用。[DSPy optimizer guide](https://dspy.ai/diving-deeper/choosing-an-optimizer/)

因此本报告的建议分成两句话：

1. **现在：NO-GO。** 先完成仍为 open 的 [Tracefold #121](https://github.com/AnalyThothAI/tracefold/issues/121)，
   用人工生成的 DRAM Prompt candidate 证明现有学习链第一次真实闭环。
2. **之后：BOUNDED GO。** 只做一次 development-only、显式预算、proposal-only 的 standalone GEPA pilot；
   不把 DSPy/GEPA 放入 production image，不自动批准或发布。

## 1. 当前系统究竟是什么

### 1.1 在线路径

当前在线语义判断路径由边界清楚的 **Module** 组成，其调用关系大致是：

```text
TriageConsumer
  |-- 读取 Event / Gate / 最后 4h told ledger
  |-- 构造 human_text
        -> TriageModel.judge(human_text)
        -> TriageCallResult(严格 TriageVerdict + 调用元数据)
  |-- final storyline key
        -> deterministic decide()
```

它已经集中隐藏了较多复杂性，具有较好的 **Depth** 与 **Locality**：

- `src/tracefold/news/agents/triage_model.py` 实现一个 structured-output 语义阶段；受控 retry/fallback 可能产生
  额外物理 provider request；
- `src/tracefold/news/models.py` 的 `TriageVerdict` 固定字段顺序、enum、长度、数值范围和 `extra="forbid"`；
- 快速可重试失败或非截断坏答案最多再试一次，并保留 deadline、fallback、circuit breaker、usage 与 finish reason；
- `src/tracefold/news/consumers.py` 构造 told ledger，在 storyline lock 内发现 ledger 变化时最多重问一次；
- 模型只表达 semantic intent；最终是否 push 仍由纯函数 `decide()` 和 policy 所有；
- prompt、schema、retrieval、model、execution contract、policy 都有独立 identity/hash；
- PostgreSQL 事实、RabbitMQ 传输、幂等写、投递和回滚不属于模型框架。

当前 system prompt 是一个已经高度校准的单体：13,477 字符、15,191 UTF-8 bytes。它同时包含新闻价值、
magnitude、direction、audience、中文 reader contract、novelty、told-ledger、格式和 prompt-injection 防御。
任意自动改写整段 Prompt 都可能同时改变质量规则和安全合同。

### 1.2 离线学习路径

`src/tracefold/news/candidate_evaluator.py` 已经实现比通用 Prompt optimizer 更重要的领域协议：

- 冻结 accepted `EventEvidenceSnapshot`；
- 一次 candidate 只改 Prompt 或 Policy 一个变量；
- stable/candidate 各自按时间顺序运行，因为某次送达会改变本臂后续 told ledger；
- development 之后必须使用 candidate 注册后才出现、生成器未见的 future holdout；
- blind pairwise、shadow、deterministic one-arm canary 依次过门；
- optimizer 无权改 accepted reviews、rubric、threshold、stable bundle 或 production assignment；
- UNKNOWN 不得冒充 PASS，provider/replay 缺失也不会产生空成功。

这意味着 Tracefold 已经拥有“评价与发布”的深 **Module**。真正缺少的只是一个可选的“候选搜索”
**Adapter**，而不是第二套 evaluator、truth plane 或 agent runtime。

## 2. DSPy / GEPA 能帮什么，不能帮什么

### 2.1 能帮的部分

GEPA 会用 task execution 的分数和自然语言反馈反思失败，再提出新的 instruction。DSPy 官方明确区分：
reflection 是 proposal mechanism，不是 evaluation mechanism；task model 负责评分，reflection model 负责提出修改。
这和 Tracefold 的“优化器可提案、不可批准”边界非常契合。
[GEPA in depth](https://dspy.ai/diving-deeper/gepa-in-depth/)

对 News 最有价值的输入不是价格 reward，而是 accepted review 中的：

- `first_bad_owner`；
- `factual_fidelity`、headline、direction、magnitude、why 等维度；
- `expected_correction`；
- must-push / must-hold；
- candidate-only schema、critical 或 injection failure；
- 具体输入、输出和 told-ledger trace。

这些可以形成 GEPA 所需的 actionable textual feedback，帮助它在一个明确 failure cluster 内搜索更好的规则表达。

GEPA 官方也支持既有自定义系统：可以使用 `gepa.optimize()` 加 custom Adapter，或使用
`optimize_anything` 优化任意文本，而不要求先把在线程序改写成 DSPy Module。
[GEPA integration guide](https://github.com/gepa-ai/gepa/blob/main/docs/docs/guides/index.md) ·
[GEPA FAQ](https://github.com/gepa-ai/gepa/blob/main/docs/docs/guides/faq.md)

### 2.2 不能替代的部分

DSPy/GEPA 不能替代：

- 哪些 review 是 accepted truth；
- temporal holdout 的隔离；
- 4 小时 arm-local told ledger 的顺序重放；
- 盲评文案质量与 safety veto；
- one Event/one arm 的 canary；
- `decide()`、幂等 delivery、rollback receipt；
- stable bundle 的最终人工所有权。

特别是，`dspy.GEPA` 当前的逐例 metric contract 是 scalar score + feedback；standalone `optimize_anything`
虽可按 task、按 metric 保留 Pareto 信息，这些分数仍只能作为 discovery surrogate，不能成为 Tracefold release
evidence。Tracefold 的发布真相是多维且带关键 veto 的；DSPy 也仍有开放的 multi-score optimizer feature
request，进一步说明不应把多维发布门压成一个“总分”。
[GEPA integration guide](https://github.com/gepa-ai/gepa/blob/main/docs/docs/guides/index.md) ·
[DSPy #8689](https://github.com/stanfordnlp/dspy/issues/8689)

价格反应同样不能成为 reward：它不证明因果，也不能替代 `should_push`、忠实度或读者价值。

## 3. 四种方案对比

| 方案 | 改动范围 | 优点 | 主要问题 | 决策 |
|---|---|---|---|---|
| A. 先完成 #121 人工候选 | 不加框架 | 先验证 evaluator 和真实闭环；因果最清楚 | 搜索速度仍依赖人工 | **现在采用** |
| B. DSPy 替换在线 Triage runtime | 热路径、renderer、schema、parser、retry/cache/audit | 未来可原生装载 DSPy program | 不是单变量实验；扩大故障面；无自动质量收益 | **拒绝** |
| C. DSPy sibling/shadow program | stable 保持，DSPy candidate 双跑 | 可直接比较完整 program | candidate 同时改变 Prompt/renderer/schema/execution；shadow 成本高 | **暂缓** |
| D. 离线 proposal workbench | development 与 candidate 注册之间 | 热路径零侵入；复用现有证据链；回滚简单 | 需 Prompt slot、安全、成本和 provenance 护栏 | **#121 后推荐** |

### 3.1 为什么完整 runtime 替换不是 Prompt 优化

DSPy Adapter 会把 Signature、instruction、field、demo 和 schema 转成消息，再解析返回值。即使业务上仍只调用
一个 predictor，wire request 也不再等于当前字节冻结的 LangChain request。默认 adapter、cache、retry、
settings/context 和 save/load 行为都会成为新的正确性表面。

因此一次迁移至少同时改变：

1. prompt rendering；
2. structured-output schema envelope；
3. parsing/validation；
4. cache/retry/error semantics；
5. token/finish metadata capture；
6. replay identity 与 artifact 格式。

这无法诚实地登记成现有 `target="prompt"` candidate。若未来确实要迁移，必须先新增独立的
`runtime_adapter` target，证明未编译 DSPy runtime 与 legacy 行为等价，再单独评估 `program_state`；这应另开 issue，
而不是借 Prompt 优化顺带上线。

当前 `TriageVerdict` 还依赖 `ge/le/max_length` 等 Pydantic constraints。DSPy 有开放 issue 报告
JSONAdapter 派生 structured-output model 时部分 Field constraints 被丢弃。它是风险信号而不是对所有 provider
都必然失败的证明，但已经足以要求：不把默认 DSPy parser 当作生产合同。
[DSPy #10195](https://github.com/stanfordnlp/dspy/issues/10195)

### 3.2 为什么不先做 sibling/shadow

一个完整 DSPy candidate 不只是新 Prompt，还可能包含 demos、program state、renderer 和 DSPy 版本。
当前 evaluator V1 不支持 `target="program"`，也没有独立证明这些变量的机制。

而且 full shadow 会为每个 eligible Event 再增加一次 candidate inference。它适合发布链后段验证一个已经过离线门的
shortlist，不适合作为搜索内循环。

## 4. 推荐架构

```text
ReviewDesk accepted reviews
        |
        v
freeze_dataset(role=development)
        |
        v
NewsOptimizationWorkbench                         [cold, manual CLI]
  |-- FailureClusterSelector
  |-- GepaPromptOptimizerAdapter                  [optional dependency]
  |-- PromptPatchCodec / PromptCandidateCompiler
  |-- Budget + provenance + leakage guards
        |
        v
full plain prompt + ProposalReceipt(generator_kind=model)
        |
        v
existing `tracefold news learning propose`
        |
        v
existing CandidateEvaluator                      [release authority]
  development sequential replay + blind pairwise
        -> candidate-unseen future holdout
        -> 24h shadow
        -> deterministic 10% one-arm canary
        -> human promotion / rollback
```

### 4.1 正确的 Module、Interface 与 Seam

建议新增的 **Module** 是 `NewsOptimizationWorkbench`，而不是 `DspyTriageAgent`。

它的最小 **Interface** 可以是：

```python
optimize(
    development_dataset_sha,
    failure_cluster_selection_policy,
    target_prompt_slot,
    explicit_budget,
) -> OptimizationReceipt
```

这个 Module 的 **Depth** 来自它隐藏：failure-cluster 的解析、校验和确定性选择、GEPA trajectory、Prompt 重组、
budget、provenance、leakage 检查和 shortlist 输出。调用者只提供选择策略或显式 override，不需要知道 GEPA 的
内部选项。

第三方库的正确 **Adapter** 是 `GepaPromptOptimizerAdapter`；测试时可替换成
`ScriptedPromptOptimizerAdapter`。真正的外部 **Seam** 仍是模型 provider；现有 live/record-replay model Adapter
继续拥有准确调用语义。这样把 GEPA 相关知识集中在一处，维持 **Locality**，也让删除该工具不影响生产。

### 4.2 第一版不要把正式 CandidateEvaluator 放入每轮搜索

正式 replay 对一个 Prompt candidate 的成本是：

- 最低 `2N` 次 semantic invocations：每个 case 的 stable 与 candidate 各一次；
- 预注册稳定性样本或首次分歧会让两臂各追加 trial 2/3；理论上升至 `6N`；
- provider retry/fallback 还可能增加物理请求。

因此 GEPA 内循环应使用 development-only、显式预算的 discovery episode；只把 1–3 个 shortlist 交给完整
CandidateEvaluator。否则 optimizer 生成几十个候选时，成本和耗时会被正式顺序 replay 成倍放大。

## 5. 必须先补的护栏

### 5.1 Prompt 只能改一个受控 slot

当前 Prompt 是 15,191 bytes 的单体。第一版不可授权 optimizer 改写全文。

需要一个 Tracefold 自有的 `PromptPatchCodec`：

- 固定 injection-defense、输入不可信声明；
- 固定输出 schema、enum、字段顺序和中文 reader contract；
- 固定 novelty/told-ledger 规则；
- 每次只开放一个 calibration slot，例如 “priced-in / expected evidence” 规则；
- 限制新增/删除 bytes，验证所有必需段存在；
- 检查新增文本与 development 新闻事实的长片段重合，降低记忆具体样本的风险；
- 最终重新组装成完整纯文本 Prompt 并计算标准 SHA。

DSPy 的“只优化 system prompt 某一部分”仍有开放 feature request，所以不能把这一安全边界寄托在框架默认行为上。
[DSPy #8637](https://github.com/stanfordnlp/dspy/issues/8637)

### 5.2 成本门要覆盖输入和总成本

当前 evaluator 虽记录 `input_tokens`，但 `candidate_token_cost_regression` 只比较 mean output tokens。
自动优化器可能把 Prompt 变长，输入费用显著上升却不触发失败。

在 pilot 前至少增加：

- Prompt UTF-8 bytes 增长上限；
- mean input tokens growth；
- mean output tokens growth；
- total tokens / provider cost；
- provider calls per semantic judgment；
- shadow/canary p50/p95 latency；
- schema、degraded、error rate。

建议 input 与 output 各自先沿用不超过 stable 10% 的保守线；是否调整只能由真实 evidence 决定。

### 5.3 预算必须显式且双重计数

`OptimizationBudget` 至少要固定：

```text
max_metric_calls
max_task_model_invocations
max_reflection_calls
wall_time_seconds
shortlist_size
```

不要把 `auto="light"` 当作费用保证。2026-08-20 的开放 issue 指出 `auto_budget()` 使用的假设与当前
reflection loop 不一致，改变 minibatch 大小也未正确进入估算。因此既要限制 GEPA metric calls，又要由
Tracefold 独立统计真实 task/reflection/provider calls 和 tokens。
[DSPy #10245](https://github.com/stanfordnlp/dspy/issues/10245)

### 5.4 数据隔离

- optimizer 只能读取冻结的 `role=development` dataset；
- 在 development 内可做 cluster-stratified search/train 切分，但二者都算“已见”；
- candidate 搜索结束并注册之后，才开始形成 future validation；
- optimizer 进程没有 validation/shadow/canary 或 stable-write 权限；
- market reaction 不进入 objective；
- GEPA score 命名为 `discovery_score`，绝不能命名为 PASS 或 release evidence。

### 5.5 可审计 artifact

每次 run 至少 hash/保存：

- stable bundle 与 development dataset SHA；
- optimizer/GEPA/DSPy 精确版本；
- seed、显式 budget 与实际消耗；
- reflection model、task model；
- proposer prompt/model/execution SHA；
- target slot、candidate lineage 和完整 diff；
- discovery score/feedback；
- shortlist 的完整 plain Prompt 与 Prompt SHA。

DSPy 官方建议优先保存 JSON state，而完整 program 使用 cloudpickle；版本不匹配通常只是 warning。
Tracefold 不应把 DSPy pickle 或 program state 当作 production trusted root，只应接受重新校验的 plain Prompt。
[DSPy save/load](https://dspy.ai/diving-deeper/saving-and-loading/)

候选恢复/保存也仍有开放 issue，因此更不应依赖 optimizer log 作为唯一业务 artifact。
[DSPy #8705](https://github.com/stanfordnlp/dspy/issues/8705) ·
[DSPy #8906](https://github.com/stanfordnlp/dspy/issues/8906)

## 6. 预期影响

| 维度 | 推荐离线 Workbench | 热路径 DSPy 替换 |
|---|---|---|
| 线上语义阶段 | 新增 0；仍是当前 structured stage | 理论可保持一个，但需防隐藏 retry/adapter fallback |
| 线上延迟 | 只受最终 Prompt 长度与行为影响 | renderer/parser/cache/context 都会改变 |
| RabbitMQ / PostgreSQL | 无 schema、queue、worker 改动 | 需扩展 program identity、recording/replay 与发布 target |
| production dependency | DSPy/GEPA 为 optional cold dependency | production image 与 worker 必须 pin DSPy |
| 质量潜力 | 更快搜索可读的 instruction candidate | 框架替换本身没有质量收益 |
| 因果归因 | 仍是 prompt-only 单变量 | Prompt、renderer、schema、execution 混在一起 |
| 回滚 | 删除工具或拒绝 candidate 即可 | 需保留 legacy runtime 与 program loader |
| 运维风险 | 主要是离线费用、泄漏、过拟合 | 新增热路径故障、兼容与审计风险 |

### 6.1 质量

潜在收益是更系统地探索人工可能遗漏的规则表达，并把多条 accepted failure 的共性反馈转成候选。
但没有任何理由预先承诺准确率提升：optimizer 可以过拟合、写长 Prompt、记忆具体事实，或修复一个 cluster
却破坏 retention/safety。真正的收益只能由未来时间留出和 canary 证明。

### 6.2 成本

推荐架构把搜索费用完全留在离线。正式运行的调用数不增加；candidate Prompt 可能改变 input/output tokens，
所以必须用新增成本门验证。

full shadow 会增加 candidate inference：若覆盖全部 eligible Event，语义判断负载大致增加一份 candidate arm；
应只在 shortlist 已过离线与 holdout 后运行。deterministic canary 是 one-arm assignment，不会为同一 Event 双调用；
总调用数大致不变，只是 stable/candidate token mix 改变。

### 6.3 维护与依赖

截至本报告快照，DSPy 最新稳定 release 为
[3.3.0](https://github.com/stanfordnlp/dspy/releases/tag/3.3.0)。项目活跃并采用 MIT，但 API 与 GEPA integration
仍在快速演进。若做 pilot，应精确 pin 版本并放在 optional extra；production Docker 不安装。

GEPA metric 的 async 支持还有开放 request，而现有 Tracefold model/evaluator 是 async；冷 CLI 可用受控同步边界或
custom Adapter 隔离这个不匹配，不应让它进入消费者并发路径。
[DSPy #9001](https://github.com/stanfordnlp/dspy/issues/9001)

## 7. 分阶段方案

### Phase 0：现在，完成 #121

1. 不安装 DSPy/GEPA；
2. 保持已冻结 stable bundle；
3. 收集现有 profile 要求的 boundary、retention、negative、safety evidence；
4. 人工生成 DRAM failure cluster 的单变量 Prompt candidate；
5. 跑完 development、future holdout、24h shadow、10% canary；
6. 证明 CandidateEvaluator 的第一轮真实学习，而不只是代码可运行。

### Phase 1：为自动候选补安全 Seam

1. Prompt 模块化或实现 `PromptPatchCodec`；
2. 增加 input/total token、Prompt bytes 和 provider-call guard；
3. 固定 optimizer run receipt 与 holdout-access attestation；
4. 为 Scripted Adapter 写 budget、cancel、leakage、non-Prompt-diff 测试。

### Phase 2：一次 bounded standalone-GEPA pilot

1. 人工触发，不建常驻 worker；
2. 只选一个重复且 owner 明确为 `triage_prompt/model` 的 failure cluster；
3. `use_merge=False`，显式小预算，shortlist=1；
4. 使用现有 exact Triage execution Adapter；
5. 输出可被现有 `learning propose` 消费的 plain-Prompt YAML；
6. 到 blind pairwise `review_required` 即停止，绝不自动推进。

### Phase 3：决定是否产品化

只有 pilot candidate 真实通过完整发布链，并且节省的人工搜索时间大于引入的维护成本，才把脚本收进
`NewsOptimizationWorkbench` CLI。仍不加入 production image，也不自动定时运行。

### Phase 4：是否评估 DSPy runtime

只有在多个独立 failure cluster 上，plain-Prompt search 已稳定获得收益，而需要 DSPy demos/program composition 的
增量价值又被新 evidence 明确指向时，才另开 runtime migration issue。先做 legacy-vs-uncompiled-DSPy 等价性实验，
再做 program-state 实验；不能与 Prompt 改动合并。

## 8. 正式采用门槛

建议同时满足以下条件才从“一次 pilot”升级为常规工具：

1. #121 完整结束并保留 PASS/FAIL/rollback receipts；
2. development dataset 达到当前 profile：boundary ≥30、retention ≥100、negative ≥50、自然日 ≥3、strata ≥3，
   且 safety set 完整；
3. 至少一个重复 failure cluster 有足够独立、accepted、Prompt-owned 证据；
4. GEPA candidate 在 blind pairwise 有 candidate win，且无 stable win、critical 或 injection regression；
5. future holdout 的预注册主指标 95% 区间下界大于 0；
6. input/output token 增长均不超过 10%，Prompt bytes 与 provider calls 过门；
7. shadow/canary 保持 schema、错误率、p95 latency 和 one-arm assignment 不变量；
8. production 仍只有当前 structured semantic stage，无 DSPy/GEPA runtime dependency，无自动 promotion。

前两次不同 failure cluster 的 pilot 都通过上述链条后，才值得把它视为可重复工具；这是本报告的工程建议，
不是当前仓库已经声明的业务阈值。

## 9. 对用户给出的 DSPy Issues 页应如何使用

GitHub Issues 很有价值，但应当作“兼容性与成熟度风险登记册”，不应当作架构设计文档或质量证明。

本次与 Tracefold 直接相关的开放风险是：

- auto budget 与当前 reflective loop 不一致：[#10245](https://github.com/stanfordnlp/dspy/issues/10245)；
- optimizer 多维分数仍在设计：[#8689](https://github.com/stanfordnlp/dspy/issues/8689)；
- GEPA async metric 缺口：[#9001](https://github.com/stanfordnlp/dspy/issues/9001)；
- 只优化部分 system prompt 的需求未成为稳定默认能力：[#8637](https://github.com/stanfordnlp/dspy/issues/8637)；
- Pydantic Field constraints 在一条 JSONAdapter 路径中的风险：
  [#10195](https://github.com/stanfordnlp/dspy/issues/10195)；
- GEPA candidate save/resume 的易用性与 bug：
  [#8705](https://github.com/stanfordnlp/dspy/issues/8705)、
  [#8906](https://github.com/stanfordnlp/dspy/issues/8906)。

这些 issue 的正确含义是“需要 pin、隔离、测试和自有 artifact”；不是“DSPy 不可用”，也不是“接入后会自动优化”。

## 10. 最终决策

**当前 News Agent 不应“改造成 DSPy Agent”。** 当前在线设计的小 Interface、深 Module 与 deterministic policy
边界是资产，不是障碍。

**#121 之后，可以把 standalone GEPA 包成离线 proposal Adapter。** 它只增加候选搜索的 Leverage，
不取得 evaluator、truth、发布或生产运行权。这个方案同时保留：

- 在线一个结构化语义阶段；
- Prompt-only 单变量因果归因；
- arm-local 顺序 told ledger；
- future holdout 的不可见性；
- blind review 与 critical veto；
- shadow/canary/rollback；
- 删除第三方优化器即可恢复原状的低耦合。

一句话：**先证明学习闭环，再自动化提案；用 GEPA 搜索 Prompt，不用 DSPy 重写 News。**

## 主要一手资料

- [DSPy optimizer selection](https://dspy.ai/diving-deeper/choosing-an-optimizer/)
- [DSPy GEPA in depth](https://dspy.ai/diving-deeper/gepa-in-depth/)
- [DSPy saving and loading](https://dspy.ai/diving-deeper/saving-and-loading/)
- [GEPA integration guide](https://github.com/gepa-ai/gepa/blob/main/docs/docs/guides/index.md)
- [GEPA FAQ](https://github.com/gepa-ai/gepa/blob/main/docs/docs/guides/faq.md)
- [Tracefold #121](https://github.com/AnalyThothAI/tracefold/issues/121)
- [DSPy 3.3.0 release](https://github.com/stanfordnlp/dspy/releases/tag/3.3.0)
