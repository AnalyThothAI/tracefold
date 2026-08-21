# Agent 持续学习生态调研：Tracefold Issue #112 的 Build vs Buy 决策

> 调研日期：2026-08-21（Asia/Taipei）
> 范围：只使用项目官方仓库、官方文档或原论文；所有关键页面均打开正文或源码核验，不以搜索摘要作证据。
> 决策对象：[Tracefold Issue #112](https://github.com/AnalyThothAI/tracefold/issues/112) 与 [《AI Agent 架构》Chapter 9](https://bojieli.github.io/ai-agent-book/book/chapter9/)。

## 结论先行

GitHub 上没有一个成熟库能直接提供 Issue #112 要的完整生产学习闭环。现有产品分别擅长“做实验”“看轨迹”“优化 Prompt”或“训练模型”，但没有一个同时理解 Tracefold 的以下业务真相：

1. 是否送达必须由真实 `sent` receipt 决定，而不是模型说了 `push`；
2. 一个候选改变了前一条推送后，下一条看到的 reader ledger 也随之改变，因此必须做 stable/candidate **两条独立的顺序 replay**；
3. eventless miss、事实簇、storyline throttle、单 Event 单卡、绝不双发等是 News V3 的领域约束；
4. 发布不是“离线分数更高”，而是 immutable evidence → hidden temporal holdout → shadow → durable canary assignment → rollback receipt；
5. judge、候选生成器和生产 stable pointer 不能由同一个 Agent 自证、自改、自发。

所以正确决策不是“再造一套通用 eval 平台”，也不是“接入一个会自动改 Prompt 的库”，而是：

- **BUILD**：继续以 Issue #112 的 `ReviewDesk` 和 `CandidateEvaluator` 为两个深 Module；自建领域真相、顺序双臂 evaluator、可信根、shadow/canary/rollback evidence。
- **WRAP**：仅用很薄的适配层接入可替换能力。优先考虑 OpenTelemetry GenAI 元数据导出；需要 UI 时可选 Phoenix 作为只读 observer；未来数据足够时可让 GEPA/DSPy 只生成离线候选。
- **BORROW**：采用 OpenAI 的人机校准评测方法、LangSmith 的 trace→dataset 工作流、τ³-bench 的 final-state/version-pin 思想、Phoenix/MLflow/Braintrust 的版本和 review UX。
- **REJECT**：不依赖即将关闭的 OpenAI Evals 平台；不让 LangSmith、MLflow、Phoenix、Braintrust 的远程 Prompt alias 成为生产 stable pointer；不采用 Reflexion/TextGrad/Agent Lightning 直接改生产；不把 1h 涨跌当 reward。

这不是保守，而是 KISS：只自建无法外包的业务语义，把通用可观测性和候选搜索留成可插拔 adapter。

## 1. “持续学习”在这里到底指什么

很多仓库把下面任何一件事都叫 learning：保存 trace、让模型写 reflection、用另一个 LLM 打分、优化一段 Prompt、训练权重。它们都可能有用，但都不足以证明生产系统学会了。

Issue #112 的定义更严格，也更接近 Chapter 9：

```text
真实且不可变的生产证据
  → 分层抽样与人工/确定性判断
  → 定位 first_bad_owner
  → 单变量候选
  → stable/candidate 独立顺序 replay
  → 未见过的 temporal holdout
  → shadow 验证运行兼容性
  → 有界 canary 验证生产安全
  → 人工批准、正常 Git/部署发布
  → 可证明的 rollback
```

上线后每一轮“学到东西”都必须留下四类证据：

- **输入证据**：模型当时实际看到什么，而不是事后被更新过的 Event；
- **评价证据**：谁、按哪个 rubric、对哪个版本做了什么判断；
- **比较证据**：stable 与 candidate 是否在同一冻结样本上公平比较；
- **发布证据**：哪个 candidate、以什么比例、在何时生效，为什么推进或回滚。

若缺任何一层，系统最多是在“试”，还没有形成生产学习。

## 2. 调研方法与能力判定口径

### 2.1 一手来源

本报告查阅了：

- 每个项目的官方 GitHub README、相关实现或官方产品文档；
- GitHub Releases/API 中的 release、push、archive 状态；
- OpenAI、LangSmith、MLflow、Phoenix、Braintrust、OpenTelemetry 官方文档；
- DSPy、GEPA、TextGrad、Reflexion、Agent Lightning、τ-bench/τ³-bench 的官方仓库或原论文。

维护状态是 **2026-08-21 的快照**，不是对未来维护的保证。Star 只作社区采用信号，不当作正确性或生产成熟度证据。

### 2.2 六项能力的严格定义

下表不把“用户可以自己写代码实现”算成产品已提供：

| 能力 | 本报告记为“提供”的最低条件 |
|---|---|
| Dataset/version | 数据集有可钉住的版本或不可变 snapshot，实验能引用该版本 |
| Pairwise | 系统原生支持同 case 两臂比较；普通 scalar score 不算 |
| Human review | 有任务分配、提交、纠正/审计或 annotation queue；手写 CSV 不算 |
| Production/online | 能接生产 trace/反馈并在线抽样评估；只跑本地 fixtures 不算 |
| Shadow/canary | 能让 candidate 在不影响用户或只影响确定比例流量的条件下运行，并记录 arm assignment |
| Rollback | 能将已发布版本恢复到先前稳定版本并保存可审计发布状态；重新 `git checkout` 不算平台能力 |

符号：✅ 原生且有官方文档；△ 部分提供、实验性或需自建关键部分；— 未发现一手来源证明。

## 3. 生态全景：能力、成熟度与维护状态

### 3.1 维护快照

| 项目 | 2026-08-21 状态 | 成熟度判断 |
|---|---|---|
| [Promptfoo](https://github.com/promptfoo/promptfoo) | 24.4k stars；未归档；当天有 push；[0.122.0](https://github.com/promptfoo/promptfoo/releases/tag/0.122.0) 发布于 2026-08-04 | 成熟的 eval/red-team CLI；不是生产发布控制面 |
| [OpenAI Evals](https://github.com/openai/evals) | 19.2k stars；未归档；最后 push 2026-04-14 | 方法仍有价值；托管 Evals 平台已进入退役时间表 |
| [DSPy](https://github.com/stanfordnlp/dspy) | 37.5k stars；2026-08-20 push；[3.3.0](https://github.com/stanfordnlp/dspy/releases/tag/3.3.0) 发布于 2026-08-03 | 成熟且活跃的声明式 LM 程序/优化框架 |
| [GEPA](https://github.com/gepa-ai/gepa) | 6.2k stars；2026-08-20 push；[v0.1.4](https://github.com/gepa-ai/gepa/releases/tag/v0.1.4) 发布于 2026-07-15 | 活跃但年轻的反思式候选搜索器 |
| [TextGrad](https://github.com/zou-group/textgrad) | 3.7k stars；最后 push 2025-07-25；[v0.1.6](https://github.com/zou-group/textgrad/releases/tag/v0.1.6) 发布于 2024-12-15 | 有正式论文的研究框架；工程维护明显弱于 DSPy/GEPA |
| [Reflexion](https://github.com/noahshinn/reflexion) | 3.2k stars；最后 push 2025-01-14；无 GitHub Release | 研究代码与实验记录，不是生产库 |
| [Agent Lightning](https://github.com/microsoft/agent-lightning) | 17.6k stars；当天有 push；[v1.0.0](https://github.com/microsoft/agent-lightning/releases/tag/v1.0.0) 发布于 2026-08-17 | 活跃、能力强，但 v1.0 很新且面向 Agent 强化学习/权重训练 |
| [LangSmith SDK](https://github.com/langchain-ai/langsmith-sdk) | 1.0k stars；当天有 push；[v0.11.1](https://github.com/langchain-ai/langsmith-sdk/releases/tag/v0.11.1) 发布于 2026-08-19 | 成熟托管评测与观测产品的 SDK |
| [MLflow](https://github.com/mlflow/mlflow) | 27.6k stars；当天有 push；[v3.15.1](https://github.com/mlflow/mlflow/releases/tag/v3.15.1) 发布于 2026-08-03 | 成熟 OSS 平台；GenAI 能力广但平台面较重 |
| [Arize Phoenix](https://github.com/Arize-ai/phoenix) | 11.1k stars；当天有 push；[v20.3.0](https://github.com/Arize-ai/phoenix/releases/tag/arize-phoenix-v20.3.0) 发布于 2026-08-17；Elastic License 2.0 | 成熟活跃的 source-available trace/eval/dataset UI；不是 MIT/Apache 意义上的 OSS |
| [Braintrust Python SDK](https://github.com/braintrustdata/braintrust-sdk-python) | 商业产品 SDK；当天有 push；[v0.34.0](https://github.com/braintrustdata/braintrust-sdk-python/releases/tag/py-sdk-v0.34.0) 发布于 2026-08-17 | 完整商业 workbench；关键 human review 能力受套餐约束 |
| [OpenTelemetry GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai) | 2026-05-05 新建；当天有 push；无 release；状态为 Development | 方向正确但规范仍不稳定；只是 telemetry vocabulary |
| [τ³-bench / tau2-bench](https://github.com/sierra-research/tau2-bench) | 1.8k stars；2026-08-18 push；[v1.0.1](https://github.com/sierra-research/tau2-bench/releases/tag/v1.0.1) 发布于 2026-07-22 | 成熟 benchmark/simulator 思路；不是生产学习平台 |

### 3.2 能力矩阵

| 方案 | Dataset/version | Pairwise | Human review | Production/online | Shadow/canary | Rollback |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| Issue #112 目标架构 | ✅ 内容寻址 manifest | ✅ 盲测双臂 | ✅ append-only ReviewDesk | ✅ 真实 receipt/evidence | ✅ durable assignment | ✅ release/rollback receipt |
| Promptfoo | △ 文件/Git 管理 | △ `select-best`/结果对照，非盲测队列 | — 基础单条人工评分不满足本报告口径 | — 主要是离线/CI | — | — |
| OpenAI Evals 平台 | ✅ | ✅ grader 可做 pairwise | △ 官方方法强调人工校准 | ✅ trace/eval run | — | — |
| LangSmith | ✅ 原生版本 | ✅ | ✅ | ✅ online eval/抽样 | — | △ Prompt commit 手动回退 |
| MLflow GenAI | △ CRUD/digest/tag，非不可变历史语义 | △ 自定义 scorer | ✅ review queue，但仍标 Experimental | ✅ | △ Gateway 仅分模型流量 | △ Prompt alias 手动回退 |
| Phoenix | ✅ | △ 有 blind/shuffle evaluator 配方，非工作流 | △ trace annotation，无多人任务队列 | ✅ | — | △ Prompt tag 手动回退 |
| Braintrust | ✅ snapshot/environment | △ side-by-side/custom scorer | ✅，部分为 Pro/Enterprise | ✅ | △ A/B/environment，不等于领域 canary | △ Prompt rollback，不是整包 rollback |
| DSPy | △ train/dev/test + 程序保存 | — | △ 外部提供 label/feedback | — | — | △ 保存/加载程序，不是发布回滚 |
| GEPA | △ split、candidate hash/checkpoint | — | △ 外部 feedback | — | — | △ checkpoint，不是发布回滚 |
| TextGrad | △ 用户自备数据 | — | — | — | — | — |
| Reflexion | △ episodic memory/log | — | — | — | — | — |
| Agent Lightning | △ trajectory/training samples | — | — | △ gateway/rollout | — | △ 模型 checkpoint，非应用发布回滚 |
| OpenTelemetry GenAI | — | — | — | ✅ trace 语义 | — | — |
| τ³-bench | ✅ task/release 可钉版本 | — | — | — | — | — |

最重要的空白不是“少了一个 LLM judge”，而是所有通用方案都没有 Tracefold 所需的 **reader-state counterfactual replay** 与 **News 领域 canary safety contract**。

## 4. 各方案深入判断

### 4.1 Promptfoo：适合开发时 eval，不适合做可信发布门

[官方 README](https://github.com/promptfoo/promptfoo/blob/main/README.md) 把它定位为本地 CLI/库，可比较 model/prompt、运行 assertions/red-team，并接入 CI/CD；项目仍为 MIT，README 同时说明 Promptfoo 已成为 OpenAI 的一部分。[Dataset 文档](https://github.com/promptfoo/promptfoo/blob/main/site/docs/configuration/datasets.md) 支持 YAML/CSV 或外部数据源，[CI 文档](https://github.com/promptfoo/promptfoo/blob/main/site/docs/integrations/ci-cd.md) 支持 JSON、HTML、JUnit 结果与 quality gate。[Web viewer](https://www.promptfoo.dev/docs/usage/web-ui/) 允许单条 Pass/Fail、0–1 评分、评论和导出，但没有 LangSmith/Braintrust 那类 reviewer assignment、reservation、多人独立复核，因此本报告不把它记作成熟 human-review queue；[`select-best`](https://www.promptfoo.dev/docs/configuration/expected-outputs/model-graded/select-best/) 是模型判别多候选，也不等于人工盲测。

它在 2026 年新增的 [prompt optimization](https://github.com/promptfoo/promptfoo/blob/main/site/docs/usage/prompt-optimization.md) 能读取 baseline 失败，让优化模型改写候选，并用可选 validation split 降低过拟合。但当前 contract 是“一次只优化一个 prompt/provider”，随机 validation split 也不等于 Issue #112 的隐藏 temporal holdout、retention/safety strata 或两臂 ledger。

**结论：条件 WRAP。** 可作为 Prompt 片段、schema、禁词、wire fixtures 的快速 smoke suite；可以借鉴 provider/assertion/CI 输出。它不得成为 release truth，也不得代替 `CandidateEvaluator`。若引入，adapter 必须把 frozen DatasetManifest 转成 Promptfoo tests，并将结果重新封装为“开发证据”，不能让其 quality gate 直接 promote。

### 4.2 OpenAI eval guidance：方法必借鉴，平台不可新依赖

[Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices) 的核心建议与 Chapter 9 高度一致：评测要针对具体任务、覆盖生产分布、持续从生产日志扩充数据集，并用人工 judgment 校准自动 grader。官方还明确指出，LLM 更擅长 pairwise、分类和 pass/fail 等判别任务，不应只让 judge 自由生成一个“感觉分”；人工评审应随机化、盲化，并检查 position/verbosity bias 与 judge-human agreement。

[Agent evals 文档](https://developers.openai.com/api/docs/guides/agent-evals) 提供 trace、grader、dataset、eval run 的组合方式。但同一官方评测最佳实践页和 [deprecations](https://developers.openai.com/api/docs/deprecations#2026-06-03-evals-platform) 已明确宣布：OpenAI Evals 平台将在 **2026-10-31 进入只读，2026-11-30 关闭**；[官方迁移指南](https://developers.openai.com/cookbook/examples/evaluation/moving-from-openai-evals-to-promptfoo) 指向 Promptfoo。Reusable prompts 也将在 2026-11-30 关闭，官方要求将 Prompt 移回应用代码，这反而支持 Tracefold 的 byte-frozen、code-owned Prompt 方向。

**结论：BORROW 方法，REJECT 平台依赖。** Issue #112 应采用 task-specific rubric、blind pairwise、人机校准和 continuous eval；不能在 2026-08-21 新建将于三个月内关闭的 API 依赖。`openai/evals` 仓库仍存在，不代表托管产品的生命周期仍安全。

### 4.3 LangSmith：完整 managed eval loop，但不是 Tracefold 的权威状态机

[LangSmith evaluation](https://docs.langchain.com/langsmith/evaluation) 同时覆盖 offline 与 online：从人工、生产 trace 或 synthetic 数据建 dataset，运行 code/LLM/human/pairwise evaluator，比较 experiment；生产失败 trace 又可回流 dataset，形成 backtest→fix→redeploy 循环。[Manage datasets](https://docs.langchain.com/langsmith/manage-datasets) 明确说明每次增删改创建 dataset version，并可用时间戳或 tag 钉版本。[Pairwise evaluation](https://docs.langchain.com/langsmith/evaluate-pairwise) 支持随机化两臂顺序以降低位置偏差，[Annotation queues](https://docs.langchain.com/langsmith/annotation-queues) 支持 reviewer 分配、reservation 和多人独立复核。[Evaluation types](https://docs.langchain.com/langsmith/evaluation-types) 覆盖 regression、backtesting、pairwise、online sampling/monitoring。

这是本次调研中最完整的托管 eval 产品之一。但其 online evaluator 是对线上 stable output 打标签，并不自动完成“candidate 对同一条生产输入做不送达 shadow”；它也不知道 candidate 改变 reader ledger 后怎样重放后续 Event，更不能替 Tracefold 保证 one-event-one-card、durable arm assignment 与 delivery receipt。

**结论：不作为 #112 核心依赖。** 若团队以后需要多人实验管理，可以单向导出脱敏 trace/score，把 LangSmith 当 observer；authoritative dataset、review、candidate state、promotion 仍留在 PostgreSQL/Git。现在接入会与已有真相表和 audit 重叠，不符合 KISS。

### 4.4 DSPy + GEPA：很好的候选生成器，不是候选批准者

[DSPy README](https://github.com/stanfordnlp/dspy/blob/main/README.md) 把 LM workflow 表达成声明式模块，再由 optimizer 搜索 prompt/demonstration。[Optimization overview](https://github.com/stanfordnlp/dspy/blob/main/docs/docs/learn/optimization/overview.md) 要求训练、开发和测试集，明确警告过拟合；官方建议从几十个高质量例子开始，并尽量积累到数百个。它说明了一个关键事实：优化器不会替你创造可信 metric 和数据。

[DSPy 的 GEPA 教程](https://github.com/stanfordnlp/dspy/blob/main/docs/docs/getting-started/gepa-optimization.md) 允许 metric 同时返回 score 与文字 feedback，反思模型据此改写候选。[GEPA README](https://github.com/gepa-ai/gepa/blob/main/README.md) 的核心是读取完整 trajectory、生成反思，并用 Pareto search 保留在不同样本上表现好的候选。实际 [`Adapter`](https://github.com/gepa-ai/gepa/blob/main/src/gepa/core/adapter.py) 接口返回 outputs、scores、trajectories 与 reflective dataset；[`state.py`](https://github.com/gepa-ai/gepa/blob/main/src/gepa/core/state.py) 有 candidate hash、split cache 和 checkpoint。

这些能力很适合未来的“proposal generator”：当 ReviewDesk 已积累足够高质量 judgment 后，GEPA 可以针对某个 failure cluster 提出最小 Prompt patch。但它没有 human review queue、生产 shadow/canary、业务 rollback，也无法阻止 optimizer 过拟合 judge。Pareto search 也不是可信 release gate。

**结论：延后 WRAP。** 先完成 Issue #112 的证据与 evaluator；达到至少真实边界/retention/negative/holdout 样本门槛后，再做 `ProposalGenerator` adapter。输入只能是 development evidence，输出只能是 `ProposalReceipt + CandidateManifest`；GEPA/DSPy 永无 stable 写权、validation 读取权或 promotion 权。当前不要把在线 LangChain 单次 judgment 重写成 DSPy program。

### 4.5 TextGrad：文本梯度是研究工具，不是生产学习控制面

[TextGrad README](https://github.com/zou-group/textgrad/blob/main/README.md) 将 LLM 的文字反馈包装成类似 autograd 的 backward pass，可优化 prompt、代码或解题过程；[Nature 论文](https://www.nature.com/articles/s41586-025-08661-4) 给出了研究证据。但官方 README 仍将新的 LiteLLM engine 标成 experimental，并计划弃用旧 engine；release 和 push 也明显滞后于 DSPy/GEPA。

它依赖一个可优化的文字 loss，却不提供 dataset registry、blind human review、shadow/canary 或发布 rollback。对 Tracefold 来说，“让一个 LLM 解释为什么另一个 LLM 错，再沿文字梯度改 Prompt”会把 judge 偏差进一步放大。

**结论：REJECT 生产接入。** 若做研究性 notebook，可以离线对比，但没有理由把它放进 #112 主架构。

### 4.6 Reflexion：有启发的论文模式，不是可上线库

[Reflexion 官方仓库](https://github.com/noahshinn/reflexion) 是 NeurIPS 2023 [论文](https://arxiv.org/abs/2303.11366) 的研究代码、notebook 和实验日志。核心思想是：Agent 失败后生成 verbal reflection，把它保存在 episodic memory，后续尝试时读取。

这能改善封闭 benchmark 的多次尝试，但 reflection 只是“模型对失败原因的假设”，不是经过人工、确定性 verifier 或 holdout 验证的知识。仓库没有稳定 package release，也没有生产 review、版本门、canary 或 rollback。

**结论：REJECT 作为依赖，只 BORROW 一个原则**：反思必须绑定真实环境反馈和 attempt history；未验证的 reflection 不能写入 stable prompt/skill，更不能成为 reader-facing truth。

### 4.7 Agent Lightning：适合有训练数据与 GPU 的 Agent RL，不适合当前 Tracefold

[Agent Lightning README](https://github.com/microsoft/agent-lightning/blob/main/README.md) 的 v1.0 提供 trainer、API gateway、rollout controller，可通过代理采集真实 Agent trajectory，并借助 verl/vLLM 做强化学习；官方还提供 Kubernetes 运行方式。其 [2026 技术报告](https://arxiv.org/abs/2608.17528) 与早期 [Agent Lightning 论文](https://arxiv.org/abs/2508.03680) 讨论了把 Agent execution 与训练解耦。

这是“训练模型权重”的框架，不是 prompt/eval release manager。Tracefold 当前没有大量可靠 reward、没有可训练模型权重、没有独立 GPU 训练面，而且价格正负、模型自报 confidence、是否送达都不能直接成为 reward。仓库虽然活跃且刚到 v1.0，但新 major 的工程风险仍高。

**结论：REJECT 当前采用。** 只有当未来积累数千个可靠、去泄漏、跨时间 holdout 的 judgment，并明确决定自训模型时，才另立研究 Issue；即便采用，它的 checkpoint 也仍需经过 Issue #112 的安全/retention/release gate。

### 4.8 MLflow：成熟但偏重；可借鉴版本/feedback，不宜接管 Prompt

[MLflow GenAI datasets](https://mlflow.org/docs/latest/genai/datasets/) 支持从 trace 或人工数据创建 SQL-backed dataset，并增删改 records、加 tags；但文档没有承诺每次 mutation 都生成不可变 dataset history，digest 也不等于 Tracefold 的 content-addressed frozen manifest。[Eval and monitor](https://mlflow.org/docs/latest/genai/eval-monitor/) 覆盖 judge、系统评测和生产 monitoring。

[Review queues](https://mlflow.org/docs/latest/genai/assessments/review-queues/) 支持共享 pending/completed 队列并把答案写回 trace，但官方仍标为 Experimental；[feedback](https://mlflow.org/docs/latest/genai/assessments/feedback/) 可来自人工、代码或 LLM。[Prompt Registry](https://mlflow.org/genai/prompt-registry) 提供版本、diff、dev/staging/prod alias，移动 alias 可以手工回退。[AI Gateway traffic routing](https://mlflow.org/docs/latest/genai/governance/ai-gateway/traffic-routing-fallbacks/) 可以按比例拆分**模型**与故障 fallback，但不会分配完整 prompt+policy+model bundle，也没有按 Tracefold 业务 guardrail 自动回滚。

**结论：BORROW，不接管。** MLflow 本身成熟，但为 #112 增加一个服务、数据库和远程 Prompt registry 会与 Tracefold PostgreSQL、byte-frozen Prompt、Git deploy 重复。可借鉴 scorer version、feedback provenance、Prompt diff UX；只有组织已经统一运营 MLflow 时，才考虑只读同步。

### 4.9 Arize Phoenix：最适合做可选 source-available observer

[Phoenix 官方文档](https://arize.com/docs/phoenix/) 将 OpenTelemetry/OpenInference tracing、evaluation、dataset/experiment 和 Prompt 管理放在同一可自托管平台。[Dataset concepts](https://arize.com/docs/phoenix/learn/datasets-and-experiments/datasets-concepts) 说明每次 insert/update/delete 都会生成版本，experiment 钉住 dataset version；[Updating datasets](https://arize.com/docs/phoenix/datasets-and-experiments/how-to-datasets/updating-datasets) 展示 stable IDs、`version_id` 与 diff/replace。[Trace annotations](https://arize.com/docs/phoenix/tracing/how-to-tracing/feedback-and-annotations) 支持人工和程序化反馈。官方 [pairwise evaluator 配方](https://arize.com/docs/phoenix/evaluation/server-evals/code-evaluators/pairwise) 展示盲化、确定性 shuffle 和 swap-and-confirm，但它是一段 evaluator 配方，不是成熟的多人 pairwise review queue。

[Prompt tagging](https://arize.com/docs/phoenix/prompt-engineering/how-to-prompts/tag-a-prompt) 提供线性版本和 prod/staging/dev tag，可把 tag 移回旧版本；[using a prompt](https://arize.com/docs/phoenix/prompt-engineering/how-to-prompts/using-a-prompt) 也坦率提醒远程 Prompt 会引入网络与调试复杂度，建议需要稳定时用 immutable version ID。[Server evaluators](https://arize.com/docs/phoenix/evaluation/server-evals/llm-evaluators) 又能版本化 judge Prompt 并保留 trace。

Phoenix 的优势是可自托管、OTel 友好、观测面完整；缺点仍是没有 Tracefold 的多人 blind pairwise workflow、两臂顺序 replay、durable canary assignment。远程 prod tag 也会破坏代码拥有、byte-frozen 的 Prompt 身份。其 [Elastic License 2.0](https://github.com/Arize-ai/phoenix/blob/main/LICENSE) 允许多数内部使用，但不应写成 MIT/Apache OSS；若把其核心功能对外托管，必须另做许可证审查。

**结论：可选 WRAP。** 若实际 trace 调试成本变高，优先在所有候选平台中选择 Phoenix 做单向、脱敏、可关闭的 visualizer；不能让 Phoenix 成为事实库、review authority 或 stable pointer。现在没有明确 operator 需求时，先不部署。

### 4.10 Braintrust：最完整的商业工作台，但不是 KISS 默认项

[Braintrust datasets](https://www.braintrust.dev/docs/annotate/datasets) 支持从 production logs/feedback 建数据集，并使用版本 snapshot/environment；[Prompt 文档](https://www.braintrust.dev/docs/evaluate/write-prompts) 提供 version diff、rollback、environment assignment 与 A/B testing；[Playground](https://www.braintrust.dev/docs/evaluate/playgrounds) 支持 side-by-side experiment 与 immutable snapshot；[Human review](https://www.braintrust.dev/docs/annotate/human-review) 支持结构化任务、分配与多 reviewer，但部分能力属于 Pro/Enterprise；[Deployment environments](https://www.braintrust.dev/docs/deploy/environments) 提供 dev/staging/prod 的原子版本分配和人工 promotion。

它是能力最接近完整 workbench 的商业产品，但 environment/A-B 仍不等于在模型调用前把每个 News Event 持久锁定到一个 arm；也不会理解全局 reader ledger 污染和不能双发。把 Prompt 远程托管还会把生产正确性绑到另一个控制面。

**结论：当前不 BUY。** 如果未来 reviewer 数量、跨团队协作和实验规模大到自建 UI 的总成本明显更高，可重新评估，但仍只能买协作/观测层，不能外包 `CandidateEvaluator` 与 canary selector。

### 4.11 OpenTelemetry GenAI：应该兼容，但只能当出口协议

OpenTelemetry 已把 GenAI semantic conventions 从主仓库 [迁往独立仓库](https://github.com/open-telemetry/semantic-conventions/blob/main/docs/gen-ai/README.md)。新仓库当前把 [GenAI 规范状态](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/README.md) 标为 **Development**；[span conventions](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md) 覆盖 provider、model、prompt version、token 等属性，并明确完整 message/system instructions 属于 opt-in，可能包含敏感数据。

它没有 dataset、review、candidate 或 release 能力，但能降低未来换 Phoenix/LangSmith/其它后端的锁定。

**结论：WRAP 最小元数据 adapter。** 保持 Tracefold 内部 audit schema 稳定，映射时钉住 semconv 版本；默认只导出 event/span ID、模型/Prompt 版本、latency/token、error/degraded，不导出原始新闻、ledger、system prompt 或 credential。Telemetry 失败不得影响 News readiness。

### 4.12 τ-bench / τ³-bench：借鉴 evaluator 语义，不直接引库

旧 [τ-bench 仓库](https://github.com/sierra-research/tau-bench) 已明确提示任务过时，应迁到新的 tau2-bench；现仓库 README 将其称为 τ³-bench。[Evaluation 文档](https://github.com/sierra-research/tau2-bench/blob/main/docs/evaluation.md) 的重要设计不是某个 grader，而是按最终数据库状态/通信约束判成功：参考 action list 只是一条可能路径，Agent 走另一条正确路径也能通过。[Evaluator 源码](https://github.com/sierra-research/tau2-bench/blob/main/src/tau2/evaluator/evaluator.py) 实现了 reward basis 组合。官方还警告 v1.0.1 修复改变结果，不能把旧版分数与新版直接比较，实验必须 pin tag。

**结论：BORROW。** Tracefold 应比较最终 reader truth、delivery、duplicate、factual support，而不是强迫模型复现某条 reasoning trajectory；所有 rubric/task/evaluator 都必须 pin version。可以借鉴多次运行的 pass^k/方差报告，但 tau2-bench 本身不适合新闻生产数据、ReviewDesk 或 release orchestration。

## 5. Build vs Buy 决策矩阵

| 能力 | 自建成本 | 外部最佳候选 | 外部缺口 | 决策 |
|---|---:|---|---|---|
| Immutable Event evidence / real sent truth | 中 | 无 | 通用平台不知道 stronger member、degraded sent、ambiguous delivery | **BUILD** |
| Eventless miss + fact-cluster ReviewCase | 中 | LangSmith/Braintrust 可存普通 examples | 无 upstream recall 语义、无领域 cluster contract | **BUILD** |
| Append-only 多维 ReviewDesk | 中 | Braintrust / LangSmith / Phoenix | 权限、blind redaction、accepted/superseded、窄 DB role 与现有 Console 不一致 | **BUILD 核心 UI/状态**；借鉴 UX |
| Dataset version/freeze | 低至中 | LangSmith/Phoenix/Braintrust | 仍需 closed window、settlement grace、reader contract、content hash | **BUILD manifest** |
| Stable/candidate 语义 replay | 高 | Promptfoo/DSPy | 都不实现 arm-specific reader ledger 与 News policy | **BUILD** |
| Blind pairwise / human calibration | 中 | LangSmith/Braintrust | release truth 仍需本地 case/arm mapping 和 trusted root | **BUILD authority**；BORROW 方法 |
| Candidate generation | 中且可延后 | GEPA/DSPy | 不会审批自己，也不能访问 hidden holdout | **WRAP later** |
| CI smoke eval | 低 | Promptfoo | 不能证明 production improvement | **WRAP optional** |
| Trace visualization | 中 | Phoenix / LangSmith | 不是业务真相，存在敏感内容与运行依赖风险 | **WRAP optional，Phoenix 优先** |
| Telemetry vocabulary | 低 | OpenTelemetry GenAI | 规范仍 Development | **WRAP minimal** |
| Shadow/canary arm assignment | 高 | 无完全匹配 | 通用 A/B 不保证 before-call durable assignment、one-card、global ledger | **BUILD** |
| Promotion/rollback evidence | 中 | Braintrust/Prompt registries 部分提供 | Tracefold 发布单位是 Git/image/Prompt manifest，不是远程 alias | **BUILD receipts** |
| Weight/RL training | 很高 | Agent Lightning | 当前无可靠 reward/数据/GPU/可训练权重 | **REJECT now** |

### 明确的 WRAP / BORROW / REJECT 清单

#### WRAP

1. **OpenTelemetry GenAI**：最小、脱敏、非阻塞 exporter；规范版本钉死。
2. **Phoenix（可选）**：只读 visualizer/experiment viewer；单向数据流，可随时关闭。
3. **Promptfoo（可选）**：开发/CI smoke，不参与 release decision。
4. **GEPA/DSPy（延后）**：只有数据与 evaluator 成熟后，作为 candidate proposal adapter。

#### BORROW

1. OpenAI：task-specific eval、生产分布、blind randomized pairwise、judge-human calibration。
2. LangSmith：trace→dataset→offline regression→redeploy 的闭环与 dataset version tag。
3. τ³-bench：final-state verifier、task/evaluator version pin、重复运行与不确定性。
4. Phoenix/MLflow/Braintrust：dataset/prompt diff、feedback provenance、review queue 与版本 UX。
5. DSPy/GEPA：明确 train/dev/test，完整 trajectory + text feedback，候选 hash/checkpoint。

#### REJECT

1. OpenAI Evals 平台新依赖：已公告 2026-11-30 关闭。
2. 任何远程 Prompt registry 作为 production stable pointer：破坏 byte-frozen code ownership，并引入热路径网络/控制面故障。
3. Reflexion/TextGrad 的 self-reflection 或 textual gradient 直接写 stable Prompt/Skill。
4. Agent Lightning：当前没有可靠 reward、可训练权重与训练基础设施。
5. 每晚 Agent 读日志后自动改 Prompt、tests、rubric、threshold 或 trusted root。
6. 把 raw 1h/4h price sign、送达与否、LLM confidence 当 reward/gold。
7. 因为接入平台就复制一套第二事实库、第二 release state machine 或通用 workflow DSL。

## 6. 与 Chapter 9 的原则逐项映射

| Chapter 9 原则 | Issue #112 中应怎样落地 | 生态中可借鉴 | 不能外包的部分 |
|---|---|---|---|
| 持久化不等于学习 | trace/reflection 只有通过独立验证并 promotion 才成为 stable | OpenAI eval guidance | learning 的领域定义与 release evidence |
| 结果、过程、质量三层 verifier | delivery/final outcome；trace/owner；factual support/reader value 分开 | τ³ final-state、Phoenix traces | sent truth、first_bad_owner、News rubric |
| immutable raw trace → 单例分析 → 跨例归纳 | EvidenceSnapshot → ReviewCase → failure cluster | LangSmith/Phoenix dataset loop | stronger evidence/version、eventless miss |
| Knowledge / Prompt-Skill / Program / Model 四载体 | Review 先路由 owner，Prompt/Policy 单变量；program bug 正常 Git 修 | DSPy/GEPA 只覆盖 Prompt/程序候选 | owner routing 与允许的 candidate kind |
| deterministic constraint 放进 harness | one-card、ledger、throttle、schema、安全规则不靠 Prompt | τ³ verifier | Tracefold deterministic policy/replay |
| stable/candidate/trusted root 隔离 | candidate 不能改 rubric、threshold、split 或 stable pointer | experiment snapshot/versioning | trusted root、exact-one-variable、权限 |
| boundary 改善且 retention 不回归 | boundary/retention/safety/negative 分层，required stratum 空则 UNKNOWN | DSPy train/dev/test、Promptfoo validation | temporal split、cluster N、hard guardrail |
| online execution / offline evolution 双环 | 热路径保持一次 Triage；shadow/评测全在冷路径 | online eval/trace products | candidate 无生产副作用、独立 budget |
| minimal diff + canary + rollback | Prompt 或 Policy 单变量，shadow 后 10% bounded canary，留 receipt | Braintrust environments、SRE 常规思想 | before-call durable arm、single card、rollback receipt |
| 避免遗忘与负迁移 | hidden temporal holdout、retention、must_push safety、长期 bake | DSPy/GEPA split/Pareto | reader-contract cohort 与真实历史顺序 |
| proposal generator 不能批准自己 | GEPA/Coding Agent 只出 ProposalReceipt；独立 evaluator 与人工批准 | GEPA candidate hash/audit | holdout 密封、权限与 promotion |

## 7. 对 Issue #112 的具体架构建议

### 7.1 保持两个深 Module，不增加通用“学习平台”抽象

`ReviewDesk` 只负责：取下一条该看的 evidence、服务端裁剪/盲化、接收 append-only judgment、给 receipt。它不懂模型运行、dataset gate 或发布。

`CandidateEvaluator` 只负责：冻结 development/validation，验证单变量 candidate，运行 stable/candidate，封存 `EvaluationReport + ReleaseEvidence`。它可以推荐下一阶段，但不能 publish。

外部工具全部放在这两个 Module 之外：

```text
Tracefold PostgreSQL / Git / image digest        ← authoritative truth
                │
                ├─ OTel metadata exporter        ← optional, one-way
                ├─ Phoenix/LangSmith observer     ← optional, one-way
                ├─ Promptfoo smoke adapter        ← development evidence only
                └─ GEPA/DSPy proposal adapter     ← candidate generation only
```

任何 adapter 失败都不得改变 News online readiness；任何外部系统 ID 都只能是辅助索引，不能替代本地 content hash、event ID、review receipt 或 release receipt。

### 7.2 Prompt 问题还是 Skill/知识问题：用 owner 路由，不靠感觉

生产复盘时先问“第一处错误在哪里”，再决定载体：

| 观察到的问题 | first_bad_owner | 应改什么 |
|---|---|---|
| 原文没有被接入、被 Deduper/Gate 错杀 | acquisition/gate/program | 确定性代码、fixture、normal Git release；不是 Prompt |
| 相关旧卡没有进入 told ledger | retrieval/reader truth | sent receipt 与 retrieval 代码；不是给 Prompt 加禁令 |
| 证据进了模型，但模型编造原因、错判方向/重要性 | semantic judgment | 最小 Prompt candidate；可由 GEPA/Coding Agent 提案 |
| 模型判断合理，`decide()` 错误 push/throttle/escalate | policy | Policy candidate；先做 0-model cheap screen，再做独立 ledger replay |
| 一类重复事实需要稳定定义/实体规则 | knowledge 或 deterministic program | 事实经验证且相对稳定才进入知识；精确约束优先代码 |
| 工作流需要可重复的一套“如何复盘”步骤 | Skill | 写给 operator/Coding Agent 的程序化操作说明，不注入在线 Triage |
| 需要改变模型本身能力 | model | 另立训练/模型候选 Issue；不能与 Prompt/Policy 混试 |

Skill 不是“模型漏一条新闻就新增一条记忆”。Skill 是稳定、可复用、可测试的工作步骤；领域事实和例外不断塞进 Skill，会变成无法做 retention 的超长 Prompt。对两个已知漏推链接，必须先创建 ReviewCase，核对 evidence/Gate/verdict/ledger/policy/delivery 的第一处断点，不能预先决定“调 Prompt”。

### 7.3 数据量不足时，闭环应返回 UNKNOWN，不要假装自动学习

Issue #112 给出的首个 proof 门槛（至少 30 boundary、100 retention、50 negative clusters、完整 safety set、候选之后 24h/200 Event hidden temporal holdout）是合理的最低工程门槛。当前 operator labels 为 0 时，优先级应是：

1. 修 sent truth 与 immutable evidence；
2. 让 ReviewDesk 真正可提交、纠正、录 eventless miss；
3. 建立分层 coverage，而不是先上 optimizer；
4. 数据充足后才运行 Prompt/Policy candidate；
5. GEPA、judge 或 pairwise 自动化都必须用人工样本校准。

没有足够 independent fact clusters、required stratum 为空、provider outage 或 review disagreement 时，结果是 `UNKNOWN/incomplete`，不是 `PASS`，也不是把 candidate 判为业务失败。

### 7.4 复盘页的正确职责

成熟产品的复盘页不是“展示 Agent 命中率”，而是让人高吞吐地产生可信 judgment：

- 默认 hero：待审数量、已审覆盖率、各 strata 的 N 与区间，而不是 1h hit rate；
- 一次只呈现一个 fact cluster 的证据，明确 source、时间、evidence version、sent/held/ambiguous；
- 支持多维 judgment：should-push、方向、时效、标题、why support、duplicate、first_bad_owner；
- correction 用 supersedes，仲裁和接受状态可追溯；
- pairwise 模式隐藏 stable/candidate、版本、结果和 outcome，提交后再按策略解盲；
- eventless miss 是一等入口；
- Market Reaction 独立成 secondary evidence，只帮助 discovery，不自动写 label；
- 页面不能创建 candidate、改 gate threshold、promote、canary 或 rollback。

LangSmith/Braintrust 的 queue 与 Phoenix 的 annotation drawer 可以作为 UX 参考，但 ReviewDesk 的真相模型必须在 Tracefold 内。

## 8. 一个完整的生产推演（高中生版本）

把系统想成一所学校：

- 正在上课的老师是 stable Agent；
- 新教案是 candidate；
- 历史试卷是 development/retention；
- 老师从没看过的新试卷是 hidden holdout；
- 监控摄像头是 shadow；
- 少量真实班级试讲是 canary；
- 校长签字和旧教案备份是 promotion/rollback。

假设一条 Moderna 新闻只有“盘前跌 13%”，stable 却写成“因为财报或指引不及预期”。闭环这样跑：

1. **留原卷**：保存当时实际输入模型的标题、正文、tags、ledger 和版本，之后 Event 更新也不能改它。
2. **老师批注**：operator 在 ReviewDesk 标记“应该推，但 why 编造原因”，并框出不受原文支持的句子。1h 后股价继续跌不能证明这句话是真的。
3. **找第一处错误**：原文进了模型，sent/ledger 也没问题，错误首次出现在 semantic judgment，所以 owner 是 Prompt，不是 Gate/Policy/Skill。
4. **出一份新教案**：人工或 GEPA 只改 evidence/copy 相关的一小段，声明目标是“原因未知时不得猜原因”；它看不到 hidden holdout。
5. **做旧题**：stable 和 candidate 用完全相同 model/schema/retrieval/policy 跑 development。若 candidate 修了 Moderna，却把 20 条有明确原因的新闻全写成“原因未知”，retention 会拦住。
6. **做新题**：candidate 注册之后才冻结未来时间窗的 holdout；两臂独立顺序运行，各自读取自己此前“模拟已送达”的卡。人工盲测 A/B，不知道哪边是新教案。
7. **影子试讲**：candidate 读真实新流量但不送卡，只看 schema、延迟、成本、degraded、输出分布；这一步证明“跑得稳”，不证明“内容更好”。
8. **小班试讲**：系统在模型调用前把少量低风险 Event 持久分到 candidate，且每个 Event 只能选一个 arm、只发一张卡。异常立即 trip，后续全回 stable。
9. **正式发布或回滚**：人工检查完整 evidence 后走普通 Git/image 发布；保存前一 stable digest。24h bake 内触发 guardrail 就回旧版，并保留负结果。

上线影响可以这样预期：

- 短期：模型调用和人工 review 成本上升，发布速度变慢；页面“看起来漂亮的命中率”会减少，因为 UNKNOWN、coverage、N 被诚实展示。
- 中期：Prompt、Policy、Gate、retrieval 的问题不再互相甩锅；漏推和重复能形成可重放 regression case；回滚从“凭印象改回去”变成有证据的操作。
- 长期：每次上线留下独立 holdout 与 release receipt，可判断质量是否真的积累，而不是 Prompt 越写越长、同一问题反复出现。
- 主要风险：人工标签偏差、judge 迎合、数据泄漏、candidate 污染 ledger、双发、远程平台泄露敏感 Prompt。Issue #112 的 blind、temporal、trusted-root、durable assignment、one-card 与脱敏 exporter 正是在控制这些风险。

## 9. 推荐实施顺序

这份生态调研不改变 Issue #112 的主实施地图，反而支持它的顺序：

1. **先完成 truth 层**：Event evidence version、真实 ReaderReceipt、ambiguous 语义。
2. **再完成 ReviewDesk**：多维 append-only judgment、correction/acceptance、eventless miss、窄写权限与真实页面操作。
3. **再做 CandidateEvaluator foundation**：content-addressed dataset/candidate/report、trusted root、RecordReplay、PASS/FAIL/UNKNOWN。
4. **实现 Prompt/Policy 两种受限 evaluator**：Policy cheap screen；finalist/Prompt 做真实双臂、独立 ledger、blind pairwise、temporal holdout。
5. **最后上 shadow/canary/rollback receipts**：没有 hidden holdout PASS 不能靠 canary 补票。
6. **首个端到端 proof 后再评估外部 adapter**：OTel metadata 可以最早加；Phoenix/Promptfoo/GEPA 必须由实际痛点触发，不预装平台。

第一轮不要做：通用 plugin evaluator、vector memory、自动 nightly optimization、在线 Reviewer Agent、remote Prompt serving、模型 RL、第二业务数据库或第二发布状态机。

## 10. 最终推荐

对 Tracefold 而言，最成熟的方案不是挑一个 star 最多的库，而是把成熟模式组合在正确边界：

```text
OpenAI / τ³-bench / Chapter 9       → 评价原则
Tracefold ReviewDesk                 → 人工真相与领域证据
Tracefold CandidateEvaluator         → 顺序双臂与可信发布门
Git + image + PostgreSQL receipts    → stable、canary、rollback 权威
OpenTelemetry                        → 可替换的观测出口
Phoenix / Promptfoo / GEPA           → 按需可插拔，不掌权
```

因此，Issue #112 **不应缩成“接 Promptfoo/LangSmith 做 eval”**，也不应扩成“造一套通用 Agent 学习平台”。它当前的两个 deep-module seam 与 hard-cut 方向是对的。需要坚持的 KISS 边界是：自建业务不可替代部分，借鉴通用产品的 UX/方法，只在第二个真实需求出现时才引入 adapter；任何优化器、judge 或观测平台都不能成为自己的裁判。

## 主要一手来源索引

- Tracefold：[Issue #112](https://github.com/AnalyThothAI/tracefold/issues/112)
- Chapter 9：[Continuous Learning](https://bojieli.github.io/ai-agent-book/book/chapter9/)
- Promptfoo：[README](https://github.com/promptfoo/promptfoo/blob/main/README.md)、[Datasets](https://github.com/promptfoo/promptfoo/blob/main/site/docs/configuration/datasets.md)、[Optimization](https://github.com/promptfoo/promptfoo/blob/main/site/docs/usage/prompt-optimization.md)、[Web UI](https://www.promptfoo.dev/docs/usage/web-ui/)、[`select-best`](https://www.promptfoo.dev/docs/configuration/expected-outputs/model-graded/select-best/)、[CI/CD](https://github.com/promptfoo/promptfoo/blob/main/site/docs/integrations/ci-cd.md)
- OpenAI：[Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)、[Agent evals](https://developers.openai.com/api/docs/guides/agent-evals)、[Deprecations](https://developers.openai.com/api/docs/deprecations#2026-06-03-evals-platform)、[Evals → Promptfoo migration](https://developers.openai.com/cookbook/examples/evaluation/moving-from-openai-evals-to-promptfoo)、[openai/evals](https://github.com/openai/evals)
- LangSmith：[Evaluation](https://docs.langchain.com/langsmith/evaluation)、[Manage datasets](https://docs.langchain.com/langsmith/manage-datasets)、[Pairwise evaluation](https://docs.langchain.com/langsmith/evaluate-pairwise)、[Annotation queues](https://docs.langchain.com/langsmith/annotation-queues)、[Evaluation types](https://docs.langchain.com/langsmith/evaluation-types)
- DSPy：[README](https://github.com/stanfordnlp/dspy/blob/main/README.md)、[Optimization](https://github.com/stanfordnlp/dspy/blob/main/docs/docs/learn/optimization/overview.md)、[Evaluation data](https://github.com/stanfordnlp/dspy/blob/main/docs/docs/learn/evaluation/data.md)、[GEPA tutorial](https://github.com/stanfordnlp/dspy/blob/main/docs/docs/getting-started/gepa-optimization.md)
- GEPA：[README](https://github.com/gepa-ai/gepa/blob/main/README.md)、[Adapter source](https://github.com/gepa-ai/gepa/blob/main/src/gepa/core/adapter.py)、[State source](https://github.com/gepa-ai/gepa/blob/main/src/gepa/core/state.py)
- TextGrad：[Repository](https://github.com/zou-group/textgrad)、[Nature paper](https://www.nature.com/articles/s41586-025-08661-4)
- Reflexion：[Repository](https://github.com/noahshinn/reflexion)、[Paper](https://arxiv.org/abs/2303.11366)
- Agent Lightning：[Repository](https://github.com/microsoft/agent-lightning)、[2026 report](https://arxiv.org/abs/2608.17528)、[2025 paper](https://arxiv.org/abs/2508.03680)
- MLflow：[Datasets](https://mlflow.org/docs/latest/genai/datasets/)、[Eval/monitor](https://mlflow.org/docs/latest/genai/eval-monitor/)、[Review queues](https://mlflow.org/docs/latest/genai/assessments/review-queues/)、[Feedback](https://mlflow.org/docs/latest/genai/assessments/feedback/)、[Prompt Registry](https://mlflow.org/genai/prompt-registry)、[Gateway traffic routing](https://mlflow.org/docs/latest/genai/governance/ai-gateway/traffic-routing-fallbacks/)
- Phoenix：[Docs](https://arize.com/docs/phoenix/)、[Dataset concepts](https://arize.com/docs/phoenix/learn/datasets-and-experiments/datasets-concepts)、[Trace annotations](https://arize.com/docs/phoenix/tracing/how-to-tracing/feedback-and-annotations)、[Pairwise evaluator](https://arize.com/docs/phoenix/evaluation/server-evals/code-evaluators/pairwise)、[Prompt tags](https://arize.com/docs/phoenix/prompt-engineering/how-to-prompts/tag-a-prompt)、[Server evaluators](https://arize.com/docs/phoenix/evaluation/server-evals/llm-evaluators)、[License](https://github.com/Arize-ai/phoenix/blob/main/LICENSE)
- Braintrust：[Datasets](https://www.braintrust.dev/docs/annotate/datasets)、[Human review](https://www.braintrust.dev/docs/annotate/human-review)、[Playgrounds](https://www.braintrust.dev/docs/evaluate/playgrounds)、[Environments](https://www.braintrust.dev/docs/deploy/environments)
- OpenTelemetry：[GenAI repository](https://github.com/open-telemetry/semantic-conventions-genai)、[GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md)
- τ³-bench：[Repository](https://github.com/sierra-research/tau2-bench)、[Evaluation](https://github.com/sierra-research/tau2-bench/blob/main/docs/evaluation.md)、[Evaluator source](https://github.com/sierra-research/tau2-bench/blob/main/src/tau2/evaluator/evaluator.py)
