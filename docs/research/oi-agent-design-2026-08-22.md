# OI Agent 设计调研：1019 OI Event Monitor → OpenTrade 自动执行

调研日期：2026-08-22。作者：Claude（本机只读调研，未下单、未修改运行时配置、未输出凭证值）。

## 结论先行

1. **可以做，但它不是"第二个 News agent"。** 触发面是纯数值（provider 直接给结构化 metrics），DSPy/LLM 在触发决策上贡献为零。把 LLM 放进触发路径只会引入不确定性、成本和一个无法回放的决策。
2. **你的规则里，`rank ≤ 2` 是真有货的一半，`Whale/OI > 100` 基本是个交易所代理变量。** Hyperliquid 帧的 whale_oi_ratio 中位数 83.2%、42.3% 超过 100；Binance 帧中位数 41.3%、只有 0.5% 超过 100。近 19 小时内命中"wr>100 且 rank≤2"的 34 条帧，**100% 来自 Hyperliquid**。你以为在筛鲸鱼，实际在筛交易所。
3. **当前证据不足以上真钱。** 我拉了 754 条 1019 历史触发、对齐 630 条 Binance 1m K 线。你的规则在全语料里只有 20 条命中、按"同 symbol 4h 去重"后只有 **15 个独立 episode**，置换检验 p = 0.223。4 小时超额收益 +2.35%（对 BTC）、胜率 79% 看着漂亮，但它来自 13 个 symbol、一个 14 小时窗口。这是**假设**，不是策略。
4. **最重要的执行事实：这个信号总是在行情之后触发。** 全语料触发前 1 小时平均已涨 +7.25%，91% 为正。Binance 帧 +7.8%，Hyperliquid 帧只有 +2.7% —— HL 帧更好的真正原因是它**领先度更高**，而不是鲸鱼比例更高。
5. **好消息：延迟容忍度极高。** 入场延迟 0/5/15/30 分钟，到 4h 的收益是 +2.14%/+2.05%/+1.91%/+1.71%。这意味着**一次 LLM 结构化调用（秒级）在预算内绰绰有余** —— 但它该用来做「否决」和「复盘」，不是「触发」。
6. **OpenTrade 的三个硬缺口决定了架构成本，而不是模型：** 没有 `clientOrderId`/幂等键、没有 testnet/paper、风控引擎明确 **fail-open**、没有时间止盈也没有追踪止损。这三件事必须由你自己的确定性内核补齐。

---

## 一、信号体检（第一手数据）

### 1.1 语料与口径

| 项 | 值 |
|---|---|
| 触发历史来源 | `GET https://ai.6551.io/open/strategy_hits?strategyId=1019`（Tracefold recovery 用的同一个官方端点） |
| 分页 | limit=100，第 9 页起空；共 **754 条唯一命中** |
| 时间跨度 | 2026-08-05T14:17+08:00 → 2026-08-22T12:52+08:00，**有断档**（08-07~08-11、08-17~08-20 无数据，应为 Strategy 在账号侧被关过，或端点有 ~800 条上限） |
| 价格数据 | Binance USD-M 永续 1m K 线（公开 REST，无凭证） |
| 对齐成功 | 630 条（`HFT`/`VIC` 无 Binance 永续；其余因窗口不足 245 根丢弃） |
| 入场定义 | 触发后**第一根 1m K 线收盘价**（保守、可成交） |
| 成本模型 | **无**。未计手续费、funding、点差、滑点、部分成交 |

**一个顺带的好消息**：`slip_vs_trigger`（第一根收盘价 vs provider 在帧里给的 `price`）中位数 −0.01%、p10/p90 = −1.10/+1.01%。provider 报的价基本可成交，不存在"报价已经跑掉"的系统性问题。

### 1.2 帧的真实结构（比你看到的那句英文多得多）

`strategy_hits` 和 WSS 帧都带 `strategy.metrics`：

```json
{"matched_group_id":"default",
 "oi_rise_pct":{"unit":"percent","value":3.4223755468280923},
 "open_interest_value":{"unit":"value","value":243185200.6454},
 "price":1.6679,
 "whale_long_profit_rate":{"unit":"percent","value":86.22754491017965},
 "whale_oi_ratio":{"unit":"percent","value":69.34513101226824}}
```

外层还有 `source`（**venue**：`binance` / `hyperliquid`）和 `coins[0].symbol`。

