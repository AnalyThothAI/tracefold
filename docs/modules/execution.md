# Execution：DEMO 执行器与场所对账

[手册](../README.md) · [Trading Analysis](trading.md) · [运维](../OPERATIONS.md#trading-operations) · [安全边界](../SECURITY.md)

账户执行由应用镜像中的 `tracefold executor` 进程负责。执行凭据只允许 Binance USD-M DEMO；分析、纸面结果与入场参考价使用 LIVE 行情。记录了请求不代表交易所接受，交易所接受也不代表成交。

## 输入与持久事实

Analysis 发布的 Signal v4 和本地认证的 operator intent 都以序号读入。执行器为每个输入写入一条 `accepted`、`refused` 或 `expired` disposition，再推进游标。重启时会给缺席期间过期的 Signal 记 `expired`，不静默跳过。

PostgreSQL 保存 `trading_signals`、`trading_operator_intents`、`trading_dispositions`、`trading_plans`、`trading_orders`、`trading_fills`、`trading_fill_attributions` 和对账游标。成交事实只追加；若成交先于订单身份查询返回，之后追加归因行，不修改原生成交。订单行先于外部请求提交。每条腿的 client ID 由账户槽位、入场身份、腿与尝试序号的 SHA-256 确定，为 32 个 Binance 合法字符。订单超时或 503 后只按该 ID 查询；不能用相同 ID 盲目重发入场。

## 生命周期

执行器用 PostgreSQL advisory lock 保证账户槽位单例，默认每秒心跳。活跃计划每 5 秒通过签名 REST 核查，整个账户每 60 秒核查。未认领持仓或挂单会暂停新入场；进程停止不会平掉场所持仓。

准入检查包括暂停与紧急停止、已有仓位或普通 / Algo 挂单、并发及杠杆容量、MARKET_LOT_SIZE、最小名义金额、LIVE 参考价漂移和点差。入场使用 DEMO MARKET `RESULT`。终态成交后才一次性挂止损和止盈 Algo 条件单，优先 `closePosition=true`。场所不支持时按实际仓位数量尝试 reduce-only；保护失败时 reduce-only 市价平仓。持有期满、操作员 flatten 和触发保护后的残余仓位也以场所仓位为准执行。

场所显示平仓后撤销剩余保护，再把 Plan 置为终态。成交由 DEMO `userTrades` 和持久 `fromId` 游标读取，订单 ID 关联到 Plan；手续费从原生成交读取。手续费资产若不是 USDT，则不把不同币种直接相减，也不报告已知净收益。终态后的 PnL 证据若未在限时内齐全，标为 `evidence_incomplete`，不会让 Plan 永远保持 open。

## 操作与恢复

`tracefold trading issue` 可提交暂停、恢复、紧急停止、手动入场或账户 flatten。命令入账仅表示请求被接受；平仓与撤单须以签名账户读回为准。Flatten 会先撤普通挂单，再按真实仓位发 reduce-only 单，最后撤 Algo 单并验证零仓零挂单。

上线前应先确认旧执行进程已停、DEMO 仓位和挂单均为零，并保留 `trading_*` 备份。迁移 A 是前向硬切，删除旧执行表，不回填或双读。运行检查使用 `make status`、`make logs`、`tracefold trading status` 及签名场所回执。没有真实账户回执时，测试通过不等于 DEMO 生命周期已验证。

## 实现与测试

- [执行器](../../tracefold/app/executor.py)、[纯决策与订单身份](../../tracefold/trading/executor/core.py)、[DEMO REST 适配](../../tracefold/integrations/trading/binance.py)
- [持久账本](../../tracefold/trading/storage/executor.py)、[硬切迁移](../../tracefold/platform/postgres/alembic/versions/20260929_0417_trading_execution_hard_cut.py)
- [恢复测试](../../tests/e2e/test_executor_recovery.py)、[真 PostgreSQL 账本测试](../../tests/e2e/test_executor_storage.py)
