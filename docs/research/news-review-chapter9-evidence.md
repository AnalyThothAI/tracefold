# News 复盘链路与持续进化：Chapter 9 证据审查

> 研究日期：2026-08-21
> 范围：`ai-agent-book` Chapter 9 正文、配套本地源码与保存的实验结果；Tracefold 当前 News V3 的 prompt、评价、发布门和价格复盘实现；必要的一手论文与官方工程资料。
> 本文是架构证据稿，不包含对生产配置的写入，也不把最近 24 小时的行情反应当成因果标签。

## 0. 结论先行

1. **Tracefold 不是缺一个“会反思的 Agent”，而是缺一个能评价并发布 Triage prompt 候选的离线 instrument。** 当前 `decide()` / `news.policy` 已有按时间顺序重建 ledger 的 release gate，而且代码主动声明“冻结 verdict 的 gate 只能评 policy，不能评 prompt”；这是最明确的架构断点。证据：`src/tracefold/news/eval/harness.py:1-19,285-344,433-471`。
2. **当前“命中复盘”是观察面，不是学习闭环。** 它把未送达事件按 1H 绝对波动排队，并明确“不证明因果、不写 label”；方向命中则只是 bullish 对应正收益、bearish 对应负收益。这个克制是正确的，但它不能告诉系统该改 Gate、Triage prompt、`decide()` 还是 delivery。证据：`src/tracefold/news/price_repository.py:557-626,652-657,801-845`；`web/src/features/news/ui/review/NewsReviewPage.tsx:23-32,74-110`。
3. **Chapter 9 的成熟内核不是“自动改 prompt”，而是在线执行与离线进化分离、外部验证、最小候选、保留集/边界集/安全集、灰度和回滚。** “反思”只能提出假设，不能自证。证据：`/Users/massis/Documents/Code/ai-agent-book/book/chapter9.md:19-29,102-124,265-306,321-341,382-390`。
4. **书中代码应当当作机制骨架，不应直接复制成生产系统。** 本地证据诚实保留了小样本、未复现与负迁移：9-1 只有 8 个虚构样本且一个维度失败召回为 0；9-2 的知识文档组反而从 50% 降到 25%；9-3 只有 5 个保留例和 5 个边界例，而且 Coding Agent 的提示里已经写死目标规则。证据：`/Users/massis/Documents/Code/ai-agent-book/chapter9/README.md:19-35`；`/Users/massis/Documents/Code/ai-agent-book/chapter9/trajectory-verifier/validation/latest.json:18-26,4635-4671,4725`；`/Users/massis/Documents/Code/ai-agent-book/chapter9/gaia-experience/validation/latest.json:2587-2610,9663-9667`；`/Users/massis/Documents/Code/ai-agent-book/chapter9/prompt-auto-optimization/airline_env.py:247-321`；`/Users/massis/Documents/Code/ai-agent-book/chapter9/prompt-auto-optimization/coding_agent.py:96-106`。
5. **遵循 KISS 的正确方向是保留单次 Triage 调用，不恢复 Analyst lane，不在生产流量上增加“复盘 Agent”。** 先补：多维人工 rubric、prompt 双臂重跑、候选 manifest、独立发布门、shadow/canary。OpenAI 的官方 eval 指南也建议以任务特定、真实分布的数据持续评价，自动评分须以人工校准，并优先用分类/成对比较而非含糊总分；复杂 Agent 架构应由 eval 证明其必要性。[OpenAI Evaluation Best Practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)

因此，目标架构不是：

```text
生产事件 -> Triage Agent -> Reviewer Agent -> Prompt Writer Agent -> 自动覆盖生产 Prompt
```

而是：

```text
在线：事件 -> 单次 Triage -> 确定性 decide -> 投递
             \-> 不可变输入、verdict、ledger、规则、投递与反应证据

离线：证据 -> 分层评价 -> 归因到 owner -> 最小候选
             -> 开发/边界/保留/安全集 -> shadow -> canary -> 人工批准/回滚
```

## 1. Chapter 9 到底给出了什么机制

### 1.1 “保存”不等于“学习”

模型推理不会跨会话自动改变参数；轨迹库也不会自动完成成功/失败对照、因果归因和迁移验证。Chapter 9 把学习定义为一个模型外系统：记录证据，验证结果和过程，跨轨迹归纳，再选择更新载体，最后验证候选。证据：`/Users/massis/Documents/Code/ai-agent-book/book/chapter9.md:3-17`。

对 Tracefold 的含义：PostgreSQL 中有 raw item、event、verdict、trace、delivery 和 reaction，只说明**证据可回查**；只有某个改变在独立数据上改善声明指标、且不破坏保留能力，才说明发生了学习。

### 1.2 评价先于总结，且评价要分层

书中将 verifier 分成三层：