> **仓库缺口（可核实）**：`src/tracefold/news/opennews.py` 的 `_provider_metadata()` 只保留 `score/source/signal/grade/coins/strategies`，**把 `strategy.metrics` 整个丢掉了**。DB 里 169 条 1019 item 的 `provider_metadata` 没有任何数值字段，只能从 `raw_first_line` 正则回收（2 位小数，精度有损）。OI agent 要用全精度数值，必须让 Receiver 把 metrics 原样带出来。

### 1.3 全体基准（N=630）

| 指标 | 均值 | 中位 | 胜率 |
|---|---|---|---|
| +5m | +0.23% | +0.13% | 54% |
| +15m | +0.12% | +0.02% | 50% |
| +1h | −0.41% | +0.05% | 51% |
| +4h | +0.04% | −0.01% | 50% |
| **触发前 1h 涨幅** | **+7.25%** | **+5.34%** | **91% 为正** |
| 1h 最大不利偏移 | −5.14% | −2.80% | — |
| 4h 最大不利偏移 | −8.10% | −5.12% | — |

**裸信号没有边缘。** 平均 4h 收益 +0.04%，扣掉手续费和 funding 就是负的。所有价值都在过滤条件里。

### 1.4 你的两个条件，分开体检

**`rank`（24h 内第几次出现）—— 这条是对的：**

| rank | N | +1h | +4h | 胜率(4h) | 触发前1h |
|---|---|---|---|---|---|
| 1 | 168 | −0.34% | +0.66% | 46% | +5.0% |
| 2 | 113 | +0.21% | **+1.33%** | 52% | +6.2% |
| 3–5 | 159 | +0.17% | +0.53% | 53% | +7.5% |
| **>5** | 190 | −1.32% | **−1.69%** | 48% | **+9.6%** |

单调、方向合理、机制清楚：同一个币在 24h 内被反复触发，说明行情已经走完，你在追一个耗尽的动能。**rank ≤ 2 的经济含义就是"别追"**，而且样本量够（N=281）。

**`Whale/OI > 100` —— 这条主要在筛交易所：**

| venue | N | whale_oi 中位 | p90 | >100 占比 | 触发前1h | +4h |
|---|---|---|---|---|---|---|
| binance | 559 | 41.3% | 61.2% | **0.5%** | +7.8% | −0.04% |
| hyperliquid | 71 | 83.2% | 298.6% | **42.3%** | **+2.7%** | +0.65% |

全语料 33 条 wr>100 中，30 条来自 Hyperliquid。近窗（08-21~08-22）"wr>100 且 rank≤2"的 34 条命中，**全部**来自 Hyperliquid。

原因不难猜：Hyperliquid 的持仓是链上公开的，鲸鱼仓位是**观测值**；Binance 的是**估计值**，两个交易所的 `whale_oi_ratio` 根本不是同一把尺子。你的阈值 100 正好卡在这条分界线上。

**同窗口、同交易所的对照（这是唯一公平的检验）**，只看 08-21/08-22：

| 切片 | N | +1h | +4h | 胜率(4h) | 触发前1h |
|---|---|---|---|---|---|
| 窗口全体 | 143 | −0.47% | −0.26% | 53% | +5.0% |
| 仅 HL | 57 | +0.71% | +1.35% | 65% | +2.6% |
| HL & rank≤2 | 41 | +0.53% | +1.44% | 68% | +2.1% |
| HL & rank≤2 & **wr>100** | 19 | +0.62% | **+2.26%** | 74% | +1.8% |
| HL & rank≤2 & wr≤100 | 22 | +0.46% | +0.73% | 64% | +2.4% |

控制掉交易所和时间窗后，`wr>100` 仍有 +2.26% vs +0.73% 的差距 —— 但那是 19 比 22 的样本，差距完全在噪声里。**"HL vs Binance"（+1.35% vs −0.26%）比"wr>100 vs wr≤100"重要得多。**

### 1.5 两个免费的额外过滤器

| 切片 | N | +1h | +4h | 胜率 |
|---|---|---|---|---|
| whale_long_profit 70–85% | 113 | −0.49% | −0.95% | 46% |
| whale_long_profit 85–95% | 298 | −0.75% | −0.60% | 49% |
| **whale_long_profit 95–100%** | 219 | +0.10% | **+1.42%** | 53% |
| OI 规模 10–50M | 274 | −0.80% | −0.77% | 48% |
| OI 规模 >200M | 8 | +1.87% | +3.12% | 75% |

