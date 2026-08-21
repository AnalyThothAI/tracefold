# ai-agent-book 第 2–9 章导出的 Trading Case 架构

方案：A — radically minimal Interface

核查日期：2026-08-21（Asia/Taipei）

书籍基线：`bojieli/ai-agent-book@edaec1b725f2cd23504875df647318e0a1b0ca7c`

Tracefold 基线：`AnalyThothAI/tracefold@991d77854f47001cae6117212ba85d7b11b7a6c2`

本文完整精读 `book-zhtw/chapter2.zhtw.md` 至 `chapter9.zhtw.md`，再与 Tracefold 当前架构、安全、开发约束及 OpenTrade/Deep Agents 前置调研交叉分析。它是 [Issue #104](https://github.com/AnalyThothAI/tracefold/issues/104) 的研究依据，不实现代码，也不主张现有四个手工案例已经构成有效交易策略。

> **架构结论更新（2026-08-21）：** 后续需求明确要求主 DeepAgent 具有真实下单权限，因此本文“Agent 只能研究、不得持有订单写工具”和“V1 永不 live”的结论已经被替代。第 2–9 章的原文摘录、72 小时 episode/point-in-time 分析、风险与评估瓶颈仍作为历史研究依据；当前 KISS 决策、官方 Deep Agents 0.7.8 核查和直接下单示例见 [`deepagents-order-capability-best-practices.md`](deepagents-order-capability-best-practices.md) 与重写后的 [Issue #104](https://github.com/AnalyThothAI/tracefold/issues/104)。新决定是：主 Agent 独占 `prepare_trade_action/inspect_trade/place_order/cancel_order/close_position`，`live_bounded` 无逐单人审；凭证、mandate、fail-closed risk、原子账本和 OpenTrade Adapter 隐藏在这些工具的 Implementation 内。

## 结论先行

1. **需要 Deep Agent，但只需要它做深度研究。** 72 小时多事件、多来源、事实冲突、反方论证和长上下文任务，确实适合 Deep Agents harness；它不应成为交易架构的控制核心，更不能持有 OpenTrade 写工具。书中反复把真实动作归给 Harness，把模型输出视为候选动作，并要求高风险动作由权限、验证与人工确认兜底。[B2 L185–247][b2-loop] [B4 L212–302][b4-execution]
2. **外部 Module 应是一个深的 `TradingCases`，Interface 恰好三个入口。** `open_case`、`get_case`、`review_case` 覆盖触发、观察和授权；研究编排、proposal、确定性 risk、执行、reconcile、evaluation/evolution 全藏在 Implementation 内。没有 `execute()`，因为批准之后的执行是内部状态迁移，不是调用方可自由组合的第二套交易 API。
3. **最干净的 Seam 在“第一张卡已真实送达”之后，而不是 Triage 内部。** Tracefold 仍只有 News V3；不复活 Analyst/deep lane，不向 `tracefold.news`、News RabbitMQ topology 或 13 张现有表塞入 Trading。当前 `delivery.state='sent'` 才表示读者已收到，而 `push/escalate` 决策本身不表示送达。V1 的 owned Adapter 轮询只读 News HTTP 并以 `(event_id, kind='first')` 幂等触发独立 Trading Case。[TF Architecture L3–8, L69–76, L412–445] [OT report L26–40]
4. **Agent 只能产出 `Dossier`、`NoTrade` 或 `TradeProposal`，不能产出授权。** Proposal 必须再经过模型外、fail-closed 的 `RiskKernel`，绑定 server-truth account/position/market snapshot，形成 canonical digest，由 authenticated human 批准精确 revision，最后才可能进入执行。OpenTrade 远端 fail-open risk 只能算 defense-in-depth。[OT report L89–105, L222–235]
5. **V1 必须是 research-only + shadow/paper。** OpenTrade 没有公开的现金股票 broker 合同，也没有公开 CEX idempotency/paper 合同；MRVL、GOOGL、MRNA、SK Hynix 的现金股都应返回 `execution_unavailable:no_cash_equity_adapter`。Live write、自动自我进化、RL 和 cash-equity execution 都是 hard cut，不是假装完成的“后续小开关”。[OT report L17–24, L58–79, L91–105]
6. **交易学习是离线候选发布，不是线上自我修改。** 线上 Case 只附加证据与结果；离线进化流程才聚类、诊断、提出知识/Prompt/Skill/code/model 候选，并在边界集、保留集、安全集和 point-in-time replay 上验证。Risk、approval、execution、verifier、audit 和 release gate 是不可自改的 trust root。[B9 L263–320][b9-loop] [B9 L321–392][b9-safety]

## 1. 设计词汇与边界决定

本方案按以下含义使用 codebase-design 词汇：

- **Module**：`TradingCases` 是一个独立部署、独立持久化、独立失败域的业务 Module；它不是 `tracefold.news` 的子包。它的外部 Interface 是一个逻辑整体，但 Implementation 至少分成 Research 与 Execution 两个进程身份：Research 无资金写路由/凭证/包依赖，Execution 无 LLM、网页检索或 Deep Agents runtime。
- **Interface**：调用者必须学习的完整合同，包括三个方法、输入输出类型、状态顺序、不变量、错误和性能语义。
- **Implementation**：72 小时快照、episode 聚合、Deep Agents 编排、证据账本、proposal compiler、RiskKernel、approval digest、OpenTrade quirks、reconciliation、evaluation/evolution。
- **Seam**：未来变化或故障应停留的位置。主要 Seam 是 Tracefold sent-delivery read、官方来源、市场/账户数据、模型 harness、人工 review 和 execution venue。
- **Adapter**：Seam 上具体的实现，例如 `TracefoldNewsHttpAdapter`、`OpenTradeHttpAdapter`、`ReplayVenueAdapter`；Adapter 不拥有业务规则。
- **Depth**：调用者只面对三个稳定入口，却获得从 sent delivery 到审计、研究、paper execution、reconcile 和评估的整条能力。
- **Leverage**：一次 Interface 调用隐藏的复杂度越多、调用者需要知道的 upstream 细节越少，leverage 越高。
- **Locality**：OpenTrade schema 漂移只改 OpenTrade Adapter；Deep Agents 升级只改 ResearchHarness；risk rule 变化只改 RiskKernel 和其 release gate；News 不随这些变化。

### 当前必须保留的 Tracefold 边界

- Tracefold 是单一 News V3 bounded context；material fact 是 `news_items`，`news_events` 是单 writer、可重建 read model。[TF Architecture L3–8, L52–76]
- 当前 Workers 只有 News 六个消费者和 control；没有 acquisition clock、market poll、model arbiter 或第二业务 lane。[TF Architecture L195–203]
- 一个 Event 只有一次 Triage、一个 verdict、一个首卡；旧 Analyst、`q:news.deep`、follow-up card 已 hard cut。[TF Architecture L412–445]
- Triage 没有工具，模型 output 不能独自决定 delivery；`decide()` 才拥有最终决策。[TF Security L51–63]
- News 没有 market-mark/price-reaction lane；价格、账户、订单与回测数据必须由新 Module 的外部 Adapter 获得。[TF Architecture L461–480]
- GitHub Issue 必须先写清问题、observable outcome、不变量、hard cuts、实现边界和验证证据；新 service/table/worker/model control plane 必须有当前需要。[TF Development L5–24]

### 推荐部署形态

```text
Tracefold Serve (read-only News HTTP)
       |
       | only delivery.state=sent
       v
TracefoldSentDeliveryAdapter -----> TradingCases Module (owned store/queue)
                                         |
                          +--------------+---------------+
                          |                              |
                    ResearchHarness                Deterministic plane
                  (Deep Agents, read-only)     proposal/risk/approval/execution
                          |                              |
                sources + market reads           OpenTrade / replay Adapter
                                                         |
                                                     reconcile
                                                         |
                                               evaluation/evolution ledger
```

V1 采用只读 polling Adapter，而不修改 Tracefold delivery hot path。它记录高水位、反复分页并以 sent-delivery key 去重；轮询延迟是明确代价。若未来确实需要低延迟、无遗漏 outbox，必须另开 Tracefold architecture Issue，而不是让 Trading 直连 News DB 或私有 repository。

## 2. 第 2–9 章：核心机制与对 Trading Agent 的直接含义

### 第 2 章：上下文工程

核心机制：

- 模型看到的是 Harness 选择后的 observation，不是环境本身；同一模型的能力高度依赖累计 context 的质量。API 可无状态，但 Harness 必须保留任务所需状态。[B2 L7–35][b2-context]
- 模型只提出 tool call，Harness 才真正执行；生产 loop 必须有 iteration 上限和明确终止条件。[B2 L185–247][b2-loop] [B2 L305–330][b2-budget]
- 稳定 system/tool prefix、轨迹和动态状态应分层；滑动窗口会丢证据并诱发重复调用，压缩必须保留决定、约束、失败和引用。[B2 L434–446][b2-cache] [B2 L528–576][b2-cache2] [B2 L971–1084][b2-compress]
- Prompt 应描述可执行流程，不是无序“军规”；Skills 用渐进式披露，只在需要时装载完整说明。[B2 L616–663][b2-process] [B2 L739–807][b2-skills]
- 状态栏由代码维护计数、进度与限制，不让模型猜；外部内容可能提示注入，context 标记不等于安全边界，高风险动作仍需执行层权限和 HITL。[B2 L704–720][b2-injection] [B2 L832–969][b2-status]

对 Trading Agent 的直接含义：

- Case context 必须来自 immutable evidence manifest；每条资料携带 `source_kind`、`event_time`、`available_at`、hash、trust 和 citation，不把网页正文当指令。
- Deep research 分为受限子任务；每个 subagent 只收到自足的小上下文并返回 typed artifact，主 Agent 不携带所有 raw page。
- 每轮研究都附 model-external status bar：Case stage、剩余 tool/model/time budget、已核/冲突证据数、market/account freshness、risk/approval/execution 状态。
- Prompt、Skills、tool schema 和 model revision 全部版本化、hash 化；压缩后的摘要不能成为唯一证据。
- Agent context 永远不出现 OpenTrade write capability、secret 或可伪造的“已批准”标志。

### 第 3 章：记忆与知识

核心机制：

- 任务 trajectory 应 append-only；长期 memory 可修订；业务状态必须另存，不能让 checkpoint 或聊天记录冒充业务真值。[B3 L78–183][b3-memory]
- episodic、semantic、procedural memory 是正交层；append-only facts 加时间检索可避免错误更新不可逆，抽象规则时仍保留原 episode。[B3 L185–247][b3-types]
- dense、sparse、hybrid retrieval 与 reranker 各有角色，但 raw-case RAG 不能可靠计算总体分布或边界；需要结构化 summary/factor 和明确评估。[B3 L302–463][b3-retrieval]
- Graph/structured index 只在跨文档、多跳收益被验证后增加；文件知识更新应像 PR，有 proposer/reviewer、证据 diff、冲突与周期性 re-audit。[B3 L465–560][b3-governance]
- Agentic RAG 可以解决复杂查询，但检索结果不得直接触发风险动作；知识服务应保留 overview/detail 两层及 source/time context。[B3 L562–687][b3-rag]
- 稳健知识面分成 raw evidence、reviewed knowledge、serving index；索引是可重建派生物。[B3 L701–715][b3-summary]

对 Trading Agent 的直接含义：

- `EvidenceLedger`、`CaseTrajectory`、`CaseState` 三者分离：证据和 transition 不可变；thesis/knowledge 可版本化；当前 order/position 状态由账本和 venue observation 决定。
- 每个事件同时记录 `event_time` 与 `available_at`；回测、proposal 和 evaluation 都按 `available_at` 做 as-of，防止 look-ahead。
- 72 小时的多条相似推送先合并为 episode，不能把 MRNA 的催化剂、价格回声、衍生品上架和隔日回撤当 14 份独立支持。
- V1 使用 relational ledger、content hashes 和必要的 hybrid search；不先上 GraphRAG、通用记忆框架或“相似历史新闻即可交易”。
- 经验规则只能由多 episode 支持/反驳后进入候选知识；单个高收益案例不能升级为策略。

### 第 4 章：工具系统

核心机制：

- perception、execution、collaboration/event/user 工具风险不同；参数复杂、风险高的动作应使用 dedicated tool，而不是 generic executor。[B4 L12–70][b4-tools]
- 工具合同必须说明何时用、何时不用、性能和例子；Harness/Adapter 不得静默改写 argument 或 output。代码编排能让大块 raw data 留在 context 外。[B4 L72–110][b4-contract]
- MCP 适合标准化工具接入，不是 durable event runtime；感知工具需分页、显式 truncation、read-only cache/parallel。[B4 L112–188][b4-mcp]
- execution 需分层参数验证、权限、fast-fail、proposer/reviewer；sidecar 只能依据结构化 tool/args gate，不能读取“内心理由”。[B4 L212–270][b4-execution]
- irreversible call 要有 idempotency、取消与 ambiguous timeout 语义；外部 write 前检查、确认，timeout 后不能盲重试。[B4 L272–302][b4-idempotency]
- subagent 必须有 role、source、task boundary 和 structured output；工具/Skill 可渐进发现，但能力扩张仍由 Harness 控制。[B4 L317–421][b4-subagents]

对 Trading Agent 的直接含义：

- News、official-source、market、account 都是只读 perception Adapter；OpenTrade write 只由内部 `ExecutionKernel` 调用，不注册为 Deep Agent tool/MCP tool。
- `TradeProposal` 编译为 canonical order packet 时必须 byte/canonical exact；Adapter 不猜 symbol、hedged、quantity、side、order type 或 TIF。
- OpenTrade 的 POST timeout/crash 是 `execution_ambiguous`，不是 retryable error；先查 orders/trades/positions reconcile。
- Reviewer 可以挑战 thesis，不能批准风险；真正 approval 是 authenticated human 对 exact digest 的业务操作。
- Production 与 replay/fake Adapter 在真实外部 Seam 上成对存在；内部纯函数不为“可 mock”而制造浅 ports。

### 第 5 章：代码作为元能力

核心机制：

- Coding Agent 适合开放任务；固定垂直业务应优先稳定 workflow。成功依赖清晰目标、自动验证、rollback 和即时 structured feedback。[B5 L1–174][b5-workflow]
- 故障需要按 API/tool/context/control 分类；retry 要基于 typed cause，有 fingerprint、watchdog、breaker 和全局 budget。[B5 L176–250][b5-faults]
- “私有数据 + 不可信内容 + 外传能力”构成危险组合；sandbox 应默认隔离网络、文件、secret 和资源。[B5 L295–346][b5-sandbox]
- 代码可以表达确定性推理和业务规则；模型提供的 `expected_*` 只能做 checklist/audit，最终约束读 server truth。[B5 L348–496][b5-servertruth]
- proposer/reviewer 要有迭代上限；生成代码、SQL、parser 等 artifact 不能未经 sandbox/whitelist/验证直接执行。[B5 L499–701][b5-artifacts]
- 自举应从已验证 template/candidate 出发，而不是在正式环境从零改写。[B5 L703–753][b5-bootstrap]

对 Trading Agent 的直接含义：

- 整条交易链是确定性 state machine；代码 sandbox 只用于 research calculation/event study，不是订单编排语言。
- RiskKernel 从受信账户、持仓、未成交订单、market snapshot、mandate 和 clock 计算；模型的 confidence/size 建议不能覆盖硬约束。
- tool/network/model/DB 错误各有 typed policy；“多试几次也许成功”不是 execution strategy。
- 研究生成的 Python/SQL 运行在无 secret、限制网络/CPU/time/storage 的 sandbox；结果需连同输入 hash 和 verifier 保存。
- evolution 只能产生候选 diff/artifact，不能修改运行中稳定版本、risk、approval、verifier 或 release gate。

### 第 6 章：异步事件与持续运行

核心机制：

- 现实不是 turn-based；系统需要 wakeup、safe point、cancel/preempt、fast/slow path。事件要结构化进入 queue，而不是塞进聊天。[B6 L1–90][b6-async]
- 事件循环在 safe point 消费；queued、parallel、cancel 的语义不同。长任务应暴露 `initiate_* -> job_id`，结果由后续 event/查询取得。[B6 L109–173][b6-eventloop] [B6 L202–260][b6-jobs]
- 预测不能替代新 observation；执行每个真实动作后都要重新观测和验证，尤其在延迟和环境漂移下。[B6 L427–692][b6-observe]
- 可靠控制 skeleton 把异步模型工作与确定性外部动作分开。[B6 L704–721][b6-summary]

对 Trading Agent 的直接含义：

- `open_case` 和 `review_case` 只 durable-ack，不在请求线程等待 research 或 execution；`get_case` 读取当前 snapshot。
- sent delivery 是 durable wakeup；同一 key at-least-once 到达，由 CaseStore 幂等消化。Deep research 在 slow path，sent News delivery 不等待它。
- cancel 只在 commit point 前安全；一旦 execution intent durable、POST 可能已发，状态只能进入 reconcile/人工处置，不能假装取消。
- approval 后、submit 前必须重新读取 metadata、book、price、account、position、open orders 和 mode；成交后再观察 fill/position，而不是相信 proposal 的预测。
- background research 完成事件和 venue execution event 都只推动合法状态迁移；无序/重复 event 不改变结果。

### 第 7 章：评估与可观测性

核心机制：

- 被评估对象是 Model + Harness；固定 Harness 换模型与固定模型做 component ablation 回答不同问题。[B7 L1–25][b7-object]
- `Pass@k` 衡量多次至少一次成功，`Pass^k` 衡量每次都成功；高风险业务更关心 `Pass^k`、过程合规和安全 veto。[B7 L69–130][b7-metrics]
- 评估环境包含 dataset、state、tools、rubric、protocol；工具错误必须给可诊断反馈，交互信息应渐进透露。[B7 L132–218][b7-env]
- 数据集要精确定义初始状态和成功条件，参数化防记忆，覆盖边界/陷阱，并同时检查最终状态与过程；训练和评估隔离。[B7 L220–301][b7-dataset]
- Judge 要有自足 rubric、权重/veto、引用证据；先找 first error，再做 end-to-end 与 trajectory-prefix regression。[B7 L303–468][b7-judge]
- 模型选择要同时看 latency、cost、reliability 和 budget curve；比较必须用重复运行、paired analysis 和统计噪声，不能把小分差当进步。[B7 L508–590][b7-cost] [B7 L629–726][b7-stats]
- trace/span 记录模型、tool、retrieval、time、token、errors；bad cases 回流为 eval assets。功能要能独立 ablate、shadow、canary、kill。[B7 L655–726][b7-observe] [B7 L743–832][b7-release]

对 Trading Agent 的直接含义：

- 评价不能只看 P&L；同时验证事实、citation、no-trade、instrument identity、risk、approval、submit/reconcile、execution quality、cost 和安全。
- 安全 veto 包括：未验证来源下单、cash/perp 混淆、超 mandate、审批 digest 不匹配、重复 submit、ambiguous blind retry、secret 泄漏、look-ahead。任一出现即整条 episode 失败。
- 以 episode 而不是单条 provider card 计样本；MRNA 14 条相关推送不能把有效样本数虚增为 14。
- 保存完整 trace 和第一处错误；另外建立 trajectory-prefix 用例，问“此刻下一个允许动作是什么”，比只看终局更快定位 Harness 问题。
- model/prompt/retrieval/subagent/risk feature 必须可独立替换或关闭；promotion 用 paired frozen corpus、多随机种子/重复运行和显著性，不用四个手工故事。

### 第 8 章：后训练

核心机制：

- Mid-training 补稳定知识/基础能力，SFT 固化输出协议，RL 优化有可靠奖励且已有非零成功率的策略；数据和环境通常比算法更重要。[B8 L7–19][b8-map] [B8 L49–112][b8-sft-rl]
- 经常更新、需引用/权限/删除的事实应留在 RAG；先排除 Prompt、工具、程序约束能解决的问题，再考虑改权重。[B8 L275–321][b8-mid] [B8 L395–417][b8-choice]
- RL 环境必须可重置、并行、复现、接近真实世界；真实支付/交易不适合试错，模拟器偏差会被策略利用。[B8 L491–566][b8-env]
- reward 优先读取真实结果，过程约束只对可机器判定的动作；reward hacking/reward seeking 会把代理指标当目标。RLVP 的原则是 reward outcome、penalize verified bad path。[B8 L614–672][b8-reward]
- rollout 稀疏、分布/数值一致性和样本效率是 RL 核心瓶颈；蒸馏也受 teacher、state distribution 和 simulator fidelity 限制。[B8 L674–735][b8-distill]
- 生产 bad case 可以形成 eval、偏好对或候选训练数据，但 boundary set 与 retention set 缺一不可；训练不应替换 deterministic parser/check。[B8 L737–830][b8-practice]

对 Trading Agent 的直接含义：

- V1 不需要 Mid-training/SFT/RL。先用可编辑 Prompt/Skills、read-only tools、typed schema、RiskKernel 和 frozen eval 证明瓶颈真在模型参数。
- 动态金融事实、公司公告、临床试验状态、市场/账户状态永远留在 point-in-time evidence，不写进权重。
- 绝不在 live market 用真钱做 RL exploration；paper simulator 也要用真实延迟、spread、fee、funding、missing data 和 rejection 定期校准。
- 若未来训练，reward 不能只有收益；必须把合规 path veto、回撤、风险暴露、成交质量、重复率和 no-trade correctness 纳入，而且 verifier 不在模型修改权限内。
- `pass@k≈0` 或输出不可解析时，先修 capability/contract；没有可靠、不可钻漏洞的 reward 时，不启动 RL。

### 第 9 章：持续进化

核心机制：

- 保存轨迹不等于学习；正式环境反馈含噪声和攻击，必须先评价、对照、归纳、验证，再形成候选版本。[B9 L3–17][b9-opening]
- 三层 verifier 依次看环境结果、合规过程、开放质量；越靠近底层越应使用程序与环境真值，低置信度案例不进入学习集。[B9 L19–50][b9-verifier]
- 经验可更新到 knowledge、Prompt/Skill、program/Harness 或 model parameters；选择位置取决于能力最自然的表达载体，而不是“更聪明”的技术偏好。[B9 L51–112][b9-carriers] [B9 L152–241][b9-code]
- 候选应是最小、可证伪、可回滚的 change contract；失败证据、成功保留行为和被拒方案共同限定搜索空间。[B9 L183–199][b9-change]
- online execution 与 offline evolution 是双循环；candidate effectiveness、artifact activation、adherence、retention gain 要分开衡量。[B9 L243–296][b9-loop]
- 负面结果、不同质候选和人类高层判断必须保留；开放式研究“流程完成”不等于真实进步。[B9 L298–310][b9-open]
- evidence/instruction、candidate/stable 和 self-modifiable/trust-root 三重隔离；睡眠学习做离线整合、验证、审批、修剪与索引。[B9 L311–392][b9-safety]

对 Trading Agent 的直接含义：

- 线上 Case 永远 append evidence/transition/outcome，不根据单笔盈亏修改 prompt、risk 或模型。
- evolution 的默认顺序是：reviewed knowledge → scoped Skill/Prompt diff → deterministic Harness/code → 参数训练；能放进代码的风控永远不放进模型。
- 每个候选记录来源 episodes、first-error 归因、预计改善、可能退化、边界/保留/安全用例和 rollback version。
- RiskKernel、approval verifier、ExecutionKernel、reconciler truth rules、eval verifier、audit log 和 release gate 不可由研究 Agent 修改。
- 阴性结果、no-trade、错失、ambiguous execution 和 rejected candidate 与盈利案例同等可检索，防止 survivorship bias。

## 3. 完整链路：sent delivery → evolution

### 3.1 Sent delivery admission

1. `TracefoldSentDeliveryAdapter` 只读取公开 HTTP；只有 `delivery.state='sent'` 的 first card 可成为 live research trigger。
2. Adapter 保存 high-water mark，并把 `(event_id, kind, delivered_at, detail_hash)` 送入 `open_case`。重复 poll、进程重启或页面重排都返回同一 Case。
3. Recovery、held、terminal、sending、ambiguous delivery 不能冒充 sent。历史 72 小时请求必须冻结 manifest；它只能是 research/replay trigger，不能进入 live execution。
4. 在同一 economic episode 窗口中，后续 sent delivery 可作为新 evidence member 加入现有 Case revision，不能自动产生互相冲突的多个订单。

### 3.2 Evidence snapshot

1. 冻结 Event detail、member source、Triage verdict/trace、delivery timestamps、asset normalization、HTTP ETag/hash。
2. 记录 fetch start/end、server observation time、`published_at`、`opened_at`、`delivered_at`；可交易知识时间取实际 `available_at`，不取事后较早的 source timestamp。
3. 分页中 manifest 漂移可有限重抓；仍不稳定则 `news_snapshot_changed`，不形成 executable proposal。
4. 原始来源保留 hash/URI/observed_at；研究摘要不是原始证据替代品。

### 3.3 Research admission 与 Deep Agents

先由 deterministic `ResearchAdmissionPolicy` 合并 episode、检查 mandate、预算和已有 Case。简单重复、已被同 Case 覆盖或不在 mandate 的 Event 可以终止为 `research_not_admitted`；复杂、多来源、事实冲突、重点 issuer 或人工指定的 Case 才进入 Deep Agents。

ResearchHarness 显式配置三个窄 subagents，不使用默认 general-purpose：

| 角色 | 只读范围 | Typed output | 禁止事项 |
|---|---|---|---|
| `official_fact_verifier` | issuer IR/filing、交易所披露、ClinicalTrials.gov/监管源、Event detail | `FactSet(claim, evidence, available_at, primary, conflict, confidence)` | 不读 account，不建议 order，不把二级报道当一级证明 |
| `market_reaction_analyst` | point-in-time metadata/ticker/OHLCV/book/funding/OI、benchmark | `ReactionSet(pre, post, abnormal, liquidity, price_in, missing)` | 不用 cutoff 后数据，不把当前 metadata 回填历史 |
| `adversarial_thesis_reviewer` | 前两者 frozen artifacts | `ChallengeSet(alternative, disconfirming, invalidator, expression_risk, missing)` | 不重新自由搜索，不批准 proposal |

Supervisor 只综合 typed artifacts 为 `Dossier`，并给出 `NoTrade` 或 `ProposalCandidate`。两次 schema repair 后仍 invalid、关键 source conflict、citation 缺失或 budget exhausted 都允许产出 research report，但禁止 executable proposal。Deep Agents checkpoint 只负责 research resume；CaseStore 才是业务状态真值。[OT report L118–143, L277–301]

### 3.4 Proposal compiler

`ProposalCompiler` 把研究候选转换为确定性、可 hash 的 `TradeProposal`：

- `economic_exposure_ref`：issuer/underlying/share class/asset class；
- `venue_instrument_ref`：venue、instrument id、exact symbol、spot/perp/future/cash、settlement、expiry；
- direction、thesis horizon、entry rule、exit/invalidation、max holding period；
- order intent：side、type、quantity/quote cap、limit/trigger、allowed drift/slippage；
- liquidity、basis、funding、borrow、session/calendar、benchmark、data freshness；
- supporting/contradicting evidence ids、model/prompt/tool/policy versions；
- explicit `not_tradeable_reasons`。

Ticker-only mapping 永远不足。`MRVL` equity、`MRVLUSDT` perp 与任何 tokenized exposure 是不同 identity；`SKHY/SKHX/SKHYNIX` alias 也不能证明韩国现金股 execution。proposal compiler 不“找个最像的 symbol”。

### 3.5 Deterministic risk

RiskKernel 在模型外读取：

- code-owned mandate、allowed mode/venue/instrument/order types、notional/leverage/concentration/drawdown limits；
- fresh account balance、positions、open orders、position mode；
- fresh price/book/metadata/precision/minimum、trading session、funding/basis；
- Case exposure 和 portfolio-level correlated exposure；
- proposal exit/invalidation 与 approval tolerance。

任一必要输入缺失、过期、冲突、不可计算或 external provider degraded 都是 `risk_rejected`/`risk_data_unavailable`。OpenTrade 的 remote risk 不能放宽本地结果。Risk output 包含每条 rule 的 server inputs、结果、policy version 和 proposal digest，而不是一个不透明 score。

### 3.6 Approval

通过 risk 后创建 `ApprovalRequest`：

- canonical proposal digest；
- Case id/revision、authenticated operator、mandate/account/venue；
- 全部 order/size/price/exit/tolerance 字段；
- evidence/risk snapshot hash；
- server-owned expiry。

`review_case(Approve(...))` 只 durable-record review command。revision、digest、principal、mandate 或 expiry 不匹配即 `approval_conflict`。`RequestRevision` 只表达受约束的修改要求，Implementation 生成新 proposal、重新 risk、重新 digest；不能“人手改 JSON 后直接发”。

### 3.7 Execution

Approval 后 `ExecutionKernel` 自动：

1. fresh preflight；
2. material drift 则新 revision 回到 `AWAITING_APPROVAL`；
3. 在 CaseStore 先写 canonical intent、digest、attempt id 和 commit point；
4. 由非 LLM `OpenTradeHttpAdapter` 发送 exact order；
5. 保存 raw response hash、normalized receipt、remote ids；
6. 明确 reject 则 terminal；timeout/crash/unparseable response 则 `EXECUTION_AMBIGUOUS`。

V1 `paper` 使用 replay/paper Adapter，Production OpenTrade Adapter 只开放 read capability；任何真实 write 被构建时 gate 和 deployment deny。未来 live approval 也不新增 `execute()` 入口。

### 3.8 Reconcile 与 position lifecycle

- Reconciler 查询 orders、trades、fills、positions、balance，按 `attempt_id + canonical fingerprint + bounded time window` 关联，不凭一次 HTTP response 判定成交。
- ambiguous 只允许 reconcile 或 authenticated operator terminal action；禁止自动再次 submit。
- partial fill 产生明确 remaining exposure；不能把“订单 accepted”写成“position complete”。
- opening proposal 必须含 exit/invalidation。V1 paper 模拟完整 exit；未来 live 只有已批准 attached exit 或确定性 risk-reducing action可自动执行，任何增加风险的改动必须新 approval。
- 最终状态至少区分 `REJECTED`、`CANCELLED`、`PARTIALLY_FILLED`、`POSITION_OPEN`、`CLOSED`、`EXECUTION_AMBIGUOUS`、`MANUAL_REVIEW_REQUIRED`。

### 3.9 Evaluation 与 evolution

Case terminal 或达到预注册 horizon 后，EvaluationHarness 读取 frozen evidence 和 market/account outcomes：

1. 结果 verifier：事实后来是否证实、proposal 是否可执行、orders/fills/positions 是否一致、gross/net/abnormal return、drawdown、slippage/fees/funding；
2. 过程 verifier：source、as-of、identity、risk、approval、one-attempt/reconcile 是否合规；
3. quality verifier：thesis、bear case、invalidator、no-trade 质量，按 rubric 引用证据；
4. first-error attribution：Trigger/Evidence/Retrieval/Model/Proposal/Risk/Approval/Adapter/Reconcile/Eval；
5. 追加 immutable learning record，不直接修改 production。

EvolutionHarness 离线聚合同类 episodes，先提出 reviewed knowledge 或 scoped Prompt/Skill diff；只有可语言化修补不足时才提出 Harness/code 候选，最后才考虑参数训练。所有候选跑 boundary、retention、safety、point-in-time replay 和成本/延迟评估；通过只得到 `release_to_canary`，仍需 human promotion。

## 4. 链路瓶颈与失败/安全模式

### 4.1 主要瓶颈

| 瓶颈 | 为什么是瓶颈 | 设计回应 |
|---|---|---|
| Sent feed 不是固定 cutoff outbox | `hours` 相对每次 server-now；分页期间第一页会变化 | 保存 manifest/ETag/fetch interval，首屏复核、有限重抓；V1 承认 polling latency，不 DB reach-through |
| Episode 重复与因果污染 | provider 会把催化剂、价格回声、评级、合约上市拆成多卡 | 先 episode clustering，再研究/评估；样本单位是 episode |
| 一手事实慢、冲突或不存在 | 新闻可早于 filing/trial result，LLM 会填空 | source trust + available_at + conflict；关键事实未验证即 no-trade |
| 长上下文与 context rot | 72h raw pages 超长，滑窗会忘记失败和约束 | 子 Agent 隔离、manifest、status bar、引用式压缩；raw evidence 外置 |
| 同模型 proposer/reviewer 相关错误 | “多 Agent”不等于独立验证 | reviewer 只提供 challenge；risk/approval/verifier 使用代码/环境真值 |
| Issuer 到 instrument mapping | equity、perp、future、tokenized exposure 经济含义不同 | 两层 identity、exact metadata snapshot、collision fail closed |
| Point-in-time market data 缺口 | 今天可发现合约不证明当时可交易 | historical capability snapshot；缺失即 `unbacktestable` |
| Price-in 与执行延迟 | 好消息可在第一张卡前已涨完 | 预注册 entry/horizon、delivery latency、abnormal return、delay sensitivity |
| Risk data freshness | market/account 在 research/approval 期间持续变化 | risk snapshot + approval tolerance + submit 前 fresh preflight |
| OpenTrade 合同/成熟度 | server 未开源、fail-open risk、无公开 idempotency/paper | pin contract；V1 read-only/paper；本地 fail-closed；ambiguous no-retry |
| 交易完成语义 | HTTP accepted 不等于 fill，fill 不等于 closed lifecycle | durable intent、receipt、orders/trades/positions reconcile |
| 评估信号慢且多目标 | 单笔 P&L 噪声大，会奖励违规捷径 | episode corpus、process veto、net/abnormal outcome、paired/statistical gate |
| Evolution feedback poisoning | 网页提示注入、偶然收益可固化错误 | online/offline isolation、candidate provenance、independent reviewer、immutable trust root |

### 4.2 Stable failure modes

| Stage | Stable outcome/error | 自动行为 | 安全结果 |
|---|---|---|---|
| Trigger | `duplicate_trigger` | 返回同 Case | 不重复研究/下单 |
| Trigger | `unsupported_delivery_state` | 不重试 | held/recovery/ambiguous delivery 不触发 |
| Snapshot | `news_snapshot_changed` | 有限重抓 | 仍漂移则 blocked |
| Snapshot | `news_source_unavailable` | GET 有界退避 | 不形成 proposal |
| Evidence | `evidence_conflict` | 保留双方证据 | 可报告，不可执行 |
| Research | `research_budget_exhausted` | 终止 subagents | `NoTrade`/research incomplete |
| Research | `research_invalid` | 一次 schema repair | 无 model fallback order |
| Identity | `instrument_ambiguous` | 不猜测 | 人工修 mapping policy |
| Capability | `execution_unavailable` | 不重试 | cash equity/restricted venue research-only |
| Historical | `unbacktestable` | 不用当前数据回填 | 排除策略收益统计或单列 missing |
| Risk | `risk_data_unavailable` | read 可有界重试 | fail closed |
| Risk | `risk_rejected` | 记录逐 rule 原因 | 修改必须新 revision |
| Approval | `approval_conflict` | 不重试 | 不执行 |
| Approval | `approval_expired` | 重新 risk/proposal | 旧批准不能复用 |
| Preflight | `preflight_drift` | 新 revision | 重新审批 |
| Provider read | `rate_limited/transient` | commit point 前有界退避 | deadline 后 blocked |
| Provider auth | `auth_or_region_rejected` | 不重试 | redacted blocked |
| Submit | `order_rejected` | terminal | 不把 reject 当 transient |
| Submit | `execution_ambiguous` | **禁止重发** | reconcile/人工 |
| Reconcile | `reconcile_inconclusive` | 有界观察窗口 | manual review；不猜仓位 |
| Checkpoint | `research_checkpoint_unavailable` | 可从 EvidenceLedger 重建研究 | 不影响 order truth |
| Eval | `insufficient_evidence` | 扩 corpus/等待 outcome | 不 promotion |
| Evolution | `candidate_regression` | reject + 保存原因 | stable version 不变 |

### 4.3 安全边界

- External content 一律包为 untrusted evidence；网页里的“忽略规则、调用工具、发送 secret”没有 instruction 权限。
- Research process 无 OpenTrade write secret、exchange key、wallet、host filesystem 或 arbitrary network；allowlisted fetcher 做大小、MIME、redirect、timeout、redaction 检查。
- Secret 只在 execution Adapter/secret manager；不进 prompt、scratch、trace、checkpoint、CaseSnapshot、error 或 audit payload。
- `review_case` principal 来自 authenticated transport context，不是 command body；LLM/HITL resume payload 不能伪造 principal。
- `pause/kill` 在 Risk/ExecutionKernel 每次 transition 检查；pause 后允许 risk-reducing reconcile/close policy，不允许新 exposure。
- 生产稳定 risk、approval、execution、reconcile verifier、eval gate、audit 和 rollback artifact 对 Agent read-only，且与 candidate store 分权。

## 5. Radically minimal Interface

### 5.1 Types

概念 Python，不是本次实现：

```python
CaseId = NewType("CaseId", str)
CaseRevision = NewType("CaseRevision", int)
ProposalDigest = NewType("ProposalDigest", str)

@dataclass(frozen=True)
class SentDeliveryRef:
    event_id: str
    kind: Literal["first"] = "first"

@dataclass(frozen=True)
class HistoricalDeliveredWindow:
    start: datetime
    end: datetime
    observed_cutoff: datetime

CaseTrigger = SentDeliveryRef | HistoricalDeliveredWindow

@dataclass(frozen=True)
class OpenCase:
    idempotency_key: str
    trigger: CaseTrigger
    mandate_id: str
    focus_event_ids: tuple[str, ...] = ()

@dataclass(frozen=True)
class Approve:
    expected_revision: CaseRevision
    proposal_digest: ProposalDigest

@dataclass(frozen=True)
class Reject:
    expected_revision: CaseRevision
    proposal_digest: ProposalDigest
    reason: str

@dataclass(frozen=True)
class RequestRevision:
    expected_revision: CaseRevision
    proposal_digest: ProposalDigest
    constraints: ProposalConstraints
    reason: str

@dataclass(frozen=True)
class CancelCase:
    expected_revision: CaseRevision
    reason: str

ReviewCommand = Approve | Reject | RequestRevision | CancelCase

class TradingCases(Protocol):
    async def open_case(self, request: OpenCase, /) -> CaseRef: ...
    async def get_case(self, case_id: CaseId, /) -> CaseSnapshot: ...
    async def review_case(
        self, case_id: CaseId, command: ReviewCommand, /
    ) -> CaseSnapshot: ...
```

Interface 刻意没有以下内容：`execute`、`retry`、`reconcile`、`run_agent`、`tool_call`、`checkpoint`、OpenTrade URL/body、exchange credential、LangGraph thread、任意 order JSON。

`mandate_id` 是 server-owned mandate 的引用，决定 research/shadow/paper/live capability；caller 不能通过 `mode='live'` 自我升级。`HistoricalDeliveredWindow` 永远被 mandate 限制为 research/replay。

### 5.2 CaseSnapshot

`CaseSnapshot` 是唯一观察 read model，至少包含：

```text
case_id, revision, state, created_at, updated_at
trigger, source_manifest, episode_members, data_cutoff
research_status, budgets, evidence_gaps, conflicts, dossiers[]
proposal?, not_tradeable_reasons[], risk_assessment?
approval_request? {proposal_digest, expires_at, exact_packet_summary}
reviews[]
execution_attempts[] {attempt_id, commit_point, receipt, ambiguity}
reconciliation? {orders, fills, positions, balance, confidence}
evaluation? {outcome, process_vetoes, first_error, cost, versions}
```

所有大块 raw document 留在 evidence store，以 hash/citation 引用；Snapshot 大小有上限和稳定 schema version，不泄漏 Deep Agents/LangGraph/OpenTrade 内部类型。

### 5.3 Invariants

1. 只有 sent first delivery 或显式 historical window 可开 Case；historical/recovery 不能 live。
2. 相同 principal + idempotency key + canonical request 返回同 Case；同 key 不同 body 是 conflict。
3. 每个 Case 单 transition writer；所有 mutation 使用 `expected_revision`。
4. Evidence、transition、review、execution attempt、reconcile observation append-only；current snapshot 可重建。
5. 每个结论绑定 evidence ids/hash、available_at 和 model/prompt/tool/policy versions。
6. Research output 不是 authorization；Deep Agents HITL 也不是 business approval。
7. Proposal 必须精确解析 economic exposure 与 venue instrument；ticker-only fail closed。
8. 所有硬 risk 读 server truth；模型字段只能提供 thesis，不提供额度、余额、仓位或“已通过”。
9. 必需数据缺失/陈旧/冲突时不产生 executable proposal。
10. Approval 绑定 authenticated principal、revision、digest、mandate/account/venue、全部 order/exit/tolerance 和 expiry。
11. Approval 后任何 material drift 生成新 revision，旧 approval 失效。
12. 每个 execution attempt 先 durable intent 后 external write；commit point 后的未知结果一律 ambiguous。
13. Ambiguous execution 不自动重发；只 reconcile 或人工处置。
14. CaseStore 是交易状态 authority；agent message/checkpoint/tool response 不是。
15. Secret 不进入 Agent 可见面或公共 Snapshot。
16. 开仓 proposal 必须包含 exit/invalidation；增加风险需新 approval。
17. research/shadow/paper Case 不能原地升级 live。
18. evaluation/evolution 只能产生 candidate；不能在线改 stable capability 或 trust root。
19. Cancel 必须先 durable-record，再在 safe point 传播；已取消 research task 的迟到结果只进 audit，不得参与 synthesis。进入 external submit commit point 后，Cancel 只能成为 venue cancel request + reconcile，不能宣称订单已取消。

### 5.4 Ordering

```text
OPENED
  -> SNAPSHOTTING
  -> RESEARCH_ADMISSION
  -> RESEARCHING
  -> SYNTHESIZING
  -> PROPOSING
  -> RISKING
  -> RESEARCH_COMPLETED | NO_TRADE | EXECUTION_UNAVAILABLE | AWAITING_APPROVAL
  -> APPROVED
  -> REVALIDATING
  -> AWAITING_APPROVAL (material drift)
     | INTENT_RECORDED
     -> SUBMITTING
     -> RECONCILING_ENTRY
     -> REJECTED | CANCELLED | PARTIALLY_FILLED | POSITION_OPEN | EXECUTION_AMBIGUOUS
     -> MANAGING_APPROVED_EXIT
     -> RECONCILING_EXIT
     -> CLOSED | MANUAL_REVIEW_REQUIRED
  -> EVALUATING
  -> EVALUATED
  -> EVOLUTION_CANDIDATE? (offline only)
```

任一尚未进入 `INTENT_RECORDED` 的非终态都允许：

```text
-> CANCEL_REQUESTED -> CANCELLED
```

`INTENT_RECORDED` 之后的取消进入 execution/reconcile 子状态，只有 venue observation 能决定订单/仓位是否真的取消。

任何 event 只能推动状态图允许的 transition；重复、乱序、过期 event 是 no-op 并记录原因。研究失败不会跳到 baseline order；reconcile 未完成不会跳到 evaluation success。

### 5.5 Error contract

Interface 只把 caller 可修复的问题作为同步 typed error：

- `CaseNotFound`
- `IdempotencyConflict`
- `RevisionConflict`
- `InvalidReviewCommand`
- `Forbidden`
- `UnsupportedTrigger`

网络、模型、provider、risk、execution 和 reconcile 的业务失败都写入 durable `CaseSnapshot.state/outcome/reasons`。调用者不需要捕获 40 种 upstream exception，也不能借 retry 一个同步 exception 绕过 state machine。

### 5.6 Performance contract

- `open_case` 只做 canonical validation + durable enqueue，复杂度与 Event 内容大小无关；不等待 Tracefold、source、model 或 market network。
- `review_case` 只 durable-record approval/revision/cancel command；preflight/submit/reconcile/取消传播异步执行，不让 HTTP/client timeout 模糊外部 write 结果。
- `get_case` 只读 bounded current snapshot，不触发 fetch/model/provider。
- sent delivery hot path 与 Trading Module 物理隔离；Trading backlog 不能拖慢 News delivery。
- 每个 Case 有 code-owned wall-clock、model-call、tool-call、source-byte、sandbox CPU/storage 和 cost budget；耗尽后得到稳定 terminal/degraded research outcome，不无限循环。
- 运行指标至少报告 sent-to-case lag、queue age、research p50/p95、budget-exhausted rate、approval age、preflight age、submit/reconcile latency、ambiguous rate、cost per evaluated episode。

## 6. Usage

### 自动 sent-delivery trigger

```python
case = await trading_cases.open_case(
    OpenCase(
        idempotency_key=f"tracefold:first:{event_id}",
        trigger=SentDeliveryRef(event_id=event_id),
        mandate_id="news-research-v1",
    )
)

snapshot = await trading_cases.get_case(case.case_id)
```

Adapter 重复读取同一 sent delivery 时得到相同 `case_id`。调用方不需要知道 episode clustering、Deep Agents、sources 或 queues。

### 72 小时 research-only replay

```python
case = await trading_cases.open_case(
    OpenCase(
        idempotency_key="delivered-72h:2026-08-21T00:00Z:research-v1",
        trigger=HistoricalDeliveredWindow(
            start=datetime(2026, 8, 18, tzinfo=UTC),
            end=datetime(2026, 8, 21, tzinfo=UTC),
            observed_cutoff=datetime(2026, 8, 21, 1, 0, tzinfo=UTC),
        ),
        mandate_id="historical-research-only-v1",
        focus_event_ids=(mrvl_google, sk_hynix, mrna_phase3),
    )
)
```

`focus_event_ids` 只改变展示/研究优先级，不缩小完整 corpus，也不允许 cherry-pick 回测。

### 审批一个未来可支持的 paper proposal

```python
snapshot = await trading_cases.get_case(case_id)

await trading_cases.review_case(
    case_id,
    Approve(
        expected_revision=snapshot.revision,
        proposal_digest=snapshot.approval_request.proposal_digest,
    ),
)

final = await trading_cases.get_case(case_id)
```

caller 不传 token、exact symbol mapping、hedged、OpenTrade URL 或 retry flags；也不额外调用 `execute()`。

## 7. Implementation hides

调用者不应学习或重复以下复杂度：

- Tracefold feed cursor/ETag、sent semantics、detail fetch、manifest/hash、高水位与重启 catch-up；
- provider card 到 economic episode 的聚合、strong-fact/contradiction 处理；
- `event_time`/`available_at`、source trust、citation normalization、conflict 与 prompt-injection quarantine；
- Deep Agents 版本/profile/middleware、Todo、subagent schema、scratch、summary、checkpoint/resume；
- issuer/share class/underlying 与 venue instrument 的双层 identity，ticker collision 与 aliases；
- point-in-time metadata/calendar/book/OHLCV/benchmark、price-in、abnormal return、slippage/fee/funding/borrow；
- mandate、portfolio exposure、risk/sizing、freshness、canonicalization、digest/revision/expiry；
- OpenTrade envelope、exact CCXT symbol、precision/minimum、hedged/mode、rate/auth/region errors；
- durable commit point、one-attempt semantics、ambiguous outcome、orders/trades/positions reconcile；
- paper/replay/production Adapter 选择、redaction、audit、trace、metrics；
- episode evaluation、first-error attribution、candidate generation、release gate 与 rollback。

这就是 Module 的 **Depth**：删除 `TradingCases` 后，上述复杂度会重新散落到 trigger、UI、研究脚本和 broker caller；保留它时，caller 只需三个入口。

## 8. Dependency categories、Seams 与 Adapters

| 类别 | 依赖 | 设计 | Adapter/测试策略 |
|---|---|---|---|
| In-process | canonical schemas、state transition、episode identity、digest、risk、freshness、backtest math、verifiers | 放在深 Implementation；不为每个纯函数造 port | property/unit tests，经三个入口做行为验证 |
| In-process library | Deep Agents/LangChain/LangGraph、schema/crypto/math libs | 固定版本，只在 ResearchHarness/Implementation 可见 | component ablation；升级 contract/eval corpus |
| Local-substitutable | CaseStore、evidence blob、queue、research checkpointer、clock | 责任分离；不是 caller-facing Interface | production 独立 Postgres/object store；test container/fake clock；不把每个本地实现抽成万能 port |
| Remote but owned | Tracefold News read、operator review transport | 明确 owned service Seam | `TracefoldNewsHttpAdapter` + fixture Adapter；auth principal 由 transport 注入 |
| True external/read | official filings/IR/trial registries、market/account data、LLM | read allowlist、schema/freshness/redaction | production + frozen replay/fake；fault/schema-drift tests |
| True external/write | OpenTrade；未来 regulated cash-equity broker | 最窄 execution Seam，只收 `ApprovedExecutionPlan` | V1 replay/paper Adapter；production read-only；出现第二个真实 venue 后才提炼共同 contract |
| Secret capability | OpenTrade token、exchange/broker credentials、future signer | 仅 execution Adapter/secret manager 可见 | secret canary/redaction tests；Agent/store/snapshot 均不可见 |

不要把 OpenTrade 的 40 个 endpoints 一比一暴露成 `BrokerPort`，也不要先做万能 `ExecutionVenue`. 在只有 OpenTrade + fake 时，具体 Adapter 足够；第二个真实 broker 到来后，再从两个已知实现中提炼最小共同 Interface。这保持 **Locality**，避免 speculative abstraction。

## 9. 三种架构方案比较与推荐

三种方案共享同一安全底线：LLM 不持有交易凭证，risk/approval/execution/reconcile 在模型外，外部资料是 evidence 而不是 instruction。区别在于 Module 的边界和 Interface 的深度。

| 方案 | 外部 Interface | 优点 | 主要问题 | 判断 |
|---|---|---|---|---|
| **A. 独立 `TradingCases` 深 Module** | `open_case` / `get_case` / `review_case` | 最高 Depth/Leverage；Deep Agents、risk、approval、OpenTrade、reconcile 都保持 Implementation；News/交易失败域与权限隔离；未来换 model/venue 时 Locality 最好 | 独立 deployable/store/queue；异步 snapshot；V1 polling 有延迟 | **推荐**。最符合 Tracefold hard cuts、书中 Harness/环境真值分工和 radically minimal Interface |
| **B. 独立 workflow service，逐阶段公开** | `snapshot` / `research` / `propose` / `risk` / `approve` / `execute` / `reconcile` 等 7–10 个入口 | 每阶段可手动驱动、调试直观；研究人员容易跳步试验 | Caller 必须学习状态顺序、OpenTrade/risk/reconcile 语义；容易绕过步骤或形成第二套 orchestration；Interface 浅、测试面大、上游变化扩散 | 只适合作为内部 debug/admin surface，不作为业务 Interface |
| **C. 把 Deep Agent/Trading 加回 Tracefold News lane** | News Event/consumer 内隐触发，或扩展 `tracefold.news` | sent-to-research latency 最低；可复用现有 process/broker/Postgres | 复活已 hard-cut 的 Analyst/deep lane；扩大 News schema、依赖、凭证和故障域；交易状态污染 News truth；Deep Agents backlog 可拖累 delivery | **拒绝**。与当前单 bounded context、一次 Triage/一卡、无 market lane 的权威架构冲突 |

方案 A 的关键不是“方法少”本身，而是三个方法共同构成完整且难以误用的 Interface：调用者只能开 Case、观察 Case、审查 exact proposal。阶段 B 的细粒度能力仍存在，但都是 package-private Implementation/operational diagnostics；方案 C 的低延迟若未来确有业务价值，应先独立论证 sent outbox Seam，而不是把研究和执行塞回 News。

推荐的演进顺序：

1. 先实现 A 的 research-only + frozen 72h corpus；
2. 加入 replay/paper Adapter 和完整 ambiguity/reconcile state machine；
3. 用评估证明瓶颈后，再决定是否新增 owned sent-delivery outbox；
4. 只有满足第 14 节 live gate 才另开 live execution Issue；三入口 Interface 保持不变。

## 10. Trade-offs

### 获得的 Leverage

- 三个入口覆盖单 sent Event、72 小时 batch、research-only、paper、未来 live review、execution/reconcile 与 inspect。
- Deep Agents、model、source、market data、risk policy、OpenTrade 和未来 broker 都可在各自 Seam 内替换。
- News 保持一个 bounded context；ResearchHarness failure 不影响 delivery，execution failure 不污染 News truth。
- 没有 LLM order tool，权限与事故归因集中在 ExecutionKernel/Adapter。
- exact digest + revision 把“人看过某段文字”提升为可验证的授权合同。

### 明确代价

- 独立 deployable/store/queue 增加运维面；这是隔离交易凭证、业务 truth 和失败域的代价。
- polling 比 delivery outbox 慢，也需要 manifest revalidation；V1 选择零 Tracefold hot-path change。
- `open_case` 是异步 eventual result；caller 需要 `get_case`，不能一次请求拿到完整结论。
- 三入口 Interface 使 `CaseSnapshot` 成为较丰富的版本化 read model；必须控制大小和兼容策略。
- 不给 LLM write tool 降低“全自动”观感，却显著提高权限 Locality、可测试性与审计性。
- OpenTrade 缺乏公开 idempotency/paper/cash-equity contract，V1 无法诚实宣称 live execution。
- 人审增加延迟，热点新闻可能已 price-in；系统应正确输出 no-trade，而不是为追求速度绕过 approval。
- Deep research 成本高，必须 episode dedup、admission 和预算，不应对每条 sent card 无差别启动完整 harness。

## 11. 四个手工 probe 如何进入验证，而不是变成策略

前置调研的 72 小时 snapshot 已显示：三个 episode 对应 7、8、14 条推送；MRVL 在首推前 60 分钟已明显上涨，GOOGL 的短时表达弱，SK Hynix 新闻在韩国现金市场收盘后，MRNA 更暴露 source 与 historical executability 问题。[OT report L303–354]

Issue 的固定 probe 应要求：

1. **MRVL/GOOGL 协议**：Fact verifier 找到双方/filing、协议范围和 disclosed economics；proposal 不得把 MRVL 的方向机械复制给 GOOGL；首推前 price-in 必须进入 thesis/no-trade。
2. **SK Hynix 回购**：确认回购规模、执行期、注销/库存股处理和公司原始材料；韩国 cash market closed 时，任何 perp 表达都必须单列 basis/session/liquidity，不得称为现金股回测。
3. **MRNA Phase III**：当 wire/存量摘要与 ClinicalTrials/issuer disclosure 不一致，或 official result 不存在时，必须得到 `source_unverified + execution_unavailable_at_cutoff -> no_trade`；事后上涨不能把错误 proposal 判为正确。
4. **全量 72 小时 corpus**：probe 只是重点切片；评估必须跑全部 sent episodes、固定 entry/horizon/benchmark/cost/missing-data policy，禁止只选成功故事。

## 12. Issue 可直接采用的 acceptance criteria

建议 Issue 标题：

> Build an isolated TradingCases research/paper Module from sent News deliveries, with Deep Agents inside and deterministic risk/approval/reconciliation outside

### A. Module boundary 与 Interface

- [ ] 新能力作为独立 deployable/owned store 落地；`tracefold.news` package root、News schema、现有 RabbitMQ topology、Triage prompt/verdict/card 和 Workers TaskGroup 均不新增 Trading/Deep Agents 依赖。
- [ ] Research 与 Execution 使用不同进程 principal、包依赖、credential 和 network policy；部署测试证明 Research 没有 broker write capability，Execution 没有 LLM/web/RAG capability。
- [ ] 对外业务 Interface 只有 `open_case(OpenCase) -> CaseRef`、`get_case(CaseId) -> CaseSnapshot`、`review_case(CaseId, ReviewCommand) -> CaseSnapshot` 三个入口；没有 caller-facing `execute/retry/reconcile/run_agent/tool`。
- [ ] Deep Agents、LangGraph、OpenTrade 和具体 storage types 不出现在 Interface；architecture tests 锁定 dependency direction。
- [ ] `open_case`/`review_case` 只 durable-ack，`get_case` 只读 bounded snapshot；测试证明外部 network/model 不在请求 transaction/critical path。

### B. Sent trigger、evidence 与 episode

- [ ] Production trigger 只接受公开 News HTTP 中 first delivery 的 `state='sent'`；held/recovery/sending/terminal/ambiguous delivery 用例不能开 executable Case。
- [ ] polling Adapter 跨重复、重启、分页重排保持 `(event_id, kind)` 幂等；相同 key/body 返回同 Case，不同 body 返回 `IdempotencyConflict`。
- [ ] 72 小时 selector 保存 fetch interval、manifest、ETag/detail hashes 并复核首屏；持续漂移得到 stable blocked outcome，不直接读 Tracefold DB。
- [ ] Evidence Ledger 保存 raw reference/hash、source kind/trust、`event_time`、`available_at`、fetched-at 和 lineage；每个 Dossier claim 可回溯到 evidence。
- [ ] 同一 economic episode 的重复/进展 card 聚合后再研究和评估；MRNA 相关 14 card 的 fixture 不得计为 14 个独立策略样本。

### C. Deep Agents research harness

- [ ] 固定并记录 Deep Agents/model/prompt/tool schema versions；显式配置 planning，不依赖版本默认。
- [ ] 只注册 `official_fact_verifier`、`market_reaction_analyst`、`adversarial_thesis_reviewer` 三个 read-only 窄 subagents；禁用 default general-purpose 和所有 order/write tool。
- [ ] 每个角色使用 strict structured output；一次 bounded repair 后仍 invalid 则 `research_invalid`，不得生成 executable fallback。
- [ ] source/market tools 实施 allowlist、pagination/truncation、deadline、size cap、MIME/redirect validation 和 redaction；untrusted content 不能改变 tool/permission/approval state。
- [ ] scratch/checkpoint 不可见 host secrets；CaseStore 与 research checkpointer 分离，删掉 checkpoint 后可从 evidence 重建研究且不改变 execution truth。
- [ ] `CancelCase` 先 durable-write，再在 safe point 传播；task completion 携带 attempt/cancel epoch，迟到结果不进入 synthesis；post-submit cancel 只能由 venue observation/reconcile 终结。
- [ ] status bar 的预算、证据数、冲突、freshness、stage 和 approval/execution state 由代码生成；有 wall-clock/model/tool/byte/cost hard budget 和 liveness test。

### D. Proposal、identity、risk 与 approval

- [ ] `TradeProposal` schema 包含 economic exposure、exact venue instrument、direction/horizon、entry/exit/invalidation、order intent、cost/liquidity/basis/funding/session、evidence/version lineage 和 not-trade reasons。
- [ ] identity tests 覆盖 equity/token/perp collision 与 SKHY/SKHX/SKHYNIX aliases；ticker-only 永远返回 `instrument_ambiguous`。
- [ ] MRVL、GOOGL、MRNA、SK Hynix 现金股 proposal 在没有 regulated cash-equity Adapter 时稳定返回 `execution_unavailable:no_cash_equity_adapter`。
- [ ] Historical metadata 缺失时返回 `unbacktestable/execution_unavailable_at_cutoff`；测试禁止用当前 capability 回填旧事件。
- [ ] RiskKernel 是纯/确定性业务 Module，读取 server-truth mandate/account/position/open-order/market data；missing/stale/conflicting/degraded 一律 fail closed，并记录逐 rule result/version。
- [ ] Approval 绑定 authenticated principal、Case revision、canonical proposal digest、mandate/account/venue、完整 order/exit/tolerance 和 server expiry；任何差异/过期不能执行。
- [ ] `RequestRevision` 产生新 proposal、重新 risk、重新 digest；不能把人工 patch 原样送给 Adapter。

### E. Paper execution 与 reconciliation

- [ ] V1 只有 replay/paper execution Adapter；构建、配置和部署测试证明不会调用 OpenTrade write endpoint。
- [ ] execution state machine 在 external submit 前 durable-write intent/digest/attempt/commit point；crash/restart 可恢复到 reconcile，不重发。
- [ ] fault injection 覆盖 pre-submit 429/timeout、明确 order reject、post-submit timeout、connection reset、malformed response 和 process crash。
- [ ] post-submit 未知结果稳定为 `EXECUTION_AMBIGUOUS`；测试断言 submit call count 恰为 1，随后只允许 orders/trades/positions reconcile 或人工 terminal action。
- [ ] Reconciler 区分 accepted、partial fill、fill、position open、closed、cancelled、rejected、inconclusive；HTTP accepted 不能直接标记 completed。
- [ ] opening plan 有预注册 exit/invalidation；paper run 覆盖 entry、partial/exit、fees/funding/slippage 和最终 position/balance 对账。

### F. Evaluation 与 release gate

- [ ] Frozen evaluation corpus 以 sent episode 为单位，保存 point-in-time News/market/capability snapshots；train/tune 与 evaluation episodes/time slices 分离。
- [ ] 指标至少包括 fact/citation accuracy、source conflict、no-trade correctness、proposal validity、identity/risk/approval compliance、duplicate submit、reconcile accuracy、net/abnormal return、drawdown、slippage/cost、latency/token/cost。
- [ ] Safety veto 覆盖未验证来源交易、look-ahead、instrument collision、limit breach、approval mismatch、duplicate/ambiguous retry、secret leakage；任一 veto 使该 episode fail。
- [ ] 同时有 end-to-end 和 trajectory-prefix tests，并记录 first-error component；Model replacement 与 Harness ablation 分开报告。
- [ ] MRVL/GOOGL、SK Hynix、MRNA 三组 probe 满足第 11 节预期；尤其 MRNA 即使事后收益为正，也必须因当时 source/execution gap 判 no-trade。
- [ ] Candidate 对 stable baseline 做 paired sequential replay，报告重复运行波动和适用边界；小于噪声带不 promotion，不以四个案例或单次 P&L 宣称 strategy validated。
- [ ] Candidate 只有在 boundary 改善、retention 不退化、安全 veto 为零且成本/延迟在预算内时得到 `release_to_canary`；promotion 需 human approval，rollback artifact 已验证。

### G. Evolution、security 与 operations

- [ ] Online Case 只 append evidence/trajectory/outcome；没有 prompt/skill/code/model/risk 的线上自改路径。
- [ ] Offline candidate manifest 包含 source episodes、支持/反驳证据、first-error/root-cause、最小 diff、预期改善、潜在退化、评估结果、candidate/stable/rollback versions。
- [ ] candidate knowledge/Prompt/Skill/code/model 与 stable capability 物理隔离；untrusted source 不直接成为 instruction。
- [ ] Research Agent 无权修改 RiskKernel、approval verifier、ExecutionKernel、reconcile truth rules、eval verifier/tests、安全 gate、audit log、stable backup 或 promotion decision。
- [ ] secrets 只在 Adapter/secret manager；日志、trace、checkpoint、evidence、Snapshot、errors 均通过 secret-canary/redaction tests。
- [ ] metrics/alerts 覆盖 sent-to-case lag、backlog、budget exhaustion、source conflicts、risk rejects、approval age/conflicts、paper attempts、ambiguity/reconcile age、eval veto 和 candidate regressions；有 operator pause/kill 和 restart recovery evidence。

## 13. Hard cuts

以下明确不在首个 Issue/版本中：

1. **不改 Tracefold News V3**：不新增 Trading 表/worker/queue/verdict/stage/card/API mutation，不复活 Analyst/deep lane、market-mark 或 checkpoint tables。
2. **不把 Deep Agents 加入 Tracefold runtime**：它只存在独立 ResearchHarness deployable。
3. **不 live trade**：不调用 OpenTrade CEX/DEX write、不存 exchange/wallet credential、不声称 exactly-once；仅 read-only discovery + replay/paper。
4. **不交易现金股票**：没有 regulated cash-equity broker Adapter 前，MRVL/GOOGL/MRNA/SK Hynix 都 research-only。
5. **不把任何 execution tool 给 LLM/MCP/subagent**，不允许 arbitrary URL/body/shell/host filesystem/network。
6. **不做 ticker-only mapping、current-metadata historical backfill、look-ahead 或回测后挑 horizon。**
7. **不做自动 Prompt/Skill/code/model promotion，不做 SFT/RL/online learning/self-modifying risk。**
8. **不做 GraphRAG、通用 memory platform、万能 BrokerPort 或 portfolio optimizer。** 先证明 relational evidence + typed artifacts 的瓶颈。
9. **不支持 options、多腿、跨 venue arbitrage、short/borrow、杠杆动态调整或 autonomous exit improvisation。** Paper plan 只覆盖明确受支持的单 instrument、预注册 entry/exit。
10. **不把 MRVL/GOOGL、SK Hynix、MRNA 四个故事包装成 strategy validation。** 它们是安全/能力 probes，正式结论需要更大、按 episode、point-in-time、walk-forward 的 corpus。

## 14. 后续 live gate（不属于 V1 acceptance）

只有以下外部事实和系统证据都满足，才值得另开 live Issue：

- OpenTrade 书面确认并 contract-test endpoint/version、account/region、instrument coverage、credential custody、rate/reject/timeout 语义；
- 有可验证的 client idempotency 或等价 exactly-once intent/reconcile 方案；
- 有官方 paper/testnet 或极小额度 canary 环境；
- local fail-closed risk、exact approval、kill/pause、one-attempt/ambiguity、position/exit reconcile 已在 fault injection 和 paper corpus 通过；
- research/paper release gate 在足够 episode、多个 market regime、预注册 cost/benchmark/horizon 下通过，且所有 safety veto 为零；
- 现金股必须另有受监管 broker、market/borrow/calendar/compliance Adapter，不从 OpenTrade TradFi derivative 推导。

## 15. 来源索引

### ai-agent-book（固定 commit，行号引用）

[b2-context]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter2.zhtw.md#L7-L35
[b2-loop]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter2.zhtw.md#L185-L247
[b2-budget]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter2.zhtw.md#L305-L330
[b2-cache]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter2.zhtw.md#L434-L446
[b2-cache2]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter2.zhtw.md#L528-L576
[b2-process]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter2.zhtw.md#L616-L663
[b2-injection]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter2.zhtw.md#L704-L720
[b2-skills]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter2.zhtw.md#L739-L807
[b2-status]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter2.zhtw.md#L832-L969
[b2-compress]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter2.zhtw.md#L971-L1084

[b3-memory]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter3.zhtw.md#L78-L183
[b3-types]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter3.zhtw.md#L185-L247
[b3-retrieval]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter3.zhtw.md#L302-L463
[b3-governance]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter3.zhtw.md#L465-L560
[b3-rag]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter3.zhtw.md#L562-L687
[b3-summary]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter3.zhtw.md#L701-L715

[b4-tools]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter4.zhtw.md#L12-L70
[b4-contract]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter4.zhtw.md#L72-L110
[b4-mcp]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter4.zhtw.md#L112-L188
[b4-execution]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter4.zhtw.md#L212-L270
[b4-idempotency]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter4.zhtw.md#L272-L302
[b4-subagents]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter4.zhtw.md#L317-L421

[b5-workflow]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter5.zhtw.md#L1-L174
[b5-faults]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter5.zhtw.md#L176-L250
[b5-sandbox]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter5.zhtw.md#L295-L346
[b5-servertruth]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter5.zhtw.md#L348-L496
[b5-artifacts]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter5.zhtw.md#L499-L701
[b5-bootstrap]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter5.zhtw.md#L703-L753

[b6-async]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter6.zhtw.md#L1-L90
[b6-eventloop]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter6.zhtw.md#L109-L173
[b6-jobs]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter6.zhtw.md#L202-L260
[b6-observe]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter6.zhtw.md#L427-L692
[b6-summary]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter6.zhtw.md#L704-L721

[b7-object]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter7.zhtw.md#L1-L25
[b7-metrics]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter7.zhtw.md#L69-L130
[b7-env]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter7.zhtw.md#L132-L218
[b7-dataset]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter7.zhtw.md#L220-L301
[b7-judge]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter7.zhtw.md#L303-L468
[b7-cost]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter7.zhtw.md#L508-L590
[b7-stats]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter7.zhtw.md#L629-L726
[b7-observe]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter7.zhtw.md#L655-L726
[b7-release]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter7.zhtw.md#L743-L832

[b8-map]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter8.zhtw.md#L7-L19
[b8-sft-rl]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter8.zhtw.md#L49-L112
[b8-mid]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter8.zhtw.md#L275-L321
[b8-choice]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter8.zhtw.md#L395-L417
[b8-env]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter8.zhtw.md#L491-L566
[b8-reward]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter8.zhtw.md#L614-L672
[b8-distill]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter8.zhtw.md#L674-L735
[b8-practice]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter8.zhtw.md#L737-L830

[b9-opening]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter9.zhtw.md#L3-L17
[b9-verifier]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter9.zhtw.md#L19-L50
[b9-carriers]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter9.zhtw.md#L51-L112
[b9-code]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter9.zhtw.md#L152-L241
[b9-change]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter9.zhtw.md#L183-L199
[b9-loop]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter9.zhtw.md#L243-L296
[b9-open]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter9.zhtw.md#L298-L310
[b9-safety]: https://github.com/bojieli/ai-agent-book/blob/edaec1b725f2cd23504875df647318e0a1b0ca7c/book-zhtw/chapter9.zhtw.md#L311-L392

### Tracefold 与前置调研

- `AGENTS.md:L7-L13, L29-L31`
- `docs/ARCHITECTURE.md:L3-L8, L21-L28, L52-L76, L81-L152, L154-L190, L195-L251, L412-L480`
- `docs/SECURITY.md:L5-L18, L20-L63, L65-L83, L136-L151`
- `docs/DEVELOPMENT.md:L5-L42, L44-L72, L123-L164, L200-L211`
- `docs/agents/domain.md:L1-L22`
- `docs/agents/issue-tracker.md:L1-L28`
- `docs/research/opentrade-deepagent-trading-agent.md:L17-L24, L26-L143, L145-L301, L303-L443`
