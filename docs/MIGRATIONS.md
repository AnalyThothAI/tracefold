# 数据库迁移与恢复边界

[手册](README.md) · [运维备份](OPERATIONS.md#backup) · [结构参考](generated/db-schema.md)

当前 schema 使用 [Alembic 单链](../tracefold/platform/postgres/alembic/versions/)，基线为 `20260831_0340`，本版本 head 为 `20261003_0430`。当前账本共 28 张表：19 张 News、7 张 Trading，以及 `runtime_processes`、`alembic_version`。已应用的迁移是升级与恢复证据，文档清理不删除或重写这些文件。

本页只维护版本兼容、维护顺序、必要导出和回退边界。具体业务行为由模块手册维护，部署回执与历史性能结果从对应 Git / Issue 记录检索。

## 确认源、镜像与数据库版本

读取检出源码的 head，不访问数据库：

```bash
uv run --locked python -c 'from tracefold.platform.postgres.migrations import latest_migration_version; print(latest_migration_version())'
```

在管理该部署的同一 Compose 上下文中读取实际数据库状态：

```bash
docker compose exec -T workers tracefold db audit
```

保存源 SHA、不可变镜像 ID、数据库 head、私密配置副本和完整 custom-format dump 的身份。记录 sha256 并核验 `pg_restore --list`；可读取不等于完整恢复成功，隔离恢复演练见[运维](OPERATIONS.md#backup)。不要手工修改 `alembic_version` 或 stamp 来越过不兼容结构。

## 正常升级顺序

1. 核实源 head 与目标镜像 head，阅读跨越的 revision 和本页对应特殊前置条件。
2. 核对实际账户持仓、普通单、Algo 单和进行中的 flatten；停止 Executor 会暂停保护维护、time exit 和操作意图消费。
3. 协调受影响写进程：先停 Analysis，再按源版本要求排空 pending，之后停 Executor、Workers 与 Serve。确认没有残留写者会话。
4. 保存并核验配对备份和必要源表导出，校验新配置中 Settings 报出的确切退役字段；不用 `init --force` 重置配置。
5. 通过 `make db-migrate` 执行显式维护迁移。它保持应用停止；`make up` 在迁移成功后启动四个匹配镜像的应用角色。
6. 核对实际数据库 head、迁移日志、`db audit`、News head / 待处理工作 / 冻结通知，以及 Trading pause/halt、pending、订单身份和真实账户对账。

部署器会拒绝仍有旧 Nautilus 容器的迁移，以及已启用 Executor 运行时跨 schema 更新。迁移失败时保持写进程停止，确认事务回滚后的真实 head 再恢复配对镜像。不要以容器 running 代替一次性迁移的退出码；部署器会比较迁移镜像 head 与实际数据库 head，并输出迁移日志后才启动角色。

## 当前召回与索引迁移：0425–0429

`20261002_0425` 从 `0424` 升级，增加两个语义任务部分索引与 News 专用的单行 reader clock 围栏：按 next_attempt / subject 排序读取 pending/failed 任务，按 updated_at 查近期失败。失败索引也包含已完成但保留失败 outcome 的任务；查询不以 state='failed' 替代原失败统计。迁移不改事实、detail、租约或重试预算，新增计数表与相关事实的 AFTER constraint 触发器（默认延迟到提交，防止 admission 的 Item/member → Event 路径先持有 clock；事实与版本原子可见），News 18 表、全库 27 表；计数是权限 CAS 证明，不替代事实。索引 SHARE 锁、触发器 SHARE ROW EXCLUSIVE 锁、5 秒锁超时和 120 秒语句超时，失败时事务回滚。按正常迁移顺序停写者，保留配套完整备份；新 head 成功后使用匹配镜像，失败时确认仍在 0424 才恢复前驱镜像。无需重复 P3 的表重写或 retired-table 导出。旧镜像不拥有 reader clock，成功后仅启动配套镜像。

[索引迁移测试](../tests/integration/test_semantic_read_index_migration.py)验证 0424→0425 且任务事实完全不变；[有界读取测试](../tests/integration/test_news_semantic_read_costs.py)用两万条已完成历史任务验证查询预算与已完成失败计数。feed 页面沿 Event 的 analysis 指针读取 head，并按 Event 查找待发送意图；全量 counts 保留批量读取，输出契约不变。

`20261002_0426`增加共享命题召回索引、精确版本向量身份与 canonical FTS 投影；`20261002_0427`把持久结构化读数 `commentary` 转为 `unknown`、`conditional_threat` 转为 `threat`，最终只接受当前 v5 mode 与 reader v3 契约。转换覆盖采用文档、冻结回执、检查点和待发公开 payload，保留来源引文、claim/update 引用、输入出处、已送正文与结果；公开 payload 的摘要随转换更新，不重新采用或发送。0427 在事务内暂时关闭两项不可变 guard，排空延迟约束后恢复，失败会整体回滚。

0426→0427 与配套镜像须在同一个停写维护窗口完成：先按正常顺序停止所有写者，核实没有写者会话；保存完整 custom-format `pg_dump`、sha256、`pg_restore --list` 核验结果、源 head 和旧镜像身份。迁移成功后核对实际 head 为 0427，并只启动匹配镜像。0427 是前向转换，回退必须恢复已核验的迁移前完整备份和对应旧镜像；不得仅 downgrade 0426/0425 或让旧镜像读取已转换的事实。[带数据迁移测试](../tests/integration/test_news_speech_migration.py)验证来源、引用、回执及 guard 的保留；业务证据见 [B 报告](reports/news-791-b.md)。

`20261002_0428` 从 0427 升级，将既有 `news_analyses_adopted`、`news_notifications_sent` 两条部分索引分别补齐为 `(adopted_at_ms,analysis_id)` 与 `(settled_at_ms,intent_id)`，保留索引名和计数查询使用的时间前缀，删除被替换的单列定义。历史回填先按稳定源文档游标取有界页面，再展开 claim，避免每个页面重复排序整个历史 JSON。已有事实、向量与 guard 不变；避免单列、复合索引并存导致分页重复排序同一时间组；模型缓存准备与向量回填由独立命令完成，不进入 migration。

0428 采用正常停写维护窗口、5 秒锁超时与 600 秒语句超时，索引创建失败整笔回滚。升级后只启动匹配 0428 镜像。需要恢复配对的 0427 镜像时，停止写者后用 0428 镜像运行 `alembic downgrade 20261002_0427`，核对 head，再恢复旧配置、匹配旧镜像及独立模型服务；该降级只恢复这两条索引的原单列定义。不得跨越 0427 的前向读数转换。验证见[游标迁移测试](../tests/integration/test_news_claim_backfill_migration.py)。


<a id="local-embedding-upgrade"></a>
`20261002_0429` 从 0428 增加 `news_events_story_window(storyline_key,opened_at_ms,event_id)` B-tree 和仅 `kind='update' AND state='sent'` 行的 `news_notifications_sent_claims` JSONB GIN，支持详情页 48 小时故事线与旧已知事实的回执查询。卡片、事实、计划、权限与触发器不变。按正常停写维护窗口执行；按 Event、Notification 顺序取得 SHARE 锁，5 秒锁超时与 600 秒语句超时，创建失败事务回滚。降级到 0428 仅删除这两个索引，再恢复匹配旧镜像；不跨越 0427 前向转换。升级／降级与真实送达事实保留由[读者索引迁移测试](../tests/integration/test_event_reader_index_migration.py)验证。

### 旧独立嵌入服务切换为本地 ONNX

**从 0427 的独立嵌入服务升级：** 0428，仅将现有两条时间索引补齐稳定 ID，作为历史回填游标索引，不重写事实或已有向量。先保存完整备份、配对的旧镜像 ID、私密配置和原模型文件。另备新格式私密配置副本，先删除 embedding 的旧端点和密钥字段并设置本地缓存，再为准备命令指定该新 operator 目录（`TRACEFOLD_HOME`），用新镜像准备并检查缓存；新命令会拒绝旧配置字段，不能先用旧配置运行 prepare。旧应用继续使用原配置。在停写维护窗口切换新配置并按[迁移手册](MIGRATIONS.md)升级至 0428；启动匹配的新应用后核对 dense 状态、pending 和真实通知处理。删除 Compose 定义不会自动停掉旧容器：使用 `docker ps -a --filter label=com.docker.compose.project=YOUR_PROJECT --filter label=com.docker.compose.service=news-embedding` 核对所属项目和容器 ID，再执行 `docker stop VERIFIED_CONTAINER_ID`。回滚窗口内保留旧镜像、权重、密钥和旧配置；不删除卷。

回滚到配对的 0427 版本时，先按正常顺序停止所有写者，用当前镜像执行 Alembic 的 0428→0427 降级并核实数据库 head；这一步只将两条索引恢复为原单列定义，不改变采用文档、回执或向量。恢复配对旧配置，再用旧 checkout 启动其匹配镜像及独立模型服务。`make deploy-image` 要求 image/database head 相同，不能直接把 0427 镜像接到 0428。若回滚到 0427 之前，仍须恢复核验过的迁移前备份及匹配镜像。


<details>
<summary><strong>0420–0424 账本收敛：旧库升级前置条件</strong></summary>

`20261001_0420` 从 `0419` 升级：压缩连续同成员钱包版本，保留每组最后一个版本 ID 与最早 `known_at_ms`，重映射成交和 tape 游标；冻结回执 `sent_claims`，收紧决策和 EventUpdate 的 NULL 检查；删除四张无读者表、两个执行消费游标、六个 delivery 删除字段、两列历史状态及六个孤儿函数。全部处于同一事务，成员、成交数、游标引用和 Claim 投影核对失败即回滚。

此迁移先停全部进程，Analysis 先于 Executor；备份并导出 `news_market_wallet_archive`、`news_market_instrument_listing_events`、`news_market_wallet_roster`、`news_market_wallet_tape_state`、`news_market_wallet_fills`、`news_deliveries`、`news_notification_decisions`，记录 sha256 并核验归档可读。存在 Trading 写者会话、非空冲突/证据表、delivery 删除状态、无效文档/决策或版本内时间不一致时拒绝升级。停 Executor 会暂停 time exit 和 flatten，停前核对实际持仓、挂单及未处置输入；恢复后核对 pause/halt、待处置数和 `db audit`。仅支持已核验备份加旧镜像恢复。

本地行为证明由 [P0 迁移测试](../tests/integration/test_p0_migration.py)、[待处置消费测试](../tests/integration/test_p0_executor_pending.py) 和 [状态缓存测试](../tests/test_measured_once.py)维护。

`20261001_0421` 从 `0420` 升级：市场 Item 与三类事实并入 `news_market_observations`，保留观测 ID、分组键、通知标记和 OI outbox 身份；钱包快照转换为 `news_market_wallets` 成员区间；四个采集器状态与事故归 `news_collectors`。编辑 `news_items` 删除七个市场列，反应与钱包 outcome 表和字段删除。迁移内冻结旧投影并双向 `EXCEPT ALL`，核对事实数、历史成员、当前监控值、链游标与未完成事故。

P1 停 Serve、Workers、Analysis 后执行。备份外另导出 `news_oi_signals`、`news_market_liquidations`、`news_market_smart_money`、`news_market_wallet_roster`、`news_market_wallet_tape_state`、`news_market_instrument_snapshot_state`、`news_opennews_incidents`、`news_ingest_state`、`news_event_reactions`、`news_market_wallet_outcomes`，以及 `news_items` 和 `news_market_wallet_events`；记录 sha256 并验证 `pg_restore -l` 可读。市场行成为编辑证据、来源事实不匹配、语义常量或派生身份不一致时拒绝升级。backlog、未开始卡片、游标与待恢复事故保留；残留 `sending` 由启动扫描标记 `unknown`。恢复后核对 kind / notify_state 分布、当前名单与 `scanned_block`；回滚使用已验证备份和旧镜像。

[P1 迁移测试](../tests/integration/test_p1_migration.py)用 P0 固定市场 JSON 证明分组、详情、时间线不变；[采集器与重放测试](../tests/integration/test_p1_market_collectors.py)覆盖 `xmin`、outbox、成员区间与并发事故。生产导出、恢复彩排、部署后性能与三天写入观测属于上线验收，尚未由这些本地测试证明。

`20261001_0422` 从 `0421` 升级：通知判断、待发送意图、回执与市场卡片统一到 `news_notifications`；通知规划和市场组节奏统一到 `news_jobs`。编辑通知保留 decision / intent / update 身份，原行执行 pending → sending → settled；可重试 not_sent 保留冻结卡片，ambiguous 不自动重发。无 FK 的孤儿事件任务由 janitor 有界清理。

P2 先停 Workers，再停 Serve，核实没有 News 写者会话。完整备份外导出 `news_notification_decisions`、`news_delivery_queue`、`news_deliveries`、`news_notification_work`、`news_market_tracks`、`news_market_deliveries`、`news_notification_feedback`、`news_notification_external_feedback`、`news_external_miss_snapshots`，记录 sha256 并验证归档可读。ReviewDesk 非空、单判断对应多个意图、queue / ledger 冻结字段不一致或 card 的正文与摘要不一致时拒绝升级；六组源投影逐列双向 EXCEPT ALL 与 md5 一致后才删除旧表、视图和函数。日志须包含 `p2_verify ok`。

迁移保留 sending、编辑状态、租约期限与重试预算。恢复时编辑通知 sending 按现有恢复路径结算为 ambiguous，市场 sending 扫描为 unknown；编辑超时也按现有路径结算，禁止将这些记录批量改为 pending。核对详情（删除 feedback）、状态（删除四个复核字段）、市场分组与外部消息身份。回滚恢复已核验备份及旧镜像；备份之后已发生的外部发送仍须逐 intent 对账，不能从数据库恢复推断未发送。

[P2 迁移测试](../tests/integration/test_p2_migration.py)覆盖空库、全部通知与市场状态、冻结载荷、终态不可变、投影不一致拒绝及并发孤儿清理；[发送恢复测试](../tests/integration/test_news_update_delivery.py)覆盖 lease、发送不确定性与崩溃窗口。

`20261001_0423` 从 `0422` 升级：观察与采纳、scope repair 统一到 `news_analyses`，Event 保存唯一 head 指针；证据保存 material 摘要、焦点来源、事实范围及版本摘要日志，来源修订归 `news_items.revisions`，band 归 Event 数组，语义任务归 `news_jobs`，checkpoint 归缓存。`news_event_assets` 保留，命题链接从不可变 changes 派生。

命题链接按 `(update_ref,current_ref,previous_ref)` 保留文档中的第一条有效关系，与旧 writer 的 `ON CONFLICT DO NOTHING` 一致；不同关系不能展开成两行。迁移临时索引旧 band 的 Event，并将 Claim 引用展开到临时索引表，避免逐行重复解析 JSON；十组数量、md5 与双向行差异校验均保留。

P3 先停 Workers，再停 Serve，核实无 News 写者会话。完整备份外导出 `news_claim_links`、`news_event_update_heads`、`news_event_updates`、`news_head_scope_repairs`、`news_semantic_observations`、`news_semantic_checkpoints`、`news_semantic_work`、`news_event_evidence_snapshots`、`news_event_bands`、`news_item_revisions`，记录 sha256 并验证 `pg_restore --list`。band 身份、采纳来源、repair、head 或 checkpoint 键不一致时拒绝升级；迁移逐列核对源投影、数量与 md5，日志须有 `p3_verify ok`。孤儿证据只记录 NOTICE，不作为存活 Event 事实回填。

启动前在维护窗口执行 `VACUUM (FULL, ANALYZE) news_events; ANALYZE news_analyses, news_jobs, news_items;`，回收回填旧版本并刷新统计。租约和重试预算保留，未发送唤醒由现有 repair turn 恢复。核对详情版本、head 和待处理任务；回滚恢复已核验备份加旧镜像。

[P3 迁移测试](../tests/integration/test_p3_migration.py)核对 20 个读取投影及预检回滚；[并发与 GIN 测试](../tests/integration/test_news_p3_semantic_chain.py)证明证据 CAS、Event 串行锁、成员 FK 兼容和 4 万 Event 的索引路径。P3 阶段库为 36 张表。

`20261001_0424` 从 `0423` 升级为该阶段的 26 张表：17 张 News 表、七张 Trading 表、`runtime_processes` 和 Alembic 版本表。Trading 的输入、case 文档、entry 与账户分别承接事实、持久决定和执行控制；订单和原生成交证据继续保留。

先记录 `trading status` 以及交易所持仓、普通单和 Algo 单；避开接近 max_hold_s 的持仓或进行中的 flatten。先停 Analysis，等待 pending 清零（最长 300 秒），然后停 Executor、Workers 和 Serve，确认没有写者会话。保留完整备份，另以 custom-format `pg_dump` 导出以下 14 张源表：`trading_triggers`、`trading_source_amendments`、`trading_assessments`、`trading_policy_actions`、`trading_paper_legs`、`trading_signals`、`trading_dispositions`、`trading_plans`、`trading_fill_attributions`、`trading_trade_cursors`、`trading_control_state`、`trading_executor_state`、`trading_analysis_runtime`、`workers_runtime`。记录导出文件 sha256，并用 `pg_restore --list` 核验；部署记录包含备份身份、源 head、目标 head 和导出清单。

P4 拒绝未执行 P0、同 case 多个 assessment、输入/动作/计划/处置/成交身份不一致、不完整 paper 对、claim 不一致，以及无法唯一确定游标账户的数据。13 组数量、md5 与双向逐行校验全部成功后才删除旧表，日志须有 `p4_verify ok`。未归属历史成交的账户保持 NULL；Analysis 和 Executor 的旧心跳不回填为新进程存活证据。

使用 `make up` 启动匹配镜像后，核对 26 表、pause/halt、pending、case 模型输入与订单身份，确认无重复下单，并在 70 秒内完成账户对账。回滚需要恢复已核验完整备份和旧镜像，数据库恢复不能撤销交易所已经发生的订单。[迁移测试](../tests/integration/test_p4_migration.py)验证带数据的 P0→P4、公开投影等价和预检回滚；[账本测试](../tests/integration/test_p4_trading_ledger.py)验证 pending CAS、同 symbol 拒绝、write-once 与进程存活边界。


</details>

## 更早源版本的必要边界

以下是升级旧库时仍需遵守的转换记录，不表示这些旧表、接口或进程仍在线。精确 SQL、锁顺序和预检以对应冻结 revision 为准。

| Revision | 当时执行的转换 | 源码 |
| --- | --- | --- |
| `20260926_0404` | Item 修订、语义工作 / 检查点 / 观察、EventUpdate 与 head、通知工作、intent 发送、公开更新与 Trading amendment | [0404](../tracefold/platform/postgres/alembic/versions/20260926_0404_news_event_updates.py) |
| `20260927_0405` | 来源修订顺序 / 前驱、保留不可变 v1 与新 v2、跨 Event claim 定位索引 | [0405](../tracefold/platform/postgres/alembic/versions/20260927_0405_news_revision_ownership.py) |
| `20260927_0407` | 不可变通知决策、工作与意图引用、逐命题复核和外部漏报短反馈 | [0407](../tracefold/platform/postgres/alembic/versions/20260927_0407_news_notification_decisions.py) |
| `20260928_0408` | Trading 同资产来源检索索引与模型请求截止前未派发状态，不改写历史调用或交易事实 | [0408](../tracefold/platform/postgres/alembic/versions/20260928_0408_trading_not_dispatched.py) |
| `20260928_0409` | News 任务级已处理阅读身份、定向重分析谱系与通知文案复用字段；删除来源级已处理跳过字段 | [0409](../tracefold/platform/postgres/alembic/versions/20260928_0409_news_task_reads.py) |
| `20260928_0410` | 历史编号事实归属修复证明；EventUpdate 来源明确区分模型观察与确定性修复 | [0410](../tracefold/platform/postgres/alembic/versions/20260928_0410_news_head_scope_repairs.py) |
| `20260928_0411` | 清除退役的 News verdict / Review / 学习表及 Trading root tape / Case evaluation；旧市场 Event、v1 EventUpdate 所属 Event 与 `first` / `followup` 发送行删除；Wallet 旧快照字段一次性改写，收紧当前约束 | [0411](../tracefold/platform/postgres/alembic/versions/20260928_0411_retire_historical_contracts.py) |
| `20260928_0412` | 通知队列保存已证明未发送结果、发送账本保存最终结果的精确结算身份；移除与从零开始的真实失败计数冲突的旧约束 | [0412](../tracefold/platform/postgres/alembic/versions/20260928_0412_news_notification_settlement.py) |
| `20260929_0413` | 通知工作增加终态 `failed` 与 `last_error_code`；删除 0407 起恒为空的 `plan` 列；迁移时已过期且 `attempts=3` 的 pending 工作改为 `failed`（`news_notification_exhausted_legacy`），未到期的仍由新代码再规划一次 | [0413](../tracefold/platform/postgres/alembic/versions/20260929_0413_news_notification_work_terminal.py) |
| `20260929_0415` | 语义工作增加失败阅读隔离 `failed_read_refs` 与本次尝试所读范围 `attempt_read_refs`：失败修订（含 Janitor 结算的崩溃最终尝试）只隔离该次尝试实际送入的材料，不再随后续修订重复送入，精确重分析仍可读取；只加列，无回填 | [0415](../tracefold/platform/postgres/alembic/versions/20260929_0415_news_semantic_failed_reads.py) |
| `20260929_0416` | 新增只追加的 `news_claim_links` 并从历史 EventUpdate 回填；通知决定支持 `reader_v2` | [0416](../tracefold/platform/postgres/alembic/versions/20260929_0416_news_reader_decisions.py) |
| `20260929_0417` | DEMO 执行账本硬切：Signal v4、disposition、Plan、订单与原生成交；删除 Nautilus 执行表 | [0417](../tracefold/platform/postgres/alembic/versions/20260929_0417_trading_execution_hard_cut.py) |
| `20260929_0418` | LIVE Analysis 硬切：仅选中 Trigger 建 Case，冻结预测、六策略和双腿纸面账本；删除旧 Gate、WATCH 与逐调用表 | [0418](../tracefold/platform/postgres/alembic/versions/20260929_0418_trading_analysis_hard_cut.py) |
| `20261001_0419` | News 关联召回改为索引驱动：`news_event_assets` 增加数据库生成的 `retrieval_symbol` / `retrieval_pair_base`（重写约 3.4 万行）与索引，`news_items.canonical_url`、`news_events.leader_item_id` 和成员事实 GIN 三元组索引；删除只被旧召回使用的标题 GiST 索引。不改事实行；支持降级 | [0419](../tracefold/platform/postgres/alembic/versions/20261001_0419_news_recall_indexes.py) |

0404/0405/0407 采用前向契约切换，不从旧卡片或 verdict 伪造新命题。0409 的任务级 `processed_read_refs` 从实际完成的阅读建立；0410 仅建立归属修复证明，运行时定向修复见[运维](OPERATIONS.md#历史编号事实的-head-归属清理)。0411 删除退役 verdict / Review / 学习、root tape、旧市场 Event、v1 head 和旧发送行，但保留原始 Item、类型化市场事实和当前版本的知识、回执与执行证据。

0412 前保存精确 intent 的状态、版本、attempts、lease、工作和回执清单；旧 attempts 混合领取与失败次数，不能批量归零或当成真实失败次数。只有确证未进入可能发送边界的当前 intent 才能按运维命令定向恢复。`sending`、`ambiguous` 与已有回执不能批量改为 pending。

0417/0418 为不可降级的 Trading 硬切：先停旧执行进程，用签名场所读确认 DEMO 仓位、普通单和 Algo 单均为零，再备份所有 `trading_*` 表与归档目录。0417 建立 Signal v4 与订单/成交账本；0418 要求 Signal 表为空，删除旧 Analysis Case、Gate、WATCH 和逐调用记录。迁移与新镜像在同一维护窗口完成，不回填旧 DEMO 数据，回退使用已验证备份和旧镜像。

## 回退与验证

### 0430：Trading 执行证据

`20261003_0430` 接 0429，在现有 entries 增加准入文档与保证金预留、orders 增加非秘密请求与应用 resolution、accounts 增加未解决执行故障集合；不新增表，不改历史已终结事实。接受时一次冻结资金依据，保留原唯一约束、订单身份、用户控制和在途责任。见[执行手册](modules/execution.md)与[带责任迁移测试](../tests/integration/test_migration_trading_evidence_0430.py)。

发布采用匹配镜像、数据库 head、配置身份与完整备份，按既有停写维护窗口进行；先记录实际仓位、普通／Algo 单和 unknown 责任，停 Executor 会暂停保护维护与到期退出，不能自动平仓作为迁移前置。升级失败事务回滚，成功后核对 0430 并先恢复责任与控制再消费新输入。

0430 的 DDL 虽为新增字段，旧镜像不理解新故障与部分成交管理。隔离测试证明：只要存在新准入、请求、resolution 或未解决故障，downgrade 就拒绝并保留 0430；没有这些新证据时才允许回到 0429。已有新执行证据采用停止新增风险后的前向修复。场所已经执行订单后，不可恢复较早数据库备份来抹掉责任；配对恢复必须另经授权并先保留当前订单与场所核验。

| 情况 | 支持的处理 |
| --- | --- |
| 当前 schema 与服务命令兼容的本地镜像替换 | `make deploy-image IMAGE_ID=sha256:<完整 ID>`；目标 image head 必须等于实际 database head |
| 0429 回到 0428 | 停写后仅删除新增故事与已送达命题索引，核对 head，再恢复配对 0428 镜像 |
| 0428 回到 0427 | 停写后执行对应可逆索引降级、核对 head，再恢复配对配置与 0427 镜像 |
| 0427 读数转换、0417/0418 硬切或其他不可降级转换 | 恢复已验证的迁移前完整备份与对应旧镜像，或经验证的前向修复 |
| 基线之前或来源未知的备份 | 使用备份记录的源码 / 镜像和恢复流程，在隔离库核实；当前单链不保证无条件接续 |

恢复数据库不会撤销交易所已有订单或成交，也不会证明通知未发送。恢复后分别核实原始来源、采用版本、冻结正文与外部结果、Case 预测与原生成交，不让缓存或 UI 成为真值来源。

迁移验证使用独立空库或 scratch clone，覆盖历史升级、带数据投影等价、预检失败回滚及当前 head。本文链接的是这些行为测试，不表示本轮文档修改重新执行了真实数据库迁移。具体资源与报告见[测试指南](TESTING.md)。

---

[返回文档中心](README.md) · [运维恢复](OPERATIONS.md#backup)