`whale_long_profit ≥ 95%`（几乎所有鲸鱼多头都在盈利）是个 N=219 的、方向一致的过滤器，比 whale_oi_ratio 那条样本厚得多，值得进候选。

### 1.6 一个更本质的替代假设（已检验）

既然 HL 帧的优势在"触发时行情还没走完"，那就**直接用领先度过滤**，这是 K 线可确定性计算的，不依赖 provider 的鲸鱼口径：

| 触发前 1h 涨幅 | N | +4h | 超额(减BTC) | 1h MAE 中位 |
|---|---|---|---|---|
| <1% | 90 | −0.50% | −0.58% | −2.08% |
| **1–3%** | 107 | **+1.27%** | **+1.46%** | −1.56% |
| 3–6% | 158 | +0.80% | +0.88% | −1.96% |
| 6–12% | 151 | −0.77% | −0.81% | −3.35% |
| >12% | 124 | −0.61% | −0.56% | **−8.20%** |

倒 U 形，且 MAE 随涨幅单调恶化 —— 追得越高、被打的越狠。这条的样本量是你规则的 5–7 倍。

**四条规则正面对比**（全语料、同 symbol 4h 去重）：

| 规则 | 独立 N | +4h 均值 | +4h 中位 | 胜率 | 超额 | 1h MAE 中位 |
|---|---|---|---|---|---|---|
| 你的规则 wr>100 & rank≤2 | **15** | +1.73% | **+0.89%** | **67%** | +1.87% | −1.00% |
| venue=HL & rank≤2 | 40 | +1.06% | +0.88% | 62% | +1.22% | −1.00% |
| pre1h<3% & rank≤2 | 91 | +1.13% | +0.00% | 49% | +1.26% | −1.31% |
| 基准（全体） | 277 | +0.50% | −0.06% | 49% | +0.53% | −2.13% |

你的规则的**中位数和胜率**确实最好（这不是尾部驱动的），但 N=15。`venue=HL & rank≤2` 是同一件事的更宽版本，样本翻 2.7 倍、结论一致 —— **它是更好的起点**，你的规则可以作为它内部的一个加仓档。

### 1.7 止损/止盈网格（RULE，去重后 N=15）

规则：入场=触发后第一根 1m 收盘；同时触发时止损优先（保守）。

| 止损 | 无止盈/持 1h | 无止盈/持 4h | TP1.5%/1h | TP3%/4h |
|---|---|---|---|---|
| 0.8% | −0.17% | +0.02% | −0.21% | −0.29% |
| 1.2% | +0.33% | **+1.23%** | +0.35% | +0.33% |
| 2.0% | +0.39% | +1.06% | +0.41% | +0.18% |
| 3.0% | +0.40% | **+1.53%** | +0.58% | +0.65% |

三个可直接用的结论：

- **0.8% 止损会把策略打死**。1h MAE 中位数就是 −1.0%，这个止损等于随机出局。下限是 **1.5–2.5%**。
- **固定止盈都是亏的**。收益分布是右尾（BCH 那笔 +11%、ONDO +2.9%、DOT +6.0% 贡献了全部均值），封顶就等于砍掉唯一的收益来源。用**时间止盈 + 追踪止损**替代固定 TP。
- **持 4h 明显优于持 1h**（+1.23% vs +0.33%）。这和 rank/pre-move 的机制一致：你买的是一个还没走完的动能。

### 1.8 频率与并发（决定仓位设计）

按近 19.1 小时的真实速率：

| 规则 | 次/天 | 4h 持仓峰值并发 |
|---|---|---|
| wr>100 & rank≤2（=全部 HL） | 42.7 | 15 |
| + OI ≥ 5M | 30.1 | 9 |
| + 每 symbol 4h 只做一次 | **22.6** | **8** |

约 23 笔/天、峰值同时持 8 个仓。这个数字直接决定：**单笔名义不能超过总权益的 5%**（8 × 5% = 40%），否则一次相关性冲击（全是山寨永续，BTC 一跌全跌）会同时打穿所有仓位。

---

## 二、Tracefold 侧的现状（决定接入点）

