# Tracefold News 生产学习闭环：从“会跑”到“会被验证地变好”

> 面向不需要先懂机器学习的读者。本文把生产链路、历史数据怎么用、两条漏推怎么学习、复盘页面怎么改、上线后会发生什么讲清楚。
>
> 结论先行：生产 Agent 不应该偷偷修改自己。真正的“学习”是：保存当时试卷 → 人工按多维标准判卷 → 找到第一处错误 → 只改一个变量 → 用旧题和未来新题对照考试 → 影子运行 → 小范围上线 → 能回滚。通过这条证据链的新版本，才成为下一版 stable。

> **当前落地状态（2026-08-21）**：Issue #112 的代码骨架已实现到迁移
> 0288，并已通过后端、前端、类型和架构测试；生产数据库仍停在 0283，未做迁移，
> 也尚无人工 accepted reviews、未来时间 holdout、24 小时 shadow 或 10% canary
> 证据。因此现在可以说“机制已经做出来”，不能说“Agent 已经学会或线上质量已提升”。
> 本文同时是上线说明和第一轮真实实验的操作手册。
>
> **policy v7 决策**：删除所有每小时、2 小时和 4 小时读者数量额度。Agent/语义 policy 判定满足 push/escalate 后就进入投递；热路径只保留明确 pause/mute、幂等、发送节奏和同事实重复证据。reader load 继续作为评测 guardrail 展示，但不再是运行时第二编辑。
>
> Issue #112 的逐项完成度、仍缺的真实生产证据和受控 rollout 见
> [`news-learning-loop-issue-112-completion-audit-2026-08-21.md`](news-learning-loop-issue-112-completion-audit-2026-08-21.md)。
> policy v7 的固定 24 小时流量反事实与代表案例见
> [`news-policy-v7-no-quota-replay-2026-08-21.md`](news-policy-v7-no-quota-replay-2026-08-21.md)。

## 1. 一句话架构判断

Tracefold 现在的在线管道并不玩具：接收、去重、Gate、一次结构化 Triage、确定性 policy、投递、幂等和审计都已经有生产骨架。policy v7 已删除数量限流，避免语义 Agent 之后再出现一个按计数否决的“第二编辑”。玩具化的是旧学习面：系统能解释“当时做了什么”，却不能可靠证明“候选 Prompt 比旧 Prompt 好”。

目标不是增加更多在线 Agent，而是补齐三个缺口：

1. **正确的试卷**：一个 Event 对应一个原子事实，保存模型当时真正看到的不可变证据；
2. **正确的判卷**：operator 在页面直接提交多维 rubric，不用价格涨跌代替人的价值判断；
3. **正确的升版考试**：通用逐案例执行交给成熟 eval 工具，Tracefold 只补它独有的顺序 reader ledger、真实送达、holdout 和发布控制。

```text
生产稳定链路
原始 Item
  -> 原子事实 FactUnit
  -> EventEvidenceSnapshot(v)
  -> 一次 SemanticJudge
  -> deterministic DecisionPolicy
  -> Delivery
  -> ReaderReceipt(sent truth)

离线学习链路
Evidence + Receipt
  -> ReviewDesk 判卷
  -> first_bad_owner / failure cluster
  -> 单变量 candidate
  -> stable vs candidate 同卷考试
  -> 两臂各自顺序 reader replay
  -> 未来时间 holdout
  -> shadow
  -> bounded canary / rollback
  -> 人工批准成为新 stable
```

## 2. 用学校考试理解完整生产闭环

把每条新闻想成一张试卷。

### 2.1 白天：稳定版只负责答题，不自我修改

