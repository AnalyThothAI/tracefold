# Execution：真实准入、订单恢复与 DEMO 对账

[手册](../README.md) · [Trading](trading.md) · [运维](../OPERATIONS.md#trading-operations) · [安全](../SECURITY.md)

`tracefold executor` 仅持 DEMO 凭据，LIVE 用于研究/参考。PG advisory lock 单例拥有账户槽位，通常每秒心跳、5 秒活跃 Plan 对账、60 秒账户核验；请求、接受、成交与平仓分别保存。

## 输入、准入与预留

Signal v4/operator intent 各有游标和唯一 accepted/refused/expired disposition；缺席期间过期也明确记录。自动/手工共用具体 EntryRequest/_enter，手工不伪造 Case/模型身份。

纯 admit_entry 使用真实 availableBalance、symbol leverage、CROSSED/one-way/single-asset/canTrade、mark/book 时刻、taker fee、未被远端包含的本地预留。数量同时受止损风险、组合名义上限与可用保证金约束，再向下取 MARKET_LOT_SIZE；缺失/过期/不支持模式/最小金额不够具名拒绝，不改模式/升杠杆。

max_plans、max_drift_bps、max_spread_pct、fee/price buffer 与14400s stop冷却实际消费。漂移分冻结→当前 LIVE 时间变化和 LIVE→DEMO 基差；冷却仅由终态 Plan 已归因 SL 原生成交证明。

短事务原子写 Plan/准入快照/预留/Order/disposition/游标，事务外 POST；下一入场看到持久未决，远端已占保证金不重复扣。−2019 不盲重发。

## 证据与恢复状态

账本含 Signal/intent/disposition/Plan/Order/原生 Fill/只追加 attribution/游标。client ID 从账户/输入/腿/attempt 确定，为32合法字符；POST 前记录 started、精确参数和时刻，receipt 后另记时刻并保留原请求。

- reserved+not_started+submitted_at=NULL 才是未发送证据；过期入场不提交，外部 flatten 以当前仓位同 ID reduce-only 一次。
- timeout/−1007/暂态503为 unknown，只查询固定 ID；查询不存在或7秒过去不证明未发送，持续 fence/fault。
- 明确业务拒绝 rejected，核实零成交 entry_rejected 结算零，不等待计划 quantity 虚构fill。
- 部分/迟到成交、working/filled/cancelled 按场所证据；终态与收益完整性分别保存。

## 保护、退出与隔离

有真实仓位即管理，不等 entry 终态；先 closePosition SL，部分成交随后撤 entry 余量，再补 TP；有界 fallback 为实际数量 reduce-only。保护失败/到期/flatten/残余仓位均按真实 positionAmt 退出。

保护/退出先于 userTrades 历史同步；Plan/symbol REST失败独立记录并继续其他持仓。账户整体 positions 失败为事实未知，不能假设flat。历史补齐后才结算，不从本地请求推断暴露。

未解提交/退出耗尽写 faults，暂停入场继续有界对账；heartbeat 不清 faults/last_error。确认订单解决只清对应键，场所flat清该Plan，账户flatten核实零仓/普通单/Algo清命令。外来敞口另由 durable control 暂停，核实后 operator 恢复，不靠重启消除。

## 历史与收益口径

userTrades 按 native symbol/trade ID 去重，晚到身份追加归因。首游标从最早保留意图前60秒起、最多最近七天，bootstrap_since_ns记范围，再用fromId；旧起点未知保留NULL。

fills 分 owned_plan/owned_command/unbound 与command/Plan；历史或未归因不表示无效。记分只连接精确action→Signal→Plan→实际entry fills。UI totals仅已归因Plan成交净收益，含手工/USDT手续费；未覆盖资金费/转账/期初权益/未归因历史，不代表账户权益变化。异币费用/缺成交为未知，有限等待后 evidence_incomplete。

## 操作与证明

trading issue 写OS认证意图，固定request ID/时刻，accepted不代表场所完成；flatten暂停入场、撤普通单、reduce-only、撤Algo，再签名读回，unknown不重发。0419 stopped-writer前向保留账本，非0417/0418硬切；维护核实备份/暴露/持续场所保护，停进程不平仓，PR不授权部署/账户副作用。

[工作流](../../tracefold/app/executor.py)、[纯准入](../../tracefold/trading/executor/core.py)、[REST](../../tracefold/integrations/trading/binance.py)、[账本](../../tracefold/trading/storage/executor.py)、[恢复测试](../../tests/e2e/test_executor_rootfix.py)、[迁移保留](../../tests/integration/test_trading_rootfix_migration.py)。真PG/录制夹具不替代自然SL/TP/到期/拒绝/重启回执与七天覆盖。
