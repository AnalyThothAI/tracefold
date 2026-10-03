# Trading #760 当前账本正确性验证

对应 [#760](https://github.com/AnalyThothAI/tracefold/issues/760) 的 2026-10-03 规格，替代已关闭未合并的 #762。
工程交付、合并、部署、账户执行和收益证明分别记录。

## 树与资源

开始 checkout / 当时 main：034b55f82621a8b038472c8a26ed8fd9ca88fdaa。提交前重新 fetch，
main 新增独立 News #814，更新后的基线为 0ac479fa6。分支 codex/760-trading-correctness，没有 cherry-pick #762。
单链 schema 为 20261002_0429 → 20261003_0430，仍使用七张 Trading 表及平台进程表。

本地 Python 3.13.13；PostgreSQL 18、RabbitMQ 4.3.5。隔离容器 tracefold-760-test-pg（55544）、
tracefold-760-test-rabbitmq（45680 / 45681）；PG harness 克隆/迁移库及 E2E testcontainers
均为一次性资源。未使用部署数据库、默认 5672 broker 或真实账户。

## 回归矩阵

| 范围 | 实际模块与证明 |
| --- | --- |
| R1 | USDT equity / available / initial margin、实际杠杆、crossed / isolated、mark、费率、filters 的 Decimal 三约束；部分成交余量保留；两个 PG 连接的旧快照不受理；真实约束失败整批回滚且不 POST；受理后来源过期阻止发送。 |
| R2 | unknown / working / filled 的可归属部分敞口由真实 runner 发 SL；首次观测时钟只写一次；外来反向/超量/无执行证明仓位不认领；撤单 ACK 与最后成交竞争；closePosition 覆盖增长、数量不足降风险；unknown 保护保留原身份，不重发。 |
| R3 | 风险动作先于 active / terminal 待补成交；即时错误与 10s 慢读取被 2s 预算局部延期；批次最多三个 symbol；fill 约束失败时 fill/归属/cursor 整批回滚；external flatten 撤单失败局部隔离。 |
| R4 | 实际 AnalysisRunner / TradingAssessor 的 8 Case / 2 slots / 虚拟 55s 调用：最多两份领取，全部合法完成；真实 PG 锁等待后租约失权、换 token 后旧 finish 被拒；取消及重复取消等待物理 DB 操作回滚后才归还容量。 |
| R5 | 新 flatten 不替换在途 command 或重置尝试；三次可靠退出失败保存责任故障；重启、heartbeat、resume 均不清除；变更退出原因不重置次数；逐 symbol 恢复只清自己的故障并保留 command、用户 pause/halt；健康进程上的故障在 UI 可见。 |
| R6 | 原生拒单回包单独保留；Algo 撤单 ACK 与不完整成功按原身份查询，不伪造终态；明确拒单、never-sent 与可靠原生零成交正常结算且不计成交笔数；timeout、503、坏/不完整回包、not-found 保留 unknown；已观察敞口不能被零 executedQty 覆盖；缺成交/非 USDT 费用沿用独立 PnL deadline，未知净收益不填零。 |
| 迁移 | 实际 0429 数据包含 active entry、unknown order、pause/flatten；0430 保留全部原字段/身份/控制；存在新责任时 downgrade 明确拒绝，事务回滚后 head 与事实保留。 |

测试入口：[纯函数与编排反例](../../tests/trading/test_correctness_760.py)、
[真实 PG 故障回归](../../tests/integration/test_trading_correctness_760.py)、
[迁移与回退边界](../../tests/integration/test_migration_trading_evidence_0430.py)、
[重启与账户 flatten E2E](../../tests/e2e/test_executor_recovery.py)。

## 已执行与证据边界

以下结果来自开发工作树。最终合并证据由实现 PR 当前提交的 Checks 提供，不能引用旧 #762 的 CI。

| 命令 / 资源 | 退出码与结果 |
| --- | --- |
| 将同一组 11 项回归复制进 git archive 034b55f8，以 locked Python 导入基线实际模块运行 pytest | 1；11 failed，覆盖 R1/R2/R3/R4/R6。临时目录在 /tmp，不是复制函数探针。 |
| pytest：专用 PG 回归、迁移与纯反例三文件 | 0；32 passed；随后新增支持模式、失败原子性和发送前 TTL 的测试由最终 CI 覆盖。 |
| make ci-python-hermetic | 0；2163 passed，1286 deselected。 |
| make ci-quality-static | 0；ruff / mypy / 生成物 / 文档链接和 357 项架构契约测试。 |
| frontend typecheck / ESLint / architecture / unit / format / build / Playwright 四 viewport | 子检查通过；23 项架构、274 项 unit、141 项浏览器交互。full-stack 初次缺显式资源，不能把该次完整 lane 报为通过。 |
| make ci-postgres-behavior 首轮 | 2；601 passed，35 个未声明 broker 错误、CLI 新字段 fixture 和 CRLF 生成物快照两项失败；已修正 fixture / 资源，最终结果以当前 PR CI 为准。 |
| make ci-runtime-broker 首轮（隔离资源） | 2；34 passed，一个 golden 子进程随机端口冲突（Errno 98）；不能报全部通过，最终 CI 重新执行。 |
| make ci-deploy-e2e 后续执行 | 2；84 passed，两项既有 init shell 的 CRLF 工作副本失败；用户原文件不随本任务提交。最终 CI 使用正常 LF checkout。 |
| make regen-contract；新隔离库升级 head 后 scripts/regen_db_schema.py | 0；OpenAPI、TS 与真实 DB introspection 同步。 |

初次 hermetic 分发打包检查要求新 migration 被 Git 跟踪，新文件已加入。
News 认证报告及前端的 CRLF 工作副本曾造成字节/格式失败；规范化不改变其 Git 内容、证书或测试断言。
没有删除反例、跳过必需检查或把 pending CI 当作成功。

发送/撤单证据复核后，专用 PG、adapter 和重启 E2E 三文件合计 31 passed。最终 HEAD 的全量 CI 仍由本 PR Checks 提供。

## 未执行

真实 DEMO 的 symbolConfig / multiAssetsMargin / commission 返回形状及订单生命周期：NOT_RUN。
官方生产接口语义与 adapter 替身不能替代现场支持证明。付费模型重问、策略收益、部署、
实际 schema 迁移、修改账户杠杆/模式、实盘交易：NOT_RUN。
不承诺模型调用物理 exactly-once；持久发布仍由 token/lease 和单 assessment fencing 约束。

发布及回退按 [迁移手册](../MIGRATIONS.md) 的 0430 条目执行。
新执行证据存在时保持新增风险停止，采用修复前进或经授权的配对恢复；
不能在已发生交易所副作用后恢复旧备份并当作未执行。
