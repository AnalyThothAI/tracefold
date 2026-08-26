# OI Agent MVP：最小闭环设计（不接 WSS / 不用 deepagents / 先零模型）

2026-08-22。信号侧的实测证据在 `oi-agent-design-2026-08-22.md`，本文只谈"怎么用最少的东西把链路跑通"。

## 三个决定

### 决定一：不接 WSS 是对的，而且代价约等于零

| | WSS | 轮询 `strategy_hits?page=1&limit=100` |
|---|---|---|
| 收到延迟 | 帧 `ts` 后 **1.3s**（实测 181 条中位） | 帧 `ts` 后 1.3s + 轮询间隔的一半 ≈ **+15s**（30s 间隔） |
| 帧本身的粒度 | **754/754 条 `ts` 秒位都是 `00`** —— provider 自己就按分钟出帧 | 同左 |
| 延迟的代价 | — | 延迟 30 分钟才损失 0.43%（4h 收益 +2.14%→+1.71%）。15 秒 ≈ **0.004%** |
| 断线 | 断了就永久丢积压（#126 踩过） | page 1 能回看 **8 小时**（近窗速率 12.5 帧/h）。挂半天都补得回来 |
| 对现网的风险 | 同账号第二条连接可能挤掉 News | **零**。纯 GET，不碰 Receiver、不碰 RabbitMQ、不碰 Tracefold 任何代码 |

**轮询在这个场景里不只是"够用"，是严格更好。** 唯一真实成本是每 30 秒一个 HTTP GET。

顺带：这也意味着 OI agent **和 Tracefold 完全解耦** —— 不需要改 `_provider_metadata()`、不需要新队列、不需要动 Receiver。它自己直接从 provider 拿全精度 metrics（`strategy.metrics` 在 `strategy_hits` 响应里是完整的）。

### 决定二：DSPy 还是 deepagents —— 都不是，v0 一个模型都不要

先说为什么不是 deepagents。#104 选 deepagents 是**对的，因为那个场景真的需要研究**：读 SEC 8-K 原文、反驳 Triage 的事实错误（MRNA 那个"流感疫苗"）、跨 issuer 找精确可交易 instrument、判断现金股 vs tokenized perp。那是开放式的、多步的、需要子 agent 的。

OI 场景**没有任何研究可做**。输入是五个浮点数：

```
oi_rise_pct  open_interest_value  price  whale_long_profit_rate  whale_oi_ratio
```

而 #104 自己列出的 deepagents 官方事实，每一条在这里都是纯成本：`tools=` 是追加不是白名单、默认 general-purpose subagent 会继承主 agent 的工具（包括下单工具）、`ToolRetryMiddleware` 默认对所有工具生效（会重发订单）、官方安全模型是 "trust the LLM"。**为了过滤五个浮点数，去买一整套用来约束开放式 agent 的安全 harness，方向是反的。**

再说 DSPy。它是**加模型时的正确选择**，理由不是偏好：

- 你已经在生产跑它（News Triage），`ProgramArtifact`、trace、replay、断路器、fallback 全都有现成的形状可抄。
- 它是一次结构化调用 + typed signature，不是 agent loop。
- **最关键的一条：DSPy Predictor 拿不到工具，它只能返回一个值。** deepagent 能自己调 `place_order`。在一个会动钱的系统里，"模型返回值、确定性代码执行动作" 和 "模型自己动手" 是两种完全不同的风险等级。

但 v0 **一个模型都不要**：

> Gate 是纯函数：五个浮点数 + 一次 24h 计数 + 一次 K 线查询。约 40 行，可单测、可回放、零延迟、零成本。加模型进去只会让"为什么这笔做了/没做"变得不可复现——而这恰恰是你在 v0 唯一想搞清楚的事。

模型在 v1 才进来，且只进 **veto** 这一个位置（"这次 OI 涨是不是只是某条新闻的回声"），且第一个月 shadow-only 不许否决任何一笔。

### 决定三：OI agent 是 #104 执行内核最便宜的试炼场

这是我最想让你看到的一点。

#104 里真正难、真正危险的部分不是 deepagents harness，是**订单账本**：durable intent、一个 intent 至多一次 provider write、timeout/crash 后稳定为 `AMBIGUOUS`、只 reconcile 不重发、部分成交、账户级单写。它的验收清单里 Order-tool tests 有 8 条，全是这个。

而 #104 的触发源**每 72 小时只有 3 个 episode**。你要用 3 个 episode 去把幂等和对账的坑踩完，得踩几个月。