1. **拆题**：如果 provider 一条消息里有十个编号 bullet，先拆成十个 `FactUnit`。每个 FactUnit 都保留 parent Item 和原文 span，不能丢来源。
2. **存卷**：用 `EventEvidenceSnapshot` 保存“模型当时看到的题目”。后来的更强 member 只能产生 `evidence_version=2`，不能把旧试卷偷偷改掉。
3. **语义作答**：一次模型调用只回答“这是什么”：事实类型、读者价值、主体/资产、方向机制、量级、新颖性、中文 headline/why、支持它的 source fact IDs。
4. **校规决定动作**：确定性 policy 根据语义字段、reader budget、重复和控制状态决定 drop/push/escalate。模型不再和 policy 各自拥有一套最终动作。
5. **快递回执**：只有 Feishu 真正 `sent` 才写入 `ReaderReceipt`。pending 是占位，不等于读者收到。
6. **当天不学习**：生产 stable Prompt、policy 和 rubric 都不可由在线模型修改。

### 2.2 晚上：人和程序一起判卷

系统从当日流量中抽样，而不是只挑涨跌最大的新闻：

- 已推送：按版本、storyline、来源、读者负载分层抽样；
- model drop、Gate suppress、throttled：分别抽样；
- delivery fail/ambiguous、operator 主动报告的 eventless miss：全量；
- 高市场反应 held：只作为 discovery queue；
- 普通 held：随机 control，防止只看“事后显眼”的样本。

Reviewer 在看价格之前先回答：

```text
should_push          应不应该打扰读者
factual_fidelity     卡片是否忠实于证据
asset_grounding      主体/资产是否绑对
direction            方向与机制是否正确
magnitude            影响量级是否正确
novelty              是新事实、进展还是复述
headline_quality     标题是否准确、完整
why_value            why 是否有价值且有证据
first_bad_owner      第一处可修错误属于谁
```

每个 reviewer 的判断 append-only 保存；纠正通过 `supersedes_review_id`，不能用“最新一个人覆盖所有历史”。有分歧就进入仲裁，不把 `uncertain` 硬折成 pass/fail。

### 2.3 错题先找责任人，再决定改什么

| 第一处错误 | 应沉淀到哪里 | 例子 |
|---|---|---|
| 原始事实没拆对 | deterministic program + regression test | 一个 digest Event 的标题讲商务部，卡片却选了 description 里的 Moderna |
| 字符/正则/限流规则错 | deterministic program | `STRAITS` 被 `strait` 命中中东；Guyana 的 oil 被归中东 |
| 产品“什么值得推”没说清 | versioned reader contract + rubric | 私营 AI 模型发布是否属于重点 |
| 模型对语义边界理解错 | Prompt rubric + boundary cases | 行业价格基本面新数据被降为 m1 |
| 需要长期、可审来源知识 | versioned knowledge source/retrieval | 某监管制度的正式定义和生效条件 |
| Coding Agent 不会执行维护流程 | Codex Skill | 怎样冻结数据集、运行评测、生成 release evidence |
| 文案格式、enum、引用 ID 错 | schema/code validator | `restates` 引用了输入里不存在的 prior-card ID |

**Skill 不是线上 Triage 的记忆。** 当前生产模型不会加载 Codex Skill；把 DRAM 或 GLM 写进 Skill 不会改变线上行为。Skill 适合规范维护 Agent 如何工作，产品边界应在 reader contract/rubric，语义规则应在 Prompt，确定性错误应在代码。

### 2.4 什么时候才生成 Prompt candidate

一条投诉先成为 boundary case，不立刻写成永久例外。出现多个独立事实簇、或一个 must-push critical case 后，才形成 failure cluster，例如：

```text
cluster: sector_fundamental_update
共同错误: 有明确供需/定价新数据，却因为 scheduled/preliminary 被自动降为 m1
目标维度: magnitude + should_push
不可退化: factual_fidelity、reader volume、duplicate、latency
```

候选一次只改一个 owner：Prompt candidate 固定 model/schema/retrieval/policy；policy candidate 固定 Prompt/model/schema。Prompt+Policy 同时改，就无法知道收益来自哪里。

### 2.5 候选怎么考试

考试分五关：

