# 运维与故障定位

[手册](README.md) · [安装](SETUP.md) · [迁移](MIGRATIONS.md) · [News](modules/news.md) · [Execution](modules/execution.md)

先明确**哪个版本、哪个角色、哪个工作身份、哪种副作用**出了问题，再执行恢复。探针成功不能替代消息推进、语义采用、实际发送或账户对账。

本页命令是操作手册，不是自动执行脚本。带写入、副作用或账户操作的命令必须在对应授权范围内运行。

<a id="diagnostics"></a>
## 1. 先做有界诊断

在管理该部署的主检出目录执行：

```bash
git rev-parse HEAD
docker compose ps --all
make status-app
docker compose logs --tail=100 workers analysis serve
curl -fsS http://127.0.0.1:8765/readyz
curl -fsS http://127.0.0.1:8766/readyz
```

端口有显式调整时使用实际绑定。仓库 HEAD、正在运行的镜像 ID 和 Runtime manifest 不一定相同；诊断记录要保留各自身份，不能只报“main 最新版”。

| 检查 | 说明 |
| --- | --- |
| `make status-app` | 应用容器、基础依赖、迁移退出、Serve / Workers 就绪与工作台 |
| `make status` | 还包含独立 Runtime；没有运行该可选角色时不能据此断言新闻应用失败 |
| `make logs` | 主应用 / 基础服务日志；Analysis 可单独 `docker compose logs analysis` |
| `make runtime-status` / `make runtime-logs` | 独立执行角色，不等于账户已平仓或收益已齐全 |
| `tracefold config` | 脱敏配置，不自动测试全部外部服务 |
| `tracefold db audit` | schema / role / catalog 有界审计，不是全表精确计数 |

```bash
docker compose exec -T workers tracefold db health
docker compose exec -T workers tracefold db audit
docker compose exec -T workers tracefold news bus-policy verify
docker compose exec -T analysis tracefold trading status
```

不要把 `db audit --deep` 或 `db query-audit --analyze` 视作同样轻量：前者进行精确计数，后者真实执行查询。只读不等于没有资源成本。

## 2. 按边界判断问题

| 现象 | 首先核查 | 不应直接做 |
| --- | --- | --- |
| 工作台空白 / 404 | Serve 镜像内静态资源、URL、bootstrap、浏览器错误 | 重跑 News 模型 |
| 容器运行但 readiness 失败 | schema head、角色所有权、原生控制操作、基础依赖 | 关闭检查强行报告健康 |
| 原始输入停滞 | provider token、接收 / 恢复状态、broker 连接 | 增加通知阈值或模型并发 |
| 输入在增加，语义无进展 | wanted / done、lease、provider breaker、模型配置与具名失败 | 删除 Event 或重放整库 |
| 已有 EventUpdate 但未推送 | 通知计划原因、读者覆盖、intent 与真实结果 | 把知识版本直接改成 sent |
| 钱包没有警报 | 名单、完整前缀、每地址规则排除、episode 与发送复查 | 先加社交模型或另一套排名门槛 |
| 有研究建议但无订单 | publish_status、Signal 作用域、Runtime 处理和账户事实 | 从 UI 强造订单状态 |
| 提示未认领敞口 | 实际 venue 持仓、订单 / 计划身份及对账新鲜度 | 清空计划或为了变绿自动 flatten |

Workers 的基础 / 可选任务监督和资源槽位见[Platform](modules/platform.md)。先判断是 provider 的可恢复错误，还是任务本身 faulted，二者恢复方式不同。

<a id="3-news-identify-the-failed-version-before-retrying"></a>
<a id="news-retry"></a>
## 3. News：先找失败版本，再恢复

```bash
docker compose exec -T workers tracefold news why EVENT_ID
```

结合 Event 详情读取这些身份：**来源修订、wanted / done 输入版本、语义 owner / lease / attempt、当前 content revision、通知工作、intent 与发送账本**。不要只凭 UI 上一个“失败”标签选重试命令。

