# Tracefold 中文手册

[项目首页](../README.md) · [安装配置](SETUP.md) · [系统架构](ARCHITECTURE.md) · [运维排障](OPERATIONS.md)

本手册以**同一检出版本的源码、契约和测试**为依据。它说明系统如何工作，不把历史 Issue 方案、旧部署结果或模型输出当作当前实现。部署镜像可能与仓库版本不同，排障时先确认版本。

## 按目标阅读

| 你要完成什么 | 推荐路径 |
| --- | --- |
| 第一次运行项目 | [安装与配置](SETUP.md) → [应用状态检查](OPERATIONS.md#diagnostics) |
| 理解整个系统 | [系统架构](ARCHITECTURE.md) → 下方模块手册 → 对应源码入口 |
| 理解新闻为什么推送或不推送 | [新闻链路](modules/news.md) → [命题级通知决策](modules/news.md#notification) → [按版本恢复](OPERATIONS.md#news-retry) |
| 理解 OI、行情和钱包的区别 | [市场观察](modules/oi.md) → [行情与复盘](modules/market-review.md) → [钱包净买入](modules/wallets.md) |
| 理解交易建议与真实成交 | [交易研究](modules/trading.md) → [执行与对账](modules/execution.md) |
| 修改代码并提交 PR | [开发指南](DEVELOPMENT.md) → [测试指南](TESTING.md) → [worktree](agents/worktrees.md) |
| 升级、恢复或处理账户异常 | [运维](OPERATIONS.md) → [数据库迁移](MIGRATIONS.md) → [安全与权限](SECURITY.md) |

<a id="modules"></a>
## 模块手册

| 模块 | 负责的问题 | 主要源码 |
| --- | --- | --- |
| [News：新闻理解与通知](modules/news.md) | Item 如何形成 Event、EventUpdate？谁决定通知？失败与修订如何恢复？ | `news/pipeline`、`news/events`、`news/updates`、`news/storage` |
| [OI 与市场观察](modules/oi.md) | 数字如何解析？观察如何分组？通知与交易研究如何解耦？ | `news/oi_signals.py`、`news/market_notifications.py` |
| [Market Review：行情与事件复盘](modules/market-review.md) | 标的身份、当前报价、新闻后价格反应如何分别计算？ | `news/market_review`、`integrations/venues` |
| [Wallets：链上净买入](modules/wallets.md) | 名单、回执、完整前缀、窗口与首报如何闭环？ | `news/chain_tape`、钱包存储与契约 |
| [Trading Analysis：交易研究](modules/trading.md) | Source → Case → Agent → 决策 → WATCH / Signal | `trading/engine`、`trading/storage`、`app/trading_*` |
| [Execution：执行与对账](modules/execution.md) | 谁下单？如何保护仓位、归属成交、恢复真实执行历史？ | `integrations/nautilus`、`app/nautilus` |
| [Review：复核与校准](modules/review.md) | 什么是已接受复核？当前保留哪些评估工具？ | `news/review`、`news/learning` |
| [Platform：基础设施与装配](modules/platform.md) | 配置、数据库、资源、适配器和 Workers 监督如何协作？ | `platform`、`integrations`、`app` |
| [Frontend：只读工作台](FRONTEND.md) | 路由、查询、页面状态与证据展示如何组织？ | `web/src`、`web/tests` |

每份模块文档按“职责与边界 → 源码入口 → 数据流 → 状态与失败 → 验证入口”组织。流程图中的步骤不自动等于数据库枚举；概念关系图也不冒充物理外键图。

## 系统与工程参考

| 文档 | 唯一维护职责 |
| --- | --- |
| [ARCHITECTURE](ARCHITECTURE.md) | 进程、依赖方向、状态归属、跨域交接和事务边界 |
| [SETUP](SETUP.md) | 首次运行、默认配置、容器网络与开发启动 |
| [CONTRACTS](CONTRACTS.md) | 公开接口语义、当前 HTTP 路由、读写边界 |
| [OPERATIONS](OPERATIONS.md) | 诊断命令、精确重试、执行运维、备份恢复 |
| [MIGRATIONS](MIGRATIONS.md) | Schema 升级、前置条件、不可逆切换与恢复 |
| [SECURITY](SECURITY.md) | 凭据、访问、模型工具与账户操作权限 |
| [DEVELOPMENT](DEVELOPMENT.md) | 设计原则、最小验证、生成物与交付标准 |
| [TESTING](TESTING.md) | 当前测试命令、CI 分工、资源隔离和证据限制 |
| [生成参考](generated/README.md) | CLI 帮助、OpenAPI、数据库结构的生成入口 |

## 代码协作与历史研究

AI 开发入口由[共享指引](agents/shared-router.md)同步至根目录 `AGENTS.md` 和 `CLAUDE.md`。[Issue / PR 约定](agents/issue-tracker.md)负责范围与标签，[worktree 约定](agents/worktrees.md)负责隔离。业务架构只在本手册及对应模块维护，不另设一份重复的领域规则。

[CONTEXT.md](../CONTEXT.md)解释复核术语；[notebooks](../notebooks/README.md)区分可用离线工具和历史实验。冻结数据、已应用迁移和原始回执不能因为“清理旧文档”被改写或删除。过时方案与一次性调查从 Git / Issue 历史检索，不作为当前使用步骤继续堆放。

## 如何维护这份手册

修改实现时更新对应模块的入口、输入输出、状态与失败语义，并附实际源码和测试链接。首页只做入口，架构页只做系统地图，模块页解释行为，运维页写操作，生成物保存精确语法。跨页引用优先于复制。

文档正文使用中文，公开标识符、错误码、路径与命令保留原样。Mermaid 使用小型流程图、时序图或状态图；提交前验证语法并实际渲染，避免把许多不相关状态挤成一张大图。

```bash
python3 scripts/check_mandatory_docs_links.py
python3 scripts/sync_agent_router.py --check
```

链接检查覆盖本地文件、Markdown 锚点与引用式链接，但不能证明远程链接可用、命令执行成功、图形布局美观或业务部署健康。完整验证分工见[测试指南](TESTING.md)。
