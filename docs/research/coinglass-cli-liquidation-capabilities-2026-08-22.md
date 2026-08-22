# `coinglass-cli` 清算能力与 Tracefold 集成边界

核查日期：2026-08-22（Asia/Taipei）

核查对象：`AnalyThothAI/coinglass-cli` 的 `main`，固定 commit
[`dc8f9d253a8dc1fded6fabcef93c96feeaa4b826`](https://github.com/AnalyThothAI/coinglass-cli/tree/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826)。

证据范围：该固定 commit 的源码、README/current-state/runbook，以及 CoinGlass 官方
API 文档、价格页和服务条款。未把第三方博客、搜索摘要或交易观点当作事实来源。
本文中的“官方 API”专指 `open-api-v4.coinglass.com` 的文档化合同；“网页路径”专指
`coinglass-cli` 逆向并适配的 CoinGlass 网页 HTTP/WSS 协议，两者不能混为一谈。

## 结论

1. **它确实能获取潜在清算 Level，不只是历史清算数量。** `liquidation-levels`
   返回按价格划分的潜在清算水平和同行情 OHLC；`liquidation-heatmap` / 
   `perpair-heatmap` 返回时间 × 价格的潜在清算强度；`liquidation-stream` 才是已经发生的
   逐笔清算事件。仓库自己也明确区分“已发生事件”和“估算暴露”。
   [语义区分](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/README.md#L160-L167)
2. **它当前不是 CoinGlass 官方 API client。** 四个清算命令都走无 Key 网页协议；HTTP
   响应需要跟随前端 bundle 的版本/解密规则，WSS 是未文档化的公开 `liq` 频道。当前版本
   根本不接受 CoinGlass API Key，也没有实现官方 REST/WSS adapter。
   [README 合同](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/README.md#L18-L35)
3. **可用于研究、影子运行和原型，不宜直接成为 News/OI 热路径依赖。** HTTP 路径会因
   CoinGlass 前端 bundle、签名或响应版本漂移而不可用；WSS 没有 replay、完整性、顺序或
   SLA。项目自己的 current-state 也要求下游经服务边界消费，而不是假设网页抓取永远可用。
   [当前风险](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/docs/current-state.md#L59-L74)
4. **可以复用 Tracefold 价格链路的调度思想，不能直接塞进同一批量 Quote 请求。** 可复用
   “最近 Events + watchlist → 精确合约解析 → 去重工作集 → cold lane → 成功替换、失败留旧值
   → 读取时判 freshness”；但 `liquidation-levels` 是一次一个交易对、返回数千行的大快照，
   不能达到现有 Quote 的 `O(source groups)` 批量成本。应有独立 adapter、较慢 cadence、独立
   latest-only read model 和独立 freshness 语义。
5. **News 卡和 OI 卡可以共用同一份清算快照，但必须是 display-only。** 代币现价仍应来自
   Tracefold 现有 Quote Snapshot；CoinGlass levels 随附的最后一根 OHLC 不能冒充当前报价。
   清算数据应显示为“潜在清算区”，过期/失败即省略，不进入 Gate、Triage、OI 算术、policy、
   duplicate、排序或推送资格。
6. **生产优先路线应是 CoinGlass 官方 API，而不是把网页逆向实现内嵌进 Workers。** 官方
   V4 原生提供一个适合卡片的 wide `liquidation/max-pain`、pair/coin liquidation map、三种
   heatmap、清算订单 REST/WSS 和历史聚合。Max Pain/map/heatmap 需要 Professional，实际
   清算订单 REST/WSS 明确需要 Standard 或以上。官方合同仍不保证数据绝对完整，但 schema、
   鉴权、计划和错误/额度至少是文档化的。

## 当前 OI 卡：线上复现、根因与直接修复

本次不是只做静态代码阅读。先按仓库 live-data 规则运行 `uv run tracefold config`，确认
`config_path=/Users/massis/.tracefold/config.yaml`，再以只读 `tracefold_serve` 角色核对生产库；
没有读取或输出 token、密码或 DSN secret。

2026-08-22 20:46（Asia/Taipei）的 DOGE OI Event（`event_id` 前缀 `165db241`）完整事实是：

```text
OI change       8.64%
OI value        73,010,000 USD
Whale/OI ratio  210.97%
Whale long PnL  80.60%
4h rank         1
decision        push
delivery        sent
```

持久化 verdict 与真正送出的 Feishu card 分别是：

```text
headline_zh / delivered header
▲ DOGE 持仓异动 8.64%

why_zh / delivered first body line
持仓 7301 万 · 鲸鱼占比 211.0% · 鲸鱼多头盈利 80.6% · 4h 内第 1 次
```

所以四项数据**没有解析丢失、没有 verdict 丢失，也没有投递丢失**；当前代码本来就把它们放在
body，而 Feishu 通知预览主要暴露 card header，才形成“标题只有 DOGE 持仓异动 8.64%”的用户
体验。`_headline()` 只生成短句，`_why()` 才拼出四项完整指标。
[`oi_signals.py`](https://github.com/AnalyThothAI/tracefold/blob/dfc984e301a8485124e8f4f474e9f5746c012cfb/src/tracefold/news/oi_signals.py#L175-L191)
通用 renderer 又明确用 `headline_zh` 做 header、`why_zh` 做第一条 body。
[`delivery.py`](https://github.com/AnalyThothAI/tracefold/blob/dfc984e301a8485124e8f4f474e9f5746c012cfb/src/tracefold/news/delivery.py#L231-L277)

### 为什么没有代币价格

同一 Event 的资产证据是：

```text
event.grounded_assets = []
verdict primary asset = DOGE / perp
news_event_assets rows = []
```

同一生产库在随后快照中已经有 `binance.perp / DOGEUSDT|last` 报价；卡片没有行情行不是
Binance/Hyperliquid 没有 DOGE，也不是 Quote loop 没工作，而是 Deliverer 根本没有请求 DOGE。

根因有两层：

1. `card_assets()` 只保留“verdict primary ∩ Gate grounded”，OI 的确定性 primary DOGE 因
   `grounded_assets=[]` 被删掉；Deliverer 又只对 `card_assets()` 返回值查询报价。
   [`card_assets`](https://github.com/AnalyThothAI/tracefold/blob/dfc984e301a8485124e8f4f474e9f5746c012cfb/src/tracefold/news/delivery.py#L180-L195)
   [`Deliverer quote read`](https://github.com/AnalyThothAI/tracefold/blob/dfc984e301a8485124e8f4f474e9f5746c012cfb/src/tracefold/news/consumers.py#L1728-L1748)
2. Quote working set 只从 `news_event_assets` 读取最近 grounded assets；一个只出现在
   `news_oi_signals` 的新币即使 renderer 开始请求，也不保证已被后台 Quote loop 预热。
   [`quote_target_symbols`](https://github.com/AnalyThothAI/tracefold/blob/dfc984e301a8485124e8f4f474e9f5746c012cfb/src/tracefold/news/price_repository.py#L179-L202)

这也是 #137 合并时明确记录的已知限制，不是本次上游偶发退化：当时 PR 已写明 OI 卡没有
ticker chip 和 quote line，因为 Gate 不为遥测落地资产。
[#137 implementation PR](https://github.com/AnalyThothAI/tracefold/pull/140)

### 建议先独立落地的 OI card v2

这项修复不应等待 CoinGlass：

1. 让确定性 OI `headline_zh` 本身包含全部指标，同时压在现有 60 字符合同内，例如：

   ```text
   ▲ DOGE 持仓异动8.64%｜持仓7301万｜鲸鱼占比211.0%｜鲸鱼多头盈利80.6%｜4h内第1次
   ```

   该示例 60 字符以内；移入 header 后 `why_zh` 留空，避免打开卡片时重复同一句。
2. 在 delivery/market-context seam 定义一个 `reader_assets(event_id)` interface：普通 News 仍然
   使用 grounded-primary 交集；只有同时满足
   `admission=telemetry_deterministic`、`program_version=news_oi_signal_v1` 且存在
   `news_oi_signals` 行时，才把该行的单一 symbol 作为 code-owned reader asset。不要把 DOGE
   回写成 Gate grounded asset，也不要放宽普通模型 verdict 的信任规则。
3. Quote target planner 把最近 live `news_oi_signals.symbol` 与现有 `news_event_assets.symbol` 做
   bounded union，再走同一个 exact-symbol-first instrument resolver、source 去重和 Quote loop。
   不要把 OI symbol 塞进 `news_event_assets`；那张表目前表达 grounded Event asset，并驱动 Event
   Reaction/Review，偷偷改义会把 OI 遥测混进另一套样本。
4. Deliverer 仍只读 fresh Quote；unavailable/stale 时省略行情行，绝不延迟、重试或取消 OI 推送。

预期卡片为：完整 OI header + `利多/影响/币种/来源/时间` facts line + 现有 `行情 DOGE ...`
line。这个改动只修读者合同和展示资产选择，不改变 OI 阈值、排名、`decide()`、storyline、去重
或 ReviewDesk 分母。

## 1. `coinglass-cli` 的准确清算表面

### 1.1 四个数据命令

| CLI 命令 | 上游与确切路由 | 请求维度 | 返回语义 | 当前覆盖边界 |
|---|---|---|---|---|
| `liquidation-heatmap` | 24h: `GET https://capi.coinglass.com/api/index/aggregate/liqHeatMap`; 48h: `GET https://fapi.coinglass.com/api/index/v4/aggregate/liqHeatMap` | 基础币 `symbol`; `24h` 或 `48h` | 跨交易所聚合、时间 × 价格格点的潜在清算强度，并带 OHLC | 代码接受任意 symbol；仓库只把 BTC 记为无 Key 已验证边界 |
| `perpair-heatmap` | 24h: `GET https://capi.coinglass.com/api/index/v2/liqHeatMap`; 48h: `GET https://fapi.coinglass.com/api/index/v6/liqHeatMap` | `Exchange_BASEQUOTE`; `24h` 或 `48h` | 单交易对时间 × 价格格点的潜在清算强度 | 代码硬限制 base asset 为 BTC |
| `liquidation-levels` | `GET https://capi.coinglass.com/api/liquidationLevels/v2` | `Exchange_BASEQUOTE`; `range=3d|7d|14d|30d`; `limit=4000` | 逐行潜在清算水平 + OHLC 快照；不是已成交订单 | 2026-08-22 仓库验证 BTC/ETH/SOL/DOGE；一次请求一个交易对 |
| `liquidation-stream` | `wss://wss.coinglass.com/ws`, channel `liq` | 本地 `symbol` / `exchange` / 最低 USD 过滤；上游订阅全量公开频道 | 已经发生的逐笔清算订单，NDJSON | 公开频道当时暴露什么就有什么；无服务端 coverage/replay 合同 |

HTTP 路由、固定参数和本地覆盖约束直接定义在
[`route_contracts.py`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/route_contracts.py#L9-L38)
与
[`ROUTE_DEFINITIONS`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/route_contracts.py#L65-L110)；
range/period 校验和默认 `Binance` / `USDT` 合约解析见
[`route_contracts.py`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/route_contracts.py#L140-L206)。
CLI 参数合同见
[`cli.py`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/cli.py#L89-L155)。

### 1.2 它没有哪些清算命令

固定 commit 中**没有**以下官方 V4 能力的 CLI 命令或 client 方法：

- pair/coin liquidation history；
- REST liquidation order history；
- 官方 authenticated `liquidation_orders` WSS；
- pair/coin liquidation map；
- liquidation max pain；
- liquidation coin list / exchange list。

换言之，`coinglass-cli` 的 `liquidation-levels` 是网页端 `liquidationLevels/v2`
适配器，不等于它已经封装官方 `/api/futures/liquidation/map` 或
`/aggregated-map`。完整公开方法只包括三个 HTTP fetcher 和五类衍生品历史 fetcher；没有隐藏的
官方清算 client。
[`CoinglassClient` 清算方法](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/client.py#L82-L90)

## 2. 数据到底表示什么

### 2.1 Potential Level / Map / Heatmap

`liquidation-levels` 的规范化输出是：

```json
{
  "symbol": "Binance_DOGEUSDT",
  "range": "3d",
  "source": {"provider": "coinglass", "endpoint": "liquidation_levels_v2"},
  "levels": [
    {
      "begin_date": 1787166660,
      "level": 3,
      "level2": "h1",
      "price": 0.0717504,
      "side": 1,
      "size": 1194468.47,
      "x": 13
    }
  ],
  "prices": [
    {"timestamp": 1787405820, "open": 0.09083, "high": 0.0909,
     "low": 0.09077, "close": 0.0909, "volume": 550528.78332}
  ]
}
```

稳定字段来自 dataclass
[`LiquidationLevel` / `NormalizedLiquidationLevels`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/models.py#L40-L67)，
解析器只是类型转换，不增加单位或业务解释。
[`normalize_liquidation_levels`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/parser.py#L60-L92)

因此目前可以可靠说：

- `price` 是模型估算水平的价格轴；
- `begin_date` 和 `prices[].timestamp` 是上游时间轴；
- `prices` 是快照附带的 OHLCV 上下文；
- `levels` 是潜在暴露，不是已经成交的清算订单。

目前**不能只凭 CLI 合同可靠说**：

- `size` 的精确单位一定是 USD；
- `side=1/2` 对 levels 的多/空含义与逐笔 stream 完全相同；
- `level=3`、`level2=h1` 的稳定业务含义；
- 上游是否对多交易所/合约做过币本位、线性合约和杠杆档位的可比归一化；
- 模型值可解释为“到该价必然发生的清算金额”。

源码刻意保留整数/string 原值而没有给这些字段语义命名。卡片若直接渲染
“上方将清算 $X”会越过现有证据。正式上线前必须用 CoinGlass 正式字段说明或有权限的
样本对照锁定 side、单位、模型版本和聚合算法。

官方 API 对“估算”语义更明确：pair heatmap 是根据市场数据和杠杆水平计算的清算 level，
返回 `y_axis`、`liquidation_leverage_data[x,y,value]` 和 `price_candlesticks`；pair map 返回
`liquidation price`、`Liquidation Level` 和 `Leverage Ratio`。范围分别为
`12h|24h|3d|7d|30d|90d|180d|1y` 和 `1d|7d|30d|180d|365d`。
[官方 Pair Heatmap Model1](https://docs.coinglass.com/reference/liquidation-heatmap)
[官方 Pair Liquidation Map](https://docs.coinglass.com/reference/liquidation-map)

### 2.2 Executed liquidation events

`liquidation-stream` 接收 gzip 帧，要求 `channel == "liq"` 且 `data` 是 list；每一行规范化为：

- `exchange`, `symbol`, `instrument`, `quote_currency`；
- `price`, `quantity`, `value_usd`；
- 原始 side，以及 `liquidated_position_side` / `forced_order_side`；
- provider event/create time、本机 receive time、两段 delay；
- fingerprint、可选 trade id、raw payload；
- transport/auth/contract/completeness/reconnect metadata。

这里源码明确把 stream 的 `side=1` 映射为 long 被清算/强制 sell，`side=2` 映射为
short 被清算/强制 buy；这个映射只应引用到 event stream，不能自动外推到 levels。
[`normalize_liquidation_event`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/liquidation_stream.py#L308-L356)

事件身份优先用 `(exchangeName, originalSymbol, tradeId)`，没有 trade id 时对
exchange/instrument/time/side/price/qty/USD amount 哈希。去重只在当前进程最多保留 10,000
个 fingerprint，不是全局幂等键或永久账本。
[`liquidation_event_fingerprint`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/liquidation_stream.py#L283-L305)

## 3. Polling、streaming、失败与陈旧语义

### 3.1 HTTP snapshots

HTTP 命令是同步、调用一次取一次；仓库没有常驻 scheduler 或 HTTP server。默认路径使用
`BestEffortAcquisition`，行为是：

- 同 cache key 跨进程 singleflight；
- 成功后仅在 1 秒 coalescing 窗口内作为 fresh 复用；窗口后下一次调用仍会访问上游；
- live 失败时可返回最长 1 小时的旧 payload，明确标为
  `freshness=stale, degraded=true`；
- 没有可用旧值时 `ok=false, freshness=unavailable`；
- 连续 3 次失败打开该 key 的 circuit 5 分钟；
- 失败分类为 `protocol_drift`, `upstream_timeout`, `no_coverage`, `rate_limited`,
  `upstream_error`, `circuit_open`；
- HTTP 外部请求跨进程默认最少间隔 1 秒；单 HTTP request timeout 为 20 秒；
- 网络、上游 code、协议漂移共享最多 5 次总 attempt 的有界重试。

默认 TTL/breaker/coalesce 数值见
[`acquisition.py`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/acquisition.py#L26-L40)
和
[`BestEffortAcquisition.acquire`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/acquisition.py#L90-L216)；
默认一秒 pacer 见
[`request_pacing.py`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/request_pacing.py#L16-L26)，
重试预算见
[`retry_policy.py`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/retry_policy.py#L14-L24)。

协议 cache 是另一件事：每 route id 保留一个当前 bundle hash 与确切
`response version -> seed plan`，默认有效 6 小时，并另存 corpus 供离线 replay。
[`protocol_cache.py`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/protocol_cache.py#L18-L68)
若响应出现未知版本，runtime 最多强制刷新一次网页 bundles，仍不能解密便返回
`protocol_drift`，不会伪装成空数据。

### 3.2 WebSocket events

stream 的连接与订阅是：

```json
{"method":"subscribe","params":[{"channel":"liq","type":"-1"}]}
```

默认 20 秒静默心跳、最多 5 次连续重连，退避 1/2/4/8/16 秒（代码封顶 30 秒）。一次
成功收到可解析消息会重置连续重连计数。
[`LiquidationStream`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/liquidation_stream.py#L18-L23)
[`连接、heartbeat 与 reconnect`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/liquidation_stream.py#L128-L226)

关键损失语义：没有 cursor、ack 或 replay；断开期间缺口不能补。事件明确携带
`contract=undocumented_public_web_channel` 与 `completeness=unknown`。WSS 不使用 HTTP
cache、singleflight、circuit 或 pacer。
[runbook](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/docs/runbooks/live-access.md#L123-L168)

## 4. 鉴权、计划、额度与许可

### 4.1 当前无 Key 网页路径

`coinglass-cli` 不读取、不接受也不注入 `CG-API-KEY`。HTTP 伪装浏览器，加载网页和
JS bundle，动态提取请求签名/响应解密规则；levels 当前请求本身不签名，但响应仍可能加密。
bundle probe 会加载 route page、route bundles，并用实时 endpoint 验证精确版本规则。
[`bundle_probe.py`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/bundle_probe.py#L81-L135)
[`probe 验证`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/bundle_probe.py#L138-L247)

这条路径没有 CoinGlass 发布的 plan、rate limit 或 SLA 合同；本地 1 req/s 只是项目自限速，
不是上游授权额度。更重要的是，CoinGlass 服务条款称网站/API 数据、图表和指标属于其或
licensors，仅授予有限、非独占、不可转让许可，并禁止未经许可的 bulk scraping、复制/销售/
再分发，以及未经授权的商业使用。把网页逆向路径用于生产卡片前，需要获得明确的数据使用
和展示授权，不能把“无需 Key”解释为“可自由商用”。
[CoinGlass Terms of Service](https://www.coinglass.com/terms)

固定 commit 根目录也没有 `LICENSE` 文件，`pyproject.toml` 没有 license metadata。
因此即使该仓库属于同一 GitHub 组织，也不应假定它已经授予一般复制、再发布或派生授权；
内部复用权限应由仓库 owner 明确落成许可。
[仓库根目录](https://github.com/AnalyThothAI/coinglass-cli/tree/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826)
[pyproject.toml](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/pyproject.toml#L1-L23)

### 4.2 CoinGlass 官方 API

官方 V4 要求每次 REST 请求带 `CG-API-KEY`；缺失/无效返回 401。响应头
`API-KEY-MAX-LIMIT` 与 `API-KEY-USE-LIMIT` 分别报告每分钟上限和当前使用量；官方错误表还
定义了 400/401/404/405/408/422/429/500。
[Authentication](https://docs.coinglass.com/reference/authentication)
[Errors & Rate Limits](https://docs.coinglass.com/reference/responses-error-codes)

2026-08-22 官方价格页显示：Hobbyist 30 req/min（personal）、Startup 80 req/min
（personal）、Standard 300 req/min（commercial）、Professional 1200 req/min
（commercial）；价格与 entitlement 会变，应在采购时重新核对。
[CoinGlass API Pricing](https://www.coinglass.com/pricing)

与本需求直接相关的已明确门槛：

- REST liquidation order：Standard / Professional / Enterprise，最近 7 天、每次最多
  200 条、全计划 1 秒 cache/update；
  [官方 Liquidation Order REST](https://docs.coinglass.com/reference/liquidation-order)
- `liquidation_orders` WSS：Standard Edition 或以上；消息字段为
  `base_asset, exchange, price, side, symbol, time, volume_usd`；
  [官方 Liquidation Order WSS](https://docs.coinglass.com/reference/ws-liquidation-order)
- pair/aggregated Heatmap Model1/2/3、pair/aggregated map 和 liquidation max-pain：
  Professional / Enterprise，官方标为 real-time；
  [Pair Map](https://docs.coinglass.com/reference/liquidation-map)
  [Coin Map](https://docs.coinglass.com/reference/liquidation-aggregated-map)
  [Max Pain](https://docs.coinglass.com/reference/liquidation-max-pain)
  [Pair Model3](https://docs.coinglass.com/reference/liquidation-heatmap-model3)
  [Coin Model3](https://docs.coinglass.com/reference/liquidation-aggregated-heatmap-model3)
- pair/coin liquidation history 与 rolling coin/exchange summaries 在全部付费 tiers 可用，
  但 Hobbyist 的历史粒度至少 4h、Startup 至少 30m、Standard 以上不受该粒度限制；仍应逐
  endpoint 在采购时重新核对。

## 5. 官方清算 API 的能力矩阵

CoinGlass 官方 endpoint overview 列出下列 liquidation surface；它比当前 CLI 大得多。
[Endpoint Overview](https://docs.coinglass.com/reference/endpoint-overview)

| 官方 endpoint | 数据性质 | 时间/参数 | 典型 schema |
|---|---|---|---|
| `/api/futures/liquidation/order` | 已发生逐笔订单 | 最近 7 天；exchange、coin、min amount、start/end；最多 200 | exchange, pair, base, price, USD value, side, ms time |
| WSS `liquidation_orders` | 实时已发生订单 | 全频道订阅 | base, exchange, pair, price, side, ms time, USD volume |
| `/api/futures/liquidation/history` | 单交易对多/空清算聚合历史 | `1m` 至 `1w`; limit ≤1000; start/end | time, long USD, short USD |
| `/api/futures/liquidation/aggregated-history` | 单币跨交易所聚合历史 | 以该 endpoint 文档为准 | 时间桶长/短清算 |
| `/api/futures/liquidation/coin-list` | 某交易所全币清算快照 | 1h/4h/12h/24h 字段 | total/long/short USD per coin |
| `/api/futures/liquidation/exchange-list` | 某币跨交易所清算快照 | range `1h|4h|12h|24h`; 10 秒更新 | total/long/short USD per exchange |
| `/api/futures/liquidation/heatmap/model1..3` | pair 潜在清算热力图 | exchange, pair, `12h..1y` | price axis, `[x,y,leverage]`, OHLCV |
| `/api/futures/liquidation/aggregated-heatmap/model1..3` | coin 聚合潜在清算热力图 | coin, `12h..1y` | 同上，跨交易所聚合 |
| `/api/futures/liquidation/map` | pair 潜在清算价位图 | exchange, pair, `1d..365d` | price -> liquidation level + leverage ratio |
| `/api/futures/liquidation/aggregated-map` | coin 聚合潜在清算价位图 | coin, `1d..365d` | price -> aggregated liquidation level |
| `/api/futures/liquidation/max-pain` | 主流币 wide 潜在压力区快照 | 一个 `range=12h|24h|48h|3d|7d|14d|30d` 请求 | 每币 current price、long/short max-pain level 与 price |

补充官方 schema/窗口证据：
[Pair Liquidation History](https://docs.coinglass.com/reference/liquidation-history)
[Liquidation Coin List](https://docs.coinglass.com/reference/liquidation-coin-list)
[Liquidation Exchange List](https://docs.coinglass.com/reference/liquidation-exchange-list)
[Coin Liquidation Heatmap](https://docs.coinglass.com/reference/liquidation-aggregate-heatmap)
[Coin Liquidation Map](https://docs.coinglass.com/reference/liquidation-aggregated-map)
[Liquidation Max Pain](https://docs.coinglass.com/reference/liquidation-max-pain)

这说明需求可以分成两条完全不同的产品数据：

- **卡片背景**：map/levels/heatmap，回答“当前价格上下有哪些估算清算密集区”；
- **事件与研究**：order stream/order history/long-short aggregates，回答“刚才实际清算了
  什么、窗口内哪一侧压力更大”。

不要用 levels 代替已发生事件，也不要用订单历史当作未来 level。

### 官方 API 中最接近卡片需求的是 Max Pain

`GET /api/futures/liquidation/max-pain?range=24h` 一次返回多个“主流币”，每币包含：

```json
{
  "symbol": "BTC",
  "price": 110625.1,
  "long_max_pain_liq_level": 75677278.26,
  "long_max_pain_liq_price": 113046.71,
  "short_max_pain_liq_level": 44617473.19,
  "short_max_pain_liq_price": 109748.37
}
```

它比逐币取数千条网页 levels 更符合 Tracefold 的 `O(source groups)` Quote 思路：一次 wide
response 即可覆盖响应中所有主流币，并天然给出当前价格上下两个代表性价位。边界是：

- 官方只说 “major cryptocurrencies”，没有给出完整币种 coverage 保证；
- `liq_level` 的展示页称为 level/intensity，没有明确标出货币单位，不能擅自加 `$`；
- 其 `price` 是 CoinGlass 模型响应中的价格，只应用于校准/距离计算；卡片现价仍以 Tracefold
  Quote 的 fresh 合同为准；
- long/short 字段虽然有命名，示例中价格相对当前价的方向仍必须用 live fixture 校准后才渲染
  多/空文字；第一版可安全地只显示“上方/下方潜在区”。

### 官方 `side` 合同存在冲突，必须按 adapter 分开

官方 REST order 页面把 `side=1` 注释为 Buy、`side=2` 为 Sell，即强制订单方向；新的官方 WSS
endpoint 页面却把 `side=1` 注释为 Long liquidation、`side=2` 为 Short liquidation，即被清算
仓位方向。更早的 WSS getting-started 页面又使用 camelCase channel `liquidationOrders` 和
camelCase 字段，而新的 endpoint 页面使用 `liquidation_orders` 与 snake_case。
[REST order side](https://docs.coinglass.com/reference/liquidation-order)
[当前 WSS side/channel](https://docs.coinglass.com/reference/ws-liquidation-order)
[WSS getting started 的旧合同](https://docs.coinglass.com/reference/ws-getting-started)

所以 REST、官方 WSS、公开网页 WSS 必须各有自己的 versioned adapter 和合同 fixture；不能共享
一个裸 `side` normalizer。levels 的 numeric side 更不能复用任何 event side mapping。

## 6. Symbol、venue 与 coverage

### 当前 CLI

- `liquidation-levels` 裸 symbol 会精确组装为
  `exchange + "_" + SYMBOL + quote`，默认 `Binance` + `USDT`。一次一个合约。
- 本地 `exchanges` 清单为 Binance、OKX、Bybit、Bitget、BingX、dYdX、CoinEx、Huobi；
  这是参数/默认清单，不是服务端 coverage snapshot，也不会随上游自动更新。
- per-pair heatmap 代码主动拒绝所有非 BTC base；aggregate heatmap 文档仅把 BTC 标为已验证。
- levels 在 2026-08-22 已验证 BTC、ETH、SOL、DOGE；这不证明任意小币、任意 venue/quote
  都可用。
- WSS 默认接收公开频道暴露的全部 symbol/exchange，filter 全在本地；上游不推的市场无法补。

对应源码：
[`resolve_symbol` 与 supported exchanges](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/route_contracts.py#L26-L42)
[`BTC-only per-pair guard`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/route_contracts.py#L164-L183)
[`2026-08-22 live boundary`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/README.md#L109-L120)

### 官方 API

官方提供 `/api/futures/supported-coins` 与 `/supported-exchange-pairs`，应成为 adapter 的
coverage source，而不是在 Tracefold 复制静态清单。官方介绍声称覆盖 2,000+ derivatives；
价格页当前列出的 futures venues 包括 Binance、OKX、Bybit、CME、Bitget、Deribit、BitMEX、
Bitfinex、Gate、Kraken、KuCoin、dYdX、CoinEx、BingX、Coinbase、Crypto.com、Hyperliquid 等；
精确可查询交易对仍以 supported endpoints 的实时返回为准。
[CoinGlass V4 Introduction](https://docs.coinglass.com/reference/getting-started-with-your-api)
[Supported Coins](https://docs.coinglass.com/reference/coins)
[官方 pricing/venue list](https://www.coinglass.com/pricing)

## 7. Python/module、CLI/subprocess 与存储表面

### 可直接 import 的实现

不用 subprocess 也能调用：

- `CoinglassClient.fetch_aggregate_heatmap(...)`；
- `CoinglassClient.fetch_perpair_heatmap(...)`；
- `CoinglassClient.fetch_liquidation_levels(...)`；
- `LiquidationSnapshotRequest` + `LiquidationSnapshotFetcher.fetch()`；
- `LiquidationStream.events(...)` iterator。

不过 package 根的 `__all__` 只导出 `__version__`，没有声明稳定的 library API；版本仍是
`0.1.0`。这些类是可 import 的源码表面，不应当成 semantic-versioned SDK 承诺。
[`__init__.py`](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/__init__.py#L1-L5)

CLI 是同步进程：snapshot/history 输出单个 JSON envelope，stream 输出 NDJSON。指定 `-o`
时 snapshot JSON 和 stream NDJSON 都以 write mode 重新创建文件；CLI 不提供 append-only log
rotation、数据库、HTTP service、message broker 或 consumer ack。README 也要求永久追加由上层
采集服务负责。
[输出实现](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/src/coinglass_cli/cli.py#L221-L325)
[README 文件边界](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/README.md#L218-L226)

默认只在本机保存：

```text
~/.cache/coinglass-cli/acquisition_cache.json
~/.cache/coinglass-cli/protocol_cache.json
~/.cache/coinglass-cli/request_pacing/
```

acquisition cache 把完整规范化 payload 放进一个 JSON 文件；protocol cache 保存 route
bundle/protocol truth。都采用 file lock + 原子替换，但这不是多节点共享存储或可查询 read model。

### 依赖与资源成本

package 要求 Python ≥3.12，并无条件安装 `curl_cffi`, `playwright`, `pycryptodome`, `quickjs`。
levels/heatmap 的 runtime 不启动 Playwright browser，但整个发行包仍携带浏览器历史通道相关依赖；
OI/history 命令需要真实 browser runtime。
[pyproject.toml](https://github.com/AnalyThothAI/coinglass-cli/blob/dc8f9d253a8dc1fded6fabcef93c96feeaa4b826/pyproject.toml#L1-L15)

无 Key levels 的工作量不是一次轻量 Quote：

- 每个 symbol 独立 HTTP 请求，无法批量；
- 每次 levels 请求声明 `limit=4000`，同时返回 level rows 与最多数千 OHLC rows；
- protocol cache 冷或未知 response version 时还要抓 route page、bundle JS，并做在线 validation；
- pacer 只把 liquidation endpoint 请求限制为每秒一次；page/bundle 获取也有网络成本；
- acquisition cache 会再次把完整 payload 写入单个本地 JSON。

本次在固定 commit、空临时 cache 上实际执行：

```bash
COINGLASS_CLI_ACQUISITION_CACHE=/tmp/.../acquisition.json \
COINGLASS_CLI_PROTOCOL_CACHE=/tmp/.../protocol.json \
COINGLASS_CLI_REQUEST_PACING_DIR=/tmp/.../pacing \
uv run coinglass-cli liquidation-levels --symbol DOGE --range 3d
```

2026-08-22 观察到 live/fresh：2,326 条 levels、4,000 条 prices，规范化 JSON 约
1,176,688 bytes，acquisition cache 约 1.3 MiB；该次响应版本为 `66`，从两个 JS bundle
探测出精确 seed plan。此处只是一次 dated capacity sample，不是上游固定 payload 大小或
latency SLA。它足以否定“按现有 20 秒 Quote cadence 对几十/几百 symbol 直接全量轮询”的方案。

## 8. 对 Tracefold News / OI 卡的具体方案

### 8.1 必须分开的三种状态

```text
现价 Quote Snapshot      -> 现在价格/24h 变化，20 s batch，已有链路
清算 Level Snapshot      -> 价格上下的潜在清算区，慢 cadence，新增 read model
实际 Liquidation Events  -> 已发生强平流，event stream/窗口聚合，可选后续研究面
```

不应把三者塞进一张 `news_quote_snapshots` row：它们的 provider、单位、刷新周期、失败语义和
生命周期不同。可以共用工作集规划与 instrument resolver，但 read model 必须分开。

Tracefold 当前 Quote plane 已经把目标按 instrument/source 去重、每 source 一次 batch、失败留旧
row，并仅在读取时把 ≤60 秒判为 fresh；卡片价格是 display-only。
[`Price Review architecture`](https://github.com/AnalyThothAI/tracefold/blob/dfc984e301a8485124e8f4f474e9f5746c012cfb/docs/ARCHITECTURE.md#L309-L347)
[`Quote code-owned budgets`](https://github.com/AnalyThothAI/tracefold/blob/dfc984e301a8485124e8f4f474e9f5746c012cfb/src/tracefold/news/pricing.py#L43-L72)

### 8.2 建议的新增边界

新增一个 provider-neutral interface，例如：

```python
class LiquidationSnapshotProvider(Protocol):
    async def fetch(self, instrument, *, model, range) -> ProviderLiquidationSnapshot: ...
```

其后用独立 loop 和 latest-only read model（示意名
`news_liquidation_snapshots`），稳定 identity 至少包括：

```text
(provider, venue, venue_symbol, model_version, range)
```

row 至少保存：

- 规范化 zones/levels（bounded top-N，不把 4,000 OHLC + 数千 level rows整包写进 card model）；
- 原始单位/side/model 语义版本；
- provider snapshot time 与本机 received time；
- source/contract/authenticated/completeness；
- payload hash；
- freshness/degraded/error class；
- 仅为审计保留的 raw snapshot locator（若许可允许），而不是在 Feed JSON 重复原始大 payload。

调度可复用 Quote 的：recent Events + watchlist、exact-symbol-first、instrument universe、cold DB
lane、targets 去重、turn 不重叠、失败不覆盖成功旧值。不能复用 Quote 的“一 source 一个 batch”
假设：CoinGlass map/levels 是 per coin/per pair。必须增加 per-turn target cap、请求 pacer、整体
deadline、较慢 cadence 和“新 symbol 立即读、旧 symbol 到期再读”。

对于无 Key CLI shadow adapter，建议起步边界是：

- 仅最近 live Events + watchlist，去重后小工作集；
- `liquidation-levels`, `3d`, exact venue/pair；
- 新 symbol 立即 fetch，之后至少分钟级而不是 20 秒级；具体 cadence 由连续 canary 的
  payload change rate、provider timestamp、带宽和 drift 率决定；
- concurrency=1 并保留其 1 req/s pacer；
- `stale` 只可展示为过期状态，卡片默认省略而非渲染旧值；
- 不在 News/OI hot transaction 中同步等待 CoinGlass。

拿到 Professional key 后，卡片 MVP 应先评估 wide `max-pain` adapter；它与现有 Quote 同样可
做到每 source 一次 wide request。只有需要完整 price ladder/heatmap 时再按 active working set
逐币请求 aggregated map/heatmap。不要让 official 与 web-scrape adapter 静默 fallback 成同一个
source；provider/contract 必须显式可见，shadow 对账后再切换。

### 8.3 卡片渲染合同

OI 卡片所缺的代币价格不需要 CoinGlass 来解决：继续调用现有 `/api/news/quotes`/Quote Snapshot
并遵守 fresh-only。News 卡与 OI 卡可共用同一渲染 helper：

```text
行情  $0.09090 · 24h +2.1%        # 现有 Quote，fresh 才显示
清算区  上方 $0.096… · 下方 $0.084…  # 新 snapshot，fresh + schema validated 才显示
```

在 `size` 单位和 `side` 定义未正式锁定前，不显示“多头/空头将清算 $X”；可以先只显示
距离当前 fresh quote 最近的上下价格区，且用“潜在清算区/估算”明示模型性质。聚类规则必须
code-owned/versioned，例如：

- 先按相邻 price buckets 聚类；
- 分开当前价格上方/下方；
- 每侧只选一至两个强度最大的 cluster；
- cluster 必须达到绝对/分位阈值，否则省略；
- 当前 Quote 不 fresh 时不计算/显示“距现价百分比”；
- 保存 raw zone → displayed cluster 的确定性 trace。

同一份 snapshot 可以同时服务 News 和 OI 卡，不应为每张卡重新请求 CoinGlass。卡片只是读
latest read model；任何 provider timeout、protocol drift 或 stale 都不能延迟、取消或改变 News/OI
的推送决策。

### 8.4 实际清算 events 是否也要接

若目标只是卡片上下清算区，先不接 WSS；levels/map 已回答该问题。若目标还包括“刚刚出现
清算级联/过去 5m 多空清算不平衡”，才新增实际 event plane：

- append-only raw events，而不是 latest-only snapshot；
- connection/gap ledger；
- database idempotency/fingerprint；
- 5s/30s/5m 等窗口聚合；
- source completeness 进入每个窗口；
- 断线 gap 不能当作零清算。

生产上优先官方 `liquidation_orders` WSS + REST 7d recovery/对账；无 Key WSS 可作为 shadow
源，不应单独声称全量。官方 REST 每次最多 200 条，因此 recovery 仍需明确分页/时间切片、
去重以及“窗口过密导致截断”的检测。

## 9. 推荐决策与分阶段验证

### 推荐选择

| 目标 | 推荐数据源 | 是否进入第一阶段 |
|---|---|---|
| OI 卡补现价 | Tracefold 现有 Quote Snapshot | 是；与 CoinGlass 集成解耦 |
| News/OI 卡显示潜在清算区 | CoinGlass 官方 wide max-pain；完整 ladder 再用 aggregated map/heatmap | 是，先 shadow；有正式 entitlement 后 reader-only 上线 |
| 快速验证 Level 算法/UI | `coinglass-cli liquidation-levels` no-key shadow | 是，但不作为生产唯一依赖 |
| 实际清算流与窗口压力 | 官方 `liquidation_orders` WSS + REST recovery | 后续独立 event plane |
| 直接把 `coinglass-cli` subprocess 放进 News consumer | 不推荐 | 否 |

### Promotion gates

1. **合同**：CoinGlass 明确商业展示/缓存许可，仓库 owner 明确代码许可。
2. **schema**：锁定 level/map 的 side、size unit、model version、时间戳和合约类型。
3. **coverage**：用 supported coins/pairs 对 Tracefold instrument universe 做逐 venue 对账；未知不是
   `unlisted`。
4. **freshness**：至少一周 shadow 记录 provider timestamp、payload hash change rate、延迟、大小、
   429、5xx、drift、stale age。
5. **成本**：证明 bounded working set 下请求/min、GB/day、DB row size 与 cold lane deadline 可控。
6. **failure**：断网、401/429/500、invalid schema、protocol drift、partial payload 时旧值只老化，
   不清零，不影响 News/OI 推送。
7. **rendering**：price 与 liquidation 各自 fresh 才显示；缺失时卡片结构仍完整；明确写“潜在/估算”。
8. **隔离**：断言 liquidation snapshot 永远不进入 Gate、Program、policy、duplicate、ranking、OI
   deterministic verdict 或推送 eligibility。

## 最终判断

`coinglass-cli` 已经证明“Level 数据拿得到”，而且当前比一个只含历史清算统计的 CLI 更完整：
它同时有潜在 levels、aggregate/per-pair heatmap 与实际 event stream。但它证明的是一个做了
缓存、漂移探测和失败标注的**网页协议采集器**，不是一个可直接嵌入 Tracefold 热路径的官方
SDK。

最合适的工程落点是：价格继续走现有 Quote；新增独立的 liquidation latest-snapshot plane，复用
工作集、合约解析、cold lane 和 stale-not-blank 机制；先用 `coinglass-cli` 做 shadow/算法验证，
生产优先切到官方 wide max-pain，只有需要完整梯度时才用 map/heatmap；News/OI 卡共用该 read
model，fresh-only、display-only。