| 层 | 回答的问题 | Tracefold 可用证据 | 适合实现 |
|---|---|---|---|
| 环境/结果真值 | 事情是否真的发生、卡片是否真的到达 | provider frame、DB 状态、delivery terminal、价格 candle 覆盖 | SQL / 代码 |
| 过程/政策 | 是否按允许的路径完成 | Gate admission、told ledger、`decide()` rule、throttle、重试/降级 trace | 代码 verifier |
| 开放质量 | 新闻值不值得推、标题是否忠实、机制句是否有用 | 原文、读者上下文、verdict 和人工 rubric | 人工；校准后才可由 LLM 扩量 |

结果正确不等于过程正确；开放质量也不应压成一个总分。每个结论需要具体证据，证据不足要允许 `uncertain`。证据：`/Users/massis/Documents/Code/ai-agent-book/book/chapter9.md:19-49`；本地三层 verifier 的接口与边界见 `/Users/massis/Documents/Code/ai-agent-book/chapter9/trajectory-verifier/verifier.py:18-31,75-105,108-263`。

这与外部一手资料一致：OpenAI 官方指南要求自动 judge 与人工评分校准，指出 position/verbosity 等偏差，推荐更明确的分类、pass/fail 或成对比较以及带实例的 scorecard，而不是开放式“感觉分”。[OpenAI Evaluation Best Practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)

### 1.3 失败要路由到正确的更新载体

Chapter 9 不是把一切都塞进 prompt，而是按可表示性分四类：

| 载体 | 适用问题 | Tracefold 例子 | 不该承载什么 |
|---|---|---|---|
| 知识/经验文档 | 有来源、会变化的事实和经跨案例验证的经验 | 来源规范、事件类型的审查手册 | 高频逐事件 raw 轨迹、硬门禁 |
| Prompt / Skill | 能用自然语言说明、需要语境与例外的判断 | actionable、magnitude、direction、headline/why 质量 rubric | SQL 可验证约束、权限、安全底线 |
| 程序 / Harness | 确定、重复、可测试的解析、状态、限制 | dedup、grounding、schema、长度、throttle、delivery 幂等 | 开放式“是否有交易价值” |
| 参数 | 高维隐式能力且样本/回归预算足够 | 未来可能的中文标题风格或领域分类器 | 当前稀疏 operator feedback 下的第一选择 |

证据：`/Users/massis/Documents/Code/ai-agent-book/book/chapter9.md:52-69,102-108,158-205,239-255`。默认选择应是**最小、最可归因、最易回滚的载体**；参数更新是最后手段，不是“成熟”的代名词。

### 1.4 在线执行与离线进化必须解耦

稳定 Agent 服务流量并记录证据；离线流程聚合同类失败、生成候选、独立验证和发布。候选不能修改批准自己的 verifier、测试集、阈值、审计日志和稳定备份。证据：`/Users/massis/Documents/Code/ai-agent-book/book/chapter9.md:265-306,321-341`。

