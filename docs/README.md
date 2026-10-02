# Tracefold 文档中心

**从整体设计进入，沿一条业务路径读到源码与验证。**

[项目首页](../README.md)　/　[架构图谱](ARCHITECTURE.md#atlas)　/　[开始运行](SETUP.md)　/　[故障定位](OPERATIONS.md)

---

## 选择你的阅读路径

| 开始使用 | 理解与开发 |
| :--- | :--- |
| **部署与配置**<br/>了解前置条件、默认能力、容器地址与持久数据。<br/>[安装配置](SETUP.md) → [状态检查](OPERATIONS.md#diagnostics) | **架构与代码审查**<br/>先区分进程、包依赖与数据所有权，再沿模块定位实现。<br/>[系统地图](ARCHITECTURE.md) → [模块手册](#modules) → [契约](CONTRACTS.md) |
| **日常运维与恢复**<br/>按角色、版本与工作身份诊断，不用重跑整库代替精确恢复。<br/>[运维排障](OPERATIONS.md) → [迁移恢复](MIGRATIONS.md) → [权限](SECURITY.md) | **功能开发与验证**<br/>理解行为所有者，修改调用方，选择能验证本次风险的测试。<br/>[开发指南](DEVELOPMENT.md) → [测试分层](TESTING.md) → [协作约定](agents/issue-tracker.md) |

> [!NOTE]
> 手册描述**同一检出版本的源码**。线上镜像、历史报告和当前 main 可能不同；排障前确认源 SHA、镜像与数据库版本。尚未合并的设计不是已实现能力。

<a id="modules"></a>
## 模块手册

### 信息产品 · 理解、观察与通知

| 模块 | 读完后能回答 | 主要入口 |
| :--- | :--- | :--- |
| [**News**](modules/news.md)<br/>[语义链路入门](modules/news-semantics-guide.md) | 多来源如何形成知识版本？哪些命题被通知？修订和失败如何恢复？入门篇用一条真实新闻走完全链路 | `news/pipeline` · `news/updates` |
| [**OI 与市场观察**](modules/oi.md) | 测量如何解析和分组？为什么通知阈值不等于交易过滤？ | `news/oi_signals.py` · `news/market_notifications.py` |
| [**Wallets**](modules/wallets.md) | 完整回执如何支撑同窗口净买入？首次与当前快照有什么区别？ | `news/chain_tape` |
| [**Market Review**](modules/market-review.md) | 同名资产如何区分？当前报价和发送时价格补充如何计算？ | `news/market_review` |

| [**Review**](modules/review.md) | 离线固定语料校准能证明什么？ | `news/learning` |

### 交易能力 · 研究与真实执行

| 模块 | 读完后能回答 | 主要入口 |
| :--- | :--- | :--- |
| [**Trading Analysis**](modules/trading.md) | 来源如何进入 Case？Agent 可读什么？WATCH 和 Signal 如何产生？ | `trading/engine` · `app/trading_*` |
| [**Execution**](modules/execution.md) | 谁拥有订单权限？如何保护、对账和归属真实成交？ | `integrations/trading` · `app/executor.py` |

### 运行与呈现 · 基础设施和只读工作台

| 模块 | 读完后能回答 | 主要入口 |
| :--- | :--- | :--- |
| [**Platform**](modules/platform.md) | 配置、短事务、物理资源和任务监督如何配合？ | `platform` · `app` · `integrations` |
| [**Frontend**](FRONTEND.md) | URL、查询缓存、页面与证据展示各由谁负责？ | `web/src` · `web/tests` |

模块页统一提供**职责摘要 → 主流程 → 数据与状态 → 恢复和验证 → 源码入口**。详细时序和特殊规则留在对应模块，不把整个系统挤成一张图。

## 直接定位一个问题

| 问题 | 入口 |
| :--- | :--- |
| 一条新闻从接入到推送经过哪些步骤？ | [语义链路入门](modules/news-semantics-guide.md) · [端到端数据流](modules/news.md#section-端到端数据流) |
| 新闻为什么不推送？ | [逐命题通知](modules/news.md#notification) · [精确版本恢复](OPERATIONS.md#news-retry) |
| 一条消息为什么有多个 Event？ | [输入范围与身份](modules/news.md#input) |
| 模型究竟调用几次？ | [NewsAgent 与预算](modules/news.md#agent) |
| 有策略方向为什么没有成交？ | [预测与发布](modules/trading.md) · [执行与对账](modules/execution.md) |
| 账户状态未知与未认领敞口怎么读？ | [账户操作边界](OPERATIONS.md#trading-operations) |
| 文档图如何修改并验证？ | [写作与图表规范](DEVELOPMENT.md#documentation-design) · [渲染检查](TESTING.md#diagrams) |

## 工程与操作参考

| 运行与维护 | 开发与契约 |
| :--- | :--- |
| [安装与配置](SETUP.md)<br/>首次启动、能力配置、地址和挂载 | [系统架构](ARCHITECTURE.md)<br/>进程、包依赖、数据所有权与跨域时序 |
| [运维排障](OPERATIONS.md)<br/>具名诊断、精确恢复、备份与独立 Runtime | [公开契约](CONTRACTS.md)<br/>接口入口、身份、版本与缺失含义 |
| [数据库迁移](MIGRATIONS.md)<br/>升级前提、前向切换与配套恢复 | [开发指南](DEVELOPMENT.md)<br/>所有者、变更范围、文档设计与交付 |
| [安全与权限](SECURITY.md)<br/>秘密、浏览器、模型工具与账户权限 | [测试与 CI](TESTING.md)<br/>当前测试分工、隔离资源和证明范围 |

精确命令与机器契约：[**生成参考**](generated/README.md)。

<details>
<summary><strong>协作、设计记录与历史研究</strong></summary>

AI 开发入口由[共享指引](agents/shared-router.md)同步至 [AGENTS.md](../AGENTS.md) 与 [CLAUDE.md](../CLAUDE.md)。[Issue / PR](agents/issue-tracker.md)负责协作范围，[worktree](agents/worktrees.md)负责隔离；它们不另造一套业务规则。

[本轮视觉审阅样张](design/handbook-visual-review.md) · [复核术语](../CONTEXT.md) · [News 详情设计记录](design/news-event-detail.md) · [Issue 717 固定窗口报告](reports/issue-717-hourly-comparison-2026-09-27.md) · [Issue 725 编辑判断历史对照](reports/issue-725-attention-2026-09-27.md) · [Issue 736 实施处置](reports/issue-736-disposition-2026-09-28.md) · [Issue 791 命题召回评测](reports/issue-791-claim-recall-2026-10-02.md) · [离线研究工作区](../notebooks/README.md)

设计截图和报告保留其时间、来源与验证限制。冻结数据、已应用迁移、原始回执不能因清理文档被改写；过时方案从 Git / Issue 历史检索，不继续作为当前操作步骤堆放。

</details>

---

**一个问题，一个维护入口。** 修改实现时更新对应模块；首页负责导航，架构页负责系统地图，运维页负责操作，生成物负责精确语法。详见[文档维护](DEVELOPMENT.md#documentation-design)。
