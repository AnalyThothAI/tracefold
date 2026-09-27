# 安装与配置

[手册](README.md) · [系统架构](ARCHITECTURE.md) · [运维](OPERATIONS.md) · [数据库迁移](MIGRATIONS.md)

推荐使用仓库的 **Make + Docker Compose** 启动完整应用。业务配置只有 `TRACEFOLD_HOME/config.yaml` 一份，默认仍为 `~/.tracefold/config.yaml`；前后端一起构建，Nautilus 执行角色独立管理。

<details>
<summary><strong>本页目录</strong></summary>

1. [前置条件](#section-前置条件)
2. [首次启动](#section-首次启动)
3. [初始化生成什么](#section-初始化生成什么)
4. [按能力配置，而不是一次开启所有功能](#section-按能力配置而不是一次开启所有功能)
5. [网络、端口与挂载](#section-网络端口与挂载)
6. [升级已有安装](#section-升级已有安装)
7. [可选执行生命周期](#section-可选执行生命周期)
8. [本地开发](#section-本地开发)
9. [停止](#section-停止)

</details>

<a id="section-前置条件"></a>
## 01 · 前置条件

| 工具 | 用途 |
| --- | --- |
| Git、GNU Make | 获取审阅后的代码与调用统一命令 |
| 系统 Python 3.10+ | 运行仅依赖标准库的部署编排，不安装应用依赖 |
| Docker 与 Compose v2 | 在镜像内安装锁定依赖、构建前端并运行完整应用 |
| uv / 项目 Python 3.13 | 仅本地开发、验证及显式发布来源检查需要 |
| GitHub CLI | 仅 `make verify-main-ci` 的显式精确 main CI 核验需要 |
| Node / npm | 仅宿主机前端开发需要；部署使用镜像构建阶段 |

部署不要求宿主机安装 uv、npm、curl 或登录 GitHub。应用始终使用镜像中的 Python 3.13；系统 Python 版本不应成为安装整个业务依赖树的理由。

解释器以 [.python-version](../.python-version)为准，Python 依赖以 [uv.lock](../uv.lock)为准，前端以 [package-lock.json](../web/package-lock.json)为准。Docker daemon 必须已启动，当前终端必须能访问它。

macOS 使用具备这些工具的终端；Windows 开发建议在已配置 Docker 访问的 WSL Linux shell 中使用同一套命令。不要把工作树的 `.git` 文件替换成目录，或让 Windows 与 WSL 的绝对 Git 路径相互污染。

<a id="section-首次启动"></a>
## 02 · 首次启动

```bash
git clone https://github.com/AnalyThothAI/tracefold.git
cd tracefold
make init  # 构建镜像、初始化配置，不启动服务
# 编辑 ~/.tracefold/config.yaml
make up
```

访问 **http://127.0.0.1:8765/**。生产使用审阅后的干净源码；`make verify-main-ci` 是显式发布来源核验，不是服务恢复的联网依赖。开发 worktree 必须使用独立项目名、配置目录和宿主机端口，不能共享生产写进程。

```mermaid
---
config:
  fontFamily: "system-ui, Noto Sans CJK SC, Microsoft YaHei, WenQuanYi Zen Hei, sans-serif"
  flowchart:
    curve: linear
    nodeSpacing: 28
    rankSpacing: 42
---
flowchart TD
    accTitle: 镜像内初始化与应用启动
    accDescr: 项目锁保护构建与初始化，先验证配置和运行时 schema 兼容，再启动基础设施，等待迁移成功后启动应用。独立 Nautilus 不由此自动重启。
    Lock["项目级 OS 锁"] --> Build["构建镜像并读取不可变 ID"]
    Build --> Init["镜像内初始化、配置与运行时清单验证"]
    Init --> Window["活动执行进程的 schema 兼容检查"]
    Window --> Infra["启动 PostgreSQL / RabbitMQ"]
    Infra --> Policy["应用 RabbitMQ 策略"]
    Policy --> Migrate["执行一次性迁移并等待结束"]
    Migrate --> Result{"退出码为 0"}
    Result -->|"是"| App["启动 Serve / Workers / Analysis"]
    Result -->|"否"| Stop["保留应用停止状态并报告失败"]
    App --> Ready["验证应用、探针与静态工作台"]

    classDef store fill:#f1f5f9,stroke:#64748b,color:#1e293b,stroke-width:1.5px
class Lock,Build,Init,Window,Infra,Policy,Migrate,Result,App,Stop,Ready store;
```

*操作视图 · 配置由选定镜像校验；迁移成功指实际退出码。此图不包含独立账户执行的启动授权。*

[Makefile](../Makefile)是薄命令入口；[scripts/deploy.py](../scripts/deploy.py)等待迁移进程真正退出，再以 `--no-deps` 启动应用。不能仅因为 `depends_on` 或部分容器 running 就宣布启动成功。再次 `make up` 会构建并更新应用角色，不应该无故重建已有数据库容器。

```bash
make topology
make status-app
make logs
```

**`make status-app` 检查应用栈；`make status` 还会检查可选执行 Runtime。** 未启用 Runtime 时报告 `disabled`；应用检查失败也会继续报告 Runtime。默认禁用的 Analysis 正常等待关闭信号，健康检查不会再将它误判为启动故障。

### 单一职责与配置归属

| 关注点 | 唯一所有者 |
| --- | --- |
| 对外命令 | [Makefile](../Makefile)，无隐式联网和重复端口默认值 |
| 部署、迁移顺序、锁与验收 | [scripts/deploy.py](../scripts/deploy.py)，只依赖系统 Python 标准库 |
| 服务、挂载、端口、关闭预算 | [compose.yaml](../compose.yaml) |
| 业务字段和初始化默认值 | [类型化设置](../tracefold/platform/config/models.py)与[初始化器](../tracefold/platform/config/loader.py) |
| 开发、测试、生成物 | [make/checks.mk](../make/checks.mk)，不参与服务启动 |

所有 Make 生命周期命令使用相同的显式 Compose 文件、项目目录和 `.env`，包括日志、状态、shell 与停止操作。不自动发现其他 Compose override。`make topology` 只输出脱敏拓扑，不打印可能含凭据的完整 Compose 文档。

<a id="section-初始化生成什么"></a>
## 03 · 初始化生成什么

`make up` 在选定镜像内、以宿主操作者 UID/GID 调用 `tracefold init`；也可以在首次启动前执行 `make init`，先查看和编辑配置。

```text
~/.tracefold/
├── config.yaml
├── postgres_password
├── postgres_database_password
├── telegram_bot_token
├── binance_usdm_api_key
├── binance_usdm_api_secret
├── archive/
├── cache/
└── logs/
```

初始化生成本地 API bearer 和数据库密码，不会生成可用的外部新闻、模型或交易所凭据。推送 / 执行文件是待填写的占位文件。目录权限为 `0700`，配置与密钥文件为 `0600`。

已有配置内容和数据库密码保留，必要权限会修正。**`tracefold init --force` 会把 config.yaml 替换为生成默认值，不是升级命令，也不会轮换既有数据库密码。** 不要用它处理一个不认识的字段错误。

默认使用用户目录；可通过 `TRACEFOLD_HOME` 指定其他目录，包括被 Git 忽略的项目 `.tracefold`。不维护第二份手写 `config.example.yaml`，业务配置也不从 `.env` fallback。生成配置与[类型化设置](../tracefold/platform/config/models.py)共同解释当前支持的字段。

```bash
make config
make help
```

`config` 输出脱敏值与路径；不要为了排障直接打印所有原始文件。

<a id="capabilities"></a>
<a id="section-按能力配置而不是一次开启所有功能"></a>
## 04 · 按能力配置，而不是一次开启所有功能

| 能力 | 配置入口 | 初始状态与注意事项 |
| --- | --- | --- |
| News 接收 | `news.enabled`、`news.opennews_token`、`news.broker` | News 默认 enabled，但没有外部 token 不会产生来源数据 |
| 编辑型模型 | `llm.api_key`、`llm.base_url`、`llm.news_triage_model` | 完整一组；字段名保留历史拼写，但当前调用 EventUpdate Agent |
| 中文卡片路由 | `llm.news_reader_card` 及对应 fallback | 可选独立完整 endpoint；未配置时按实际装配复用默认生成能力 |
| News 有界原生判断 | `llm.news_judgment` | 可选完整 `api_key / base_url / model`，不从 Trading 路由推断 |
| 新闻 / 市场推送 | `news.push` | 默认关闭；Feishu / Telegram 各需自身有效目的地与凭据 |
| 钱包净买入 | `news.chain_tape` | 默认关闭；名单、RPC、规则与后续价格能力分开诊断 |
| Trading Analysis | `trading.enabled`、`trading.analysis` | 默认关闭；需要研究模型和有界市场证据 |
| Signal 发布 | `trading.analysis.publish_signals` | 默认 false；有研究决策也可以不发布 |
| 账户执行 | `trading.execution` | 默认关闭；另配连接、凭据、风险并显式管理 Runtime |

以下是**合并到生成配置中的字段示例，不是完整配置文件**；占位值必须替换，未填写前不要期待模型或新闻正常工作：

```yaml
news:
  opennews_token: "<你的新闻源凭据>"
  push:
    enabled: false

llm:
  api_key: "<你的模型端点凭据>"
  base_url: "https://your-model-endpoint.example/v1"
  news_triage_model: "<端点实际提供的模型名称>"

trading:
  enabled: false
```

启用的来源 Strategy 在提供商账户管理，不是本地维护一个过时的 ID 白名单。模型 endpoint 组只填一部分会被拒绝；当前 News fallback 与 ReaderCard fallback 也有明确配置依赖，按 Settings 错误路径修正，不复制旧 YAML。

推送无效可能只使 delivery capability unavailable；模型缺失、行情不可用和钱包尚未形成完整监控窗口分别展示，不能用一条“服务未启动”概括。空数据不是自动注入模拟内容的理由。

<a id="section-网络端口与挂载"></a>
## 05 · 网络、端口与挂载

| 服务 | 默认宿主机绑定 | 容器内含义 |
| --- | --- | --- |
| PostgreSQL | `127.0.0.1:56532` | `postgres:5432` |
| RabbitMQ AMQP | `127.0.0.1:5672` | `rabbitmq:5672` |
| RabbitMQ 管理 | `127.0.0.1:15672` | 管理接口，不是应用 HTTP |
| Serve / 工作台 | `127.0.0.1:8765` | 只读 API 与生产静态资源 |
| Workers 探针 | `127.0.0.1:8766` | Workers 存活、就绪与指标 |
| Nautilus 探针 | `127.0.0.1:8767` | 仅独立 Runtime 运行时可用 |

[compose.yaml](../compose.yaml)是全部绑定默认值的唯一来源；Make 不再复制一套默认值。Analysis 也有自己的容器健康检查，不应凭空假设还有一个公开端口。

生成的 PostgreSQL DSN 和 broker URL 使用容器网络地址，系统不会为宿主机 CLI 自动改写。因此数据库与 broker 诊断应在具备这些地址和挂载的容器内执行：

```bash
docker compose exec -T workers tracefold db audit
docker compose exec -T workers tracefold news bus-policy verify
```

初始数据库的 bootstrap 密码与应用用户密码不同。应用角色使用普通应用登录，以装配能力、事务设置和 `application_name` 区分用途；不向 Serve / Workers / Analysis 挂载 bootstrap 超级用户凭据。

在长期运行的角色中，只有 Nautilus 挂载 Binance 执行密钥；短暂运行的可信初始化器以操作者身份初始化其目录。Analysis 使用公共市场 / 模型适配和连接身份，不应为研究顺手获得账户写凭据。

需要持久化项目、目录或端口时：

```bash
cp .env.example .env
# 修改 COMPOSE_PROJECT_NAME、TRACEFOLD_HOME 和宿主机端口
make topology
```

Compose 读取 `.env`；业务 Settings 不读取它。显式 Make 参数 / shell 变量优先于 `.env`，默认值仍由 Compose 定义。配置和 `.env*` 不进入镜像构建上下文。

改变项目名会选择另一组命名数据卷；改变配置目录不会迁移既有密码。开发实例必须同时隔离项目、目录和全部发布端口。数据库绑定改变可能导致容器重建，须作为运维变更处理；不要用它排查新闻逻辑。

容器内路径仍是 `/root/.tracefold`；只改变宿主机挂载来源。修改 broker 初始化凭据还须同步客户端 broker URL，不等于改端口。

`api.host` / `api.port` 是监听地址；`api.public_url` 是读者可访问的绝对 HTTP(S) 链接基础地址，不能有 query / fragment，不从 `0.0.0.0` 或 loopback 猜出来。对公网开放工作台前阅读[安全边界](SECURITY.md)。

<a id="section-升级已有安装"></a>
## 06 · 升级已有安装

先确认源版本、镜像和数据库 head，保存配置与适用备份，再处理确切的已删除字段。EventUpdate 切换删除了 `news.policy` 与 `llm.news_compiler_reflection`；钱包双窗口的 `news.chain_tape.rules.net_buy_fast_n` 也不再支持。

只删除对应 YAML 路径，不批量清除所有同名 key。普通 `init` 保留旧配置，`init --force` 不是迁移工具。0404 / 0405 的前向切换与协调写进程要求见[迁移指南](MIGRATIONS.md)。

已有 Runtime 运行时，不能在其持有账户的同时随意改变数据库契约。`make up` 不会替你重启执行进程；精确镜像替换的范围见[运维](OPERATIONS.md#deployment)。

`make deploy-image IMAGE_ID=sha256:<完整 ID>` 仅恢复当前命令契约兼容、且 image / database head 相同的本地镜像，不要求旧镜像与当前 Git HEAD 相同，不构建、不降级数据库、不重启执行。`make db-migrate` 是显式维护操作，会停止应用角色并在迁移后保持停止；日常更新直接使用 `make up`。

只读 `tracefold runtime-manifest` 替代历史数据库 genesis 命令，报告不可变镜像和 News 程序清单。未提交的开发构建标记为 `-dirty`，不伪装成已提交源码；生产应使用审阅后的干净来源。

<a id="section-可选执行生命周期"></a>
## 07 · 可选执行生命周期

```bash
make runtime-build
make runtime-status
make runtime-logs
```

`runtime-build` 生成 `tracefold-runtime:<sha>` 镜像。`make runtime-up RUNTIME_IMAGE=<已有镜像>` 不构建、不迁移、不重建 PostgreSQL；先检查启用状态和 image / database head，再操作执行容器。`runtime-restart` 使用实际容器的不可变 image ID，不跟随可变 tag；关闭预算仍为 Compose 中的 90 秒。真正的 `runtime-up`、`runtime-restart`、`runtime-down` 是独立、显式的执行生命周期操作；是否有交易权限取决于实际配置、作用域和账户状态，不取决于是否完成了本安装指南。

没有内置 Paper 模拟器；`trading.execution.binance.environment` 指定原生适配器目标，`LIVE` / `DEMO` / `TESTNET` 也必须配合相应凭据。不要假设未设置环境就一定是测试连接。

<a id="section-本地开发"></a>
## 08 · 本地开发

在[独立 worktree](agents/worktrees.md)安装锁定 Python 依赖：

```bash
make sync  # uv sync --locked
```

前端开发使用有意配置的后端：

```bash
cd web
npm ci
npm run dev
```

需要进程级调试时，先配置**隔离的**数据库 / broker，再在分别管理的终端运行 `make dev-serve`、`make dev-workers`、`make dev-analysis`。不要在生产 Workers 正占有相同数据库时再启动另一个所有者。

开发服务器与生产镜像路径不是同一验证证据。提交前按[开发指南](DEVELOPMENT.md)与[测试指南](TESTING.md)选择检查，不让每个文档改动都启动完整部署。

<a id="section-停止"></a>
## 09 · 停止

```bash
make down
```

先停止 Nautilus，再停止应用与依赖；保留配置和数据卷。不要把 `docker compose down -v` 加入日常升级或排障步骤。进程停止也不表示交易所仓位自动关闭。

保留的离线迁移和历史研究工具及其调用时机见 [scripts 工具归属](../scripts/README.md)。普通启动不会执行批量重标注、归档搬迁、历史研究或全局 CLI 安装。

---

[返回文档中心](README.md) · [架构图谱](ARCHITECTURE.md#atlas) · [返回顶部](#安装与配置)