1. **开发集**：已知错题用来生成候选；它只能证明候选“会做练过的题”。
2. **保留集/安全集**：旧的 must-push、must-hold、事实忠实、注入与资产边界不能退化。
3. **未来时间 holdout**：候选注册后才收集的新事实簇，用于证明泛化；不能一边看答案一边改卷。
4. **顺序 replay**：stable 和 candidate 各自按时间跑完整链路。前一张是否发送会改变下一张的 told ledger、storyline count 和 hourly budget，所以两臂不能共用同一个历史 ledger。
5. **shadow/canary**：shadow 读真实输入但不发卡；canary 只证明运行安全、延迟、成本和回滚，不代替已经完成的盲审质量评价。

任何 required stratum 为空、模型供应商故障、预算耗尽或 reviewer 分歧过大，都应该得到 `UNKNOWN`，不能因为“没有反例”而自动 PASS。

### 2.6 什么时候算“学会了”

只有这些证据都在，candidate 才能变成 stable：

- candidate manifest 和唯一改动可追溯；
- 目标维度在未见过的 holdout 改善；
- must-push/must-hold/safety 无回归；
- 事实错误、错资产、重复、reader load 没越界；
- schema、p95 latency、成本、degraded rate 没越界；
- shadow 正常；
- bounded canary 没触发 runtime guardrail；
- 人工批准，保存 previous stable hash 和 rollback receipt。

这就是 Chapter 9 所说持续进化在生产中的含义。它不是“模型写了一段反思”，而是一个被验证、可回滚的版本变化。

## 3. 两条重点漏推到底错在哪里

两条 Event 都通过了 Gate，没有被节流，也没有投递失败。模型主动写 drop，policy 走 `below_threshold`。所以修 Feishu、RabbitMQ 或小时上限都不会解决它们。

### 3.1 DRAM：这是 Prompt/模型语义判断错误

Event：`0fefa7b402187b3247d3d162f71910b7b0e08b98bbb55d0c078f15a0049d32fc`

模型当时真正看到的是：韩国 8 月 1–20 日 DRAM 初步出口单价继续上涨，member_count=1、provider score=75。它输出：

```text
new_fact / bullish / sector / magnitude=1
actionable=false / model_decision=drop
```

`why_zh` 还写了“已被前期预期覆盖”，但原始输入和 told cards 都没有这个证据。这是 unsupported dismissal。

11 分 37 秒后更强 member（score=80）加入 Event；当前代码会更新 Event facts，却不会为已经判断过的 candidate 生成新 evidence version 或 rejudge。现在详情/`news why` 显示 `member_count=2/score=80`，但 verdict 实际基于 `1/75`。这证明 latest mutable Event 不能代替当时试卷。

应该沉淀：

```text
boundary: sector_fundamental_update
规则: 新的行业定价/供需数据覆盖多家可交易公司时，不能只因 preliminary/scheduled 自动降为 m1
证据约束: “priced in / 已预期”必须能引用 source fact 或 told evidence
```

不应该沉淀：“看到 DRAM 就推”。关键词例外会让 Prompt 越来越脆。

### 3.2 GLM-5.3：这是产品合同冲突，不是模型没听话

Event：`da2e8ef353563ee6e011e9764ffb0e6a104d647f94ff7501520cc4c66c72135d`

模型看到了完整发布信息：GLM-5.3 的 benchmark、排名、价格、参数量、coding/cyber 能力。它正确发现 Z.ai 的 GLM 模型不是 GLM crypto token，所以输出 assets=[]、actionable=false、drop。

这完全符合当前 Prompt：“无直接 listed stock/token 就不 actionable；只推 actionable”。如果 operator 明确认为它是重点，意味着现有 reader contract 太窄。建议把合同版本升级为：

```text
strategic_technology_frontier:
即使没有直接 ticker，只要公开的新模型在能力、推理成本、开放权重/许可或产业供给曲线上
形成可验证的明显台阶，并能影响公开市场的竞争/资本开支判断，就可进入 sector-context review。
```

这是产品范围变化，必须重建 baseline，不能伪装成普通 Prompt 修辞优化。

还有一个独立错误：Gate 把模型名 `GLM` 绑定到了同名 Golem token。Reaction 虽标记 `is_primary=false`，事件级汇总会排除，但详情页没有显示“非主标的”，仍展示 GLMUSDT 的 +36bps，极易让人误以为模型发布带动了币价。应在详情页隐藏非主 reaction，或明确显示“同名候选，未用于评价”。

