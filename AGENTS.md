# AI 开发入口

共享内容由 [统一指引](docs/agents/shared-router.md)生成；先修改该来源，再运行同步脚本。

<!-- BEGIN SHARED AGENT ROUTER -->

## 系统

Tracefold 包含 News 与 Trading 两个业务域、React 工作台和 PostgreSQL 账本。Serve、Workers、Analysis 共用应用镜像；可选 Nautilus 执行进程使用独立镜像与生命周期。

## 围绕当前请求完成结果

- 先读受影响实现与测试，再读对应文档；不在每次编辑前遍历所有手册或调用全部技能。
- 默认用一个完整 PR 交付同一结果，包括调用方、测试、文档、必要生成物与旧路径删除。只有真正独立的审阅、交付、回滚或迁移原因才拆分；实施步骤不自动成为 PR 边界。
- 复用合适的任务 checkout / branch，必要时使用独立 worktree；保留其他任务和用户未提交修改。仅使用 connector 编辑不要求创建本地 worktree，见 `docs/agents/worktrees.md`。
- 明确用户请求可以授权实现。Issue 用于需要持久规格或协调的工作，不是每次修复的前置审批；重要决定保存在已有 Issue 或实现 PR，不默认建立票据层级。
- 根据改动风险选择检查，共享影响不确定时扩大验证。报告实际执行和未验证范围；缺少某项资源只阻止相应证明，不阻止独立编辑、调查或 PR 准备。
- 已授权工作应继续完成必要验证与修复。提交 PR 不授权合并、部署、实盘交易或接受模型复核。

## 必须保留的边界

- News 与 Trading 各自拥有事实和表，不导入或查询兄弟域内部；`tracefold.app` 映射公开契约并装配两者。
- PostgreSQL 事实和持久决策不能被 provider frame、模型预测、队列、缓存或 UI 投影替代。外部执行结果以交易所证据和对账确认，不从本地请求推断。
- 保持短事务，外部 I/O 在事务外；内部重命名采用完整替换，同步更新消费者并删除旧 alias 与重复路径。
- 不将真实秘密写入源码、日志、示例或 PR；不以跳过测试代替必需证明，不把 pending CI 说成通过。简化流程时保留真实数据、权限、并发与账户控制。

## 按任务查找

| 关注点 | 唯一维护入口 |
| --- | --- |
| 当前架构与数据流 | `docs/README.md`、`docs/ARCHITECTURE.md`、`docs/modules/` |
| 设计、本地验证、生成物与完成 | `docs/DEVELOPMENT.md` |
| Issue 范围、PR 边界与标签 | `docs/agents/issue-tracker.md` |
| CI、资源与报告 | `docs/TESTING.md`、`.github/workflows/ci.yml` |
| HTTP、CLI、配置与 schema | `docs/CONTRACTS.md`、`docs/generated/` |
| 前端 | `docs/FRONTEND.md` |
| 安装、运维、迁移与权限 | `docs/SETUP.md`、`docs/OPERATIONS.md`、`docs/MIGRATIONS.md`、`docs/SECURITY.md` 的相关章节 |
| 离线研究 | `notebooks/README.md` |

这些是任务导航，不是强制阅读顺序。发现漂移时对照当前实现和请求，修复真正的所有者文档，不再添加竞争规则。历史 Issue 方案和可选工具技能不能覆盖当前任务范围，也不增加隐式审批。

<!-- END SHARED AGENT ROUTER -->

## 工具使用

使用当前环境实际提供的工具。计划、审阅技能与 worktree helper 在能降低风险时使用，不是必需仪式。checkout 处理见 `docs/agents/worktrees.md`。