生成错误区分 `news_generation_output_truncated`、`news_generation_output_empty`、`news_generation_output_schema_invalid`。配置了请求契约具有实质差异的 fallback 时，允许一次替代回答；否则显式失败，不重复同一请求消耗全部预算。引用与配置错误立即失败；provider 限流、超时、服务端或传输错误保留有界恢复。先修正具体输出 / 配置原因，再决定是否精确恢复。

状态接口将可领取、等待调度、有效租约和耗尽失败分别记录为 `semantic_pending`、`semantic_deferred`、`semantic_in_progress`、`semantic_failed_exhausted`，不要把最后一类解释成即将自动运行的积压。

### 语义失败

```bash
# 写操作：替换为诊断返回的精确 Event 与 wanted input revision
docker compose exec -T workers tracefold news retry-work \
  --event EVENT_ID --kind semantic --revision INPUT_REVISION
```

只恢复对应失败工作版本，保留事实、检查点和发送回执。不是更换模型后的全库重跑，也不续期原始来源。最终尝试仍持有有效 lease 时，不能把它当作已经耗尽并手工抢占。

### 通知计划失败

```bash
# 写操作：指定当前失败的 content revision
docker compose exec -T workers tracefold news retry-work \
  --event EVENT_ID --kind notification --revision CONTENT_REVISION
```

这针对失败的通知工作，不等于“忽略已发正文再发一次”。明确的 `no_notification` 不是技术故障：先看逐命题的 retired、stale、coverage、mode 等原因。

### 卡片生成失败

```bash
# 写操作：仅适用于当前版本、尚未发送的失败 intent
docker compose exec -T workers tracefold news retry-work \
  --event EVENT_ID --kind card --revision CONTENT_REVISION --intent INTENT_ID
```

必须精确到 intent。已经进入任何发送账本的意图不能通过此命令重开；包括 terminal 或 ambiguous。卡片失败和 planner 失败预算不同，不为恢复文案顺手重置整个通知工作。

| 发送结果 | 操作原则 |
| --- | --- |
| 已证明 `sent` | 以实际正文作为读者覆盖，不重新发送 |
| 已证明 `not_sent` | 按发送 owner 的错误分类和有限重试规则处理 |
| `ambiguous` / 结果未知 | 先核实外部结果；不能伪造成功，也不盲重试 |

