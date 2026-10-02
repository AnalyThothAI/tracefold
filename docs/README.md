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
| [**News**](modules/news.md)<br/>[语义链路入门](modules/news-semantics-guide.md) | 多来源如何形成知识版本？哪些命题被通知？修订和失败如何恢复？入门篇用示意案例走完全链路 | `news/pipeline` · `news/updates` |
| [**OI 与市场观察**](modules/oi.md) | 测量如何解析和分组？为什么通知阈值不等于交易过滤？ | `news/oi_signals.py` · `news/market_notifications.py` |
| [**Wallets**](modules/wallets.md) | 完整回执如何支撑同窗口净买入？首次与当前快照有什么区别？ | `news/chain_tape` |
| [**Market Review**](modules/market-review.md) | 同名资产如何区分？当前报价和发送时价格补充如何计算？ | `news/market_review` |
| [**评审器校准**](modules/review.md) | 离线固定语料校准能证明什么？ | `news/learning` |

### 交易能力 · 研究与真实执行

| 模块 | 读完后能回答 | 主要入口 |
| :--- | :--- | :--- |
| [**Trading Analysis**](modules/trading.md) | 来源如何进入 Case？预测、六策略、纸面结果与 Signal 如何产生？ | `trading/engine` · `app/trading_*` |
| [**Execution**](modules/execution.md) | 谁拥有订单权限？如何保护、对账和归属真实成交？ | `integrations/trading` · `app/executor.py` |

### 运行与呈现 · 基础设施和只读工作台

| 模块 | 读完后能回答 | 主要入口 |
| :--- | :--- | :--- |
| [**Platform**](modules/platform.md) | 配置、短事务、物理资源和任务监督如何配合？ | `platform` · `app` · `integrations` |
| [**Frontend**](FRONTEND.md) | URL、查询缓存、页面与证据展示各由谁负责？ | `web/src` · `web/tests` |

模块页说明当前职责、主流程、数据与状态、恢复和验证，并链接实际源码。详细时序和特殊规则留在对应模块。

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
| [运维排障](OPERATIONS.md)<br/>具名诊断、精确恢复、备份与 DEMO Executor | [公开契约](CONTRACTS.md)<br/>接口入口、身份、版本与缺失含义 |
| [数据库迁移](MIGRATIONS.md)<br/>升级前提、前向切换与配套恢复 | [开发指南](DEVELOPMENT.md)<br/>所有者、变更范围、文档设计与交付 |
| [安全与权限](SECURITY.md)<br/>秘密、浏览器、模型工具与账户权限 | [测试与 CI](TESTING.md)<br/>当前测试分工、隔离资源和证明范围 |

精确命令与机器契约：[**生成参考**](generated/README.md)。

<details>
<summary><strong>协作与离线研究</strong></summary>

AI 开发入口由[共享指引](agents/shared-router.md)同步至 [AGENTS.md](../AGENTS.md) 与 [CLAUDE.md](../CLAUDE.md)。[Issue / PR](agents/issue-tracker.md)负责协作范围，[worktree](agents/worktrees.md)负责隔离；它们不另造一套业务规则。

[当前术语](../CONTEXT.md) · [离线研究工作区](../notebooks/README.md)

过时原型、截图、逐函数实施清单和退役评测从 Git / Issue 历史检索。冻结数据、已应用迁移、原始回执与仍支撑当前政策的评测证据保留。

</details>

## 当前评测证据

| 报告 | 仍有价值的范围 |
| :--- | :--- |
| [命题召回校准](reports/issue-791-claim-recall-2026-10-02.md) | 固定模型、RRF 校准与已知召回误差；其中独立服务的工程记录属于旧版本 |
| [读者判断校准](reports/news-791-b.md) | v5 命题读数、reader v3、真实重问与未通过指标；不作为部署健康报告 |
| [本地 ONNX 与维护验收](reports/news-799.md) | 当前 Workers 本地编码器、兼容性、降级、回填与资源证明 |

报告保留测量时间、版本和局限；当前运行行为以模块手册为准，操作步骤以运维与迁移手册为准。

---

**一个问题，一个维护入口。** 修改实现时更新对应模块；首页负责导航，架构页负责系统地图，运维页负责操作，生成物负责精确语法。详见[文档维护](DEVELOPMENT.md#documentation-design)。
