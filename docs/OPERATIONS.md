# 运维与故障定位

[手册](README.md) · [安装](SETUP.md) · [迁移](MIGRATIONS.md) · [News](modules/news.md) · [Execution](modules/execution.md)

先明确**哪个版本、哪个角色、哪个工作身份、哪种副作用**出了问题，再执行恢复。探针成功不能替代消息推进、语义采用、实际发送或账户对账。

本页命令是操作手册，不是自动执行脚本。带写入、副作用或账户操作的命令必须在对应授权范围内运行。

<details>
<summary><strong>本页目录</strong></summary>

1. [先做有界诊断](#section-先做有界诊断)
2. [按边界判断问题](#section-按边界判断问题)
3. [News：先找失败版本，再恢复](#section-news先找失败版本再恢复)
4. [市场与钱包定位](#section-市场与钱包定位)
5. [Trading 与账户操作](#section-trading-与账户操作)
6. [部署与独立 Runtime](#section-部署与独立-runtime)
7. [备份与恢复](#section-备份与恢复)
8. [故障记录应包含什么](#section-故障记录应包含什么)

</details>

<a id="diagnostics"></a>
<a id="section-先做有界诊断"></a>
## 01 · 先做有界诊断

在管理该部署的主检出目录执行：

```bash
make topology
make status
make logs
make config
```

Make 命令统一使用选定的 Compose 文件、项目和 `.env`，并从实际绑定推导探针 URL。下面的高级 `docker compose` 示例必须保持相同上下文；也可以先用 `make workers-shell` 进入选定容器后执行 CLI。端口有显式调整时使用实际绑定。仓库 HEAD、正在运行的镜像 ID 和 Runtime manifest 不一定相同；诊断记录要保留各自身份，不能只报“main 最新版”。

| 检查 | 说明 |
| --- | --- |
| `make status-app` | 应用容器、基础依赖、迁移退出、Serve / Workers 就绪与工作台 |
| `make status` | 同时报告应用与独立 Runtime；未启用时报告 disabled，应用失败也不会吞掉执行状态 |
| `make logs` | 所有服务日志，包含 Analysis、RabbitMQ policy 与 migrate |
| `make runtime-status` / `make runtime-logs` | 独立执行角色，不等于账户已平仓或收益已齐全 |
| `make config` | 脱敏配置，不自动测试全部外部服务 |
| `tracefold db audit` | schema / role / catalog 有界审计，不是全表精确计数 |

```bash
docker compose exec -T workers tracefold db health
docker compose exec -T workers tracefold db audit
docker compose exec -T workers tracefold news bus-policy verify
docker compose exec -T analysis tracefold trading status
```

不要把 `db audit --deep` 或 `db query-audit --analyze` 视作同样轻量：前者进行精确计数，后者真实执行查询。只读不等于没有资源成本。

<a id="section-按边界判断问题"></a>
## 02 · 按边界判断问题

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
<a id="section-news先找失败版本再恢复"></a>
## 03 · News：先找失败版本，再恢复

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

### 已完成工作确认漏范围

先从 Event 详情或 `news why EVENT_ID` 取得当前 wanted/head，再用 `tracefold news reanalyze --event EVENT_ID --wanted WANTED_REVISION --head HEAD_REVISION` 预览 wanted/done、head 和各来源任务 `read_ref` 清单。没有 head 时 `--head none`。核对原文与确切漏读范围后，用清单中的 wanted、head、read 值提交定向处理修订：

```bash
tracefold news reanalyze --event EVENT_ID --wanted WANTED_REVISION \
  --head HEAD_REVISION --read READ_REF --reason '已核实的漏读范围说明' --execute
```

命令使用版本与 head 条件更新；状态已前进、lease 有效或范围不再匹配时返回冲突。它保留原 Event、旧 head、已送账本和来源修订，不授权历史重发。选择名单时记录原文、任务范围与原因；不要对所有旧 Event 盲目执行。

### 历史编号事实的 head 归属清理

`tracefold news repair-head-scopes` 按 `--after` / `--limit` 有界预览当前编号事实 Event 的已采用 head，逐 Event 输出当前 head、修复证明和无法自动判定的引用。执行前保存并验证当前数据库备份，核对目标 Event 的来源原文、head 与证明，再逐 Event 运行：

```bash
docker compose exec -T serve tracefold news repair-head-scopes --limit 50
docker compose exec -T serve tracefold news repair-head-scopes \
  --event EVENT_ID --head HEAD_REVISION --proof PROOF_DIGEST --execute
docker compose exec -T serve tracefold news repair-head-scopes --limit 50
```

每个执行命令只在一个 Event 事务中锁定并重审当前 head 与证明；不匹配或无法判定时拒绝该 Event。修复可与 News 发送进程并行：发送许可先取得时，其旧 intent 仍由旧 head 的回执结算；修复先提交时，旧 intent 不能再取得发送许可。成功后逐页复查越界活跃 Claim，核对 `news_head_scope_repairs` 的证明、`news_event_updates` 的新旧链和 `news_trade_events` 的 `source_update`。已完成的通知工作不重新打开；仍待处理或已 `failed` 的工作改为指向修复后 head（状态、尝试数和错误码不变），失败工作之后按新 head 的 content revision 定向重试。已送回执作为实际外部结果保留。

### 通知计划失败

```bash
# 写操作：指定当前失败的 content revision
docker compose exec -T workers tracefold news retry-work \
  --event EVENT_ID --kind notification --revision CONTENT_REVISION
```

只作用于状态为 `failed` 的通知工作：控制台和 `news why` 显示“通知失败”与 `last_error_code`。三类真实失败会走到这里：规划异常三次（含整个通知阶段超时）、同一未发送 intent 的卡片失败或可重试 `not_sent` 三次、预检证明未发送但不可重试。命令把该版本工作重置为 pending（尝试数归零），并复活同版本中**没有任何发送账本**的失败 intent，冻结卡片按原身份重用；错误码保留到工作完成。它不等于“忽略已发正文再发一次”：已有 `sending` / `sent` / `ambiguous` / `terminal` 账本的 intent 从不重开。

以下都不是失败，不需要重试：明确的 `no_notification`（先看逐命题的 `retired`、`stale_source`、实际正文覆盖或 `editor_feed_only`）；等待本 Event 仍在发送中的命题（`send_outcome_unresolved`，不计尝试，发送结算或孤儿对账后自动继续）；结果不明的命题（`send_outcome_ambiguous`，按可能已送达处理，不重发）；数据库暂时无法应答（不计尝试，推迟一轮后自动再试）。`news_notification_exhausted_legacy` 是 0413 从旧代码耗尽且无原因的工作回填的错误码。

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

<a id="section-市场与钱包定位"></a>
## 04 · 市场与钱包定位

```bash
docker compose exec -T workers tracefold news instruments summary
docker compose exec -T workers tracefold news instruments resolve --symbol SYMBOL
docker compose exec -T workers tracefold news wallets --hours 24 --queue-limit 10
```

目录 `snapshot` 是外部读取并写目录的维护操作，不与 `summary` 混用。OI 问题沿**来源 → 解析 → 类型化事实 → 分组 / intent → relay**检查；报价 / Reaction 的缺失不能解释成 OI 为零。

钱包沿**已发布名单 → 完整回执前缀 → 净买入资格 → episode → 发送时复查 → 实际回执**检查。价格采样另看目标与实际观察时间，不能拿迟到报价补成触发当时的价格。

<a id="5-trading-and-account-operations"></a>
<a id="trading-operations"></a>
<a id="section-trading-与账户操作"></a>
## 05 · Trading 与账户操作

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

### #719 一次性执行基线硬切

切换前保存脱敏 ARK/INJ 取证与可恢复备份，记录部署 SHA、账户槽位、环境及 Binance 当前仓位、普通单、Algo 单和在途请求。暂停新经济指令并按明确现场授权处理真实敞口；PG 的 closed 不证明场所已平。停旧 Runtime、会重投执行输入的生产者及其他 writer，复核场所后升级匹配的镜像和 schema。

审查 [一次性 SQL](../scripts/issue719_execution_hard_cut.sql) 的账户范围和影响行数，在停写、备份可用且场所风险已处置时才运行：

```bash
: "${TRACEFOLD_POSTGRES_DSN:?set the reviewed target PostgreSQL DSN}"
psql -X -d "$TRACEFOLD_POSTGRES_DSN" -v ON_ERROR_STOP=1 \
  -v account_slot=binance_usdm_primary -v expected_connection=DEMO \
  -f scripts/issue719_execution_hard_cut.sql
```

SQL 只删除目标账户旧 Plan、执行观察、手动意图、最终入场核验及派生 Runtime 快照；已发布 Signal 保留为 Case 证据并全数退休，避免删除 Plan/处置后重新可执行。手动意图必须全部过期，脚本才允许删除，以免旧请求重试恢复可执行性。保留现有 pause/halt 与稳定 namespace。旧 RabbitMQ 投递或其他待发送工作必须在停写阶段清理并核对晚到消息；脚本不触及 broker。重启后核对当前外部风险、Cache、Plan 保护、PG 原生结果以及旧 Signal/命令未重放，再验收一笔新生命周期。未完成这些现场回执时不得宣称硬切已完成。

<a id="deployment"></a>
<a id="section-部署与独立-runtime"></a>
## 06 · 部署与独立 Runtime

正常升级使用 `make up`。根 Makefile 只负责公开命令，[scripts/deploy.py](../scripts/deploy.py)持有项目级 OS 锁、验证配置、按顺序迁移并验收；[compose.yaml](../compose.yaml)拥有服务、挂载和关闭预算。迁移非零退出时，应用保持停止，不把 `depends_on` 或容器 running 当作成功。

生产使用审阅后的干净源码；`make verify-main-ci` 可显式核验精确 main push 的发布证据，需要 uv 和已登录的 GitHub CLI。普通部署不再要求宿主机安装项目依赖；诊断、停止及兼容镜像恢复不依赖 GitHub 在线。

```bash
# 已有本地镜像，完整 ID；先确认目标镜像与数据库 head 相同
make deploy-image IMAGE_ID=sha256:FULL_LOCAL_IMAGE_ID
```

精确恢复只适用于能运行当前服务命令、且 schema 相同的镜像，不要求旧镜像等于当前源码 HEAD，不构建、不降级 PostgreSQL、不替换执行进程。配置和 image / database head 校验先于停止应用；随后验证实际镜像与 Workers 身份。不可变 image ID 而不是 tag 才是恢复依据。

`make db-migrate` 是显式维护操作：构建、验证、停止应用写进程并迁移，完成后保持应用停止；再用 `make up` 启动。日常更新直接使用 `make up`，不要自行拼接多个并发部署步骤。

```bash
make runtime-build
make runtime-up RUNTIME_IMAGE=tracefold-runtime:SOURCE_REVISION
make runtime-restart
make runtime-status
make runtime-down
```

执行启动不构建、不迁移、不通过依赖关系重建 PostgreSQL。先验证执行启用状态与 image / database head，再操作账户所有者。`runtime-restart` 复用实际容器的 image ID，关闭预算仍为 90 秒。账户 paused / blocked 是诊断信息，不是自动重启依据。

应用 / 前端发布不能隐式重启账户所有者。schema 变化时按[迁移指南](MIGRATIONS.md)协调 Runtime 和其他写进程；不提供绕过不兼容检查的环境开关。`make down` 先关闭执行，再停止其余服务，保留数据卷。

<a id="6-backup-and-restore"></a>
<a id="backup"></a>
<a id="section-备份与恢复"></a>
## 07 · 备份与恢复

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

<a id="section-故障记录应包含什么"></a>
## 08 · 故障记录应包含什么

记录角色 / 版本、观察时间、工作身份、具名错误、重试或外部结果、做了什么和剩余未知。日志、备份和截图不泄露 token / key / 带密码 URL。调查中的一次性能样本必须附数据规模与测量条件，不能写成长期架构承诺。

能证明修复的是**同一身份下的后续进展与真实结果**，不是删除失败记录、重复启动进程或让状态页暂时变绿。

---

[返回文档中心](README.md) · [架构图谱](ARCHITECTURE.md#atlas) · [返回顶部](#运维与故障定位)
