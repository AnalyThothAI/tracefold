# 新闻分类：现网体检、成熟实践调研与 #117/#344 之后的路线（2026-08-29）

> 状态：研究稿，不是实现 PR；不改变任何在线行为、身份或 Issue 验收。
>
> 范围：News 分类（`event_type` → `news_taxonomy_v1`）的执行前基线、[#117](https://github.com/AnalyThothAI/tracefold/issues/117) 与 [#344](https://github.com/AnalyThothAI/tracefold/issues/344) 的终态形状、与 DSPy 官方 classification 教程的方法论对照、行业成熟实践调研，以及数据集与深度分析 Agent 的路线建议。建议只作为后续 Issue 的输入，本文不构成验收。
>
> 证据规则：现网数字来自 `tracefold_serve` 只读聚合，`.sql`/`.json` 快照对与本文同目录提交（[discovery](news-classification-baseline-discovery-2026-08-29.sql) · [snapshot](news-classification-baseline-snapshot-2026-08-29.json)，captured `2026-08-29T11:44:43Z`）；只提交计数、比率、身份前缀与窗口，不含任何新闻文本、卡片、Prompt 或凭据。外部事实只采用官方规范、官方文档与论文并附链接；标注「推断」的句子是本文观点。

## 结论先行

1. **#117 目前没有「执行后」。** 旧 #117 于 2026-08-24 被标记关闭，但没有 closure comment、关联 PR 或实现证据；2026-08-29 06:13 UTC 被重开、改题并整体替换为「先建 accepted Gold、多轴 shadow、受控 hard cut」的 P0 计划。现网仍在跑 17 类混轴 `event_type`（[signatures.py](../../src/tracefold/news/program/signatures.py) 的单选枚举）。本文第 1 节是它的执行前基线。
2. **现网分类的病不是「分错」，是「不可测」。** 7 天窗口有 10,060 条 triage 判定（本文快照）；同期口径的 488 个 accepted review 里 exact Gold 覆盖仅 12.7%、`must_push` 仅 3 例（#117 正文的 operator 聚合）；metric 明确把 `event_type` 排除在可评分维度外（[objective.py](../../src/tracefold/news/learning/objective.py) 的维度注释）。「分类对不对」至今没有任何直接测量。
3. **混轴枚举的代价可以量化。** `macro` 一个桶占 40% 流量；`product_progress` 通道 1,440 行中 41.1%（592 行）的 `event_type` 不是 `product`——平面单选标签表达不了「这同时是产品进展」，靠多标签 channel 才兜住。这判死了「用 `event_type` 做股票/产品路由」。
4. **运行可用性与分类质量是两回事，前者已经很好。** #314+#315 于 08-28 20:26 UTC 部署后：433 条判定、0 次备路接管、2 次降级（0.46%）。24h 口径里的 147 次备路全部落在部署前的旧运行时尾巴。
5. **#344 结束后，News 模型程序回到原生 DSPy**（`Module/Signature/Predict/LM/JSONAdapter/GEPA`），自研 transport/graph/GEPA adapter（约 80KB 框架代码）删除，Tracefold 只保留业务契约、RoutePolicy、审计与发布治理。当前方向已批、实施被 P0 闸挡住（`ready-for-human`），单 PR、单部署，且要 #117 的 GEPA spend waiver 才能合并。
6. **DSPy 官方 classification_finetuning 教程与我们不是同一道题。** 教程从「干净单轴 taxonomy + 标签即真值」起步做权重蒸馏（Banking77，1B student 86.7% 反超 teacher 55%）；我们卡在第 0 步——标签空间不成立。#117 的次序（先修 label space，再谈优化器）在方法论上是对的；教程真正可搬的是「小模型足以承载分类」与「Gold 攒够后 `BootstrapFinetune` 是 GEPA 平台期之后的下一根杠杆」。
7. **成熟实践（通讯社标准、金融分析供应商、事件编码体系）三线独立、结论收敛**：事件类目小而封闭且多轴正交（subject ≠ 体裁 ≠ 地理 ≠ 实体 ≠ 证据状态）；稳定不透明 ID + 版本化 codebook；「谁/在哪」用注册表词典而非类目枚举；商业供应商的产品形态统一为每条 (story, entity) 输出 who / what / how much（relevance）/ how new（novelty，对台账算）/ which way + how sure / what status。#117 的 `news_taxonomy_v1` 与此同构——拆轴是行业共识，不是过度设计（第 4–5 节，含来源）。
8. **「宏观下也有重点」（美联储、俄乌）不该也不需要进类目枚举。** #117 落地后 `macro_policy_data` 与 `geopolitical_conflict` 分家、`subject_codes` 承接货币政策等主题；但「哪家央行、哪场冲突」是 actor/geo 身份轴 + storyline 的事——成熟事件编码体系（CAMEO/PLOVER/ACLED）全部把「谁/在哪」做成字典而不是类。这是 v2 方向，不是 #117 的缺陷。
9. **数据集：需要，而且形式已经定了。** 不是下载外部数据集，而是 #117 Phase 1 的 accepted-review Gold + 版本化 codebook；现网已经免费攒好了第一批困难样本池（592 行通道错配、m1/m2 边界、confirmed↔rumor 翻转）。外部数据集只用于方法论校准，不作为我们契约的真值。
10. **深度分析 Agent 是 post-#117 的建设**（#117 白纸黑字先冻结它），但它的契约、语料圈选与 rubric 现在就能零身份风险地准备。方法论一句话：contract → Gold → shadow → gate → optimize；永远不在能测量之前优化，不在标签空间成立之前测量。

---

## 1. 执行前基线：17 类混轴枚举在现网的真实产出

### 1.1 分布与处置（7 天，快照 `event_type_by_decision_7d`）

窗口 `captured_at - 7d`，`stage='triage'`，10,060 条判定，送达（push+escalate）3,623 条（36.0%）：

| event_type | n | push | escalate | drop | throttled | delivered% |
|---|---|---|---|---|---|---|
| macro | 4,053 | 1,438 | 215 | 2,268 | 132 | 40.8 |
| noise | 1,819 | 17 | 0 | 1,802 | 0 | 0.9 |
| product | 1,362 | 649 | 0 | 663 | 50 | 47.7 |
| regulation | 801 | 305 | 20 | 431 | 45 | 40.6 |
| partnership | 432 | 203 | 0 | 202 | 27 | 47.0 |
| earnings | 343 | 165 | 1 | 153 | 24 | 48.4 |
| funding | 272 | 138 | 0 | 116 | 18 | 50.7 |
| filing | 193 | 81 | 0 | 94 | 18 | 42.0 |
| rumor | 174 | 14 | 0 | 157 | 3 | 8.0 |
| listing | 173 | 79 | 0 | 65 | 29 | 45.7 |
| whale | 126 | 90 | 0 | 32 | 4 | 71.4 |
| rates | 94 | 35 | 6 | 52 | 1 | 43.6 |
| hack | 71 | 44 | 2 | 17 | 8 | 64.8 |
| oi_spike | 67 | 64 | 0 | 3 | 0 | 95.5 |
| exploit | 42 | 30 | 0 | 11 | 1 | 71.4 |
| delisting | 21 | 12 | 0 | 2 | 7 | 57.1 |
| liquidation | 17 | 15 | 0 | 2 | 0 | 88.2 |

注意读法：`delivered%` 是 `decide()`（policy v10）的最终处置，不是分类质量——模型只提议字段，推/不推的政策权在确定性代码。这张表说明的是**枚举的结构问题**，不是某一类「分错了多少」（后者恰恰因为没有 Gold 而无法回答）。

### 1.2 四个可量化的病灶

**（a）主题轴吞掉事件轴。** `macro` 一个桶占 40.3% 流量，把真宏观数据、地缘冲突、央行表态、行情评论压进同一个值。旧 #117 已指出 geopolitics/war、price move、ETF flow、buyback 分别落进 regulation/whale/funding 等垃圾桶；重开正文据此裁定「继续扩一个混轴 enum」不是修法。

**（b）平面单选标签选不出产品事件人群。** 快照 `product_progress_channel_7d`：TradeRelevance channels 含 `product_progress` 的 1,440 行里，**592 行（41.1%）的 `event_type` 不是 `product`**（partnership 273、listing 133、funding 72、regulation 36、filing 26…）。这不等于 41% 分错——一条「交易所上线新产品线」既是 listing 也是产品进展——它证明的是**单选枚举丢失路由信息**：多标签的 channel 字段能表达的事实，单轴 `event_type` 表达不了。任何「基于 event_type 的股票/产品路由」都会漏掉这 41%。与 #117 正文引用的 operator 聚合（1,435 例、约 41%）相互印证。

**（c）处置结果与证据状态混进了分类。** `noise`（18% 流量，0.9% 送达）是处置、`rumor`（8.0% 送达）是断言状态、`whale` 是主体形态、`filing` 是内容形式——它们与 `earnings/product` 互斥并列。#117 的正交拆解（`event_family` / `change_state` / `source_authority` / `assertion_status`）正是把这六个轴还原成独立字段，`rumor` 回到 `assertion_status`，`noise` 从 taxonomy 里消失。

**（d）m1/m2 边界仍是产品事件的分水岭。** 快照 `product_magnitude_7d`：`product` 里 m1 471 条只送达 2 条（0.4%），m2 891 条送达 647 条（72.6%）。[#173](https://github.com/AnalyThothAI/tracefold/issues/173) 固定窗口审计里「产品候选约 55% 落 m1 的死区」如今 m1 占比 34.6%——`product_progress` 通道已在现网存在（#173 的通道决策已落地），但「官方、具体、已发生的产品状态变化默认 m2」的先验修正仍在 #173 未关。

### 1.3 Gold 缺口：为什么「分类对不对」至今无法回答

- metric 侧：[objective.py](../../src/tracefold/news/learning/objective.py) 的维度组只有 relevance/semantics（`asset_grounding/direction/magnitude`）/card/delivery；注释明确说把 `event_type`、`novelty`、`actionable` 发明成维度会产生「没有 reviewer 能标注的死条目」。
- 语料侧（#117 正文 2026-08-29 的 operator 聚合）：混合 7 天 488 个 accepted review / 420 个 fact cluster，exact Gold 覆盖 12.7%，`must_push` 只有 3 例；当前 learning epoch baseline 返回 `news_program_baseline_no_accepted_reviews_in_window`。
- 身份侧：快照 `program_identity_mix_7d` 显示 7 天窗口横跨 **18 个 `program_sha256`**（最大三个：`e54c8d69b960` 4,603 条、`8c6dcf5085ba` 1,246 条、`535a1dff0ad5` 648 条）。任何按窗口聚合的「分类质量率」都在把多个身份的输出混成一个数——#117 Phase 0 要求生成诊断时显式携带 mixed-cohort caveat 的原因。

### 1.4 运行健康度 ≠ 分类质量（快照 `health_*`）

24h 口径：1,081 条判定、43 降级、147 次备路接管。按小时拆开后事实清楚：**147 次备路全部落在 08-28 11:00–20:00 UTC**，错误码 `restatement_index_invalid`（94）与 `event_semantics_invalid`（49）正是 [#315](https://github.com/AnalyThothAI/tracefold/issues/315) 修复的主路违约类；[#314](https://github.com/AnalyThothAI/tracefold/issues/314)+#315 于 20:26 UTC 部署后（`health_post_315_deploy`）：**433 条判定、0 备路、2 降级（0.46%）**。降级里的 `provider_http_400`（30 次）同样是部署前 [#310](https://github.com/AnalyThothAI/tracefold/issues/310) 类 DeepSeek structured-output 尾巴。

结论（推断）：本地 Qwen 主路的**可用性**已不是瓶颈；瓶颈是分类的**可测性**。这也是第 3 节对照 DSPy 教程时「小模型不是短板」判断的现网依据。

---

## 2. 两个在途 Issue 的终态形状

### 2.1 #117：`news_taxonomy_v1` —— 先证明分类正确，再受控发布

重开后的 #117 是 News 模型质量工作的最高优先级，且带三条冻结：完成前**不启动深度分析 Agent**、不新增基于 `event_type` 的股票/产品路由、不花新的泛化 Prompt/GEPA 预算（这直接压后了 #245 的 v8 战役时钟，并成为 #344 合并的前置 waiver）。

目标契约把普通 `event_kind=news` 的分类拆为五个正交字段：`subject_codes`（≤3 个钉选 IPTC finance/product 节点）、`event_family`（13 值：financial_results / guidance_outlook / product_service_change / corporate_transaction / financing_capital_allocation / leadership_governance / regulatory_legal / security_operational_incident / market_access / market_flow_price / macro_policy_data / geopolitical_conflict / other）、`change_state`（announced…recalled/unknown）、`source_authority`（由代码从 provenance 计算，模型不得自报）、`assertion_status`（confirmed/claimed/rumor/conflicted/unknown）。`other/unknown` 是正式弃权。

四个 Phase 的里程碑：Phase 0 冻结与基线（消费者盘点、legacy 只读 baseline、可重复诊断）；Phase 1 发布 `news_review_v5`，五字段进入 exact expected，产出 `TaxonomyEvaluationReportV1`（confusion matrix、per-class P/R/F1、abstention 的 risk-coverage、语言/来源切片、reviewer agreement）——**`event_type` 正确性第一次被显式测量**；Phase 2 内容寻址的离线 shadow classifier（无生产权威，legacy 与 shadow 在同一 frozen Gold 上对比）；Phase 3 verified 字段并入现有 EventSemantics predictor（普通成功仍恰好两次物理调用，Gate/policy/ReaderCard 行为不变）；Phase 4 冻结语料、只改 EventSemantics instruction 的候选、**五个字段各自设 release gate**，走完 holdout/shadow/canary/人工 promotion。

数据就绪门槛（按独立 connected fact cluster 计）：boundary ≥30、retention ≥100、negative ≥50、`product_service_change` ≥30、`financial_results+guidance_outlook` ≥30，至少 3 个现有 release strata。

**执行后的「表现」因此是一组第一次存在的测量**，而不是一个感觉：每字段的混淆矩阵与 P/R/F1、弃权覆盖曲线、跨版本可比的 `taxonomy_version` 语义。终态声明也刻意收敛：taxonomy 只是「经验证的 News 事实投影」，不等于深度分析、投递价值或交易能力。

### 2.2 #344：Native DSPy hard cut —— 框架职责交还上游

现状：`architecture_direction APPROVED / afk_implementation BLOCKED`，P0 八项关闭前保持 `ready-for-human`；固定为**一个任务分支、一个实现 PR、一次合并、一次部署**，且 #117/#245 冻结解除有可机读 receipt 才能合并。

结束后（terminal `NATIVE_DSPY_NEWS_PROGRAM_ACTIVE`）：

- **删除**：`transport.py`（约 40.6KB 自研 HTTP/schema/provider 家族）、`graph.py`（约 39.6KB 自研执行器）或收敛为 routing、自研 GEPA adapter 与 direct `gepa==0.1.1` 依赖。模型调用、结构化输出、GEPA 兼容性此后由 DSPy/LiteLLM 上游维护（钉 `dspy==3.3.1`）。
- **保留**（业务承重）：primary/fallback RoutePolicy、20s deadline、3/60 breaker、错误分类、`AuditedConfiguredLM` 物理调用账本、endpoint capability 由 Tracefold 配置声明（`json_schema/json_object/prompt_json`——#310 的教训直接写进 P0-3）、Artifact V1 两条 instruction、全部 Gold/holdout/shadow/canary/manual promotion 治理。
- **显式行为迁移**：调用上限每 route ≤3 → ≤4、每 judgment ≤6 → ≤8（stock JSONAdapter 每个 Predictor 允许一次格式回退）；`novelty_defaulted`/extra-key/truncation/learning-retry 四项语义在切换前逐项裁决。
- **验收**：old/new 在同一冻结 Dataset 的 paired gate（metric ≥ old−0.01、mean tokens ≤ old×1.10、business normalize 差异零容忍），部署后 ≥2h 且 ≥200 判定 live acceptance（degraded ≤5%、备路承担 ≤10%、common success 恰好 2 次物理调用）。

对本文主题的意义（推断）：#344 之后，「分类」相关的一切改进（per-field metric、shadow classifier、GEPA per-predictor feedback、将来可能的 `BootstrapFinetune`）都直接落在 `dspy.Example/Evaluate/GEPA` 的公共 API 上，不再穿过自研 transport——第 3 节教程里的每个配方从「要移植」变成「可直接用」。

### 2.3 时序：一条冻结链

```text
#117 重开（P0，暂停泛化 GEPA spend）
  └─ 压后 #245 v8 受理战役（原时钟：#326 部署 + 24h）
  └─ #344 合并前置：#117 waiver + #245 unblock receipt + paired gate + 回滚镜像
       └─ 深度分析 Agent：#117 终态验证之后（其正文 Non-goals 与优先级声明）
```

---

## 3. 对照 DSPy 官方 classification_finetuning 教程

教程配方（[dspy.ai/tutorials/classification_finetuning](https://dspy.ai/tutorials/classification_finetuning/)）：Banking77（77 类客服意图，单轴互斥、标签即真值），`dspy.ChainOfThought("text -> label")`；teacher GPT-4o-mini 在约 500 条**无标签**输入上生成轨迹，`BootstrapFinetune` 蒸馏进 student Llama-3.2-1B（SGLang 本地服务）：无标签 51.5% → 加标签并用 exact-match metric 过滤轨迹后 **86.7%，反超 teacher 裸跑的 55%**。

| 轴 | 教程（Banking77） | Tracefold 现状 | 差异的含义 |
|---|---|---|---|
| 标签空间 | 单轴、互斥、closed-world，真值由构造保证 | 17 类混六轴、exact Gold 覆盖 12.7% | 教程从我们的「第 3 步」起步；#117 是在补第 0–2 步 |
| 分类的地位 | 分类即最终输出 | 分类是事实投影，`decide()` 拥有处置权 | 我们的准确率永远不能用「推得对不对」代答 |
| 优化杠杆 | 权重（蒸馏 + metric 过滤） | 指令（GEPA、demos 恒空、+10% token 闸） | 权重一动 = model binding/envelope 全动；指令是身份代价最小的杠杆 |
| 数据策略 | teacher 轨迹当训练数据 | 模型 pre-label + 人工裁决（reviewer/adjudicator 分离，拒绝「双 LLM 一致=Gold」） | 教程有 oracle test set 兜底蒸馏漂移；生产新闻流没有，人工裁决是唯一真相锚 |
| 评估纪律 | 单 dev 集随机切分 | cluster-disjoint、time-ordered、future holdout 注册后开窗、shadow、10% canary | 教程是 demo；我们的闸门体系已超出它，缺的只是「分类」这一科的考卷 |
| 模型经济 | 1B 学到 86.7% | 本地 Qwen 主路，#315 修复后 0 备路 | 小模型不是瓶颈的两条独立证据 |

两个可搬的结论（推断，依据如上）：

1. **次序**：教程隐含的前提（干净 taxonomy + 标签）正是 #117 要建的东西。对着坏的标签空间跑任何优化器，只会把噪声固化进指令或权重——#117「先修 label space、冻结优化预算」的排序与教程的成功条件一致。
2. **路线**：#117 Phase 1 攒出几百个 adjudicated cluster 后，`BootstrapFinetune` 从玩具变成真选项——GEPA（指令）平台期之后的下一根杠杆。代价要照实算：GGUF 导出、llama.cpp 换模、model identity 迁移、单 GPU 槽没有训练算力——是独立立项。#344 之后它与 `dspy.GEPA` 共享同一 substrate，接入才是「薄」的。

---

## 4. 成熟新闻分类实践调研

> 本节来源均为官方规范/文档/论文，链接随文；调研由三条并行线完成（通讯社标准、金融新闻分析供应商、事件编码与学术），事实与推断分开标注。

### 4.1 通讯社与行业标准（IPTC / NewsML-G2 / AP / Reuters）

**IPTC Media Topics：1,200+ 词、5 层、17 个顶层，季度批处理更新（事实）。** 每季 release note 明列 added/retired/moved/renamed（2025-Q1：新增 8、退役 17、改名 43、改定义 64、**28 次 hierarchy move**），允许整季跳票（[guidelines](https://iptc.org/std/NewsCodes/guidelines/) · [2025-Q1](https://iptc.org/news/iptc-newscodes-2025-q1-release/)）。加密货币主题 2019 年就有：`medtop:20001279`，broader=currency，带 Wikidata QID（[概念页](https://cv.iptc.org/newscodes/mediatopic/20001279)）。

**ID 稳定性的机制值得单独记（事实）：** 官方承诺 "URL-based ID which will never change"、不做 breaking change；新词用**不透明流水号**（20000xxx），层级信息全部外置在 broader 关系里——所以单季敢做 28 次层级重组而消费者零迁移。反面教材是它自己的前身：旧 Subject Codes 把层级编进 8 位数字 ID，重组即换身份，2010 年靠一次硬切才甩掉。退役机制：**从不删除**，标 retired + note 指明替代词（"water → 用 water supply/ocean/river"）。新词准入五判据：granularity balance / threshold of coverage（低频不设专类）/ distinct semantics / external validation（Wikidata、LoC 有对应）/ not specialized（[guidelines](https://iptc.org/std/NewsCodes/guidelines/)）。

**「一个词表只回答一个问题」（事实）。** IPTC 明确拒绝 one big vocabulary，NewsCodes 家族按轴分立：subject（是什么事）、genre（体裁：Analysis/Exclusive/**Fact Check/Satire**）、cpnat（实体性质：person/organisation/event…）、why（标签来历：direct/**ancestor**/inferred/associated）、urgency（多急）（[groups](https://iptc.org/standards/newscodes/groups/) · [genre](https://cv.iptc.org/newscodes/genre/) · [whypresent](https://cv.iptc.org/newscodes/whypresent/)）。注意两点：subject 词表里**没有 rumor、没有 other/noise 桶**——真伪状态放在体裁与编辑流程，「不值得发」由 urgency/priority 轴处理，不占 subject 槽位；multi-label + 每标注 0–100 relevance/confidence 是规范字段。

**NewsML-G2 三层身份分离（事实）：** 单条报道 guid+version（修订递增）；event 是独立 concept，跨多条报道**同一 event concept id**；BBC/PA 的 Storyline ontology 再把「无可争议的事实事件」与「编辑叙事」分成两类对象（[G2 guidelines](https://www.iptc.org/std/NewsML-G2/guidelines/) · [storyline](https://iptc.org/thirdparty/bbc-ontologies/storyline.html)）。

**AP/Reuters 的治理形态（AP 数字为二手，[Poynter 2013 采访](https://www.poynter.org/reporting-editing/2013/how-taxonomies-help-news-organizations-understand-and-categorize-their-content/)，已标注）。** AP：约 4,300 个 subject、日均 10 万条**全自动规则打标、编辑不逐条审批**；10 人团队的人力全部花在杠杆上——每词条 **≥85% precision/recall** 的生产门槛、gold set 回归（改规则后全量重跑对账）、新规则最长两周试运行。Reuters/LSEG：3,000+ topic codes，官方文档里一条真实 MRN 消息带 **50 个 code**，按前缀分轴（B: 行业、G: 地理、P: PermID 实体、R: RIC、N2: 主题），主题层级显式同发（N2:FINS08 与父码 N2:FINS 同现），随文附 relevance/confidence/sentiment/dedup 字段（[LSEG News](https://developers.lseg.com/en/product/news) · [MRN 样例](https://developers.lseg.com/en/article-catalog/article/introduction-machine-readable-news-elektron-websocket-api-refinitiv)）。IPTC 自己出资建的分类引擎 EXTRA 选了规则引擎，理由原文：突发新闻不能等重训练、规则可解释可控、自动化比人工逐条更一致（[EXTRA](https://iptc.org/news/extra-iptc-infalia-elasticsearch-open-source-rules-based-classification-engine/)）。

### 4.2 金融新闻分析供应商（RavenPack / LSEG-Refinitiv / Bloomberg / Dow Jones）

**RavenPack：taxonomy 是层级 + 正交治理字段（事实）。** 结构为 `TOPIC → GROUP → TYPE → SUB_TYPE`，叶子（CATEGORY）把实体 role 编进类目名（如 `analyst-ratings-change-positive`）；规模三代膨胀 2,064（RPNA 4.0）→ 6,895（RPA 1.0）→ 7,400+（[RPA User Guide 镜像](https://som.ustc.edu.cn/_upload/article/files/c0/28/c4afd94448c68b4ca1c174b1a7c6/e0a62fa2-646e-491a-acc5-18efdbab1181.pdf) · [WRDS 对照表](https://wrds-www.wharton.upenn.edu/documents/1395/RavenPack.pdf) · [官网](https://www.ravenpack.com/technology/classification)）。每个叶子固定声明三个治理字段：`FACT_LEVEL`（fact/forecast/opinion——**证据状态是独立的轴**）、`SCHEDULED`（是否日程可预期）、`VALID_ENTITY_TYPES`。关键的版本化事实：类目三代膨胀，但 **FACT_LEVEL 的三个值与 RELEVANCE 的值域从未变过**——稳定轴与膨胀轴分离，是版本化得以进行的前提；taxonomy 本身是可下载的带版本 CSV 工件，契约写明「解析器必须容忍未知值」。

**per-entity relevance 的值域语义写死在契约里（事实）。** `RELEVANCE` 0–100：0=被动提及、**>75 视为显著相关**、≥90 通常在 headline、**source 角色打 10 分**；与 `EVENT_RELEVANCE`（事件在故事中的位置分）是两个分开的字段。一条故事**每个实体产一条记录**。学界实操直接拿这些锚点做阈值（Heston–Sinha 剔除 relevance<0.35；von Beschwitz 等发现市场对 relevance≥90 的文章反应显著更快——**元数据本身移动市场**，[Fed IFDP 1233](https://www.federalreserve.gov/econres/ifdp/files/ifdp1233.pdf)，检索摘录级转述）。

**novelty 是对持久 archive 计算的，两代机制都不是「让模型判断」（事实）。** 旧 ENS：24h 窗口内同实体同类事件首报=100，同链按固定序列 100,75,56,42…衰减（衰减序列来自已下线产品页的检索摘录，两次独立检索一致；ENS 存在与 24h 窗口由[官方研究页](https://www.ravenpack.com/research/systematically-trading-infrequent-news)确认）。现产品换成 `EVENT_SIMILARITY_KEY`（**结构化事件属性相等**——role、百分比、magnitude 全等则同 key）+ `EVENT_SIMILARITY_DAYS`（距上条相似事件 0–365 天）。LSEG/TRNA 用另一种机制：对 (story, asset) 对算 **linguistic fingerprint**，`noveltyCounts` 按 12H/24H/3D/5D/7D 五窗口给 linked 计数并附 `linkedIds`（[MRN WebSocket 文档](https://developers.lseg.com/en/article-catalog/article/how-to-get-mrn-news-analytics-data-via-elektron-websocket-api)）。

**方向/情感分是「先分类、后查表」，不是自由发挥（事实）。** RavenPack ESS 的构造：金融专家给**每个 event category** 定一个分数区间，运行时按故事披露的 magnitude（beat 幅度、评级档位、震级、伤亡数）在区间内定值——**分类是模型的活，打分是查表 + 算术**。LSEG 输出 pos/neu/neg **概率三元组** + relevance + `firstMentionSentence`；Bloomberg 输出 S∈{-1,0,1} + **confidence 0–100**（[arXiv:2604.26811](https://arxiv.org/abs/2604.26811) 旁证），公司级日度分按 confidence 加权聚合。

**实体身份全部是永久 ID + point-in-time 别名库（事实）。** RavenPack `RP_ENTITY_ID`（6 位永久码，别名/证券标识/子公司关系带生效区间）、LSEG PermID（开放永久 ID）、Dow Jones 按 ticker/CUSIP/DUNS/ISIN 对表；Dow Jones 还把 per-entity relevance 压缩成二值：`company_codes_about`（实质相关）vs `company_codes_occur`（仅出现）（[Factiva 文档](https://factiva-news-python.readthedocs.io/en/latest/overview/querybuilding.html)）。来源质量单独成轴：RavenPack `SOURCE_RANK` 1–10。

**标注治理：有机制，无准确率声明（事实）。** LSEG 初代引擎用 **3,000 篇三重标注**新闻训练，标注顺序随机化以防从行情倒推（[Heston & Sinha, Fed WP 2016-048](https://www.federalreserve.gov/econresdata/feds/2016/files/2016048pap.pdf)）；topic code 走记者人工 + NLP 双通道，RCS code **带 confidence 与 provenance**。Bloomberg 内部数据集「每条 2 人标注 + 第三人裁决」，专职金融专家参与，标签语义以投资者立场定义（[BloombergGPT, arXiv:2303.17564](https://arxiv.org/abs/2303.17564)）。四家都**不公布** precision/recall 基准——可信度的落点是「机制可审计 + 覆盖可数 + 字段值域语义明确」，不是一个准确率百分数。

**收敛结论（事实归纳）：** 四家在完全不同的技术栈上（模板规则 / 三层 NN / 深度模型 / 编辑索引）独立收敛到同一行形态——**每条 (story, entity) 对输出一行：who（永久 ID）、what（taxonomy 叶子 + role）、how much（relevance）、how new（novelty，对台账算）、which way / how sure（sentiment + confidence）、what status（fact level / scheduled）**。这是消费端需求决定的形态，不是实现偏好：下游量化只能吃「可阈值化的标量 + 可 join 的 ID」。

### 4.3 政治/冲突事件编码与学术数据集（CAMEO / PLOVER / ACLED / UCDP / ACE / MAVEN / 金融事件抽取 / FOMC）

**事件 = actor–action–target + geo，四个轴永远分开（事实）。** GDELT/CAMEO 的事件记录是「Actor1 – Action – Actor2」三元组加独立地理字段；动作轴是 20 个根类下的层级码表（[GDELT Event Codebook V2.0](http://data.gdeltproject.org/documentation/GDELT-Event_Codebook-V2.0.pdf)）。**actor 不是枚举而是组合码**：CountryCode + 角色码（38 个：GOV/MIL/REB/OPP/BUS/MED…）+ 已知组织专码（约 165 个：IMF/WBK/NAT…）——`USAGOV` = USA+GOV、`RUSMIL` = RUS+MIL（[type](https://www.gdeltproject.org/data/lookups/CAMEO.type.txt) · [knowngroup](https://www.gdeltproject.org/data/lookups/CAMEO.knowngroup.txt)）。「谁」靠带日期范围的 actor dictionary 解析（同一个人任职前后换码，[openeventdata/Dictionaries](https://github.com/openeventdata/Dictionaries)）；新一代 coder NGEC 干脆放弃词典，把 actor 解析成 **Wikidata 实体链接**再映射国家+sector 码——词典对 gold 的召回只有 0–30%，维护成本荒谬（9,000 条动词 pattern 翻成阿拉伯语耗 15 人 750 小时）（[arXiv:2304.01331](https://arxiv.org/abs/2304.01331)）。

**PLOVER：砍类目的理由写的是人的工作记忆（事实）。** [PLOVER](https://github.com/openeventdata/PLOVER) 把 CAMEO 250+ 个细码收敛为 16–18 个事件类，原子码信息拆进 **event–mode–context**（what–how–why）三个正交小词表（context 约 37 个值：economic/election/cyber/territory…）。手册原文："a human coder can hold most of the relevant categories in working memory"——250 个数字码没有任何认知设施能处理。其继任数据集 POLECAT 的逐轴实测：event type 每类约 **200–400 篇**专家标注、F1 0.56–0.90；mode/context 正例 8–464 即上线、F1 0.7–0.97；论文自认短板是预算所限每篇单人标注。

**一场战争不是一个类（事实）。** [ACLED](https://acleddata.com/knowledge-base/codebook/) 是 6 个 event type / 25 个 sub-event type + 8 类 actor + admin1-3 地理与精度码；俄乌战争在数据里是**逐日逐地的原子事件流**（同日同城的空袭与地面交火是两条记录，actor 对是 "Military Forces of Russia" vs "Military Forces of Ukraine"）。[UCDP GED](https://ucdp.uu.se/downloads/ged/ged241.pdf) 走得更远：每条事件携带 `conflict_new_id` 与 `dyad_new_id` 外键，指向**冲突注册表**与 **actor 对（dyad）注册表**——「俄乌战争」是注册表里的一行，有自己的生命周期语义（年度 25 battle-deaths 的 active 阈值）。ACLED 的另一个教训：living dataset 每周回溯修订，某周事件数从初版 445 条修到 870 条（[arXiv:2603.25964](https://arxiv.org/pdf/2603.25964)）——报告延迟下「按窗口读数」必须声明版本。

**标注一致性与数据量级（事实）：**

| 数据集 | taxonomy | 规模 | 一致性 |
|---|---|---|---|
| [ACE 2005](https://catalog.ldc.upenn.edu/LDC2006T06) | 8 types / 33 subtypes + 22 roles | 599 英文文档 / 5,349 event mentions | 部分双标 + adjudication（事件层数字未公开成文） |
| [MAVEN](https://arxiv.org/abs/2004.13590) | 168 types（树状） | 4,480 文档 / 118,732 mentions | 众包首轮 κ=0.38–0.43（不可用）；专家二轮 κ=0.64–0.737 |
| [ChFinAnn](https://arxiv.org/abs/1904.07535) | 5 类中文公告事件 ×6–9 roles | 32,040 篇 | 远程监督对齐披露库；人工抽检自动标签 F1 94.0 |
| [Trade-the-Event/EDT](https://arxiv.org/abs/2105.12825) | 11 类公司事件 | 9,721 篇标注 + 30 万篇分钟级评测 | 双人合意制，未报告 κ |
| [CrudeOilNews](https://arxiv.org/abs/2204.03871) | 18 types + argument + polarity/modality | 425 篇 / 10,578 events | event type κ=0.79、trigger 0.68、modality 0.63 |

规律（推断）：taxonomy 越大层越深，一致性单调下降；168 类直接交众包掉到 κ≈0.4，必须「预标注候选 + 专家裁决」两段制；金融两大数据集都绕开了人工全量标注（远程监督或小类目+合意制）——**「类目小而边界硬」是金融事件标注可行性的前提**。

**央行观察是独立标签轴，不是事件类（事实）。** [Trillion Dollar Words](https://arxiv.org/abs/2305.07972)（ACL 2023）：sentence 级 hawkish/dovish/neutral，**人工标注 2,480 句**（标注者原始一致率约 90%），fine-tuned RoBERTa-large F1 0.7113 **显著高于 zero-shot ChatGPT 的 0.5868**；文档级信号由句级聚合，与 CPI/PPI 相关 0.54–0.81。[Hansen & Kazinnik](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4399406)（500 句 FOMC statements、五档标签；数字来自二手一致转述，原 PDF 未取得）结论相反：GPT-4 zero-shot 大幅超过 BERT 与词典法。两篇的分歧可由语料难度解释（推断）：minutes/speeches 长而绕→微调赢；statements 短而程式化→zero-shot 够。共同设计：**立场是挂在固定 actor（Fed）语料上的独立轴，由句级聚合成时序**。

**LLM 时代分类实践（有实证的，事实）：**

- 把**标注指南含边界判例写进 prompt** 显著提升零样本抽取，消融证明 guidelines 是关键（[GoLLIE, ICLR 2024](https://arxiv.org/abs/2310.03668)）；prompt 本身价值约等于数百个训练样本（[Le Scao & Rush](https://arxiv.org/abs/2103.08493)）。
- 但分类任务上**微调小模型仍普遍赢过 zero/few-shot 大模型**（[Bucher & Martini](https://arxiv.org/abs/2406.08660)、[16 数据集系统比较](https://arxiv.org/abs/2403.17661)、FOMC 上同样成立）；微调门槛可以很低——SetFit 每类 8 例追平全量微调（[arXiv:2209.11055](https://arxiv.org/abs/2209.11055)）。
- **弃权要教**：训练数据里显式构造拒答同时提升已答准确率与校准（[R-Tuning](https://arxiv.org/abs/2311.09677)）；风险–覆盖框架下，独立校准器在 80% 准确率约束下把可答比例 48%→56%（[Kamath et al., ACL 2020](https://arxiv.org/abs/2006.09462)）；LLM 口头 confidence 系统性过度自信，多采样一致性+独立校准器更可靠（[Xiong et al., ICLR 2024](https://arxiv.org/abs/2306.13063)）。
- **严格 schema 约束会损伤推理**，约束越紧退化越大（分类类任务受影响较小）（[Let Me Speak Freely](https://arxiv.org/abs/2408.02442)）——先自由推理后填字段的两段式更稳。
- **pre-label + 人工裁决的经济学**：ChatGPT 标注在多数任务上超过众包、成本约 1/20（[Gilardi, PNAS 2023](https://arxiv.org/abs/2303.15056)），但质量高度任务相关，必须 validation-first——每个任务先用人工 gold 验证再放量（[Pangakis et al.](https://arxiv.org/abs/2306.00176)）；伪标签+少量人工可省 50–96% 成本（[Wang et al.](https://arxiv.org/abs/2108.13487)）；按模型不确定性分配人机分工最多 +21%（[CoAnnotating, EMNLP 2023](https://arxiv.org/abs/2310.15638)）。

---

## 5. 启示：能否彻底改善？

### 5.1 判决

**结构性改善可达，且 #117 的方向与三条独立调研线的收敛完全同构——拆轴不是过度设计，是行业共识。** 证据链：IPTC 明文拒绝 one big vocabulary、按轴分立词表（4.1）；四家商业供应商没有任何一家把「rumored-M&A-about-X」做成一个枚举值，全部表达为 category × fact_level × entity+role × relevance 的组合（4.2）；政治事件编码从 CAMEO 250 码收敛到 PLOVER 16–18 类 + mode + context，理由写的是编码可靠性（4.3）。`news_taxonomy_v1` 的五字段正是这个形态；`assertion_status` 单独成轴甚至比 IPTC 更细（行业把真伪放在体裁与流程里），与 RavenPack 的 FACT_LEVEL/SOURCE_RANK 双轴一致——是合理的领域特化，不是偏离。

**「彻底」取决于三件事，全部可控：**

1. **Gold 投入到位**（第 7 节的量级与一致性目标）：POLECAT 证明每类 200–400 篇专家标注足以支撑 F1 0.56–0.90 的生产级分类器；FOMC 证明 2,480 句支撑出可交易信号；反例是 168 类交众包 κ≈0.4。我们的量级要求（#117 地板 + 30–50/family 评估 support）在这些锚点的最低档，是两人团队负担得起的。
2. **焦点表达补注册表轴而不是扩枚举**（5.2）：这是 v2 的一小步，不动 #117。
3. **消费端字段语义写死在契约里**：relevance/confidence/弃权的值域锚点（「>75 显著」这一类）是供应商产品可被量化消费的原因；`TaxonomyEvaluationReportV1` 的 per-class gate 与 risk-coverage 曲线就是我们的对应物。

**「彻底」不包括的部分，也要说清楚（推断）：** (a) 语义模糊的尾部不会消失，只会被 `other/unknown` 弃权承接、由 risk-coverage 曲线管理——行业同样如此（IPTC 低频不设专类、供应商靠 confidence 阈值弃权）；(b) 一致性差的轴修的是 taxonomy 不是标注员（κ<0.67 → 合并/重写定义/降级，见第 7 节）；(c) 混 cohort 窗口的读数永远只是诊断，单一身份 cohort 才有质量结论（1.3）。模型能力不在风险清单上：77 类 1B 学到 86.7%（第 3 节）、POLECAT 逐轴 F1 0.7+、现网 Qwen 主路 0 备路（1.4），三条独立证据同向。

还有一个值得记录的事实（推断）：本仓库已经**独立收敛**到供应商的两个核心机制——told ledger + 来源制品指纹（#64/#154）≈ TRNA linked-stories / RavenPack similarity-key 的 novelty 台账；`decide()` 拥有处置权 ≈ 供应商只卖分数不做决策。这说明差距不在架构直觉，纯在「分类这一轴没有 Gold」。

### 5.2 「宏观下也有重点」：美联储、俄乌在 #117 下怎么表达

先说 #117 已经给到的：`macro_policy_data` 与 `geopolitical_conflict` 在 `event_family` 分家——「美联储加息」与「俄乌前线变化」不再共享一个 `macro` 桶；`subject_codes` 若把货币政策/央行类 IPTC 节点纳入钉选集，Fed 主题可以被稳定标出；`change_state` 区分「预告的 FOMC 决议」与「意外表态」。

还缺的：「哪家央行、哪场冲突」是**身份**，不是**类目**。4.3 的证据显示成熟体系在这里完全收敛——事件类目枚举只装「行为/事件族」，小而封闭（ACLED 6、PLOVER 16–18、CAMEO 根类 20）；「谁/在哪/什么主题/哪场戏」各有自己的轴：

- **actor 是注册表不是枚举**：CAMEO/GDELT 用国家码×角色码组合（`USAGOV`、`RUSMIL`），PLOVER/POLECAT 直接挂 Wikidata QID；GDELT GKG 甚至有现成的 `ECON_CENTRALBANK`/`ECON_INTERESTRATE` 主题码。「美联储政策」= actor:Fed ∧ theme:central_bank，两个既有轴的交点，事件枚举零改动。
- **持续冲突是 episode 注册表 + 外键**：UCDP 每条事件带 `conflict_new_id`/`dyad_new_id` 指向冲突注册表与 actor-对注册表——「俄乌战争」是注册表里的一行，下挂逐日逐地的原子事件流；ACLED 里它只是 actor 对 × geo × 时间窗的一个查询视图。NewsML-G2 同构：报道 guid、event concept id、storyline 三层身份分离。
- **立场（鹰/鸽）是挂在固定 actor 语料上的独立标签轴**（FOMC 实践），由句级聚合成时序，不是事件类。

落到我们（推断）：(a) 给宏观主体与地缘对一个小的钉选注册表（`actor:us_fed`、`geo:ru-ua` 一类，可带 Wikidata QID 做外部锚），挂在 grounding/assets 同层而不是 `event_family`；(b) 用已有 storyline 机制承接「持续焦点」，俄乌是一条长 storyline 上的 episode 流。判据抄 4.3 的原话：**新增一个焦点时只在注册表加行、不改枚举，即为无污染**。这两件事都是 `news_taxonomy_v2` 的候选，不应塞进 #117——#117 的五字段先立住，注册表轴才有挂靠点。

### 5.3 逐轴对照：我们已有 / #117 将有 / 仍缺

| 轴 | 行业收敛做法（4.x） | 我们现状 | #117 之后 | 仍缺 / 去向 |
|---|---|---|---|---|
| 事件类目 | 小而封闭：ACLED 6、PLOVER 16–18；IPTC 五条准入判据挡门 | 17 类混六轴 | `event_family` 13 值 + 4 正交轴 | 值集变更学 IPTC：季度批处理 + changelog + retired 指路牌（v2 治理） |
| 主题 subject | IPTC 钉选叶子 + 代码展开祖先（`why=ancestor`） | 无 | `subject_codes` ≤3 钉选节点 | 祖先展开放确定性代码，不进 prompt |
| 实体/actor | 永久 ID + point-in-time 别名（RP_ENTITY_ID/PermID/CAMEO 组合码/Wikidata QID） | assets（ticker）+ venue universe + 别名表 | 不变（`subject/event` 分离已定） | 宏观 actor/geo 注册表（5.2，v2） |
| per-entity relevance | 每 (story, entity) 一行，0–100 带值域锚点；最粗也是 about/occur 二值 | 判定粒度是 event；`asset_grounding` 是质量维度不是分值 | 不变 | 读者产品当前不需要；deep/Trading 消费时是 v2 候选 |
| novelty | 对台账计算：指纹+窗口计数 或 结构键+距离天数 | told ledger + storyline + 制品指纹（#64/#154）——**已独立收敛** | 不变 | 补半步：重复计数按窗口分档暴露为字段 |
| 证据状态 | RavenPack FACT_LEVEL（fact/forecast/opinion）+ SCHEDULED，逐叶子声明 | `rumor` 混在 17 类里 | `assertion_status` + `change_state` 双轴，**比行业更细** | — |
| 来源质量 | SOURCE_RANK 1–10，独立于证据状态 | provenance 已存但未成轴 | `source_authority` 由代码从 provenance 计算 | — |
| confidence/弃权 | 概率三元组或 C 0–100 + 阈值弃权；LLM 自报置信不可靠（4.3） | `confidence` 模型自报 0–1 | `other/unknown` 正式弃权 + risk-coverage 报告 | 中期把自报分换成一致性/独立校准器（4.3 的实证方向） |
| 打分构造 | ESS＝事件类先验区间 + magnitude 修正：**分类是模型的活，打分是查表+算术** | `magnitude` 0–3 模型直出 + RulePack 判例校准 | 不变 | v2 思路：per-family magnitude 先验表，缩小模型自由度 |
| 标注治理 | AP：人管判据与 gold set（每词条 ≥85% P/R、回归、试运行）；LSEG 三重标注随机化；Bloomberg 2+1 裁决 | ReviewDesk + accepted review 机制在，taxonomy 无 Gold | `news_review_v5` 五字段 exact + 裁决分离 | κ 判据落进 gate（第 7 节）；pre-label validation-first |
| 身份/版本化 | 不透明 ID + 层级外置；taxonomy 是带版本可下载工件；「容忍未知值」前向契约 | program/envelope/policy pin 纪律已同构 | `taxonomy_version` 落 Judgment，跨版本不混算 | 枚举值退役机制照 IPTC（不删值，retired+映射表） |

---

## 6. 基于 DSPy 的改进路径（post-#344）

按依赖顺序，每步都以前一步的证据为门：

1. **原生 substrate（#344 本体）**：两个 Signature 用现有 Pydantic 输出模型做 typed output；`dspy.JSONAdapter` 承担结构化输出与能力回退；GEPA 走 `dspy.GEPA` + 现有 instruction growth budget。此后「分类改进」不再碰 transport。
2. **Shadow taxonomy classifier（#117 Phase 2）**：内容寻址、release-neutral 的 `TaxonomyShadowProgramV1`，与生产同一 bounded renderer、同一 record/replay seam——在 #344 之后它就是一个普通 `dspy.Module`，评估直接用 `dspy.Evaluate` + per-field exact metric。
3. **Per-field release gates（#117 Phase 4）**：五字段分立 gate；现有 production-action/grounding/novelty metrics 继续作非回归门。GEPA 候选只许改 EventSemantics instruction，反馈继续按 owner 路由（objective.py 的 target/control/excluded 三分）。
4. **有条件的 `BootstrapFinetune`（新立项，非 #117/#344 范围）**：触发条件全部满足才立项——(a) adjudicated Gold ≥ 数百 cluster 且 per-class support 达标；(b) GEPA 在 taxonomy 字段上出现平台期的量化证据；(c) 接受一次 model identity 迁移（GGUF 导出、llama.cpp 换模、envelope/model binding 移动、paired gate 重跑）；(d) 训练算力另行解决（单槽 4090 推理机不承担训练）。
5. **不做的**（与 #117/#344 Non-goals 对齐）：热路径不加第三个 Predictor；不引入 ReAct/工具/向量库做分类；不用价格反应、模型自报 confidence 或双 LLM 一致当 Gold；不建第二套实验平台。

---

## 7. 数据集：要不要、要什么、怎么攒

**要。但不是「找一个数据集」，而是把 #117 Phase 1 当成数据集工程来跑。**

1. **Gold 的形式已经定了**：`news_review_v5` 的 accepted review，五字段 exact expected + evidence refs；draft author / reviewer / adjudicator 身份分离，模型可 pre-label 但不能 accept 自己的草稿。独立样本单位是 connected fact cluster，train/development cluster-disjoint 且 time-ordered。
2. **起步量照 #117 的地板，放量目标用外部锚点校准**：#117 地板是 boundary ≥30、retention ≥100、negative ≥50、`product_service_change` ≥30、`financial_results+guidance_outlook` ≥30（cluster 计）。外部锚点（4.3）：POLECAT 每类 **200–400 篇**专家标注做到 F1 0.56–0.90；FOMC 三分类 **2,480 句**支撑出可交易的时序信号；SetFit 证明每类 8 个精选例是 prompt/对比学习路线的下限。本文建议（推断）：per-family 评估 support 先到 30–50 cluster（让 per-field gate 有裁决力），高价值 family（product/financial）向 200+ 放量；将来若做 finetune，量级 500–2,000。
3. **困难样本池现网已经免费攒好**：592 行 `product_progress` 通道错配（本文 1.2b）、`product` 的 m1/m2 边界带（471 行 m1）、confirmed↔rumor 翻转、`other/unknown` 候选。按 cluster 去重后，这就是第一批标注队列。外部实证支持这种「按不确定性/分歧路由标注」的形态（[CoAnnotating](https://arxiv.org/abs/2310.15638)：按模型不确定性分配人机分工最多 +21%），且 pre-label 放量前必须 validation-first——先用人工 gold 验证该任务上的 pre-label 质量再扩产（[Pangakis et al.](https://arxiv.org/abs/2306.00176)）。
4. **Codebook 即 Prompt**：#117 要求每个值有 plain-language 定义、正例、反例、边界例，IPTC 钉选节点带上游版本与 digest。这份 codebook 同时是标注指南与 EventSemantics instruction 的素材——写一次，两处用。
5. **外部数据集的角色**（推断）：Banking77 之类只用于方法论校准（管线通不通、metric 写得对不对），不是我们契约的真值；Fed 鹰鸽等领域数据集（见 4.3）可作子任务参考。领域分布、中文读者契约与我们的 evidence 形态都无法从外部语料迁移。

6. **一致性目标要写成数字**（依据 4.3，推断部分已标）：通行阈值是 Krippendorff **α ≥ 0.800 可靠、0.667–0.800 仅供试探性结论、<0.667 弃用**（[Krippendorff 规则](https://en.wikipedia.org/wiki/Krippendorff's_alpha)）。实测锚点：训练有素标注者在 event type 层做到 κ≈0.74–0.79（CrudeOilNews 0.79、MAVEN 专家轮 0.737）；168 类直接交众包 κ≈0.38–0.43，完全不可用。运营建议（推断）：#117 的 `TaxonomyEvaluationReportV1` 已含 reviewer agreement 与 adjudication rate——给它们配上判据：核心轴 κ ≥ 0.75 起步、迭代到 0.8；某一轴 <0.67 时不是加人力，而是合并类目、重写定义补边界判例、或把该轴降级为非承重标签。一致性差是 taxonomy 的病，不是标注员的病。

---

## 8. 深度分析 Agent：设计与方法论（post-#117）

#117 明确「完成前不启动深度分析 Agent」，且 1.2b 的 41.1% 错配说明现在连触发人群都圈不准。以下是 post-#117 的设计，契约与语料准备可以现在零身份风险地做。

### 8.1 定位与触发（code-owned）

Triage 回答「20 秒内推不推」；deep 回答「分钟级、这意味着什么」。触发是 taxonomy 上的确定性谓词：

```text
event_family = product_service_change
AND change_state IN (announced, effective, reported)
AND source_authority IN (regulatory_filing, issuer_first_party, reputable_secondary)
AND assertion_status = confirmed
AND magnitude >= 2 AND 已投递
```

按 storyline 去重（每次实质 `change_state` 跃迁一篇，不是每条推文一篇），加日配额与冷却。基建已预留：`news_verdicts.stage` 的 CHECK 里本有 `'deep'`（现存 14 行历史数据）；[progression_review.py](../../src/tracefold/news/progression_review.py) 已示范 post-delivery 车道的接法。

### 8.2 输出契约先行：结构化断言，不是文章

`DeepAnalysisV1`：`what_changed`（对 told ledger 说清增量）、`mechanism_path`（枚举价值通道）、`materiality + timing`（何时能在哪个数字上看到）、`expectation_state`（预告=priced-in vs 意外）、**`falsifiable_checkpoints` 2–5 条带日期与阈值**、`comparables`（带实测 base rate）、`invalidation`、允许弃权。每条断言必须带 evidence refs——无据不断言。投递由确定性 DeliverPolicy 决定（≥1 检查点 + ≥1 第一方证据 + 通道非空，否则只归档）：模型永远没有最终投递权，与 triage 同一不变量。

### 8.3 程序形状：固定 fetch plan，不是自由浏览

证据装配是代码：按资产类型与 `source_authority` 走确定性抓取计划（股票：EDGAR 8-K/官方稿原文；加密：发行方 docs、链上指标、交易所公告），bounded renderer + 定界不可信文本 + request-hash 录制回放（复用 #344 的 `RecordedLM` seam）。模型侧三个串行 predictor：FactDelta → MechanismMap（structured output）→ AnalystCard（中文稿）。comparables 检索是 SQL，不是模型调用。理由：预算可控、可回放、身份可 pin；自由 ReAct 三者全失。

### 8.4 领域方法论

- **股票**：产品 → 哪条价值通道（涉事 segment 收入占比是否 >5%、改不改 guidance、moat/竞品反应、capex 含义）；来源纪律（8-K/官方稿优先于媒体转述）；expectation（investor day 预告过的发布与真意外的 post-event 漂移完全不同）；检查点 = 下季 earnings call 提及/指引修订、D+30/90 第三方渠道数据。
- **加密**：核心判别是**价值捕获 vs 纯使用**——费用是否流向代币（burn/staking/treasury 才是 accrual，否则是 vanity 使用量）；供给侧是否同期有 unlock/emissions 对冲；采用可链上度量（D+7/30 协议费、活跃地址、TVL）→ 检查点天然可机器解析；roadmap 内事件的 sell-the-news base rate 用自家 storyline + price review 数据量化，不靠直觉。
- [#173](https://github.com/AnalyThothAI/tracefold/issues/173) 的边界表就是这个 Agent 要编码的判别力样本：付费且不可逆的部署前置步骤 = 真事件；活跃交易者 ATH = 真采用指标；累计地址数 = vanity；预测市场赔率 = 不是产品事实。

### 8.5 学习闭环：复用，不新建

整套 accepted-review/ReviewDesk/裁决机制照用（#344 验收原话：不建第二套学习平台）。冷启动走历史影子回填：圈选过去数月**结局已知**的产品 storyline，跑管线出分析，按维度裁决成 Gold。此车道独有的资产是**检查点解析器**——[#88](https://github.com/AnalyThothAI/tracefold/issues/88) price review 的同构物（现网已有 1h 命中率基建；2026-08-21 部署时 hit_1h 54.4%、N=886、覆盖 28.4%），到期把每条 checkpoint 写成 hit/miss/unresolvable 的客观结局。但按 #117 的原则推广使用：**结局数据做发现/抽样/非回归地板/读者端 track record，不做 GEPA 的 reward**（价格太噪，直接当 reward 就是 Goodhart：#88 评估期实测 1h 命中 56.0% vs 零假设 51.5%）。Gold 仍是人工裁决的 rubric；数据就绪门槛照抄 #117 风格。

### 8.6 一句话方法论与落地顺序

**contract → Gold → shadow → gate → optimize；永远不在能测量之前优化，不在标签空间成立之前测量。**

现在可做（零身份移动）：DeepAnalysisV1 契约草案、历史影子语料圈选、标注 rubric、base-rate SQL。#117 Phase 1–2 给出可信触发字段与 Gold 机制；#344 给出原生 substrate（deep 程序直接是一个 `dspy.Module`）。

---

## 附录 A：快照读法

[news-classification-baseline-snapshot-2026-08-29.json](news-classification-baseline-snapshot-2026-08-29.json) 的每个键对应 [discovery SQL](news-classification-baseline-discovery-2026-08-29.sql) 中同名小节；窗口相对 `captured_at_ms`（`2026-08-29T11:44:43Z`），唯一绝对界是 `post_315_deploy_since_ms`（#314+#315 部署时刻，来自 operator 部署回执）。所有值为聚合与身份前缀，可由任何 `tracefold_serve` 只读会话重算（数值随窗口漂移，结构不变）。

## 附录 B：外部来源清单与未能核实项

正文事实均已随文附链；此处按调研线汇总主一手来源，并保留诚实账本（未能核实项照实列出，正文对应条目已按「检索摘录/旁证/推断」降级标注）。

**4.1 通讯社与标准**：[IPTC Media Topics](https://iptc.org/standards/media-topics/) · [NewsCodes Guidelines](https://iptc.org/std/NewsCodes/guidelines/)（ID 不变承诺、退役机制、准入五判据）· [NewsCodes 家族分组](https://iptc.org/standards/newscodes/groups/) · [genre](https://cv.iptc.org/newscodes/genre/) / [cpnat](https://cv.iptc.org/newscodes/cpnature/) / [whypresent](https://cv.iptc.org/newscodes/whypresent/) 词表 · [2025-Q1 release](https://iptc.org/news/iptc-newscodes-2025-q1-release/) · [cryptocurrency 概念页](https://cv.iptc.org/newscodes/mediatopic/20001279) · [NewsML-G2 guidelines](https://www.iptc.org/std/NewsML-G2/guidelines/) · [Storyline ontology](https://iptc.org/thirdparty/bbc-ontologies/storyline.html) · [EXTRA 规则引擎](https://iptc.org/news/extra-iptc-infalia-elasticsearch-open-source-rules-based-classification-engine/) · [AP APISamples](https://github.com/TheAssociatedPress/APISamples) · [LSEG News 产品页](https://developers.lseg.com/en/product/news) · [MRN 样例（50 码消息）](https://developers.lseg.com/en/article-catalog/article/introduction-machine-readable-news-elektron-websocket-api-refinitiv) · 二手：[Poynter 2013 AP 采访](https://www.poynter.org/reporting-editing/2013/how-taxonomies-help-news-organizations-understand-and-categorize-their-content/)。

**4.2 金融供应商**：[RavenPack RPA User Guide v1.0 镜像](https://som.ustc.edu.cn/_upload/article/files/c0/28/c4afd94448c68b4ca1c174b1a7c6/e0a62fa2-646e-491a-acc5-18efdbab1181.pdf)（taxonomy/RELEVANCE/similarity/ESS/实体 ID/SOURCE_RANK 的定义均出于此）· [WRDS RPA vs RPNA 对照表](https://wrds-www.wharton.upenn.edu/documents/1395/RavenPack.pdf) · [RavenPack 官网 classification](https://www.ravenpack.com/technology/classification) · [ENS/G_ENS 研究页](https://www.ravenpack.com/research/systematically-trading-infrequent-news) · [LSEG MRN WebSocket 文档](https://developers.lseg.com/en/article-catalog/article/how-to-get-mrn-news-analytics-data-via-elektron-websocket-api)（noveltyCounts/linkedIds/概率三元组/firstMentionSentence）· [News Analytics 产品页](https://developers.lseg.com/en/product/news/news_analytics) · [PermID FAQ](https://developers.lseg.com/en/api-catalog/open-perm-id/permid-record-matching-restful-api/documentation/overview-and-concepts/faq) · [Bloomberg EDF Textual News fact sheet](https://assets.bbhub.io/professional/sites/41/Fact-Sheet-EDF-Textual-News.pdf) · [Bloomberg FX 白皮书](https://data.bloomberglp.com/promo/sites/12/99405_WP_MachineReadableNewsToTradeFX.pdf) · [Bloomberg 2025-11 新闻稿](https://www.prnewswire.com/news-releases/bloomberg-launches-customizable-real-time-news-feeds-for-enhanced-systematic-workflows-302701889.html) · [BloombergGPT](https://arxiv.org/abs/2303.17564)（2+1 标注制）· [Heston & Sinha, Fed WP 2016-048](https://www.federalreserve.gov/econresdata/feds/2016/files/2016048pap.pdf)（TRNA 引擎与 3,000 篇三重标注）· [Factiva 字段文档](https://factiva-news-python.readthedocs.io/en/latest/overview/querybuilding.html)。

**4.3 事件编码与学术**：[GDELT Event Codebook V2.0](http://data.gdeltproject.org/documentation/GDELT-Event_Codebook-V2.0.pdf) · GDELT lookups（[type](https://www.gdeltproject.org/data/lookups/CAMEO.type.txt) / [knowngroup](https://www.gdeltproject.org/data/lookups/CAMEO.knowngroup.txt) / [eventcodes](https://www.gdeltproject.org/data/lookups/CAMEO.eventcodes.txt) / [GKG themes](http://data.gdeltproject.org/api/v2/guides/LOOKUP-GKGTHEMES.TXT)）· [PLOVER](https://github.com/openeventdata/PLOVER) · [NGEC/POLECAT, arXiv:2304.01331](https://arxiv.org/abs/2304.01331) · [openeventdata/Dictionaries](https://github.com/openeventdata/Dictionaries) · [ICEWS 词典](https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:10.7910/DVN/28118) · [ACLED Codebook](https://acleddata.com/knowledge-base/codebook/) 与[更新机制](https://acleddata.com/methodology/keeping-acled-data-updated) · [UCDP GED Codebook 24.1](https://ucdp.uu.se/downloads/ged/ged241.pdf) · [ACE 2005](https://catalog.ldc.upenn.edu/LDC2006T06) · [MAVEN](https://arxiv.org/abs/2004.13590) · [ChFinAnn](https://arxiv.org/abs/1904.07535) · [Trade-the-Event](https://arxiv.org/abs/2105.12825) · [CrudeOilNews](https://arxiv.org/abs/2204.03871) · [Trillion Dollar Words](https://arxiv.org/abs/2305.07972) · LLM 实践各篇已随文附链（GoLLIE/SetFit/R-Tuning/Kamath/Zhao/Xiong/Gilardi/Pangakis/CoAnnotating/Let-Me-Speak-Freely 等）。

**未能核实项（诚实账本）**：AP Classification Metadata Reference Guide 原件（仅 Scribd 镜像检索摘要）；Reuters topic codes 治理流程（登录墙）；AFP 技术指南正文（站点拒抓）；IPTC Media Topics 当前精确总数（官方只写 "over 1,200"）；RavenPack ENS 衰减序列逐字原文（产品页已下线，两次独立检索一致）；RPNA 4.0 完整 user guide、LSEG MRN Data Models 实现指南、Bloomberg EDF 完整 schema（注册/付费墙）；[Fed IFDP 1233](https://www.federalreserve.gov/econres/ifdp/files/ifdp1233.pdf) 全文（转述级）；[Hansen & Kazinnik](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4399406) 原 PDF（403，数字来自多个二手一致转述）；CAMEO 手册 PDF 文本层（用 GDELT lookup 文件替代）；ERE 一致性论文的表格数字。
