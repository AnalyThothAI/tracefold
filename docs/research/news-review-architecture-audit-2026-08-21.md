# Tracefold News 复盘链路架构审计（2026-08-21）

> 结论先行：Tracefold 的在线执行面已经具备生产级骨架；目前“玩具化”的是学习面，而不是消息管道。系统能稳定地接收、判断、限流、投递和记录，却不能可靠回答“当前 Agent 是否比上一版更好、错在 Gate/Prompt/Policy/Delivery 的哪一层、一个候选是否应该发布”。
>
> 本报告基于代码与 Git 历史、`ai-agent-book` Chapter 9 及本地实验、生产库固定 24 小时窗口的只读审计。未修改配置、数据库、标签或线上流量。

## 1. 最终判断

我不同意“整个系统都是玩具”，但同意“当前所谓复盘还没有成为持续进化系统”。更准确的分层判断是：

| 平面 | 判断 | 证据 |
|---|---|---|
| 在线执行 | **成熟骨架** | 单次 structured Triage、纯 `decide()`、幂等事实/判定/投递、broker retry/DLQ、deadline、degraded/circuit breaker、advisory lock、prompt/input/status/ledger trace |
| 可观测性 | **较成熟** | 24h funnel、规则理由、`news why`、reaction coverage、版本 hash、decision/delivery trace、延迟和队列健康 |
| 价格复盘 | **合理的观察面，但不是评价器** | 只说明事件后的 raw return；不能证明因果、内容价值或“该不该推” |
| 人工反馈 | **设施已实现，生产 adoption 为 0** | 全库 operator labels = 0，eventless miss = 0；详情页主要让人复制 CLI 命令 |
| 离线评测 | **断裂** | 单标签混合多个维度；未标注样本不约束候选；eventless miss 进不了 corpus |
| 发布门 | **Policy replay 有成熟骨架，release evidence 不完整** | 现有 sequential gate 冻结 verdict，只能重跑 `decide()`；当前 gold labels 为空，也不能评价模型、Prompt、schema、文案或事件证据变化 |
| 持续进化 | **尚不存在** | 没有 evidence → rubric → candidate → holdout → shadow → canary → rollback 的闭环 |

最终关键缺口不是一个“反思 Agent”，也不是继续往约 168 行（15,191 bytes）的 Prompt 里追加规则，而是一个与 policy gate 同等级的 **Prompt/Agent Candidate Evaluator**。依赖顺序上仍应先修 delivery truth 与 evidence version，避免 evaluator 在错误事实之上变得精密。

## 2. 审计范围、窗口与置信边界

### 2.1 固定实盘窗口

- 台北时间：`2026-08-20 10:30:09.757` 至 `2026-08-21 10:30:09.757`
- UTC：`2026-08-20 02:30:09.757Z` 至 `2026-08-21 02:30:09.757Z`
- SQL 半开区间：`news_events.opened_at_ms >= 1787193009757 AND news_events.opened_at_ms < 1787279409757`
- 配置入口已先验证为 operator-owned `~/.tracefold/config.yaml`；未输出 secret。

所有 24h Event/cohort/判定数字都按 **Event `opened_at_ms` 落窗**；419 表示这些 Events 最终查询到 `sent`，不是按 `settled_at_ms` 落窗的 delivery 流量。延迟用同一 Event 的 opened/create/verdict/settled 时间差，reader-load 则按实际 `settled_at_ms` 排列已发送卡。由于同一窗口混合多个 policy/prompt cohort，不能把总数解释成“当前 v9 Agent 的效果”。

### 2.2 证据快照与版本

审计开始时 repo HEAD 为 `4593ba6c12d0be2c67a2a758452030697a4fdd29`；交付前工作区已由外部推进到 `dedd869f93502bdad4bb219100e56dbb57568f11`。中间提交只改 Price Review 报价节奏/backlog telemetry、测试与文档，没有改 Triage Prompt/Policy、ledger、storyline、labels 或 eval harness；相关结论已在新 HEAD 上复核。

- PostgreSQL migration：`20260820_0283`（health=ready）；
- 当前 Prompt/Policy：`news_triage_prompt_v9` / `news_triage_policy_v6`；
- Prompt SHA-256：`71c42e60c40b2033c20a2f8f068ef70f5f93718e7fe87575afe55e778b9b48ed`；
- Schema SHA-256：`714f1f524bc2c1f51d0107b8e69e2b6996bac4485d3b4b71e6d8fdd283f31f7b`；
- 固定输出快照：[`news-review-24h-audit-snapshot-2026-08-21.json`](news-review-24h-audit-snapshot-2026-08-21.json)，SHA-256 `08eb5c2ec7e602cf2878cbb0300370405ed2aa2ded9da6db0f690c3dda43bf4f`；
- 只读 SQL：[`news-review-24h-audit-2026-08-21.sql`](news-review-24h-audit-2026-08-21.sql)，SHA-256 `8343607442ac720c02bd9bf0d8a8d502e6129c1abb88fb21f4bed332eb34a0b6`。