| 事实 | 证据 | 含义 |
|---|---|---|
| 1019 已放行 | #126 取消本地 Strategy 白名单；DB 里 12h 内 169 条 1019 item（≈330/天） | 帧已经在管道里，不用改 provider 侧 |
| **全部被 Gate 压掉** | 168 个 event，`admission` 全是 `suppressed_low_signal`，**0 条 verdict**，不进 Triage、不出卡、不进 outcome | 走 `/api/news/feed` 这条只读 HTTP 合同**拿不到**（也不该拿）；OI agent 需要自己的入口 |
| 数值被丢弃 | `_provider_metadata()` 不保留 `strategy.metrics` | 需要 Receiver 侧改动，或者正则回收（有损） |
| 架构禁令 | CLAUDE.md：单一业务能力 News V3；`docs/research/opentrade-deepagent-trading-agent.md` §1 已列出全部不可破坏条件 | **不能**在 `tracefold.news` 里加 Trading/Execution 子域 |

**三个接入方案：**

| 方案 | 做法 | 评价 |
|---|---|---|
| A. 自开一条 OpenNews WSS | OI agent 独立连 `wss://ai.6551.io/open/news_wss` | ⚠️ **先验证 provider 是否允许同账号多连接**。如果只允许一条，会把 News 的连接挤掉 —— 这是能把现网 News 打挂的风险，必须先测 |
| **B. Receiver 多发一份（推荐）** | Tracefold Receiver 在发 `news.raw` 的同时，把 `engine_type=market` 的帧（**带完整 metrics**）发一份到新 exchange/queue `oi.raw` | 单连接、复用 publisher confirms 和幂等、改动最小、边界干净（Receiver 只做转发，不解释） |
| C. 轮询 `strategy_hits` | 每分钟拉第 1 页 | 分钟级延迟（考虑到 30 分钟延迟容忍其实可接受）、但有 ~800 条上限、且断档过。**适合回补和研究，不适合当唯一实时源** |

推荐 **B 做实时 + C 做回补/研究语料**。

---

## 三、Agent 该怎么设计

### 3.1 Seam：独立 deployable，不是 Tracefold 的一个模块

```
OpenNews WSS ──> Tracefold Receiver ──┬──> news.raw   ──> News V3（不动）
                                      └──> oi.raw     ──> [OI Agent，独立进程/独立 schema]
                                                              │
                                          只读 HTTP ──────────┤ /api/news/feed（上下文否决用）
                                                              │
                                                              └──> OpenTrade HTTP adapter ──> CEX
```

OI Agent 拥有自己的 PostgreSQL schema（或独立库），自己的表，自己的 CLI。它**只**从 Tracefold 拿两样只读的东西：OI 帧、以及新闻上下文。它绝不写 Tracefold 的任何表。

### 3.2 六个组件（五个确定性 + 一个可选的模型）

**1) Ingestor / Normalizer —— 确定性**

- 落 `oi_signals(signal_id PK)`，`signal_id` = provider 的 `id`（帧里那个雪花号），天然幂等。
- 存全精度 metrics + venue + symbol + 收到时刻 + 帧原文哈希。
- 这一层不做任何判断。

**2) Rule Gate —— 确定性、纯函数、版本化 `oi.policy.v1`**

输入：一条 signal + 24h 历史 + 当前持仓账本。输出：`qualified` 或一个具名的 `skip_reason`（照抄 News 的"每条路径都报出规则名"的做法）。

建议的 v1（基于 §1 的证据，明确标注哪条有样本支持）：

| 条件 | 依据 | 样本强度 |
|---|---|---|
| `venue == hyperliquid` | +1.35% vs −0.26% 同窗对照 | N=57 vs 86，**中** |
| `rank_24h ≤ 2` | rank>5 是 −1.69% | N=281，**强** |
| `whale_oi_ratio > 100` | +2.26% vs +0.73% 同窗同所 | N=19 vs 22，**弱**（当加分项，不当硬门槛） |
| `whale_long_profit ≥ 95` | +1.42% vs −0.60% | N=219，**中** |
| `open_interest_value ≥ 5M` | 小 OI = 滑点与操纵风险 | 先验，非统计 |
| `pre_1h_move < 6%` | >6% 全是负的，MAE 恶化到 −3.4% | N=275，**强** |
| 同 symbol 4h 冷却 | 去重后 N 从 20 掉到 15，说明重复严重 | 机制性 |
| 并发预算 ≤ 8、总敞口 ≤ 40% | §1.8 | 机制性 |

