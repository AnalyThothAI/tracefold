# Agent 持续学习闭环：Build vs Buy 调研（2026-08-21）

> 状态：架构研究稿，不是实现 PR。
>
> 范围：只研究离线评测、人工复核、候选生成、Tracing 与发布证据；不改变 News 在线热路径。
>
> 证据规则：外部事实只采用官方仓库、官方文档和论文；GitHub 活跃度快照截至 2026-08-21。Star 只作维护面旁证，不作技术选型分数。本文没有复制生产新闻、Prompt 输入或凭据。

## 结论先行

Tracefold 不应该采购一个“大而全 Agent 平台”来替代 #112，也不应该继续手写所有通用评测工具。KISS 下的正确边界是：

1. **自己构建领域闭环的四根骨架**：`ReviewDesk`、`CandidateEvaluator` 的编排与可信根、候选注册/隔离、shadow/canary/rollback 发布控制。这些能力依赖 Tracefold 特有的 reader ledger、one Event/one card、不双发、真实 `sent` receipt 和盲评协议，候选库都没有一等实现。
2. **有条件地包装 Promptfoo**：仅作为开发机/CI 的 Prompt 测试、通用 assertion、模型裁判和结果展示工具；虽然当前版本已有 optimizer，V1 也不授权它自动生成或批准候选。它不得成为数据真相、顺序重放器或发布控制器。若一日 spike 不能通过同一个生产 `SemanticJudge` adapter 执行，就不引入 Node 依赖，继续使用原生 Python evaluator。
3. **V1 不部署 Langfuse/Phoenix/MLflow/Opik 这类第二套平台**：现有 PostgreSQL 已保存业务事实和审计；现在缺的是 gold judgment 和领域发布协议，不是另一个 Trace UI。未来如果 operator 确实需要跨运行检索，可优先把 **Langfuse 作为只读 OpenTelemetry sidecar** 做单一候选试点，绝不回写判定或成为可信根。
4. **V1 不自动改 Prompt**：先用人工/Coding Agent 生成单变量 proposal。等 `ReviewDesk` 有足够、多维、可仲裁的标签，`CandidateEvaluator` 通过了泄漏测试和 temporal holdout，再把 **GEPA 包在离线 `ProposalGenerator` 后面**。GEPA 只能提案，不能读 hidden holdout、不能批准、不能发布。
5. **停止建设在即将关闭的 OpenAI Evals 产品面上**：OpenAI 已宣布 Evals 平台在 2026-10-31 转只读、2026-11-30 关闭 dashboard/API，并明确推荐迁移到 Promptfoo；依赖 dataset-backed Evals 的 Prompt Optimizer 同期弃用。开源 `openai/evals` 仓库本身没有被官方标成 archived，这两件事必须分开表述。

一句话架构：

```text
PostgreSQL 不可变业务证据（truth）
  -> 自建 ReviewDesk（人如何给可信答案）
  -> 自建 CandidateEvaluator（两臂如何按时间真实跑）
       -> 可选 Promptfoo adapter（通用断言/CI/展示，不掌权）
  -> 人工或离线 GEPA proposal（只提案）
  -> 自建 shadow/canary/rollback（生产权威与安全）
  -> Git/CI/image 正常发布

可选 Langfuse OTel sidecar：只读观察，不参与上述箭头的决定
```

这与 #112 的硬边界一致：在线仍是一次 SemanticJudge 加一次确定性 `decide()`；学习发生在冷路径，发布必须有人批准，且不建设通用实验平台。[#112：目标、两个 deep module 与非目标](https://github.com/AnalyThothAI/tracefold/issues/112)

