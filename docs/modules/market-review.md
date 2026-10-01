# Market Review：标的目录与当前行情

[手册](../README.md) · [News](news.md) · [OI](oi.md) · [交易研究](trading.md) · [执行收益](execution.md)

本模块回答两个问题：**这是什么资产、这个合约现在多少钱**。它不回答账户是否成交，也不是高频订单簿采集器。

| 模块速览 | 说明 |
| :--- | :--- |
| **定位** | 信息产品 / 行情与派生计算 |
| **运行位置** | Workers · 目录与报价循环 |
| **输入 → 产物** | 类型化资产、公开报价 → 标的目录、最新报价快照 |

> [!IMPORTANT]
> 当前价格的滚动 24H 变化与账户收益有各自时间和证据。

[交易研究](trading.md) · [原生执行收益](execution.md)

<details>
<summary><strong>本页目录</strong></summary>

1. [两种数据产品](#section-两种数据产品)
2. [目录与两条价格链路](#section-目录与两条价格链路)
3. [类型化资产与来源选择](#section-类型化资产与来源选择)
4. [当前报价循环](#section-当前报价循环)
5. [发送时行情与历史适配](#section-固定期限-event-reaction)
6. [市场变化与账户收益](#section-四种收益不能混合)
7. [排障与验证](#section-排障与验证)
8. [源码责任地图](#section-源码责任地图)
9. [常见误解](#section-常见误解)

</details>

<a id="section-两种数据产品"></a>
## 01 · 两种数据产品

| 产品 | 身份 / 时间 | 更新语义 | 不能替代 |
| --- | --- | --- | --- |
| Instrument catalogue | 资产类别、来源、交易所、原生合约 | 周期更新目录，保存明确的可解析身份 | Trading 的已验证经济映射和下单权限 |
| Quote Snapshot | 当前来源报价、来源 / 接收时间、类型化资产 | 有界工作集的最新值；旧快照可被新值替换 | 新闻发生时的历史价、成交价 |


<a id="section-目录与两条价格链路"></a>
## 02 · 目录与两条价格链路

```mermaid
---
config:
  fontFamily: "system-ui, Noto Sans CJK SC, Microsoft YaHei, WenQuanYi Zen Hei, sans-serif"
  flowchart:
    curve: linear
    nodeSpacing: 28
    rankSpacing: 42
---
flowchart TB
    accTitle: 目录与当前报价
    accDescr: 类型化目录解析当前报价工作集；当前 REST 报价形成最新快照。
    Catalog["交易所目录与参考身份"] --> Instruments[("标的目录快照")]
    Instruments --> Resolve["按 symbol 与 market_type 解析"]
    Resolve --> QPlan["当前报价工作集<br/>按来源合并"]
    QPlan --> Public["公开 REST：当前报价"]
    Public --> Quotes[("最新 Quote Snapshot")]
    Quotes --> UI["只读页面与可选卡片补充"]

    classDef news fill:#ecfdf5,stroke:#0f766e,color:#134e4a,stroke-width:1.5px
    classDef research fill:#eef2ff,stroke:#6366f1,color:#312e81,stroke-width:1.5px
    classDef execution fill:#fff7ed,stroke:#c2410c,color:#7c2d12,stroke-width:1.5px
    classDef store fill:#f1f5f9,stroke:#64748b,color:#1e293b,stroke-width:1.5px
    classDef external fill:#f8fafc,stroke:#94a3b8,color:#334155,stroke-dasharray:4 3
class Catalog,Public external;
class Instruments,Quotes,UI store;
class Resolve,QPlan news;
```

*数据流 · 当前报价快照保留来源与时效；账户成交读取执行账本。*

这里没有新的 broker 队列、每个标的一条常驻任务，也没有因为一次行情失败而重新启动新闻语义分析。

<a id="section-类型化资产与来源选择"></a>
## 03 · 类型化资产与来源选择

资产匹配不是把一个裸 ticker 丢给任意交易所。`symbol` 必须结合 `market_type` / `instrument_class`，再由目录解析候选合约。股票 `SEI` 不应匹配同名币；找不到可用匹配就明确缺失，不能借用另一类资产的价格。

来源优先级和报价货币顺序由 [pricing.py](../../tracefold/news/market_review/pricing.py)的共同规则维护，目录 chip 与报价共用排序。参考性身份条目不自动变成可交易报价或执行路由。

解析成功仍只说明该只读行情产品有可用目标。Trading 对非标准合约的经济身份、每张单位和 mapping digest 有自己的契约；执行侧还必须检查实际配置的交易所连接。

<a id="section-当前报价循环"></a>
## 04 · 当前报价循环

`QuoteSnapshotLoop` 从 PostgreSQL 读有界目标，按来源合并请求，在事务外拉取，再以短事务批量写回。工作量按来源组而不是“Event 数 × 资产数”增长，同一资产被多条新闻提到不应重复请求行情。

| 当前代码预算 | 数值 | 含义 |
| --- | --- | --- |
| 报价主周期 | 20 秒 | 按开始时间安排，不重叠、不为错过的轮次无限追赶 |
| 当前报价阶段 deadline | 10 秒 | 必需来源组的有界外部读取 |
| 工作集 / 来源组上限 | 256 个目标 / 12 组 | 防止页面需求变成无界全市场采集 |
| 外部并发 | 4 | 该行情接缝的并发上限，不是模型并发 |
| Binance 日参考刷新 | 300 秒 | 必需当前报价写回之后的可选补充 |
| 报价新鲜窗口 | 45 秒 | 判断 fresh / stale；不是数据延时服务承诺 |
| 卡片读取报价等待 | 1.5 秒 | 超出后应允许缺少该补充，而非伪造价格 |

来源失败时保留上一条快照并明确 stale / unavailable，不把它变为零。报价字段保留 last / mark / mid 等价格种类及 freshness basis；只有接收时钟与有真实来源时钟的证据强度不同。

24h 涨跌与当前价格也要使用一致的参考含义：不能把很旧的日参考伪装成新鲜窗口，更不能把滚动 24h 变化称为“新闻后的收益”。

<a id="section-固定期限-event-reaction"></a>
## 05 · 发送时行情与历史适配

#764 P1 删除固定期限 Event Reaction、相关状态和接口字段。发送补充行情与 trailing quote 仍有生产消费者；公共成交和 K 线适配、`Candle`、`select_candle` 与 `return_bps` 保留给实际调用方。目录快照戳改存 `news_collectors.instrument_catalog.state.venues`，部分场所成功只更新对应场所时间。

<a id="section-四种收益不能混合"></a>
## 06 · 市场变化与账户收益

| 数字 | 正确解释 |
| --- | --- |
| 当前报价的 24h 变化 | 来源定义的滚动市场变化 |
| Trading 研究结果 | 冻结 Case / 候选根在规定研究协议下的价格路径 |
| 原生执行收益 | 真实成交、手续费、资金费率及其覆盖情况 |

新闻后的上涨不证明新闻造成上涨；通知命中率不证明策略盈利；纸面价格路径也不能补上缺失的手续费和成交证据。`review_storage.py` 只提供价格新鲜度读取，不生成历史 verdict cohort 评分。

<a id="section-排障与验证"></a>
## 07 · 排障与验证

先检查标的类别与匹配结果，再检查来源可用性、报价来源 / 接收 / 参考时钟。公共行情超时与语义失败是两个问题，不应共享一个“新闻不可用”结论。

验证入口：[报价循环](../../tests/news/test_news_v3_price_loops.py)、[价格存储与复盘](../../tests/integration/test_news_v3_price.py)、[规模接缝](../../tests/integration/test_news_v3_price_scale.py)。这些测试验证有界行为和计算，不等于任意交易所此刻可用或所有资产都有完整历史。

<a id="section-源码责任地图"></a>
## 08 · 源码责任地图

| 源码 | 职责 |
| --- | --- |
| [instruments.py](../../tracefold/news/market_review/instruments.py)、[instrument_storage.py](../../tracefold/news/market_review/instrument_storage.py) | 标的类别、目录快照、匹配与目录存储 |
| [pricing.py](../../tracefold/news/market_review/pricing.py) | 来源排序、价格类型、时效、K 线选择、期限、收益与资源上限；纯函数 |
| [loops.py](../../tracefold/news/market_review/loops.py) | `QuoteSnapshotLoop` 和 `EventReactionLoop` 的有界 I/O 编排 |
| [quote_storage.py](../../tracefold/news/market_review/quote_storage.py) | 目标查询、当前快照与到期 Reaction 存储 |
| [projections.py](../../tracefold/news/market_review/projections.py) | 显示字段、覆盖率与派生结果投影 |
| [review_storage.py](../../tracefold/news/market_review/review_storage.py) | 价格新鲜度状态读取 |
| [integrations/venues](../../tracefold/integrations/venues/) | Binance、Hyperliquid、OKX 等来源的公开行情与历史适配 |
| [Workers wiring](../../tracefold/app/workers/wiring/) | 将目录与报价能力装配进 Workers，分配独立资源接缝 |

<a id="section-常见误解"></a>
## 09 · 常见误解

<details>
<summary><strong>展开常见问题</strong></summary>

**新闻后价格上涨，是否证明系统交易获利？**

不能。执行收益需要真实成交、费用与资金费率证据。

**同名股票与代币能直接共享报价吗？**

不能仅凭 ticker。需要资产类别和来源合约；找不到可用目标时保留缺失。

</details>

---

[返回文档中心](../README.md) · [架构图谱](../ARCHITECTURE.md#atlas) · [返回顶部](#market-review标的目录与当前行情)