注意 `pre_1h_move` 需要在触发瞬间拉一次 K 线（OpenTrade 有 `/market/klines` 和 `/public/metadata/ohlcv`）—— 这是 Gate 的唯一外部依赖，拉不到就 **fail closed**（不交易）。

**3) Context Veto —— 这里，且只有这里，才是 DSPy 的位置（见 §四）**

**4) Execution Kernel —— 确定性、fail-closed**

**5) Position Manager —— 确定性、定时循环**

**6) Evaluator —— 确定性**

复用 News 已有的 `reaction_v1` 口径（`p0` = 触发时刻或之前最后一根已收 5m K 线、`p1`/`p4` 同法取 +1h/+4h、缺 K 线记 `no_candle_within_gap` 绝不前向填充）。每笔记录：signal 全量、Gate 的 rule 名、veto verdict、实际成交价、退出原因、扣费后 PnL、以及同期 BTC 收益（超额才是真信号）。

### 3.3 执行状态机与幂等（**最难的部分**）

OpenTrade **没有 `clientOrderId`，没有幂等键**（已在 v1.0.4 SKILL.md 逐条核过）。而你的上游是 at-least-once 的 AMQP。这两件事撞在一起 = **重复下单**。

必须本地解决：

```
intent_id = sha256(signal_id | policy_version | side)      -- 唯一约束
planned → preflight_ok → submitting → submitted → open → closing → closed
                              ↓ 进程崩溃
                          ambiguous  ← 绝不自动重发
```

- `intent_id` 在表上有 UNIQUE 约束。同一条 signal 被重投递 N 次，只会有一个 intent。
- 进入 `submitting` 前先写库并提交事务。
- **崩溃恢复的唯一合法动作是对账，不是重试**：`GET /orders/open` + `GET /positions` + `GET /trades/history`，用 (symbol, side, 数量, 时间窗) 匹配。匹配上 → 转 `submitted`；确认没有 → 才允许重发；无法判定 → `ambiguous` + 告警人工，**永不自动重发**。
- 这和 News Deliverer "send 和 ack 之间崩溃就终结为 ambiguous 而不是重发" 是同一条规则，可以直接照抄那段逻辑的形状。

### 3.4 下单前置（每次都要做，缺一不可）

1. `GET /position/mode` 取 `hedged`（**必填参数**，值错会打错仓位）。按 (symbol, exchange) 缓存。
2. `GET /market/metadata` 取精度、最小下单量、tick size —— 数量算错会被拒单。
3. `GET /public/metadata/orderbook` 检查盘口深度：**你的名义 / 前 5 档深度 > 阈值就放弃**。这是 §1 完全没建模的成本，而 OI ≥ 5M 的门槛只是它的粗代理。
4. `GET /public/metadata/funding-rate` —— 持 4h 会跨 funding，山寨永续在动能行情里 funding 可以吃掉全部 +1.2%。**这是最容易被忽略的成本**。
5. `GET /account/summary` 核对本地账本 vs 交易所真实余额，不一致就 fail closed。

### 3.5 下单与止损

```
POST /open/trader/newsliquid/v1/orders
{"symbol":"DOGE/USDT:USDT","side":"buy","type":"market","quantity":<确定性算出>,
 "exchangeId":"hyperliquid","hedged":<来自 step 1>,
 "stopLossPrice":<entry*(1-0.02)>}
```

- 止损**随单附加**（`stopLossPrice`），这样它活在交易所侧 —— 你的进程挂了，止损还在。这是唯一不能只靠本地循环的东西。
- **不设 `takeProfitPrice`**（§1.7：固定止盈全是亏的）。
- Hyperliquid 的市价单文档要求带 `price` 字段，注意别漏。

### 3.6 时间止盈：venue 不支持，必须自己做

OpenTrade 的四种单型（`market`/`limit`/`stop_market`/`take_profit_market`）里**没有任何时间条件**，也没有追踪止损。所以：

- 一个 60s tick 的持仓循环（形状照抄 `price_loops.py` 的冷通道模式：独立的一槽 DB lane，不占执行热槽）。
- 每 tick：`GET /positions` 为准（**本地账本只是意图，交易所是事实**），然后判定
  - `now - opened_at ≥ 4h` → 时间止盈，`POST /positions/close`（market，`quantity > 0`，正确的 `hedged`；**不能用 0 表示全平**）
  - 追踪止损：最高价回撤 > X% → 平（这是替代固定 TP 的机制，用来保住 BCH 那种 +11%）
  - 仓位在 venue 侧已消失（被 SL 打掉）→ 本地终结为 `stopped`，记录
  - 数量不一致（部分成交/部分平仓）→ 以 venue 为准修正本地，记一条对账差异