它也延续了 Chapter 9 研究稿已经从书与本地样例验证出的边界：当前缺口是 Prompt evaluator 而不是 Reflection Agent，价格观察不是 reward，成熟内核是 online/offline 隔离、最小候选、外部验证、保留/边界/安全集与回滚；教学样例本身还包含小样本、失败召回和负迁移，不能直接复制成生产收益。[Chapter 9 审计结论](./news-review-chapter9-evidence.md#L9-L13) · [评价、owner 路由与离线进化](./news-review-chapter9-evidence.md#L31-L70) · [现有 policy gate 可复用部分](./news-review-chapter9-evidence.md#L98-L110)

## 1. 先统一术语，否则能力表会骗人

厂商都可能写“experiment”“A/B”“rollback”或“agent eval”，但它们不自动满足 Tracefold 的语义。本文采用下面的严格定义：

| 术语 | 本文要求 | 常见但不等价的功能 |
|---|---|---|
| versioned dataset | 每次内容变化产生可固定、可回放、不可静默漂移的身份 | Git 中的一份 CSV、可编辑的 latest dataset |
| blind pairwise | 人看不到 stable/candidate、结果和价格标签；服务端保存 arm mapping，并支持纠正/仲裁 | 两列 side-by-side、LLM `select-best` |
| temporal holdout | candidate 注册后才形成、生成器无读取凭据的未来时间窗 | 从现有 dataset 随机切 `test_set` |
| stateful sequential replay | 两臂分别按时间推进；本臂前一次真实“送达”会改变本臂后续 told ledger、窗口与决策 | 单 case 内 multi-turn session、并行逐行评测 |
| shadow | 同一线上输入异步跑 candidate、无副作用、stable 仍是唯一权威，结果可审计 | 生产 trace 上跑一个 scorer、离线回放旧数据 |
| canary | 每个 Event 只由一个 arm 掌权，不双发；有预算、停止条件、CAS 激活和自动/人工回滚证据 | Prompt 标签叫 `canary`、Dashboard 做 A/B 分组 |
| rollback | 恢复上一稳定 image/config/control state，并写 durable receipt | 把 Prompt registry 的 `production` alias 指回旧版 |
| production tracing | 采集真实运行 span/输入输出/版本/反馈并可设 retention/redaction | 只记录评测过程的 trace |

因此，能力矩阵里的 `△` 不是“差”，而是“可以提供零件，但 Tracefold 仍须实现协议”；`—` 表示在本次一手资料中没有发现一等能力，**不表示理论上不能用自定义代码拼出**。

## 2. OpenAI Evals 与 Prompt Optimizer：已确认的弃用事实

截至 2026-08-21，官方状态是：

- OpenAI 在 2026-06-03 发布 Evals 平台弃用通知；2026-10-31 转只读，2026-11-30 dashboard 与 API 关闭，相关 graders 也在迁移范围。[OpenAI API Deprecations](https://developers.openai.com/api/docs/deprecations)
- OpenAI 的官方迁移 Cookbook 明确说明正在收尾 Evals 产品，并推荐迁移到开源 Promptfoo；推荐理由包括本地/CI 可运行、配置可移植和不绑定单一 provider。[Moving from OpenAI Evals to Promptfoo](https://developers.openai.com/cookbook/examples/evaluation/moving-from-openai-evals-to-promptfoo)
- 这项推荐针对 **eval execution 的迁移落点**，不是承诺完全等价替换 Evals 的审阅、历史结果、所有 grader 与 provider。官方迁移稿明确要求 unsupported graders、tools/custom providers 和历史数据另行处理；Tracefold 仍需做 adapter 验证，不能把“官方推荐”读成“零迁移成本”。[官方迁移稿源文件](https://github.com/openai/openai-cookbook/blob/main/examples/evaluation/moving-from-openai-evals-to-promptfoo.md)
- 依赖 Evals dataset 的 Prompt Optimizer 同期弃用：2026-10-31 转只读，2026-11-30 关闭。官方还特别要求人工检查优化后的 Prompt，因为优化可能导致回归。[Prompt Optimizer](https://developers.openai.com/api/docs/guides/prompt-optimizer)
- OpenAI 的评测最佳实践仍然有效：评测应覆盖真实生产分布，持续记录日志，自动化能自动化的部分，用人类反馈校准 LLM judge，并偏好 pairwise/classification 而不是开放式“感觉分”。[Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)

需要避免一个不准确说法：[`openai/evals`](https://github.com/openai/evals) 开源仓库截至快照日仍未 archived，根 LICENSE 是 MIT（部分数据集另有许可证），但没有 GitHub Release。**被明确弃用的是 OpenAI 托管 Evals 产品面和 dataset-backed Prompt Optimizer，不是官方公告说整个 GitHub 仓库已删除。** 对 Tracefold 的实际结论仍是 `AVOID`：不要以它作为新闭环的战略依赖；如已有托管 Evals 工作流，应迁移而不是扩建。

## 3. 维护面与许可证快照

GitHub 数据通过各官方仓库的公开 metadata/release/tag 与默认分支读取。最近提交只能说明近期有人维护，不代表 API 稳定；没有 GitHub Release 也不等于无人维护。

| 项目 | 许可证边界 | Stars（旁证） | 最新 GitHub Release / 活跃快照 | 本文结论 |
|---|---|---:|---|---|
| [Promptfoo](https://github.com/promptfoo/promptfoo) | [MIT](https://github.com/promptfoo/promptfoo/blob/main/LICENSE) | 24,419 | [0.122.0](https://github.com/promptfoo/promptfoo/releases/tag/0.122.0)，2026-08-04；main 2026-08-21 | **WRAP（有条件采用）** |
| [Langfuse](https://github.com/langfuse/langfuse) | [core MIT + 指定 EE 目录商业许可](https://github.com/langfuse/langfuse/blob/main/LICENSE)；不能笼统称“全仓 MIT” | 33,496 | 按发布时间 [v3.225.4](https://github.com/langfuse/langfuse/releases/tag/v3.225.4)，2026-08-20；GitHub `Latest` 标记另为 [v4.15.0](https://github.com/langfuse/langfuse/releases/tag/v4.15.0)，2026-08-19；main 2026-08-20 | **DEFER / WRAP-LATER** |
| [Arize Phoenix](https://github.com/Arize-ai/phoenix) | [Elastic License 2.0](https://github.com/Arize-ai/phoenix/blob/main/LICENSE)；可自托管，但不是 OSI 开源，且有限制作为托管服务提供 | 11,132 | [arize-phoenix-v20.3.0](https://github.com/Arize-ai/phoenix/releases/tag/arize-phoenix-v20.3.0)，2026-08-17；main 2026-08-20 | **AVOID V1** |
| [MLflow](https://github.com/mlflow/mlflow) | [Apache-2.0](https://github.com/mlflow/mlflow/blob/master/LICENSE.txt) | 27,598 | [v3.15.1](https://github.com/mlflow/mlflow/releases/tag/v3.15.1)，2026-08-03；main 2026-08-21 | **AVOID V1；已有企业标准时再 WRAP** |
| [Opik](https://github.com/comet-ml/opik) | [Apache-2.0](https://github.com/comet-ml/opik/blob/main/LICENSE) | 21,506 | [2.2.35](https://github.com/comet-ml/opik/releases/tag/2.2.35)，2026-08-20；main 2026-08-20 | **AVOID V1** |
| [DeepEval](https://github.com/confident-ai/deepeval) | [Apache-2.0](https://github.com/confident-ai/deepeval/blob/main/LICENSE.md) | 17,746 | [v4.1.7](https://github.com/confident-ai/deepeval/releases/tag/v4.1.7)，2026-07-29；main 2026-08-20 | **AVOID** |
| [Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai) | [MIT](https://github.com/UKGovernmentBEIS/inspect_ai/blob/main/LICENSE) | 2,595 | 无 GitHub Release；最新 tag [0.3.259](https://github.com/UKGovernmentBEIS/inspect_ai/tree/0.3.259)，2026-08-16；main 2026-08-21 | **AVOID（当前问题不匹配）** |
| [DSPy](https://github.com/stanfordnlp/dspy) | [MIT](https://github.com/stanfordnlp/dspy/blob/main/LICENSE) | 37,463 | [3.3.0](https://github.com/stanfordnlp/dspy/releases/tag/3.3.0)，2026-08-03；main 2026-08-20 | **AVOID 当前架构迁移** |
| [GEPA](https://github.com/gepa-ai/gepa) | [MIT](https://github.com/gepa-ai/gepa/blob/main/LICENSE) | 6,174 | [v0.1.4](https://github.com/gepa-ai/gepa/releases/tag/v0.1.4)，2026-07-15；main 2026-08-19 | **DEFER / WRAP-LATER** |

许可证判断的落点：Tracefold 可以依法自托管上述 MIT/Apache 项目；Phoenix 的 ELv2 和 Langfuse 的 core/EE 混合边界必须进入 ADR/采购审查。许可证合适并不等于应该增加第二个数据平面。

## 4. 能力矩阵：通用评测与观察面

符号：`✓` 一手文档中是一等能力；`△` 可通过文件、API、插件或自定义代码实现，但不满足本文完整协议；`—` 未见一等能力。

| 项目 | Dataset / 版本 | 实验比较 | 人工标注 | LLM judge | 盲 pairwise | CI | 生产 tracing | Prompt optimizer | Self-host |
|---|---|---|---|---|---|---|---|---|---|
| Promptfoo | ✓ dataset；△ 版本依赖文件/Git | ✓ | △ 结果编辑、评论、Human Eval | ✓ | △ `select-best`/并排，不是服务端盲评协议 | ✓ | — OTel 只覆盖 eval run | ✓ 内建 optimizer | △ 官方基础 self-host 明说不建议生产使用 |
| Langfuse | ✓ 每次 dataset 变更产生版本 | ✓ | ✓ annotation queue | ✓ | △ 有 side-by-side，未覆盖 #112 盲映射/仲裁 | ✓ | ✓ OTel 生产 trace | — | ✓ core；治理功能需核对 EE 边界 |
| Phoenix | ✓ | ✓ | ✓ span/document annotation | ✓ | △ | △ SDK/CLI | ✓ OpenInference/OTel | — | ✓，但 ELv2 |
| MLflow GenAI | △ lineage/digest/history 很强，未见等价的 sealed temporal manifest | ✓ | △ review queues 官方标为 Experimental | ✓ | △ | ✓ pytest/regression testing | ✓ | ✓ GEPA/MetaPrompt | ✓ |
| Opik | ✓ immutable dataset versions | ✓ | ✓ annotation queue | ✓ | △ | ✓ pytest | ✓ online evaluation/monitoring | ✓ 多种 optimizer | ✓；本地 Compose 与生产 K8s 路径分开 |
| DeepEval | △ 本地 dataset；协作版本偏向 Confident AI 产品 | △ | — OSS 库无完整 ReviewDesk | ✓ | △ 自定义 metric | ✓ pytest | △ 完整在线协作面偏向厂商平台 | ✓ GEPA/MIPRO 等 | △ 本地库可自管，不等于完整平台可自托管 |
| Inspect AI | △ 文件/Git | ✓ eval logs / rescore | △ 可编辑 score，无队列协议 | ✓ model grader | △ 自定义 scorer | △ CLI 可放 CI | — 重点是 eval log，不是生产 APM | — | △ 本地 runner，无协作控制面 |
| DSPy | △ Python 数据/Git | △ `Evaluate` | — | ✓ metric/judge 可编程 | △ 自定义 metric | △ 常规 Python CI | — 原生无生产 tracing 平台 | ✓ | △ 本地库 |
| GEPA | △ Python train/val/test | △ run/Pareto 结果 | — | △ evaluator 提供 | △ evaluator 提供 | △ 常规 Python CI | — | ✓ 核心能力 | △ 本地库 |

### 4.1 一手证据索引

- Promptfoo：配置将 prompts 与 test cases/assertions 组合运行，可读 CSV/Sheets；支持确定性和 model-graded assertions、`select-best`、CI/GitHub Action、JSON/CSV/JSONL/JUnit 输出。当前版本也有一等 Prompt optimizer：先跑 baseline、生成候选，并可用 `--validation-split` 留出验证；因此不能再把它写成“只做 eval”。这个 split 仍不是 #112 的 candidate-unseen future holdout。[Configuration](https://www.promptfoo.dev/docs/configuration/guide/) · [Model-graded metrics](https://www.promptfoo.dev/docs/configuration/expected-outputs/model-graded/) · [Prompt optimization](https://www.promptfoo.dev/docs/usage/prompt-optimization/) · [CI/CD](https://www.promptfoo.dev/docs/integrations/ci-cd/) · [Outputs](https://www.promptfoo.dev/docs/configuration/outputs/)
- Promptfoo Web UI 可以保存 score/comment 并比较输出，但其基础 self-host 指南明确说 SQLite 方案不适合生产级多团队部署；Tracing 文档描述的是评测期间的 OTel spans，不应被宣传成完整生产 observability store。[Web UI](https://www.promptfoo.dev/docs/usage/web-ui/) · [Self-hosting](https://www.promptfoo.dev/docs/usage/self-hosting/) · [Tracing](https://www.promptfoo.dev/docs/tracing/)
- Langfuse 把 production traces、dataset/experiment、online/offline evaluator、annotation queue 连成一个通用循环；dataset 每次增删改/归档会形成版本。Prompt 版本不可变，可移动 label 做快速指针回退；这不等于 Tracefold 的 image/circuit rollback。[Evaluation concepts](https://langfuse.com/docs/evaluation/core-concepts) · [Dataset versioning](https://langfuse.com/docs/evaluation/experiments/datasets) · [Annotation queues](https://langfuse.com/docs/evaluation/evaluation-methods/annotation-queues) · [Prompt version control](https://langfuse.com/docs/prompt-management/features/prompt-version-control) · [Prompt CI/CD](https://langfuse.com/resources/engineering/prompt-cicd) · [Observability data model](https://langfuse.com/docs/observability/data-model)
- Phoenix 提供 versioned datasets/experiments、LLM/code evaluators、人工/模型 annotations、Prompt versions/tags 和 OpenInference tracing。Phoenix OSS 的 production online eval/alerting 文档会指向商业 Arize AX，不能把两者能力混写。[Datasets](https://arize.com/docs/phoenix/learn/datasets-and-experiments/datasets-concepts) · [Run experiments](https://arize.com/docs/phoenix/datasets-and-experiments/how-to-experiments/run-experiments) · [Annotations](https://arize.com/docs/phoenix/tracing/concepts-tracing/annotations-concepts) · [Prompts](https://arize.com/docs/phoenix/prompt-engineering/quickstart-prompts/quickstart-prompts-ui) · [License](https://arize.com/docs/phoenix/self-hosting/license)
- MLflow GenAI 官方定位是 trace → feedback → dataset → evaluate → deploy 的持续循环；支持 scorer、Prompt Registry alias、GEPA/MetaPrompt 优化和 pytest regression testing。Review Queue 是较新的 Experimental 能力，不能直接承担 #112 的永久 ReviewDesk 合同。[GenAI overview](https://mlflow.org/docs/latest/genai/) · [Evaluation datasets](https://mlflow.org/docs/latest/genai/datasets/) · [Scorers](https://mlflow.org/docs/latest/genai/eval-monitor/scorers/) · [Review queues](https://mlflow.org/docs/latest/genai/assessments/review-queues/) · [Prompt optimization](https://mlflow.org/docs/latest/genai/prompt-registry/optimize-prompts/) · [Regression testing](https://mlflow.org/docs/latest/genai/eval-monitor/regression-testing/)
- Opik 把 tracing、datasets/experiments、human/LLM evaluation、Prompt registry、pytest 和 optimizers 做成一体化平台；dataset version 是 immutable，能 pin/rollback，optimizer 文档还明确区分 training、validation 与 held-out test。这里的 dataset 或 Prompt rollback 仍不是生产消息管线的回滚。[Official repository](https://github.com/comet-ml/opik) · [Dataset versions](https://www.comet.com/docs/opik/v1/evaluation/manage_datasets) · [Evaluation overview](https://www.comet.com/docs/opik/latest/evaluation/overview) · [Optimization datasets](https://www.comet.com/docs/opik/development/optimization-runs/optimization/define_datasets) · [Prompt version control（官方仓库文档源）](https://github.com/comet-ml/opik/blob/main/apps/opik-documentation/documentation/fern/docs-v2/prompt_engineering/version-control.mdx) · [Pytest integration](https://www.comet.com/docs/opik/v1/testing/pytest_integration) · [Production monitoring](https://www.comet.com/docs/opik/v1/production/production_monitoring) · [Self-hosting](https://www.comet.com/docs/opik/v1/self-host/local_deployment)
- DeepEval 是 pytest 风格 Python eval library，重点是 LLM/agent/multi-turn metrics 与 Prompt optimizers；多人协作、dataset versions 和 production UI 多数落在 Confident AI 产品面。不能只看 `deepeval` 的 Apache 许可证便推导整个托管产品可自托管。[Getting started](https://deepeval.com/docs/getting-started) · [Datasets](https://deepeval.com/docs/evaluation-datasets) · [Tracing](https://deepeval.com/docs/evaluation-llm-tracing) · [Prompt optimization](https://deepeval.com/docs/prompt-optimization-introduction)
- Inspect AI 是 UK AI Security Institute 维护的评测框架，强项是 tool/agent/multi-turn/sandbox 任务、model graders、可重评分的 eval logs；不是生产学习控制面。[Official docs](https://inspect.aisi.org.uk/) · [Scoring](https://inspect.aisi.org.uk/scoring.html) · [Model-graded scoring](https://inspect.aisi.org.uk/scorers.html) · [Eval logs](https://inspect.aisi.org.uk/eval-logs.html)
- DSPy 是声明式 LM program 与 optimizer 框架；`Evaluate` 和 optimizers 很成熟，但它不是 dataset/review/release 平台。[Official repository](https://github.com/stanfordnlp/dspy) · [Metrics and evaluation](https://dspy.ai/learn/evaluation/metrics/) · [Optimization](https://dspy.ai/learn/optimization/optimizers/)
- GEPA 是反思式 Prompt 演化算法/库，能用 train/val/test 与 evaluator feedback 搜索 Pareto candidates；论文报告了跨任务收益和更少 rollouts，但它优化的是调用者给出的 metric，不提供 metric 的真实性、数据隔离或发布安全。[Official repository](https://github.com/gepa-ai/gepa) · [Optimize Anything API](https://gepa-ai.github.io/gepa/api/optimize_anything/optimize_anything/) · [GEPA paper](https://arxiv.org/abs/2507.19457)

Pairwise 还需单独勘误：Promptfoo `select-best` 是 LLM judge，Langfuse compare annotation 是可见的并排标注，Phoenix Pairwise Evaluator 虽会随机顺序并支持 swap-confirm，但对象仍是自动 judge；三者都不是 #112 的人工 blind pairwise ReviewDesk。[Promptfoo LLM judge](https://www.promptfoo.dev/docs/guides/llm-as-a-judge/) · [Langfuse compare annotation](https://langfuse.com/changelog/2025-10-23-annotate-from-compare-view) · [Phoenix pairwise evaluator](https://arize.com/docs/phoenix/evaluation/server-evals/code-evaluators/pairwise)

## 5. 能力矩阵：#112 真正困难的部分

`✓` 仍表示无需 Tracefold 自己补关键语义。结果很清楚：**没有候选在这一表得到 `✓`。**

| 项目 | unseen temporal holdout | 两臂 stateful sequential replay | 真 shadow | 单权威 canary | 生产 rollback receipt |
|---|---|---|---|---|---|
| Promptfoo | △ 有 `validation-split`，不能证明候选未见 | — custom provider 能自写，不算原生协议 | — | — | — |
| Langfuse | △ dataset 可固定，隔离/注册时钟须自建 | — task function 能自写，不算原生协议 | △ 可观察 A/B/release，不是无副作用 shadow runner | — | △ Prompt label 回退而已 |
| Phoenix | △ 有 dataset split，未来窗/ACL 须自建 | — | — | — | △ Prompt version/tag 而已 |
| MLflow GenAI | △ | — | — | — | △ Prompt alias/model deploy 能提供零件，不满足 #112 receipt |
| Opik | △ 有 train/validation/test，未来窗/ACL 须自建 | — | — | — | △ Prompt/dataset rollback，不是消息管线 rollback |
| DeepEval | △ | — multi-turn case 不等于跨 Event ledger | — | — | — |
| Inspect AI | △ | — stateful task 可编程，不是读者顺序协议 | — | — | — |
| DSPy | △ | — program 可编程，不是原生 reader replay | — | — | — |
| GEPA | △ 有 `test_set` 参数，但访问隔离/未来时间窗须自建 | — evaluator callback 能自写，不算原生协议 | — | — | — |

表里的 rollback `△` 只表示“存在旧 Prompt 指针”这一块零件：Langfuse 的 weighted split/canary 需要应用代码组合，Phoenix 只是移动 Prompt tag，MLflow 只是重指 Prompt alias。它们都不回滚代码、模型、policy state、消息副作用或整个部署。[Langfuse Prompt CI/CD](https://langfuse.com/resources/engineering/prompt-cicd) · [Phoenix Prompt tags](https://arize.com/docs/phoenix/prompt-engineering/how-to-prompts/tag-a-prompt) · [MLflow Prompt Registry](https://mlflow.org/docs/latest/genai/prompt-registry/)

这张表是选型的主结论。Tracefold 的难点不是“能不能给两段文本打分”，而是：

1. stable/candidate 每一臂有独立、随本臂发送结果演化的 ledger；现有 policy gate 已按时间重建状态，而不是无状态 map。[`eval/harness.py` 解释 second-order replay](../../src/tracefold/news/eval/harness.py#L1-L19) · [`replay_corpus()` 的独立 ledger](../../src/tracefold/news/eval/harness.py#L285-L344)
2. Prompt candidate 必须重新问模型；当前 gate 明确冻结 verdict，只能评 `decide()`，这是产品代码自己承认的边界。[`harness.py`](../../src/tracefold/news/eval/harness.py#L13-L19)
3. 发布阶段必须保持 one Event → one authoritative arm → at most one card；这是业务副作用协议，不是 Prompt registry 功能。[#112 在线不变量](https://github.com/AnalyThothAI/tracefold/issues/112)
4. Prompt、schema 目前已有 byte hash/version 身份，但身份固定不等于质量评估。[Prompt/schema SHA](../../src/tracefold/news/agents/prompts/__init__.py#L185-L224)

## 6. 逐项裁决：adopt / wrap / avoid

### 6.1 Promptfoo：`WRAP`，一日 spike 通过才采用

适合拿来做：

- Prompt/schema 的静态 regression cases；
- deterministic assertions、LLM-as-judge、格式/禁词/引用检查；
- 开发机和 CI 的 before/after 展示；
- 如果未来从 OpenAI Evals 迁移，作为官方推荐落点。

Promptfoo 当前确实已有自动 Prompt optimizer 和 `--validation-split`；本方案仍在 V1 禁用它，因为一个 optimizer 只能放大现有 evaluator，不能创造 gold truth、访问隔离或未来 holdout。未来若选择 standalone GEPA，就不再启用 Promptfoo optimizer；二者只能有一个实现 `ProposalGenerator` adapter，不能形成两套候选身份。

不能让它做：

- `DatasetManifest`、`EvaluationReport` 或 release truth；
- 跨 Event 的顺序双臂 ledger；
- blind pairwise 的 arm mapping、reviewer 身份、supersession；
- shadow/canary/rollback；
- 生产在线 Agent 依赖。

Spike 只有同时满足以下条件才引入：

1. Promptfoo custom provider/HTTP provider 调用 Tracefold **同一个** `SemanticJudge` contract，而不是另写一条“看起来相同”的 provider 调用；
2. 输入由 `CandidateEvaluator` 给出 frozen evidence + 本臂 ledger，Promptfoo 不自己重排或并行跨 case；
3. 输出 JSON 能完整记录 Prompt/schema/model/provider/execution SHA、token/latency/error，且被导入 content-addressed `EvaluationReport`；
4. record mode miss 明确失败，绝不静默调用 live provider；
5. Node 工具只进 dev/CI image，不进 News worker runtime；
6. adapter 代码保持薄。如果为了顺序重放需要把 #112 的领域编排重写进 JavaScript，就停止 spike，保留 Python 原生 evaluator。

因此这不是“采购 Promptfoo 重做 CandidateEvaluator”，而是让 CandidateEvaluator 在少量通用场景中调用一个可替换工具。

### 6.2 Langfuse：`DEFER / WRAP-LATER`，只能做观察 sidecar

它在候选中最适合未来的 tracing/experiment 浏览需求：OTel data model、版本化 dataset、annotation queues 和 Prompt registry 都比较完整。但直接让它承担 ReviewDesk 会丢失 #112 的关键合同：task-scoped redaction、服务端 blind mapping、append-only supersession、乐观并发、eventless miss、仲裁和“页面不可 promote”。

V1 不部署。只有出现下面的真实需求时才试点：operator 无法用现有复盘页定位跨运行 trace，且维护自建检索 UI 的成本已被量化。试点规则：

- 单向 OTel/export，PostgreSQL 仍是唯一业务 truth；
- 先用 synthetic/redacted 数据验证 retention、删除、权限和 EE license；
- Langfuse score/label 不自动回写 release gate；
- 不在 Langfuse 的 Prompt label 上实现生产流量选择；
- 能无损停掉 sidecar，不影响 Triage/Delivery。

### 6.3 Phoenix：`AVOID V1`

Phoenix 的 datasets/experiments/annotations/OTel 很成熟，尤其适合离线 tracing lab。但它与 Langfuse 重叠，ELv2 又比 MIT/Apache 增加许可证判断；Phoenix OSS 也没有 #112 的发布控制。没有理由同时引入两个观察平台。若未来组织已标准化 Arize/Phoenix，可作为只读 exporter 重新评估；当前不选。

### 6.4 MLflow GenAI：`AVOID V1；组织已有 MLflow 时再 WRAP`

MLflow 是功能最广、Python 生态最顺手的候选之一：tracking、scorers、Prompt Registry、优化和 CI 都有。但正因为它宽，最容易诱导 Tracefold建设第二套 generic experiment platform。Review Queues 尚标 Experimental；Prompt alias 与模型部署也不自动满足 one-card canary。除非组织已经运维 MLflow 并希望复用现成基础设施，否则为了一个 News 闭环新增整个平台不符合 KISS。

### 6.5 Opik：`AVOID V1`

Opik 在 Apache-2.0 项目中最接近 all-in-one：immutable dataset versions、experiments、annotations、online evaluation、pytest、optimizers 都有。如果这是没有业务审计库的绿地项目，它会进入 shortlist；Tracefold 已有 PostgreSQL truth、trace 和 policy replay，真正缺口仍是领域 review/release 协议。部署 Opik 只会增加第二份 dataset、Prompt 和 score 身份，并不能删除核心自建工作，所以 V1 不选。

### 6.6 DeepEval：`AVOID`

DeepEval 适合作为 Python 单元/集成评测库，但它与 Promptfoo 的通用 metric/CI 角色重复，协作与生产能力又更多依赖 Confident AI 产品。KISS 下不同时引入两套 assertion/optimizer 生态。只有 Promptfoo 缺少某个经验证、不可简单实现的 metric 时，才把那一个 metric 独立评估，不整体采用平台。

### 6.7 Inspect AI：`AVOID（当前问题不匹配）`

Inspect AI 对 tool-use、sandbox、复杂多轮 agent 安全评测很有价值；Tracefold 热路径故意只有一次结构化 Triage，没有 tool agent 或 sandbox trajectory。为了未来可能存在的 agent 预装 Inspect 是过度设计。若将来真实出现“模型调用工具并改变环境”的 program candidate，再开独立 Issue 评估。

### 6.8 DSPy：`AVOID 当前架构迁移`

DSPy 的优化器成熟，但采用它会把当前 byte-frozen LangChain structured call 重写成 DSPy program。这样一次实验同时改变 program、Prompt 生成方式、执行 contract，违反 #112 V1 的单变量 `prompt | policy` 原则。不能为了得到 optimizer 而迁移在线架构；未来可以让 standalone GEPA 读取导出的开发集，不必让生产 Agent 变成 DSPy。

### 6.9 GEPA：`DEFER / WRAP-LATER`，只做 ProposalGenerator

GEPA 是唯一值得为“候选生成”保留接口位置的库，但现在就运行它会优化一个尚未可信的 reward。它会非常高效地放大 evaluator 的偏差；若当前 labels 把不同失败 owner 折成一个分数，它只会更快地产生错误 Prompt。

启用前必须同时满足：

1. Review v2 已积累 development/retention/safety 多维 judgments，且 reviewer disagreement 可见；
2. `CandidateEvaluator` 已对人工编写的至少一个 Prompt candidate 完成 stable/candidate 两臂真实模型重放；
3. temporal holdout 的注册时间、凭据隔离和 `UNKNOWN` 路径通过测试；
4. optimizer 只能读取 development artifact，不能读取 validation/hidden holdout；
5. 输出只是 `ProposalReceipt + CandidateManifest + patch`，需要人工 review，且一次只改 Prompt；
6. 保留所有失败 candidates、预算和运行记录；不自动 apply、promote 或回滚。

不同时引入 DSPy/MLflow/Opik/DeepEval 各自的 GEPA 包装层；选一个最薄的 standalone adapter，避免同一算法有四套运行身份。

## 7. 映射到 #112 的四个责任区

### 7.1 ReviewDesk：必须 build

| #112 合同 | 通用平台能给的零件 | 为什么仍要 build |
|---|---|---|
| 分层队列、证据展示 | Langfuse/Phoenix/Opik annotation queue | News 的 evidence snapshot、reader receipt、market reaction 需要 task-scoped 裁剪 |
| blind pairwise | 平台 side-by-side 或 automated pairwise | #112 要服务端隐藏 arm mapping、提交前不 reveal、提交后按权限 reveal |
| append-only judgment + correction | 多数平台可编辑 score | 当前实现是 overwrite；目标要 `supersedes_review_id`、冲突 409、完整审计 |
| eventless miss | 通用 dataset row 可存文本 | Tracefold 需要把“pipeline 根本没建 Event”作为 recall 上界证据，而不是伪造 Event |
| 不得 promote | 平台可能把 experiment/prompt/release 放同一 UI | ReviewDesk 必须只收 judgment，不创建 candidate、不改 threshold、不发布 |

当前代码证据：旧 label 按确定性 key `ON CONFLICT DO UPDATE`，是“可改的一行”，不是 append-only judgment。[`repository.insert_label()`](../../src/tracefold/news/repository.py#L815-L856)；详情页按钮只复制 CLI 命令，API 读而不写。[`NewsEventDetailPage.tsx`](../../web/src/features/news/ui/detail/NewsEventDetailPage.tsx#L422-L462)。因此平台 UI 不是当前断点的最短修复，自建一个窄 `ReviewDesk` HTTP/CLI seam 才是。

### 7.2 CandidateEvaluator：build 编排，Promptfoo 仅可插拔

`CandidateEvaluator` 自己拥有：

- content-addressed dataset/candidate/run/report manifests；
- stable/candidate one-variable diff 校验和 trusted root；
- exact evidence/input 构建；
- 两臂独立、按时间推进的 model + policy + delivery simulation；
- blind task 生成、required strata、置信区间、`PASS/FAIL/UNKNOWN`；
- temporal holdout、预算、provider outage 和 stale stable 语义；
- sealed `ReleaseEvidence`，且永不发布。

可选 Promptfoo adapter 只拥有：

- 单次已构建输入的 provider execution；
- 通用 deterministic/model-graded assertions；
- CI 可读输出与本地对比 UI。

当前 `validate_candidate()` 已有 asymmetric boundary/retention/noise/duplicate gates，但只针对 frozen verdict policy replay。[`validate_candidate()`](../../src/tracefold/news/eval/harness.py#L433-L496)。正确演进是把它保留为 `CandidateEvaluator` 的 policy fast path，而不是用厂商 experiment runner 覆盖它。

### 7.3 Proposal generator：V1 build 一个薄凭证；GEPA 后置

V1 不需要“Agent 自动学习服务”。一个 Git-side 命令足够：

```text
accepted failure cluster
  -> human | one cold Coding Agent call
  -> proposed single-variable diff
  -> ProposalReceipt（读过哪些 development evidence）
  -> candidate registration（先于未来 holdout）
```

GEPA 未来实现同一个内部 adapter，但权限更少：只有 development artifact，输出候选，不拥有 ReviewDesk、trusted root、holdout、stable pointer 或 deploy credential。这样 optimizer 可以被删除，不会破坏学习链。

### 7.4 发布控制：必须 build，沿用现有 Git/CI/image/control

没有候选库能替代以下 News 业务协议：

- shadow 异步、不影响 Triage deadline、不发送卡；
- canary 每个 Event 恰有一个 authoritative arm，不允许 stable/candidate 双发；
- activation 使用 active stable/candidate parent 的 CAS，避免旧报告发布到新 stable；
- canary 有 sample/time/token/error/delivery budget 和 circuit breaker；
- holdout `FAIL/UNKNOWN` 不能被“线上看起来没出错”洗白；
- rollback 恢复上一 image/config/control 并写 receipt，ambiguous send 仍按现有 one-attempt 语义；
- 最终 promotion 只通过人工批准的 Git/CI/image 发布。

Langfuse/Phoenix/MLflow/Opik 的 Prompt version/alias/tag 可以作为元数据或快速 Prompt 指针，但不能证明这些不变量。把 alias 叫 rollback 会制造危险的虚假安全感。

## 8. 推荐的最小落地顺序

### Phase 0：不引入任何平台

1. 先实现 #112 的不可变 evidence、ReaderReceipt truth、Review v2 与 ReviewDesk 直接提交。
2. 把旧 `label v1` 只读迁移为 legacy evidence，不继续让 `_outcome()` 把 `wrong_direction/late/missed/good` 压成同一个 `moved`。[`offline._outcome()`](../../src/tracefold/news/eval/offline.py#L51-L62)
3. 把现有 policy sequential replay 迁入新的 CandidateEvaluator 私有实现，补 trusted-root 必填和 required-stratum empty → `UNKNOWN`。
4. 用一个人工 Prompt candidate 打通双臂 live-model replay、blind pairwise 与 candidate-unseen temporal holdout。没有这个 proof，不讨论 optimizer。

### Phase 1：Promptfoo 一日 spike（可失败、可不采用）

用 synthetic/fixture 数据验证上一节六条 acceptance。目标不是“让测试数量变多”，而是确认它能否作为薄 adapter，而不复制 SemanticJudge contract。成功则仅加入 dev/CI；失败则记录 ADR 并使用 Python 原生 executor。

### Phase 2：shadow/canary/rollback

CandidateEvaluator 的 holdout 通过后再实现 cold shadow；shadow 只验证兼容性、成本、延迟、结构化错误和分布。通过后才做小流量单权威 canary。所有 stage 都输出 sealed receipt，人批准后走正常部署。

### Phase 3：有证据才加工具

- operator 确有跨运行 trace 检索痛点：单独试点 Langfuse OTel read-only sink；
- 已有多轮可信人工数据且 proposal 成本高：试点 standalone GEPA；
- 组织已经统一 MLflow/Arize/Opik：再评估一个 adapter；
- 不同时运行多个 experiment truth store 或 optimizer。

## 9. 删除、降级和明确不做

| 现有/拟议能力 | 处理 | 理由 |
|---|---|---|
| OpenAI 托管 Evals / dataset-backed Prompt Optimizer 新接入 | **删除计划 / 不新增** | 官方已有关闭日期 |
| “命中复盘”把 1H/4H 涨跌当学习 reward | **降级为事后市场观察/discovery** | 页面代码自己也承认涨跌不证明因果或应推；不能进入 optimizer reward。[`NewsReviewPage.tsx`](../../web/src/features/news/ui/review/NewsReviewPage.tsx#L23-L32) |
| legacy 单标签作为 release gold | **冻结为 legacy evidence** | owner 与多个质量维度被折叠，且当前行可覆盖 |
| 在热路径增加 Reviewer/Reflection Agent | **不做** | 增加延迟/成本却不产生独立真值，违反 #112 |
| nightly 自动改 Prompt、自动 promote | **不做** | proposal generator 与 evaluator 同源会自证，且无 unseen holdout |
| 在线 Codex Skill/知识包参与每条新闻判断 | **不做** | Skill 是 Coding Agent/离线提案工具，不是 News 产品 runtime contract；线上知识必须是显式、版本化、可 replay 的产品输入 |
| 同时部署 Langfuse + Phoenix + MLflow + Opik | **不做** | 四个第二真相源，增加身份漂移和运维，不增加领域验证力 |
| 为获得 DSPy optimizer 重写 SemanticJudge | **不做** | 将 Prompt 实验变成 program migration，失去单变量归因 |
| 用 Prompt registry alias 冒充 canary rollback | **不做** | 不覆盖 one-card、CAS、circuit、delivery ambiguity 和 image receipt |

## 10. Build vs Buy 最终决策表

| #112 责任 | 决策 | 选用/候选 | 可信根 |
|---|---|---|---|
| Evidence/DatasetManifest | **Build** | PostgreSQL + content-addressed manifests | Tracefold PostgreSQL / sealed artifact |
| ReviewDesk | **Build** | 窄 HTTP + CLI + 页面；平台不可替代 | append-only ReviewJudgment |
| CandidateEvaluator 编排 | **Build** | 复用 Python policy replay，新增 true model two-arm | EvaluationReport |
| 通用 Prompt assertions / CI 展示 | **Wrap，条件采用** | Promptfoo | 导入后的 EvaluationReport，而非 Promptfoo DB |
| Production tracing UI | **Defer** | 若有真实需求，先试 Langfuse read-only OTel | PostgreSQL 仍是业务 truth |
| Proposal generation | **Build thin first；Wrap later** | 人工/Coding Agent；未来 standalone GEPA | ProposalReceipt；无发布权 |
| Shadow/canary/rollback | **Build** | 现有 Triage seam + control + Git/CI/image | ReleaseEvidence / rollback receipt |
| Generic all-in-one platform | **Avoid V1** | Phoenix、MLflow、Opik | 不新增第二 truth store |
| 另一套通用 eval library | **Avoid** | DeepEval、Inspect AI、DSPy（当前范围） | — |
| OpenAI Evals 产品面 | **Avoid / migrate** | 官方推荐 Promptfoo | — |

## 11. 决策验收标准

这个方案只有在以下结果都能被真实演示时才算“生产学习闭环”，而不是新玩具：

1. operator 在 ReviewDesk 对一个盲评 case 提交多维 judgment；第二次纠正产生 superseding row，不覆盖历史。
2. 一个 Prompt candidate 在相同 evidence、schema、model、policy 下，两臂按时间独立推进，后续 case 看见各自不同 ledger。
3. candidate 注册后才出现的未来数据进入 hidden temporal holdout；generator 的运行身份读不到它。
4. required stratum 为空、provider outage、预算耗尽或 reviewer 不一致时，结果是 `UNKNOWN`，绝不空集合 PASS。
5. shadow 不发卡；canary 对每个 Event 只选一个权威 arm；达到 stop condition 后能恢复 stable 并留下 receipt。
6. Promptfoo、Langfuse 或 GEPA 任一个被完全停用，核心闭环仍能运行、审计和回滚。
7. 市场价格只能帮助发现 review case，不能自动写 `should_push`、不能训练 optimizer、不能洗白 holdout failure。

达到这七点，系统才是“证据 → 人类判断 → 单变量候选 → 独立验证 → 有界上线 → 可回滚”的生产学习链；安装更多 Agent/Eval 平台并不会替代其中任何一步。