SQL 覆盖 funnel、admission、cohort、audience、storyline、delivery truth 与 1h maturity。94 个 near-pairs 与 42 个 uncovered-text proxies 的 event grain、3-gram normalization、containment 阈值和 4h window 也固化在 SQL 注释与 JSON manifest 中。

### 2.3 置信边界

- **高置信**：运行状态、表内计数、版本混合、标签为零、SQL/规则实现缺陷、storyline 错分实例。
- **中等置信**：宏观内容偏重、reader load 偏高、在可定价子集上看不到 selection lift。这些受输入分布、版本混合与价格覆盖偏差影响。
- **不可判定**：真实 precision、recall、false-push rate、miss rate，以及“新闻是否为价格变化原因”。没有人工 gold labels 时不能计算。

## 3. 现在的实际链路：三个“复盘”彼此没有闭环

```text
OpenNews frame
  -> news_items
  -> Deduper / Event / Gate
  -> one Triage model call
  -> structured verdict
  -> deterministic decide()
  -> delivery

旁路 A：所有 live Event-assets -> 5m candles -> news_event_reactions -> 命中复盘 UI
旁路 B：operator 单标签 -> news eval 离线统计
旁路 C：冻结 verdict/trace -> sequential policy replay -> validate-candidate
```

三个旁路各有价值，但没有共同的证据合同：

1. Price Review 不写 label，也不区分 prompt/policy/model cohort；
2. operator label 把相关性、重复、方向、时效、漏推压成一个互斥枚举；
3. release gate 冻结 Triage verdict，只能评价 policy mapping；
4. Gate suppress 和“根本没有 Event”的 miss 不进入 prompt/policy corpus；
5. 没有 candidate prompt 的真实模型重跑、temporal holdout、shadow 或 canary ledger。