模型、endpoint、提示词或镜像改变不会自动重置失败预算。语义完成、知识采用与通知完成的区别见[News 状态](modules/news.md#state)。

### Broker 与死信

```bash
docker compose exec -T workers tracefold news bus-check
docker compose exec -T workers tracefold news dlq inspect --limit 20
```

`bus-check` 包含拓扑声明，不应称为纯只读 GET。死信检查也经过 broker 消费接缝，先确认目标 broker 与命令行为。`dlq replay` 会重新投递，`dlq purge` 会删除消息；均不能代替 PostgreSQL 中精确工作版本的恢复。

当前 `news.triage` 只负责唤醒语义 Worker；通知不是靠清空某个旧 delivery queue 就能重新开始。

## 4. 市场与钱包定位

```bash
docker compose exec -T workers tracefold news instruments summary
docker compose exec -T workers tracefold news instruments resolve --symbol SYMBOL
docker compose exec -T workers tracefold news wallets --hours 24 --queue-limit 10
```

目录 `snapshot` 是外部读取并写目录的维护操作，不与 `summary` 混用。OI 问题沿**来源 → 解析 → 类型化事实 → 分组 / intent → relay**检查；报价 / Reaction 的缺失不能解释成 OI 为零。

钱包沿**已发布名单 → 完整回执前缀 → 净买入资格 → episode → 发送时复查 → 实际回执**检查。价格采样另看目标与实际观察时间，不能拿迟到报价补成触发当时的价格。

<a id="5-trading-and-account-operations"></a>
<a id="trading-operations"></a>
## 5. Trading 与账户操作

```bash
docker compose exec -T analysis tracefold trading cases --limit 20
docker compose exec -T analysis tracefold trading signals --limit 20
docker compose exec -T analysis tracefold trading gate --limit 20
docker compose exec -T analysis tracefold trading observations --limit 20
docker compose exec -T analysis tracefold trading commands --limit 20
make runtime-status
```

`trading diagnose` 提供有界只读执行诊断；检查实际配置和探针地址，不把 host loopback 自动当作另一个容器。

### 显式本地操作意图

公开 HTTP 没有下单 / 控制 POST。`trading issue` 使用本地 OS 身份与关闭的命令语法，保存意图而不是声称动作已完成。只有在明确授权该操作时才运行。例如暂停新入场：

```bash
# 写操作：一次请求生成一次身份；网络不确定时保留这两个值重试
request_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
requested_at_ns="$(python3 -c 'import time; print(time.time_ns())')"
docker compose exec -T nautilus tracefold trading issue '/pause maintenance' \
  --request-id "$request_id" --requested-at-ns "$requested_at_ns"
```

`/pause` 不平仓；`/flatten account` 是 reduce-only 平仓并暂停的请求，必须继续核实 venue 结果；`/halt` 在本次 Runtime 生命周期内具有粘性，不能假设 `/resume` 可清除。新 account slot 也不天然代表 paused。

### 原生执行历史核验

仅在持有相应凭据的 Runtime 环境，对精确历史 Plan 执行：

```bash
# 签名外部读取：ENVIRONMENT 替换为该连接实际 LIVE / DEMO / TESTNET
docker compose exec -T nautilus tracefold trading verify-execution \
  --entry-id ENTRY_ID --account-slot ACCOUNT_SLOT --environment ENVIRONMENT
```

默认预览；显式 `--apply` 才追加核实证据。核对账户、环境、父子订单与真实成交，不合成数量 / 价格，也不让最后一个平仓腿覆盖整笔退出原因。该操作不是新闻排障的常规步骤。

<a id="deployment"></a>
## 6. 部署与独立 Runtime

正常应用升级使用 `make up`；同 schema 的精确本地镜像替换使用：

```bash
# 部署操作：完整本地 sha256 镜像 ID，先核实与源 / 数据库 head 兼容
make deploy-image IMAGE_ID=sha256:FULL_LOCAL_IMAGE_ID
```

它不构建新镜像、不降级 PostgreSQL、不自动替换执行进程。必须验证应用真正运行的镜像和 Workers 报告的身份，而不是只看命令返回。

`make runtime-build`、`runtime-up`、`runtime-restart`、`runtime-down` 分别负责执行镜像和生命周期。应用 / 前端发布不能隐式重启账户所有者。schema 变化时按[迁移指南](MIGRATIONS.md)协调 Runtime 和其他写进程，不使用环境标志绕过不兼容检查。

<a id="6-backup-and-restore"></a>
<a id="backup"></a>
## 7. 备份与恢复

备份必须配套保存源 SHA、镜像 ID、数据库 head 和操作配置的安全副本。以下命令读取数据库并将 dump 保存到受限目录；不会打印密码：

```bash
umask 077
mkdir -p "$HOME/.tracefold/backups"
backup="$HOME/.tracefold/backups/tracefold-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T postgres sh -eu -c \
  'PGPASSWORD="$(cat /run/secrets/postgres_database_password)" exec pg_dump -U tracefold -d tracefold --format=custom' > "$backup"
docker compose exec -T postgres pg_restore --list < "$backup"
```

`pg_restore --list` 只验证归档可读取，不证明完整可恢复。`make postgres-restore-drill` 使用隔离资源做恢复演练，实际资源要求以 Makefile 和 [restore_drill.py](../tracefold/platform/postgres/restore_drill.py)为准。

恢复前停止或隔离会写入目标数据库的进程，先在隔离环境恢复与验证，再按备份版本的兼容路径升级。旧镜像不能靠改 `alembic_version` 假装兼容新 schema；新代码也不能无条件解释 baseline 以前的数据库。

## 8. 故障记录应包含什么

记录角色 / 版本、观察时间、工作身份、具名错误、重试或外部结果、做了什么和剩余未知。日志、备份和截图不泄露 token / key / 带密码 URL。调查中的一次性能样本必须附数据规模与测量条件，不能写成长期架构承诺。

能证明修复的是**同一身份下的后续进展与真实结果**，不是删除失败记录、重复启动进程或让状态页暂时变绿。