### 3.3 反事实验证

在两条各自的历史 reader ledger 上，只把 semantic verdict 改成 `magnitude=2/actionable=true/model_decision=push` 重跑现有 policy，两条都会走 `model_push_actionable`，不会被重复或小时 cap 拦截。

所以正确 owner 是：

| Event | 第一处错误 | 不需要改 |
|---|---|---|
| DRAM | Prompt/semantic rubric + evidence version | transport、Deliverer、全局阈值 |
| GLM-5.3 | reader contract；另有 entity resolution/UI bug | 用 Skill 记 GLM、调小时 cap |

## 4. 最近 24 小时应该怎样读

固定审计窗口使用 Event `opened_at_ms` 半开区间；完整 SQL 与快照见：

- [`news-review-24h-audit-2026-08-21.sql`](news-review-24h-audit-2026-08-21.sql)
- [`news-review-24h-audit-snapshot-2026-08-21.json`](news-review-24h-audit-snapshot-2026-08-21.json)
- [`news-learning-loop-audit-2026-08-21.ipynb`](news-learning-loop-audit-2026-08-21.ipynb)

### 4.1 能确定的事实

- 约 1,600 个 live Events，约 412–419 张最终 sent，取决于固定窗口截止时刻；
- 约 17 张/小时，滚动 1h 峰值 30、4h 峰值 100；相邻卡中位 112 秒，32.6% 不超过 60 秒；
- 整窗混合 v4/v8、v5/v8、v6/v8、v6/v9，不能把总体结果归因给当前 Agent；
- 全库人工 labels=0、eventless misses=0，真实 precision/recall 不可识别；
- v6 的字面近重复代理约 2.6%，低于 v5 的 11.0%，但不是随机 A/B；总 reader load 仍然过高；
- 412 张全量机械检查没有空 headline/why、pushed noise/restatement 或 degraded；有 6 张 `actionable=false` 仍发送，其中 1 张 model drop 被 magnitude 路径升级。

### 4.2 不能诚实声称的结论

没有 gold labels 时，不能把 412 张硬分成“多少百分比高质量”。保守人工审计只能说：

- 10 张高置信好卡样例；
- 至少 22 张高置信产品缺陷；
- 其余约 380 张需要按 rubric 人工裁决，不代表它们差。

高置信好卡共同特点：事实与来源对齐、是明确新事实、读者能立即采取行动。例如交易所下架、安全事件/桥暂停、链停机、官方监管里程碑、治理决议。

高置信缺陷主要来自：

1. **多事实 Item 身份错位 3 张**：Event 标题、Gate 资产和最终读者卡不是同一事实；
2. **OKX 批次轰炸至少 18 张额外卡**：6 分 51 秒连续发送 19 张“某股票代币出现在 OKX”，产品上应是一张批次摘要；
3. **方向/why 内部矛盾 1 张**：headline/direction 看多，why 却写形成卖压。

另有 22 张命中透明因果文案风险规则，例如把解押直接写成“即将卖出”、把新增交易入口写成“新增买方”、把相关性写成“直接支撑价格”。它们是 review queue，不是自动定罪。

### 4.3 多事实 Item 为什么比 Prompt 更优先

已发现三个明确例子：

| Event 前台身份 | 最终卡片选择的另一 bullet |
|---|---|
| 商务部反对欧方打压中国企业 | 美股/Moderna/沃尔玛下跌 |
| 花旗或承销 Anthropic IPO | 博通洽谈 600 亿美元 AI 芯片融资 |
| 美国管道淡化霍尔木兹重要性 | 对伊朗最严厉经济行动 |

模型没有凭空编造；这些事实都在同一个 description 的其它编号 bullet。问题是“题目”本身装了多道题：Deduper/Gate 用 A 建 Event，模型却选择 B 写卡。此时 event_id、storyline、资产、label、价格 reaction 全部可能贴错对象。

