# 测试、CI 与验证证据

[手册](README.md) · [开发验证选择](DEVELOPMENT.md#risk-tiered-local-verification) · [生成参考](generated/README.md)

本页描述当前测试实现、所需资源和证据范围，不新增一套审批流程。**测试命令成功、远程必需 CI 通过、部署健康和真实业务正确，是不同结论。**

<a id="fixed-full-ci-implementation"></a>
## 1. 当前固定 CI 分工

[ci.yml](../.github/workflows/ci.yml)对面向 main 的 PR、main push、release 和手动触发使用当前固定计划，没有按路径、draft 或提交文字排除。PR 并发规则会取消同一 PR 的旧运行；取消不是成功证据。

| Job | Make target | 资源 | 主要原生结果 |
| --- | --- | --- | --- |
| `quality-static` | `ci-quality-static` | Python | `junit-quality-static.xml` |
| `python-hermetic` | `ci-python-hermetic` | Python | `junit-python-hermetic.xml` |
| `postgres-behavior` | `ci-postgres-behavior` | PostgreSQL、RabbitMQ | `junit-postgres-behavior.xml`、`junit-migration.xml` |
| `runtime-broker` | `ci-runtime-broker` | PostgreSQL、RabbitMQ、可重启的一次性 broker | `junit-runtime-broker.xml` |
| `deploy-e2e` | `ci-deploy-e2e` | PostgreSQL、Node、Docker / Testcontainers | `junit-deploy-e2e.xml` |
| `frontend` | `ci-frontend` | PostgreSQL、RabbitMQ、Node、Chromium | Python / harness JUnit、Vitest 与 Playwright JSON |

`postgres-behavior` 包含迁移行走与隔离数据库 schema 文档校验；`frontend` 包含外部 codegen、harness 完整性、Vitest、视口交互和 full-stack browser smoke。具体 test selection 和报告名由 [make/checks.mk](../make/checks.mk)拥有，根 [Makefile](../Makefile)提供统一入口，不在手册保留会漂移的测试数量表。

每个 job 检出并验证 `TESTED_SHA`，PR 使用其 HEAD，按锁文件安装需要的依赖并隔离资源。必需结果写入 `artifacts/test-results/`，由 [require_test_reports.py](../scripts/require_test_reports.py)拒绝缺失、空或不通过的结果。

`ci-gate` 需要所有必需 job success。仓库分支规则是远程设置，执行已授权合并时要核实其实际状态；不在文档复制某个会过期的规则名或绕过名单。`make verify-main-ci` 显式调用 [require_main_ci.py](../scripts/require_main_ci.py)验证精确 main push SHA；它属于发布来源核验，不作为诊断、停止或同 schema 镜像恢复的联网依赖。PR head 的绿灯不自动证明合并后的新 SHA。

当前没有必需的覆盖率百分比 gate；`make coverage` 按需测量，必需 lane 不额外承担 tracer 开销。历史运行时间与旧拆分保存在对应 Issue / run，不是当前容量承诺。

<a id="local-lane-implementation"></a>
## 2. 本地入口与证明范围

| 命令 | 主要覆盖 |
| --- | --- |
| `make check-static` | 静态质量、纯生成 / router 漂移、文档链接、编译 |
| `make check` | 静态检查与 hermetic 架构 / 契约选择 |
| `make test` / `make test-fast` | 广泛的隔离 Python 回归，不连接真实 DB / broker |
| `make test-integration` | 真实依赖的行为接缝，排除另行选择的慢 / 定时诊断 |
| `make test-deploy` | 部署与运维生命周期 |
| `make test-e2e` | 运行中服务边界 |
| `make test-golden` | broker 驱动 Workers → PostgreSQL → HTTP |
| `make test-browser-smoke` | 生产后端、静态资源、bootstrap 与 Chromium |
| `make test-visual` | 当前 CI 也选择的视口交互测试 |
| `make test-slow` | 明确的慢进程 / harness 诊断 |
| `make test-scheduled` | 生产时长诊断，不属于必需合并证明 |
| `make test-ci` | 串行执行当前固定分工，需要全部隔离资源与报告 |
| `make coverage` | 按需 Python 覆盖率测量 |

开发中先运行改动相关的 pytest / 前端测试，再按共享影响扩展。不能仅从 `test-fast` 名字推断它等于完整 CI；本地完整 preflight 通过也不授权合并或部署。

## 3. 真实资源必须隔离

PostgreSQL 行为测试按 harness 在迁移基线上克隆隔离数据库；迁移历史测试使用单独空库。显式配置 `TRACEFOLD_TEST_POSTGRES_DSN`，不要把生产 DSN 临时塞进测试变量来绕过资源缺失。

RabbitMQ 测试没有默认 broker。运维主机上的 `127.0.0.1:5672` 可能就是线上部署，必须显式提供 `TRACEFOLD_TEST_AMQP_URL`，管理接口不是默认端口时还要提供 `TRACEFOLD_TEST_RABBITMQ_MANAGEMENT_URL`；需要重启 broker 的测试另需 `TRACEFOLD_TEST_RABBITMQ_CONTAINER`。

以下是**一次性测试资源示例**，先确认名称和端口未被其他任务使用：

```bash
docker run -d --rm --name tracefold-test-rabbitmq \
  -p 127.0.0.1:45672:5672 -p 127.0.0.1:45673:15672 \
  -e RABBITMQ_DEFAULT_USER=tracefold -e RABBITMQ_DEFAULT_PASS=tracefold \
  rabbitmq:4.3.5-management-alpine
export TRACEFOLD_TEST_AMQP_URL=amqp://tracefold:tracefold@127.0.0.1:45672/
export TRACEFOLD_TEST_RABBITMQ_MANAGEMENT_URL=http://127.0.0.1:45673
export TRACEFOLD_TEST_RABBITMQ_CONTAINER=tracefold-test-rabbitmq
```

先等该测试 broker 就绪，再运行相应测试；清理时只删除自己创建的资源。固定本次被测树和资源配置，不让并发任务共享会被迁移、清空或重启的同一个资源。

本地没有声明资源时，某些测试会跳过；必需 CI 与完整 preflight 把缺失必需资源视为失败。跳过不构成该接缝已验证的证明，但不影响继续运行其他纯检查。

## 4. 文档与生成物检查

```bash
python3 scripts/check_mandatory_docs_links.py
python3 scripts/sync_agent_router.py --check
uv run python scripts/regen_cli_help.py --check
```

链接检查包括本地文件、Markdown / 显式锚点、引用式链接和编码路径；它**不证明**远程网址可用、命令实际执行成功、架构语义准确、代码块闭合或图形布局美观。

[文档表面测试](../tests/architecture/test_docs_surface.py)检查共享入口同步与纯 Make 选择；[文档导航测试](../tests/architecture/test_documentation_navigation.py)检查嵌套文档、中文锚点、引用链接、模块入口以及从首页可达的手册。不要加入固定段落文字或文档行数的脆弱断言来代替行为检查。

### Mermaid 与 Markdown

新增或修改图时，先核对节点是否实际存在、箭头的含义及事务边界，再提取 Mermaid 代码块进行解析和实际渲染。查看主要图的中文字体、节点裁切、连线交叉和适合阅读的尺寸；解析通过不等于图已可读。

可以在独立临时工具目录使用 Mermaid CLI，不必为纯文档渲染向生产应用引入 npm 依赖或新文档站。图形工具输出作为检查证据；可编辑的 `.md` 中 Mermaid 仍是文档源。

同时检查代码围栏是否闭合，避免一个旧 bash fence 让后半篇手册都显示成脚本。不要执行文档中标注为写操作、部署或账户操作的示例来“测试 Markdown”。

### 数据库与 HTTP 生成物

OpenAPI 与前端类型由实际 contract / codegen owner 验证。数据库 schema 生成需要**已迁移到正确 head 的隔离数据库**；不设置显式测试 DSN 时生成器可能读取 operator 配置，禁止用生产数据库完成文档更新。

纯中文改写不更改生成 CLI、OpenAPI 或数据库字段；需要刷新时按[生成参考](generated/README.md)运行正确生成器。`make docs-generated` 包含真实数据库 introspection，不是无资源的 Markdown 格式化命令。

## 5. 前端验证分层

`npm run typecheck` 验证类型；`npm run lint` 包含 ESLint 和架构测试；`npm run test:unit` 覆盖纯模型、组件与路由；`npm run build:checked` 验证类型并构建。

Mock API 浏览器场景证明交互逻辑，不证明真实服务 bootstrap、静态资源和数据库接缝；`npm run test:e2e:full-stack` 专门承担实际栈边界。视觉改动需查看加载、空数据、错误、窄屏、导航及控制台，不用一张正常首页截图替代全部验收。

## 6. 修改测试体系与提交结果

改善慢或冗余测试时，从它实际覆盖的风险入手。修改选择、重试、资源或必需 job 时说明覆盖如何保留，并验证受影响 harness；不要把必需行为悄悄搬到可选诊断，也不要用 focus、无理由 skip / xfail 或自动更新快照取得绿色。

提交报告写精确命令、被测版本、通过 / 失败 / 跳过数量和未运行范围。标出远程 CI 当前状态，不把“提交了测试”“开始执行”和“运行通过”混为一谈。临时工具和测试数据不应污染产品依赖、operator 配置或其他 worktree。
