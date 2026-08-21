# OpenTrade × Deep Agents 交易研究模块：方案 A（最小 Interface）

核查日期：2026-08-21（Asia/Taipei）

核查基线：

- OpenTrade：[6551Team/opentrade@0efa9b4](https://github.com/6551Team/opentrade/tree/0efa9b4d27fc644a667453c5c41e55ad0d04557d)（2026-08-15）
- Deep Agents SDK：[deepagents==0.7.8](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.8)，对应 [1e261ba](https://github.com/langchain-ai/deepagents/tree/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8)（2026-08-20）
- Tracefold：本地 991d77854f47001cae6117212ba85d7b11b7a6c2

本文只做来源核查与模块设计，不实现代码、不改变 Tracefold 业务行为。标记说明：

- **来源事实**：可由项目所有者文档、源码、release 或本仓库权威文档直接核实。
- **官方声明、服务端不可核**：OpenTrade 文档如此承诺，但公开仓库没有对应服务端实现。
- **设计推断**：基于事实提出的 Tracefold 适配方案或风险判断，不冒充上游合同。

> **架构结论更新（2026-08-21）：** 本文对 OpenTrade endpoint、CEX idempotency/paper 缺口、远端 risk fail-open、instrument identity 和 Deep Agents 0.7.8 的事实核查仍有效；但“DeepAgent 不持有写工具、只由独立 Execution Authority 下单”的方案已被后续需求替代。当前 KISS 决策见 [`deepagents-order-capability-best-practices.md`](deepagents-order-capability-best-practices.md) 与重写后的 [Issue #104](https://github.com/AnalyThothAI/tracefold/issues/104)：主 DeepAgent 独占真实 `prepare_trade_action/inspect_trade/place_order/cancel_order/close_position` tools，`live_bounded` 可无逐单人审直接产生 OpenTrade 副作用；确定性 mandate/risk/ledger/credential custody 是这些工具的内部 capability 边界，不是另一个替 Agent 决策的执行流程。

## 结论先行

1. **Deep Agents 适合“深度研究 harness”，不应成为交易安全架构。** 它擅长上下文隔离、文件化中间产物、subagent、checkpoint 和 HITL；订单资格、仓位、审批绑定、幂等与成交对账必须由确定性模块拥有。Deep Agents 自己也明确采用 “trust the LLM” 模型，要求在 tool/backend 层实施权限边界。[官方安全说明](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/README.md#security)
2. **不要把 Deep Agents 接回 News V3，复活旧 Analyst/deep lane。** Tracefold 当前只有 News V3 一个业务能力，一次 Triage、一个 verdict、一个卡片；旧 <code>q:news.deep</code>、LangGraph checkpoint 表、market-mark/price lane 都已硬删除。[架构](../ARCHITECTURE.md#product-flows) [checkpoint hard cut](../../src/tracefold/platform/postgres/alembic/versions/20260818_0276_review_49_hard_cut.py) 最干净的 seam 是独立 Trading Case 模块，通过 Tracefold 现有只读 HTTP 合同消费已送达 Event。
3. **OpenTrade 先做 read-only / paper，不能直接当作无人值守 live execution 核心。** 公开仓库只有 Markdown skill 合同、Rust CLI 和安装脚本，没有交易服务端、风险引擎或凭证存储实现；文档还明确说远端风险检查在 market data 或 Redis 不可用时会 **fail open**。[风险说明](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#risk-engine-notes) 本模块必须另设 fail-closed 风控。
4. **MRVL、GOOGL、MRNA、SK Hynix 不能据现有 OpenTrade 合同直接下现金股票订单。** OpenTrade 明列的执行 venue 是 Binance、Bybit、OKX、Hyperliquid、Aster，定位为 spot/perpetual/future；虽有美股期权 gamma 只读 endpoint，却没有 Nasdaq/NYSE、证券经纪商或现金股票订单合同。[支持交易所](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#supported-exchanges) [Gamma endpoint](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#19-get-gamma-exposure) 2026-08-21 的只读 capability discovery 的确找到这些 issuer 的 TradFi/tokenized-equity 衍生品，但它们不是现金股等价物，也不能证明在历史触发时已存在。现金股 proposal 必须返回 <code>execution_unavailable:no_cash_equity_adapter</code>；衍生品 proposal 只有在 exact venue metadata、mandate、basis/funding/leverage/交易时段与流动性检查全部通过后才可形成，ticker 相同绝不够。

一句话建议：**Agent 负责查清、反驳、形成结构化 thesis；确定性 Case 模块负责冻结证据、判定能否交易、请求人审，并在人审后做一次可审计的下单与对账。**

## 1. Tracefold 当前 seam 与不可破坏条件

以下均为**来源事实**：

| 约束 | 当前合同 | 对新模块的含义 |
|---|---|---|
| 单一业务能力 | Tracefold 是一个 Python service/CLI、一个 PostgreSQL，业务能力只有 News V3；业务 package root 是 <code>tracefold.news</code>。[架构](../ARCHITECTURE.md#package-map) [domain router](../agents/domain.md) | 不在 <code>tracefold.news</code> 内加入 Trading、Portfolio 或 Execution 子域，不从外部 import repository 等内部实现。 |
| 一次语义判断 | 一个 Event 只有一次 Triage structured call、一个 verdict、一个首卡；旧 Analyst lane 不再运行。[架构](../ARCHITECTURE.md#product-flows) | 深度研究是下游新产品，不写 <code>news_verdicts(stage='deep')</code>，不发布 <code>verdict.deep</code>，不发 follow-up News 卡。 |
| News 真相 | <code>news_items</code> 是 material facts；<code>news_events</code> 是可重建 read model；provider raw frame 不是事实。[架构](../ARCHITECTURE.md#truth-control-state-and-derived-state) | Trading Case 保存“读取到的 Event 快照 + hash + 时间”，不重新解释 raw frame 为另一份 News 真相。 |
| 只读公开面 | News 公开面只有 <code>/api/news/feed</code>、<code>/api/news/events/{event_id}</code>、<code>/api/news/status</code>，read endpoint 不调用 provider/model、也不写事实。[合同](../CONTRACTS.md#news) | 使用拥有方 HTTP adapter；禁止 Trading 模块直连 Tracefold DB 或 package-private repository。 |
| “已推送”的定义 | <code>outcome=pushed</code> 对应第一张卡 <code>delivery.state='sent'</code>，不是仅有 <code>final_decision=push|escalate</code>。[outcome](../../src/tracefold/news/outcome.py) [feed SQL](../../src/tracefold/news/repository.py) | 72 小时 corpus 从 <code>GET /api/news/feed?hours=72&outcome=pushed</code> 分页取得；失败送达和处理中不能冒充读者已收到。 |
| 无价格反应面 | market-mark / price-reaction lane 已删除。[架构](../ARCHITECTURE.md#product-flows) | 价格、成交量、benchmark、spread、borrow、funding 来自外部 market adapter，绝不回写 News 表。 |
| checkpoint 已删除 | 旧 LangGraph checkpoint 表从 Tracefold schema 硬删除，且从未在 runtime 使用。[migration](../../src/tracefold/platform/postgres/alembic/versions/20260818_0276_review_49_hard_cut.py) | Deep Agents checkpointer 和 Trading Case ledger 不能偷偷重建在当前 Tracefold schema；若落地，应是独立 deployable/owned store，除非先接受新的架构 Issue。 |

当前 feed 的 <code>hours</code> 是相对每次请求的 server-now，而不是带 <code>as_of</code> 的事务快照。因此“过去 72 小时”只能冻结为**本次抓取观察到的 delivered Event manifest**。适配器应记录抓取开始/结束时间、所有 Event ID、详情 hash、页 ETag，并在分页结束后重新验证第一页；若第一页变化则有限次重抓。需要精确、可复现的历史切片时，应将“按固定 cutoff 导出只读 manifest”作为未来公开合同变更，而不是 DB reach-through。这一段是**设计推断**。

## 2. OpenTrade 第一方核查

### 2.1 语言与运行架构

**来源事实：** 固定树中可执行实现只有一个 Rust CLI（clap + tokio + reqwest），另有 Shell 安装器和七份 Markdown skill；没有 exchange gateway、订单服务、风控引擎或密钥存储服务端源码。[固定树](https://github.com/6551Team/opentrade/tree/0efa9b4d27fc644a667453c5c41e55ad0d04557d) [Cargo.toml](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/cli/Cargo.toml) [HTTP client](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/cli/src/client.rs)

运行形态是：

    operator / agent
      -> Rust opentrade CLI，或 skill 中的 curl
      -> HTTPS JSON https://ai.6551.io/open/trader/...
      -> 6551/NewsLiquid 未开源服务端
      -> CEX / DEX / chain

CLI 以 <code>OPENNEWS_TOKEN</code> 做 Bearer auth，默认 base URL 为 <code>https://ai.6551.io</code>，request timeout 30 秒；<code>--trader</code> 默认 <code>okx</code>、<code>--api</code> 默认 <code>v1</code>。[main.rs](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/cli/src/main.rs) [client.rs](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/cli/src/client.rs)

### 2.2 Venue / broker 支持

| 能力 | 第一方证据 | 判断 |
|---|---|---|
| CEX spot/perpetual | Binance、Bybit、OKX、Hyperliquid、Aster；skill 声称统一订单/持仓/leverage 接口。[CEX skill](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#supported-exchanges) | **官方声明，服务端不可核。** |
| DEX swap | quote、approve、生成 swap transaction；用户签名后由 gateway broadcast。[DEX workflow](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-dex-swap/SKILL.md#workflow-b-evm-swap-with-approval) [CLI source](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/cli/src/commands/swap.rs) | CLI 源码可核的是请求组装，不是聚合器实现。 |
| Gateway | gas、gas-limit、simulation、broadcast、order tracking；broadcast 接受已签 transaction。[gateway.rs](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/cli/src/commands/gateway.rs) | DEX 可把 signing authority 留在 agent 外。 |
| Custodial wallet | README 声称 Turnkey + AWS KMS，BSC/Solana，non-extractable。[README](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/README.md#opentrade-wallet) | **官方声明，服务端不可核。** 不是方案 A 必需依赖。 |
| 现金股票 broker | 仓库未列 Nasdaq/NYSE、Alpaca、IBKR 等证券 broker，也没有 equity order endpoint；Gamma 是只读 metadata。[command index](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#command-index) | **无公开合同。** MRVL/GOOGL/MRNA/SK Hynix 默认 research-only。 |

不能把“Binance 上可能存在某个 TradFi perp”推导成“OpenTrade 支持现金股票”。实际可交易性必须以调用当时 <code>/market/metadata</code> 返回的 exact symbol、venue、instrument type 为准，并与 News issuer identity 显式映射。此处是**设计推断**。

**2026-08-21 的本机只读 capability observation（未下单、未输出凭证）：**

| Economic exposure | OpenTrade metadata 当时可发现的表达 | 仍需阻断的错误推断 |
|---|---|---|
| MRVL | Binance / Bybit / OKX / Aster USDT perpetual；OKX dated future | 不能标成 Nasdaq MRVL 现金股。 |
| GOOGL | 上述 perpetual/future；另有 Hyperliquid spot | 不能忽略 share class、token issuer、赎回与 basis。 |
| SK hynix | Binance / Bybit / OKX / Aster perpetual；OKX dated future | 不能把 SKHY/SKHX/SKHYNIX alias 当成韩国现金股成交证明。 |
| MRNA | Bybit / Aster perpetual | 当前可发现不代表首条 Phase 3 推送时已上市；历史 metadata 未归档时必须标为 <code>unbacktestable</code>。 |

该 observation 只证明“现在的 discovery surface 能返回候选合约”。它不证明账户区域权限、历史可用性、报价/订单簿质量、成交能力、现金股 tracking quality 或 OpenTrade 服务端合同稳定性。生产设计必须把 <code>EconomicExposureRef</code> 与 <code>VenueInstrumentRef</code> 分离，并把 capability snapshot 与 observed-at/hash 写入 case evidence。

### 2.3 API 与 CLI 的真实差异

**来源事实：** CEX skill 文档列出 40 个 HTTP endpoints，覆盖 metadata/ticker/OHLCV/orderbook/funding/OI、账户、config、订单、持仓、历史、leverage/margin/position mode。[40-endpoint index](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#command-index)

但 Rust CLI 的顶层命令只有 <code>market</code>、<code>token</code>、<code>swap</code>、<code>trade</code>、<code>gateway</code>、<code>portfolio</code>；其中 <code>trade</code> 只实现 <code>routers</code>，**没有 CEX place/cancel/order/position/leverage 命令**。[main.rs](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/cli/src/main.rs#L48-L80) [trade.rs](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/cli/src/commands/trade.rs) CEX skill 是直接 curl 到 <code>/open/trader/newsliquid/v1/...</code>。

**设计推断：** 写一个小型 typed <code>OpenTradeHttpAdapter</code>，不 subprocess CLI。read-only methods 只暴露研究与 preflight 所需的 metadata/ticker/orderbook/account/positions/open-orders/position-mode；live write 只暴露内部 <code>submit_exact_order(ApprovedOrder)</code>，不把任意 URL/body 暴露给 LLM。Production adapter 与 replay/fake adapter 构成真实 seam。

### 2.4 订单、风控与凭证

**订单来源事实：**

- <code>POST /orders</code> 要求 exact CCXT symbol、side、明确 type，以及 quantity 或 quoteAmount；limit/trigger 类订单有条件必填字段；hedged 必填，未知时先 <code>GET /position/mode</code>。[Place Order](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#27-place-order-risk-controlled)
- 支持文档所列 market、limit、stop_market、take_profit_market，并可附 stopLossPrice/takeProfitPrice；README 另称七类订单，但详细 Place Order 只明确四类，属于文档漂移，不应猜剩余合同。[README](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/README.md#opentrade-cex)
- close position 要显式 <code>quantity > 0</code> 和正确 hedged；不能用 0 表示全平。[Close Position](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#33-close-position-risk-controlled)
- 文档没有 clientOrderId/idempotency key、time-in-force、paper/testnet 或 CEX dry-run 合同。DEX gateway simulation 不能推导为 CEX order simulation。[CEX skill](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md)

**风控官方声明、服务端不可核：** place order、close position、set leverage 会经过 rate limit、price deviation、position limit、balance check；默认阈值包括 30 req/min、最大偏离 10%、单仓 20%/总仓 80%、保留 5% balance。文档又明确：market data 或 Redis 不可用时检查 **fail open**。[Risk Engine Notes](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#risk-engine-notes)

**凭证官方声明、服务端不可核：**

- 客户端用 OPENNEWS_TOKEN Bearer；CEX config endpoint 接收 exchange apiKey、secret、可选 password。[Update Trading Config](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#26-update-trading-config)
- README 声称 exchange credentials encrypted at rest、server-side execution，Hyperliquid/Aster 使用 delegated ECDSA wallet agent，但公开仓库没有相应实现可审计。[README Security](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/README.md#security)

设计后果：远端风控只能当 defense-in-depth；本地 Deterministic Risk Kernel 必须 fail closed。OpenTrade token、exchange key、wallet material 不得进入 prompt、filesystem、checkpoint、case evidence 或日志；不让 agent 自己读取项目根 .env，而由 adapter 从进程 secret store 注入。这是**设计推断**。

### 2.5 License 与成熟度

| 观察 | 第一方证据 | 判断 |
|---|---|---|
| 根 license | 根 [LICENSE](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/LICENSE) 是 MIT。 | 来源事实。 |
| license 漂移 | CEX frontmatter 是 MIT；DEX skill 与 CLI README 写 Apache-2.0；Cargo.toml 未声明 package license。[CEX](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#L1-L9) [DEX](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-dex-swap/SKILL.md#L1-L9) [CLI README](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/cli/README.md#license) | 若 vendoring/redistribution，先让上游澄清。 |
| release | 一个公开 GitHub release [v1.0.2](https://github.com/6551Team/opentrade/releases/tag/v1.0.2)，发布 9 平台 CLI binary 与 checksums。 | release 是 CLI，不证明未开源 CEX backend 稳定。 |
| CI | workflow 在 Linux/macOS/Windows 执行 fmt、clippy、cargo test。[CI](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/.github/workflows/ci.yml) | 固定树中没有 Rust test function；“CI 绿”不是交易语义测试证据。 |
| 项目历史 | 固定树只有 20 commits，首个内容 commit 为 2026-04-03；核查时无公开 issue。[commits](https://github.com/6551Team/opentrade/commits/main/) [issues](https://github.com/6551Team/opentrade/issues) | **成熟度推断：早期、小样本公开面。** 零 issue 不等于无缺陷。 |
| 合同漂移 | CLI/release 为 1.0.2，main 上 CEX skill metadata 为 1.0.4；README/详细 order type、license、CLI/CEX surface 不一致。 | **推断：必须 pin commit + contract tests，先 read-only/paper。** |

## 3. Deep Agents harness 第一方核查

### 3.1 与 LangChain / LangGraph 的真实关系

**来源事实：** Deep Agents 不是新 runtime，而是 LangChain <code>create_agent()</code> 上的 opinionated harness；LangChain 创建 model/tools/middleware agent loop，LangGraph 拥有 graph state、streaming、checkpoint、interrupt/resume。<code>create_deep_agent()</code> 最终调用 LangChain <code>create_agent()</code> 并返回 CompiledStateGraph。[官方 architecture](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/ARCHITECTURE.md#the-three-layers) [graph source](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/graph.py)

    Deep Agents   harness：middleware、backends、profiles、subagents
    LangChain     agent loop：model + tools + middleware
    LangGraph     runtime：state + streaming + checkpoints + interrupts

deepagents 0.7.8 的 classifier 是 <code>Development Status :: 4 - Beta</code>，依赖 langchain >=1.3.16、langchain-core >=1.6，同时直接依赖 Anthropic、Google GenAI 与 LangSmith packages。[pyproject](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/pyproject.toml#L1-L35) 当前 Tracefold 仅依赖 langchain-litellm/langchain-core，没有 deepagents 或 langgraph runtime。[Tracefold pyproject](../../pyproject.toml) 这支持把 harness 放在独立 deployable，而不是扩大 News Workers 的依赖与失败域；后一句是**设计推断**。

### 3.2 能力矩阵

| 能力 | 当前第一方事实 | 方案 A 的明确配置 |
|---|---|---|
| Planning | 官方 overview 仍称内置 <code>write_todos</code>。[overview](https://docs.langchain.com/oss/python/deepagents/overview#planning-and-task-decomposition) 但 0.7.8 source 已把 TodoListMiddleware 变为 opt-in；默认 graph assembly 不加入它，仅列出的 OpenAI Codex profiles 显式加回。[graph](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/graph.py#L817-L944) [Codex profile](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/profiles/harness/_openai_codex.py#L69-L88) | Pin 0.7.8，并显式加入 LangChain TodoListMiddleware。Todo 只是研究进度，不是交易 state machine。 |
| Filesystem/context | 内置 ls/read/write/edit/glob/grep 走 pluggable backend；默认 StateBackend 是 thread-scoped，StoreBackend 可跨 thread，sandbox/LocalShell 才有 execute。[backends docs](https://docs.langchain.com/oss/python/deepagents/backends) 自动 summarization 与大 tool output offload 属于 middleware。[architecture](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/ARCHITECTURE.md#middleware-stack) | /scratch 用 State/sandbox；durable evidence 由 CaseStore 保存 hashes/manifests。不开 host filesystem，不让 agent 看 secret paths。 |
| Permissions | filesystem rule 按声明顺序 first-match，未匹配默认 allow；只覆盖内置 filesystem tools，不覆盖 custom/MCP tools。[permissions docs](https://docs.langchain.com/oss/python/deepagents/permissions) | 文件规则末尾 deny-all；所有 source/market tools 自身再做 allowlist、超时、schema、redaction。 |
| Sync subagent | 默认加入 general-purpose，task 同步等待；custom declarative subagent、CompiledSubAgent 均可用，并可返回 structured output。[subagents docs](https://docs.langchain.com/oss/python/deepagents/subagents) [source types](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/middleware/subagents.py#L29-L190) | 禁用泛化 default，显式提供三个窄角色；各自只有 read tools 和 typed response。 |
| Async subagent | remote/background spec 可 launch/check/update/cancel/list；不继承主 agent HITL。[create_deep_agent source](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/graph.py#L268-L580) | V1 不需要 remote async；先用受控 sync fan-out。 |
| Checkpoint | checkpointer 参数交给 LangChain/LangGraph；保存 graph state、messages、interrupt 与 resumability。filesystem/memory persistence 是另一套 backend/store。[architecture](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/ARCHITECTURE.md#state-and-persistence) | 独立持久 checkpointer + stable thread id；不充当 proposal/order/approval ledger。 |
| HITL | interrupt_on 安装 HumanInTheLoopMiddleware，支持 approve/edit/reject；需要 checkpointer，并用同一 thread id resume。[HITL docs](https://docs.langchain.com/oss/python/deepagents/human-in-the-loop) [integration test](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/tests/integration_tests/test_hitl.py#L65-L145) | HITL 暂停并呈现 proposal；真正授权仍由 review_case 验证 operator、revision、digest、expiry。 |
| Structured output | main agent 与 declarative subagent 都支持 response_format；subagent structured result JSON-serialize 给 parent。[graph](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/graph.py#L268-L580) [SubAgent](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/middleware/subagents.py#L29-L190) | 每个 role 返回严格 schema；一次有界修复仍失败则 research_invalid，绝不 baseline 下单。 |

Planning 一行存在官方网页与 0.7.8 release source 的时间差。生产设计应以**锁定版本源码优先，显式配置优于假设默认**。

## 4. 方案 A：Trading Case 深模块

### 4.1 Seam

这是**设计推断**：新建独立 deployable（暂称 trading-cases），不加入当前 Tracefold Workers TaskGroup、RabbitMQ topology、News PostgreSQL schema 或 <code>tracefold.news</code> package root。

    Tracefold read-only HTTP             official disclosures / trial registries
      delivered Event snapshots                         |
              |                                          v
              +----------> Trading Case module <--- market/account reads
                                |
                        Deep Agents research harness
                        (no live-order tool exposed)
                                |
                        deterministic Risk Kernel
                                |
                        exact proposal + human review
                                |
                         OpenTradeHttpAdapter
                                |
                      one submit attempt + reconcile

删除测试：若删掉 Trading Case，调用方将被迫各自重做 72 h 快照、证据冻结、subagent 编排、ticker/issuer/instrument 映射、风控、审批 digest、OpenTrade preflight、模糊下单与对账；复杂度会重新散落，因此该 module 有足够 depth。

### 4.2 Interface：恰好三个入口

概念接口，不是实现代码：

    class TradingCases:
        async def open_case(self, request: OpenCase) -> CaseRef: ...
        async def get_case(self, case_id: CaseId) -> CaseSnapshot: ...
        async def review_case(
            self, case_id: CaseId, command: ReviewCommand
        ) -> CaseSnapshot: ...

#### 1. open_case(request) -> CaseRef

OpenCase 最小字段：

    idempotency_key
    news_selector = DeliveredWindow(hours=72) | EventIds([...])
    observed_as_of_ms
    mandate_id
    mode = research_only | paper | live   # 默认 research_only
    focus_event_ids?                      # 排序提示，不改变 corpus

语义：立即创建 durable case、冻结输入选择规则并返回 case_id/revision/state；外部检索与模型异步继续。相同 principal + idempotency_key + canonical request 返回同一 case；key 相同而 body 不同返回 conflict。

#### 2. get_case(case_id) -> CaseSnapshot

返回唯一观察面：

    state / revision / timestamps
    source_manifest[event_id, event_hash, etag, fetched_at_ms]
    research_status / subagent status / evidence conflicts
    dossiers[]
    proposals[]
    risk_assessment
    approval_request?          # exact proposal_digest + expiry
    execution_receipts[] / reconciliation / ambiguity

Dossier 至少包含：已核实事实、一手来源、事件与价格时间轴、是否 price-in、bull/base/bear mechanism、horizon、invalidators、下一催化剂、可交易表达、不可交易原因、confidence 与 evidence gaps。模型自由文本不能替代 typed fields。

#### 3. review_case(case_id, command) -> CaseSnapshot

ReviewCommand 是封闭 union：

    Approve(expected_revision, proposal_digest, approval_expires_at_ms)
    Edit(expected_revision, proposal_digest, allowed_patch, reason)
    Reject(expected_revision, proposal_digest, reason)

- Approve 只对 digest 完全匹配、未过期且 revision 相同的 proposal 生效。Live 模式先做 fresh preflight；若 price、position、balance、mode 或 metadata 超出批准 tolerance，则产生新 revision 并重新等审批，绝不按更差条件继续。
- Edit 只允许改数量上限、limit/trigger、expiry 等白名单字段；任何编辑都重新跑 risk、生成新 digest，不能边改边成交。
- Reject terminalize 当前 proposal；重新研究必须新 revision/case，不改历史。

没有单独 execute：执行是**已批准 Case 的内部 state transition**。调用方不需要学习 OpenTrade 的 40 个 endpoints、hedged、symbol format 或错误 envelope，这是方案 A 的主要 leverage。

### 4.3 Interface invariants

1. **News read-only**：只读取公开 HTTP；不写 News 表、verdict、delivery、label 或 broker queue。
2. **Frozen evidence**：每个结论与 proposal 绑定 Event/detail hash、官方来源 hash、market snapshot timestamp、model/prompt/tool schema/policy versions。
3. **Research cannot authorize**：LLM、subagent、tool result、Deep Agents HITL resume payload 都不能单独授权 live order。
4. **Exact instrument identity**：issuer/underlying、asset class、venue instrument ID、exact exchange symbol 四者必须解析；ticker-only、别名-only、token/equity collision 一律 instrument_ambiguous。
5. **Fail closed**：无新鲜 price/book/account/position/mode、无法算 exposure、数据冲突或 provider degraded 时，不生成 executable proposal。OpenTrade fail-open risk 不得放宽。
6. **Human-bound approval**：live approval 绑定 authenticated operator、case revision、proposal digest、account/venue、side/type/quantity/price/TP/SL、最大滑点/漂移、expiry；字段变化即失效。
7. **One approved intent, at most one submit attempt**：公开 OpenTrade contract 没有 idempotency key。请求送出后 timeout/crash 记 execution_ambiguous，禁止自动重发；先读 orders/positions/trades 对账。
8. **Order ledger is authority**：CaseStore append-only transition/receipt 是业务证据；LangGraph checkpoint 只负责 research resume，不能推断订单未发或已发。
9. **Credentials never enter agent context**：token/key/password/signature 只在 adapter/secret manager；tool I/O、filesystem、trace、checkpoint、snapshot 统一 redaction。
10. **No hidden auto-trade fallback**：模型 timeout、invalid output、citation conflict、checkpoint failure 都只阻断交易，不做 News Triage 式 degraded baseline。
11. **Exit is part of intent**：live opening proposal 必须有 holding horizon 与 invalidation/exit；若 venue 支持并验证，优先把 approved TP/SL 随订单一次提交。扩大风险须重新批准。
12. **Mode monotonicity**：research-only/paper case 不能原地升级 live；live 必须新 case 或明确新 mandate。

### 4.4 Ordering

    OPENED
      -> SNAPSHOTTING
      -> RESEARCHING (official facts + market reaction + bear case, parallel)
      -> SYNTHESIZING
      -> RISKING
      -> RESEARCH_ONLY_COMPLETED | EXECUTION_UNAVAILABLE | AWAITING_APPROVAL
      -> PREFLIGHTING
      -> AWAITING_APPROVAL again if material drift
         or SUBMITTING (one attempt)
      -> RECONCILING
      -> COMPLETED | REJECTED | BLOCKED | EXECUTION_AMBIGUOUS

- Fact verification 先于 thesis；instrument resolve 先于 risk；risk 先于 approval；fresh preflight 在 approval 后、submit 前。
- 同一 case 只有一个 transition writer；expected_revision 做 optimistic concurrency。
- SUBMITTING 前先 durable-write intent/digest/attempt；response 后写 raw response hash、remote order ID 与 normalized receipt。
- Ambiguous 状态只允许 reconcile 或 operator terminal action，不允许 agent 再试一次。

### 4.5 Error modes

| Stable error | 条件 | 自动处理 | 结果 |
|---|---|---|---|
| news_snapshot_changed | 分页中首屏 ETag/manifest 变化 | 有界重抓两次 | 仍变化则 blocked。 |
| news_source_unavailable | Tracefold auth/HTTP/schema 失败 | GET 可退避 | 不形成 proposal。 |
| evidence_conflict | 一手来源冲突或关键条款无法确认 | 不重试 | 可出 dossier，不可执行。 |
| research_invalid | structured output 两次仍不合 schema/缺 citation | 一次修复 | 无交易 fallback。 |
| instrument_ambiguous | ticker collision、issuer/venue mapping 不唯一 | 不重试 | 人工修 mapping policy。 |
| execution_unavailable | 无现金股票 broker、venue/region/account 不支持 | 不重试 | research-only。 |
| risk_rejected | exposure/drawdown/size/leverage/freshness/mandate 失败 | 不重试 | 改 proposal 必须新 revision。 |
| approval_conflict | revision/digest/operator/expiry 不匹配 | 不重试 | Conflict，不执行。 |
| preflight_drift | price/position/balance/mode 超 tolerance | 新 proposal | 必须重新审批。 |
| provider_rate_limited | OpenTrade 429，且尚未 submit | 有界 backoff | 保持 preflight。 |
| provider_auth_or_region | 401/403/region restriction | 不重试 | blocked，只返回 redacted reason。 |
| order_rejected | OpenTrade/exchange 明确拒单 | 不重试 | terminal receipt。 |
| execution_ambiguous | submit 后 timeout/断线/crash/无法解析 | **禁止重发** | reconcile；不能证明则人工。 |
| checkpoint_unavailable | Deep Agents graph 无法 resume | research 可重建 | 以 CaseStore 判断交易状态。 |

Performance：open_case 只 durable-ack，目标秒级；研究是分钟级异步；get_case 是有界只读；Approve 只允许有限 preflight + 一次 submit。外部调用超 deadline 就返回 ambiguous/executing，不无限阻塞。这里是**设计目标**，不是上游保证。

## 5. Harness 在 implementation 内部如何用

以下均为**设计推断**。

### 5.1 三个窄 subagents，不给 live-order tool

| subagent | 只读工具 | typed 输出 | 对手工案例的关注点 |
|---|---|---|---|
| official-fact-verifier | Event detail；issuer IR/filing、交易所披露、ClinicalTrials.gov/监管源等 allowlisted fetch | FactSet(claim, source, observed_at, primary, conflict) | MRVL/Google：协议双方、范围、期限、财务披露；SK Hynix：回购规模、执行/注销；MRNA：Phase III endpoint、统计/临床意义、安全性、监管下一步。 |
| market-reaction-analyst | OpenTrade read-only metadata/ticker/OHLCV/orderbook/funding/OI；未来 cash-equity market adapter | ReactionSet(pre_event, post_event, benchmark, liquidity, price_in) | 区分事实利多与价格已反映；严格按 Event delivery/publication time，禁止事后数据泄漏。 |
| adversarial-thesis-reviewer | 前两者 frozen artifacts | ChallengeSet(alternative, disconfirming_evidence, invalidator, missing) | 强制 bear case、交易表达错配、后续条件和不交易理由。 |

Supervisor 只合并 typed outputs 为 Dossier，不再次自由搜索；evidence acquisition 与 synthesis 分离。三者可并行，但每个 tool 有 allowlist、deadline、response-size cap 与 redaction。

### 5.2 Harness 配置

- Pin deepagents 0.7.8 和 model；显式传 TodoListMiddleware，不依赖 planning 默认。
- 禁用 default general-purpose，只注册三个窄角色。
- response_format 用严格 schema；model、prompt、tool schema、source adapter version 写 manifest。
- Scratch 使用受限 sandbox/StateBackend；不使用 host-wide filesystem。Filesystem rule 最后一条 deny-all；custom tool 自己实施安全。
- 独立 persistent checkpointer，stable thread_id = case_id:research_revision；CaseStore 独立保存业务 transition。
- <code>interrupt_on=stage_trade_proposal</code> 可提供 UX pause，但该 tool 只写 pending proposal，不触碰 OpenTrade。外部 review_case 才是业务授权。
- 不把 OpenTrade submit 注册成 LLM tool；由批准后的确定性 orchestrator 调用。

所以，对“是否需要 DEEPAGENT”的回答是：**72 小时、多事件、多来源、反方验证和大上下文时，Deep Agents 是合适 harness；它不是 Trading module 的外部 Interface，也不是风控/订单架构。** 对单条新闻的一次判断，现有 Triage 已足够，不应无条件再跑 Deep Agent。

## 6. 72 小时研究与回测口径

### 6.1 Corpus

1. 调 <code>GET /api/news/feed?hours=72&outcome=pushed&sort=latest&limit=100</code>，遍历 cursor；保存第一页 ETag 与抓取窗口。
2. 对每个 Event 调 detail endpoint，冻结 member sources、Triage trace、delivery time、assets/normalization 与 detail hash。
3. Universe 是读者实际收到的 <code>delivery.state=sent</code>，不是全部 candidate 或模型建议 push。
4. 记录 published/opened/delivered 时间；交易可用时间取策略当时实际可见的较晚者，禁止 look-ahead。

### 6.2 Market outcome 在 Trading 模块预先定义

Tracefold 没有 price lane，所以 mandate 必须固定：

- instrument 与 benchmark（个股对 sector/index、perp 对 spot/index）；
- entry 规则（delivery 后第一笔可交易 quote、下一根 bar、开盘 auction）；
- horizon（例如 30m、4h、session close、next close、72h），不能看结果再选；
- gross/abnormal return、最大顺/逆向波动、spread/slippage/fee/borrow/funding；
- market calendar、停牌、盘前盘后、币股衍生品 basis；
- missing data 与 survivorship policy。

MRVL/Google 协议、SK Hynix 回购、MRNA Phase III 是很好的**事件类型 probe**，但各一个手工案例不能证明 strategy。先把 72 h corpus 全量跑成 research-only dossier，再扩展为跨同类事件和不同 regime 的 frozen corpus；candidate policy 按时间顺序 replay，避免只挑成功故事。这是**方法设计**，不是三条新闻收益结论。

### 6.3 案例的可执行性预期

| News issuer | 2026-08-21 observation | 方案 A 输出 |
|---|---|---|
| MRVL / GOOGL | 可发现多个 TradFi perpetual/future；GOOGL 另有 Hyperliquid spot；无现金美股 broker | 现金股为 <code>execution_unavailable</code>；衍生品只进入 shadow/paper candidate，并显式列出 basis/funding/session/liquidity 风险。 |
| SK Hynix | 可发现多个 TradFi perpetual/future；无韩国现金股 broker | 精确解析 SKHY issuer 与 venue contract；现金股 unavailable，闭市期衍生品的 basis 风险必须 fail closed。 |
| MRNA | 当前可发现 Bybit/Aster perpetual，但首条新闻约 150 分钟后才出现 Hyperliquid candle | 历史触发时未证明可执行，返回 <code>unbacktestable</code>/<code>execution_unavailable_at_cutoff</code>；不能用今天的 metadata 回填过去。 |

未来新增受监管 cash-equity broker，只需增加 ExecutionVenue adapter；Trading Case Interface 不变。

### 6.4 本次 72 小时 snapshot 的描述性结果

可执行 Notebook 见 [trading-agent-72h-event-study.ipynb](./trading-agent-72h-event-study.ipynb)。它在 2026-08-20T16:56:01Z（Asia/Taipei 2026-08-21T00:56:01+08:00）观察到：

- 3,652 个 Event 中，776 个落在 <code>outcome=pushed</code>，2,876 个 held；delivered share 为 21.25%。这是相对执行时刻的滑动窗口，不是事务级固定 cutoff。
- 透明标题规则把 MRVL–Google、SK hynix、MRNA 三个 episode 分别归入 7、8、14 个已推送 Event。MRNA 数包含催化剂、价格回声、tokenized-equity 上线、评级与隔日回撤；这些条目不能作为 14 份独立置信度。
- 第一条 MRVL 事件在 2026-08-19T12:26:31Z 打开，首张卡约 5.3 秒后 settled；SK hynix 为 06:41:39Z；MRNA 为 11:11:35Z。

以下是公开 1 分钟序列的 gross、无费用结果；entry 是第一条推送后第一根可见 close，固定 exit 是首推后 120 分钟：

| 表达 | 推送前 60m | 立即 entry → +120m | 延迟 5m → +120m | 延迟 30m → +120m | 解释 |
|---|---:|---:|---:|---:|---|
| MRVLUSDT | +6.09% | +2.30% | -1.64% | -5.01% | 事实虽由 SEC 8-K 确认，但 feed 首推前已明显 price-in；追涨结果对延迟高度敏感。 |
| GOOGLUSDT | -0.17% | +0.42% | +0.38% | +0.17% | 协议对 Google 的短时价格表达弱，不能把 MRVL 的方向直接复制给 GOOGL。 |
| SKHYUSDT | +0.70% | +8.01% | +4.68% | +2.42% | 回购由公司材料确认，但事件发生在韩国现金市场收盘后；perp 回报必须连同 basis/流动性风险解释。 |
| MRNA 原生股（Yahoo 二级行情） | +50.95% | +27.73% | +21.67% | +12.67% | 第一条卡时相对前收已约 +47.56%；该价格路径不是 OpenTrade 可执行性证明。 |

MRNA 是最重要的安全反例：wire headline 写的是 INTerpath-001 个性化癌症疫苗 Phase 3，存量 <code>why_zh</code> 却写成“流感疫苗三期”；ClinicalTrials.gov 当时仍显示 <code>No Results Posted</code>、预计 2029 年 primary completion，且没有找到当日 Moderna/Merck filing。Hyperliquid <code>xyz:MRNA</code> 的第一根公开 candle 又比首推晚约 150.4 分钟。即使事后价格继续上涨，正确的 point-in-time 交易输出仍应是 <code>source_unverified + execution_unavailable_at_cutoff → no_trade</code>，而不是用事后收益替模型错误辩护。

这只是三个 probe 的 descriptive replay，不是 strategy validation。正式 promotion gate 至少需要数百个按 episode 合并的事件、预注册 entry/horizon、benchmark/abnormal return、spread/fee/slippage/funding、失败成交、停牌/交易日历、历史 instrument metadata 与 walk-forward 检验。

## 7. 调用示例

概念性示例，caller 只学习三个入口：

    case = await trading_cases.open_case(
        OpenCase(
            idempotency_key="delivered-72h:2026-08-21T12:00+08:00:research-v1",
            news_selector=DeliveredWindow(hours=72),
            observed_as_of_ms=1787284800000,
            mandate_id="research-only-v1",
            mode="research_only",
            focus_event_ids=(
                mrvl_google_event,
                sk_hynix_buyback_event,
                mrna_phase3_event,
            ),
        )
    )

    snapshot = await trading_cases.get_case(case.case_id)
    # dossiers 覆盖完整 delivered corpus；三个 Event 重点展示。
    # 这些现金股票 proposal 预期为 execution_unavailable。

未来存在明确受支持的 crypto/perpetual proposal 时，live 审批仍走相同 Interface：

    approved = await trading_cases.review_case(
        case_id,
        Approve(
            expected_revision=snapshot.revision,
            proposal_digest=snapshot.approval_request.proposal_digest,
            approval_expires_at_ms=snapshot.approval_request.expires_at_ms,
        ),
    )

    final = await trading_cases.get_case(case_id)
    # receipt 给出 accepted/rejected/ambiguous + reconciliation；
    # caller 从不传 token、hedged 或任意 URL。

## 8. Implementation 隐藏的复杂度

- Tracefold feed pagination、ETag revalidation、detail fetch、delivered manifest 与 hash。
- Event/issuer/instrument identity，尤其 equity/token ticker collision、SKHY/SKHX/SKHYNIX normalization、spot/perp/TradFi derivative。
- Deep Agents profile/middleware、显式 planning、context quarantine、filesystem、summarization、checkpoint、HITL resume。
- 一手来源 allowlist、citation normalization、文档时间与 Event 可见时间对齐、证据冲突。
- 市场时序、benchmark、price-in/abnormal-return、slippage/fee/funding/borrow、missing data。
- Deterministic mandate/risk/sizing、proposal canonicalization/digest、revision/approval/expiry。
- OpenTrade envelope、exact CCXT symbol、precision/minimum、hedged、账户/持仓/余额 preflight、quota/rate limit。
- One-attempt submit、crash/timeout ambiguity、orders/trades/position reconcile、redaction/audit。
- Paper/replay/live adapter 选择；caller 无需了解内部细节。

## 9. 依赖类别与 adapters

| 类别 | 依赖 | seam / adapter 策略 |
|---|---|---|
| In-process | canonical schema、digest、case transition、risk/sizing、freshness、instrument eligibility、backtest math | 放入 deep implementation；测试通过三个入口观察，不为每个小函数暴露 port。 |
| In-process library | Deep Agents、LangChain、LangGraph | 固定版本；只在 research implementation 可见，不泄漏 graph/checkpoint type 到 Interface。 |
| Local-substitutable | TradingCaseStore、evidence blob、persistent checkpointer、clock | Production 用独立 Postgres/object store；测试用临时 Postgres/in-memory/fake clock。CaseStore 与 checkpointer 责任分离。 |
| Remote but owned | TracefoldNewsRead、operator auth/review UI | HTTP adapter + fixture adapter；只依赖公开 /api/news schema。 |
| True external | OpenTrade、LLM、official filing/IR/trial sources、market data、未来 cash-equity broker | 每个 seam 至少 production + replay/fake adapter；timeout、quota、schema drift、redaction 在 adapter 内。 |
| Secret capability | OpenTrade token、exchange/broker credentials、wallet signing | Adapter 从 secret manager 注入；agent/backend/checkpointer 不可见。未来 DEX signer 独立。 |

不要预先做万能 BrokerPort。V1 的真实变化只有 OpenTrade HTTP 与 fake/replay；未来 cash equity adapter 出现后，再提炼双方共有的最小 ExecutionVenue contract。不要把 40 个 endpoint 一比一暴露为浅 Interface。

## 10. Trade-offs

### 高 leverage

- 三个入口覆盖 72 h batch、单 Event 深查、research-only、paper、live review 与事后 inspect。
- Agent/runtime、source acquisition、risk、approval、venue quirks、reconcile 都可内部替换，caller 不变。
- Deep Agents 出问题只影响 research；Tracefold News 与 order ledger 不随它失败。
- 新 venue adapter 不要求修改 News schema、queue 或 verdict vocabulary。

### 代价

- 独立 deployable/owned persistence 增加运维面，但隔离交易权限并保护 News 单能力。
- open_case 是异步 eventual result；caller 需读取 snapshot，不是单次请求拿完整答案。
- OpenTrade 未公开 idempotency/paper 合同，不能宣称 live exactly-once；one-attempt + ambiguity 避免双单，却增加人工 reconcile。
- 不给 LLM live tool 牺牲“全自动”的表面速度，但提高权限 locality、测试性与可审计性。
- 对现金股票案例，V1 只能研究不能成交；这是正确暴露 capability gap。
- Deep Agents 0.7.8 为 Beta，planning 文档与源码漂移；版本 pin、contract/eval tests、升级审查是持续成本。

## 11. 推荐门槛（不在本次实现范围）

1. **Research-only gate**：冻结过去 72 h delivered corpus；对 MRVL/GOOGL、SK Hynix、MRNA 等生成有一手来源、反方观点、可见时间和 execution_unavailable 的 dossier。
2. **Paper gate**：用 frozen market snapshots 做无前视 replay；operator label proposal quality；不得调用 OpenTrade write。
3. **OpenTrade canary gate**：上游书面确认 endpoint version、account/region、idempotency/ambiguity、credential custody、risk fail-open；完成 schema/contract tests 与极小额度手工审批 canary，仅限明确支持的 crypto/perp。
4. **Cash equity gate**：接受受监管 broker adapter、market data/borrow/calendar/compliance mandate 后，才允许 MRVL/GOOGL/MRNA/SK Hynix live proposal。

## 12. 可追溯来源索引

### Tracefold

- [Architecture](../ARCHITECTURE.md)
- [Public contracts](../CONTRACTS.md)
- [Domain router](../agents/domain.md)
- [News public Interface](../../src/tracefold/news/__init__.py)
- [News outcome](../../src/tracefold/news/outcome.py)
- [News feed repository](../../src/tracefold/news/repository.py)
- [LangGraph checkpoint hard cut](../../src/tracefold/platform/postgres/alembic/versions/20260818_0276_review_49_hard_cut.py)

### OpenTrade

- [Pinned tree](https://github.com/6551Team/opentrade/tree/0efa9b4d27fc644a667453c5c41e55ad0d04557d)
- [README](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/README.md)
- [CEX skill](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md)
- [CLI README](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/cli/README.md)
- [Rust CLI main](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/cli/src/main.rs)
- [Rust HTTP client](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/cli/src/client.rs)
- [CEX CLI trade command](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/cli/src/commands/trade.rs)
- [DEX swap command](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/cli/src/commands/swap.rs)
- [Gateway command](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/cli/src/commands/gateway.rs)
- [v1.0.2 release](https://github.com/6551Team/opentrade/releases/tag/v1.0.2)
- [MIT root LICENSE](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/LICENSE)

### Deep Agents / LangChain / LangGraph

- [Deep Agents 0.7.8 release](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.8)
- [README](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/README.md)
- [Architecture](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/ARCHITECTURE.md)
- [create_deep_agent source](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/graph.py)
- [Subagent source](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/middleware/subagents.py)
- [Package metadata / Beta classifier](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/pyproject.toml)
- [Official overview](https://docs.langchain.com/oss/python/deepagents/overview)
- [Official backends guide](https://docs.langchain.com/oss/python/deepagents/backends)
- [Official subagents guide](https://docs.langchain.com/oss/python/deepagents/subagents)
- [Official HITL guide](https://docs.langchain.com/oss/python/deepagents/human-in-the-loop)
- [Official LangGraph persistence guide](https://docs.langchain.com/oss/python/langgraph/persistence)
