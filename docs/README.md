# Tracefold 文档中心

**从整体设计进入，沿一条业务路径读到源码与验证。**

[项目首页](../README.md)　/　[架构图谱](ARCHITECTURE.md#atlas)　/　[开始运行](SETUP.md)　/　[故障定位](OPERATIONS.md)

---

<a id="developer-route"></a>
## 第一次参与开发

先沿一条业务路径建立理解，再查字段和命令。下面的顺序不要求先部署、调用模型或连接账户。

1. 读[架构地图](ARCHITECTURE.md#atlas)，分清业务域、运行进程、数据所有者与权限。
2. 用[统一术语](../CONTEXT.md)识别来源、命题、知识版本、发送意图和实际回执。
3. 沿[News 示意案例](modules/news-semantics-guide.md)走到发送结果，再到[模块设计](modules/news.md)查召回、决策、状态和失败处理。
4. 修改研究或执行时，继续读 [Trading](modules/trading.md)与 [Execution](modules/execution.md)，区分 LIVE 研究、DEMO 账户和场所证据。
5. 在模块的源码与测试入口定位行为所有者，按[开发验证](DEVELOPMENT.md#risk-tiered-local-verification)选择检查。字段和命令查[契约参考](CONTRACTS.md)，恢复步骤查[运维](OPERATIONS.md)。

读完一条路径后，应能解释一个正常结果和一个失败结果：哪个版本被处理、哪个决定被保存、外部效果由什么证明，以及下一步由谁恢复。中文手册的写法由[清晰技术写作](DEVELOPMENT.md#clear-technical-writing)维护。

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
| [**读者标注规范**](modules/news-reader-labeling.md) | 如何独立盲标、冻结故事抽样框、认证切线并保留失败分母？ | `scripts/news_reader_labeling.py` · `scripts/eval_news_reader.py` |
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
| 召回命中为什么不等于重复新闻？ | [统一术语](../CONTEXT.md#decision-terms) · [候选与排序](modules/news.md#related-recall) |
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

| 文档类型 | 回答什么 | 维护边界 |
| :--- | :--- | :--- |
| 入门走读 | 一条输入如何经过各阶段？ | 用例解释交接，具体规则链接模块 |
| 架构与模块设计 | 谁负责、为什么这样处理、保存哪些状态？ | 描述当前实现，连接源码和测试 |
| 操作手册 | 什么前提下执行什么命令，如何核实和恢复？ | 保留权限、版本与实际结果的前提 |
| 生成参考 | 精确字段、约束和命令语法是什么？ | 由实现生成，不手工改派生内容 |
| 评测报告 | 某版本在某样本上证明了什么？ | 保留测量时间、失败和局限 |

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