- **关键陷阱**：4h 到期时如果 close 请求失败（限流、网络），不能就地重试到底 —— 重试要走同一套 intent/对账逻辑，否则会平两次（在单向模式下就是反手开了个空单）。

### 3.7 仓位与风控内核（fail-closed）

OpenTrade 的远端风控写得很清楚：限流 30 req/min、限价单偏离 ≤10%、单仓 ≤20% 余额、总仓 ≤80%、保留 5% 余额 —— 但同样明确写着**"market data 或 Redis 不可用时检查通过（fail-open，优先可用性）"**。

**所以远端风控只能算纵深防御，本地必须 fail-closed：**

| 项 | 建议 | 依据 |
|---|---|---|
| 单笔名义 | ≤ 5% 权益 | 峰值并发 8（§1.8） |
| 总敞口 | ≤ 40% 权益 | 同上；且这些标的高度相关 |
| 杠杆 | 1–2x | 4h MAE 中位 −1.6%、p10 −11.5% |
| 日内最大笔数 | ≤ 25 | 实测 22.6/天，超出说明 provider 侧配置变了 |
| 日内最大回撤 | 触发即全局停机 | 必须有 |
| 全局 kill switch | PostgreSQL 单例，每条消息读一次 | 照抄 News 的 control 表 |
| 限流 | 本地令牌桶 < 30/min | 上面每笔要 5 个预检 + 1 个下单 + 每分钟对账，**很容易撞上限流** |

注意最后一条：23 笔/天 × 6 个请求 + 8 个持仓 × 每分钟对账 = 对账本身就 480 req/h。**必须批量化对账（一次 `GET /positions` 拿全部），否则会被 30/min 打死。**

---

## 四、DSPy 到底放在哪

### 4.1 不放在触发

触发是 5 个浮点数 + 一次计数 + 一次 K 线查询。写成纯函数是 30 行代码，可回放、可单测、零成本、零延迟。**用 LLM 做这件事没有任何收益，只有成本和不可复现。**

### 4.2 放在这三个位置（按价值排序）

**a) 上下文否决（唯一有信息优势的地方）**

问题：这次 OI 上涨是**先行**还是**跟随**？如果同一时刻 Tracefold 的 News 管道刚推过这个币的上币公告、被黑、解锁、或者交易所公告，那么 OI 上涨只是新闻的回声，动能已经被定价，你在追高。

- 这是唯一 LLM 有真实信息优势的判断：它要读非结构化的新闻文本，跨 symbol 别名做匹配，判断"这条新闻是否解释了这次 OI 变化"。
- 数据现成：Tracefold 的 `news_events` 和 OI 帧在同一条时间轴上，只读 HTTP 就能拿。
- **延迟预算够**：30 分钟延迟只损失 0.43%（§1.5），一次结构化调用是秒级。

契约照抄 News 的成熟做法：内容寻址的 `ProgramArtifact`、code-owned kernel + 有界 LearnedStrategy、关闭 DSPy cache 和隐藏重试、一个 route deadline + 一次快速重试、fallback 模型、断路器、全量 trace（Program 身份、每 Predictor 的 request/output/usage/cost）。

**输出必须是枚举，不是数字：**

```python
class OIContextVerdict(Signature):
    """Judge whether recent news already explains this OI move."""
    veto: bool
    reason_code: Literal["no_context","news_explains_move","adverse_news",
                         "listing_pump","unlock","exchange_incident"]
    confidence: Literal["low","medium","high"]
```

**绝不让模型输出 symbol、数量、价格、杠杆、方向。** 那些永远来自确定性内核。模型能做的最坏的事只能是"该交易的不交易"。

**b) 持仓期间的重估**（第二阶段）：4h 持仓里出现反向新闻 → 提前平仓。同样是 veto 形状。

**c) 复盘归因**（最安全、长期最有价值）：每笔结束后写一条结构化 review（为什么赢/输、事后看该不该做），喂给和 News 同一套 `CandidateEvaluator` / 冻结证据 / 未来 holdout 的学习闭环。这里 LLM 不碰任何钱。

### 4.3 Shadow-first：模型的第一个月不许否决任何一笔

