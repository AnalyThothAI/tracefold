# Execution：DEMO 执行器与场所对账

[手册](../README.md) · [Trading Analysis](trading.md) · [运维](../OPERATIONS.md#trading-operations) · [安全边界](../SECURITY.md) · [术语](../../CONTEXT.md)

Executor 根据持久请求、账户控制和交易所证据执行订单。它使用应用镜像中的独立 `tracefold executor` 进程，执行凭据只允许 Binance USD-M **DEMO**。Analysis、纸面结果和入场参考价使用 **LIVE** 行情。

**请求入账、执行器接受、交易所接受和实际成交分别需要证据。** 进程停止也不表示交易所仓位已经平掉。

<details>
<summary><strong>本页目录</strong></summary>

1. [运行位置与责任](#运行位置与责任) · [输入与持久事实](#输入与持久事实)
2. [生命周期](#生命周期)
3. [状态与证据](#状态与证据)
4. [操作与恢复](#操作与恢复)
5. [实现与测试](#实现与测试)

</details>

## 运行位置与责任

| 责任主体 | 负责什么 |
| --- | --- |
| Analysis | 发布 Signal v4，提供方向、参考价格、双侧几何和时效 |
| 本地操作员命令 | 提交暂停、恢复、紧急停止、手动入场或账户 flatten 意图 |
| Executor | 独占账户槽位，执行准入、订单提交、保护、成交归属与对账 |
| Trading 账本 | 保存请求、处置、订单、成交、账户控制与场所证据 |
| HTTP / 工作台 | 读取执行投影，不拥有下单或账户控制权限 |

手动入场同样经过账户、规则、时效与风险准入。操作员提交命令不会绕过执行器。

## 输入与持久事实

| 输入 | 持久产物 |
| --- | --- |
| Signal v4 | `trading_entries` 中的请求、处置和入场计划 |
| 本地认证的 operator intent | `trading_operator_intents` 中的命令及处置 |
| 订单请求及场所响应 | `trading_orders` 中的稳定身份、状态与原生证据 |
| DEMO `userTrades` | `trading_fills` 中的原生成交事实 |
| 签名账户查询 | `trading_accounts` 中的控制、对账快照和按 symbol 的成交游标 |

Trading 共用七张表：`trading_inputs`、`trading_cases`、`trading_entries`、`trading_operator_intents`、`trading_orders`、`trading_fills` 和 `trading_accounts`。研究输入、请求、订单与成交各有用途，不能互相替代。

账户行只由 Executor 心跳创建。Analysis 发布 Signal 不创建或更新账户控制；执行器不可用时，发布资格报告为 `runtime_unavailable`。

## 生命周期

### 1. 取得账户槽位，建立进程报告

Executor 使用 PostgreSQL advisory lock 保证账户槽位单例。每次启动有独立实例 UUID，防止旧实例继续更新进程行。

默认每秒向平台 `runtime_processes` 写毫秒心跳，退出时记录 `stopped`。超过 5 秒、非 `running` 或有 fault 的执行进程不能放行发布。Analysis 也有独立进程行；账户快照不承担进程存活证明。

### 2. 读取尚未处置的请求

Signal 按 `(requested_at_ns, entry_id)` 选择 pending 行。operator intent 按 seq 选择 disposition 为空的行。处置就地写入 entry 或 intent，不使用消费高水位，因此晚提交的请求仍会被处理。

交易所暂时不可用时，请求保持待处置。重启之后，缺席期间已经过期的 Signal 会被记为 `expired`。

### 3. 用账户和行情事实决定是否接受

Executor 并行读取签名仓位、普通订单、Algo 订单、权益、持仓模式，以及 DEMO 盘口和合约规则。准入检查分为以下几组：

| 检查 | 目的 |
| --- | --- |
| Signal 时效、暂停、紧急停止、flatten、未认领敞口 | 确认账户当前允许新入场 |
| Hedge Mode、同 symbol 仓位或挂单、最多 5 个活跃 Entry | 限制持仓模式、重复敞口与并发 |
| 杠杆容量、`MARKET_LOT_SIZE`、最小名义金额 | 确认数量符合账户和合约约束 |
| LIVE 参考价漂移和 DEMO 盘口点差 | 确认当前可执行价格没有偏离研究约束 |

数量按权益风险比例与冻结止损距离计算。已有仓位和待成交订单预留的名义金额都占用容量。最终数量按步长向下取整。

### 4. 先提交接受与订单预留，再访问交易所

Signal 的 `accept_entry` 路径只更新 pending 行。手动入场则锁定尚未处置的 intent，创建 accepted entry，并记录命令处置。

接受入场与订单预留在同一事务提交。条件更新落败时，不预留也不发送订单；同账户、同 symbol 只允许一个 accepted/open/closing entry。

每条腿的 client ID 由账户槽位、入场身份、腿和尝试序号的 SHA-256 确定，结果是 32 个 Binance 合法字符。稳定身份使重启后能够查询同一请求的实际结果。

订单行提交后，Executor 才在事务外发送场所请求。入场使用 DEMO MARKET `RESULT`。传输超时、503 等暂态失败记为 `unknown`；适配器不自动重试订单，Executor 先按记录的 ID 查询，不能盲目重发入场。

### 5. 核实持仓，再建立止损和止盈

订单达到终态并确认持仓时钟后，Executor 先挂止损、再挂止盈 Algo 条件单。保护采用 `MARK_PRICE`，优先使用 `closePosition=true`。

场所不支持该方式时，执行器按实际仓位数量尝试 reduce-only。每条保护腿最多尝试三次；结果不确定时先查询。保护失败后，流程转入 reduce-only 市价平仓。

持有期满、操作员 flatten、保护触发后的残余仓位，都以真实场所仓位为准处理，不按本地请求推断已经平仓。

### 6. 对账、归属成交并结束入场

活跃 entry 每 5 秒通过签名 REST 核查，整个账户每 60 秒核查。未认领持仓或挂单会暂停新入场。相同状态和场所证据的订单对账不更新行，也不重写更新时间。

场所显示平仓后，执行器撤销剩余保护，再把 Entry 置为终态。成交来自 DEMO `userTrades`，通过持久 `fromId` 游标续读，并用订单 ID 关联 Entry。

原生成交数量、价格和费用不能改写。订单身份查明后，只可一次补入账户、client ID 和归属时钟。手续费资产不是 USDT 时，不把不同币种直接相减，也不报告已知净收益。

终态后的 PnL 证据未在限时内齐全时，结果标为 `evidence_incomplete`。证据不完整不会让 Entry 永远保持 open。

## 状态与证据

| 记录或状态 | 含义 |
| --- | --- |
| pending 请求 | 请求已经入账，执行器尚未作出处置 |
| accepted 入场 | 本地准入与订单预留已提交；不证明交易所接受 |
| `unknown` 订单 | 请求结果尚未查明；不能据此重发或宣称没有成交 |
| 订单和保护记录 | 保存场所响应及查询证据 |
| 原生成交与账户对账 | 证明实际数量、价格、费用以及场所当前敞口 |
| `runtime_processes` | 证明进程报告的新鲜度，不证明账户已经平仓 |

只读监控根据 Entry、订单、保护与仓位事实，推导 `pending / accepted / submission_unknown / rejected / expired / ordered / filled / protected / closed` 阶段。这是展示词表，不是另一套执行账本，也不是交易所原始状态的替代来源。

## 操作与恢复

`tracefold trading issue` 可提交暂停、恢复、紧急停止、手动入场或账户 flatten。命令入账表示请求已保存；平仓与撤单须以签名账户读回为准。

同账户较新序号的 pause/halt/flatten 已被接受时，晚到的 `resume_entries` 记为 `refused / superseded`，账户保持暂停或停止。这避免旧的恢复命令撤销较新的控制。

Flatten 的处理顺序是：

1. 原子设置暂停并保留 flatten command。
2. 撤销普通挂单。
3. 按真实仓位发送 reduce-only 平仓单。
4. 撤销 Algo 单。
5. 签名核查零仓位、零挂单，再清除 command。

完成核查并清除 command 前，resume 拒绝为 `flatten_in_progress`。

| 情况 | 当前处理与恢复边界 |
| --- | --- |
| 准入读取暂时失败 | 保留待处置请求；再次读取时重新检查时效和账户事实 |
| 入场提交结果未知 | 先查询已记录 client ID，不盲目重发入场 |
| 保护结果未知或失败 | 未知结果先查询；有限尝试失败后转入安全平仓 |
| 出现未认领敞口 | 暂停新入场，以全账户对账确定真实仓位和挂单 |
| 进程重启 | 从持久请求、订单身份、成交游标和账户事实继续处理 |
| PnL 证据不完整 | 明确保存 `evidence_incomplete`，不编造已知净收益 |

运行检查入口如下；操作语法以 [CLI 契约](../generated/cli-help.md)为准：

```bash
tracefold executor
tracefold trading status
tracefold trading diagnose
tracefold trading signals --limit 20
tracefold trading fills --limit 20
tracefold trading commands --limit 20
```

诊断中的数据库和可选本地 HTTP 样本各有采样时钟与错误。顺序采样不构成同一时刻的原子证明。停写、备份与前向迁移步骤见 [迁移手册](../MIGRATIONS.md)。

## 实现与测试

| 实现 | 职责 |
| --- | --- |
| [executor.py](../../tracefold/app/executor.py) | 账户槽位、持久请求消费、场所访问与对账编排 |
| [executor/core.py](../../tracefold/trading/executor/core.py) | 纯准入、生命周期决策与订单身份 |
| [DEMO REST 适配](../../tracefold/integrations/trading/binance.py) | 签名传输、场所请求和错误分类 |
| [storage/executor.py](../../tracefold/trading/storage/executor.py) | 持久请求、订单、成交与账户账本 |
| [operator_control.py](../../tracefold/trading/operator_control.py) | 操作契约与控制决策 |
| [stages.py](../../tracefold/trading/stages.py) | 只读执行阶段投影 |

[恢复测试](../../tests/e2e/test_executor_recovery.py)覆盖未知订单、重启与账户 flatten；[真 PostgreSQL 账本测试](../../tests/e2e/test_executor_storage.py)覆盖持久事实与条件更新。没有真实账户回执时，测试通过不等于 DEMO 生命周期已经验证。
