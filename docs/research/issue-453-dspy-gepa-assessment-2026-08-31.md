# Issue #453 与 DSPy 3.3.1 / GEPA 的一致性审计

日期：2026-08-31

Tracefold 审计基线：`0cd0acee7a227fd7792d14c923345f44b097d178`

对象：[AnalyThothAI/tracefold#453](https://github.com/AnalyThothAI/tracefold/issues/453)

冻结 Dataset SHA：`e4558469ce7ca0f613264e7c6284038a45781eb64f4661663918d67e4c42059d`

Episode projection root：`deab3aa0477eee24d2bbd9fd81cbe0958eea1ce15804bfd2a9fb5456785901e2`

上游版本：DSPy `3.3.1`（commit [`638e155`](https://github.com/stanfordnlp/dspy/tree/638e155cf725236fe5d01b5332394a7bc128881d)），其精确依赖 GEPA `0.1.4`（commit [`8b0ce6c`](https://github.com/gepa-ai/gepa/tree/8b0ce6cd99a234f6b74daf37558a2ac0ce18f975)）。DSPy 3.3.1 自己也把 `gepa[dspy]==0.1.4` 固定为精确版本，而不是浮动依赖（[DSPy 3.3.1 `pyproject.toml`](https://github.com/stanfordnlp/dspy/blob/3.3.1/pyproject.toml#L31-L40)）。

## 结论先行

Issue #453 的核心方向符合 GEPA 的基本原理：把可重放的 Gold 变成逐例 score 与具体自然语言 feedback；用 train examples 产生反思信号、用独立 validation examples 选择候选；把 Stable 程序作为同一次搜索的 candidate 0；让 GEPA 只改 Prompt-owned component；最后仍由 Tracefold 的业务 gate 和未来 holdout 决定是否发布。

但 Issue 目前还不能支持“77 条数据已经足以建立完整、可靠的新闻推送优化数据集”这一强结论。更准确的判断是：

- **修完代码与数据合同后，可以跑一个诚实的端到端 pilot**：DSPy 没有更高的框架级最小样本硬门槛；但当前代码对该 Dataset 产生 `0 target`，而且 development release profile 的 retention coverage 仍不合格，所以现状不是可直接启动 GEPA 的 `GO`。
- **不能仅凭 77 条 / 8 个 residual 宣称优化有效或可泛化**：按当前整体准入规则只有 37 条 honest controls；#453 字面会把 8 个 residual 全部作为 target，若另行采纳 derived upstream-owner 排除才会收缩为 6 个。70/30 后 selection 都只有 2 个 target。它足以发现“链路是否真的工作”，不足以稳定估计不同 taxonomy 轴、来源、语言和事件类型上的真实改进。
- **不能仅凭 taxonomy 指标宣称新闻推送变好**：它最多证明 Prompt-owned 语义判断变好。新闻推送的最终收益仍须由 future holdout 上的发送/拦截质量、关键漏报、错误推送、重复、时效等业务结果证明；DSPy 只优化 metric 表达出来的目标。

需要在实现前修正或收紧的关键点有六个：

1. `Prediction(score, feedback)` 是获得 GEPA 核心反思能力的**强推荐契约**，不是“没有它框架就不能运行”的硬要求；裸 `float` 会运行，但只得到通用分数反馈。
2. feedback 是**提案信息**，不是 hard gate。任何 candidate-only regression、instruction bound 或 write-set 违规，必须进入 score/显式 Tracefold 候选 gate；只写进 feedback 不会阻止当前候选被接受。
3. `detailed_results` 只包含进入候选池的候选及其 validation 结果，不包含全部被拒提案，也不包含反思时实际看到的完整 feedback。`log_dir` 也不是完整的结构化 feedback receipt。
4. 必须显式持久化 `canonical_json(optimized.detailed_results.to_dict())`；仅保存 `program_state.json` 和 `log_dir`，进程退出后并没有一份安全、便携的官方 result JSON。
5. `log_dir` 必须是与 dataset/program/metric/run identity 绑定的全新目录。上游把同一目录解释为“resume”；而 resume 路径仍会先重新执行一次 seed validation，再载入旧 state，不能把它当作零重复的 fresh run。
6. 需要明确 70/30 的整数舍入规则，并处理稀疏 target minibatch。按 37 个 honest controls 和 8/6-target 两种口径，默认大小为 3 的 epoch-shuffled minibatch 约 52%–64% 会全是 control，先消耗 rollout 再因全满分跳过反思。

## 0. 对 Issue 固定 Dataset 的实测审计

本节使用上方完整 Dataset SHA 和本机 operator-owned 配置做只读、零 provider-call 检查；不复制新闻正文、模型 reasoning 或凭证。`readiness`、recorded taxonomy baseline、Dataset bind/root 校验都由当前代码执行。配置只确认 task LM `qwen3.8-27b` 与 reflection LM `deepseek-v4-pro` 已配置；没有发出真实调用，因此不声称凭据、endpoint 或模型当日可用。

可复核命令为：

```bash
uv run tracefold news learning readiness \
  --development e4558469ce7ca0f613264e7c6284038a45781eb64f4661663918d67e4c42059d \
  --out /tmp/tracefold-453-readiness.json
uv run tracefold news learning baseline \
  --dataset e4558469ce7ca0f613264e7c6284038a45781eb64f4661663918d67e4c42059d \
  --mode recorded \
  --out /tmp/tracefold-453-taxonomy-baseline.json
```

readiness artifact 绑定上方 projection root；recorded baseline report SHA 为 `c43afd38dac49458ea0ca01cb14dfde5842e867322ec4b3ac8fc82fed263e743`。

### 0.1 数据与当前 Objective 的真实形状

| 项目 | 实测值 | 含义 |
| --- | ---: | --- |
| episodes / independent clusters | 77 / 77 | exact cluster id 没有直接跨 split 复用。 |
| 时间覆盖 | 1.305 h、1 个自然日 | 能做 plumbing pilot，不能代表新闻流的时间分布。 |
| recorded Stable taxonomy | 69 exact / 77，8 residual | exact rate `0.896104`；event-family macro-F1 `0.913512`、subject micro-F1 `0.951807`、change accuracy `0.948052`、assertion macro-F1 `0.946966`。 |
| 当前 Objective Plan | 0 target、37 control、40 excluded | 当前实现明确把 taxonomy 当 diagnostic 排除，故会在花钱前拒绝。 |
| development coverage | boundary 66、retention 11、negative 69、safety 49、strata 7 | release profile 要求 retention ≥100；当前还差至少 89 个独立 retention clusters。 |
| eligible events | 106 | future validation 还需 Candidate 注册后的新窗口；不能把这个 development 数当 future evidence。 |

当前代码无法直接在这份 Dataset 上优化 taxonomy，不是 DSPy 限制，而是仓库尚未接线：Objective 不选 taxonomy target（[`objective.py`](https://github.com/AnalyThothAI/tracefold/blob/0cd0acee7a227fd7792d14c923345f44b097d178/tracefold/news/learning/objective.py#L1-L20)），唯一 Metric 不评分 taxonomy（[`metric.py`](https://github.com/AnalyThothAI/tracefold/blob/0cd0acee7a227fd7792d14c923345f44b097d178/tracefold/news/learning/metric.py#L53-L72)）。实测 `learning run` 因 `no_verified_prompt_target_clusters` 在 provider call 前 `REJECTED`，usage 全为 0；这是正确的 fail-closed 行为。

### 0.2 Issue 字面是 8 个 target；若引入 derived-owner 排除则是 6 个

8 条 residual 都有 recorded Stable 输出并可重放，accepted Review 的显式 `first_bad_owner` 全为空。因此按 #453 “仅显式 upstream owner 排除、taxonomy mismatch 结构化归 `event_semantics`”的字面规则，**8 条全部是 target**。

其中 3 条同时命中现行 Stable 输出的 hard-gate diagnostic：2 条 `factual_contradiction`，1 条 `must_hold_send`。这不等于它们都是“不可修 target”：当前 Objective 明确把带 evidence/correction 的 `factual_fidelity` 作为 judge 可验证的 ReaderCard repair；`must_hold_send` 也应由现有 action regression gate 约束，而不是自动抹掉 taxonomy failure。`stable_hard_gate` 在当前 Objective 中首先是防止错误 Stable case 混入 **control** 的规则。

真正需要 Issue 决定的是：是否把没有人工确认的 **derived upstream owner** 也提升为 taxonomy target 的排除依据。8 条的 derived owner 分布为 Gate 2、taxonomy 2、triage-prompt 4；若只排除 2 条 derived Gate case，才得到保守的 6-target 口径：

| 口径 | target | control | train | development-selection | 合同状态 |
| --- | ---: | ---: | ---: | ---: | --- |
| #453 字面：只排显式 upstream owner | 8 | 37 | 6 target + 26 control = 32 | 2 target + 11 control = 13 | 当前 Issue 可直接推出。 |
| 修订：derived Gate owner 也排除 | 6 | 37 | 4 + 26 = 30 | 2 + 11 = 13 | 必须新增 machine-readable precedence 与 focused test。 |

两种分层 split 的 exact cluster overlap 都为 0，并能满足当前 safety/positive-action/negative-action/novelty 两半覆盖；这只证明 split 机械可构造，不证明新增的 derived-owner authority 合理。Issue 应明确“显式 owner、derived owner、taxonomy mismatch”的优先级，才能冻结唯一 split root；`stable_hard_gate` 本身不定义第三种 target 排除口径。

同理，69 个 taxonomy exact case 不是 69 个 honest controls。按现行整体准入规则只有 37 个：其余 32 个分别因 derived triage-prompt、Gate、retrieval、unknown 或 ReaderCard URL lint 等原因被排除。taxonomy exact 不能把其它不可修失败洗成 control。

### 0.3 这份 Gold 仍是 pilot 证据，不是生产推送 Gold

四个 taxonomy 字段本身不是最终 push/hold policy 的直接输入；但 `event_semantics` 同一个 Predictor 还产出 direction、magnitude、novelty 和 relevance，改写其 instruction 可能间接改变推送行为。因此正确结论不是“taxonomy 优化绝不会影响推送”，而是“taxonomy 指标本身不能证明推送改善”；现有 action/relevance/novelty regression gates 和 future business evidence 都必须保留。

- 8 个 raw taxonomy target 全是 `should_hold`/`must_hold`，没有正向 push target；37 个 controls 也只有 3 个 positive-action case。优化器可能学会更保守，却没有足够证据证明 must-push/should-push recall 不下降。
- 当前 cluster id 虽然 77/77 唯一，轻量字符二元组审计仍发现一对 target 在不同 cluster、拟跨 train/selection 且相似度约 0.5。它不是重复的定论，却足以要求在 freeze 前补一轮人工/语义 near-duplicate 审计；exact cluster-disjoint 不是语义无泄漏证明。
- #437 的 77 条 acceptance provenance 是 owner-authorized AI adjudication，不是独立双人盲审/裁决；proposal、Stable task LM 与 reflection/evaluator LM 又存在模型家族复用风险。它可以是 audited Gold authority，但其 label 独立性不足以支撑强泛化结论（[#437](https://github.com/AnalyThothAI/tracefold/issues/437)）。
- 当前 release profile 的 development floor 包含 boundary ≥30、retention ≥100、negative ≥50、strata ≥3 和 safety；future validation 还要求 Candidate 注册后 ≥24 h、eligible events ≥200、至少 30 个 primary clusters（[`profile.py`](https://github.com/AnalyThothAI/tracefold/blob/0cd0acee7a227fd7792d14c923345f44b097d178/tracefold/news/learning/profile.py#L35-L57)）。现有 retention 只有 11，所以即使 GEPA `ADVANCE`，offline/holdout release 仍会 fail closed。

因此，这份 Dataset 的准确身份是：**可复用的唯一语料源 + taxonomy plumbing pilot corpus**。它不是一份无需扩充即可完成 production release 的“完整优化数据集”。仓库既有研究给出的非框架建议仍更可信：先达到正式 development profile，再以 150–250 个独立 clusters、自然 verified Prompt targets ≥30、controls ≥100 作为 production-v1 discovery 目标，并始终另开 future holdout。

## 1. 先分清三类约束

### 1.1 框架硬要求

在 DSPy 3.3.1 中，以下属于当前实现实际检查或调用所要求的条件：

| 要求 | 证据 | 对 #453 的含义 |
|---|---|---|
| metric 必须能绑定五个位置参数 `(gold, pred, trace, pred_name, pred_trace)` | 构造器直接用 `inspect.signature(...).bind(...)` 检查，不满足就抛 `TypeError`（[`gepa.py` L416-L422](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L416-L422)） | Issue 应把“五参数可调用”写入 contract test；这比返回类型是否为 `Prediction` 更接近真正硬要求。 |
| `trainset` 必须非空 | `compile()` 明确断言（[`gepa.py` L522-L545](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L522-L545)） | readiness 的零调用拒绝是正确的业务前置。 |
| `auto`、`max_full_evals`、`max_metric_calls` 三选一 | 构造器断言只能设置一个（[`gepa.py` L426-L435](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L426-L435)） | `learning run` 必须收敛为一个明确的预算形状。 |
| 默认 proposer 路径需要 `reflection_lm`；只有自定义 proposer 时可不传 | 构造器检查（[`gepa.py` L441-L445](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L441-L445)） | 删除自研 proposer 后，reflection LM identity 与预算成为必填运行证据。 |
| `track_best_outputs=True` 时必须同时 `track_stats=True` | 构造器断言（[`gepa.py` L469-L471](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L469-L471)） | #453 并不需要打开 `track_best_outputs`；若打开，需计入更大的 artifact。 |

框架**没有**要求 target/control 各至少 2 条、70/30、cluster-disjoint、时间排序、future holdout、任何 taxonomy regression 一票否决，亦没有要求整个产品只能有一份 Dataset 或 Metric。这些都是 Tracefold 为数据真实性、泄漏防护、发布安全与 KISS 设定的业务/架构规则。

### 1.2 官方推荐实践

- GEPA 应使用富 feedback metric。官方文档明确说裸 float 仍可运行，但 proposer 只能看到 “This trajectory got a score of …” 的弱反馈（[DSPy 3.3.1 GEPA deep dive L11-L17](https://github.com/stanfordnlp/dspy/blob/3.3.1/docs/docs/diving-deeper/gepa-in-depth.md#L11-L17)）。
- train 用于反思更新，val 用于 Pareto 跟踪和赢家选择；不传 val 会复用 train，DSPy 明确警告这会过拟合，若追求 unseen generalization 应提供独立、尽可能小但足够代表 downstream distribution 的 val（[`gepa.py` L531-L580](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L531-L580)）。
- DSPy 的通用数据建议是“30 条常能获得价值，目标至少 300 条”；对 GEPA，最大化 train，同时保留刚好足以代表 downstream/test distribution 的 val（[Optimization overview L6-L10](https://github.com/stanfordnlp/dspy/blob/3.3.1/docs/docs/learn/optimization/overview.md#L6-L10)）。这是经验建议，不是门槛。
- 默认 `instruction_proposer=None` 被 DSPy 3.3.1 明文标为 “recommended for most users”；自定义 proposer 是多模态、强结构约束、领域知识注入、耦合更新等高级场景（[`gepa.py` L260-L288](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L260-L288)）。

### 1.3 Tracefold 自己的业务规则

以下方向与框架不冲突，而且大多合理，但不得再表述成“DSPy/GEPA 官方要求”：

- accepted Review 是 Gold authority；PostgreSQL frozen Dataset 是业务真相。
- one connected-fact cluster one representative、target 优先、不得复制错误样本。
- taxonomy mismatch 的 owner 是 `event_semantics`，显式 upstream owner 优先排除。
- code-owned `source_authority` 不进入模型 target。
- candidate-only taxonomy regression、schema/provider regression 一票否决。
- future holdout、shadow、canary、manual promotion 是发布 authority。
- 一个正式 CLI、一个 Objective Plan、一个候选类型、一个 release path 是 Tracefold 的 KISS/防漂移决策。

## 2. Metric：Issue 的方向正确，但“必须”与“hard gate”表述需修正

### 2.1 返回 `Prediction(score, feedback)`：应当做，但不是运行硬门槛

DSPy 3.3.1 的类型协议允许 `float | ScoreWithFeedback`，并说明没有 feedback 时会合成通用文本（[`GEPAFeedbackMetric` L27-L60](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L27-L60)）。实际 adapter 也明确把裸数字包装成 `{score, feedback="This trajectory got a score of ..."}`（[`gepa.py` L584-L605](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L584-L605)）。

所以 #453 中“唯一 optimizer Metric **必须直接返回官方 GEPA 需要的** `dspy.Prediction(score, feedback)`”应改为：

> 为了让 taxonomy 的 expected/predicted/missing/extra 真正进入反思提示，Tracefold 的正式 GEPA metric 必须按本项目契约返回 `dspy.Prediction(score, feedback)`；DSPy 本身仍接受裸 float，但那只会退化为弱反馈模式。

这一区分很重要：前半句是 Tracefold 为保证优化有效设定的硬规则，后半句才是框架事实。

### 2.2 feedback 只指导下一次 rewrite，不能执行一票否决

原论文算法把两条信号分得很清楚：feedback/trace 用来生成新 prompt；新旧候选是否 improved 则由 minibatch score 决定，进入候选池后再在完整 selection set 上评分（[GEPA 原论文，第 5–6 页、Algorithm 1](https://arxiv.org/pdf/2507.19457)）。GEPA 0.1.4 的默认 acceptance 也是比较 minibatch score 总和，只有严格增加才接受（[`acceptance.py` L44-L53](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/strategies/acceptance.py#L44-L53)）。

因此以下规则不能“只通过 Metric feedback”实现：

- candidate-only taxonomy regression 必须为 0；
- instruction byte/token/growth bound；
- schema/provider candidate-only regression；
- closed write set。

feedback 可以告诉 reflection LM 为什么失败，但不会倒回去否决刚刚产生的候选。要做到 hard gate，至少要采用下列一种明确机制：

1. 把违规映射成足够明确的逐例 score 惩罚，同时在 GEPA 结束后再跑 Tracefold 的 set-level 一票否决 gate；或
2. 使用 GEPA 0.1.4 的官方 `AcceptanceCriterion` 扩展在 minibatch 阶段拒绝，但最终仍需对完整 development-selection 做 Tracefold gate；或
3. 仅把 GEPA 当 proposer/search，任何 winner 在构造 `PromptCandidateV1` 前都经过独立 deterministic candidate guard。

推荐第 3 种为主：它最符合 Issue 已声明的“DSPy 不拥有发布 authority”。“Metric feedback + final business gate”可以共用同一 taxonomy comparison outcome，避免再实现第二把尺。

### 2.3 predictor-specific feedback 可以不同，predictor-specific score 当前不能不同

DSPy 会对被优化 predictor 再调用 metric，让 `pred_name`/`pred_trace` 产生局部 feedback；但 DSPy 3.3.1 当前不支持 predictor-level score 与 module-level score 不同，发现不同时会警告并强制改回 module score（[`gepa_utils.py` L414-L430](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa_utils.py#L414-L430)）。

因此 integrated metric 应遵守：

- module-level 与 `pred_name="event_semantics"` 返回相同的总 score；
- feedback 可按 predictor 定向，只给 `event_semantics` taxonomy 的 expected/predicted/missing/extra；
- 不要假设 taxonomy predictor 的局部分数会单独驱动 candidate selection。

DSPy 3.3.1 还支持在同一 `Prediction` 中增加 `objective_scores`，GEPA 可跟踪 objective frontier（[`gepa_utils.py` L57-L66](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa_utils.py#L57-L66)，[官方测试](https://github.com/stanfordnlp/dspy/blob/3.3.1/tests/teleprompt/test_gepa.py#L656-L696)）。这可以作为诊断选项，但不是 #453 必须引入的复杂度；一个清晰 aggregate score 加 Tracefold hard gates 已足够。

### 2.4 Issue 尚未定义可实现的逐例 taxonomy scalar

现有 recorded taxonomy report 的 event-family/assertion macro-F1、subject micro-F1 和 confusion matrix 都是 corpus aggregate；GEPA metric 却必须逐例给分。把 aggregate report “并入唯一 Metric”并没有唯一决定以下问题：

- `subject_codes` 用 exact、set-F1、Jaccard 还是 precision/recall；
- 四个 taxonomy 轴等权还是按业务风险加权；
- taxonomy 是现有 `semantics_novelty` 的一部分，还是一个新 component；
- target 修复与 control 保持如何在总分中权衡；
- validation 只有 2 个 target 时，11 个 control 是否会把修复信号稀释成同分。

最低限度应 pin 一条 per-example 公式，例如 `mean(subject_set_f1, event_family_exact, change_state_exact, assertion_status_exact)`，再 pin 它在总 Metric 的权重、`perfect_score`、缺失 Gold 的分母规则和 candidate-only regression 规则。此公式只是一个待业务确认的合理起点，不是 DSPy 官方答案。没有这条，两个实现可以选择不同 winner，却都声称符合 #453。

### 2.5 Issue 的 optional-judge 条件会改变现有尺子

#453 建议“只有 Objective 中出现 ReaderCard free-text target 才创建 judge”。当前 Metric 并非如此：只要 passed free-text retention anchor 的 Candidate 文本与 Stable 不同，就需要 judge 判断语义等价（[`metric.py`](https://github.com/AnalyThothAI/tracefold/blob/0cd0acee7a227fd7792d14c923345f44b097d178/tracefold/news/learning/metric.py#L255-L279)）。而 `event_semantics` 输出还是 `reader_card` 的输入；taxonomy-only rewrite 也可能间接改变 free text。

因此本次最小安全做法是继续为全部 optimizer cases 保留 judge。若以后要条件化，`needs_judge` 必须由 target **与 control** 中所有 free-text/factual retention requirements 推导，并证明不提供 judge 时不会从语义等价退化为 byte equality。仅检查 `target_predictors` 不足以保持同一 Metric identity。

## 3. train / validation / minibatch / full evaluation 的真实语义

### 3.1 Issue 的 train/development-selection 分工基本正确

上游实现把 train 交给 reflective proposer，把 val 交给 Pareto 与最终 aggregate winner（[GEPA `optimize()` L139-L162](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/api.py#L139-L162)）。原论文也把 `D_feedback` 用于 minibatch reflection、把 `D_pareto` 用于候选选择，最后返回 `D_pareto` aggregate 最好的候选（[原论文，第 5–6 页](https://arxiv.org/pdf/2507.19457)）。

故 #453 的如下判断是对的：

- development-selection 不是 future holdout；
- cluster 不得跨 train/validation；
- val 不应把 Gold/feedback 内容送给 reflection LM；
- Candidate 注册后的未来窗口才是最终 generalization 证据。

不过，“targets 与 controls 分别按时间排序做 70/30”是 Tracefold 的分层业务策略，不是 GEPA 原理。它还缺一个可复现细节：`0.7 * n` 如何取整。建议冻结为类似 `train_n = min(n - 1, max(1, floor(0.7*n)))` 的明确公式，否则 target 只有 2、3、8 条时，不同实现会产生不同 split root。

### 3.2 GEPA 不是每次都在 train+val 全量上比较

默认反思 minibatch 大小是 3。每次迭代先在 train minibatch 上执行 parent、收集 trace/feedback；若全批达到 `perfect_score` 且 `skip_perfect_score=True`，就直接跳过反思（[`reflective_mutation.py` L372-L417](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/proposer/reflective_mutation/reflective_mutation.py#L372-L417)）。产生 child 后，再在同一个 minibatch 上执行 child；默认 acceptance 比较新旧 score 总和。只有通过 acceptance 的 child 才会在完整 val 上评估并进入候选池（[`reflective_mutation.py` L514-L639](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/proposer/reflective_mutation/reflective_mutation.py#L514-L639)，[`engine.py` L513-L545](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/core/engine.py#L513-L545)）。

这解释了 control 的两种不同作用：

- train controls 可在 mixed minibatch 中约束 rewrite 不要破坏已有行为；
- val controls 用来衡量所有进入候选池的程序是否回归，并参与 aggregate winner；
- Tracefold 的“一条回归即 REJECTED”仍是 GEPA 之外的 set-level gate，不能寄希望于平均分自动实现。

## 4. candidate 0、seed baseline 与 `detailed_results`

### 4.1 candidate 0 确实是同次 run 的 Stable seed

DSPy 从 student 当前每个 predictor 的 `signature.instructions` 构造 `seed_candidate`，再把它交给 upstream `optimize()`（[`gepa.py` L626-L648](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L626-L648)）。GEPA state 初始化时把它放在 `program_candidates[0]`，父节点为 `[None]`，每个 val instance 的最初 Pareto winner 也是 `{0}`（[`state.py` L191-L242](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/core/state.py#L191-L242)）。engine 随后将它作为 iteration 0 / candidate 0 的 baseline 记录（[`engine.py` L625-L708](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/core/engine.py#L625-L708)）。

所以在**全新 run、默认 full val policy** 下：

```python
seed_score = detailed_results.val_aggregate_scores[0]
seed_subscores = detailed_results.val_subscores[0]
winner_idx = detailed_results.best_idx
winner_score = detailed_results.val_aggregate_scores[winner_idx]
winner_subscores = detailed_results.val_subscores[winner_idx]
```

是正确而且比先独立 `compile_live baseline` 再跑 GEPA 更干净的做法。`best_idx` 是 validation aggregate 最高者；分数并列时 Python `max` 保留第一个 index，因此 seed 与候选同分时 seed 会赢，天然支持 `NO_OP`（[`DspyGEPAResult` L112-L119](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L112-L119)）。

### 4.2 这些字段的 3.3.1 精确形状

DSPy 3.3.1 的 `DspyGEPAResult` 不是旧版形状：

- `candidates: list[Module]`，不是 instruction dict；
- `parents: list[list[int | None]]`；
- `val_aggregate_scores: list[float]`；
- `val_subscores: list[dict[val_id, float]]`，不是二维 list；
- `per_val_instance_best_candidates: dict[val_id, set[int]]`；
- `discovery_eval_counts` 是每个 candidate 被发现前消耗的 rollout 数；
- `total_metric_calls`、`num_full_val_evals`、`log_dir`、`seed` 是运行元数据；
- `.to_dict()` 会把候选 Module 提取为 component text，并附 `best_idx`。

这些都由版本化源码直接定义（[`gepa.py` L64-L183](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L64-L183)），3.3.1 release note 也明确提醒 upstream GEPA result shape 已变化（[DSPy 3.3.1 release](https://github.com/stanfordnlp/dspy/releases/tag/3.3.1)）。#453 对 `val_subscores[best_idx]` 和 `best_idx` 的使用符合该版本。

### 4.3 `detailed_results` 不是“所有提案”的完整审计轨迹

`candidates` 实际只包含通过 minibatch acceptance、被 full-val evaluate 并加入 pool 的候选；被拒绝的 rewrite 不会成为 candidate index（[`engine.py` L450-L545](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/core/engine.py#L450-L545)）。`DspyGEPAResult` 也没有 feedback、reflection prompt、raw reflection response 字段。

所以 Issue 的“用官方 `detailed_results.to_dict()` / `log_dir` 作为 trajectory/candidate evidence”只能支持：

- seed/accepted candidate 文本；
- lineage；
- validation aggregate 与逐例分数；
- best index；
- discovery/total rollout 数。

它不能单独证明：

- reflection LM 实际看到了哪段 taxonomy feedback；
- 被拒 proposer 生成了什么；
- 为什么某个提案被 guard 或 acceptance 拒绝；
- 每个 reflection 调用的完整 prompt/raw output。

若运行审计必须证明这些事实，应使用 GEPA 官方 callback 事件（例如 reflective-dataset/proposal/rejection events）做一份最薄的 append-only capture，或使用官方 MLflow/W&B tracker。若不愿增加运行 artifact，则至少需要一个 dummy-LM contract test，证明具体 taxonomy feedback 确实进入 reflection prompt；不要把 `detailed_results` 说成它没有提供的完整 receipt。

### 4.4 `val_subscores` 也不足以生成 seed/winner taxonomy regression receipt

`val_subscores` 每例只有最终 scalar，不含四轴 prediction。即便打开 `track_best_outputs`，upstream state 保存的也是每个 example 当前“严格更好”的 best output；它不保证同时保留 candidate 0 和最终 winner 的两套输出（[GEPA state](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/core/state.py)）。因此仅从 `detailed_results` 不能重建：

- seed 与 winner 的四轴 confusion/F1；
- 哪个 axis 被修复、哪个 axis 新回归；
- candidate-only taxonomy regression 的结构化证据。

最小可靠方案是在 `compile()` 后，对 frozen development-selection 做一次**有界 paired final evaluation**：显式执行 candidate 0 与 winner，使用同一 comparison primitive 捕获两边 outputs、逐轴比较和 physical usage，再执行一票否决。它不是 run 前那次重复的 standalone baseline，而是补齐 official result 未保存的结构化 release evidence。若不愿增加这次调用，则必须通过受支持的 callback/observer 在首次 seed 与 winner evaluation 时捕获等价证据，不能把 float subscore 误称为完整 regression receipt。

还要区分两种 “Stable”：Dataset 中的 recorded production Stable 用于发现 target/control 和历史诊断；candidate 0 是本次 fresh run 对 selection 的 live seed。Candidate-only regression 应比较同次 paired evaluation 的 seed 与 winner，不能把受模型 nondeterminism 影响的 recorded Stable 和 live seed 混成同一个 before。

## 5. 默认 proposer：推荐路径属实，但它不会自动去重、压缩或守住全部业务边界

### 5.1 删除现有自研 proposer 的理由成立到什么程度

DSPy 3.3.1 明确把 `instruction_proposer=None` 标为多数用户推荐路径（[`gepa.py` L260-L288](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L260-L288)）。在 DSPy adapter 中，`None` 会逐 component 调用 GEPA 0.1.4 的官方 `InstructionProposalSignature`；只有显式传入 custom proposer 才覆盖这一路径（[`gepa_utils.py` L128-L180](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa_utils.py#L128-L180)）。

因此，如果 Tracefold 当前 `InstructionProposer` 只是重新包装一次同类 instruction rewrite、再维护自己的 calls/components/rejections，那么删除它符合 KISS，也减少了两套 proposal semantics。

### 5.2 Issue 对默认 proposer 的效果有过度推断

官方默认 meta-prompt 要求 reflection LM 从 examples/feedback 中提取详细任务规则和领域信息，并输出一份完整新 instruction（[`InstructionProposalSignature` L12-L29](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/strategies/instruction_proposal.py#L12-L29)）。它没有“尽量短”“删除重复规则”或 token bound 的内建目标。

GEPA 官方 FAQ 反而明确记录了两类已知现象：早期 prompt 往往很长、会包含训练样本内容；reflection LM 也可能漂移评分尺度或输出 schema。官方建议通过 feedback 加 anti-overfit/task-preservation constraint；在生产稳定性要求很高时，也可使用自定义 proposer 做增量更新（[GEPA FAQ L294-L341](https://github.com/gepa-ai/gepa/blob/v0.1.4/docs/docs/guides/faq.md#L294-L341)）。

因此应把 Issue 中“当前 proposer 阻碍 prompt 去重和压缩，换默认 proposer即可”改成可测试假设：

> 默认 proposer 是最小官方基线；是否去重、压缩、避免 exemplar leakage 与 schema drift，必须由 instruction-size score/feedback、candidate guard 和 future holdout 验证。若官方默认在真实数据上持续违反硬边界，自定义 proposer 仍是官方支持的合理扩展，而非原则错误。

## 6. 自定义 `ReflectionComponentSelector`：协议是官方的，但仍属 experimental/version-pinned surface

GEPA 0.1.4 确实定义了公开 protocol：selector 接收 `(state, trajectories, subsample_scores, candidate_idx, candidate)`，返回待更新 component name 列表（[`base.py` L11-L24](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/proposer/reflective_mutation/base.py#L11-L24)）。DSPy 3.3.1 的 `component_selector` 文档也公开支持 `round_robin`、`all` 或 custom selector（[`gepa.py` L289-L296](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L289-L296)），并有 custom-function 测试（[DSPy test L352-L586](https://github.com/stanfordnlp/dspy/blob/3.3.1/tests/teleprompt/test_gepa.py#L352-L586)）。

所以 #453 的薄 selector 设计在固定版本上是合法的：

- taxonomy-only：只返回 `event_semantics`；
- two-target：只在计划允许的 predictor 集合中轮换；
- 返回值必须是 candidate 已有 key；
- selector 不负责 rewrite、merge、score 或 candidate population。

但不能称它为长期稳定 ABI。`dspy.GEPA` 本身带 `@experimental` 标记（[`gepa.py` L186-L190](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L186-L190)），protocol 又直接暴露 `GEPAState`。可靠使用方式应是：

1. 同时 pin DSPy `3.3.1` 与 resolved GEPA `0.1.4`；
2. selector 尽量只读取 `candidate.keys()` 与 frozen plan，若需轮换用可恢复的 `state.i`，不要写 upstream private fields；
3. 增加 protocol signature、只改 allowed component、resume determinism 的 focused contract tests；
4. 每次升级 DSPy/GEPA 重新核对 release note 与 tests。

只要所有祖先 candidate 的非目标 component 都与 seed 相同，即便 `use_merge=True`，merge 也无法凭空修改该 component；但仍应对最终 state 做 byte-identical write-set check，而不是只统计 reflection call。

## 7. 官方 artifact：哪些足够，哪些不足

### 7.1 `program_state.json`

`optimized.save(path, save_program=False)` 是正式公开 API；`.json` 保存的是 instructions/demos/signature 等 state，不保存 Python 程序结构。DSPy 官方把 state-only JSON 作为通常首选，因为小、可读、可 diff、加载时不执行 pickle（[Saving and loading L11-L25](https://github.com/stanfordnlp/dspy/blob/3.3.1/docs/docs/diving-deeper/saving-and-loading.md#L11-L25)，[`BaseModule.save` L171-L252](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/primitives/base_module.py#L171-L252)）。#453 的 `program_state.json` 选择正确，`save_program=False` 只是显式写出默认值。

但 `detailed_results` 是 optimized Module 的普通附加属性，不在 named parameter state 中。保存 `program_state.json` 不会保存 GEPA trajectory/result。

### 7.2 `track_stats=True` 与 `detailed_results.to_dict()`

`track_stats=True` 才会把官方 `DspyGEPAResult` 附到返回程序上（[`gepa.py` L664-L669](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L664-L669)）。`.to_dict()` 是公开序列化面，包含 accepted candidates、parents、scores、subscores、budget metadata 和 best index（[`gepa.py` L128-L164](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L128-L164)）。

Issue 的 artifact 清单应明确增加：

```text
gepa_detailed_results.json
  = canonical JSON of optimized.detailed_results.to_dict()
```

只在内存中“使用 `.to_dict()`”不等于留下 artifact；`learning_run.json` 里只摘录 seed/winner/best_idx 也不足以替代官方逐候选/逐例结果。

### 7.3 `log_dir`

DSPy 把 `log_dir` 传成 upstream `run_dir`（[`gepa.py` L632-L662](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L632-L662)）。GEPA 0.1.4 会写：

- `gepa_state.bin`：resume 用 pickle state；
- `run_log.json`：iteration trace；
- `candidates.json`：进入 pool 的候选；
- `run_log.txt`、candidate tree、可选 best outputs。

state、run log 和 candidates 的保存逻辑见 [`state.py` L299-L353](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/core/state.py#L299-L353)。它们适合 debug/resume 和审计 accepted search path，但 `gepa_state.bin` 是 pickle，不应成为唯一可读证据，也不要把它当部署 artifact。

还必须防止 run identity 混淆：如果 `gepa_state.bin` 已存在，GEPA 会恢复旧 state（[`state.py` L660-L706](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/core/state.py#L660-L706)）。当前 engine 在判断/载入 resume state 之前，已经执行 seed candidate 的 validation（[`engine.py` L625-L641](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/core/engine.py#L625-L641)）。因此：

- fresh authoritative run 必须使用不存在或明确为空的 `<RUN_DIR>/gepa`；
- resume 必须核对 dataset/split/program/metric/task/reflection identities；
- resume 的 audited physical usage 会包含一次新的 seed val evaluation，不能声称绝对“无 seed rerun”。

## 8. 77 条 / 8 residual：能否跑通，预算意味着什么

### 8.1 机械可运行性

按实测值，77 个 case 也是 77 个独立 cluster，69 个 taxonomy exact **raw cases**、8 个 pre-exclusion residual；其中真正满足整体 Objective 准入的 control 只有 37 个。77 落在官方“30 条常能获得价值、目标至少 300 条”的经验区间内，也接近原论文最小实验的量级；但原论文仍使用独立 train/val/test，最小 AIME 优化 split 是 45 train + 45 val，其他任务多为 111–150 train 与 111–300 val（[原论文 Appendix E，第 23–25 页](https://arxiv.org/pdf/2507.19457)）。这些论文数字不是最低门槛，却显示 6–8 个 error case 远低于论文用来证明泛化的证据规模。

只要 #453 接线后形成非空 train 与 val，DSPy 可以运行。若 target/coverage 不足而返回零调用 `INSUFFICIENT_DATA`，也是完整链路的正确终态，不是失败。当前实测终态正是零调用拒绝，不能写成“已具备运行 readiness”。

### 8.2 70/30 后真正进入反思和选择的 target 很少

按 Issue 倾向的 Python `round(n * 0.7)`、target/control 分别时间排序再合并，实测可构造的三种口径为：

- 8 target + 37 control：train `6 + 26 = 32`，selection `2 + 11 = 13`；
- 若 Issue 修订为排除 2 条 derived Gate-owner case，6 + 37：train `4 + 26 = 30`，selection `2 + 11 = 13`。

因此 development-selection 上只有 2 个 taxonomy residual。它可以回答“候选是否修了这两个已知 case”，不能稳定估计新的 taxonomy residual 是否会改善，也不足以证明四个 taxonomy 轴在未见分布上都改善。先冻结 ownership 与取整规则，再谈唯一 split root。

### 8.3 默认 minibatch 会浪费大量预算在全 control 批次

GEPA 0.1.4 默认用 epoch-shuffled、大小 3 的 minibatch（[`api.py` L336-L355](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/api.py#L336-L355)，[`batch_sampler.py` L26-L103](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/strategies/batch_sampler.py#L26-L103)）。如果 taxonomy 是唯一错误信号，并且 control 都得到 `perfect_score=1`：

- 8-target raw 口径的 train 为 6 target / 26 control，一个随机 3-case batch 全是 control 的概率约为 `C(26,3)/C(32,3)=52.4%`；
- 6-target derived-owner 排除口径的 train 为 4 target / 26 control，对应约 `C(26,3)/C(30,3)=64.0%`。

这些批次仍先执行 parent 的 3 个 rollouts，然后因全满分跳过 reflection/child。也就是说，默认 sampler 能跑，但不是这组高度不平衡数据最节省预算的形状。

实现前必须选择并冻结一种策略：

- 增长真实 target corpus（首选，既提高 diversity 也降低稀疏度）；
- 在保留 one-case-one-weight 的前提下使用官方 `BatchSampler` protocol 做 target/control 分层 minibatch，不复制 case；或
- 增大 reflection minibatch，并接受更长 reflection prompt；或
- 明确接受当前数据只做 pilot，预算覆盖至少一个完整 shuffled epoch，并把大量 skipped-perfect batches 视为预期结果。

不能通过复制 8 个 residual 来提高抽样概率；这会改变优化分布，也违反 Issue 的 cluster truth。

### 8.4 `max_metric_calls` 不等于物理 provider call budget

一次 fresh run 至少先花 `|val|` 个 rollout 评估 candidate 0。每个正常 reflection attempt 再花 `b` 个 parent rollout；产生 child 后再花 `b` 个 child rollout；child 被接受时再花 `|val|` 个 full-val rollout。按 `b=3`、`|val|=12~13`，一个被接受的有效 mutation粗略增加 `6 + 12~13 = 18~19` 个 rollout，此外还有全-control skipped batch、rejected child 与 merge。

DSPy 的 `max_full_evals` 只是换算为 `max_metric_calls = max_full_evals * (len(trainset)+len(valset))`（[`gepa.py` L552-L564](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L552-L564)）。upstream stopper 在迭代边界检查已用量（[`stop_condition.py` L163-L173](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/utils/stop_condition.py#L163-L173)），所以一个原子 minibatch/full-val 操作可以让最终值越过名义阈值。

更重要的是，一条 program rollout 可能包含多个 task LM 调用；reflection LM 调用和可选 judge 调用也不是同一个计数单位。DSPy 3.3.1 还明确不支持 `max_reflection_cost` passthrough，传入会报错（[`gepa.py` L480-L487](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py#L480-L487)）。因此 #453 保留 `AuditedConfiguredLM` 的 pre-call physical-call/cost/wall-clock ledger 是正确且必要的，不是重复 GEPA budget。

## 9. “完整优化数据集 + GEPA 全链路”应满足的最小验收

若目标是先诚实跑通 pilot，而不是提前宣称生产提升，建议把完整性定义为以下可观察链路：

1. **Frozen source**：一个 Dataset SHA、一个 episode projection root；每个 optimizer case 有稳定 cluster id。
2. **Replayable input**：每个 `dspy.Example` 的 `.with_inputs(...)` 只暴露生产推理时可得信息；不得把 accepted Gold 或 reviewer rationale 泄漏进 inputs。
3. **Gold output**：exact taxonomy 四轴完整；code-owned `source_authority` 不作为模型 Gold。
4. **Ownership election**：先排除 receiver/deduper/evidence/gate/retrieval owner，剩余 mismatch 才是 prompt target；每条排除有 machine-readable reason。
5. **Deterministic split**：target/control 分层、cluster-disjoint、冻结取整算法和 split roots；增加 near-duplicate 跨 split 审计；val 内容不进入 reflection。
6. **Five-argument metric**：pin 逐例 taxonomy scalar 与总分权重；同一 comparison primitive 同时产出 score、具体 feedback 和 aggregate diagnostics；module/predictor score 相同，feedback 可定向。
7. **Fresh one-run baseline/search**：唯一 fresh `dspy.GEPA.compile()`；candidate 0 与 winner 来自同一 valset；`best_idx=0` 是合法 `NO_OP`。
8. **Closed write set**：官方 component-selector protocol 只选择 Objective Plan 允许的 predictor；最终 program state 再做 byte-level diff。
9. **Hard release gates**：任何 candidate-only regression、schema/provider 回归、instruction bound 违规使 `REJECTED`；不能只靠 feedback 或平均分。
10. **Artifacts**：`program_state.json`、`gepa_detailed_results.json`、fresh `log_dir`、薄 `learning_run.json`、物理调用 ledger；另有 seed/winner structured outputs 或等价 callback capture；若要求反思可审计，再加 proposal/feedback callback 或对应 contract test。
11. **Development release coverage**：至少满足 frozen profile 的 boundary/retention/negative/strata/safety；当前 retention 11/100 不合格。此项可以晚于 plumbing smoke，但必须早于可发布 Candidate 声明。
12. **Future evidence**：PromptCandidate 产生后，用新的时间窗口做 holdout；不在同一 77 条上循环证明 generalization；若目标是推送，预注册 push precision/recall、must-push miss、false push、duplicate leak、reader load/latency 等业务指标。

满足 1–10 只能称“GEPA plumbing pilot 跑通”；满足 11 才具备进入正式 offline/release evaluation 的 development 证据；只有再满足 12 并通过新闻发布业务指标，才可称“优化新闻推送”。

## 10. Issue 中仍存在的重复与非重复

### 应删除的真实重复

- 在同一正式实验前先独立 provider-backed Stable baseline，再让 candidate 0 对同一 valset 重跑：fresh GEPA candidate 0 已经提供可比 baseline。
- taxonomy comparison 在 recorded metric、GEPA surrogate、release gate 各实现一次：应共享一个纯 comparison outcome，再由不同 consumer 聚合/判定。
- 自研 proposer 若只是复刻官方完整 rewrite + re-ask + receipt：在没有证据证明必要时可删除。
- exact taxonomy Gold 与人工重复填写的 taxonomy pass/fail fields 同时充当 active truth：这是 Tracefold domain duplication。
- standalone public `optimize` 与 `learning run` 同时生成 Candidate：这是第二个 candidate-authoritative 入口；内部 optimize 函数仍可由唯一 run 调用。
- custom checkpoint 完整复制 patch instructions、trajectory 大量镜像 upstream candidate/parent/score：应收敛为 official artifact hash/path + bounded terminal summary。

### 看似重复、实则职责不同，不能删

- GEPA `max_metric_calls` 与 Tracefold physical-call/cost/wall-clock pre-call budget：计数单位和安全语义不同。
- GEPA aggregate winner 与 Tracefold candidate-only regression/release gate：前者优化，后者授权发布。
- development-selection 与 future holdout：前者参与搜索选择，后者验证未见数据。
- `program_state.json` 与 `gepa_detailed_results.json`：前者可部署状态，后者搜索证据。
- official `log_dir` 与薄 `learning_run.json`：前者是 optimizer state/debug；后者把它绑定到 Dataset、Program、Metric、预算和 PromptCandidate identity。
- Dataset 中 recorded Stable taxonomy report 与 GEPA candidate 0：前者测历史生产行为，后者测本次 live seed selection；若删除独立 CLI，必须先把前者的 diagnostics 迁入 run。
- 零调用 `readiness` 与 provider-backed optimize：前者在花钱前给出 dispositions、coverage 和预算拒绝，不是重复执行。KISS 上更适合保留；若删除公开命令，唯一 run 必须在首个 provider call 前产出完全相同的 preflight artifact。
- framework-neutral `MetricOutcome` 与 `_DspyAcceptedReviewMetric`：后者只是把 domain outcome 适配成 `dspy.Prediction`，不是第二把业务尺。

### Issue 自己新增的必要薄层

`ReflectionComponentSelector`、Objective Plan、candidate guard、PromptCandidate、physical budget ledger、future release path 都不是 GEPA 的重复实现。它们分别拥有 write-set、业务 ownership、安全边界、真实 provider 成本和发布权限；只要保持薄且不接管 proposal/search 即可。官方 GEPA pickle/log 也不能替代 PostgreSQL material truth 或跨 optimizer→release 信任边界的 Candidate manifest。

## 11. 对 Issue #453 的建议修订

建议在进入实现前，将以下文字直接纳入 acceptance/PRD：

1. 把“官方 GEPA **需要** `Prediction(score, feedback)`”改成“Tracefold 为启用富反思而要求；裸 float 在 DSPy 中可运行但不接受为正式 metric”。
2. 冻结 per-example taxonomy scalar、四轴权重、它在总 Metric 中的权重、缺失 Gold 分母和 `perfect_score`；aggregate macro-F1/confusion 仅作 corpus diagnostics。
3. 按 #453 字面冻结 8-target disposition；若要把 derived Gate owner 升格为排除 authority，则明确修订为 6 targets 并写 focused tests。`stable_hard_gate` 继续约束 controls/regressions，不另行创造第三种 target 数。
4. 增加 metric 五参数 signature contract test，并验证 predictor-specific call 返回同一 module score、只改变定向 feedback。
5. 把“约束失败通过 Metric feedback 返回”改成“feedback 负责解释；score + deterministic final candidate gate 负责执行”。
6. 明确 candidate-only regression 是 GEPA 结束后的完整 development-selection gate；paired seed/winner structured outputs 是其输入，不要假设 float `val_subscores` 或默认 minibatch acceptance 能一票否决。
7. 删除“只有 ReaderCard target 才建 judge”的条件；本次先保留 judge，或从所有 target/control 的 free-text retention requirements 推导 `needs_judge`。
8. 冻结 70/30 的整数取整公式，并在 split 前加入 near-duplicate 审计，不只检查 exact cluster id。
9. 增加 target sparsity 报告：每轴 target 数、train/selection target 数、预计/实际 target-bearing batch 数、skipped-perfect batch 数。
10. 明确首批 77 条的身份是 **pilot corpus**：37 个 honest controls、按 Issue 字面 8 个 targets（若新增 derived Gate-owner 排除则为 6 个）、retention 11/100。允许链路零调用结束于 `NO_OP/INSUFFICIENT_DATA`，但不得产生可发布 Candidate。
11. 增加 `gepa_detailed_results.json`，由 `detailed_results.to_dict()` 规范化持久化；同时增加 seed/winner outputs receipt 或等价受支持的 callback capture。
12. 规定 authoritative fresh run 的 `<RUN_DIR>/gepa` 必须为空；resume 是另一种有单独 identity/usage receipt 的运行模式。
13. 将“official artifact 是完整 trajectory evidence”收窄为“accepted candidate/lineage/score evidence”；若需证明 feedback consumption 或 rejected proposal，保留最薄 callback capture 或 focused dummy-LM test。
14. 默认 proposer 的压缩、长度、schema preservation、example leakage 都写成可测 acceptance，不写成默认算法保证；保留 Tracefold candidate guard 和 physical budget ledger。
15. 删除 standalone provider baseline 与 public Candidate-generating `optimize` 可以成立；零调用 readiness 和 recorded Stable diagnostics 不是相同语义，必须保留或无损迁入唯一 run。
16. 把“优化新闻推送”拆成两个可验证结论：先证明 Prompt-owned taxonomy/action/relevance 在 development-selection 改善且零回归，再由满足 profile 的 future holdout、shadow/canary 证明最终 push/hold 业务指标改善。

## 最终判断

Issue #453 **原则上通过，但现状为 NO-GO；按上述合同修订并补足数据后才是 CONDITIONAL GO**。

- “one Gold → one replayable corpus → score + feedback → one fresh GEPA compile → candidate 0 vs winner → business gate → existing release path”与 DSPy/GEPA 原理一致。
- 删除独立 provider baseline 和无证据必要的自研 proposer，方向正确。
- 官方 component selector 足以实现 closed target-predictor write set，但依赖 experimental/version-pinned protocol，必须有 focused contract test。
- 77 条是可复用的 pilot corpus；实际只有 37 个 honest controls，Issue 字面给出 8 个 target、保守 derived-owner 修订给出 6 个，retention 11/100 又阻断正式 release evidence。它不足以单独支持可靠泛化或“新闻推送已优化”声明。
- 当前最大的设计缺口不是有没有 taxonomy Gold，而是逐例优化目标未定义、target ownership 未闭合、judge 条件会改变尺子、seed/winner outputs 缺失，以及 target 稀疏下的 minibatch/budget效率。

换句话说，#453 可以把“GEPA 看不见 taxonomy”的架构断点修掉；补齐上述条件后可以建立完整的 **pilot** 链路。但现有 Dataset 和 Issue 原文不能直接建立完整的 **production optimization/release** 数据集，更不能把 taxonomy improvement 当作新闻推送 uplift。

## 一手来源索引

- [DSPy 3.3.1 release](https://github.com/stanfordnlp/dspy/releases/tag/3.3.1)
- [DSPy 3.3.1 `dspy.GEPA` implementation](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa.py)
- [DSPy 3.3.1 GEPA adapter](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/teleprompt/gepa/gepa_utils.py)
- [DSPy 3.3.1 GEPA deep dive](https://github.com/stanfordnlp/dspy/blob/3.3.1/docs/docs/diving-deeper/gepa-in-depth.md)
- [DSPy 3.3.1 optimization data guidance](https://github.com/stanfordnlp/dspy/blob/3.3.1/docs/docs/learn/optimization/overview.md)
- [DSPy 3.3.1 save/load implementation](https://github.com/stanfordnlp/dspy/blob/3.3.1/dspy/primitives/base_module.py)
- [GEPA 0.1.4 public API](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/api.py)
- [GEPA 0.1.4 engine](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/core/engine.py)
- [GEPA 0.1.4 state/artifacts](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/core/state.py)
- [GEPA 0.1.4 result](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/core/result.py)
- [GEPA 0.1.4 reflection component selector protocol](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/proposer/reflective_mutation/base.py)
- [GEPA 0.1.4 default instruction proposer](https://github.com/gepa-ai/gepa/blob/v0.1.4/src/gepa/strategies/instruction_proposal.py)
- [GEPA original paper, accepted at ICLR 2026](https://arxiv.org/abs/2507.19457)
