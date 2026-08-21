# DSPy 全量替换 News Triage：未来收益与效果上限

> **决策状态（2026-08-22）**：本报告是收益不确定性分析，不是当前 release gate。Operator 已在
> [#129](https://github.com/AnalyThothAI/tracefold/issues/129) 选择报告第 4 节的 D 型双 Predictor 架构，并授权
> 一次性替换旧生成 runtime、从 `program_v1` epoch 重新积累证据。文中的 A/B/C/D 反事实与 break-even
> 数字继续作为后续 Program candidate 的评估背景，不构成保留 LangChain compatibility 的要求。

日期：2026-08-22
问题边界：假设迁移本身不是问题，只比较未来收益；对照组是“保留现有单次结构化 Triage 运行时，使用 standalone GEPA 优化普通 Prompt”。

## 0. 结论

**即使把迁移成本视为零，现在也没有充分证据支持为了质量而把单次 Triage 全量替换为 DSPy。**

原因不是 DSPy 无效，而是最可靠的收益证据主要属于 **GEPA、MIPRO、few-shot、fine-tuning 或程序结构搜索这些优化器与搜索空间**，并不属于 DSPy 运行时抽象本身。standalone GEPA 已能把候选表示成多个文本 component、调用任意 evaluator、批量评分，并隔离 held-out test；所以，如果未来一年仍是一条 `event + gate + told ledger -> TriageVerdict` 的单 Predictor 路径，DSPy runtime 相对 standalone GEPA 的预期增量接近零。[GEPA `optimize_anything` 官方 API](https://gepa-ai.github.io/gepa/api/optimize_anything/optimize_anything/)

真正可能使全量替换产生收益的是搜索空间扩张：

1. 把一个判断拆成多个可独立优化的 Predictor；
2. 自动选择或生成每个 Predictor 的 demonstrations；
3. 在 Prompt 优化后继续 fine-tune 较小模型；
4. 未来让优化器搜索 program structure / dataflow，而不只是 Prompt 文本。

这些能力有真实但异构的证据；它们对 Tracefold 的可迁移性则有限。最接近的官方结构化分类教程把 GPT-4.1 nano 从 75.4% 提升到 87.0%，但它是英语、无状态、三个相互独立分类 Predictor、66/66/68 的 train/validation/test；Tracefold 则是联合的中文新闻判断，并且某条 Event 是否发送会改变同一实验臂后续 4 小时 told ledger。[DSPy Facility Support Analyzer 教程：数据和程序](https://raw.githubusercontent.com/stanfordnlp/dspy/main/docs/docs/tutorials/gepa_facilitysupportanalyzer/index.ipynb) · [测试结果](https://raw.githubusercontent.com/stanfordnlp/dspy/main/docs/docs/tutorials/gepa_facilitysupportanalyzer/index.ipynb#L3478-L3736)

因此强建议是：

- **把 DSPy 视为“未来 program-native 优化选项”，不是当前自动质量升级。**
- 先让 standalone GEPA 与现有专家 Prompt 建立真实效果基线；这会取得 instruction optimization 的绝大部分可得收益。
- 同时做一个等价的单 Predictor DSPy candidate，证明它与 plain-Prompt candidate 在同一 future holdout 上是否存在净增量。
- 只有当 demonstrations、两段式分解或小模型 fine-tuning 中至少一项在两个独立时间 cohort 上稳定超过本报告的 break-even 门槛，才把 DSPy program state 升为生产 **Module** 的正式身份。

## 1. 先把“DSPy 收益”拆开归因

DSPy 是一个可参数化 LM program 的运行与编译框架。原始论文把程序表达为带 typed Signature 的可组合 Module，再由 compiler 学习 demonstrations 等参数；论文报告的大幅提升发生在“程序 + compiler”组合，而不是对未优化 runtime wrapper 的消融。[DSPy 原始论文](https://arxiv.org/abs/2310.03714)

| 能力或已报道结果 | 收益真正来自哪里 | standalone GEPA 能否取得 | 全 DSPy 的增量 |
|---|---|---:|---:|
| 改写 instruction | GEPA/MIPRO 的搜索算法、metric、数据 | 能；单文本或多 component | 很小，主要是少写 Adapter |
| predictor-level feedback | 轨迹、分模块评分和 GEPA credit assignment | 能；component dict、自定义 evaluator | 中等工程 Leverage，不是必然质量收益 |
| typed inputs/outputs | Signature + Adapter | 可由现有 Pydantic/structured output 实现 | 维护性收益；Tracefold 已拥有大部分 |
| bootstrapped demonstrations | BootstrapFewShot/MIPRO + DSPy predictor state | 可手工实现，但没有同等原生流水线 | **真实的新增搜索空间** |
| Prompt + weights 联合优化 | BootstrapFinetune/BetterTogether | standalone GEPA 本身不能完成 | **中长期新增搜索空间** |
| 多 Predictor 联合优化 | program structure + optimizer | 多 component GEPA 可以优化文本；仍需自建 program | 中等；DSPy 的 Depth 与 Locality 更好 |
| 自动搜索结构/dataflow | GEPA full-program evolution + DSPy program representation | 可搜索任意代码，但 DSPy 提供可执行积木 | 高 option value，高方差、高治理成本 |
| 模型切换 | LM/Adapter abstraction + 为新模型重新 compile | 当前运行时也能换 provider | 开发速度收益，不自动带来质量收益 |
| tracing/save/load | DSPy callbacks、MLflow、program state | GEPA 自有 trajectory；Tracefold 已有 audit | 边际可观测性收益，非业务质量收益 |

关键事实是 standalone GEPA 已支持 `{component: text}` 的多 component candidate、opaque dataset、自定义单例或 batch evaluator、以及不会进入优化 server 的 test set。因此“多 Prompt、复杂 evaluator、held-out”本身不要求生产 runtime 使用 DSPy。[官方 `optimize_anything` API](https://gepa-ai.github.io/gepa/api/optimize_anything/optimize_anything/)

### 1.1 必须用四臂反事实回答，而不是比较框架名称

| 臂 | program/runtime | 可优化参数 | 这个臂回答什么 |
|---|---|---|---|
| A. current/manual | 当前 LangChain structured Triage | 当前专家 Prompt | 真实业务 baseline |
| B. standalone GEPA prompt | 仍是当前 runtime | 只优化一个 plain Prompt | 自动 instruction search 相对人工到底带来多少收益 |
| C. DSPy single-predictor | 一个 `Predict(TriageSignature)` | 同一 instruction-only GEPA 预算；无 demos | DSPy Signature/Adapter/program state 本身是否产生净增量 |
| D. DSPy multi-predictor | `EventSemantics -> ReaderCard` | 两个 Predictor 的 instructions；首轮无 demos | 分解与 predictor-level credit assignment 是否提高质量上限 |

三个差分必须分别解释：

- **B - A：optimizer effect。** 测 GEPA 的 instruction optimization；不能记作 DSPy runtime 收益。
- **C - B：runtime representation effect。** 测 typed Signature、DSPy renderer/Adapter 与 program state；这是“是否全量替换”的直接问题。必须尽量固定模型、预算、数据、metric、temperature 和 output contract，并记录两边实际 wire prompt。
- **D - C：program architecture effect。** 测多 Predictor 分解、intermediate Interface 与 per-predictor feedback；这才是 full DSPy 可能扩大的质量上限。第一轮不得同时加入 demos、fine-tuning 或 verifier，否则无法归因。

如果 D 胜出，再增加一个非正式的 D+demo follow-up，单独测 demonstration effect。当前 Tracefold evaluator 一次只正式比较 stable/candidate，可以依次完成 A:B、胜者:C、胜者:D；不必让四臂同时进入 live canary。

目前 primary sources 中**没有**受控实验固定 GEPA、模型、数据、metric 和 Prompt，只改变“standalone runtime vs DSPy runtime”。DSPy 官方教程说明 `dspy.GEPA` 内部使用 gepa-ai/gepa 实现；GEPA 官方 FAQ 也明确二者是同一实现，`dspy.GEPA` 源码所做的是把 DSPy predictors 映射成文本 components、构造 Adapter，再调用上游优化器。因此 B 与 C 的 optimizer core 不是两种算法。[DSPy GEPA 教程索引](https://github.com/stanfordnlp/dspy/blob/main/docs/docs/tutorials/gepa_ai_program/index.md) · [GEPA 官方 FAQ](https://gepa-ai.github.io/gepa/guides/faq/#whats-the-difference-between-gepa-aigepa-and-dspys-gepa) · [`dspy.GEPA` 源码](https://github.com/stanfordnlp/dspy/blob/main/dspy/teleprompt/gepa/gepa.py)

最接近 C - B 的来源观测来自 GEPA 论文的跨框架 baseline 控制：作者把 DSPy Signature、解析提示和 program structure 原样移植到 Trace，使用相同初始 Prompt 与数据；未优化 Trace baseline 与 DSPy baseline 的差异不超过 0.5 percentage point。它不是 LangChain-vs-DSPy 的直接实验，但没有显示 runtime abstraction 自带明显质量增益。[GEPA 论文跨框架控制](https://arxiv.org/pdf/2507.19457)

## 2. 当前 News 问题的结构决定了可得收益

当前 `TriageModel` 是一次 structured-output 调用，并在统一 deadline 内处理一次快速 retry、fallback 与 circuit breaker；消费者把 Event、Gate、watchlist 和 told ledger 组装成输入。[`triage_model.py`](../../src/tracefold/news/agents/triage_model.py#L208) · [`consumers.py`](../../src/tracefold/news/consumers.py#L775)

当前学习 **Module** 也不是独立同分布的逐行 evaluator。`CandidateEvaluator` 为 stable 和 candidate 分别维护 4 小时 receipt state；前一条的 verdict、`decide()` 结果与发送状态会改变后一条看到的 told ledger。[`candidate_evaluator.py`](../../src/tracefold/news/candidate_evaluator.py#L407) · [`candidate_evaluator.py`](../../src/tracefold/news/candidate_evaluator.py#L1310)

这形成三个重要约束：

1. **当前质量上限更可能受 rubric、证据和 temporal coverage 限制，而不是 Prompt 表达形式限制。** DSPy 不会凭空增加 accepted reviews，也不会自动知道“该推但未推”“事实忠实度”“中文读者价值”之间的优先级。
2. **一个 Event 的局部正确率不是完整目标。** 如果优化器不顺序重放 arm-local ledger，它可能改善单条 novelty，却让后续 storyline 重复发送。
3. **当前任务高度耦合。** assets、direction、magnitude、novelty、actionable、audience、中文 headline/why 之间共享同一事实解释。拆开可能提升每个字段的专注度，也可能丢失联合一致性。

DSPy 官方 `Evaluate` 的基本 **Interface** 是把每个 example 交给 program，再聚合逐例 metric，并可并行执行；这对普通分类方便，但不会自动表达 Tracefold 的顺序状态机。[DSPy Metrics and Evaluation](https://github.com/stanfordnlp/dspy/blob/main/docs/docs/diving-deeper/metrics-and-evaluation.md)

standalone GEPA 的 batch evaluator 接收一整个 evaluation stage 的 candidate/example pairs；也可以把一个时间序列当作 opaque example，调用现有顺序 replay。这反而让现有 evaluator 与搜索器之间的 **Seam** 更自然。[GEPA `batch_evaluator`](https://gepa-ai.github.io/gepa/api/optimize_anything/optimize_anything/)

## 3. 已有实证：哪些数字可以相信，哪些不能外推

### 3.1 Instruction optimization 的确有效，但这是 GEPA 收益

standalone GEPA 官方 Quick Start 只优化一个 system prompt，在 AIME-2025 上把 GPT-4.1 Mini 从 46.6% 提高到 56.6%（+10.0 points），预算为 150 metric calls。这个例子直接证明，大幅 instruction gain 不要求生产调用使用 DSPy runtime；它对应四臂中的 B - A，而不是 C - B。[GEPA 官方 Simple Prompt Optimization](https://github.com/gepa-ai/gepa#simple-prompt-optimization)

GEPA 的 ICLR 2026 论文在六项任务上报告：Qwen3-8B 的 aggregate 从 45.23 提升到 54.85；GPT-4.1 Mini 从 53.03 提升到 65.22。它对 Qwen 的总优化预算按任务约为 1,839–7,051 rollouts，显著少于论文对照的 24,000 GRPO rollouts。[GEPA 论文表 1、表 2](https://arxiv.org/pdf/2507.19457)

论文还报告，把在 Qwen3-8B 上优化的 Prompt 原样放到 GPT-4.1 Mini，六项任务 aggregate 比基线高 9.00 points；这是有价值的跨模型证据，但只是一个模型方向、六项非 News 任务，不证明 Prompt 对任意模型别名升级都可移植。[GEPA cross-model 结果](https://arxiv.org/pdf/2507.19457)

对 Tracefold 更接近的第一方示例是 Facility Support Analyzer：

- 输入为企业设施请求文本；输出 urgency、sentiment、categories；
- 三个独立 `ChainOfThought` Predictor；
- GPT-4.1 nano，66 train / 66 validation / 68 test；
- `auto="light"` 后测试 aggregate 75.4% -> 87.0%，即 +11.6 points。

[教程程序与数据切分](https://raw.githubusercontent.com/stanfordnlp/dspy/main/docs/docs/tutorials/gepa_facilitysupportanalyzer/index.ipynb) · [优化后结果](https://raw.githubusercontent.com/stanfordnlp/dspy/main/docs/docs/tutorials/gepa_facilitysupportanalyzer/index.ipynb#L3478-L3736)

这证明“分 Predictor + predictor feedback + GEPA”在一个小型结构化分类任务上能有大收益，但没有区分：如果完全相同的三个 Prompt 交给 standalone GEPA 多 component candidate，能否取得同样结果。因此它不是 full-runtime 相对 plain-Prompt 的增量证据。

同一 GEPA 论文的 system-aware merge 也说明多 component 的收益并不单向：GPT-4.1 Mini aggregate 从普通 GEPA 的 65.22 到 GEPA+Merge 的 66.36（额外 +1.14），但 Qwen3-8B 从 54.85 降到 52.40（-2.45）。Merge 不是“拆成两个 Predictor”的同义词，不过它足以反驳“更程序化/更多模块必然更好”。[GEPA 论文 system-aware merge](https://arxiv.org/pdf/2507.19457)

### 3.2 专家 Prompt 会显著压缩优化器的剩余空间

一项 2026 年针对 translation、terminology insertion 和 language quality assessment 的研究比较了专家手写 Prompt、base DSPy Signature 与 GEPA-optimized Signature，覆盖五组模型配置。GEPA 稳定抬高 minimal Signature，但大多数“专家 vs 优化后”差异没有统计显著性；某些任务专家更好，某些任务优化后更好。研究还发现，在其 linguistic tasks 上，统一单阶段通常优于分解后的多阶段，LQA detection F1 通常高 0.05–0.20。[linguistic Prompt 对照研究](https://arxiv.org/abs/2603.25169)

这比从弱 zero-shot baseline 出发的演示更适合作为 Tracefold 的先验：当前 Prompt 已是多轮人工校准、包含大量边界条件的专家 Prompt。对这种起点，合理预期应是“找出特定 failure cluster 的小幅提升”，而不是复制 10–30 points 的 benchmark gain。

该研究的语言方向仍全部 English-centric，并明确说结果未必推广到 non-English-centric directions；目前没有找到 DSPy/GEPA 对“英文新闻证据 -> 中文结构化读者文案 + 状态 ledger”的第一方 benchmark。因此中文新闻效果必须视为未证实。[同一研究的限制](https://arxiv.org/abs/2603.25169)

### 3.3 Demonstrations 是 full DSPy 最清楚的新增收益来源，但证据并不单向

MIPRO 论文在七项 program benchmark 上使用 500 train、500 dev 和最多 2,000 test，五次重复。论文发现，大多数任务中优化 bootstrapped demonstrations 优于 instruction-only；其中 HotPotQA test 从 zero-shot base 31.8 到 demonstration-only 45.8，MIPRO instruction+demos 为 46.4。另一方面，也有任务是 instruction-only 更适合，且论文明确承认复杂规则仍需好的 seed prompt。[MIPRO 论文](https://arxiv.org/pdf/2406.11695)

当前 DSPy 官方选择指南给出更谨慎的经验总结：demo tuning 倾向 overfit，instruction tuning 倾向更好地泛化；多数团队从 prompt-only 开始，plateau 后才考虑 fine-tuning。[DSPy Optimizer Guide](https://dspy.ai/diving-deeper/choosing-an-optimizer/)

对 Tracefold 的推论：demonstrations 可能帮助稳定 magnitude、direction、中文 headline 风格和边界分类，尤其是在较小 task model 上；但它们也会：

- 占用本已较长的输入 context；
- 把具体资产、地缘事件或旧 reader contract 带入新 Event；
- 在 told ledger 分布改变时过拟合；
- 让 Prompt identity 从一个文本 SHA 扩展为 instruction + demo set + ordering + renderer。

因此 demos 是值得实验的新增搜索空间，不是已证明的净收益。

### 3.4 Fine-tuning 是两至三年最有价值的 option，但需要完全不同的数据规模与治理

BetterTogether 论文在 multi-hop QA、数学推理和 feature classification 上，用 Mistral-7B、Llama-2-7B、Llama-3-8B 比较 Prompt 与 weights 的联合优化；组合策略平均可比 weight-only 高最多 60%，比 prompt-only 高最多 6%。这是“DSPy program state 能承载 Prompt + weights 联合优化”的直接证据，但不是金融新闻、中文生成或 frontier API model 的证据。[BetterTogether 论文](https://arxiv.org/abs/2407.10930)

DSPy 当前 `BootstrapFinetune` 会从 program traces 构建每个 Module 的训练数据；官方文档列出的支持 provider 包括 Local、Databricks 和 OpenAI。这个能力的最大未来价值不是把当前模型再提高一点，而是把已优化的行为蒸馏到更便宜、可固定 snapshot 的小模型。[DSPy optimizer overview](https://github.com/stanfordnlp/dspy/blob/main/docs/docs/learn/optimization/optimizers.md) · [BetterTogether API](https://github.com/stanfordnlp/dspy/blob/main/docs/docs/api/optimizers/BetterTogether.md)

不过，当前 accepted evidence 数量和事件覆盖远小于成熟 supervised adaptation 所需的分布覆盖；而且金融新闻漂移很快。没有 evidence 支持现在就估算具体倍数的成本下降。

### 3.5 Full-program evolution 显示很高上限，也显示很高方差

GEPA 官方 full-program evolution 教程把一段 `ChainOfThought` 数学程序的 test accuracy 从 67.1% 提升到 93.2%，使用 350 train、200 validation、487 test 和 2,000 metric calls；候选不只改 Prompt，还能改 DSPy program 源码、增加 deterministic post-processing 或新 Predictor。[GEPA DSPy Full Program Evolution 教程](https://gepa-ai.github.io/gepa/tutorials/dspy_full_program_evolution/)

这是 full DSPy 最大的三年 option value：搜索“什么步骤应该由 LM 做、什么应该由 Python 做、是否分解、是否增加 verifier”。但这项结果是单个官方教程、没有多随机种子统计，而且数学答案的规范化远比中文新闻价值容易验证。对 Tracefold，允许优化器改 control flow 或 Python 会跨越当前 trusted root；它必须作为新 program candidate 走代码审查和新的 release target，不能与 Prompt candidate 混为一谈。

## 4. 假设迁移免费，最强的 full DSPy 未来架构是什么

如果决定充分利用 DSPy，而不是只把现有调用包一层，最有希望的设计是两个语义 Predictor，加一个确定性合成器：

```text
NewsTriageProgram                                      [Module]
  Interface: triage(TriageContext) -> TriageVerdict

  EventSemantics                                      [Predictor A]
    event + gate + told ledger
      -> assets, scope, direction, magnitude,
         novelty/restates, actionable, decision intent

  ReaderCard                                          [Predictor B]
    original event + frozen EventSemantics
      -> audience, headline_zh, why_zh, title_zh

  VerdictAssembler                                    [deterministic]
    schema validation + cross-field invariants
      -> TriageVerdict

  decide()                                            [existing deterministic policy]
```

这个 **Module** 保留一个稳定业务 **Interface**，把 DSPy program state、每个 Predictor 的 Signature、demos、task model 和 Adapter 隐藏在内部，形成较好的 **Depth**。第三方 provider 与 DSPy LM 之间仍是一条外部 **Seam**；Tracefold 应保留自己的 provider **Adapter**，而不是让 DSPy 的全局设置泄漏到消费者。

它比当前单 Prompt 的潜在 **Leverage** 在于：

- semantic correctness 与中文 reader writing 可以用不同 metric、different feedback、甚至不同模型；
- GEPA 可以把失败归因到具体 Predictor，而不是在一段 13k 字符级规则中猜修改位置；
- demos 可以只进入需要它们的 Predictor；
- 将来可只 fine-tune `EventSemantics`，保留更强模型写中文；
- 每个 Predictor 的 instructions、demos、model 和 Adapter 都可以独立版本化。

它的 **Locality** 也更好：magnitude/novelty 问题主要落在 `EventSemantics`，文案问题主要落在 `ReaderCard`。DSPy 的 Adapter 能让同一 Signature 在 Chat、JSON、XML 或 two-step extraction 之间切换；官方文档也说明 JSONAdapter 会优先使用 provider native structured output。[DSPy Adapter lifecycle](https://dspy.ai/diving-deeper/adapters/)

但它不应继续拆成 assets、direction、magnitude、novelty、headline 五六个独立 LM Predictor。linguistic study 已显示过度分解可能降低统一判断；而 Tracefold 的这些字段强耦合。两个 Predictor 是质量与成本之间较合理的最深边界，不是“模块越多越好”。[linguistic Prompt 对照研究](https://arxiv.org/abs/2603.25169)

## 5. 与 hybrid/plain-Prompt 对照：full runtime 实际多得到什么

### 5.1 保持一个 Predictor 时

若 full DSPy 仍只是：

```python
dspy.Predict(TriageSignature)
```

并只让 GEPA 改 instruction，那么它与 standalone GEPA 改完整 Prompt 的有效搜索空间高度重叠。DSPy 的 Signature、Adapter、save/load 会提高开发便利，但没有可引用的实验表明“相同模型、相同 wire prompt、相同 metric、相同候选”因为运行在 DSPy 内就更准确。

现有证据只支持把 **C - B 的零增量作为起始假设**，不支持为 Tracefold 给出效果预测。工程规划可以用 `-0.5 / 0 / +0.5 point` 做敏感性压力测试；这个带宽借用了论文跨框架 baseline 的量级，但不是其结论，也不是 Tracefold 的预测或准入阈值。

### 5.2 采用两个 Predictor 时

新增收益来自分解、per-predictor feedback、不同 demos/model，而不是 runtime 名称。standalone GEPA 也能优化两个 component，但 full DSPy 会减少自定义 trace attribution、renderer、program serialization 的实现量。

效果可能是双向的：

- 正向：降低一次调用同时完成 15+ 字段和中文写作的认知负担；让 failure feedback 更精准；
- 负向：第二步继承第一步错误；失去一次联合生成的跨字段 coherence；多一次调用增加随机性。

structured tutorial 给出正向方向，linguistic study 则给出分解可能变差的反例。因为两者任务都不是 Tracefold，本报告**不把它们合成为 D - C 的效果预测**。如需做容量与风险规划，可用 `-2 / 0 / +4 points` 作为失败、中性、成功三档压力测试；它们只是决策敏感性输入，不能用于宣称预期收益。

### 5.3 启用 demonstrations 时

相对 standalone instruction-only GEPA，DSPy 的原生 demo bootstrap/selection 是最现实的短中期新增能力。MIPRO 只提供“可能正向”的方向性证据，无法转成 Tracefold 的百分点。规划时可用 `0 / +1 / +3 points` 做敏感性档位，同时单独测 input tokens；这些数字不是预测。

### 5.4 启用 fine-tuning 或 program evolution 时

这里 full DSPy 才真正打开另一类 Pareto frontier：

- 同质量、更低单位成本；
- 同成本、更稳定的 schema/边界遵循；
- Prompt + weight 联合适配；
- 自动发现 deterministic post-processing 或有价值的分解。

但这依赖数百到数千个覆盖足够失败 strata 的 accepted examples、稳定 reader contract、可 fine-tune model，以及新 program target 的治理。它属于两至三年 option，不是当前可兑现收益。

## 6. 质量上限、样本效率与迭代速度

### 6.1 质量上限

质量上限由四层构成：

1. evidence 是否足以判断；
2. review rubric 是否能表达业务价值；
3. metric 是否与人工 should-push/文本质量一致；
4. program/Prompt/model 是否能学会。

DSPy 主要改善第 4 层，并通过更清楚的 Signature 和 tracing 帮助第 3 层。它不会修复 provider facts、grounding、told-ledger truth 或 accepted labels。对当前系统，先改善前三层通常比更换运行时有更高 Leverage。

### 6.2 样本效率

GEPA 的强项是从自然语言 feedback 中提取高层规则。论文报告某些任务仅用 79–737 个 train rollouts 达到最佳表现，但包含 candidate selection 的总 rollout 通常仍是数千。[GEPA sample-efficiency 分析](https://arxiv.org/pdf/2507.19457)

Tracefold 的 review 比 exact-match 更富含 feedback，这对 GEPA 有利；同时 label 数更少、分布更时变、状态依赖更强，这对泛化不利。最有效的数据单位不应是随机 Event，而应是 fact-cluster / failure-cluster / sequential episode。

### 6.3 迭代速度

full DSPy 的真正速度收益是把修改单位从“人工编辑大 Prompt”变成“改 Signature/Module，compile，保存 program state”。DSPy 可用 callback 捕获 module、LM、Adapter、Evaluate 和 compile 生命周期，也能接 MLflow tracing。[DSPy Observability](https://dspy.ai/tutorials/observability/)

然而，standalone GEPA 也返回 candidate pool、eval log、Pareto data 与 test scores；Tracefold 已有 frozen dataset、hash、arm replay 和 release chain。故迭代速度差主要发生在未来多 Predictor/demos/fine-tune 阶段，而不是今天的单 Prompt 阶段。

## 7. 模型可移植性、成本、延迟、可复现性

### 模型可移植性

DSPy Adapter 将 Signature 与具体 prompt shape 分开，同一 **Interface** 可选择 ChatAdapter、JSONAdapter 或 TwoStepAdapter。这提高换模型的工程速度。[DSPy Adapters](https://dspy.ai/diving-deeper/adapters/)

但“可运行”不等于“质量可移植”。GEPA 有 Qwen -> GPT-4.1 Mini 的正向 cross-model 证据；MIPRO 论文则明确把不同 proposer/task model 的一致性列为未来研究。安全做法仍是每个 model snapshot 重新 compile 和 future-holdout 验证。[GEPA cross-model 结果](https://arxiv.org/pdf/2507.19457) · [MIPRO limitations](https://arxiv.org/pdf/2406.11695)

### 在线成本和延迟

| 形态 | LM calls / Event | 相对当前的效果 |
|---|---:|---|
| 单 Predictor DSPy | 通常 1 | 理论上近似当前；Adapter parse fallback 可能额外请求 |
| 两 Predictor 串行 | 2 | token 与请求成本约为两步之和；p95 latency 接近两步 latency 之和 |
| 三个独立 Predictor 并行 | 3 | wall latency 可接近最慢一步，但 inference cost 约三份 |
| demos | calls 不变 | input token、cache footprint 上升 |
| fine-tuned 小模型 | calls 不变 | 有机会显著降单位成本，但无 Tracefold 数据可量化 |

DSPy 的 ChatAdapter 默认在 parse failure 时重新请求 JSONAdapter；TwoStepAdapter 固定使用第二次 extraction call。因此如果目标是最低 latency，必须显式选择 JSON/native structured output 和禁用隐式 fallback。[DSPy Adapter design](https://dspy.ai/diving-deeper/adapters/)

### 优化成本

GEPA 论文在 GPT-4.1 Mini 的六任务表中报告总实验费用：GEPA $86、GEPA+Merge $67、MIPROv2 $76；这些是论文那组模型、数据与预算的总数，不能直接换算 Tracefold，但说明优化成本是一次性可控而非免费。[GEPA cost appendix](https://arxiv.org/pdf/2507.19457)

对 Tracefold，正式 sequential two-arm replay 的一次 candidate 远贵于普通逐例 metric；搜索内循环应使用 development episode，只有 shortlist 进入完整 evaluator。

### 可观测性与可复现性

DSPy state-only JSON 会保存 instruction、demos、LM config 和 predictor state，便于 diff；full program 使用 cloudpickle。官方文档说明版本 mismatch 只 warning、仍继续 load，callbacks/history 不保存。[DSPy Saving and Loading](https://dspy.ai/diving-deeper/saving-and-loading/)

因此 full DSPy 能改善 module-level trace，却不能替代 Tracefold 的强 reproducibility contract。production identity 仍应显式 hash：DSPy/GEPA version、program source、Signature/schema、Adapter、instruction、demo content/order、LM snapshot、temperature、cache policy 和 execution contract。DSPy JSON 是 program artifact，不是 trusted truth。

## 8. 一年、两年、三年的 option value

### 未来 1 年：高概率仍是单 Predictor

最可能的收益来自 GEPA 对 failure clusters 的 instruction refinement，以及更好的 evaluator 数据。standalone GEPA 能取得这一主收益。full DSPy 的 option value 主要是建立 Signature/Module vocabulary 和做等价性候选。

**判断：full runtime 的业务质量增量低；开发平台增量中等。**

### 未来 2 年：demos、双 Predictor、模型分工

如果 accepted reviews 覆盖稳定，可能把 semantics 与 reader writing 分离，给两个 Predictor 不同 feedback/demos/model。DSPy 的 **Depth**、predictor state 与 optimizer composition 开始产生实际 Leverage。

**判断：full runtime 的条件价值中等；前提是单 Predictor 已出现可测 plateau。**

### 未来 3 年：fine-tuned 小模型与 program evolution

如果数据规模足够、reader contract 稳定且需要控制 provider 成本，DSPy 的 BootstrapFinetune/BetterTogether 与 program evolution 可能同时优化 Prompt、weights 和结构。这是 full replacement 最大的长期 option。

**判断：上行很高，但发生概率取决于 Tracefold 是否真的演变成 multi-Predictor / own-model system。** 如果产品继续坚持一次 frontier structured call，这个 option 不会兑现。

## 9. 三年情景敏感性：明确不是效果预测

下面的权重是为了问“哪种未来值得买 option”的主观产品情景，不是统计概率；效果栏是压力测试边界，不是期望值、置信区间或来源观测。“points”仅指 Tracefold 自有 future-holdout primary quality score 的百分点。

| 三年场景 | 敏感性权重 | full DSPy 相对 standalone GEPA + 当前 runtime 的规划压力测试 | 主要来源 |
|---|---:|---:|---|
| 保持一个结构化 Predictor | 55% | -0.5 到 +0.5，中心 0 | runtime abstraction 本身不增加搜索能力 |
| 形成两 Predictor + demos | 30% | -1 到 +4，中心 +1 | 更好的 credit assignment、demo/model 分工；也有分解损失 |
| 进入 fine-tune / program evolution | 15% | 0 到 +6，中心 +3 | weights、结构和 deterministic logic 的新增搜索空间 |

本报告刻意不把这些数字相乘成“预期 +X points”：没有中文 News、动态 ledger 的外部有效性证据，这样的加权会制造伪精度。它们只用于验证决策是否稳健：即便采用乐观档，收益也主要来自 D 的新搜索空间；若系统保持单 Predictor，C 相对 B 没有证据支持显著上行。

工程 option value 可能高于质量增量：一旦 program 有两个以上 Predictor，DSPy 会减少自建 prompt renderer、demo state、per-predictor trace 和 optimizer composition 的工作。不过这属于 iteration velocity，不能作为质量提升来计数。

## 10. 来源观测、Tracefold 准入目标与 Break-even

下表严格分开两类数字。左列是第一方来源在其自身任务上实际报告的结果，不能成为 Tracefold 效果承诺；右列是本报告为 Tracefold 建议的治理阈值，必须由本地 sealed future holdout 产生。

| 比较 | 来源观测（外部任务） | Tracefold 准入目标（治理建议，不是预测） |
|---|---|---|
| B - A：optimizer effect | standalone AIME 46.6% -> 56.6%（+10.0）；GEPA 六任务 aggregate +9.62 / +12.19 | primary quality >= +1.5 points，两个不重叠 future cohorts 同向；critical safety 不退步 |
| C - B：runtime representation effect | 无直接实验；最接近的 Trace-vs-DSPy 未优化 baseline 差异 <= 0.5 point | 若仅为质量替换：>= +1.0 point；否则质量非劣且过去 6 个月满足维护性门槛 |
| D - C：program architecture effect | Facility Support +11.6，但无 C 对照；linguistic unified 比 decomposed 高 0.05–0.20 F1；GEPA Merge 对两模型为 +1.14 / -2.45 | primary quality >= +2.0 points，两个 cohorts；critical safety 不退步，p95 latency <= +50%，单 Event 成本 <= +30% |
| D+demo - D：demonstration effect | HotPotQA 31.8 -> 45.8（demo-only），instruction+demos 46.4 | primary quality >= +2.0 points，两个 cohorts；总 token 成本 <= +20% |

这些准入目标不是 DSPy 官方标准，也不是对未来结果的估计；它们表达的是“多引入一个自由度至少应换回多少业务价值”。其中 **C - B 是全量 runtime 替换的直接 break-even**；D - C、D+demo - D 则衡量只有 program-native 路线才容易取得的 option。

满足以下任一条，full DSPy 才从“保留 option”变成“应该成为生产 runtime”：

1. **C - B 直接门槛**：单 Predictor DSPy 若只声称质量收益，需在两个不重叠 future cohorts 上提高 primary quality >= 1 point；若只声称平台收益，则质量必须非劣，且过去六个月至少发生两次模型迁移或至少两个 Predictor 需要独立修改。
2. **D - C 双 Predictor 门槛**：`EventSemantics -> ReaderCard` 相对最佳单 Predictor 达到上表阈值。
3. **Demonstration 门槛**：MIPRO/BootstrapFewShot candidate 相对 instruction-only GEPA 达到上表阈值。
4. **模型迁移门槛**：DSPy-compiled 较小/可固定 snapshot 模型达到 quality non-inferiority（下界不差于 stable 1 point），同时单位 Triage 成本下降 >= 30%。
5. **Program evolution 门槛**：结构 candidate 相对所有 Prompt-only candidates 提高 >= 3 points，经过代码审查，且不改变 `decide()` ownership、事实 plane 或 arm-local sequential replay。

这些阈值是本报告的治理建议，不是 DSPy 官方规定。

## 11. 最终建议

**不要因为“DSPy 未来可能更强”就直接把当前单调用换成 DSPy；要让 full DSPy 赢得生产身份。**

推荐的验证顺序是：

1. 同一 frozen development / future holdout 上比较：专家 stable、standalone GEPA plain Prompt、单 Predictor DSPy+GEPA；这一步隔离 runtime 是否有任何净增量。
2. 若前两者 plateau，再比较两个 Predictor DSPy program；不要先拆五六个字段。
3. 只有双 Predictor 证明收益后，才试 demos；只有 accepted evidence 达到足够覆盖后，才试 fine-tuning。
4. 任何 DSPy candidate 都继续通过 Tracefold 的 sequential replay、temporal holdout、blind pairwise、shadow、one-arm canary 和人工 promotion；DSPy optimizer 永远不拥有发布权。

若用户的真实战略判断是“未来 News 会变成多个语义 Predictor、需要小模型蒸馏和自动 program search”，那么**现在建立 DSPy-compatible program candidate 是正确的 option investment**。若战略仍是“一次 frontier structured judgment + deterministic policy”，那么**standalone GEPA 已经覆盖大部分可得收益，全量 runtime 替换不会显著提高未来效果**。
