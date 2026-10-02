# 开发指南

[手册](README.md) · [架构](ARCHITECTURE.md) · [测试](TESTING.md) · [worktree](agents/worktrees.md) · [Issue / PR](agents/issue-tracker.md)

以一个可观察、可验证的结果为单位完成改动。先读受影响实现与测试，再修改对应文档；不为了遵守一套流程把小问题拆成许多新服务、gate、Issue 或兼容层。

<details>
<summary><strong>本页目录</strong></summary>

1. [开始前确认范围](#section-开始前确认范围)
2. [代码应该放在哪里](#section-代码应该放在哪里)
3. [保持事实、政策和副作用分离](#section-保持事实政策和副作用分离)
4. [事务与资源完成](#section-事务与资源完成)
5. [按改动风险选择本地验证](#section-按改动风险选择本地验证)
6. [生成契约与文档同步](#section-生成契约与文档同步)
7. [测试本身也需要可信](#section-测试本身也需要可信)
8. [完成与交付](#section-完成与交付)

</details>

<a id="section-开始前确认范围"></a>
## 01 · 开始前确认范围

```bash
git status --short --branch
git rev-parse HEAD
git worktree list
```

复用已分配且合适的任务 checkout；涉及并行开发、未提交修改或部署目录时使用独立 worktree。只读调查不要求创建分支。通过 GitHub connector 修改时同样使用独立分支与已知 base SHA，不假装做过本地测试。

项目的 Python 解释器由 `.python-version` 固定，依赖由锁文件管理：

```bash
uv sync --locked
```

只有前端相关工作才需要安装前端依赖；只有真实依赖测试才需要准备对应隔离资源。不要为了运行一个文档检查先重启整套部署。

<a id="section-代码应该放在哪里"></a>
## 02 · 代码应该放在哪里

| 要改变的行为 | 首选位置 |
| --- | --- |
| 新闻来源范围、归组与证据 | `news/events`、`news/pipeline` |
| 命题提取与采用 | `news/updates`、语义 storage；具体模型调用放 `news/adapters` |
| 通知选择、卡片与持久发送工作流 | `news/notifications`、通知 storage；实际传输由 pipeline 适配器完成 |
| 市场 / 钱包确定性规则 | 对应 market、chain_tape 领域逻辑，不复制进 UI |
| LIVE 特征、概率策略与纸面双腿几何 | `trading/engine` 的纯逻辑 |
| Signal 准入与执行生命周期决策 | `trading/executor` 的纯逻辑；I/O 编排由 `app/executor.py` 拥有 |
| 持久 Case / 执行记录 | `trading/storage` |
| 外部 provider、交易所或传输 | `integrations`，通过已有业务端口装配 |
| 进程、HTTP / CLI、跨域映射 | `app` |
| 配置、数据库物理资源、可观测性 | `platform` |
| 页面阅读与交互 | feature-owned 前端 API / model / state / UI |

完整的源码入口见[架构地图](ARCHITECTURE.md#packages)。News 与 Trading 不导入对方内部实现、不直接访问对方表；App 映射公开契约。新增 helper 不自动需要一个新包或抽象接口，先判断是否真的存在独立所有权与变化原因。

<a id="section-保持事实政策和副作用分离"></a>
## 03 · 保持事实、政策和副作用分离

| 层次 | 设计要求 |
| --- | --- |
| 原始事实 | 保存来源、时钟、身份、修订与未知，不因模型答案改变 |
| 派生结果 | 明确输入和版本，能够说明如何重算；不冒充原始事实 |
| 业务政策 | 一个具名所有者；规则、默认与例外不在 CLI / UI / Worker 各复制一次 |
| 外部副作用 | 有稳定身份、持久意图、实际结果与恢复边界 |
| 展示 | 读取已有事实与决策，不隐藏模型调用、数据写入或账户命令 |

一个内部重命名或替换应同时修改调用方、测试和文档，并删除被替代路径。没有实际外部兼容需求时不保留旧 alias、双配置和临时并行实现；有持久历史或真实外部契约时明确版本，不重写原始证据。

不要把“缺数据”“内容不确定”“模型失败”“不值得通知”“不交易”和“账户不可核实”压成一个失败码。保留对用户下一步有用的原因，也不为每个小分支创建第二套状态机。

<a id="section-事务与资源完成"></a>
## 04 · 事务与资源完成

调用方拥有短事务，仓储不隐藏 commit。SQL、必要锁和条件更新放在事务内；模型、网络、文件读取、昂贵转换与哈希在事务外。跨 PostgreSQL、RabbitMQ、模型和交易所的步骤不能假装是一笔原子事务。

外部提交必须考虑“调用超时但结果未明”。稳定身份帮助重放，实际副作用由适配器证据与对账确认。不能通过无限重试、补一个成功值或提前释放仍在执行的资源来得到表面闭环。

资源许可应跟随真实操作完成，而非仅跟随等待协程取消。具体能力与超时所有者见[平台](modules/platform.md)和[事务边界](ARCHITECTURE.md#transactions)。

<a id="risk-tiered-local-verification"></a>
<a id="section-按改动风险选择本地验证"></a>
## 05 · 按改动风险选择本地验证

| 改动 | 优先验证 | 何时扩大 |
| --- | --- | --- |
| 文档、导航、共享 Agent 指引 | 本地链接 / 锚点、入口同步、文档导航测试、Mermaid 渲染 | 文档修改了公开参数或接口清单时加对应契约检查 |
| 纯业务函数 | 相关单元测试、边界和错误输入 | 影响共享契约或多模块调用方时扩大回归 |
| 查询、约束、事务或迁移 | 隔离 PostgreSQL 行为与迁移测试 | 涉及进程顺序、升级或生产资源时加部署验证 |
| broker、重试、租约与恢复 | 独立 RabbitMQ / PostgreSQL 接缝与进程测试 | 跨角色链路改变时加 golden path |
| 前端类型与派生 | TypeScript、架构、单元 / 组件 | 路由 / 布局 / 会话变化时加实际浏览器检查 |
| 模型工具或 proposal 契约 | 确定性替身、输入 / 引用 / 预算 / 失败路径 | 明确需要质量评估时单独记录真实模型与数据协议 |
| 执行与对账 | 作用域、订单身份、原生证据与恢复测试 | 真实账户操作必须另有明确授权，测试不能自动升级为实盘 |

常用纯检查：

```bash
make check
make test-fast
```

它们不是所有任务每次必跑的仪式；修改范围已由更小检查充分覆盖时，可报告精确结果与未验证项。一个成功的完整集合已经覆盖未变化的子集时，不必为填清单反复运行相同测试。

真实资源测试、CI 选择和报告定义由[测试指南](TESTING.md)维护。缺少某个资源仅限制对应证明；不能假装通过，也不阻止独立修订和 PR 准备。

<a id="section-生成契约与文档同步"></a>
## 06 · 生成契约与文档同步

| 发生变化 | 更新什么 |
| --- | --- |
| HTTP schema / route | OpenAPI、前端生成类型与 `CONTRACTS.md` 接口语义 |
| CLI parser / help | 生成 CLI 帮助及对应操作步骤 |
| Alembic schema | 隔离目标数据库上的结构生成物、迁移说明 |
| RabbitMQ definitions / policy | 由实际生成器刷新并校验，不直接编辑派生 JSON |
| Agent 共享约定 | 先改 `docs/agents/shared-router.md`，再同步两个根入口 |
| 模块行为 / 所有权 | 更新唯一对应模块手册；首页和总架构保持索引职责 |

```bash
python3 scripts/sync_agent_router.py --write
python3 scripts/sync_agent_router.py --check
python3 scripts/check_mandatory_docs_links.py
```

纯文字中文化不需要刷新未改变的 API 或数据库快照。生成方法与资源约束见[生成参考](generated/README.md)。不要对生产库运行 schema generator 来完成一份文档 PR。

文档应包含实际入口、输入输出、状态与失败恢复、测试证据链接。架构图表达当前实现；规划中的方案必须明确标识，不与已存在节点混画。概念关系图不标成真实外键 ER 图，概念状态图不冒充完整数据库枚举。

<a id="documentation-design"></a>
### 文档的信息与视觉设计

**结构先于装饰。** README 提供定位、能力和开始路径；文档中心按任务导航；架构页区分不同视图；模块页先展示职责与流程，源码索引放在阅读路径后部。操作指南保留完整前置条件和恢复步骤，不为缩短页面而隐藏关键警告。

| 元素 | 维护方式 |
| :--- | :--- |
| 页首 | 一个清晰标题、简短职责说明、上级导航；长文提供可折叠的本页目录 |
| 模块摘要 | 简述运行位置、输入与主要产物，紧邻真正重要的职责边界 |
| 章节 | 一级主题用二级标题，细节用三级标题；稳定锚点独立于显示编号 |
| 表格 | 用于比较职责、状态、契约；缩短单元格，长解释放回正文 |
| 折叠区域 | 放目录、源码索引和历史参考，不默认折叠关键操作风险 |
| 提示块 | NOTE 解释前提；IMPORTANT 澄清影响理解的边界；WARNING 仅用于真实写入、覆盖或账户风险 |
| 语言与证据 | 中文叙述，命令、错误码和协议名保留原样；示例、设计稿、实测截图分别标注 |

**每张图只回答一个问题。** 部署图画进程和依赖，包图画 import 方向，数据图画产物，时序图画调用顺序；概念状态不冒充完整数据库枚举。箭头语义写入图注，不能统一宣称“虚线就是只读”。调用回复、条件后续和外部边界是不同含义。

图中优先短标签与必要换行，相关流程用 `subgraph` 表达真实边界。信息产品用青绿、研究用靛蓝、执行用橙色、共享运行基础用灰色；**颜色只是职责辅助，必须同时保留文字和形状**，不编码成功率或事实可信度。

Mermaid 源码直接保存在相应 Markdown 中；不再维护第二套手工 SVG 流程图。使用标准 `flowchart`、`sequenceDiagram`、`stateDiagram-v2`，为图添加 `accTitle` / `accDescr`；复杂时序添加编号，正常路径之外的重要失败分支不要省略成无条件成功。

图表的字体和间距使用本图配置，不强制覆盖宿主明暗主题。节点自定义填充时也指定对比清晰的文字颜色；箭头、背景和文字在深色模式下同样检查。避免依赖未经目标 GitHub 渲染器验证的新语法、外部图标或浏览器脚本。

参考：[GitHub Mermaid 支持](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/creating-diagrams)、[Mermaid 配置](https://mermaid.js.org/config/configuration.html)、[可访问标题与说明](https://mermaid.js.org/config/accessibility.html)。实际检查命令和资源范围由[图表验证](TESTING.md#diagrams)维护，不额外引入文档服务或生产依赖。

<a id="section-测试本身也需要可信"></a>
## 07 · 测试本身也需要可信

不通过删断言、无理由 skip / xfail、自动更新快照、重试直到绿或静默移出必需 lane 来掩盖失败。改动测试系统时说明原风险如何继续覆盖，并验证相应 harness，而不是冻结一套永远不能改善的 CI 拆分。

真实模型评估与确定性契约测试是不同证明。固定合成语料上的评审器分数，不证明当前真实新闻质量、人工一致性或交易收益。研究结果必须说明样本、时间、版本和未知。

可以评估有价值的新工具，但不因一个技能模板自动要求创建独立 Issue、读取全部手册或运行所有检测器。工具服务于当前结果，不重新定义授权与交付范围。

<a id="completion"></a>
<a id="section-完成与交付"></a>
## 08 · 完成与交付

一个完整变更包括受影响实现、调用方、测试、文档、必要生成物和旧路径删除。默认形成一个可审阅的 PR；只有独立交付、回滚、分阶段迁移或真实审阅困难时才拆分，并写清依赖与完成条件。

提交说明应回答：**改了什么、为什么、依据哪个版本、实际运行了哪些检查、还不能证明什么**。有治理 Issue 时链接它；没有也不必为了模板补建一套票据层级。

PR 可以在远程 CI 等待时提交，但不能把 pending 说成通过。授权合并前核实当前 HEAD 的必需检查及仓库规则；PR HEAD 的测试不证明后续 squash commit 的部署身份。部署和生产验收是另一个明确边界。

提交 PR 不意味着允许合并、部署、数据库变更、接受模型复核或真实账户操作。保留其他任务的工作树与用户未提交修改；只有明确授权后才清理相应任务资源。

部署和开发入口分离：`make sync` 安装锁定开发依赖，`make dev-serve` / `make dev-workers` / `make dev-analysis` / `make dev-executor` 在前台运行隔离实例；普通 Compose 部署不依赖这些宿主机进程。配置与服务归属见[安装](SETUP.md)及[scripts 工具说明](../scripts/README.md)。

---

[返回文档中心](README.md) · [架构图谱](ARCHITECTURE.md#atlas) · [返回顶部](#开发指南)
