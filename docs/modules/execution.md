# Execution：DEMO 执行器与场所对账

[手册](../README.md) · [Trading Analysis](trading.md) · [运维](../OPERATIONS.md#trading-operations) · [安全边界](../SECURITY.md)

账户执行由应用镜像中的 `tracefold executor` 进程负责。执行凭据只允许 Binance USD-M DEMO；分析、纸面结果与入场参考价使用 LIVE 行情。记录了请求不代表交易所接受，交易所接受也不代表成交。

## 输入与持久事实

Analysis 发布的 Signal v4 存入 `trading_entries`，按 `(created_at_ns, entry_id)` 选择 pending 行。本地认证的 operator intent 按 seq 选择 disposition 为空的行。处置就地写入 entry 或 intent，不维护消费高水位，晚提交仍会被处理。交易所暂时不可用时保持待处置；重启时会给缺席期间过期的 Signal 记 `expired`。

同账户较新序号的 pause/halt 已接受时，晚到的 `resume_entries` 记为 `refused` / `superseded`，保持暂停或停止。相同状态和场所证据的订单对账不更新行，也不重写更新时间。

PostgreSQL 保存 inputs、cases、entries、operator intents、orders、fills 和 accounts 七张 Trading 表。Signal 请求、处置与入场计划归同一个 entry；`accept_entry` 仅更新 pending 行，并与订单预留在同一事务提交。条件更新落败不会预留或发送订单，同账户同 symbol 只允许一个 accepted/open/closing entry。成交事实保留原生证据，订单身份查明后只可一次补入账户、client ID 与归属时钟；不能重写原始数量、价格或费用。账户控制、对账快照和按 symbol 的成交游标归 accounts。订单行先于外部请求提交。每条腿的 client ID 由账户槽位、入场身份、腿与尝试序号的 SHA-256 确定，为 32 个 Binance 合法字符。订单超时或 503 后只按该 ID 查询；不能用相同 ID 盲目重发入场。

## 生命周期

执行器用 PostgreSQL advisory lock 保证账户槽位单例，默认每秒向平台 `runtime_processes` 写毫秒心跳；实例 UUID 对写入进行隔离，退出记录 stopped，超过 5 秒、非 running 或有 fault 的进程不能放行发布。Analysis 也拥有独立进程行，业务账户事实不承担存活证明。活跃计划每 5 秒通过签名 REST 核查，整个账户每 60 秒核查。未认领持仓或挂单会暂停新入场；进程停止不会平掉场所持仓。

准入检查包括暂停与紧急停止、已有仓位或普通 / Algo 挂单、并发及杠杆容量、MARKET_LOT_SIZE、最小名义金额、LIVE 参考价漂移和点差。入场使用 DEMO MARKET `RESULT`。终态成交后才一次性挂止损和止盈 Algo 条件单，优先 `closePosition=true`。场所不支持时按实际仓位数量尝试 reduce-only；保护失败时 reduce-only 市价平仓。持有期满、操作员 flatten 和触发保护后的残余仓位也以场所仓位为准执行。

场所显示平仓后撤销剩余保护，再把 Plan 置为终态。成交由 DEMO `userTrades` 和持久 `fromId` 游标读取，订单 ID 关联到 Plan；手续费从原生成交读取。手续费资产若不是 USDT，则不把不同币种直接相减，也不报告已知净收益。终态后的 PnL 证据若未在限时内齐全，标为 `evidence_incomplete`，不会让 Plan 永远保持 open。

## 操作与恢复

`tracefold trading issue` 可提交暂停、恢复、紧急停止、手动入场或账户 flatten。命令入账仅表示请求被接受；平仓与撤单须以签名账户读回为准。Flatten 会先撤普通挂单，再按真实仓位发 reduce-only 单，最后撤 Algo 单并验证零仓零挂单。

上线前应先确认旧执行进程已停、DEMO 仓位和挂单均为零，并保留 `trading_*` 备份。迁移 A 是前向硬切，删除旧执行表，不回填或双读。运行检查使用 `make status`、`make logs`、`tracefold trading status` 及签名场所回执。没有真实账户回执时，测试通过不等于 DEMO 生命周期已验证。

## 实现与测试

- [执行器](../../tracefold/app/executor.py)、[纯决策与订单身份](../../tracefold/trading/executor/core.py)、[DEMO REST 适配](../../tracefold/integrations/trading/binance.py)
- [持久账本](../../tracefold/trading/storage/executor.py)、[硬切迁移](../../tracefold/platform/postgres/alembic/versions/20260929_0417_trading_execution_hard_cut.py)
- [恢复测试](../../tests/e2e/test_executor_recovery.py)、[真 PostgreSQL 账本测试](../../tests/e2e/test_executor_storage.py)

`20261001_0424` 是当前的前向账本迁移，原有订单身份不变，`trading_orders.plan_id` 内部改名为 `entry_id`。完整维护窗口、14 张旧表的导出和备份恢复见 [迁移手册](../MIGRATIONS.md)。