这正是软件发布而不是“自省”：Google SRE 将 canary 定义为局部、限时的候选部署，要求有 control、评价过程和发布集成；对异步数据流水线，可以先用真实生产输入 dry-run、跳过生产写入，再比较 live 与 canary。[Google SRE Workbook: Canarying Releases](https://sre.google/workbook/canarying-releases/)；[Google SRE Workbook: Data Processing Pipelines](https://sre.google/workbook/data-processing/)

## 2. 本地 Chapter 9 例子：可复用机制与生产边界

| 实验 | 真正可复用的机制 | 保存的实证结果 | 为什么仍是教学/受控模式 | 证据 |
|---|---|---|---|---|
| 9-1 Trajectory Verifier | 环境真值 + 确定性过程规则 + 仅开放质量交给 judge；逐维保存 evidence/confidence；高风险/低置信度转人工 | 8 个虚构案例、4 类场景；总标签一致率 0.929，但 `compliant_flexibility` 失败召回为 0，部分确定性维度 precision 仅 0.667，且结果文件标记不是所有正文主张都复现 | 样本极小；`quality_facts` 是离线样例预置，README 明说生产不可直接拥有 | `/Users/massis/Documents/Code/ai-agent-book/chapter9/trajectory-verifier/README.md:1-17,53-55`；`/Users/massis/Documents/Code/ai-agent-book/chapter9/trajectory-verifier/validation/latest.json:18-26,4635-4671,4725` |
| 9-2 GAIA Experience | 不可变轨迹 -> 单次分析 -> 跨轨迹支持/反驳 -> 文档 -> 独立迁移集；失败轨迹也保留 | 无经验 2/4、单轨迹摘要 2/4、知识文档 1/4，知识文档负迁移率 0.25 | 结果证明“更长的经验文档”可能更差；检索和迁移必须单独过门 | `/Users/massis/Documents/Code/ai-agent-book/chapter9/gaia-experience/README.md:1-42,57-77`；`/Users/massis/Documents/Code/ai-agent-book/chapter9/gaia-experience/validation/latest.json:2587-2610,9663-9667` |
| 9-3 Prompt Optimization | 失败 case ID -> 结构化诊断 -> 精确 `old_str -> new_str` 最小 diff -> candidate manifest -> 边界改善、保留不退化 -> 只允许 canary | 初始 5/5 保留、0/5 边界；自动 5/5、2/5；人工 5/5、4/5；自动候选只到 `release_to_canary` | 仅 10 个例；发布门只要求任意边界改善；Coding Agent system prompt 已写死“只在明确人工请求/安全事件转接”的目标，因此主要验证编辑与发布协议，不是自主发现根因 | `/Users/massis/Documents/Code/ai-agent-book/chapter9/prompt-auto-optimization/README.md:1-18,69-112`；`/Users/massis/Documents/Code/ai-agent-book/chapter9/prompt-auto-optimization/coding_agent.py:96-106,124-189`；`/Users/massis/Documents/Code/ai-agent-book/chapter9/prompt-auto-optimization/release_gate.py:8-54`；`/Users/massis/Documents/Code/ai-agent-book/chapter9/prompt-auto-optimization/airline_env.py:247-321` |
| 9-6 / 9-7 Self-modifying Harness | 重复、确定、可验证的错误改程序而非加 prompt；candidate 隔离；AST/沙箱；失败回放、保留回放、安全检查、canary、rollback；trusted root 不可写 | 9-7 的真实 LLM 候选未过门而被安全拒绝，确定性对照通过 | “生成失败、门禁拒绝”本身是系统正确行为；不能拿一次被接受的补丁证明长期收益 | `/Users/massis/Documents/Code/ai-agent-book/chapter9/self-modifying-agent/README.md:1-21,49-57`；`/Users/massis/Documents/Code/ai-agent-book/chapter9/harness-safety-gate/README.md:30-51` |
| 9-9 Longitudinal Evaluation | 反馈在 action 之后暴露；static / append-only / evolving 三臂；迁移、替换、遗忘、负迁移、安全、激活/遵循、成本分开报告 | 3 seeds × 14 个顺序任务；append-only 能迁移但不会替换过期规则，evolving 能替换并保持 | 环境动作词和学习信号是固定、外部提供的合成任务；它证明 harness 指标方向，不等于真实 News 自主进化 | `/Users/massis/Documents/Code/ai-agent-book/chapter9/self-evolution-eval/README.md:1-18,44-76`；`/Users/massis/Documents/Code/ai-agent-book/chapter9/self-evolution-eval/harness.py:23-142` |

两个额外的诚实边界也很重要：Chapter 9 总索引把这些实现称为 skeleton；9-1/9-2/9-3 的 `latest.json` 缺顶层源码/证据 hash manifest，可审计强度低于 9-6/9-9。证据：`/Users/massis/Documents/Code/ai-agent-book/chapter9/README.md:7-13,29-35`。

Reflexion 展示了把语言反思放入 episodic memory、用任务反馈改善后续尝试的研究路径，但它并没有推翻上述边界：在生产系统里，反思文本是候选假设而不是环境真值。[Reflexion 原论文](https://arxiv.org/abs/2303.11366)；Chapter 9 对此也明确限定，见 `/Users/massis/Documents/Code/ai-agent-book/book/chapter9.md:81-93`。

## 3. Tracefold 当前架构：已经成熟的部分

### 3.1 在线执行边界总体是对的

- Triage 是一次 structured call；`decision` 只是模型意图，最终由纯 `decide()` 决定；模型失败有可命名的降级路径。证据：`docs/ARCHITECTURE.md:369-410,443-479`。
- 没有第二个 Analyst 模型调用；一个 Event 只有一个 judgment 和一张 card。证据：`docs/ARCHITECTURE.md:481-485`。
- verdict 保存 model/final decision、rule、throttle、degraded、prompt/input SHA、前后 status、实际 told ledger、re-ask 等 trace，已经具备离线重跑所需的大部分输入。证据：`docs/ARCHITECTURE.md:462-479`。
- prompt 文本和 structured-output schema 分别固定 SHA，并按版本保留历史 hash，解决“同一版本名对应不同字节”的审计问题。证据：`src/tracefold/news/agents/prompts/__init__.py:185-224`。
- `TriageVerdict` 逐字段保留 novelty、asset、direction、magnitude、actionable、decision、audience、中文文本，而不是只有一个综合分。证据：`src/tracefold/news/models.py:67-122`。

这些都应保留。所谓“高维重构”不应破坏这条单调用、确定性门控、可追踪的深模块边界。

### 3.2 Policy release gate 已接近 Chapter 9 的正确形态

`freeze_corpus()`、`replay_corpus()`、`validate_candidate()` 已经实现：

- 同一冻结 corpus 上 stable/candidate 双臂；
- 按事件时间顺序重建 storyline window 与 reader ledger，而不是只翻转单张卡；
- boundary=`must_push` 必须改善/不得丢失，retention=`good` 不得退化，noise 不得新增；
- duplicate evaluator 使用 `decide()` 不读取的 3-gram containment，避免用被测规则给自己打分；
- trusted-root hash 可被检查。

证据：`src/tracefold/news/eval/harness.py:41-57,83-133,285-418,433-545`；操作与 trusted-root 约束见 `docs/DEVELOPMENT.md:123-164`。

这是应该**复用接口思想**而不是重写的部分。

### 3.3 Price Review 的定位是克制且正确的

Reaction 指标版本化、可重建，固定 5 分钟 candle 对齐、gap tolerance 和 1H/4H horizon；页面先报 coverage，再报命中率；potential miss 只是一条人工检查队列。证据：`src/tracefold/news/pricing.py:1-41`；`src/tracefold/news/price_repository.py:557-626,652-657`；`web/src/features/news/ui/review/NewsReviewPage.tsx:23-32,74-110`。

这里的问题不是实现“太玩具”，而是若把它升级为自动 reward 就会越界。经典事件研究要用相对正常表现的 **abnormal return** 来估计事件影响，而不是把事件后的 raw return 直接等同于因果；即便做了 event study，也仍需处理共同消息、市场因子、事件聚集等识别问题。[MacKinlay, *Event Studies in Economics and Finance*（作者机构 PDF）](https://www.bu.edu/econ/files/2011/01/MacKinlay-1996-Event-Studies-in-Economics-and-Finance.pdf)

## 4. 当前最关键的断点与偏差

### P0-A：Policy 能评，Prompt 不能评

当前 release gate 的模块注释明确写着：verdict 冻结，因此只能评 `decide()`；prompt candidate 需要另一套 instrument。证据：`src/tracefold/news/eval/harness.py:7-19`。`offline.replay_decisions()` 同样只在 stored verdict 上重跑规则。证据：`src/tracefold/news/eval/offline.py:1-6,137-197`。

这意味着修改 prompt 虽然会触发 SHA/version pin 测试，却没有回答以下问题：

- 同一 raw Event 和同一 told ledger 下，新 prompt 的 assets/direction/magnitude/actionable/decision/text 是否更好；
- candidate 改变前一张卡的投递后，后续 told ledger 和 novelty 会怎样改变；
- 新版本是否减少目标错误，又没有增加漏推、噪声、错误方向、事实删减、重复与 token/延迟成本。

SHA pin 证明“是什么版本”，不证明“版本更好”。当前源码自己记录了 prompt 在 32 小时内烧过 8 个版本；这正说明 evaluator 的优先级高于继续加 prompt 规则。证据：`src/tracefold/news/agents/prompts/__init__.py:209-215`；`src/tracefold/news/eval/harness.py:7-11`。

### P0-B：Operator label 是单标签 outcome，不是可归因 rubric

目前 label payload 的核心是 `label + note`，同一 operator/event/version 会被纠正覆盖，并支持没有 Event 的 miss；这使 recall 上界可被观察，是优点。证据：`src/tracefold/news/repository.py:815-856`。

但离线评价把 `good / wrong_direction / late / missed / must_push` 全部折成 `moved`，把 `noise / dup` 折成 `flat`。因此：

- `wrong_direction` 会提高“moved”而不是形成 direction 失败维度；
- `late` 无法区分 provider 晚、队列慢、模型慢、delivery 慢；
- 一条新闻可能同时是“该推但标题删事实、方向错、why 无机制”，单标签无法表达；
- note 不能稳定聚合、校准 evaluator 或建立 regression case。

证据：`src/tracefold/news/eval/offline.py:51-98`。CLI 虽支持更多标签，详情 UI 只暴露 `good/noise/missed/must_push` 四个复制命令。证据：`web/src/features/news/ui/detail/NewsEventDetailPage.tsx:422-461`。

### P0-C：未标注数据不约束候选，最近 24 小时容易产生选择偏差

`freeze_corpus()` 明确规定：未标注 case 默认 `may_push`，不约束候选；Gate-suppressed 且没有 verdict 的事件也无法进入 policy replay。证据：`src/tracefold/news/eval/harness.py:125-133`。

所以“复盘最近 24 小时”若只看已推卡和价格波动最大的 held 卡，会系统性漏掉：

- 没有 ticker、没有可定价 instrument 但应推的宏观/地缘事件；
- 市场没立即动、但读者仍应知的结构性事实；
- 流水线根本没建 Event 的 miss；
- 普通、无波动、正确被丢弃的负样本；
- operator 没注意到的标题/方向/why 质量问题。

因此，24H 只能作为**发现集**：全量 pushes + 按 admission/rule/event_type 分层抽样的 held + eventless misses + 随机负样本。候选生成后必须冻结一个它没见过的未来时间窗作 temporal holdout；不能用同一 24H 既发现规则又批准规则。

### P1-A：Prompt 是一个过载的单体协议

当前约 182 行的 system prompt 同时承担 topic/value filter、asset grounding、event type、magnitude、direction、actionable、model intent、价格异动特例、中文标题、why、禁词、injection 防护、分类示例和 novelty ledger 判断。证据：`src/tracefold/news/agents/prompts/__init__.py:14-181`。一次 schema 又同时输出这些字段。证据：`src/tracefold/news/models.py:67-122`。

这不是说要拆成多个在线 Agent；那会重复读取同一 Event，引入阶段间不一致、额外延迟和更难校准的错误面。更小的改法是：

1. 源码中把 prompt 组合成有 owner 的 byte-frozen sections，最终仍发送一个 system message；
2. 可完全判定的约束（schema、字符长度、empty sentinel、固定 enum、明确禁词）移到代码 validator；
3. prompt 只保留必须依赖语境的 semantic rubric 和少量反例；
4. 每个 section 有独立边界/保留 cases，候选只改一个 section。

### P1-B：价格 raw sign 是弱代理，不能成为方向或“该不该推”的真值

当前 direction section 直接计数 `bps_1h > 0` / `< 0`，再将 bullish-up 或 bearish-down 视为 hit；潜在漏推按 `abs(bps_1h)` 排序。证据：`src/tracefold/news/price_repository.py:579-626,801-845`。

它适合回答“事件之后市场怎么走、哪些 held 值得人看”，不适合回答“模型方向为什么错”或“事件造成了走势”。最低限度也应先引入相对 BTC/指数/行业的 abnormal-return 观察、事件重叠标记与 dead zone；但即使如此，它仍只能作为 reviewer evidence，不自动写 operator label。事件研究的一手方法边界见 [MacKinlay](https://www.bu.edu/econ/files/2011/01/MacKinlay-1996-Event-Studies-in-Economics-and-Finance.pdf)。

### P1-C：模型 `confidence` 尚不是校准信号

schema 要求 0–1 confidence，但代码搜索显示它主要被存储/展示；`decide()` 没有用它作门槛。证据：`src/tracefold/news/models.py:106-113`；`src/tracefold/news/repository.py:1404`；`src/tracefold/news/timeline.py:168`；`web/src/features/news/ui/detail/NewsEventDetailPage.tsx:305`。这比盲目用自报信心更安全；下一步不是“接上阈值”，而是先用人工 rubric 画 reliability/calibration，再决定它是否有信息增益。Chapter 9 也明确说模型自报 confidence 不能充当批准门槛：`/Users/massis/Documents/Code/ai-agent-book/book/chapter9.md:321-329`。

### P1-D：架构文档与实际代码已有漂移

`docs/ARCHITECTURE.md` 的 learning-plane 段仍写着“没有 price-reaction lane”，而当前 `pricing.py`、`price_repository.py` 和 `NewsReviewPage.tsx` 已完整实现 Reaction/Review；同一段对 label/learning surface 的描述也落后于当前 `must_push` 与 release gate。证据：`docs/ARCHITECTURE.md:530-549`；`src/tracefold/news/pricing.py:1-41`；`src/tracefold/news/price_repository.py:519-657`；`web/src/features/news/ui/review/NewsReviewPage.tsx:23-110`。

这不是运行 bug，但会让后续 Agent 从错误的架构地图出发，继续把“学习面”和“价格观察面”混在一起。持续进化首先要求 evidence contract 与 owner map 可追溯；因此在实现新链路前，应把 canonical architecture 对齐实际代码，但不要把 Price Review 改写成 learning truth。

## 5. KISS 下的成熟最小闭环

### 5.1 证据合同：复用现有 trace，只补 review schema

不新建长期运行的“学习服务”。先在现有 `news_event_labels.label` JSON 中引入 versioned rubric（例如 `news_review_v2`），允许每个维度独立为 `pass / fail / uncertain / not_applicable`：

```json
{
  "should_push": "pass",
  "novelty": "fail",
  "asset_grounding": "pass",
  "direction": "uncertain",
  "magnitude": "pass",
  "timeliness": "fail",
  "headline_fidelity": "fail",
  "why_value": "pass",
  "duplicate_of": "event-id-or-null",
  "first_bad_owner": "triage_prompt",
  "evidence": ["原文含 25.8%，headline_zh 删除该数字"],
  "reviewer": "operator"
}
```

该 shape 是建议，不是要求立即迁移表。现有 JSONB 与 correctable label identity 已能承载版本化 payload：`src/tracefold/news/repository.py:815-856`。

建议的 News rubric：

| 维度 | 评价问题 | 主要证据 | 首要 owner |
|---|---|---|---|
| should_push | 这条事实此时值得占用 reader budget 吗 | 原文、目标读者、当时 ledger | Gate / Triage / policy，需 reviewer 归因 |
| factual/headline fidelity | 标题是否忠实保留主体、动作、数字、条件 | raw headline/body 对 `headline_zh/title_zh` | Triage prompt；长度/schema 属代码 |
| asset grounding | primary/mentioned 是否真是事件对象 | provider tags、原文、instrument universe | Gate 或 Triage |
| novelty | 是新事实、进展还是复述；匹配哪张 told card | 当时实际 told ledger | Dedup / storyline / Triage |
| direction | why 中的机制是否支持方向 | 原文、why、方向；价格只作弱证据 | Triage prompt |
| magnitude | 影响程度是否符合稳定 ordinal rubric | 原文、event type、已审 examples | Triage prompt / policy threshold |
| timeliness | 延迟发生在哪一段 | provider/opened/model/delivery timestamps | receiver/queue/model/delivery |
| why value | 是否增加原文没有的、受证据支持的机制 | 原文、why、reader card | Triage prompt |
| duplicate/budget | 读者是否已经收到同一事实 | dedup family、told ledger、相似度证据 | dedup/storyline/policy |
| delivery | 应送达的卡是否终局送达且只尝试一次 | delivery terminal 与 error code | delivery/harness |

每个失败只记录**首个可操作 owner**，避免同一 case 同时触发四个组件的无边界修改。τ-bench 用最终数据库状态验证任务，并用 `pass^k` 衡量多次运行的一致性，说明 agent eval 不应只看一次成功。[τ-bench 原论文](https://arxiv.org/abs/2406.12045)

### 5.2 Reviewer 的分工

1. **代码 verifier 先跑**：schema、字段长度、数字/实体保留的可判定部分、grounded tag 规则、ledger 索引、decision trace、delivery、latency、cost。
2. **人工 reviewer 处理高价值开放维度**：should_push、direction mechanism、magnitude、headline fidelity、why value；采用盲化 stable/candidate 成对比较，并允许 tie/uncertain。
3. **LLM judge 只负责扩量**：先在人工集合上按维度校准 precision/recall；低置信度、分歧、高风险和候选造成的 decision flip 仍回人工。

不要让生成候选的模型批准候选；也不要仅因为换了另一个模型就称为“独立”。独立性来自它读取的证据、rubric、权限和 release decision 不受 candidate generator 控制。书中 9-7 的真实 LLM 提案被模型外门禁拒绝，就是该原则的正例：`/Users/massis/Documents/Code/ai-agent-book/chapter9/harness-safety-gate/README.md:30-51`。

### 5.3 Prompt candidate evaluator：两层重跑

新增一个离线 CLI/deep module，而不是新 worker：

#### 第一层：逐事件 semantic replay（先做）

- 冻结 raw event、gate facts、当时 human input/status/told、model snapshot、inference 参数；
- stable prompt 与 candidate prompt 在相同输入上各跑至少一个 arm；对高方差 case 用重复运行报告一致性；
- 逐字段比较 schema validity、asset、novelty、direction、magnitude、actionable、decision、headline、why、tokens、latency；
- 只要候选改变 declared target 之外的字段，就把它列为 side effect，而不是被一个总分吞掉。

这一层可直接利用 Triage 当前已经构建并 hash 的输入：`src/tracefold/news/agents/triage_model.py:93-191,218-330`；prompt/schema pin：`src/tracefold/news/agents/prompts/__init__.py:185-224`。

#### 第二层：顺序 reader replay（只对可能发布的候选做）

如果 prompt 会改变 `decision / novelty / headline_zh / storyline primary`，前一张卡会改变后一张卡看见的 ledger。此时两个 arm 都必须按时间顺序：

```text
build arm-specific status -> call stable/candidate Triage -> final key -> decide
-> update that arm's delivered ledger -> next event
```

不能把 current policy gate 的 frozen verdict 误用到这里；该模块已明确承认这个限制：`src/tracefold/news/eval/harness.py:13-19`。可以复用其 corpus、sequential ledger、independent duplicate scoring 和 `ReleaseDecision` 思想，但不复用 frozen verdict 假设。

### 5.4 候选 change contract 与发布门

每个 prompt 候选必须是一个不可变 artifact，而不是覆盖 `TRIAGE_SYSTEM_PROMPT`：

- source event/review IDs 与时间窗；
- 一句话根因与 owner；
- exact section diff；
- 要改善的唯一主维度；
- 预期副作用/不可退化维度；
- stable/candidate prompt SHA、schema SHA、model snapshot、inference config；
- development / boundary / retention / safety / temporal-holdout 数据 SHA；
- stable/candidate 逐例输出、judge 版本和人工分歧；
- rollback 版本。

最小 release gate：

1. candidate 非空、来源可追溯、只改声明 section；
2. declared boundary 明确改善；
3. 已送达的 `must_push` 与 `good` 无回退；
4. 无新增事实失真、错误资产、关键方向错误或 injection 服从；
5. noise/dup、reader volume、hourly peak 不突破预设 guardrail；
6. schema 成功率、tokens、P95 latency、degraded rate 不越界；
7. temporal holdout 仍改善，并报告每个比例的 N；
8. shadow 通过后才允许小流量/小时间 canary，任何 guardrail 退化自动回滚。

Google SRE 的 canary 一手指南强调 control、代表且可归因的指标、时间和流量受限、可回滚，并建议采用满足业务目标的最简单模型，而不是过度建模。[Google SRE Workbook: Canarying Releases](https://sre.google/workbook/canarying-releases/)

### 5.5 复盘周期：先报告/提案，后自动生成；永不自动批准

一个最小“sleep review”周期：

1. 冻结过去 24H discovery slice 与 trace；
2. 运行确定性 funnel/coverage/latency/duplicate 检查；
3. 生成分层 review queue，并由 operator 补多维 rubric；
4. 聚合重复失败，仅为有跨 case 支持的 cluster 生成最小候选；
5. 在不相交的 boundary/retention/safety/未来 holdout 重跑；
6. 输出候选 manifest 和 PASS/REJECT，不改 stable prompt；
7. 被接受的 case 加入 regression library；被拒 proposal 与负结果也保留；
8. 定期合并/删除被代码 validator 取代或被证据推翻的 prompt 规则。

证据：Chapter 9 的五步 sleep learning 与修剪原则，`/Users/massis/Documents/Code/ai-agent-book/book/chapter9.md:331-354`。

## 6. Prompt 的具体优化方向：先减职责，再调措辞

### 6.1 保持不动的边界

- 保留一个 Triage structured call；
- 保留 model intent 与 final `decide()` 分离；
- 保留 actual told ledger，且只让 novelty 用它做语义判断；
- 保留 prompt/schema SHA 与完整 trace；
- 保留 model failure 的 deterministic baseline；
- 保留 price 完全不进入 Gate/Triage/decide。

证据：`docs/ARCHITECTURE.md:369-479`；`src/tracefold/news/agents/prompts/__init__.py:1-5,166-181,185-224`。

### 6.2 优先搬到程序的规则

只搬“可完全判定”的部分：

- enum、required field、字符上限、`title_zh` empty sentinel；
- banned exact phrases、meta language、URL/emoji 等格式；
- headline 中明确数字是否全部保留，可先做 validator flag 而不是自动拒绝；
- `restates` 索引是否指向实际 told entry；
- direction 与 why 中固定反向词的冲突可作 reviewer flag，不宜直接改方向；
- provider tag / cashtag 的 grounding 与 security/injection 边界继续由代码和 prompt 双层防御。

这会让 prompt 留下真正需要模型的判断，而不是继续堆积 lint 规则。Chapter 9 对“确定、重复、可验证”的约束应进程序有明确论述：`/Users/massis/Documents/Code/ai-agent-book/book/chapter9.md:158-205`。

### 6.3 Prompt 仍应承载的语义

- “清晰、及时、可行动”的读者价值；
- event type / primary asset / scope 的上下文语义；
- direction 的机制推理；
- magnitude ordinal rubric；
- progression 与 restatement 的语义边界；
- headline fidelity 和 why 的信息增量。

但每段只保留少量能区分边界的正反例。新例子只有在一个复现失败 cluster 通过 candidate gate 后才进入 prompt；不能把每次线上抱怨追加为一条例外。

### 6.4 不建议现在做的事

- 不恢复 Analyst/reviewer 在线 lane；
- 不让模型读取最近 24H 后直接重写全 prompt；
- 不把 raw trajectory 或“成功经验”塞进向量库并自动检索；9-2 的本地负迁移已经说明这可能变差；
- 不因一日价格 hit rate 调 direction/magnitude；
- 不用模型自报 confidence 自动 fail-open/fail-close；
- 不做在线权重更新或 fine-tune；当前 label 量和 evaluator 成熟度远未支持；
- 不让 proposal generator 修改 rubric、golden cases、release threshold 或 stable hash。

后期若 evaluator、数据量和回归门已稳定，可以把 DSPy 或 GEPA 用作**离线候选搜索器**。DSPy 将 LM pipeline 声明成模块并针对显式 metric 编译；GEPA 从轨迹反思诊断、提出和测试 prompt 更新，并维护多目标 Pareto 候选。但它们只替代“提案搜索”，不能替代独立 verifier、temporal holdout、人工批准与 canary。[DSPy 原论文](https://arxiv.org/abs/2310.03714)；[GEPA 原论文](https://arxiv.org/abs/2507.19457)

## 7. 主要失败模式清单

| 失败模式 | 表现 | 护栏 |
|---|---|---|
| 自评自批 | 同一模型生成规则又用含糊 rubric 宣布改进 | 外部环境/代码优先；人工校准；candidate generator 无批准权限 |
| 24H 数据泄漏 | 用同一批失败生成 prompt，又用同一批证明提升 | discovery 与 temporal holdout 分离；候选生成后才冻结未来窗 |
| 价格奖励投机 | 学会只推高波动资产，漏掉重要但无 ticker/无即时波动的事实 | reaction 只排 review queue；should_push 由 reader rubric 判定 |
| 单标签压扁根因 | wrong direction、late、标题失真都被算成 moved | versioned 多维 rubric；记录 first bad owner |
| Prompt 规则淤积 | 修一个 case 加一条例外，规则冲突、token/延迟和遵循率恶化 | 最小 section diff；规则支持门槛；定期修剪；保留集 |
| 幸存者偏差 | 只看已推、已定价、operator 注意到的案例 | 全 pushes + 分层 held + 随机负样本 + eventless miss |
| 用被测规则打分 | similarity rule 用自身 similarity metric 宣布胜利 | 独立 evaluator；当前 policy gate 的 3-gram 做法应复用 |
| 非确定性被掩盖 | 单次 replay 碰巧通过 | 固定模型快照/参数；重复 trials；报告一致性和 N；参考 τ-bench `pass^k` |
| 模型漂移 | prompt 不变但 provider model 行为改变 | model snapshot 与 inference config 进入 artifact；持续 eval |
| 负结果丢失 | 同一失败 proposal 被反复生成 | 保留 REJECT manifest、失败原因与 superseded rules |
| validator 被修改 | Agent 降阈值或删测试让自己通过 | trusted root、hash、权限隔离、人工 review |
| before/after 假相关 | canary 与 control 输入/时段不同 | shadow 同输入双臂；canary 并行 control + absolute guardrail |

Chapter 9 对提示注入固化、候选/稳定隔离、可信根和负结果保留的风险有直接说明：`/Users/massis/Documents/Code/ai-agent-book/book/chapter9.md:308-329`。

## 8. 实施顺序建议

### Phase 0：先让复盘可归因

- 定义 `news_review_v2` 多维 rubric；
- 最近 24H 只做 discovery：全 pushes、分层 held、random control、eventless miss；
- 展示每个 cohort 的 N、覆盖率和未标注比例；
- 为每个失败标 `first_bad_owner`；
- 不改生产 prompt。

### Phase 1：补上 Prompt Candidate Gate

- 实现离线 stable/candidate semantic replay；
- candidate manifest 固定 prompt/schema/model/input hashes；
- 接入 boundary/retention/safety/temporal holdout；
- 盲化 pairwise reviewer；
- 输出 PASS/REJECT 与逐例 delta，不自动写 stable prompt。

### Phase 2：补顺序 replay 与 shadow

- 对通过第一层的 candidate 做 arm-specific ledger sequential replay；
- 用真实输入 shadow，禁止 production delivery；
- 观察 schema、latency、token、degraded、reader budget、duplicate、must-push regression；
- 人工批准小 canary，保留一键 rollback。

### Phase 3：证据足够后再自动提案

- 只有同一失败在多条独立轨迹中重复且 evaluator 稳定，才让 LLM / DSPy / GEPA 生成候选；
- 自动化边界停在 candidate artifact；
- 追踪 proposal validity、activation/adherence、held-out gain、regression、negative transfer 和维护成本，而不是只看最终 push 命中率。Chapter 9 的分层纵向指标见 `/Users/massis/Documents/Code/ai-agent-book/book/chapter9.md:279-306`。

## 9. 最终判断

从高维看，Tracefold 的在线 News pipeline 与 deterministic policy gate 并不是玩具：单次语义判断、确定性 final decision、完整 trace、幂等事实、顺序重放、独立 duplicate metric 和 trusted root 都是生产级方向。真正“玩具化”的风险集中在**把 dashboard 当 learning、把单标签当 evaluator、把 prompt pin 当 prompt validation、把 24H raw return 当 reward**。

最有杠杆的架构动作只有一个：**在现有 evidence plane 和 policy gate 旁边补一个同等级的 prompt candidate evaluator；在 evaluator 可信之前，所有“自主进化”只生成报告和候选，不改变生产能力。** 这既符合 Chapter 9，也最符合 KISS。

## 一手资料与本地证据索引

- 指定章节网页：[Agent 的持续进化](https://bojieli.github.io/ai-agent-book/book/chapter9/)
- 本地章节：`/Users/massis/Documents/Code/ai-agent-book/book/chapter9.md`
- 本地实验总索引：`/Users/massis/Documents/Code/ai-agent-book/chapter9/README.md`
- OpenAI 官方：[Evaluation Best Practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)
- Google SRE 官方：[Canarying Releases](https://sre.google/workbook/canarying-releases/)；[Data Processing Pipelines](https://sre.google/workbook/data-processing/)
- 原论文：[Reflexion](https://arxiv.org/abs/2303.11366)；[τ-bench](https://arxiv.org/abs/2406.12045)；[DSPy](https://arxiv.org/abs/2310.03714)；[GEPA](https://arxiv.org/abs/2507.19457)
- 事件研究原文：[MacKinlay, Event Studies in Economics and Finance](https://www.bu.edu/econ/files/2011/01/MacKinlay-1996-Event-Studies-in-Economics-and-Finance.pdf)
