# Issue #736 实施处置与验证记录

基线：`364e0d9abdc5c1f2dcc27aa19c2bb0736b7fffaf`。本记录对应一个完整实现 PR；精确到 Python 函数/方法的基线与新行号、处置、所有者和测试处理见 [逐函数清单](issue-736-function-disposition.csv)。清单只包含实际删除、新增或源码变化的节点，不把未经修改的函数标为已审计。前端改动的入口和组件在下表单独记录。

| 范围 / 当前入口 | 决定与数据、作用边界 | 测试处置与证据 |
| --- | --- | --- |
| §2 无引用目标 | 25 个节点删除，包含 `evidence_artifacts.py`、`json_safety.py` 两个整模块。当前 `AnalysisFiles`、News 实际证据写入与平台 future 完成边界继续拥有原职责；不留 alias。 | 基线隔离删除证明、全量 hermetic、静态类型与导入检查。 |
| ReviewDesk `ReviewDesk.open/evidence/submit` | 当前 `dec.*` 与外部漏报反馈使用同一 SQL builder；状态、来源、cursor 在 PostgreSQL 中过滤和分页，避免先取 40 行再筛导致稀疏结果丢失。旧 `evt.*` 明确拒绝，旧 rubric/虚拟 task/抽样/coverage 副本退役。提交仍由原事务持有。 | 40+1 与稀疏分页真实 PostgreSQL 反例；Review CLI、合同与 query audit。 |
| News `FeedStorage` / HTTP / React reader | EventUpdate head、来源、实际发送回执各自展示；删除旧 Triage verdict、editorial v2/v3/v4 rich projection 的解析及 fallback。旧历史表和记录保留；没有据源数据推断历史发送。OpenAPI 和前端类型按当前合同生成。 | API PostgreSQL 7 项、News 相关集成与前端组件/Vitest/Playwright。Golden 的旧字段断言改为确认响应不再暴露该字段；状态容量测试改为 1,948 条当前 observed evidence Event，单项 PostgreSQL 通过。 |
| Trading root tape / 旧报告 | 删除 producer、scheduler、PG 方法、七个研究/归档脚本及其专属测试；正常 Case、Signal、WATCH、标签与执行状态机保留。九个固定样本 notebook 从维护树退役；四个可复用 OI 工具保留在 `notebooks/research`，15 项原回归纳入正常 collection，另补窗口、取消和在途上限回归。旧实现从基线 Git 可取。 | Trading PostgreSQL 32 项、迁移 4 项、OI 17 项；不对真实行情网络作验证。 |
| `CaseToolContext` / `FrameReader` / `AnalysisRunner` | 共享准确的 `PreparedAnalysis` 合同；预算必须提供真实 deadline 和 `remaining_ms`，缺字段直接出错。仅提取 brief、终态 payload、市场投影的纯组装；取数、lease、模型调用、提交与异常归因由原编排持有。 | 工具/fake 回归 79 项、Trading PostgreSQL 32 项、mypy 355 个源文件。 |
| 平台 drain | 两处同构排空改用 `platform.resource.drain_futures`；调用者取消不表示底层线程已结束，permit 等物理完成后释放。 | worker drain / 取消测试及全量 hermetic。 |
| 测试共享替身 | 跨 `test_*.py` 导入 20 条归零；窄 `tests/support` 分属 CLI、market OI、Telegram、Trading、wallet。`RecordingNews` 对未知方法具名失败，合法空结果由显式回应构造。 | import 语法扫描 0 条、相关 hermetic 与 PG 回归。 |
| 历史 PostgreSQL 对象 | 历史 trading tape 表物理存在，运行必需/读写审计不再把它当当前路径；schema 生成仍精确对照物理库，不删除历史 migration 或行。 | PG audit/schema/generated docs 14 项。 |
| 前端入口 `NewsFeedToolbar`、`NewsEventRow`、`NewsEventDetailPage`、`NewsEventDrawer`、`NewsSymbolPage` | 移除旧方向筛选/徽章与旧响应字段；当前命题、来源、报价分别展示。抽屉仅对当前命题且在事件标的表实际列出的标的提供代币页链接，不把商品代码当币种。 | Vitest 311 项、typecheck/build/lint、响应式 Playwright；具体最终计数见 PR。 |

## 刻意保留的界线

- `news_verdicts` 与历史 trading tape 表仍是 PostgreSQL 历史事实；此次仅退出当前运行读取与采样。旧 migration 和历史事实没有改写。
- News wallet chain tape 是另一条当前链上事实路径；这次的 “root research tape” 退役不删除它。
- `replay_hits` 只声明 provider admission/dedupe 诊断；不声称模拟完整 EventUpdate、多 scope、发送链。
- 没有复制第二个 Case 状态机、跨外部 I/O 持有事务，或用不完整 fake 让缺预算被视为空。

## 剩余独立证明

本地测试与 PR CI 是不同证据。独立 RabbitMQ/Workers → PostgreSQL → HTTP 的生产链路和断路修复两项 golden 已通过；部署生命周期及远端必需 CI 应以 PR head 的运行回执为准。没有本地回执的项目不记为通过。此实现没有执行合并、部署或实盘交易。
