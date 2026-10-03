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
| `make status-app` / `make status` | 相同检查：四个应用容器、基础依赖、迁移退出、Serve / Workers 就绪与工作台 |
| `make logs` | 所有服务日志，包含 Analysis、RabbitMQ policy 与 migrate |
| `tracefold trading status` | 读取 Analysis / Executor 进程与执行投影；账户状态仍须读取签名场所事实 |
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

生成错误先按输出与配置分开定位：

| 错误 | 核查重点 |
| --- | --- |
| `news_generation_output_truncated` | 输出上限与 `finish_reason`；`length` 即使经 JSON 修复仍算截断 |
| `news_generation_output_empty` | 路由是否返回有效内容 |
| `news_generation_output_schema_invalid` | 回答是否满足当前契约，以及逐命题的校验原因 |
| 配置错误 | 修正配置后再恢复；该错误立即失败 |
| provider 限流、超时、服务端或传输错误 | 按具名 LM 错误和有界恢复规则处理，如 `news_generation_lm_timeout_error` |

fallback 只有在请求契约有实质差异时才允许一次替代回答，例如模型、端点或输出上限不同；抽取 fallback 的输出上限须更大。没有这样的 fallback 就显式失败，不重复同一请求耗尽预算。一个抽取回答没有任何可用命题时也先走允许的 fallback，最后一路仍不可用才失败。

抽取逐条校验命题。可修复字段就地修复，包括写错层级的 citations / topics、选项外读数、不可解析列表条目、null 可选字段和多余键。缺陈述、引文、主语或动作，或引文不在来源中时，丢弃该条并在观察的 `discarded_claims` 记录原因；其他可用命题继续进入后续理解。所有命题均不可用才算失败。

`news_extraction_claim_schema_invalid index=… errors=[(loc, type)]` 给出不可用字段的位置与错误类型，`news_extraction_claim_repaired` 给出已修复字段；二者都不含模型原文。先修正具体输出或配置原因，再决定是否精确恢复。

状态接口将可领取、等待调度、有效租约和已失败分别记录为 `semantic_pending`、`semantic_deferred`、`semantic_in_progress`、`semantic_failed_exhausted`，不要把最后一类解释成即将自动运行的积压：它计入当前仍失败、等待新证据或人工恢复的修订，只要大于 0，模型健康就至少为 warn。失败行保留真实尝试次数，一次性的契约错误显示 `attempts=1`。

### 语义失败

```bash
# 写操作：替换为诊断返回的精确 Event 与 wanted input revision
docker compose exec -T workers tracefold news retry-work \
  --event EVENT_ID --kind semantic --revision INPUT_REVISION
```

只恢复对应失败工作版本，保留事实、检查点和发送回执。不是更换模型后的全库重跑，也不续期原始来源。最终尝试仍持有有效 lease 时，不能把它当作已经耗尽并手工抢占。

以失败结束的修订会隔离本次尝试实际读入的任务范围，Janitor 结算的崩溃最终尝试也适用。领取时记入 `attempt_read_refs`，失败结算时记入 `failed_read_refs`。后续新成员只读新材料，不受旧失败范围牵连。

`retry-work --kind semantic` 清空隔离，重新送入全部隔离材料。只需重读其中一段时，用下节的 `news reanalyze` 按精确修订指定 `read_ref`。构建冻结输入本身失败，例如来源缺失、重读范围已变或 head 无法解码，只让该 Event 的工作失败；错误码可见，不使语义消费者故障。

Worker 先在只读一致快照中构建输入，再按 wanted revision 做短事务 CAS 领取。读取因 statement timeout 或查询取消失败时，记录 `news_semantic_input_timeout`；只有该版本仍可领取，才增加一次尝试、清除领取租约并退避。三次耗尽进入可见失败。若版本已前进或另有有效 owner，本次旧读取不能扣新版本预算。

跨 Event 召回在成功领取并抽取之后进行。排查输入超时应先查来源、head 与数据库语句。召回和嵌入失败分别记录 `news_claim_recall` 的 `recall_degraded` 与 `news_embedding_*`，不能把它们当成输入读取超时。

### 命题向量缺失与降级

`/api/news/status` 的 `claim_index_pending` 是最近 30 天及 48 小时内已送精确版本的缺向量或旧身份行数。`recall_dense=on` 同时要求活动索引完整和 Workers 新鲜心跳报告编码器可用。命题嵌入在 Workers 内使用固定 MiniLM FP32 ONNX；模型权重不进入应用镜像，运行时只读缓存，不发网络请求。

配置 `llm.news_embedding.model` 为校准文件的固定模型名，`cache_dir` 默认 `cache/news-embedding`（相对 operator home），`max_batch_size` 默认 32、上限 32。目录按完整 revision 隔离，准备命令下载 tokenizer 与 ONNX、记录文件摘要并完整发布；加载时核对文件和黄金向量。编码器启用时关闭进程级 tokenizer Rayon 并行，仅由专用执行器与 ONNX 的两个内部线程处理，避免按宿主核数创建线程池。缺文件、损坏、权限失败或自检失败仅使稠密路线降级；修复缓存后重启 Workers。Serve、Analysis 和普通 CLI 不加载权重。

