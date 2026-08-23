# Issue #160 的 DSPy 规范与代码影响复核

> 研究日期：2026-08-23（Asia/Taipei）  
> 代码基线：`main@7ab44ef00e539486954f6a73c6266dcd5d67dd4f`  
> Issue 基线：[AnalyThothAI/tracefold#160](https://github.com/AnalyThothAI/tracefold/issues/160)（访问于 2026-08-23）  
> DSPy 运行基线：仓库锁定 `dspy==3.3.0` / `gepa==0.1.1`；对照最新稳定版 DSPy 3.3.1  
> 证据边界：只使用 Tracefold GitHub Issue/PR/当前代码，以及 DSPy 官方文档、官方仓库源码和官方 release。

## 结论先行

**结论是“有条件可落地”，不是按 #160 当前文本直接开工。** 交易相关性作为
`EventSemantics.v2` 的嵌套 typed output、继续采用两个串行 `Predict`、以真实 policy action 为
metric、给 GEPA predictor-specific feedback，这些形状都符合 DSPy 3.3.0 的正式接口和典型用法。
没有 DSPy 依据要求增加第三个 Predictor、ReAct、RLM、向量检索或在线学习。

但是，当前 `main` 有三个优先级高于 #160 本身的 hermetic compile 断点：

1. `news learning compile` 在容器启动前就会失败：trusted seam 固定 reflection 输出预算为
   `32_000`，`CompilerProviderEndpointSecret.max_tokens` 却限制为 `<=16_384`。
2. 即使修掉上限，CLI 封装 `CompileInputBundle` 时误把 task endpoint 的 token/timeout 写进
   reflection 字段，launcher 会因 reflection secret 与 bundle 不一致而拒绝。
3. 即使前两处都修掉，runner 构造 `ProgramCompiler` 时仍未传入 #148 的
   `CardEquivalenceJudge`；最终执行 `bind_metric(None)`，GEPA 实际优化的仍是自由文本逐字相等尺子。

因此，#160 中“#145/#151 已把真实 GEPA + 语义等价 metric 接通”的前提在当前 hermetic CLI
路径上不成立。**先修完整 compiler chain，并做一次入口到 patch/receipt 的整链测试，再谈
Program v6 GEPA；否则新 metric v4 只是写在代码里的目标，不是 optimizer 真正在最大化的目标。**

此外，#160 还需修订以下契约后才适合落地：

- 明确固定 `dspy==3.3.0` / `gepa==0.1.1`，不在本 Issue 顺手升级 3.3.1；后者虽增加
  multi-proposal 与 objective-aware frontier，却有官方已知的 full-val trace 对齐限制。
- `TradeRelevanceV1` 的枚举必须在编译前完整、无歧义；当前渠道缺少
  `commodity_supply`，却用“铜供应中断”作为正例，schema 无法表达自己的 gold。
- `channels` / `affected_markets` 的“最多 4 个”不能只写注释；还需代码约束、去重和 canonical
  顺序。GEPA 不会改字段类型、描述、枚举或 validator。
- #148 的 semantic judge 只应用于自由文本卡片维度；TradeRelevance 的枚举与集合必须 exact
  typed gold，不能交给语义 judge。
- 保持两个 Predictor 的调用次数和顺序不仅是成本选择，也是当前 GEPA trace 归因正确性的前提；
  `program_compiler.py` 目前按“第 1 次调用是 EventSemantics、第 2 次是 ReaderCard”做 positional
  re-key。
- 当前 model-visible payload 还含 `queue_lag_s`。#150 已把 timeliness 定义为 delivery-owned，
  #160 若不明确删除或授权它，queue/editorial authority separation 仍不完整。
- 新 RulePack 将是第 9 个，但代码上限是 8；`trade_relevance_attention@1` 是展示形式，不是合法
  `rule_id`（正则不允许 `@`）。应使用 `rule_id="trade_relevance_attention", revision=1`，并显式
  调整/重构 pack 上限与顺序。
- telemetry deterministic lane 从不经过 Program，而 #160 又要求 policy v10 新行 relevance
  非空；不能伪造模型 TradeRelevance。必须为 `model | listing_deterministic |
  telemetry_deterministic | degraded` 建立 typed provenance，或把 deterministic lane 明确排除在
  relevance 必填与 v4 optimizer denominator 外。
- 当前 optimizer 的 failure-cluster 选择按 `production_verdict.decision` 判断 push/hold，没有执行
  frozen `decide()`；因此“模型 hold、policy priority rescue 后实际误推”恰好可能不进入 GEPA
  失败样本。选簇与打分必须共用同一 production-action pure function。
- consumer stale re-ask 目前只保存/复用 `first_verdict`。v6 增加 sibling editorial 后必须原子保存
  和复用整个 `SemanticJudgment(verdict + editorial)`，否则会丢 relevance 或把第一次 editorial
  与第二次 verdict 混配。

按用户明确偏好，本报告建议做**真正 hard cut**：无 `priority` alias、无双 factory、无旧
Program 执行兼容；新 binary 只接受 factory v4 / Program v6 / policy v10 / review v4 / metric v4。
旧 Program、旧 verdict、v2/v3 review 和旧 epoch evidence 保持字节级历史可读，但一律
audit-only，不能进入 v6 runtime、v4 metric、GEPA corpus、DemoBank 或发布证据。需要比较 v5/v6
时使用已封存的旧 exact image/recorded outputs 做离线对照，而不是在新 runtime 内保留旧 factory。

另外还有两个 release-contract 冲突必须一起解决：现有 canary activation 虽持久化
selector/profile SHA，assignment 与 worker startup 却不校验 eligibility/selector identity；而
Issue 同时要求 priority 硬改名、无 alias，又要求 HTTP shape 不变，当前 API 与 React
filter/sort/badge/detail 则都把 priority 当成读者重要性。前者会让新 selector 在旧 receipt 名下
静默运行，后者的两条要求无法同时满足。

## 判定口径：哪些是 DSPy 规范，哪些不是

全文用三类口径，避免把项目治理误写成框架要求：

| 类别 | 含义 | 例子 |
|---|---|---|
| **DSPy 强制契约** | 官方 API/3.3.0 源码实际执行的行为，违反会报错、被忽略或产生不同运行语义 | `Signature` 的 typed fields；GEPA metric 调用协议；普通 Predict 的 GEPA candidate 只替换 signature instructions；LM 默认 cache/retry |
| **DSPy 官方建议/典型做法** | 官方教程或文档推荐，但框架不强制 | baseline first；独立 train/val；强 reflection LM；rich textual feedback；根据问题选择 instruction/demo optimizer |
| **Tracefold 更严格约束** | 为审计、安全、成本、单写者和发布治理建立的项目规则，不应冒充 DSPy 要求 | canonical state-only Artifact；exact dependency fail-closed；无 pickle；cache/retry/history 关闭；两调用上限；future holdout、shadow、one-arm canary、人工 promotion |

这一区分对 #160 很重要：DSPy 允许更自由的保存、重试、缓存、demo 甚至 Flex 代码优化；
Tracefold 有理由把它们禁掉，但理由是本项目的可信执行与成本契约，不是“DSPy 规定如此”。

## Issue / PR 脉络复核

### 直接相关 Issue

| Issue | 结论与 #160 的关系 | 对 #160 的约束 |
|---|---|---|
| [#117](https://github.com/AnalyThothAI/tracefold/issues/117) | event taxonomy v2，描述“事件是什么”；与“是否值得现在打断交易员”正交 | 可独立排期；不能用扩充 `event_type` 代替 TradeRelevance |
| [#129](https://github.com/AnalyThothAI/tracefold/issues/129) | Program-native News Triage 总体 hard-cut spec；规定 framework-neutral `SemanticJudge`、固定两个 Predictor、确定性 policy 保留最终权威 | 支持 #160 保持两调用、不引入 agent topology；新一代必须仍从单一业务接口进入 |
| [#134](https://github.com/AnalyThothAI/tracefold/issues/134) | D-generation hard cut；定义 QualityKernel/RulePacks 与 LearnedStrategy/DemoBank 的所有权边界、冷编译与受信 applier | #160 不能让 GEPA改 schema/RulePack/policy/deploy；旧 epoch 可审计但不能继续作为新代发布证据 |
| [#138](https://github.com/AnalyThothAI/tracefold/issues/138) | 候选相关 ToldContext、Predictor 输入分区、production-action metric、connected-cluster/time split；明确 GEPA 只做 instruction，DemoBank 不会被该 optimizer 填充 | #160 应复用同一 action ruler 和 split；TradeRelevance 必须冻结进每条 metric episode；不应声称 GEPA 生成 demos |
| [#143](https://github.com/AnalyThothAI/tracefold/issues/143) | 把 `dspy.Evaluate` baseline 与真实 GEPA 跑通，真实运行暴露了 5-arg/full-val metric、匿名 inner Predict trace、RulePack proposer 等问题 | 支持 baseline-first、custom proposer、独立 reflection endpoint；但当前 CLI compile 又出现新的整链断点，不能只引用“曾跑通” |
| [#148](https://github.com/AnalyThothAI/tracefold/issues/148) | 证明 ReaderCard 自由文本按字节比对结构性不可赢；加入 metric-only semantic equivalence judge 和 v3 gold drafter | relevance typed fields仍必须 exact；更关键的是当前 judge 未进入 hermetic compiler，见本文 P0 |
| [#150](https://github.com/AnalyThothAI/tracefold/issues/150) | 将含糊 live 拆成 `recorded`、`compile_live`、`runtime_live`；冻结 policy；同时报告 answered quality 与 failure-as-zero | #160 三种模式不得合并；`compile_live` 不等于生产可靠性，`runtime_live` 也不包含 consumer transaction/broker/delivery |
| [#160](https://github.com/AnalyThothAI/tracefold/issues/160) | 本次目标：queue/editorial 权威分离、TradeRelevance、review/metric/policy/Program hard cut | 产品根因与 DSPy 形状成立，但依赖、schema、compiler 和 hard-cut 条款需按本文修订 |

### 直接相关实现 PR

| PR | 已落地事实 | #160 需要保留或纠正 |
|---|---|---|
| [#130](https://github.com/AnalyThothAI/tracefold/pull/130)、[#133](https://github.com/AnalyThothAI/tracefold/pull/133)、[#135](https://github.com/AnalyThothAI/tracefold/pull/135) | 两 Predictor DSPy Program、quality baseline、D-generation Artifact/optimizer 边界 | 新代可重写 factory，但不能恢复多条语义 lane 或模型投递权 |
| [#136](https://github.com/AnalyThothAI/tracefold/pull/136) | recall-first policy v8，修正 `noise` 一票否决 | #160 删除 priority rescue 时必须用 objective guards 保住真正召回，而不是再引入语义 hint |
| [#139](https://github.com/AnalyThothAI/tracefold/pull/139)、[#141](https://github.com/AnalyThothAI/tracefold/pull/141)、[#142](https://github.com/AnalyThothAI/tracefold/pull/142) | told instrument、candidate-conditioned ledger、Predictor 输入隔离、production-action metric 与 honest split | 保持 EventSemantics 独占 told；ReaderCard 不应拥有 relevance；metric 必须执行冻结 policy |
| [#145](https://github.com/AnalyThothAI/tracefold/pull/145) | `dspy.Evaluate` baseline、可运行 GEPA、trace re-key、custom RulePack proposer | trace re-key 依赖固定两次/固定顺序；当前真实 CLI chain 的 token/bundle 问题需新集成测试覆盖 |
| [#151](https://github.com/AnalyThothAI/tracefold/pull/151) | CardEquivalenceJudge、v3 gold drafter；实测 semantic judge 相对逐字尺子 `+0.060662`，不是 Issue 预估的约 `+0.13`；默认 adapter 曾造成双调用，512 tokens 曾截断 | 不得继续引用预估值；judge 必须固定 JSONAdapter、足够 token、单独计费；当前只在 baseline 接通、未在 hermetic optimizer 接通 |
| [#155](https://github.com/AnalyThothAI/tracefold/pull/155) | 修复编号 digest 上下文串扰、其他条目 macro lexicon 污染、时刻被误拆 | Phase 0 必须只用 post-#155 exact-image cohort，不能把 parser/context 旧缺陷归因给 TradeRelevance |
| [#157](https://github.com/AnalyThothAI/tracefold/pull/157) | source artifact identity 与 stale source policy；metric projection因此新增 source age | 任何影响 policy v10 的 relevance/provenance字段都必须随 episode 冻结，不能运行时回读 current default |
| [#158](https://github.com/AnalyThothAI/tracefold/pull/158) | 三 baseline 模式、policy freeze、v2 report、failure lower bound | #160 应保留逐 case outcome、失败入零、route/cost，而非只看单 scalar |
| [#159](https://github.com/AnalyThothAI/tracefold/pull/159) | `corpus_sha256`、policy/schema fail-before-spend、metric v3 身份与校准收尾 | metric v4 必须新建 calibration fixture 和 identity，不能沿用 v3 黄金数字 |

## 官方 DSPy 契约逐项核对

### 1. Module、Signature、Predict 与嵌套 typed output

**DSPy 强制契约。** `dspy.Signature` 以 Pydantic model 为底层 contract，可用明确的
`InputField` / `OutputField` 类型和嵌套 Pydantic model；字段顺序有语义。`dspy.Module` 在
`__init__` 声明 submodules，在 `forward` 组合它们并返回 `Prediction`；`Predict` 是可被 optimizer
发现的参数。官方说明见 [Signatures in Depth](https://dspy.ai/diving-deeper/signatures-in-depth/)
和 [Modules](https://dspy.ai/diving-deeper/modules/)（均访问于 2026-08-23）。

因此，将 `TradeRelevanceV1` 作为 `EventSemantics.v2` 的一个 nested Pydantic output 是合法且
优先的落地形状。它比第三 Predictor 更符合现有所有权：EventSemantics 同时读取事件证据和
ToldContext，ReaderCard 只负责文案。

但 typed 不等于“schema 自动正确”。`Signature.with_instructions()` 保留字段、只换 instruction；
官方 3.3.0 源码见
[`signature.py`](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/signatures/signature.py)
（访问于 2026-08-23）。普通 GEPA candidate 无法增删 `TradeRelevanceV1` 字段、修正 enum、添加
`max_length` 或补 validator。**schema 和 taxonomy 必须由 code owner 在 baseline 前定稿。**

### 2. Structured output 与 adapter

**DSPy 强制契约。** DSPy 3.3.0 的 `JSONAdapter` 会为明确的 output fields 构造 Pydantic
structured-output model，并递归处理 nested object/array；开放式 `dict` 无法形成封闭 schema，
会使用 JSON object 模式。官方源码见
[`json_adapter.py`](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/adapters/json_adapter.py)
（访问于 2026-08-23）。所以 `TradeRelevanceV1` 应保持封闭 enum/tuple，而不是
`dict[str, Any]`。

官方 adapter 同时允许格式 fallback。`ChatAdapter` 默认
`use_json_adapter_fallback=True`；`JSONAdapter` 在本地 structured schema/setup 失败时也可改用
JSON object。源码见
[`chat_adapter.py`](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/adapters/chat_adapter.py)
（访问于 2026-08-23）。这解释了 #148 实测“一次 verdict 两次物理请求”。

**Tracefold 更严格约束。** 当前 `DspyStrictJSONAdapter` 明确只允许一种格式，不做隐式格式
重试；`DspyPredictorAdapter` 还拒绝 DSPy 默认 cache/retry。#160 增加 nested output 后必须继续
走该 strict adapter，并用 provider integration test 证明：

- 正常 EventSemantics 恰好一个 physical call；
- 正常 ReaderCard 恰好一个 physical call；
- schema/parse 失败不会在 adapter 内偷偷重发；
- 输出 token 增长与截断分别计入 receipt。

### 3. Evaluate 与 metric 调用协议

**DSPy 强制契约。** 官方 metric 是 duck-typed callable；GEPA 会在 module-level 和
predictor-level 两种上下文调用。常见协议为
`(gold, pred, trace=None, pred_name=None, pred_trace=None)`；3.3.0 为 Flex 又允许可选第六个
`program_trace`。metric 可返回 scalar，也可返回包含 `score` 与 `feedback` 的
`dspy.Prediction`。`dspy.Evaluate` 是批量评估 harness，失败按 `failure_score` 处理；官方说明见
[Metrics and Evaluation](https://dspy.ai/diving-deeper/metrics-and-evaluation/) 与 3.3.0
[`evaluate.py`](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/evaluate/evaluate.py)
（访问于 2026-08-23）。

由此得到三条 #160 必须保持的约束：

1. policy/relevance/schema projection 必须在第一笔 provider spend 前完整验证；否则
   `Evaluate` 可能把 metric wiring defect 记成普通 0 分。
2. 同一 gold/prediction 在带/不带 `pred_name` 时必须返回相同 module-level `score`，只过滤
   `feedback`。DSPy 3.3.0 明确说明 predictor-level score 尚不被支持，当前 Tracefold 也打开了
   `warn_on_score_mismatch=True`。
3. hard-gated case 仍需保留 action、per-dimension outcomes，并让 0 进入 denominator；这是
   Tracefold 的 decision-grade 报告约束，不是 DSPy 自动提供的能力。

### 4. GEPA 的真实优化表面

**DSPy 强制契约。** 对当前这种普通、非 Flex 的两个 `Predict` Program，GEPA 3.3.0：

- 从 `named_predictors()` 读取各 Predictor 的 `signature.instructions`；
- deep-copy student；
- 用 `with_instructions(candidate[name])` 替换选中 Predictor 的 instruction；
- 保持 demos、submodule 结构和其他状态不变。

源码见 3.3.0
[`gepa.py`](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/teleprompt/gepa/gepa.py) 与
[`gepa_utils.py`](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/teleprompt/gepa/gepa_utils.py)
（访问于 2026-08-23）。

需要精确限定这句话：DSPy 3.3.0 另有 experimental `Flex`，GEPA 可在 Flex 场景优化代码结构；
所以“GEPA 天生只能改 prompt”不是全局框架事实。**对 #160 的 non-Flex fixed graph，且在
Tracefold 显式禁止 Flex 的条件下，GEPA candidate 才只是 instruction candidate。**

`Predict.reset()` 初始 `demos=[]`，state dump 包含 demos/signature/LM；源码见
[`predict.py`](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/predict/predict.py)
（访问于 2026-08-23）。GEPA 3.3.0 的上述 build path 不写 demos。因此 #160 应改成：

> 本轮 GEPA 只可改变两个 LearnedStrategy instruction；DemoBank records/refs 必须保持空且进入
> machine diff。若未来要优化 demos，另立 MIPROv2/BootstrapFewShot ablation。

官方 [MIPROv2](https://dspy.ai/api/optimizers/MIPROv2/) 会联合选择 instructions/demos，
[BootstrapFewShot](https://dspy.ai/api/optimizers/BootstrapFewShot/) 会构造 demonstrations
（均访问于 2026-08-23）。这两者是不同 optimizer，不应通过模糊的“GEPA 可写 demo refs”混在
本 Issue。

### 5. rich feedback、component selection 与 multi-stage Program

**DSPy 官方建议/典型做法。** GEPA 通过 metric 返回的自然语言 feedback 反思失败；
`pred_name` / `pred_trace` 让 feedback 针对拥有问题的 Predictor。官方
[GEPA in Depth](https://dspy.ai/diving-deeper/gepa-in-depth/)、
[GEPA API](https://dspy.ai/api/optimizers/GEPA/overview/) 和
[GEPA Advanced](https://dspy.ai/api/optimizers/GEPA/GEPA_Advanced/)
（均访问于 2026-08-23）也支持独立 reflection LM、custom instruction proposer、trainset/valset、
不同 component selector。

#160 的 predictor ownership 是清楚的：

- EventSemantics feedback：TradeRelevance、现有 semantic enums、novelty；
- ReaderCard feedback：headline/why/factual retention；
- module-level score：真实 policy v10 action + 全部 components，同一 prediction 始终相同。

因此第一轮继续 `component_selector="round_robin"` 是合理的；没有证据要求改成同时重写两个
component。若以后证明 EventSemantics 与 ReaderCard instruction 必须联动，再以 sealed ablation
比较 `"all"`，不要在 #160 先扩大搜索空间。

固定两阶段 Program 也完全合法。DSPy 没有“multi-stage task 必须改成 Agent”的规定；Module
本来就用于组合多个 Predict。当前 Tracefold 还有更强的本地理由保持顺序：
`program_compiler.py::_rekey_trace()` 按两个 serial calls 的位置映射到
`event_semantics` / `reader_card`。增加、删除、并行或交换调用都会使当前 GEPA reflective trace
归因失真，必须先重写并验证归因器。

### 6. train / val / holdout

**DSPy 强制契约与官方建议并存。** GEPA `compile(student, trainset, valset)` 用 trainset 产生
reflection data、用 valset 跟踪/选择 candidate；不传 valset 时可复用 trainset，但官方会提示
overfit 风险。官方建议给足够大的 trainset 和独立、代表性的 valset；见上述 GEPA API（访问于
2026-08-23）。

#138 的 connected fact cluster + time-ordered 70/30 split 与该建议一致，并比框架默认更严格。
future holdout 完全不可见、shadow、canary、人工 promotion 是 Tracefold 发布治理，不是 DSPy
compile API 强制，但必须保留。

### 7. cache、retry、history 与物理调用

**DSPy 强制契约。** DSPy 3.3.0 `LM` 默认 `cache=True`、`num_retries=3`；官方缓存教程也说明
内存/磁盘 cache 默认开启。见 [Cache](https://dspy.ai/tutorials/cache/) 和 3.3.0
[`lm.py`](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/clients/lm.py)
（访问于 2026-08-23）。

**Tracefold 更严格约束。** 生产 Program、compile task/reflection/judge 都应显式
`cache=False, num_retries=0`，由 Tracefold 外层预算、route retry/fallback 和 receipt 统一记账。
CardEquivalenceJudge 自己的内容寻址内存 cache 可以保留，但它是 metric-level deterministic
memoization，必须单独报告 `calls/cache_hits/failures`，不能混成 DSPy/provider cache。

### 8. save/load/state/artifact

**DSPy 强制契约。** 官方支持：

- state-only JSON/Pickle；
- whole-program cloudpickle；
- state 中包含 Predictor instructions、demos、LM state；
- load 先在 deepcopy 上试装再作用于原对象；
- dependency mismatch 默认记录 warning 后继续，而非 fail closed。

官方说明见 [Saving and Loading](https://dspy.ai/diving-deeper/saving-and-loading/) 与 3.3.0
[`base_module.py`](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/primitives/base_module.py)
（访问于 2026-08-23）。

**Tracefold 更严格约束。** canonical JSON、内容 SHA、无 pickle/cloudpickle、QualityKernel 与
optimizer-owned state 分离、exact dependency/factory/signature fail closed 都是合理的项目规则。
不能用 DSPy native save/load 代替可信 Artifact，因为它的所有权范围更宽、版本不一致只警告。

#160 可以复用 `ProgramArtifact v2` 的 envelope，前提是新 codec 在新 binary 中只接受
factory v4 / Program v6 的 exact literals 和新 hashes；不应加入 v3/v4 factory union。若
compiler bundle/metric receipt 新增 `metric_judge` role，相关 bundle/receipt schema 应正常升版，
这不是“为了版本对称”，而是 wire contract 实际变化。

### 9. DSPy 3.3.0 与 3.3.1

仓库当前在 `pyproject.toml:9` 固定 `dspy==3.3.0`，`uv.lock` 固定 `gepa==0.1.1`。DSPy 官方
[3.3.0 release](https://github.com/stanfordnlp/dspy/releases/tag/3.3.0) 发布于 2026-08-03，
[3.3.1 release](https://github.com/stanfordnlp/dspy/releases/tag/3.3.1) 发布于 2026-08-21
（均访问于 2026-08-23）。

3.3.1 将 GEPA 升至 0.1.4，新增 multi-proposal 和 objective-aware frontier；但官方明确：scalar
仍负责 acceptance 与 final winner，而且 full validation trace 在 runtime failure 后存在可能缺失/
错位的已知限制，计划在 GEPA 0.1.5 修复。

因此 #160 不应借机升级：

- 继续固定 `dspy==3.3.0/gepa==0.1.1`；
- metric v4 的 buckets/hard gates 保持 Tracefold release gates，不假设 multi-objective frontier
  会替代它们；
- 若另立升级 Issue，应新建 dependency lock/factory/compiler image/receipt identity，并针对
  full-val trace failure 做整链测试；旧 compile evidence audit-only。

## P0：当前 hermetic compile 的四处闭环断点

### P0-1：32k reflection 配置永远过不了 16k schema

代码证据：

- `src/tracefold/news/agents/program_compiler_trusted.py:38-44`：
  `REFLECTION_MAX_TOKENS = 32_000`；注释明确它来自 GEPA reflection 推荐预算。
- `src/tracefold/news/agents/program_compiler_proxy.py:78-86`：
  `CompilerProviderEndpointSecret.max_tokens = Field(ge=64, le=16_384)`。
- `src/tracefold/app/cli/commands/news.py:475-481`：CLI 用上述 32k 构造 reflection secret。

用当前类型直接构造得到 Pydantic `less_than_equal`，上限 `16384`。这个错误发生在 Docker
launcher、proxy 和 GEPA 之前，所以当前真实 CLI compile 没有机会开始。

**修订要求：** 为 reflection role 建立一致的、code-owned token ceiling；若继续采用官方示例的
32k，就必须同时调整 secret、grant、bundle、proxy request validation 和 tariff reservation 的
上限及测试。不要只放宽一个 Pydantic 字段。

### P0-2：CLI seal bundle 写错 reflection token/timeout

代码证据：

- `src/tracefold/app/cli/commands/news.py:499-508` 正确用 `reflection_secret` 构造
  `CompilerModelProxyGrant`；
- 但 `src/tracefold/app/cli/commands/news.py:537-540` 在 `seal_compile_input()` 中把
  `reflection_max_output_tokens` 和 `reflection_timeout_seconds` 都写成 `endpoint_secret`（task）
  的值；
- `src/tracefold/news/agents/program_compiler_launcher.py:145-154` 要求 bundle 值与实际
  reflection secret 完全一致。

所以即便先让 32k secret 通过，launcher 仍会报
`news_program_compile_launcher_proxy_binding_mismatch`。

**修订要求：** bundle 必须只从对应 role 的 typed endpoint object 构造，避免四个相邻 scalar
位置传参。建议引入 per-role sealed config model，统一由同一个对象派生 secret-free identity、grant、
bundle 和 proxy enforcement，消除 task/reflection/metric-judge 的手工复制错误。

### P0-3：#148 judge 没有进入 hermetic optimizer

完整 call graph 证据：

1. `src/tracefold/news/agents/program_compiler.py:502-531` 的构造器虽有可选 `judge=None`，并注释
   “baseline 与 optimizer 必须用同一 ruler”；
2. `compile()` 随后执行 `metric = bind_metric(self._judge)`；`bind_metric(None)` 就是旧逐字尺子；
3. `src/tracefold/news/agents/program_compiler_runner.py:158-176` 是生产 hermetic runner 唯一
   `ProgramCompiler(...)` 调用点，却没有传 `judge`；
4. 当前 `CompileInputBundle`、proxy grant、tariff 和 call receipt 只有 `task` / `reflection` 两个
   role，也没有 metric-side judge 的 endpoint/budget/identity；
5. 相反，baseline CLI 在 `src/tracefold/app/cli/commands/news.py:1076-1095` 才根据
   `--semantic-judge` 构造 judge，并传给 `run_baseline()`；
6. `program_baseline.py` 的 `compile_live` 用 `bind_metric(judge)` 评分，因此 #151 的 live 实测
   证明的是 baseline path 的尺子，不是 hermetic `news learning compile` 的尺子。

结论：**#148 semantic judge 当前确实没有进入 hermetic compile。** `metric_receipt` 在该路径会把
`semantic_judge` 记为 `null`，GEPA 的 ReaderCard 15%（#160 拟改 10%）仍按字节相等，正是 #148
要消除的结构性不可赢目标。

**无兼容修复建议：** 在新的 compiler protocol 中新增明确的 `metric_judge` role：

- 可以使用独立 endpoint，也可以显式绑定到与 reflection 相同的物理 endpoint；即使物理 endpoint
  相同，role、limit、tariff、call counters 和 receipt identity 也必须分开，禁止隐式 alias；
- 为 judge 固定自己的 output tokens（#151 实测 512 会截断，当前已采用 8192 的经验值）、timeout、
  max calls、max cost、adapter、instruction/schema/model hashes；
- runner 用 `CompilerProxyLM(role="metric_judge")` 构造 `CardEquivalenceJudge`，再显式
  `ProgramCompiler(..., judge=judge)`；
- `CompileBudget`、proxy grant、input bundle、runner/proxy receipts 和 final provenance 分别记录
  task/reflection/judge calls 与 cost；judge memoization hits 单列；
- compile 前强制 judge self-calibration/identity 完整；judge 不可用时不得静默退成另一个发布尺子。
  单 case 可保留 #148 的严格失败结果，但整次 candidate 是否可作为 release evidence 必须有
  judge failure gate；
- 新增从真实 CLI 参数/typed bundle，经 launcher/preflight/runner，到 non-null judge metric receipt 和
  patch 的整链测试。只测 `ProgramCompiler(judge=fake)` 不足以证明生产 wiring。

前三处 wiring 断点与下述 action-scope 断点必须共同作为 #160 的 PR-0/P0 前置项；修复前不得
产出或引用“v6 GEPA uplift”。

### P0-3 的补充：judge identity 仍不足以证明是同一把尺子

即使把 #148 judge 接进 runner，当前 CardEquivalenceJudge.identity 也只绑定 model 字符串、
instruction、字段名和 output schema。它没有绑定 max tokens、JSONAdapter 配置、endpoint
identity、timeout、LM kwargs（特别是 thinking disabled）、temperature、cache 与 retry。

#148 自己已经用实测证明 adapter 与 token budget 会改变调用次数、截断和判分；因此这些不是运行
细节，而是 metric ruler 的组成部分。metric v4 receipt 必须绑定完整 judge execution identity，
并对任一漂移 fail closed。秘密值不入 receipt，只记录经过规范化的非秘密配置与内容 SHA。

### P0-4：optimizer 失败选簇没有使用 production action

`src/tracefold/news/agents/program_compiler.py:669-694` 的 `_failure_scope()` 用
`production_verdict["decision"] in {"push", "escalate"}` 判断 action 是否失败。该字段只是模型
intent；当前真正送达动作由冻结 policy `decide()` 决定。#160 的核心错误形状正是：

```text
model decision = hold
priority rescue = push
accepted review = should/must_hold
```

这种 case 在 `_failure_scope()` 看起来“模型没有 push”，可能不被加入 failure cluster；但
metric 的 `_production_action()` 会把它判成真实误推。结果是 metric 知道错，optimizer 的 reflective
dataset 却可能根本看不到这个最重要的错误簇。

**修订要求：** failure selection、metric scoring、baseline report 三处共用同一个冻结
production-action pure function和同一 `policy_values/policy_sha256` projection。不得分别重写动作
推导。选簇 projection 也必须在 provider spend 前验证；Acceptance 加入一条 priority-rescued
false interrupt，证明它进入 failure clusters 并只向 EventSemantics 发 relevance feedback。

## #160 对照当前代码的影响

### 1. 两调用 topology：支持，但现在是隐藏硬依赖

当前 `_FeedbackCompileProgram._rekey_trace()` 在
`src/tracefold/news/agents/program_compiler.py:247-267` 明确按两个串行 trace entry 的位置重绑到
`event_semantics`、`reader_card`。所以 #160 采用 nested TradeRelevance、保持两个 Predictor 是
正确选择。Acceptance 应从“正常成功 2 calls”加强为：

- named predictors 恰为 `("event_semantics", "reader_card")`；
- 每个 case 成功 trace 恰有两个按顺序匹配的 entry；
- schema failure、advisory rejection、provider failure 下的 trace 仍正确归因；
- judge 调用不进入 Program physical-call count，而进入 metric-side count。

### 2. EventSemantics.v2 schema：方向正确，当前草案未闭合

需在 Issue 修改：

1. 增加 `commodity_supply`；不要用 `commodity_demand` 表示供应中断。
2. 中国降准示例不应无定义地写成 `usd_liquidity`。要么增加/重命名为更中性的
   `liquidity_conditions` / `domestic_liquidity`，要么把该例只标 `commodity_demand` 并说明传导链。
3. `channels` / `affected_markets` 用真正的 `max_length=4` 约束；增加去重、canonical sort 与
   allowed combination validator。tuple 类型本身不会去重。
4. 明确定义空集合何时合法，以及 `reader_value=background/none` 与空 channel/market 的关系。
5. `impact_breadth`、`tradability`、`reader_value` 是不同轴；在 RulePack 与 validator 中给出所有
   易混边界，不能依赖 GEPA补 taxonomy。
6. Review v4 对每个 typed enum/set 使用 exact gold；集合先 canonicalize 再比较。

### 3. model-visible authority：还漏了 `queue_lag_s`

#160 删除 `event.priority`、`provider_score`、`gate.macro_lexicon` 是对的。当前
`src/tracefold/news/semantic_contract.py:316,441` 仍把 `queue_lag_s` 渲染进模型；它是运行/排队
状态，不是事件事实，且 #150 已把 timeliness 移到 delivery-owned report。

建议从 EventSemantics.v2 和 ReaderCard model-visible payload 一并删除。若确有需求，Issue 必须
明确它只用于某个定义清楚的字段，并用 sealed ablation 证明；不能默认为兼容保留。

同理，`watchlist` 若只作为 objective recall guard，最干净的权威分离是让 policy 使用而不让模型
用它推断“普遍交易相关性”。若产品定义的 `reader_value` 本来就是 account-specific，则 Issue 必须
明确该语义，并让 review gold 使用同一 watchlist snapshot。两种含义不能混用。

Issue 给出的 realtime_eligible 伪代码也没有通过自己的 acceptance：地方监管直接影响美股的
例子若是 material_detail + surprise unknown，会因 material_change=false 被拒绝，尽管测试条款要求
realtime。必须先写成完整 truth table，明确 direct material exception、scheduled/in-line、state
change 与 reader_value 的组合，再编码 pure function。

RecallGuard 也不应是一个互斥 enum；同一 Event 可以同时 watchlist-grounded 与
listing-deterministic。更稳的 Interface 是一组 canonical objective facts，或直接把每个 guard
作为 code-owned boolean 输入给有序 policy，避免为了序列化丢失同时成立的事实。

Issue 的 Review v4 列表还漏了 trade_affected_markets，人工必验字段列表也漏了 surprise 与
affected_markets。若不补，现有 expected-requires-failed-dimension 约束无法合法提交/评分这些 gold；
这必须在 schema 定稿时一起修复，不能让自由文本 correction 兜底。

### 4. ReaderCard 是否看到 relevance 必须写清

当前第二个 Predictor 接收 canonical EventSemantics JSON。加入 nested relevance 后，如果沿用整个
JSON，ReaderCard 会看到 relevance：

- 好处：文案可准确解释 transmission channel；
- 代价：第二次调用 input tokens 增长，且 card 文案可能把模型 intent 当作事实复述。

这不改变 relevance 的所有权，因为 ReaderCard 没有相应 output，但 #160 应在 signature 中明确
选择：要么传一个只读、经过 normalizer 的 relevance projection；要么从 card input 排除
`reader_value`，只提供可解释的 channels/breadth。无论选择哪种，都必须进入 factory/input schema
identity，并实测总 token/cost 增长 `<=10%`；DSPy 不提供该成本保证。

### 5. RulePack 数量和 ID 直接冲突

`semantic_program.py:75` 的 `PROGRAM_RULE_PACK_MAX=8`，当前已有八个；新 pack 不能直接 append。
`semantic_program.py:543` 的 `rule_id` pattern 为 `^[a-z][a-z0-9_]{2,63}$`，所以 Issue 中
`trade_relevance_attention@1` 只能作为显示名。

推荐 Issue 改为：

```text
rule_id = trade_relevance_attention
revision = 1
```

并由 code owner 决定合并旧 pack 或把上限升为 9；同时更新 QualityKernel/RulePack root、prompt
budget 和 exact fixture。不要让 optimizer通过 LearnedStrategy承担本应 code-owned 的基础定义。

### 6. metric v4：大方向正确，需保持 #148/#150 的真实边界

支持：45% action / 35% relevance / 10% existing semantics / 10% card 是 code-owned objective，
DSPy 不规定权重，但允许这样的 scalar + rich feedback。

必须修订/补充：

- relevance 无 gold 就不评分，不使用“改了就得分”；正确。
- relevance enum/set 用 exact gold；CardEquivalenceJudge 只处理 headline/why 等自由文本；正确。
- `pred_name` 只过滤 feedback，不改变同一 prediction 的 scalar；写入 acceptance test。
- 当前 `program_metric.py:542-544` 会把自由文本 `expected_correction` 无条件追加给两个
  `pred_name`。v4 relevance feedback 必须来自 typed v4 gold，并按 owner 只给 EventSemantics；
  ReaderCard 只收到它能修复的 headline/why/factual-copy feedback。通用 correction 若保留，必须
  被拆成 owner-scoped 字段，不能继续广播。
- judge/task/reflection calls、tokens、cost 分开报告；#148 的 judge calls 不属于“生产 Program 正常
  两调用”。
- corruption 在任何 task/judge/reflection provider call 前 fail closed。
- 新建 metric v4 redacted calibration fixture 和唯一 expected values；不能沿用 v3 的
  `0.896373/n=162`，该数字只证明旧 metric wiring。
- `recorded` 校准仍应零 judge calls（identical text fast path）；`compile_live` 和 hermetic GEPA
  必须使用同一 non-null judge identity，否则 before/after 不可比。
- report 除 scalar 外继续发布 hard buckets；这是 release gate，不应误写为 GEPA objective-aware
  frontier 已自动保证。

45% final action 与 35% reader_value/relevance 可能对同一 human label 双重加权；DSPy 允许，但
Issue 必须把它写成有意的 product objective，并用 ablation 报告而不是当成自然权重。缺失 gold
会让 component 内部重新归一化，receipt/report 还应公开每 case 的 effective weight mass 与各字段
denominator。

hard gate 名称 must_interrupt 与现有 review label must_push 不一致，也要确定唯一映射；objective
listing/watchlist guard 可能有意 realtime，即使 reviewer 将普通 editorial value 标成 background。
这类 case 应在 review submission 时拒绝为 contract-conflicting，或作为 objective-guard 独立 bucket，
不能同时要求 background_sent_realtime=0 又让 policy 正确 fail-open。

补充时间口径：#148 评论中的 0.896373 / n=162 是当时 live corpus 的历史读数，不是当前校准
常量。当前 checked-in redacted fixture 的唯一 source of truth 是
tests/news/test_news_baseline_calibration.py：n=242、case macro=0.888206、cluster
macro=0.89004。metric v4 必须新增自己的 fixture/expected values，不能覆盖或继承这组 v3 数字。

### 7. deterministic / degraded lane 与 relevance 非空冲突

当前 OI telemetry 明确绕过 Program。模型故障的 degraded card 也没有 EventSemantics.v2
TradeRelevance。#160 同时规定“Program v6 / policy v10 新行 editorial 必须非空”和 policy 第 2 步
先处理 listing/telemetry，这在类型上未闭合。

不要为兼容而伪造 `TradeRelevanceV1`。推荐持久化 typed envelope：

```text
editorial_source = model | listing_deterministic | telemetry_deterministic | degraded
editorial_contract_version
relevance = TradeRelevanceV1 | null  # 仅 model source 必填
deterministic_reason / degraded_reason
editorial_sha256                     # 对整个 envelope，而非仅 relevance
```

policy v10 先按 source 进入 objective lane；只有 model source 调 `realtime_eligible(relevance)`。
v4 Program metric/GEPA corpus排除 telemetry，与现有模型健康 denominator 一致。旧行仍可审计显示，
但不能被新 runtime“补默认 relevance”后执行。

### 8. stale re-ask 必须原子复用完整 judgment

当前 consumer 在 `src/tracefold/news/consumers.py:1009` 只声明
`first_verdict: TriageVerdict | None`，在 stale settle 后于 `:1214` 只缓存 verdict；told-only re-ask
失败时，`:1038`、`:1105`、`:1139` 也只恢复 `first_verdict`。这在 v5 尚可，因为业务输出都装在
verdict；#160 把 relevance 放到 `SemanticJudgment` sibling 后会立即破坏原子性：

- 只恢复 verdict 会丢失第一次成功调用的 editorial；
- 若循环变量仍保留第二次/部分调用的 editorial，可能把第一次 verdict 与另一次 editorial 配对；
- persisted `editorial_sha256`、Program trace 与最终 `decide()` 输入将不再证明来自同一执行。

v6 应将缓存改为 `first_judgment: SemanticJudgment | None`，并在成功、stale、失败恢复、trace 记录、
settle/persist 全路径以一个不可拆的 typed object传递 `verdict + editorial + trace + usage + identities`。
`SemanticJudgment` validator 也要同时绑定 `verdict_sha256` 与 `editorial_sha256`。这不是兼容字段
补丁，而是新 factory 的原子 contract；旧 v5 consumer 不保留在新 binary。

### 9. failure selection 与 metric 必须使用同一个 action ruler

前述 `_failure_scope()` gap 还会影响 #160 的 targeted strata：如果 failure clusters 先按模型
`decision` 过滤，后面再给 metric 加 typed relevance，也无法让 GEPA看到被 policy rescue 的
local-macro false interrupt。应删除这个第二套 action 判断，直接复用 metric/production 的冻结
`decide()` projection，或在 sealed episode 中保存由同一 pure function验证的 `production_action`。

Acceptance 至少覆盖：model hold + v9 priority rescue push + accepted must_hold；该 case 在旧
attribution 中可识别，在 v4 编译 export 中进入失败簇，在 policy v10 replay 中变为 hold，并且
所有三处动作值来自同一个函数/同一个 policy hash。

### 10. 当前 PR 顺序会让稳定 Artifact 自己失效

semantic_program.py 的 factory source root 明确包含 semantic_contract.py；ProgramArtifact 的
validator 又要求 artifact QualityKernel 与当前 binary 重算结果完全相等。因此 #160 的 PR-1 若先
修改 model-visible contract，却仍声称 stable Program v5 继续运行，旧 Artifact 会因
news_program_quality_kernel_unknown 被拒绝。与此同时，PR-1 的 policy v10 需要 PR-2 才存在的
TradeRelevance，依赖方向也是反的。

这不是迁移脚本可以绕过的小问题，而是 exact factory identity 在正确地 fail closed。按无兼容
偏好，所有影响 model input、signature、normalizer、assembler、policy authority 的变化必须在
同一 Program v6/factory v4 根中冻结并原子切换。可以拆成 stacked review commits，但不能把其中
任一混合态部署为 stable。

### 11. 不能保留两套模型 intent authority

Issue 草案在 EventSemantics.v2 新增 reader_value/tradability，同时保留模型生成的
decision/actionable。前两者与后两者分别回答了近似相同的问题；policy v10 若只认 reader_value，
旧字段会成为无权威但仍被评分、渲染或反馈的重复答案。

推荐让 assembler 从唯一的 normalized relevance 确定性派生 legacy TriageVerdict 字段，至少：

    reader_value=escalate          -> decision=escalate
    reader_value=realtime          -> decision=push
    reader_value=background/none   -> decision=drop

actionable 也应从 tradability、非空 channel/market 与 code-owned eligibility 定义派生，或被明确定义
为正交字段。若仍让模型回答两遍，Issue 必须给出完整一致性矩阵、双重 gold 与冲突处理；简单地把
不一致 drop 掉，会让 GEPA 同时收到互相矛盾的目标。

### 12. Assembler、baseline 与 evaluator 都会静默丢 sibling

直接给 EventSemantics 增加 relevance 会立即触发 assembler 错误：semantic_program.py 当前把
整个 semantics model dump 展开给 extra-forbid 的 TriageVerdict。v6 必须改成显式字段投影，再把
editorial 作为 SemanticJudgment sibling 原子返回。

影响不止 runtime：

- DevelopmentEpisode 目前只有 production_verdict；
- recorded baseline 与 runtime_live 都只构造 Prediction(verdict=...)；
- CandidateEvaluator 的 policy replay、stability hash 与 recording 只处理 verdict；
- trusted DemoBank builder 从 TriageVerdict 反推 EventSemantics，无法重建新 relevance。

应建立唯一 typed ScoredJudgment projection，包含 verdict、editorial envelope、contract version
与 SHA。runtime、recorded、compile_live、runtime_live、compiler、CandidateEvaluator、recording、
replay 和 demo validation 全部使用它，禁止各自拼 dict。即使本轮 DemoBank 必须为空，该 validator
也必须理解 v6 schema，不能靠“现在没 demo”掩盖断链。

身份也不能只靠间接 source hash：当前 EventSemantics signature SHA 只绑定顶层字段名，不绑定
嵌套 Pydantic JSON schema；metric SHA 只取 accepted_review_metric 主函数源码，没有显式绑定它
调用的 production-action、gold/component helper closure。factory/compiler source root 提供了部分
间接保护，但 v4 的独立审计 identity 仍应直接绑定 EventSemantics.v2、TradeRelevanceV1、
SemanticJudgment schema、normalizer/assembler、完整 metric implementation root 与 policy
projection identity。

### 13. priority hard cut 是 HTTP/UI/config 联合变更

当前 priority/queue_priority 相关引用至少覆盖 72 个文件：23 个后端、8 个前端源文件、24 个测试
和 11 个文档。公开面并非稳定旁观者：

- HTTP feed 接受 priority filter 与 sort=priority，并返回 priority；
- React toolbar、query key、URL state、row badge、detail 与生成的 OpenAPI 类型都读取它；
- repository 以 high 优先排序；
- NewsPolicySettings 是 extra-forbid，仍含三项 priority policy knob。

因此“数据库字段硬改名但 HTTP 不变”会继续把 transport hint 暴露成 editorial importance alias。
最佳实践是同一 release 中：

1. 数据库与内部对象直接 priority -> queue_priority，不留 alias；
2. HTTP 若保留操作诊断，明确改名 queue_priority；reader feed 移除基于它的 filter/sort/loudness
   badge，或改用真正的 reader outcome/value；
3. React、OpenAPI、契约测试同步 hard cut；
4. 先从 operator-owned config 删除已废弃 priority policy keys，让旧 binary 临时使用相同默认值，
   运行 tracefold config 验证后再部署 extra-forbid 的新 binary。

更准确的架构约束不是“只有 queue 代码可读取”，因为审计与归因仍需要它；应写成：只有
broker/scheduler 可以让 queue_priority 产生运行时因果效果，storage/audit/measurement 可读，但
Program、policy、ReaderCard 与 reader importance UI 不得以它决策。

### 14. canary 既有 eligibility 偏差和身份漏洞

canary.py 当前把 high priority 排除在候选之外。#160 的 systemic macro positives 与被 priority
rescue 的 local-macro negatives 恰好高度集中于该层；不移除这个 exclusion，v6 learned candidate
的 online canary 永远测不到目标分布。

但只改常量还不安全。arm 时数据库保存 selector_version、eligibility_profile_sha 与
rolling_profile_sha；后续 assign_agent_arm 只校验 baseline bundle，worker startup 只校验 candidate
artifact，二者都不验证 selector/eligibility SHA。于是一个跨部署仍 active 的 canary 会在旧
activation/receipt 名下运行新 eligibility。

v6 release 前必须：

- assignment、resume、startup 都比较 selector version、eligibility SHA、rolling SHA；
- 任一不一致立即 trip，不能自动沿用；
- assignment receipt 保存实际使用的全部 profile identity；
- v6 的 GEPA canary 不再按 queue high 排除，但 listing/telemetry 等 objective deterministic lane
  仍按明确定义排除。

### 15. runtime attribution 与 rollback 也有硬边界

runtime manifest 虽内容寻址 stable/candidates/image/revision，但当前每条 verdict trace 没有
runtime_manifest_sha。滚动发布窗口仅靠时间不能无歧义回填 exact image。Phase 0 只能把旧窗口标为
inferred；先前瞻性写入该 identity，再按用户允许的方式重新积累 24h/72h，之后才可声称 exact
attribution。

另一个三难是：物理数据库列硬改名、完全无 schema alias、旧 exact image 可直接 rollback，三者
不可同时成立。PostgreSQL 将 priority 改为 queue_priority 后，旧 binary 会查询不存在的列。推荐
新 stable binary 仍不带双 factory/alias，但发布前单独封存并演练一个“新 schema、v5 行为”的
rollback image；它不是新 stable 的兼容分支。若连该独立 rollback artifact 也禁止，则 Issue 必须
明确放弃旧 image rollback，改为 forward fix，不能继续勾选 exact-image rollback proof。

### 16. 原子 hard cut 的科学口径

CandidateEvaluator 的一条 candidate 只允许一个 program 或 policy 变量。Program v6 与 policy v10
一起原子切换后，不能把 v5/v9 -> v6/v10 的总差异宣称为单独的“Program uplift”或“policy
uplift”。这是无兼容换来的可解释性代价，应明确接受，而不是偷偷保留双 factory。

Phase 0 的 sealed v9 ablation 仍可证明 priority rescue 的归因；新 v6/v10 组合被定义为新的
code-owned baseline/root。等新 epoch 从零积累后，第一次 GEPA candidate 只改变 v6 的
LearnedStrategy instructions，在固定 v10/metric v4 下恢复 exact-one-variable 比较。若确需代际
离线对照，使用两个封存 exact image 对同一 case root 独立运行并分别出 receipt，不共享不兼容的
provider recording。

## 推荐的真正 hard-cut 落地顺序

### Phase 0：只读归因与 compiler protocol 修复

- 用 post-#155 exact image 的 sealed 24h/72h cohort 做归因与 policy ablation；不改 stable 行为。
- 在新 compiler protocol 中一次性修复 P0-1/P0-2/P0-3，引入 metered metric-judge role。
- 整链测试必须从 CLI typed input 开始，不能只测试 inner `ProgramCompiler`。
- 继续固定 DSPy 3.3.0/gepa 0.1.1。

- 先给每条 verdict 绑定 runtime manifest SHA；现有历史窗口只能标为 inferred，重新积累后才发布
  exact 24h/72h attribution。
- 关闭任何 active canary，并让新 startup/assignment 对 selector 与两个 profile SHA fail closed。

### Phase 1：构建单一新根，不部署兼容中间态

在 task branch 中共同完成并冻结。三个版本轴必须分别命名：Artifact envelope 可继续 v2，
executable Program 是 news_semantic_program_v4/factory_v4，learning epoch 才是 program_v6。
不得再用一个“v6”同时指代三者：

```text
factory_v4
EventSemantics.v2 + TradeRelevance.v1
normalizer/relevance invariants
Program v6 learning epoch
policy v10
review v4
metric v4
compiler input/receipt protocol v3（含 metric_judge）
exact Program v6 code-owned baseline Artifact
```

`priority -> queue_priority` 直接 hard rename，无 alias。新 binary 不包含 factory v3/v5 executor，
不做 v3/v4 union loader，不把 old `editorial=NULL` 行送入 policy v10。

可以让 PR 分开 review，但在部署前保持行为关闭；不要部署“新 Gate + 旧 Program/旧 policy”或
“新 Program + 旧 metric”的混合状态。policy v10 的 pre-deploy 证据用 frozen pure replay 产生，
不需要 runtime 双 policy。

### Phase 2：原子切换到 code-owned Program v6 baseline

- 部署一个 exact image，其中 runtime 只有 Program v6 + policy v10。
- v5 Program/旧 evidence 立即成为 audit-only；新 epoch 的 eligibility 从零重新积累。
- v2/v3 reviews 可以在历史 UI/审计查询中读取，但只有新 epoch 下人工接受的 v4 gold 可进入
  metric v4、GEPA、DemoBank 和 release evidence。
- 需要 v5/v6 对照时，用封存 v5 exact image 对 sealed cases 离线执行，或使用 recorded v5
  predictions；不要把 v5 factory塞回新 binary。
- 物理列 hard rename 后旧 exact image 不再 DB-compatible；发布前须封存并演练一个独立的
  new-schema/v5-behaviour rollback image。它与新 stable 分离，新 binary 仍不提供旧 factory 或旧
  执行语义。若拒绝该 rollback artifact，则只能声明 forward-fix，不能声称旧 exact-image rollback。

### Phase 3：重新积累 v6 证据，再运行 GEPA

- 先发布 v6 `recorded` / `compile_live` / `runtime_live` baseline，三者各自命名执行边界。
- 等 v4 accepted gold 的每个 required stratum 在 train 与 dev-selection 都达标后再 compile。
- 第一次 candidate 只允许两个 LearnedStrategy instruction 变化，DemoBank保持空。
- 同一 sealed policy v10、metric v4、judge identity 比较 code-owned baseline 与 candidate。
- future holdout、shadow、one-arm canary、人工 promotion 继续执行；任一 hard bucket 失败即拒绝。

- canary selector 使用新 identity，不再排除 queue high 目标层；profile 任一变化都须关闭/重开
  activation，不能跨 image 静默继承。

该顺序牺牲旧 cohort 的“连续可训练性”，但符合用户选择：遇到代际契约冲突，宁可 reset epoch
重新积累，也不让兼容代码和旧 truth 污染新目标。

## 建议直接写回 #160 的条款

以下均应成为 Acceptance 或实施条款，而不是聊天里的注意事项：

### P0 / blocking

1. **Compiler entry-to-receipt proof**：修复 32k/16k、reflection bundle misbinding、judge omission；
   一次 hermetic test 证明非空 judge identity、task/reflection/judge 三类调用与成本、patch/receipt
   全链一致。
2. **Same ruler proof**：同一 sealed predictions 在 `compile_live` 与 hermetic compiler metric v4 下
   score/per-dimension outcome 完全一致；judge cache 不改变值。
3. **Dependency freeze**：本 Issue 固定 DSPy 3.3.0/gepa 0.1.1；升级另立 hard-cut Issue。
4. **Single executable generation**：新 binary 只执行 factory v4/Program v6/policy v10；无 alias、
   无双 factory、旧 epoch audit-only。
5. **One production-action ruler**：failure-cluster selection、metric 和 baseline 共用 frozen
   `decide()`；priority-rescued false interrupt 必须进入 optimizer failure scope。
6. **Atomic judgment reuse**：stale re-ask 的成功/失败恢复只缓存完整
   `SemanticJudgment(verdict+editorial)`，trace/hash/persist 不允许 sibling 混配。

- **Exact runtime attribution**：每条 verdict/trace 绑定 runtime manifest SHA；旧窗口只标 inferred，
  新积累窗口才可作为 Phase 0 exact evidence。
- **Canary identity**：startup/resume/assignment 校验 selector version、eligibility profile SHA 与
  rolling profile SHA；任何漂移立即 trip。
- **Public contract decision**：删除“HTTP 不变”与“无 alias”的矛盾；明确 reader feed 的 priority
  字段/filter/sort/badge 是删除还是破坏性改名，并与 React/OpenAPI 同版发布。
- **Rollback truth**：明确 hard DB rename 后的 rollback artifact；不得同时承诺无 schema alias 和
  旧 image 直接可运行。

### P1 / contract

7. 补齐 `commodity_supply`（并解决中国降准/`usd_liquidity` 定义）；集合 max-4、唯一、canonical
   ordering 由 code validator/normalizer 强制。
8. 从 model-visible input 删除/明确裁决 `queue_lag_s`；重新决定 watchlist 是 policy guard 还是
   account-specific semantic input。
9. 写清 ReaderCard 获得哪一部分 normalized relevance；该 projection 进入 signature/artifact hash。
10. 新 RulePack 使用合法 `rule_id`/`revision`，解决现有 pack 上限 8。
11. deterministic/degraded editorial provenance 类型闭合；不伪造 model relevance。
12. metric v4 保持同 scalar/分 Predictor feedback、typed exact relevance、free-text-only judge、
    fail-before-spend 与新 calibration fixture。
13. `expected_correction` 不再跨 Predictor 广播；relevance feedback typed 且只归 EventSemantics，
    ReaderCard 不承担 relevance 修复。

### P2 / evidence and runtime

14. Program 正常物理调用 2 次；metric judge、reflection 调用分开计数，不能让“2 calls”掩盖学习
    成本。
15. token/cost `<=10%` 用 provider receipts 实测；nested EventSemantics output 和 ReaderCard input
    均纳入，不做静态猜测。
16. v5/v6 对照只能通过 recorded/封存 exact image 离线完成，不在新 runtime 引入兼容 factory。
17. 只有 v6 epoch 下 accepted review v4 可用于 GEPA/release；旧 review/evidence 仅历史可读。

## 本次只读验证

- 用当前 CompilerProviderEndpointSecret 构造 reflection max_tokens=32000，稳定复现 Pydantic
  less_than_equal ValidationError；错误发生在 launcher/provider 之前。
- 运行 judge、baseline、compiler、真实 GEPA 与 recording replay 的 focused suite：
  66 passed in 2.56s。
- 这 66 项全绿并不反驳 P0，反而证明现有测试没有覆盖真实
  CLI -> typed secret/bundle -> launcher -> runner -> non-null judge receipt 的组合路径。
- 未调用生产 provider、未读取真实凭据、未修改业务代码或数据库。

## 最终评估

| 维度 | 结论 |
|---|---|
| 产品问题 | 成立。queue scheduling hint 被复用成 reader urgency，是可从代码证明的 authority leak |
| DSPy task shape | 成立。typed nested relevance + fixed two-stage Module + exact metric + rich feedback 是合适形状 |
| 是否需要 Agent/第三 Predictor | 不需要；还会破坏当前 trace attribution 与两调用契约 |
| GEPA 是否能补 schema | 不能。普通 non-Flex GEPA 只替换 instructions，schema/enum/validator 必须先由代码定稿 |
| GEPA 是否会填 DemoBank | 当前 3.3.0 路径不会；本轮必须保持 empty，demo optimizer 另立实验 |
| 当前 hermetic compiler | **不可用且 ruler 未接通**：启动 token validation、bundle binding、judge wiring 三处 P0 |
| Artifact/治理 | Tracefold 的 exact state-only hard cut比 DSPy native save/load严格且必要，但应如实标为项目约束 |
| 当前 #160 是否可原样实施 | 否。完成 P0 与 contract 修订后可实施 |
| 推荐发布方式 | 单一 Program v6/policy v10 原子 hard cut；旧代历史可读、禁止兼容执行；epoch 从零重积累 |

## 一手来源清单

以下来源均于 **2026-08-23** 访问。

### Tracefold Issue / PR

- [Issue #117 — News taxonomy v2](https://github.com/AnalyThothAI/tracefold/issues/117)
- [Issue #129 — DSPy program-native News Triage hard cut](https://github.com/AnalyThothAI/tracefold/issues/129)
- [PR #130 — hard-cut triage to DSPy programs](https://github.com/AnalyThothAI/tracefold/pull/130)
- [PR #133 — DSPy News quality baseline v3](https://github.com/AnalyThothAI/tracefold/pull/133)
- [Issue #134 — D-generation hard cut](https://github.com/AnalyThothAI/tracefold/issues/134)
- [PR #135 — D-generation hard cut implementation](https://github.com/AnalyThothAI/tracefold/pull/135)
- [PR #136 — policy v8 recall-first decide](https://github.com/AnalyThothAI/tracefold/pull/136)
- [Issue #138 — ToldContext / production-action metric / honest split](https://github.com/AnalyThothAI/tracefold/issues/138)
- [PR #139 — told instrument fix](https://github.com/AnalyThothAI/tracefold/pull/139)
- [PR #141 — candidate-conditioned ToldContext + input partition](https://github.com/AnalyThothAI/tracefold/pull/141)
- [PR #142 — production-action metric + honest split](https://github.com/AnalyThothAI/tracefold/pull/142)
- [Issue #143 — baseline + real GEPA](https://github.com/AnalyThothAI/tracefold/issues/143)
- [PR #145 — baseline / structured gold / real GEPA](https://github.com/AnalyThothAI/tracefold/pull/145)
- [Issue #148 — semantic equivalence + v3 gold drafts](https://github.com/AnalyThothAI/tracefold/issues/148)
- [PR #151 — CardEquivalenceJudge + review drafter](https://github.com/AnalyThothAI/tracefold/pull/151)
- [Issue #150 — decision-grade baseline v2](https://github.com/AnalyThothAI/tracefold/issues/150)
- [PR #155 — digest context / macro pollution / schedule split fixes](https://github.com/AnalyThothAI/tracefold/pull/155)
- [PR #157 — source artifact identity](https://github.com/AnalyThothAI/tracefold/pull/157)
- [PR #158 — three baseline modes + frozen policy + v2 report](https://github.com/AnalyThothAI/tracefold/pull/158)
- [PR #159 — fail-before-spend + corpus identity + metric v3](https://github.com/AnalyThothAI/tracefold/pull/159)
- [Issue #160 — TradeRelevance hard cut](https://github.com/AnalyThothAI/tracefold/issues/160)

### DSPy 官方文档

- [Signatures in Depth](https://dspy.ai/diving-deeper/signatures-in-depth/)
- [Modules](https://dspy.ai/diving-deeper/modules/)
- [Metrics and Evaluation](https://dspy.ai/diving-deeper/metrics-and-evaluation/)
- [Choosing an Optimizer](https://dspy.ai/diving-deeper/choosing-an-optimizer/)
- [GEPA in Depth](https://dspy.ai/diving-deeper/gepa-in-depth/)
- [GEPA API](https://dspy.ai/api/optimizers/GEPA/overview/)
- [GEPA Advanced](https://dspy.ai/api/optimizers/GEPA/GEPA_Advanced/)
- [JSONAdapter API](https://dspy.ai/api/adapters/JSONAdapter/)
- [Cache](https://dspy.ai/tutorials/cache/)
- [Saving and Loading](https://dspy.ai/diving-deeper/saving-and-loading/)
- [MIPROv2](https://dspy.ai/api/optimizers/MIPROv2/)
- [BootstrapFewShot](https://dspy.ai/api/optimizers/BootstrapFewShot/)

### DSPy 官方 release / 3.3.0 固定源码

- [DSPy 3.3.0 release](https://github.com/stanfordnlp/dspy/releases/tag/3.3.0)
- [DSPy 3.3.1 release](https://github.com/stanfordnlp/dspy/releases/tag/3.3.1)
- [`Signature` source, tag 3.3.0](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/signatures/signature.py)
- [`Module` / state save-load source, tag 3.3.0](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/primitives/base_module.py)
- [`Predict` source, tag 3.3.0](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/predict/predict.py)
- [`JSONAdapter` source, tag 3.3.0](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/adapters/json_adapter.py)
- [`ChatAdapter` source, tag 3.3.0](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/adapters/chat_adapter.py)
- [`LM` defaults source, tag 3.3.0](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/clients/lm.py)
- [`Evaluate` source, tag 3.3.0](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/evaluate/evaluate.py)
- [`GEPA` source, tag 3.3.0](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/teleprompt/gepa/gepa.py)
- [`GEPA` program builder source, tag 3.3.0](https://github.com/stanfordnlp/dspy/blob/3.3.0/dspy/teleprompt/gepa/gepa_utils.py)
