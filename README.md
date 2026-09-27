# Tracefold

### 从一条消息，到可追溯的研究与执行证据

**新闻增量理解 · 市场观察 · 链上净买入警报 · 交易研究 · 独立执行**

[快速开始](docs/SETUP.md) · [系统架构](docs/ARCHITECTURE.md) · [中文手册](docs/README.md) · [开发指南](docs/DEVELOPMENT.md) · [运维排障](docs/OPERATIONS.md)

---

Tracefold 是一个以证据为中心的市场研究系统。它保存原始消息和链上回执，将新闻中的新增事实、条件变化与更正组织成版本化 **EventUpdate**，独立决定哪些内容值得通知读者，再把符合条件的公开事实交给交易分析。

交易研究由受限的 DSPy Agent 完成；真正的订单、保护单与账户对账由独立的 Nautilus 进程负责。中文 React 工作台只读取已记录的事实、决策和结果，不在浏览器里下单。

> **报道不等于事实已兑现，模型建议不等于交易指令，指令受理不等于成交。**
> Tracefold 保留这些边界，让每一步都能回答：依据是什么、当时知道什么、实际发生了什么。

## 能做什么

| 能力 | 当前实现 | 深入阅读 |
| --- | --- | --- |
| **新闻理解** | 保存来源修订；抽取带引文的命题；识别复述、补充、阶段变化与更正；形成不可变 EventUpdate | [News](docs/modules/news.md) |
| **读者通知** | 按命题比较实际已发送正文；只为选中的内容生成中文卡片；记录真实发送结果 | [通知决策](docs/modules/news.md#notification) |
| **OI 与市场观察** | 确定性解析 OI、清算和大户报告；分组控制通知节奏，不经过编辑型新闻的模型链路 | [OI / 市场观察](docs/modules/oi.md) |
| **行情与事件复盘** | 类型化标的目录、当前报价快照，以及独立的新闻后 1h / 4h 价格反应 | [Market Review](docs/modules/market-review.md) |
| **钱包净买入** | 关注地址名单 → 完整链上回执 → 同窗口净买入 → 首报与当前快照；价格观察独立运行 | [Wallets](docs/modules/wallets.md) |
| **交易研究** | 单一合格标的、冻结证据、有限计划菜单、只读 ReAct 工具，输出 TRADE / NO_TRADE / WATCH | [Trading Analysis](docs/modules/trading.md) |
| **独立执行** | 消费有作用域的 Signal；账户检查、真实下单、保护、成交归属与对账 | [Execution](docs/modules/execution.md) |
| **复核与校准** | ReviewDesk 版本化复核与卡片评审器校准；不冒充自动训练发布闭环 | [Review](docs/modules/review.md) |

## 一张图理解系统

```mermaid
flowchart TB
    Sources["消息来源 / 链上回执"] --> Workers["Workers<br/>接收、理解与市场观察"]
    Workers --> Facts[("PostgreSQL<br/>事实、版本、工作与回执")]
    Workers --> Notify["独立通知链路<br/>计划 → 卡片 → 发送回执"]
    Facts --> Analysis["Analysis<br/>冻结证据与交易研究"]
    Analysis --> Facts
    Facts --> Serve["Serve<br/>只读 API 与中文工作台"]
    Facts --> Execution["可选 Nautilus 进程<br/>执行与对账"]
    Execution <--> Venue["配置指定的 Binance 连接"]
    Execution --> Facts
```

代码只有 **News、Trading 两个业务域**，运行时分为 **Serve、Workers、Analysis、Nautilus 四种角色**。前三者共用应用镜像；Nautilus 使用独立镜像与生命周期。RabbitMQ 承接原始输入和语义工作唤醒，PostgreSQL 保存需要恢复与审计的状态。

详细的进程拓扑、模块依赖、事务边界和跨域时序见[系统架构](docs/ARCHITECTURE.md)。

## 快速开始

### 1. 准备环境

部署宿主机只需要 **Git、GNU Make、Python 3.10+、Docker 与 Compose v2**。系统 Python 只运行标准库部署脚本；应用 Python 3.13、uv 和前端依赖都在镜像中构建。Windows 使用 WSL，macOS 使用 Docker Desktop。

```bash
git clone https://github.com/AnalyThothAI/tracefold.git
cd tracefold
make init   # 构建镜像并初始化文件，不启动服务
# 编辑 ~/.tracefold/config.yaml，配置实际需要的能力
make up
```

启动成功后访问 **http://127.0.0.1:8765/**。`make up` 也会初始化缺失文件，因此无外部凭据的首次启动可以直接运行它。

[Makefile](Makefile)提供命令，[scripts/deploy.py](scripts/deploy.py)负责配置验证、迁移等待、镜像与就绪检查，[compose.yaml](compose.yaml)定义容器、端口和关闭预算。**应用更新不会启动或重启 Nautilus。**

生产发布使用审阅后的干净源码；`make verify-main-ci` 是显式发布来源核验，不再是诊断、停止或兼容镜像恢复的联网前置条件。只有该核验和本地开发需要宿主机 uv / GitHub CLI；普通部署不需要先登录 GitHub。

### 2. 配置实际需要的能力

唯一业务配置是 **`TRACEFOLD_HOME/config.yaml`**，默认仍为 **`~/.tracefold/config.yaml`**，由镜像内的 `tracefold init` 生成。可将 [`.env.example`](.env.example) 复制为 `.env`，持久化 Compose 项目名、配置目录和宿主机端口；它不是第二份业务配置。已有目录、密码文件和命名数据卷不变。

| 初始状态 | 含义 |
| --- | --- |
| 没有新闻源凭据 | 不会产生虚构演示新闻；空列表可能是正常状态 |
| 没有完整模型配置 | 编辑型语义工作不能正常推进；市场确定性解析不是同一条能力 |
| 推送默认关闭 | 看到 EventUpdate 不代表已发送通知 |
| 交易分析、Signal 发布、执行分别控制 | 开启研究不等于允许下单；执行默认关闭 |

```bash
make topology           # 实际项目、配置路径、服务与端口
make config             # 用容器镜像查看脱敏配置
make status             # 同时报告应用和独立执行角色
make logs               # 包含 Analysis 与一次性准备作业
```

完整的配置归属、容器地址、挂载、升级和可选执行操作见[安装与配置](docs/SETUP.md)。已有配置和数据会保留；不要用 `init --force` 或删除数据卷来代替故障诊断。

### 3. 停止服务

```bash
make down
```

这会先停止独立执行进程，再停止其余服务，**不会删除数据卷**。停止进程不等于账户已经平仓。

## 从哪里读源码

```text
tracefold/
├── news/          新闻、市场事实、钱包、通知与复核
├── trading/       交易研究契约、纯策略逻辑与交易存储
├── integrations/  消息、模型周边、行情、链上与执行适配
├── platform/      配置、PostgreSQL、资源和可观测性
└── app/           进程装配、HTTP / CLI、跨业务域映射
web/               React 工作台与前端测试
scripts/           检查、生成、部署与维护工具
tests/             单元、契约、架构与真实依赖测试
notebooks/         离线研究；历史实验不属于在线运行链路
```

新开发者可沿 **[架构](docs/ARCHITECTURE.md) → [模块手册](docs/README.md#modules) → 对应源码与测试** 阅读。每份模块文档解释入口、输入输出、状态、失败恢复与验证点，而不是复制全部函数声明。

```bash
make sync  # uv sync --locked
make check
make test-fast
```

这是开发验证，不是部署授权。修改文档时优先运行链接、生成入口和相关契约检查；数据库、消息队列与浏览器测试需要各自的隔离资源，见[测试指南](docs/TESTING.md)。

## 文档与边界

**文档描述同一版本源码，不证明某个部署已经健康。** 精确 CLI、HTTP 与数据库字段保留在[生成参考](docs/generated/README.md)，不手工翻译机器契约。模型调用数量取决于证据、命题、缓存和通知选择，不固定为“三次”。

旧版三预测器 News Program、GEPA / release / canary 在线流程与进程内 Paper 模拟执行不属于当前能力。历史研究和已应用迁移保留其原始语境；测试通过、研究收益与真实成交是不同证据。

开发入口：[AGENTS.md](AGENTS.md) · [CLAUDE.md](CLAUDE.md) · [贡献与开发](docs/DEVELOPMENT.md) · [安全边界](docs/SECURITY.md)