```bash
# 显式联网下载；使用新应用镜像，与 Workers 共享挂载缓存。不会启动业务或访问数据库。
docker compose run --rm --no-deps workers tracefold news embedding prepare
# 离线数值自检，不访问数据库。
docker compose run --rm --no-deps workers tracefold news embedding check
# 写操作：投影已采用事实并补向量，不重抽取、不重判、不发送通知。
# 中断后复用同一 checkpoint 继续；不要把 checkpoint 用于另一数据库或模型。
docker compose run --rm --no-deps workers tracefold news embedding backfill \
  --batch-size 32 --checkpoint /root/.tracefold/cache/claim-index-backfill.json
```

回填按冻结起点和持久游标读取已送精确版本及最近 30 天已采用命题，批次幂等提交后才推进本地 checkpoint，完成后 `ANALYZE news_claim_index`。模型调用和文件 checkpoint 在数据库事务外。一次回填命令会另载一份模型，应计入宿主机总内存；避免并发运行多个全量任务。Janitor 只补索引中直接记录的偶发 pending，不再循环扫描历史分析 JSON。正常采用时复用抽取向量，仅当最终采用文本与模型身份完全匹配时保存；通知优先读取相同精确版本的向量，缺失时才事务外编码。

旧独立嵌入服务的配置切换、容器退休与配对回退见[迁移指南](MIGRATIONS.md#local-embedding-upgrade)。

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

命令只作用于精确 content revision 下状态为 `failed` 的通知工作。控制台和 `news why` 显示“通知失败”及 `last_error_code`。以下真实失败会使工作进入该状态：

| 失败位置 | 计入哪份预算 |
| --- | --- |
| 规划异常，包括通知准备阶段的期限耗尽 | notify work；第三次失败终结 |
| 卡片失败或可重试的确定 `not_sent` | 同一 intent；第三次失败终结 |
| 预检已证明未发送，且错误不可重试 | 未发送 intent 直接终结 |

恢复将该版本工作重置为 pending，尝试数归零，并复活同版本中已证明未送出的 `dead` intent。冻结卡片按原身份重用，错误码保留到工作完成。`dead` 不要求空白账本：可重试的 provider `not_sent` 结果可以已保存在 `settlement`，该记录证明未送出。

`sending` / `sent` / `ambiguous` / `terminal` 的 intent 从不通过此命令重开。不可重试的 provider `not_sent` 结算为 terminal，按其真实结果处理；不能将它与未发送的预检失败混为一类。

以下情况不会进入上述失败恢复。先读原因，再等待或核查对应边界：

| 原因或状态 | 处理方式 |
| --- | --- |
| `no_notification` | 读取逐命题 `retired`、`stale_source`、`stale_occurrence`、`known_to_reader` 或 `reader_feed`；这是明确不通知 |
| `reader_unavailable` | 暂缓；采用 10 分钟后仍不可得记 `reader_unassessed`，不按猜测推送 |
| `linked_send_in_flight` | 等待链接命题的发送结果 |
| `send_outcome_unresolved` | 等待本 Event 的在途发送，结算或孤儿对账后自动继续 |
| `send_outcome_ambiguous` | 按可能已送达处理，不重发；其他命题仍可继续 |
| 数据库暂时无法应答 | 不计业务尝试，推迟一轮后自动再试 |

等待在途发送不消耗失败预算。孤儿清扫以实际 lease 到期为条件，排除本进程仍持有的发送，并按候选 lease token 与 attempted time 做 CAS；不能仅凭经过固定秒数手工终结。`news_notification_exhausted_legacy` 是 0413 从旧代码耗尽且无原因的工作回填的错误码。

| 发送结果 | 操作原则 |
| --- | --- |
| 已证明 `sent` | 以实际正文作为读者覆盖，不重新发送 |
| 已证明 `not_sent` | 按发送 owner 的错误分类和有限重试规则处理 |
| `ambiguous` / 结果未知 | 先核实外部结果；不能伪造成功，也不盲重试 |

模型、endpoint、提示词或镜像改变不会自动重置失败预算。语义完成、知识采用与通知完成的区别见[News 状态](modules/news.md#state)。

<a id="news-reader-switch"></a>
### 读者判断切换与回滚

#805 改变运行时问题和计划结构，不改变数据库 schema，通知来源仍为 `reader_v2`。
native 推送已由[认证批 1](reports/news-805-certification.md)认证（切线 0.372，`key_cut` 为空，没有重点）；generated
回退后端未认证，回答只进信息流。owner 发布审阅已写入校准文件（`release_ready=true`，`review_ref` 指向 #805 的审阅评论）：
审阅记录绑定证书、报告摘要、生产组合判断器身份、外部证据和未通过门槛的豁免。重新认证时用导出桥接加 `--review`
重新生成文件并以 PR 提交；`release_ready=false` 的文件会让普通模型判断全部只进信息流，等于暂停模型推送。
校准文件、模型身份、报告和部署镜像必须是同一份已审阅结果。以下步骤在获得部署授权后执行，回滚时也执行一次。

1. 保存当前镜像身份、回滚镜像、校准文件摘要与数据库备份。按既有发布流程准备目标镜像，先不要启动新 Workers。
2. 先核对 pending intent 的失败次数和 provider 结果。有重试/发送历史的 pending 必须让旧版本按原预算完成有界重试或结算，不能清空计数后交给新版本再试。
   然后在部署目录停止旧 Workers：`docker compose stop workers`。核对进程已退出；正常停机会释放未发送 lease，但不会清除冻结卡片。
3. 通过数据库容器读取 update intent 状态；`sending` 必须为零。非零时先核实 provider 结果并完成正常结算/孤儿对账，不能用维护 SQL 将其改成未发送。

```bash
docker compose exec -T postgres sh -eu -c \
  'exec psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT state,count(*) FROM news_notifications WHERE kind='update' GROUP BY state ORDER BY state;
SELECT count(*) AS pending_with_attempt_history FROM news_notifications
WHERE kind='update' AND state='pending'
  AND (attempts > 0 OR attempted_at_ms IS NOT NULL OR settlement IS NOT NULL OR error_code IS NOT NULL);
SELECT count(*) AS active_pending_leases FROM news_notifications
WHERE kind='update' AND state='pending' AND lease_token IS NOT NULL
  AND lease_until_ms > floor(extract(epoch FROM transaction_timestamp()) * 1000)::bigint;
SQL
```

4. 使用本次已审阅 checkout 中的 [切换 SQL](../scripts/news_reader_switch.sql)清除未发送 pending reservation、卡片和卡片缓存身份，立即唤醒对应 notify job。
   SQL 在短事务中再次检查 `sending=0`、没有有效 pending lease 或 pending 重试/发送历史、工作可以重新规划；存在冲突则整批回滚。
   `pending` 的 lease 仍有效时等待租约到期后复查。有尝试历史时暂不切换，恢复旧 Workers 让原意图按原预算收敛，再重复停机检查。
   工作处于 failed 或缺失时先诊断，不能通过该脚本重置失败预算。

```bash
docker compose exec -T postgres sh -eu -c \
  'exec psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < scripts/news_reader_switch.sql
```

保留输出的 `cleared_pending_reservations` 与 `due_notify_jobs`。该 SQL 不改采用知识、冻结判断与原计划，
不删历史行，不改 sent/ambiguous/terminal 回执；清除的是未发送的预留，因此下次必须重新规划和生成卡片。
重复执行只处理仍存在的 pending 行。不要用 `retry-work` 代替此步骤，该命令会复用原冻结卡片。

5. 按既有发布流程切换共享应用镜像，更新 Serve 后最后启动 Workers。核对新的判断器/策略身份、配置摘要、Workers readiness 和通知工作推进；判断缓存随判断器身份自然失效。
   新计划里 native 判断的 `certification_status` 应为 `certified`、`push_cut` 为 0.372、`key_cut` 为空；若为 `uncalibrated`，
   核对规划器组合判断器身份、作答适配器和 served model 是否与校准文件一致，不能改文件绕过。
6. 检查清理的 Event 产生新计划及新卡片，已送与 ambiguous 回执保持原样。上线后 24 小时按 #805 逐条复核；日量偏离需 owner 取舍，不自动移动切线（工作日估计约 393 张卡/天，命题加权）。

回滚重复步骤 2–4，再切回已保存的旧镜像，最后启动旧 Workers。切换后生成的新计划保留为不可执行的历史记录，
不能尝试让旧代码执行它们。包括新版本生成的 `dead` 意图：不得在旧镜像运行 `retry-work` 复活并继承其冻结卡片；
恢复这类工作须使用原产出版本或单独审阅的恢复步骤。旧界面的历史展示能力按旧镜像记录，完整新证据可由新版本只读查看。
回滚不重发已经送达或可能送达的消息，不需要删除数据库事实或手动清理判断缓存。

[隔离 PostgreSQL 回归](../tests/integration/test_news_reader_switch.py)验证清除旧卡片、重新生成发送、重复清理、
拒绝 sending/有效 lease/已有尝试、保留失败预算和已送账本。它证明切换接缝，不代替真实部署授权与上线审计。

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

### 研究与执行状态

Analysis 的冻结预测、六策略与双腿标签写入 Trading 账本；发布与执行各有资格检查。通过 `trading status` 区分 decision 和 execution，结合 Signal / entry 的处置原因确认阻塞位置。平台进程 UUID 与心跳只能证明进程新鲜度，不能替代账户签名对账；过期 Executor 心跳不允许发布 Signal。版本切换统一见[迁移指南](MIGRATIONS.md)，不在本页复制历史升级流水。

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
