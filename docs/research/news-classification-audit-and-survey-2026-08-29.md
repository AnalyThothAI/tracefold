# 新闻分类：现网体检、成熟实践调研与 #117/#344 之后的路线（2026-08-29）

> 状态：研究稿，不是实现 PR；不改变任何在线行为、身份或 Issue 验收。
>
> 范围：News 分类（`event_type` → `news_taxonomy_v1`）的执行前基线、[#117](https://github.com/AnalyThothAI/tracefold/issues/117) 与 [#344](https://github.com/AnalyThothAI/tracefold/issues/344) 的终态形状、与 DSPy 官方 classification 教程的方法论对照、行业成熟实践调研，以及数据集与深度分析 Agent 的路线建议。建议只作为后续 Issue 的输入，本文不构成验收。
>
> 证据规则：现网数字来自 `tracefold_serve` 只读聚合，`.sql`/`.json` 快照对与本文同目录提交（[discovery](news-classification-baseline-discovery-2026-08-29.sql) · [snapshot](news-classification-baseline-snapshot-2026-08-29.json)，captured `2026-08-29T11:44:43Z`）；只提交计数、比率、身份前缀与窗口，不含任何新闻文本、卡片、Prompt 或凭据。外部事实只采用官方规范、官方文档与论文并附链接；标注「推断」的句子是本文观点。

## 结论先行

1. **#117 目前没有「执行后」。** 旧 #117 于 2026-08-24 被标记关闭，但没有 closure comment、关联 PR 或实现证据；2026-08-29 06:13 UTC 被重开、改题并整体替换为「先建 accepted Gold、多轴 shadow、受控 hard cut」的 P0 计划。现网仍在跑 17 类混轴 `event_type`（[signatures.py](../../src/tracefold/news/program/signatures.py) 的单选枚举）。本文第 1 节是它的执行前基线。
2. **现网分类的病不是「分错」，是「不可测」。** 7 天 10,060 条 triage 判定里，exact Gold 只覆盖约 12.7% 的 accepted review、`must_push` 仅 3 例（#117 正文的 operator 聚合）；metric 明确把 `event_type` 排除在可评分维度外（[objective.py](../../src/tracefold/news/learning/objective.py) 的维度注释）。「分类对不对」至今没有任何直接测量。
3. **混轴枚举的代价可以量化。** `macro` 一个桶占 40% 流量；`product_progress` 通道 1,440 行中 41.1%（592 行）的 `event_type` 不是 `product`——平面单选标签表达不了「这同时是产品进展」，靠多标签 channel 才兜住。这判死了「用 `event_type` 做股票/产品路由」。
4. **运行可用性与分类质量是两回事，前者已经很好。** #314+#315 于 08-28 20:26 UTC 部署后：433 条判定、0 次备路接管、2 次降级（0.46%）。24h 口径里的 147 次备路全部落在部署前的旧运行时尾巴。
5. **#344 结束后，News 模型程序回到原生 DSPy**（`Module/Signature/Predict/LM/JSONAdapter/GEPA`），自研 transport/graph/GEPA adapter（约 80KB 框架代码）删除，Tracefold 只保留业务契约、RoutePolicy、审计与发布治理。当前方向已批、实施被 P0 闸挡住（`ready-for-human`），单 PR、单部署，且要 #117 的 GEPA spend waiver 才能合并。
6. **DSPy 官方 classification_finetuning 教程与我们不是同一道题。** 教程从「干净单轴 taxonomy + 标签即真值」起步做权重蒸馏（Banking77，1B student 86.7% 反超 teacher 55%）；我们卡在第 0 步——标签空间不成立。#117 的次序（先修 label space，再谈优化器）在方法论上是对的；教程真正可搬的是「小模型足以承载分类」与「Gold 攒够后 `BootstrapFinetune` 是 GEPA 平台期之后的下一根杠杆」。
7. **成熟实践（通讯社标准、金融分析供应商、事件编码体系）在三件事上高度收敛**：多轴正交（subject ≠ genre ≠ 地理 ≠ 实体 ≠ 证据状态）、稳定 ID + 版本化 codebook、实体/地理用词表而非类目枚举。#117 的 `news_taxonomy_v1` 与这三条一致（详见第 4–5 节，含来源）。
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

（终稿填充：IPTC Media Topics 规模/层级/更新节奏/ID 稳定性与 codebook 形态；NewsML-G2 的 item/event/concept 身份分离；AP/Reuters 的人工索引与自动分类混合治理；multi-label 与弃权惯例。）

### 4.2 金融新闻分析供应商（RavenPack / LSEG-Refinitiv / Bloomberg）

（终稿填充：event taxonomy 结构与规模、per-entity relevance、novelty（ENS/linked counts）、实体永久 ID、taxonomy 版本化与准确率声明；以及「彼此独立却收敛到同一组输出字段」的清单。）

### 4.3 政治/冲突事件编码与学术数据集（CAMEO / PLOVER / ACLED / ACE / MAVEN / 金融事件抽取 / FOMC）

（终稿填充：actor–action–target + geo 的分解方式与 actor 字典；ACLED 对持续冲突的 episode 表达；标注一致性与数据量级；Trillion Dollar Words 的 Fed 鹰鸽分类设置。）

---

## 5. 启示：能否彻底改善？

### 5.1 判决

（终稿填充：以 4.x 的收敛事实为据，给出「结构性改善可达；『彻底』取决于 Gold 投入、actor/geo 轴补全与弃权/校准纪律」的完整论证。）

### 5.2 「宏观下也有重点」：美联储、俄乌在 #117 下怎么表达

先说 #117 已经给到的：`macro_policy_data` 与 `geopolitical_conflict` 在 `event_family` 分家——「美联储加息」与「俄乌前线变化」不再共享一个 `macro` 桶；`subject_codes` 若把货币政策/央行类 IPTC 节点纳入钉选集，Fed 主题可以被稳定标出；`change_state` 区分「预告的 FOMC 决议」与「意外表态」。

还缺的（推断，待 4.3 佐证后成为建议）：「哪家央行、哪场冲突」是**身份**，不是**类目**。成熟事件编码体系全部把「谁/在哪」做成受控词表（actor/geo 字典）而非扩类；我们的对应物是：(a) 给宏观主体与地缘对一个小的钉选词表（如 `actor:us_fed`、`geo:ru-ua`），挂在 grounding/assets 同层而不是 `event_family`；(b) 用已有 storyline 机制承接「持续焦点」——俄乌是一条长 storyline 上的 episode 流，不是一个类。这两件事都是 `news_taxonomy_v2` 的候选，不应塞进 #117。

### 5.3 逐轴对照：我们已有 / #117 将有 / 仍缺

（终稿填充：与 4.x 的字段级对照表——entity+relevance、novelty、event type、confidence/abstention、版本化、人机混合治理。）

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
2. **起步量照 #117 的地板**：boundary ≥30、retention ≥100、negative ≥50、`product_service_change` ≥30、`financial_results+guidance_outlook` ≥30（cluster 计）。本文补充建议（推断）：要让 per-class gate 有裁决力，每个 `event_family` 的评估 support 至少 30–50 cluster；将来若做 finetune，量级要到 500–2,000。
3. **困难样本池现网已经免费攒好**：592 行 `product_progress` 通道错配（本文 1.2b）、`product` 的 m1/m2 边界带（471 行 m1）、confirmed↔rumor 翻转、`other/unknown` 候选。按 cluster 去重后，这就是第一批标注队列——比随机抽样的单位标注信息量高得多（不确定性采样的现成形态）。
4. **Codebook 即 Prompt**：#117 要求每个值有 plain-language 定义、正例、反例、边界例，IPTC 钉选节点带上游版本与 digest。这份 codebook 同时是标注指南与 EventSemantics instruction 的素材——写一次，两处用。
5. **外部数据集的角色**（推断）：Banking77 之类只用于方法论校准（管线通不通、metric 写得对不对），不是我们契约的真值；Fed 鹰鸽等领域数据集（见 4.3）可作子任务参考。领域分布、中文读者契约与我们的 evidence 形态都无法从外部语料迁移。

（终稿补充：标注一致性目标——kappa 阈值与 adjudication rate 的行业参考值，见 4.3。）

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

## 附录 B：外部来源清单

（终稿填充：4.x 全部一手来源链接。）