如果先建设精密 evaluator，却不先原子化，系统会非常认真地给错误试卷打分。

### 4.4 为什么 1h 涨跌不能评新闻质量

冻结窗口中，修正 maturity 后：

| cohort | 1h priced / mature eligible | raw sign alignment |
|---|---:|---:|
| delivered | 253 / 402 = 62.9% | 121 / 241 = 50.2% |
| held | 247 / 1,109 = 22.3% | 93 / 185 = 50.3% |

两组几乎一样，但这既不能证明 Agent 无用，也不能证明它有用，因为：

- 只有一部分 Event 能映射到价格，coverage 强烈不同；
- raw return 没减市场/行业基线；
- 市场可能提前定价；
- 同时发生的宏观事件会共同驱动价格；
- “应该让读者知道”不等于“一小时方向必须正确”；
- GLM 例子甚至是同名错误资产。

正确叫法是 `MarketReaction` 或“事后市场观察”。它可以帮助人找到值得看的 case，但不能自动写 `should_push` 或作为 Prompt optimizer reward。

### 4.5 你现有的 Claude「377 张卡片」复盘 Agent 属于哪一层

我实际打开并阅读了这份 [Claude Artifact：377 张卡片](https://claude.ai/code/artifact/beb7b810-e0c6-4591-8759-11a65759dc20?via=auto_preview)。它是一份很有价值的 **discovery audit**，但还不是 Chapter 9 意义上的学习闭环。

它做得好的地方：

- 固定了时间窗、Git/Prompt/Policy/Model 版本，并逐条阅读真实送达卡；
- 从 reader load 和跨卡重复，而非 1h 涨跌，发现 G1/G2 等真实结构问题；
- 用现有 `decide()` 做顺序反事实重放，保留二阶 reader-ledger 影响；
- 没有因为第一次假设合理就停下：它保留了 G2 单改无收益、magnitude 豁免会回弹重复等负结果；
- 最终明确指出生产人工标注为 0，旧发布门只能验证 frozen verdict 下的 policy 自洽性。

它不能证明的地方：

- 窗口混合 policy v3/v4/v5，且是 Prompt v8；报告里的总体比例不能代表当前 v6/v9 Agent；
- 104→23 一类指标来自字符相似度启发式，不是人工确认的独立事实 gold；
- frozen verdict replay 只改变 policy/storyline，不能证明一个新 Prompt 会生成更好的语义和文案；
- 只看系统形成的 Event，无法发现 receiver/Gate 之前的 eventless miss；
- 它没有 candidate 注册后的 future holdout、blind comparison、shadow/canary 或 rollback receipt；
- 因此“架构没问题”应收窄为“当时在线可靠性和单次 Triage 形态合理”；证据契约与学习架构仍需补齐。

新闭环不会取代这种深度审计，而是把它变成可积累的生产输入：G1/G2 进入 deterministic regression；人工确认的 18 个重复与 15 个被挤压事实进入 boundary/retention case；每个新结论绑定 evidence/version/owner；下次候选必须在独立 holdout 上过关。也就是说，复盘 Agent 负责**提出有证据的假设**，ReviewDesk/CandidateEvaluator 负责**让假设接受不能作弊的考试**。

## 5. KISS 的 build-vs-buy 边界

成熟开源项目能替我们做通用工作，但不能替我们定义 News 产品真相。

### 5.1 建议采用的组合

```text
Promptfoo（先做一日 spike，成功才包装）
  - stable/candidate 执行
  - provider/model matrix
  - assertions、pairwise/LLM judge 接口
  - 本地/CI 报告

Tracefold domain adapter（项目必须自有）
  - EventEvidenceSnapshot / FactUnit
  - ReviewJudgment / reader contract
  - fact-cluster 与 temporal holdout
  - 真实 ReaderReceipt
  - arm-specific sequential ledger replay
  - release/rollback receipt
```

OpenAI 官方已经把旧 Evals/Prompt Optimizer 产品面列入弃用，并给出迁移到 Promptfoo 的官方指南；因此不应把新架构押在已进入 sunset 的托管 Evals 上。这个推荐针对 eval execution，不承诺 Promptfoo 等价替换历史审阅、全部 grader、工具和发布治理。

Promptfoo 不是先验必选。用一天 synthetic/fixture spike 验证：它能否通过 custom/HTTP provider 调用 Tracefold **同一个** `SemanticJudge` contract，保留所有版本/成本/错误 receipt，而且不接管跨 Event 顺序。如果必须在 JavaScript 重写一份领域编排，就停止引入，保留 Python 原生 executor。

### 5.2 V1 不建议引入的东西

- **Langfuse/Phoenix/MLflow 作为第二套在线数据平台**：它们的 tracing、datasets、experiments 很成熟，但当前 Tracefold 已有 PostgreSQL trace 和自己的 operator console。再加一个服务会形成第二 truth/storage/权限面。等出现第二个 LLM 产品或多人共享实验平台需求再评估。
- **DSPy/GEPA 自动优化后直接发布**：它们适合在可信 evaluator 建好后离线搜索 candidate，不提供业务 gold、holdout、sent receipt 或上线授权。
- **多 Agent reviewer 热路径**：增加延迟、成本和新的失败 owner，不能替代人工校准。
- **向量化“经验库”**：当前首要缺口是 rubric 和正确 evidence，不是召回更多自我反思。

### 5.3 对 Issue #112 的 KISS 修订建议

现有 #112 的方向正确，但第一版应缩小：

1. `CandidateEvaluator` 保持领域编排与可信根；通用 assertion/CI 展示通过可替换 adapter 调 Promptfoo。Promptfoo spike 失败时，仍用同一 Python interface，不影响领域设计。
2. `ReviewDesk` 保留，因为它承载 Tracefold 独有的 evidence、reader contract、blind judgment 和 eventless miss；不做通用 annotation platform。
3. P0 前置 `FactUnit + EventEvidenceSnapshot + ReaderReceipt truth`；没有正确试卷，先不做 Prompt gate。
4. V1 用 Git/CLI 人工 promotion 和现有部署 rollback；自动 canary control plane 在第二个真实候选证明需要后再建。
5. V1 只支持 Prompt/Policy 单变量；model/retrieval/program candidate 另开 profile，不提前造插件系统。
6. Langfuse/Phoenix/MLflow、DSPy/GEPA 全部列为后续可选，不作为上线依赖。

详细库审计另见 [`agent-continuous-learning-build-vs-buy-2026-08-21.md`](agent-continuous-learning-build-vs-buy-2026-08-21.md)。

## 6. 复盘页面 hard cut

审计时的旧 `/news/review` 把 `HIT 1H` 放在 hero，虽然页面写了“非因果”免责声明，视觉上仍会教 operator 把价格当成绩；详情页反馈按钮主要复制 CLI 命令，全库 label=0 证明交互没有形成习惯。Issue #112 已在代码中 hard cut 为 ReviewDesk；生产迁移和真实使用仍待验证。

### 6.1 新信息架构

```text
/news/review
  待判队列       默认页，完成一次判断再看结果
  证据覆盖       rubric N、strata、分歧、未判区域
  候选对比       stable/candidate 盲测、gate、holdout、shadow
  市场观察       独立 secondary view，永不生成 label
```

### 6.2 待判队列

一屏只完成一件事：判一张卷。

```text
左：当时的 frozen evidence
    source、opened_at、FactUnit、member refs、grounded candidates

中：当时系统结果
    semantic fields、card、policy rule、真实 sent/held
    默认隐藏 1h/4h price 和 stable/candidate 身份

右：rubric
    should_push / fidelity / assets / direction / magnitude / novelty / copy
    first_bad_owner + 证据说明
    [提交并下一条]
```

提交直接写 review role，返回 receipt 和下一 task；纠正、并发冲突和 unavailable 都在 UI 明示。不再让人复制 shell 命令。

### 6.3 证据覆盖

显示的是“我们知道多少”，不是一个漂亮百分比：

- 每个版本/来源/storyline/decision stratum 的已判 N、未判 N；
- must-push/must-hold/boundary/retention/safety 是否为空；
- reviewer agreement 与 adjudication backlog；
- 空数据明确写“证据不足”，不显示 100%。

### 6.4 候选对比

- development 和 hidden holdout 分开；
- 逐维 stable/candidate paired 结果；
- must-push 回归、factual/asset safety、reader volume、duplicate、tokens/latency/cost；
- sequential replay 的两臂 reader load；
- `PASS / REJECT / UNKNOWN` 与原因；
- 只允许查看和提交 blind preference，不能在页面直接 promote。

### 6.5 市场观察

- 页面改名，不出现“准确率/命中”主语；
- 先显示 coverage、maturity、cohort 和样本 N；
- potential miss 按 `fact_cluster` 聚合，不让同一 WMT 事实占四行；
- 显示 raw return 与可用时的市场/行业 baseline，但仍写“非因果”；
- 非主资产 reaction 默认隐藏；展开时清楚标记“不用于事件评价”；
- operator 判断前不展示 outcome，避免 hindsight bias。

## 7. 上线影响推演

### 7.1 会立即改善什么

| 变化 | 预期影响 |
|---|---|
| FactUnit 原子化 | Event、Gate、storyline、label 和卡片重新指向同一个事实 |
| EvidenceSnapshot | 能还原模型当时真正看到什么；stronger member 可产生显式 rejudge |
| sent receipt truth | novelty/限流不再把 pending 当成读者收到 |
| 页面直接多维判卷 | feedback adoption 从 0 开始变成可运营指标 |
| Promptfoo + domain adapter | 少造通用 runner，能真实比较 Prompt 行为 |
| hidden holdout | 防止用同一批错题既出题又证明自己优秀 |
| MarketReaction 降级 | 避免把随机波动和错实体价格写回 Prompt |

### 7.2 会付出什么成本

- **模型成本**：stronger evidence rejudge、stable/candidate replay、shadow 会增加冷路径调用；线上 stable 仍一次调用。必须有 per-run budget 和 incomplete/UNKNOWN。
- **存储**：EvidenceSnapshot、review judgments、candidate outputs 会增长；它们是可审计学习的必要成本，按内容寻址和 retention 管理。
- **变更速度**：Prompt 不再当天凭感觉上线；至少等待未来 holdout。速度下降换来可证明和可回滚。
- **Event 数可能上升**：digest 被拆成多个 FactUnit 后候选数增加。reader budget 仍由 policy 控制；显式 bullet atomization 可先只处理高置信编号列表，并设每 Item 上限。
- **人力**：每天需要小规模分层 review。KISS 目标不是人工看完 400 张，而是 100% 廉价 code checks + 有设计的 20–40 张人工样本 + 全量 critical misses/failures。

### 7.3 可能出现的新风险与保护

| 风险 | 保护 |
|---|---|
| 原子化丢失上下文 | FactUnit 保留 parent ref 和必要 context；低置信不拆 |
| reviewer 被价格结果影响 | 判断前隐藏 MarketReaction；提交后才 reveal |
| 自动 judge 复制模型偏见 | 先在人审集逐维校准；不能单独批准 candidate |
| 候选为了 recall 轰炸读者 | sequential reader replay + delivered-volume/peak guardrail |
| “修 DRAM”破坏其它行业新闻 | 独立 retention + future holdout + 单变量 diff |
| GLM 合同扩大后科技新闻泛滥 | 明确 strategic frontier rubric、随机 negative samples、reader volume ceiling |
| Promptfoo 成为新业务 truth | 只把它当执行器；domain evidence/judgment/release receipt 留在 Tracefold |

### 7.4 分阶段上线

**Phase 0：先修试卷，暂不自称学习闭环**

- 多事实显式 bullet 原子化；
- EventEvidenceSnapshot/evidence_version；
- reservation 与 sent ledger 分离；
- GLM 同名资产、reaction primary 显示；
- 已知 regression tests。

**Phase 1：能判卷**

- Review v2 schema + write role；
- 待判队列和直接 submit；
- 价格先隐藏、提交后 reveal；
- 分层抽样与 coverage 页面；
- DRAM、GLM、三条 identity mismatch、OKX batch 进入 boundary/retention cases。

**Phase 2：能证明 Prompt candidate**

- Promptfoo 条件 adapter（或同 interface 的 Python executor）；
- Tracefold dataset exporter/domain adapter；
- stable/candidate 同输入；
- current policy fast path + arm-specific sequential replay；
- temporal holdout；
- `PASS/REJECT/UNKNOWN`。

**Phase 3：能安全发布**

- 24h shadow；
- 人工批准的低风险 canary；
- runtime guardrail 和 rollback receipt；
- 第一个真实负结果也必须保留，不能只展示胜利案例。

## 8. 第一个真实学习实验建议

不要同时修所有问题。第一个实验建议选 DRAM 类 `sector_fundamental_update`，因为 owner 清晰、当前 policy 在正确语义下已经能工作。

```text
假设:
  Prompt rubric 要求 sector pricing/supply-demand new data 至少进入 m2 review，
  并禁止无证据的 priced-in dismissal，能减少 must-push miss，
  且不增加普通 scheduled/noise 的发送。

开发集:
  DRAM + 已知相似错误簇

保留/负例:
  例行无增量统计、纯价格播报、无机制的行业传闻

未来 holdout:
  candidate 注册后 24h/至少 200 Events，按独立 fact cluster 计

主指标:
  blind human should-push + magnitude paired preference

硬 guardrail:
  factual/asset regression=0
  must-push regression=0
  reader volume/peak 不劣于阈值
  duplicate、schema、latency、cost 不越界
```

GLM-5.3 不应和它混在同一个 Prompt 实验中。GLM 先升级并确认 reader contract，再重建 baseline；否则无法区分“模型变好”还是“产品目标变了”。

## 9. 最终验收问题

模块上线后，系统必须能直接回答：

1. 这张卡的模型当时看到哪一版证据？
2. 它错在哪个 rubric 维度，第一处 owner 是谁？
3. 这个 candidate 只改了什么？
4. stable/candidate 在同一证据下有何字段级差异？
5. 在候选没见过的未来事实簇上是否仍改善？
6. 前序推送改变后，后续重复、限流和 reader load 怎样变化？
7. shadow/canary 证明的是质量、运行安全，还是两者中的哪一个？
8. 数据 N、coverage、cohort、版本、hash、reviewer 分歧和 UNKNOWN 在哪里？
9. 当前 stable 从哪份 release evidence 晋升，怎样回滚？

答不出来时，产品应诚实叫“复盘观察”或“候选实验”，不能叫“Agent 已学习”。

## 10. 证据入口

- 总架构审计：[`news-review-architecture-audit-2026-08-21.md`](news-review-architecture-audit-2026-08-21.md)
- Chapter 9 证据：[`news-review-chapter9-evidence.md`](news-review-chapter9-evidence.md)
- build-vs-buy：[`agent-continuous-learning-build-vs-buy-2026-08-21.md`](agent-continuous-learning-build-vs-buy-2026-08-21.md)
- 固定 24h SQL：[`news-review-24h-audit-2026-08-21.sql`](news-review-24h-audit-2026-08-21.sql)
- 聚合快照：[`news-review-24h-audit-snapshot-2026-08-21.json`](news-review-24h-audit-snapshot-2026-08-21.json)
- 可重复复算：[`news-learning-loop-audit-2026-08-21.ipynb`](news-learning-loop-audit-2026-08-21.ipynb)
- 架构 Issue：[#112](https://github.com/AnalyThothAI/tracefold/issues/112)
- Chapter 9：[Agent 的持续进化](https://bojieli.github.io/ai-agent-book/book/chapter9/)
- OpenAI：[Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)
- OpenAI：[Moving from OpenAI Evals to Promptfoo](https://developers.openai.com/cookbook/examples/evaluation/moving-from-openai-evals-to-promptfoo)
- Google SRE：[Canarying Releases](https://sre.google/workbook/canarying-releases/)