照抄 News 学习闭环踩过的坑（`docs/research/news-learning-loop-audit-2026-08-21.md`：机制通了但闭环 0 次运转，因为三道硬闸让历史不可学）：

1. **阶段 0**：Program 每条 signal 都跑、都写 verdict，但 `decide()` 完全忽略它。同时记录"如果听它的会怎样"。
2. 攒够 **N ≥ 100 条有 veto=true 的**、且都有 4h reaction 的样本后，做一次离线对比：被否决的那批的实际 4h 超额，是否显著低于未否决的。
3. **显著才启用**，且首次启用只在 canary 那一档。

### 4.4 明确不用 deepagents

`docs/research/deepagents-order-capability-best-practices.md` 已经第一手核过：subagent 默认 tool scoping 会泄漏下单权限、built-in filesystem 默认不安全、官方自己声明是 "trust the LLM" 模型、权限边界必须在 tool/backend 层实施。给一个能下单的 agent 用它，是把安全边界交给 prompt。**这个 OI agent 不需要 planning/子 agent/文件系统 —— 它需要一个纯函数和一台状态机。**

---

## 五、发布门槛（不达标不上真钱）

| 阶段 | 内容 | 通过条件 |
|---|---|---|
| **0. Shadow** | 全链路跑，只写 intent 和 evaluation，不下单 | **N ≥ 200 独立 episode**（4h/symbol 去重）；按 23/天约需 9–10 天。**超额（减 BTC）4h 均值 > 0 且置换检验 p < 0.05** |
| **1. Paper** | 同一个 Execution Kernel，换成本地 paper adapter（OpenTrade 无 testnet） | 加入实测的 taker fee、实测 funding、盘口滑点模型后**仍为正**。这是最可能杀死策略的一关：+1.2% 的 4h 均值要扛住 HL taker fee（双边）+ 一次 funding |
| **2. Canary** | 单笔固定 50 USDT、每天 ≤5 笔、只做最强档（HL & rank≤2 & wr>100 & wlp≥95 & OI≥5M & pre1h<6%）、人工 kill switch 常驻 | 连续 30 天、N ≥ 100、无一次幂等事故、无一次对账 ambiguous |
| **3. 放大** | 按 Kelly 分数的 1/4 缓慢放大 | canary 的超额 > 2× 总成本 |

**当前位置：阶段 0 之前。** 你手上是 15 个独立 episode 和 p=0.22。

---

## 六、下一步：三件立刻能做、且互不依赖的事

1. **把语料补厚（今天就能做，零风险）**：起一个每分钟拉 `strategy_hits?strategyId=1019&page=1` 的采集器，落自己的表。同时验证 §二 方案 A 的问题——**provider 是否允许同账号第二条 WSS**。这个必须在写任何代码之前测清楚，因为它能打挂现网 News。
2. **修 metrics 丢弃**（Tracefold 侧一个小 PR）：`_provider_metadata()` 保留 `strategy.metrics`（`engine_type=market` 的帧）。有了全精度数值，历史 OI item 才可用。
3. **写 Evaluator 和 Gate，先不写执行**：Gate 是纯函数、Evaluator 复用 `reaction_v1`。这两个加起来就是完整的 shadow 阶段，能在 10 天内给出"这个策略到底有没有边缘"的答案 —— 而且这 10 天里一分钱风险都没有。

**不要**先写下单。执行内核是这个系统里最难、最危险的部分，而它的价值完全取决于第 3 步的答案。

---

## 附：本次调研的局限

- 语料 754 条、断档严重、跨越了 provider 侧至少一次配置变化（08-21 起 whale_oi>100 的密度从 ~1/天 跳到 ~50/天）。**08-21 之前和之后可能不是同一个 Strategy。**
- 价格全部用 Binance USD-M 永续，即使信号来自 Hyperliquid。HL 的实际成交价、盘口和 funding 与 Binance 不同，而**恰恰所有正收益样本都是 HL 帧** —— 这是本次分析最大的单点缺陷，必须用 HL 自己的 K 线重做一遍。
- 无手续费、无 funding、无点差、无滑点、无部分成交、无强平模型。
- 多重检验：本次试了 rank、venue、whale_oi、whale_profit、OI 规模、pre-move 六个维度，没有做多重检验校正。**p=0.22 是校正前的值。**
- OpenTrade 服务端不开源。所有风控、成交、凭证行为都是官方声明，无法核实。
