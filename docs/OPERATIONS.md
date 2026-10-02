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
6. [部署与 executor](#section-部署与-executor)
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

Make 命令统一使用选定的 Compose 文件、项目和 `.env`，并从实际绑定推导探针 URL。下面的高级 `docker compose` 示例必须保持相同上下文；也可以先用 `make workers-shell` 进入选定容器后执行 CLI。端口有显式调整时使用实际绑定。仓库 HEAD 和正在运行的镜像 ID 不一定相同；诊断记录要保留各自身份，不能只报“main 最新版”。

| 检查 | 说明 |
| --- | --- |
| `make status-app` | 应用容器、基础依赖、迁移退出、Serve / Workers 就绪与工作台 |
| `make status` | 报告应用、Analysis 与 executor；账户状态仍须读取签名场所事实 |
| `make logs` | 所有服务日志，包含 Analysis、RabbitMQ policy 与 migrate |
| `make status` / `make logs` | 查看共享镜像中的 executor 进程；不等于账户已平仓或收益已齐全 |
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
| 有研究建议但无订单 | publish_status、Signal 去向、executor 心跳和账户事实 | 从 UI 强造订单状态 |
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

生成错误区分 `news_generation_output_truncated`、`news_generation_output_empty`、`news_generation_output_schema_invalid`。配置了请求契约具有实质差异的 fallback 时（模型、端点或输出上限不同；抽取 fallback 的输出上限更大），允许一次替代回答；否则显式失败，不重复同一请求消耗全部预算。截断（`finish_reason=length`）即使被 JSON 修复也按截断处理；一个抽取回答里没有任何可用命题时同样先走 fallback，路由上最后一个回答仍不可用才失败。命题逐条校验：可修复的字段就地修复（写错层级的 citations / topics、选项外的读数、不可解析的列表条目、null 可选字段、多余的键），只有缺陈述、引文、主语或动作，或引文不在来源中的命题才丢弃这一条（原因记在观察的 `discarded_claims`），其余照常采纳；所有命题都不可用才算失败。日志 `news_extraction_claim_schema_invalid index=… errors=[(loc, type)]` 给出不可用命题的字段位置与错误类型，`news_extraction_claim_repaired` 给出被修复的字段，二者都不含模型原文。配置错误立即失败；provider 限流、超时、服务端或传输错误保留有界恢复，错误码保留 LM 错误类型（如 `news_generation_lm_timeout_error`）。先修正具体输出 / 配置原因，再决定是否精确恢复。

状态接口将可领取、等待调度、有效租约和已失败分别记录为 `semantic_pending`、`semantic_deferred`、`semantic_in_progress`、`semantic_failed_exhausted`，不要把最后一类解释成即将自动运行的积压：它计入当前仍失败、等待新证据或人工恢复的修订，只要大于 0，模型健康就至少为 warn。失败行保留真实尝试次数，一次性的契约错误显示 `attempts=1`。

### 语义失败

```bash
# 写操作：替换为诊断返回的精确 Event 与 wanted input revision
docker compose exec -T workers tracefold news retry-work \
  --event EVENT_ID --kind semantic --revision INPUT_REVISION
```

只恢复对应失败工作版本，保留事实、检查点和发送回执。不是更换模型后的全库重跑，也不续期原始来源。最终尝试仍持有有效 lease 时，不能把它当作已经耗尽并手工抢占。

以失败结束的修订（含 Janitor 结算的崩溃最终尝试）会把该次尝试实际送入的任务范围记为隔离（`failed_read_refs`，尝试所读范围在领取时记入 `attempt_read_refs`）：之后该 Event 的新成员只读新材料，不再被同一份坏材料拖累。`retry-work --kind semantic` 清空隔离、重新送入全部隔离材料；只想重读其中一段时，用下节的 `news reanalyze` 按精确修订指定该 `read_ref`。构建冻结输入本身失败（来源缺失、重读范围已变、head 无法解码）只让该 Event 的工作失败，错误码可见，不再让语义消费者故障。

领取读取本 Event 输入遇到 statement timeout 或取消时，记 `news_semantic_input_timeout`，照常计尝试、释放租约并退避；三次耗尽进入可见失败。#791 已将跨 Event 召回移出领取读取，排查这个错误应先查来源、head 与数据库语句，而不是提高召回预算。召回和嵌入失败分别记录 `news_claim_recall` 的 `recall_degraded` 与 `news_embedding_*`。

### 命题向量缺失与降级

`/api/news/status` 的 `claim_index_pending` 是最近 30 天及 48 小时内已送精确版本的缺向量或旧身份行数；已送命题原始年龄不限制回填。`recall_dense=on` 要求无活动缺失、配置路由且 Workers 的新鲜心跳报告路由可用。运行中批次失败显示降级，下一次成功恢复。配置独立 `llm.news_embedding` 路由的 `api_key_file`、`base_url`、`model` 和 `max_batch_size`；模型名称和固定 revision、token 上限、pooling、dtype 必须对应校准身份。密钥只由 Workers 从私密文件读取，生成式端点不承担嵌入。模型权重在外部服务部署，应用和 PostgreSQL 镜像不包含权重。启动探针遇到网络、429 或服务暂时故障时隔 30 秒重试；模型、维度或探针不匹配保留可见降级，修复后重启。Janitor 每次有界补算，顺序为已送 48 小时、7 天、30 天；无须重新抽取、开启历史通知或写模型缓存。

独立模型服务使用 [News embedding runtime](../services/news_embedding/README.md) 的 `embedding-build`、`embedding-download`、`embedding-up` 和 `embedding-status` 命令；它们复用项目部署锁，只操作可选模型服务。默认 `make up` 不启动或下载模型。模型、校准身份和运行时固定探针验证完成后，配置 Workers 使用 `http://news-embedding:8080/v1`，默认批次为 2。模型缓存独立挂载，应用升级不把权重打入应用镜像。

升级前备份并停止写者，迁移至 0426 后启动新镜像；新索引行随采用和有界历史投影写入。Janitor 内的命题索引循环独立排空有界批次，有进展时让出执行后继续，空闲或暂时故障时等待 30 秒；保留原维护清理周期。补算不因每分钟只执行一批而持续落后于新增命题，数据库事务结束后才调用模型。回滚先 downgrade 至 0425，恢复旧检索生成列、函数和三元组索引，再启动旧镜像。PR-A 的采用文档与冻结回执形状不变；应用只保留共享命题召回的单一路径。

每日只读回执运行 `uv run --locked python scripts/news_recall_receipts.py --as-of-ms <冻结时刻>`，连接由 `TRACEFOLD_READONLY_DSN` 提供，不放进命令参数。配套 SQL 统计关系对数、有效关系产出率及 prior / receipt 两端降级占比；历史没有诊断的调用保持未知。漏召回代理检查 48 小时内先后已送、超过校准稠密下限、但无两跳链接或实际读者锚点的命题对。缺失向量单列未知数，代理不能证明同一事实。`tracefold_news_reader_changed_total{stage="plan"|"send"}` 记录最终 CAS 冲突次数，不重复计算内部重读。

### 已完成或已失败工作的定向重读

先从 Event 详情或 `news why EVENT_ID` 取得当前 wanted/head，再用 `tracefold news reanalyze --event EVENT_ID --wanted WANTED_REVISION --head HEAD_REVISION` 预览 wanted/done、是否失败及错误码、head 和各来源任务 `read_ref` 清单（`completed` 为已处理，`failed` 为已隔离）。没有 head 时 `--head none`。目标修订必须已完成或已失败。核对原文与确切漏读范围后，用清单中的 wanted、head、read 值提交定向处理修订：

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

每个执行命令只在一个 Event 事务中锁定并重审当前 head 与证明；不匹配或无法判定时拒绝该 Event。修复可与 News 发送进程并行：发送许可先取得时，其旧 intent 仍由旧 head 的回执结算；修复先提交时，旧 intent 不能再取得发送许可。成功后逐页复查越界活跃 Claim，核对 `news_analyses` 中 `origin=scope_repair` 的 repair 证明、document 的新旧链和 `news_trade_events` 的 `source_update`。已完成的通知工作不重新打开；仍待处理或已 `failed` 的工作改为指向修复后 head（状态、尝试数和错误码不变），失败工作之后按新 head 的 content revision 定向重试。已送回执作为实际外部结果保留。

### 断线后的新闻没有被分析

先查恢复窗口对应的 Item：比较 `observed_at_ms` 与 provider `params.ts`。不超过 30 分钟的新鲜补抄应是 `candidate`（上币为 `listing_deterministic`），新证据应有持久 semantic 工作；超过窗口或缺有效时间戳的稿件保持 `recovery`，不唤醒。已有历史 recovery Event 不回补。归组后应检查成员所属 Event 的工作，不能只按 Event 的 `ingest_mode` 推断是否分析。

接收日志里的 `news_ingest_frame_deferred` 表示帧已确认发布、辅助 DB 记账暂缓，后续帧会幂等重试；它本身不应触发进程重启。连接、断开或事故开启写入失败仍可能由根监督重启。验收新一次断线时，分别核对窗口内外的 admission 与 semantic 工作，并检索 warning；实时延迟百分位不包含补抄 Event。

### 通知计划失败

```bash
# 写操作：指定当前失败的 content revision
docker compose exec -T workers tracefold news retry-work \
  --event EVENT_ID --kind notification --revision CONTENT_REVISION
```

只作用于状态为 `failed` 的通知工作：控制台和 `news why` 显示“通知失败”与 `last_error_code`。三类真实失败会走到这里：规划异常三次（含整个通知阶段超时）、同一未发送 intent 的卡片失败或可重试 `not_sent` 三次、预检证明未发送但不可重试。命令把该版本工作重置为 pending（尝试数归零），并复活同版本中**没有任何发送账本**的失败 intent，冻结卡片按原身份重用；错误码保留到工作完成。它不等于“忽略已发正文再发一次”：已有 `sending` / `sent` / `ambiguous` / `terminal` 账本的 intent 从不重开。

以下都不是失败，不需要重试：明确的 `no_notification`（先看逐命题的 `retired`、`stale_source`、`stale_occurrence`、`known_to_reader` 或 `reader_feed`）；读者判断暂不可用而暂缓的命题（`reader_unavailable`，采纳 10 分钟后记为 `reader_unassessed`，不推送）；等待链接命题发送结果的命题（`linked_send_in_flight`）；等待本 Event 仍在发送中的命题（`send_outcome_unresolved`，不计尝试，发送结算或孤儿对账后自动继续）；结果不明的命题（`send_outcome_ambiguous`，按可能已送达处理，不重发）；数据库暂时无法应答（不计尝试，推迟一轮后自动再试）。`news_notification_exhausted_legacy` 是 0413 从旧代码耗尽且无原因的工作回填的错误码。

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

目录 `snapshot` 是外部读取并写目录的维护操作，不与 `summary` 混用。OI 问题沿**来源 → 解析 → 类型化事实 → 分组 / intent → relay**检查；报价的缺失不能解释成 OI 为零。

钱包沿**已发布名单 → 完整回执前缀 → 净买入资格 → episode → 发送时复查 → 实际回执**检查。钱包价格 outcome 已删除；实际发送与链上事实按各自时钟对账。

<a id="5-trading-and-account-operations"></a>
<a id="trading-operations"></a>
<a id="section-trading-与账户操作"></a>
## 05 · Trading 与账户操作

```bash
docker compose exec -T analysis tracefold trading cases --limit 20
docker compose exec -T analysis tracefold trading scoreboard --since 2026-09-01 --until 2026-09-08
docker compose exec -T analysis tracefold trading signals --limit 20
docker compose exec -T analysis tracefold trading fills --limit 20
docker compose exec -T analysis tracefold trading commands --limit 20
make status
```

`trading diagnose` 提供有界只读执行诊断。公开 HTTP 没有下单 / 控制 POST；`trading issue` 使用本地 OS 身份写持久操作意图。旧 `trading gate` 已不再由当前代码写入，不能作为准入依据。

### 显式本地操作意图

```bash
request_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
requested_at_ns="$(python3 -c 'import time; print(time.time_ns())')"
docker compose exec -T executor tracefold trading issue '/pause maintenance' \
  --request-id "$request_id" --requested-at-ns "$requested_at_ns"
```

重试必须保留相同 request ID 和时间。`/pause` 不平仓；`/flatten account` 先暂停入场，再撤普通单、平仓、撤 Algo 单，并以签名场所读回验证。命令受理不等于场所动作完成。

### #764 P4 账本收敛

P4 在 `20261001_0424` 完成账本收敛，当前 head 为 `20261002_0425`。P4 切换前保存应用状态和交易所持仓/挂单，停 Analysis 并在 300 秒内排空 pending，再停 Executor、Workers 和 Serve；完整备份和 14 张旧表导出应记录 sha256。迁移用 13 组校验确认事实与投影一致，启动后核对 pause/halt、订单身份与 70 秒内的账户对账。0425 仅增加语义任务部分索引，无退役表导出；仍按迁移前停写者、成功后启动匹配镜像的顺序执行。具体顺序及回滚见 [迁移手册](MIGRATIONS.md)。进程 UUID 与毫秒心跳属于平台，停止或过期的 executor 心跳不能证明可以发布 Signal；账户的签名对账证据仍属于 Trading。

### #746 Trading 硬切

先停旧执行进程，确认 DEMO 仓位、普通单和 Algo 单均为零，再备份所有 `trading_*` 表及归档目录。迁移 `20260929_0417` 删除旧执行表、建立 Signal v4 与订单/成交账本；`20260929_0418` 删除旧 Analysis Case、Gate、WATCH、逐调用账本并建立冻结预测、六策略和纸面双腿账本。两者不可降级，也不回填旧 DEMO 数据。0418 要求 Signal 表为空；恢复只能使用已验证备份。迁移和新镜像须在同一维护窗口完成。不要把本地 Plan 的 terminal 当作场所平仓回执。保留签名账户检查与备份，直至 DEMO 生命周期回执通过。

<a id="deployment"></a>
<a id="section-部署与-executor"></a>
## 06 · 部署与 executor

正常升级使用 `make up`。`scripts/deploy.py` 持锁验证配置、迁移、启动 Serve、Workers、Analysis 和 executor。未启用交易时 executor 保持空闲。执行启用时，迁移前必须停止执行进程；部署脚本会拒绝带旧 Nautilus 进程的迁移。迁移失败时保持应用停止。

```bash
make status
make logs
make db-migrate
make up
```

`make db-migrate` 是显式维护操作，迁移后应用仍保持停止。`make down` 停止进程并保留数据卷。账户 paused 或 blocked 应读场所与 PG 事实排查，不能仅为清除告警而重启或重置账本。镜像恢复只适用于当前 schema 和服务命令均兼容的镜像。

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


#764 性能验收使用同一 24 小时窗口的前后增量，按表分别记录 `pg_stat_user_tables.n_tup_upd`、`n_tup_hot_upd`、`n_dead_tup` 和大小，以及 `pg_stat_statements` 的调用数、平均耗时与 p95 采样。重点分类 news_events、news_jobs、news_collectors、news_analyses、news_notifications、trading_accounts。有索引字段变化的状态转换不能 HOT；append-only 表不以零更新计算 HOT 比例。两万条历史任务的本地查询预算证明不能代替生产 24 小时观测，早期约 35% 的全库 HOT 比例也不能当作达标结论。