OI 信号**每天 23 笔**（实测），小额、重复、机械。用它来把 OrderGateway + 账本打磨出来，一周的样本量比 #104 三个月还多。等 #104 真要上 `live_bounded` 的时候，它 `place_order` 工具背后的那个 gateway 已经是被 500 笔真实执行验证过的了。

**所以建议：一个 companion deployable，两个触发器，一套执行内核。**

```
                      ┌─ oi_poller (30s)  ── OI Gate（纯函数）──┐
                      │                                        ├─> OrderGateway ─> 账本 ─> OpenTrade
#104 news episode ────┴─ TradingDeepAgent（以后）──────────────┘        ↑
                                                                  一把单写锁 / 一个 mandate / 一个 kill switch
```

**这不是"顺便合并"，是硬约束**：两个进程用同一个交易所账户，就必然违反 #104 自己定的"一账户单写"和"原子 risk 预占"。要么共用一个账本和一把锁，要么用两个子账户。别无第三条路，而且这个坑要是等到 #104 上线才发现，就是真金白银的竞态。

---

## MVP：v0 闭环（零模型、零下单、约 700 行）

一个 Python 进程，一个 60 秒 tick，自己的 PostgreSQL schema。**跑完就能回答"这个策略到底有没有边缘"，全程零资金风险。**

### 四张表

```sql
oi_signals(signal_id PK,        -- = provider 帧的 id，天然幂等
           ts_ms, symbol, venue,
           oi_rise_pct, oi_value, price, whale_profit, whale_oi_ratio,
           raw jsonb, fetched_at_ms)

oi_intents(intent_id PK,        -- = sha256(signal_id|policy_version|side)  UNIQUE
           signal_id FK, policy_version,
           decision,            -- 'trade' | 'skip'
           rule,                -- 每条路径都报出规则名（抄 News decide() 的做法）
           side, notional, sl_price, planned_exit_ms,
           state,               -- planned→submitting→open→closing→closed | ambiguous
           created_at_ms)

oi_fills(intent_id FK, kind,    -- 'entry' | 'exit'
         price, qty, fee, reason, at_ms)   -- reason: time_stop|trail|stop_loss|manual

oi_evaluations(signal_id FK, p0, p1h, p4h, btc_p0, btc_p4h, bps_1h, bps_4h, metric_version)
```

### 六个循环步骤

```python
# tick 每 60s
1. poll()      GET /open/strategy_hits?strategyId=1019&limit=100&page=1
               → upsert oi_signals（按 signal_id，重复即 no-op）

2. gate()      对每条新 signal 跑纯函数 → 写 oi_intents（trade 或 skip+rule 名）

3. enter()     对 decision='trade' 的 intent：adapter.place(...)
               v0 的 adapter = PaperAdapter（用 klines 的下一根开盘价成交 + 手续费模型）

4. manage()    对 state='open' 的仓位：
               - now >= planned_exit_ms        → 平，reason='time_stop'
               - 回撤 > trail%（从最高价）      → 平，reason='trail'
               - 触及 sl_price                  → 平，reason='stop_loss'

5. evaluate()  对 4h 前的 signal 算 p0/p1h/p4h + BTC 同期（口径抄 news 的 reaction_v1）

6. reconcile() v0 是 no-op；换真 adapter 时这里才有内容
```

### Gate（v0 全文，就这么点）

```python
POLICY_VERSION = "oi.v1"

def gate(sig, ranks_24h, open_positions, prices) -> Decision:
    if sig.venue != "hyperliquid":          return skip("venue_not_hl")
    if ranks_24h[sig.symbol] > 2:           return skip("rank_exhausted")
    if sig.whale_profit < 95:               return skip("whale_profit_low")
    if sig.oi_value < 5_000_000:            return skip("oi_too_small")
    if pre_1h_move(prices, sig) is None:    return skip("no_price_fail_closed")
    if pre_1h_move(prices, sig) > 6:        return skip("chasing")
    if sig.symbol in open_positions:        return skip("already_open")
    if cooled_down(sig.symbol) is False:    return skip("symbol_cooldown_4h")
    if len(open_positions) >= 8:            return skip("concurrency_budget")
    return trade(side="long",
                 notional=equity * 0.05,
                 sl_price=sig.price * 0.98,
                 planned_exit_ms=sig.ts_ms + 4*3600_000,
                 rule="oi_hl_fresh_whale",
                 strong=sig.whale_oi_ratio > 100)   # 记录，v0 不加仓
```