因此当前链路是“可观测、可回放”，不是“可学习、可发布”。Chapter 9 的核心也不是让 Agent 自己总结，而是把原始证据、评价、候选、独立验证与发布隔离。[指定章节：Agent 的持续进化](https://bojieli.github.io/ai-agent-book/book/chapter9/)

## 4. 最近 24 小时：系统健康，产品判定不可证明

### 4.1 漏斗与可靠性

| 指标 | 结果 | 解读 |
|---|---:|---|
| Events | 1,628 | live 1,608；recovery 20 |
| 进入 Triage | 1,572 | candidate 1,551；deterministic listing 21 |
| push | 391 | 最终判定 |
| escalate | 28 | 高重要性推送 |
| drop | 1,065 | 语义/政策丢弃 |
| throttled | 88 | reader budget/重复类限制 |
| 应交付且 sent | 419 / 419 | 本窗口无 terminal delivery |
| operator labels | 0 | precision/recall 无法计算 |

完整性审计未发现重复主键、孤儿、leader/member mismatch 或 admitted-without-verdict。队列、retry 与 DLQ 均为空。延迟也健康：

| 阶段 | p50 | p95 |
|---|---:|---:|
| Event 创建 | 1.31 s | 6.48 s |
| Triage end-to-end | 1.42 s | 10.29 s |
| Delivery end-to-end | 6.27 s | 24.29 s |

这说明主要矛盾不是 RabbitMQ、PostgreSQL 或 worker 可靠性，而是“推什么、为何推、如何评价”。

### 4.2 版本污染：24h 总览不能代表当前 Agent

同一窗口包含：

| Policy / Prompt cohort | Events |
|---|---:|
| v4 / v8 | 220 |
| v5 / v8 | 702 |
| v6 / v8 | 34 |
| v6 / v9 | 616 |

当前 v6/v9 只覆盖约 10.5 小时。现有 Review API 只有 `hours` 参数，没有按 prompt SHA、policy version、model snapshot 分层。任何“过去 24h 命中率提升/下降”的结论都被 rollout 混合污染。

成熟做法：所有评价最小 grain 必须是

```text
(evidence_version, prompt_sha, schema_sha, model_snapshot,
 inference_config, policy_version, reaction_metric_version)
```

聚合时可以向上汇总，但不能在底层丢失 cohort identity。

### 4.3 当前重点与 reader budget 偏差

419 张卡的 audience 分布：

| audience | 推送 | 占比 |
|---|---:|---:|
| crypto | 174 | 41.5% |
| macro | 147 | 35.1% |
| us_equity | 98 | 23.4% |

相较前一个 24h 窗口，总推送由 348 增至 419（+20%）；macro Events 只由 447 增至 475（+6%），macro 推送却由 100 增至 147（+47%）。这是强描述性信号，但因为版本和输入分布同时变化，不能把原因直接归给 v9 Prompt。

reader load 已经很高：

- 平均 17.5 张/小时；峰值 27 张/小时；
- 相邻推送间隔中位数约 111 秒；
- 约 32% 的相邻卡间隔不超过 60 秒；
- `theme:mideast_energy` 与 `macro:general` 两个桶占全部推送 29.1%，前四个桶占 44.2%。

这里暴露的是产品合同问题：Prompt 把 `us_equity` 定义成“any listed equity”，而产品读者、watchlist 和全球股票范围没有被清晰钉死；空 watchlist 下，韩国、香港等个股也会被标为 `us_equity`，并占用全局 hourly/storyline reader budget。系统没有 audience 专属 budget。先定义“谁是读者、什么范围值得打扰”，再调措辞。

### 4.4 分类本体已经不能支撑可信复盘

当前 `event_type` 缺少 geopolitics/war、price_move、ETF_flow、buyback/capex 等常见类型，导致：

- `regulation` 的 165 个 Events / 50 个 pushes 中大量实际为宏观或中东事件，约 46% 的 regulation pushes 被污染；
- `whale` 同时容纳 BTC 过价、Hormuz 船流、铜挤仓和股票回购；
- `funding` 混入 ETF 流、股东回报、资本开支与债务。

因此目前按 `event_type` 做“哪个类型表现最好/最差”会得出伪结论。taxonomy 是评价坐标系，不只是 UI 标签；坐标系错了，后续 prompt optimizer 会优化错误目标。

### 4.5 已确认的 storyline bug 会直接造成错误限流

[`storyline.py`](../../src/tracefold/news/storyline.py#L23-L27) 的 `strait` 缺少词界；`oil` 已有词界，但“任何 oil 都属于中东”的 lexicon 又过宽。最终 key 还让 theme 优先于未被 provider grounded 的 model primary：

- `aacbcb37ae6320ea6624dbce2fc695b04cddb642a6069c47c7ab08d9ef176849`：`STRAITS: Crypto surge ... $2.7b liquidations` 因来源前缀 `STRAITS` 命中 `strait`，被分进中东桶后触发该 storyline 的 flood ceiling；
- `901deb2eef01030c6447eb60e089b182368f06ae92d9895c98b952abc20dcd61`：Exxon Guyana FPSO 仅因含 `oil` 被分进中东桶，随后也被该错误 storyline 的 hard ceiling throttled。

这不是 Prompt 能修的错误，owner 是 deterministic storyline classifier。应先修程序和 regression case，而不是给 Prompt 增加“不要误判 STRAITS/Guyana”的例外。

### 4.6 重复与漏推只能叫 proxy

独立 3-gram containment 在 419 张 delivered cards 中找到 94 对近似文本，但 18 张“X appears on OKX”的不同 ticker 模板制造了大量假阳性；84 对还跨 storyline。当前 v9 仅 4 对，其中约 2 对是真复述。

明确的真实逃逸是 `985ef2cabf460829390a6aa23f62d1ce9b8dc513ad6b28915bd91a17f4810f36` 与 `b320575a35fe88050d624e5ee8712062efa48bf62c0ece39076deaba82b9fef3` 两张“贝森特推动伊朗政权更迭/最大经济孤立”卡，后者 magnitude=3 后 escalate。准确地说，escalate 不进入 v6 `similarity_all_pushes`；只有自身 storyline throttle 已触发时才仍会走 v5 similarity。这留下了一条跨 storyline 的重复通道。[`triage_rules.py`](../../src/tracefold/news/triage_rules.py#L273-L281)

88 张 throttled 卡中，有 42 张在前后四小时找不到 containment ≥ 0.35 的 delivered neighbor；这只能叫“独立事实未覆盖代理”，不能叫 42 个真实漏推。代表例子包括上述两个 storyline 错分、`6109d461999c3cd29c17c553725b9c587432e429668073ec5be4a85ba968921e`（Moderna 盘前 -13%）的 hourly cap，以及 Panama Canal 容量/吃水事实。

## 5. 价格复盘为什么不能当 Agent reward

### 5.1 现有结果

修正 horizon maturity 后：

| cohort | 1h priced / mature eligible | directional hit |
|---|---:|---:|
| delivered | 253 / 402 = 62.9%；directional N=241 | 121 / 241 = 50.2% |
| held | 247 / 1,109 = 22.3%；directional N=185 | 93 / 185 = 50.3% |
| 全部成熟 Events | 500 / 1,511 = 33.1% coverage | — |

1h 绝对波动均值/中位数：delivered 84.3/41 bps，held 82.5/46 bps。在这个低覆盖、强选择偏差的可定价子集上，看不到明显 selection lift。

这不等于“Agent 无价值”，因为：

1. raw sign 不是新闻因果效应；
2. 宏观/地缘新闻常无直接 ticker；
3. 市场可能提前定价或被共同事件主导；
4. “读者应该知道”不等于一小时方向正确；
5. 当前样本混合四个 Agent cohort。

经典事件研究至少需要相对市场/行业基线的 abnormal return，并处理共同消息和事件聚集；即便如此也仍是 reviewer evidence，不是自动 gold label。[MacKinlay, *Event Studies in Economics and Finance*](https://www.bu.edu/econ/files/2011/01/MacKinlay-1996-Event-Studies-in-Economics-and-Finance.pdf)

### 5.2 已确认的 Review 实现问题

1. [`price_repository.py`](../../src/tracefold/news/price_repository.py) 的 eligible 分母没有排除尚未成熟到 1h/4h 的事件；实测 `hours=1` 会返回 60 eligible、0 priced，正确 eligible 应为 0。
2. Review topbar 聚合所有 triaged Events，不是 delivered cohort，所以展示的 hit rate 不是“推送准确率”。
3. potential miss 按单 Event 的 `abs(bps_1h)` 排序；榜首四条实际上是同一个 WMT -692 bps 事实，Event-grain 夸大 miss。
4. 没有 cohort、置信区间、最小 N、市场基线、共同事件标记或 coverage-bias 提示。
5. Price resolver 只按 symbol 解析、没有使用 Event asset class，且当前 `news_event_assets.market_type` 写入为空；股票/币同名理论上可能污染 reaction。审计未证明该风险在本窗口实际发生，因此它是实现 caveat，不是本窗口已观测错误。[`price_repository.py`](../../src/tracefold/news/price_repository.py#L105-L150)

因此建议把中文“命中复盘”改成更诚实的“市场反应观察”，并将 potential misses 改为 `fact_cluster` grain 的人工队列。

## 6. 代码级 P0 断点

### P0-1：reader ledger 不是 delivery truth

[`repository.py`](../../src/tracefold/news/repository.py#L578-L608) 的 told ledger 会把 final decision 为 push/escalate、且 delivery state 不是 terminal 的行纳入。没有 delivery row 或仍 pending 时也会被视为“读者已收到”；相反，真实 sent 的 degraded card 会被排除。

后果：一个 pending 卡可能先污染 restatement/throttle，后来 terminal 后也不触发后续卡重判；真实 degraded 卡又不参与语义去重和 reader load。

KISS 修法：拆成两个概念。

```text
DeliveryReservationLedger：短时防止并发重复，pending/ambiguous 有 TTL
ReaderReceivedLedger：只有 delivery.state = sent 的 durable truth
```

Triage/复盘必须明确自己读取哪一个，不能用一个含混 SQL 同时表示“正在占位”和“读者看过”。

### P0-2：Event evidence 在首次 leader 上过早冻结

`event_card()` 只读取 leader item。后来的 stronger member 能升级 Gate facts/assets 并让 suppressed Event 进入 Triage，却不会替换 leader 文本或 provider metadata；已经 judged 的 Event 加强后也不会产生新的判断。[`repository.py`](../../src/tracefold/news/repository.py#L530-L541)；[`events.py`](../../src/tracefold/news/events.py#L260-L316)

这会出现“更强证据使事件入场，但模型仍只看到旧弱标题”的不一致。需要不可变的：

```text
EventEvidenceSnapshot(event_id, evidence_version, leader/member refs,
                      normalized facts, grounded assets, created_at)
```

只有 evidence version 变化才允许显式 rejudge；stable/candidate replay 必须读取同一 snapshot。

### P0-3：人工 miss 的 recall 证据进不了 release gate

CLI 支持 `--subject` 记录无 Event 的 missed case，但 offline eval 从 `news_events` 开始，freeze corpus 从 `news_verdicts` 开始。pipeline 根本没建 Event 的漏召回只能显示一个计数，不能成为 regression case。

应让 dataset item 可以是 `event_id` 或 `external_miss_id`，并保存最小 source evidence。只要 must-push/eventless-miss 集为空，release gate 就应返回 **UNKNOWN/INSUFFICIENT_EVIDENCE**，而不是 vacuous PASS。

### P0-4：label 维度和交互都不支持学习

当前 mutually-exclusive enum 同时混合 `good/noise/dup/wrong_direction/late/missed/must_push`。offline eval 又把 good、wrong_direction、late、missed、must_push 全折成内部 outcome `moved`，其真实语义是“event mattered / 应该推”，不是发生了价格走势。真正的问题是折叠后丢失了 `direction_correctness`、timeliness 与 miss 类型，无法归因具体能力。[`offline.py`](../../src/tracefold/news/eval/offline.py#L51-L62)

详情页多数场景只生成 CLI copy command；全库标签为 0 说明反馈入口实际没有进入操作习惯。先做可写 UI、review queue 和多维 rubric，再谈 prompt optimizer。

### P0-5：现有 release gate 不是 Agent/Prompt gate

[`eval/harness.py`](../../src/tracefold/news/eval/harness.py#L1-L19) 在模块注释中明确承认 verdict 已冻结，只测试 `decide()`。Prompt SHA pin 只能证明 bytes 没变，不能证明行为更好。当前 CLI 还没有把 trusted-root SHA 传入 `validate_candidate()`，默认的 trusted-root 检查基本没有实际约束。[`commands/news.py`](../../src/tracefold/app/cli/commands/news.py#L305-L344)

## 7. Prompt/Agent 的具体问题

### 7.1 一个 Prompt 同时承担太多 owner

当前一次调用同时处理：topic/value filter、asset grounding、event type、direction、magnitude、actionable、model decision、audience、中文 headline/why/title、novelty/restates、禁词与 injection。随后 `decide()` 又重新解释 magnitude/actionable/decision。

结果是两个政策源：模型在做一次“该不该推”，程序又做一次；`magnitude=3` 还可能覆盖 actionable/model decision 并直接 escalate。

### 7.2 24h 已见的行为矛盾

- 存在 `push/escalate + actionable=false`；
- 存在 `noise + decision != drop`；
- 32 张推送仍包含 Prompt 禁止的“利好”等词；
- Moderna 仅有价格异动事实，`why_zh` 却猜测“通常由财报/指引不及预期触发”；
- 一些宏观卡把“可能影响”写成缺乏来源支撑的具体因果机制。

这些不是继续加自然语言禁令就能稳定解决。结构矛盾、禁词、长度和索引必须由代码 validator 管；事实支持需要 evidence references 和行为 eval。

### 7.3 novelty 输入与任务不匹配

Prompt 的 told ledger 上限为 12，v8/v9 基本每次饱和；按当前 ledger SQL 得到的 4h **完整候选集合**平均约 62–71 条、最大约 96–100 条，模型平均只看到约 17%，却被要求声明 new/progression/restatement。这里不能称为完整 reader-received truth，因为当前 SQL 本身会纳入 no-delivery/pending，并排除 sent degraded。[`repository.py`](../../src/tracefold/news/repository.py#L578-L608)

KISS 不是把全部 100 张塞进 Prompt，而是：

1. `ReaderReceivedLedger` 保留完整 sent truth；
2. 代码按 subject/entity/theme/time 检索 top-K **候选 prior cards**；
3. Prompt 只判断“当前事实是否复述这些候选”，不声称看过完整历史；
4. deterministic all-push guard 在完整 ledger 上继续做廉价保护；
5. evaluator 报告 retrieval recall 与 semantic duplicate precision，不能只报一个相似度数。

## 8. KISS 的目标架构：稳定在线、离线进化

```text
ONLINE — 只有一个语义 Agent

Items -> EventEvidenceSnapshot(v)
      -> SemanticJudge (one structured call)
      -> SemanticVerdict
      -> deterministic DecisionPolicy
      -> DeliveryIntent
      -> sent/terminal truth

OFFLINE — 一个 batch workflow，不是多 Agent 热路径

EvaluationBundle
  -> stratified review queue
  -> code verifiers + human rubric + calibrated judge
  -> failure cluster / first_bad_owner
  -> minimal candidate artifact
  -> stable vs candidate semantic replay
  -> arm-specific sequential reader replay
  -> temporal holdout
  -> shadow
  -> canary + automatic rollback
```

在线 Agent 永远不自改 Prompt；离线 proposal generator 最多生成 candidate artifact，不能修改 verifier、gold cases、阈值、trusted root 或 stable Prompt。

这与 Google SRE 的 canary 原则一致：候选先在受限流量/时间内与 control 比较，指标必须可归因且可回滚；异步数据流水线可先用真实输入 dry-run，禁止生产写入。[Google SRE：Canarying Releases](https://sre.google/workbook/canarying-releases/)；[Data Processing Pipelines](https://sre.google/workbook/data-processing/)

### 8.1 唯一深模块：`CandidateEvaluator`

不要新建一堆互相调用的 Reviewer/Reflection/PromptWriter 服务。建立一个隐藏存储、版本、true-external model port、record/replay adapter、重跑与统计复杂度的深模块：

```python
report = evaluator.evaluate(
    dataset_spec,
    stable=stable_manifest,
    candidate=candidate_manifest,
)
# -> EvalReport + ReleaseEvidence
```

每个 bundle 至少包含：

| 类别 | 字段 |
|---|---|
| identity | event/external-miss id、fact cluster、opened_at、evidence_version |
| exact input | evidence snapshot、selected prior cards、status、prompt input hash |
| execution | prompt/schema/model/inference/policy versions、tokens、latency、error/degraded |
| semantic output | assets、type、direction、magnitude、novelty、headline、why、support refs |
| policy output | final decision、rule、throttle key、arm-specific ledger before/after |
| delivery | reservation、sent/terminal truth、reader-received time |
| outcome evidence | reaction version、coverage、raw/abnormal return、共同事件标记 |
| review | rubric version、每位 reviewer、adjudication、uncertain/NA、first_bad_owner |

`CandidateEvaluator` 是深模块；`EvaluationBundle` 只是它隐藏并持久化的不可变 evidence carrier/value artifact，`EvalReport` 才做统计。不要把聚合指标反写成单事件 truth。

### 8.2 多维 label v2

建议不再使用互斥单标签，而是独立维度：

```json
{
  "rubric_version": "news_review_v2",
  "should_push": "pass",
  "factual_fidelity": "fail",
  "asset_grounding": "pass",
  "direction": "uncertain",
  "magnitude": "pass",
  "novelty": "fail",
  "timeliness": "not_applicable",
  "headline_quality": "fail",
  "why_value": "pass",
  "duplicate_of": "event-id-or-null",
  "first_bad_owner": "triage_prompt",
  "evidence": ["headline 删除了来源中的 25.8%"],
  "reviewer": "operator"
}
```

保留每位 reviewer 的原始判断，另建 adjudication；不要用“最近一个人覆盖所有人”。自动 judge 只有在各维度与人工集合校准后才扩量。OpenAI 官方 eval 指南建议使用任务特定、真实分布的数据，优先明确的分类/成对比较，并用人工校准自动评分；复杂 Agent 架构也应由 eval 证明必要性。[OpenAI Evaluation Best Practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)

### 8.3 分层抽样，而不是只看波动最大 miss

每天 discovery slice 至少包括。100% 范围表示廉价 code checks；人工 review 对 delivered、常规 held 与高反应队列做分层抽样，只有 delivery failures 和 eventless misses 才全量人工：

- delivered pushes/escalates 做 100% code checks，并按 cohort/storyline/reader-load 分层人工抽样；
- 按 Gate suppress reason 分层的样本；
- 按 model drop rule 分层的样本；
- 按 throttle key/rule 分层的样本；
- delivery fail/ambiguous 全量；
- eventless operator misses 全量；
- high-reaction held queue；
- 普通 held 的随机 control；
- prompt/policy/model cohort 分层。

发现集用于找问题，不用于批准自己生成的候选。候选生成后再冻结未来时间窗作 temporal holdout；避免用同一 24h 同时“发现规则”和“证明规则有效”。

## 9. Prompt 重构：先改职责，再改文字

### 9.1 先钉产品合同

在任何 v10 文案前，必须回答并版本化：

1. 目标读者究竟是 crypto + global macro + US equity，还是任何全球上市公司？
2. 空 watchlist 是“只推全局系统性事件”，还是“全市场开放”？
3. 每小时/每天 reader budget 和高优先级例外是什么？
4. magnitude 代表事实影响强度，还是 delivery urgency？两者不能混成一个枚举。
5. price_move、geopolitics、ETF_flow 等是否是一等 event type？

如果产品合同不清楚，Prompt evaluator 也只能更稳定地优化错误目标。

### 9.2 建议的单调用输出边界

模型只输出依赖语义的事实与文案，不输出最终 delivery action：

```text
relevance / reader_value
event_type
primary subjects + grounded assets
direction + mechanism
magnitude（稳定 ordinal rubric）
novelty + matched prior-card ids
headline_zh / why_zh / audience
supporting source-sentence ids
```

删除或降级：

- `model decision`：与 deterministic policy 重复，是双政策源；
- 自报 `confidence`：在校准前只作诊断，不进入门禁；
- 可由程序完全验证的格式/禁词/长度规则。

最终 drop/push/escalate 只由一个 deterministic policy 根据语义字段、reader budget 与控制状态产生。这样模型负责“这是什么”，程序负责“系统做什么”。

### 9.3 Prompt 仍保持一个请求，但内部按 owner 组合

最终仍是一个 byte-frozen system message，源码可组合为：

```text
1. Reader/Product Contract
2. Evidence & Injection Boundary
3. Semantic Rubric
4. Novelty Candidate Protocol
5. Chinese Copy Contract
6. Structured Output Contract
```

候选每次只改一个 section，并声明目标维度和不可退化维度。Prompt pin 保留，但旁边增加 behavioral eval；SHA 证明身份，eval 证明质量。

### 9.4 Schema 后 validator

第一版只做确定性检查：

- enum、required、长度、empty sentinel；
- banned exact phrase、URL、emoji、meta-language；
- `restates` 是否引用输入中的 prior-card id；
- `supporting_fact_ids` 是否存在；
- 明显矛盾组合；
- 来源中的关键数字被删除时生成 review flag。

可修复的格式问题做一次 bounded re-ask；不可证明的开放质量不得被 validator 冒充为 truth。

## 10. Prompt Candidate Gate

### 10.1 第一层：同输入 semantic replay

除声明的 target variable 外，stable/candidate 的一切输入都固定：Prompt candidate 固定 evidence、retrieval、model snapshot 与 inference config；model candidate 固定 evidence、retrieval、Prompt 与 inference config；retrieval candidate 则固定 evidence、Prompt 与 model。逐字段比较：

- schema validity；
- relevance/assets/type/direction/magnitude/novelty；
- factual/headline/why rubric；
- declared target 之外的 side effects；
- tokens、latency、degraded/error；
- 多次 trial 的一致性。

τ-bench 用最终环境状态和 `pass^k` 强调多次运行的一致性；单次“过了”不等于 Agent 可靠。[τ-bench](https://arxiv.org/abs/2406.12045)

### 10.2 第二层：arm-specific sequential replay

只要候选可能改变 headline、novelty、storyline 或 decision，前一张卡就会改变后一张卡的 reader ledger。两臂必须独立按时间顺序：

```text
build arm status -> model -> final storyline -> decide
-> update that arm's counterfactual would-reach-reader ledger -> next Event
```

不能用 current frozen-verdict policy replay 假装完成 Prompt A/B。离线 arm 没有真实 delivery receipt，`sent` 只能来自生产；counterfactual ledger 必须用同一 delivery-capacity policy 模拟，并在报告中明确标记为 simulated。

### 10.3 最小发布门

候选只有同时满足以下条件才能从 `REJECT` 进入 `SHADOW_READY`：

1. candidate manifest 非空、可追溯、只改声明 section；
2. boundary set 的目标维度明确改善；
3. must-push、good、safety retention 不回退；
4. 无新增事实失真、错误资产、关键方向错或 injection 服从；
5. noise、duplicate、reader volume、hourly peak 不越 guardrail；
6. schema、tokens、p95 latency、degraded rate 不越界；
7. temporal holdout 保持改善，所有比例报告 N 与区间；
8. must-push/eventless-miss 集为空时返回 UNKNOWN，而非 PASS。

Shadow 只读真实输入、禁止投递；通过后由人批准小时间/小流量 canary，任何绝对 guardrail 退化自动 rollback。

## 11. 实施顺序：按依赖关系，不按“智能感”

### PR 1 — 修 correctness foundation

- 拆 reservation ledger 与 sent ledger；
- 给 `strait` 加词界、让 `oil` 需要中东上下文，并加入已知 regression cases；
- 修 reaction eligibility 的 horizon denominator；
- 修 `first_push_delay_min_p50` 的定义；
- Review/状态按 cohort 分层。

### PR 2 — 建立 evidence 与 review contract

- `EventEvidenceSnapshot/evidence_version`；
- `news_review_v2` 多维 rubric；
- 可写 review UI + 分层 queue；
- eventless miss 可进入 dataset；
- 空 gold set 明确 UNKNOWN。

### PR 3 — Prompt semantic evaluator

- `EvaluationBundle` 与 immutable dataset manifest；
- stable/candidate 同输入重跑；
- code verifier + 盲化 human pairwise；
- exact section diff、model/inference/hash manifest；
- 修 trusted-root 实参。

### PR 4 — sequential replay、shadow、canary

- arm-specific reader ledger；
- temporal holdout；
- shadow 禁止生产写入；
- canary control、guardrail、自动 rollback。

### PR 5 — evaluator 稳定后才引入自动候选

LLM、DSPy 或 GEPA 只作为离线 candidate searcher，权限停在 immutable proposal。DSPy/GEPA 能帮助搜索 Prompt，但不能替代独立 verifier、holdout 与发布控制。[DSPy](https://arxiv.org/abs/2310.03714)；[GEPA](https://arxiv.org/abs/2507.19457)

## 12. 明确不做

- 不恢复 Analyst/reviewer 在线 lane；
- 不做“生产 Agent 每晚读日志后直接改 Prompt”；
- 不把 raw 1h/4h sign 写成 reward；
- 不把所有历史卡塞入 Prompt；
- 不用自报 confidence 自动开关门；
- 不在 labels=0 时做 fine-tune 或在线权重更新；
- 不允许 candidate generator 改 rubric、gold cases、阈值、trusted root 或 stable hash；
- 不把每次投诉追加成一条永久 Prompt 规则。

本地 Chapter 9 的小样本没有提供生产有效性证据：8 个样本仍有一个维度 recall=0；知识文档组 25%，两个 control 各 50%；prompt optimization 只有 5+5 个例且目标规则预先写进 Coding Agent。这些结果说明机制能运行，也诚实暴露负迁移可能；不能外推为生产收益。详细证据见配套研究稿 [`news-review-chapter9-evidence.md`](news-review-chapter9-evidence.md)。

## 13. 完成定义

当以下问题能由系统直接、可重复地回答，才能称为“持续进化闭环”：

1. 某张卡错在哪里，首个可操作 owner 是谁？
2. 同一 evidence/context 下，stable 与 candidate 每个语义字段有什么差异？
3. 候选目标维度是否在未见过的未来数据上改善？
4. 它是否牺牲 must-push、事实忠实、重复、reader load、延迟或成本？
5. 它改变前序推送后，后续 reader ledger 的系统级结果怎样变化？
6. shadow/canary 的 control 是什么，何时自动回滚？
7. 所有结论的 N、覆盖率、版本、数据 hash 和 reviewer 分歧是什么？

在此之前，正确的产品措辞是“复盘观察与候选验证”，不是“自主学习”。

## 14. 证据与复现入口

### 本地证据

- Chapter 9 深入证据：[`docs/research/news-review-chapter9-evidence.md`](news-review-chapter9-evidence.md)
- Prompt：`src/tracefold/news/agents/prompts/__init__.py`
- Triage 输入/ledger：`src/tracefold/news/agents/triage_model.py`
- 最终 policy：`src/tracefold/news/triage_rules.py`
- Event/ledger/labels：`src/tracefold/news/repository.py`
- Policy replay：`src/tracefold/news/eval/harness.py`
- Offline eval：`src/tracefold/news/eval/offline.py`
- Reaction Review：`src/tracefold/news/price_repository.py`
- Storyline：`src/tracefold/news/storyline.py`
- 本地书稿：`/Users/massis/Documents/Code/ai-agent-book/book/chapter9.md`
- 本地实验：`/Users/massis/Documents/Code/ai-agent-book/chapter9/`

### 一手资料

- [Agent 的持续进化（Chapter 9）](https://bojieli.github.io/ai-agent-book/book/chapter9/)
- [OpenAI Evaluation Best Practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)
- [Google SRE: Canarying Releases](https://sre.google/workbook/canarying-releases/)
- [Google SRE: Data Processing Pipelines](https://sre.google/workbook/data-processing/)
- [Reflexion](https://arxiv.org/abs/2303.11366)
- [τ-bench](https://arxiv.org/abs/2406.12045)
- [DSPy](https://arxiv.org/abs/2310.03714)
- [GEPA](https://arxiv.org/abs/2507.19457)
- [MacKinlay: Event Studies in Economics and Finance](https://www.bu.edu/econ/files/2011/01/MacKinlay-1996-Event-Studies-in-Economics-and-Finance.pdf)

### 运行健康复核（移动窗口）

```bash
uv run tracefold config
uv run tracefold db health
uv run tracefold db audit
uv run tracefold ops validate-projections
uv run tracefold news eval --hours 24
uv run tracefold news why <event_id>
```

这些 CLI 适合复核当前健康，不会重现已经冻结的 24h 窗口。固定数字应在 operator 的 Serve 只读连接上执行 [`news-review-24h-audit-2026-08-21.sql`](news-review-24h-audit-2026-08-21.sql)，并用快照 hash 验证交付物：

```bash
shasum -a 256 \
  docs/research/news-review-24h-audit-snapshot-2026-08-21.json \
  docs/research/news-review-24h-audit-2026-08-21.sql
```

查询使用 `DISTINCT ON (event_id) ... ORDER BY event_id, created_at_ms DESC` 固定 latest Triage verdict。不要把连接串或凭据写入复现脚本。
