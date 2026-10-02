# Execution：DEMO 执行器与场所对账

[手册](../README.md) · [Trading Analysis](trading.md) · [运维](../OPERATIONS.md#trading-operations) · [安全边界](../SECURITY.md)

账户执行由应用镜像中的 `tracefold executor` 进程负责。执行凭据只允许 Binance USD-M DEMO；分析、纸面结果与入场参考价使用 LIVE 行情。记录了请求不代表交易所接受，交易所接受也不代表成交。

## 输入与持久事实

账户行仅由 Executor 心跳创建；Analysis 发布 Signal 不创建或更新账户控制。缺失账户由发布资格报告为 `runtime_unavailable`。

Analysis 发布的 Signal v4 存入 `trading_entries`，按 `(requested_at_ns, entry_id)` 选择 pending 行。本地认证的 operator intent 按 seq 选择 disposition 为空的行。处置就地写入 entry 或 intent，不维护消费高水位，晚提交仍会被处理。交易所暂时不可用时保持待处置；重启时会给缺席期间过期的 Signal 记 `expired`。

同账户较新序号的 pause/halt/flatten 已接受时，晚到的 `resume_entries` 记为 `refused` / `superseded`，保持暂停或停止。相同状态和场所证据的订单对账不更新行，也不重写更新时间。

PostgreSQL 保存 `trading_inputs`、`trading_cases`、`trading_entries`、`trading_operator_intents`、`trading_orders`、`trading_fills` 和 `trading_accounts` 七张 Trading 表。Signal 请求、处置与入场计划归同一个 entry；`accept_entry` 仅更新 pending 行，并与订单预留在同一事务提交。条件更新落败不会预留或发送订单，同账户同 symbol 只允许一个 accepted/open/closing entry。成交事实保留原生证据，订单身份查明后只可一次补入账户、client ID 与归属时钟；不能重写原始数量、价格或费用。账户控制、对账快照和按 symbol 的成交游标归 accounts。订单行先于事务外的场所请求提交。每条腿的 client ID 由账户槽位、入场身份、腿与尝试序号的 SHA-256 确定，为 32 个 Binance 合法字符。运输超时、503 等暂态失败是 `unknown`，Adapter 不重试订单；Executor 先按记录的 ID 查询，不能盲目重发入场。

## 生命周期

执行器用 PostgreSQL advisory lock 保证账户槽位单例，默认每秒向平台 `runtime_processes` 写毫秒心跳；实例 UUID 对写入进行隔离，退出记录 stopped，超过 5 秒、非 running 或有 fault 的进程不能放行发布。Analysis 也拥有独立进程行，业务账户事实不承担存活证明。活跃 entry 每 5 秒通过签名 REST 核查，整个账户每 60 秒核查。未认领持仓或挂单会暂停新入场；进程停止不会平掉场所持仓。

准入并行读取签名仓位、普通 / Algo 单、权益、持仓模式以及 DEMO 盘口和合约规则。检查 Signal 时效、暂停/紧急停止、flatten、未认领暴露、Hedge Mode、同 symbol 仓位/挂单、最多 5 个活跃 Entry、杠杆容量、MARKET_LOT_SIZE、最小名义金额、LIVE 参考价漂移和点差。数量按权益风险比例与冻结止损距离计算，扣除已有仓位和待成交预留名义金额占用，按数量步长向下取整。

入场使用 DEMO MARKET `RESULT`。订单达到终态并确认持仓时钟后，先挂止损、再挂止盈 Algo 条件单，采用 `MARK_PRICE`，优先 `closePosition=true`。场所不支持时按实际仓位数量尝试 reduce-only；每条保护腿最多三次尝试，不确定结果先查询，保护失败转入 reduce-only 市价平仓。持有期满、操作员 flatten 和保护触发后的残余仓位也以真实场所仓位为准。

场所显示平仓后撤销剩余保护，再把 Entry 置为终态。成交由 DEMO `userTrades` 和持久 `fromId` 游标读取，订单 ID 关联到 Entry；手续费从原生成交读取。手续费资产若不是 USDT，则不把不同币种直接相减，也不报告已知净收益。终态后的 PnL 证据若未在限时内齐全，标为 `evidence_incomplete`，不会让 Entry 永远保持 open。

## 操作与恢复

`tracefold trading issue` 可提交暂停、恢复、紧急停止、手动入场或账户 flatten。命令入账仅表示请求被接受；平仓与撤单须以签名账户读回为准。Flatten 原子设置暂停并保留 flatten command；在确认零仓零挂单并清除 command 前，resume 拒绝为 `flatten_in_progress`。Flatten 会先撤普通挂单，再按真实仓位发 reduce-only 单，最后撤 Algo 单并验证零仓零挂单。

手动入场也经过账户、规则、时效与风险准入；Trading HTTP 和工作台只读。运行检查可使用以下入口，操作语法以 [CLI 契约](../generated/cli-help.md)为准：

```bash
tracefold executor
tracefold trading status
tracefold trading diagnose
tracefold trading signals --limit 20
tracefold trading fills --limit 20
tracefold trading commands --limit 20
```

诊断的数据库和可选本地 HTTP 样本各自保留采样时钟与错误；顺序采样不是同一时刻的原子证明。停写、备份与当前前向迁移统一见 [迁移手册](../MIGRATIONS.md)。没有真实账户回执时，测试通过不等于 DEMO 生命周期已验证。

## 实现与测试

- [执行器](../../tracefold/app/executor.py)、[纯决策与订单身份](../../tracefold/trading/executor/core.py)、[DEMO REST 适配](../../tracefold/integrations/trading/binance.py)
- [持久账本](../../tracefold/trading/storage/executor.py)、[操作契约](../../tracefold/trading/operator_control.py)、[执行阶段投影](../../tracefold/trading/stages.py)
- [恢复测试](../../tests/e2e/test_executor_recovery.py)、[真 PostgreSQL 账本测试](../../tests/e2e/test_executor_storage.py)

监控阶段由 Entry、订单、保护与仓位事实推导为 `pending / accepted / submission_unknown / rejected / expired / ordered / filled / protected / closed`，仅用于只读展示；场所证据仍由执行账本保存。