注意 `whale_oi_ratio > 100` 在这里只是**打个标记**，不是硬门槛——因为它的独立样本只有 15 个（见另一份文档 §1.4/§1.6），而 `venue=hyperliquid` 是它的更宽版本，样本 2.7 倍、结论一致。跑满 200 个 episode 之后再回来看这个标记有没有区分度。

### 一个 CLI，四个子命令

```
oitrade poll        # 手工跑一次采集（也可以是 systemd timer）
oitrade run         # 常驻 tick
oitrade status      # 今日信号数 / 通过数 / 各 skip 规则计数 / 持仓 / 累计 PnL
oitrade review      # 逐笔：signal → rule → 入场 → 退出原因 → 净 PnL → BTC 超额
```

`status` 的 skip 规则计数就是 News `status.pipeline` 的 `dropped_by_rule` 那套东西——**每条被丢弃的路径都要有名字**，否则调不动策略。

### v0 明确不做

不做 RabbitMQ、不做 WSS、不做 Receiver 改动、不做 DSPy、不做 deepagents、不做真实下单、不做多交易所、不做加仓/减仓、不做杠杆调整、不做 HITL。

---

## 从 v0 到真钱：只换一个东西

```
v0   PaperAdapter        ──┐
v0.5 OpenTradeAdapter     ─┼─> 完全相同的 gate / manage / ledger / CLI
     （单笔 50 USDT）       │
v1   + DSPy veto（shadow）─┘
```

**v0.5 只改 adapter**，这是整个设计唯一重要的性质，也正是 #104 决定 3 说的"三档静态模式共用同一工具合同"。换 adapter 时新增的东西只有三样（这三样就是 #104 Order-tool tests 的全部内容）：

1. **写前先落 durable intent 并提交事务**，再发 provider 请求。
2. **一个 intent 至多一次 provider write attempt。** timeout / 崩溃 / 畸形响应 → 稳定为 `AMBIGUOUS`，冻结同 fingerprint 的 intent，**只 reconcile 不重发**。
3. **reconcile 才是真相**：`GET /positions` + `/orders/open` + `/trades/history`。provider 回 `success` 不等于成交，本地账本只是意图。

外加三个下单前置（OpenTrade 的 quirks，v0 可以先不管，v0.5 必须有）：`GET /position/mode` 拿 `hedged`（必填、值错会打错仓位）、`GET /market/metadata` 拿精度和最小量、`GET /public/metadata/funding-rate`（持 4h 会跨 funding，山寨永续在动能行情里能吃掉全部 +1.2%）。

止损用随单附加的 `stopLossPrice`，这样它活在交易所侧——进程挂了止损还在。**不设 `takeProfitPrice`**（实测固定止盈全是亏的，收益在右尾）。

---

## 工作量与顺序

| 步骤 | 内容 | 规模 | 产出 |
|---|---|---|---|
| 1 | poller + `oi_signals` | ~100 行 | 语料每天自动变厚，**今天就能开始跑** |
| 2 | gate + `oi_intents` + `status` | ~200 行 | 每天看得到"通过几条、被哪条规则挡掉" |
| 3 | PaperAdapter + manage 循环 | ~250 行 | **闭环完成**：进场→时间止盈→止损→出场 |
| 4 | evaluator + `review` | ~150 行 | 净 PnL 和 BTC 超额 |
| 5 | 跑 10 天，攒 200 个独立 episode | 0 行 | **决定要不要有第 6 步** |
| 6 | OpenTradeAdapter + 账本硬化 | ~400 行 | canary |
| 7 | DSPy veto（shadow） | ~200 行 | 只写 verdict，不否决 |

第 1–4 步加起来 ~700 行、零风险，能在几天内跑通。第 5 步是唯一诚实的门槛：**现在手上是 15 个独立 episode、p=0.223，还不知道这策略有没有边缘。** 第 6 步之前不该有真钱。

## 落到 issue 上

建议开两个，都引用 #104：

- **`Spec: OI signal agent（轮询 + 确定性 Gate + paper 闭环）`** — 上面 v0 的全部内容，明确 hard cut：不接 WSS、不改 News、不用 deepagents、v0 无模型。
- **`Spec: 共享 OrderGateway 与订单账本`** — 从 #104 里把执行内核**拆出来独立交付**，由 OI agent 先用起来，#104 的 `place_order` 工具后续接同一个 gateway。这样 #104 那个庞大的 spec 就少了最危险的一块，也不会出现两个进程抢一个账户。
