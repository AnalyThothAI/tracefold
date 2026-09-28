# #733 · EventUpdate 上线后生产证据审计（2026-09-28）

## 审计身份与口径

- 生产 PostgreSQL，只读事务 `REPEATABLE READ, READ ONLY`，`statement_timeout=20s`；查询首次执行于 **2026-09-28 07:54:56 UTC**。固定 Event cohort 是 **[2026-09-27 07:54:00, 2026-09-28 07:54:00) UTC**，以 `news_events.created_at_ms` 入组；下游 insert-only 记录须早于窗口终点。精确 SQL 见[同名查询](issue-733-event-update-audit-2026-09-28.sql)。
- 最早可见的持久 EventUpdate adoption 是 **2026-09-27 09:46:43.090 UTC**。这是数据可证明的起点，不能据此断言部署开始的准确时刻。以此起点到窗口终点另算一组上线后 cohort。
- `origin/main`、本审计 worktree 的 HEAD、当时 workers/serve 镜像标签 `org.opencontainers.image.revision` 均为 `79f66fb91461457eeb1f3574d190a5f41507007c`；镜像 ID 为 `sha256:d2ca42738d873b919a74147be6ba2f2a40f27945058351e0fc766c7e4eaaf2fc`。#733 原文记录的较早生产 revision 是 `02b7d315a9ea97ed28221cd543d0e6d1306bf227`。这两个 Git revision 的 `tracefold/news` 无差异，但数据库 cohort 仍跨部署和配置时间，不能宣称为严格版本对照实验。
- 粒度分别为 Event、leader Item、所有贡献的 Item、Fact membership、首次 observation/adoption/decision/sent Event，以及实际 delivery row。`news_semantic_work` 是当前可变工作状态；其失败数和 attempts 数是上述查询时点的快照，日后重跑不能复原。

在有权读取该生产库的主机上，从仓库根目录复跑查询：

```bash
docker exec -i tracefold-postgres-1 psql -X -A -F '|' -U tracefold -d tracefold -v ON_ERROR_STOP=1 \
  < docs/reports/issue-733-event-update-audit-2026-09-28.sql
```

重跑 insert-only 指标时仍按 SQL 中的固定终点截断；当前工作状态以及因保留期清理而消失的历史行不能视为原快照。

## 审计结论

1. **性能与产品问题仍在，证据强。** 71 个 `explicit_numbered` Event 的 created→首次 observation P50/P95 为 **110.17/286.50 秒**，599 个 `whole_item` Event 为 **16.07/84.35 秒**。首次 adoption→首次 sent 为 **199.99/564.09 秒**，whole 为 **37.04/292.94 秒**。9 个 leader Item 中最大的 14 Event 样本有 11 个 Event 实际送达，首末相隔 **1276.3 秒**。这证明实际碎片化与延迟，尚不能仅凭这些分布确定模型调用、队列和并发各自贡献。
2. **通知主要慢在 adoption→持久决定，证据强。** 拆分 Event 的该段 P50/P95 为 **183.53/417.38 秒**，whole 为 **30.25/362.29 秒**。与实际 sent delivery 的 `decision_ref` 匹配后，拆分 Event 的决定→sent P50/P95 仅 **5.65/11.05 秒**（40/45 个 sent Event 有匹配的决定）；发送尝试→sent P50 为 **2.19 秒**。`DelivererLoop.advance()` 顺序 `await` 每个完整 `Notifications.process()`，而 `20` 只是每轮上限。观察结果与通知准备/排队的队头阻塞一致，不能从记录单独分离每个模型调用时间。
3. **#733 的“9 个原始 Item”和“平均 attempts”需要改口径。** 71 Event 的 leader 有 9 个 Item，但 `news_event_members` 指向 **16 个贡献 Item、148 条 Fact membership**（71 leader、75 exact、2 near）。例如 `4233542` 是 13 个 Event 的成员，却只领导其中 3 个；`4233178` 参与 11 个，只领导 1 个。`news_semantic_work.attempts` 是**当前 wanted revision** 的计数，不是上线以来所有语义尝试总数。剔除最后 10 分钟的工作快照为 split **0.549（13/71 当前 revision 耗尽失败）**、whole **0.079（15/594 失败）**；不能把 6.9 倍写成模型执行次数或耗时的放大倍数。
4. **FactUnit scope 存在生产可复现的漏段，证据强；下游事实影响未证实。** `news-opennews` 的 `source_item_key=4234240` 原始 `evidence_text` 末尾为“（以上内容整理自我的钢铁网、正信期货、五矿期货等研报及公开资料，仅供参考，不构成投资建议）”。该 Item 无 `news_item_revisions`。用当前 `extract_fact_units()` 对该原文重放，得到 6 个 `explicit_numbered` 单元；原文包含“仅供参考，不构成投资建议”，但 6 个单元的 `text/context` 均不含此句。现有抽取提示明确把 scoped `fact_text` 定为任务边界，因此这是确定的 scope 丢失风险。此句是整篇免责声明；本审计**没有证据**表明已经把其中一个具体命题的条件、否定或阶段错误写成事实。
5. **不能把未发送都算成故障。** 70/71 split Event 已有首次 observation、adoption 和决定；首次决定中 44 为 `notify/uncovered_claims`，26 为 `no_notification/no_uncovered_actionable_claims`。最终 45 个 Event 有 sent 回执，其中一个起初为 no-notification、在后续更新后发送。7 个 Event 的 `4228341` 是一周财经日程，全部首轮 no-notification 且 0 sent；这不等于 7 次发送失败。窗口内该 cohort 的已结算 delivery 为 45 条 split update sent、382 条 whole update sent 和 4 条 legacy first sent；均有 receipt，update sent 均有实际 `body`，未见此 cohort 的 `ambiguous/not_sent` 结算记录。

## 固定窗口核算

| 粒度 | explicit_numbered | whole_item | 解释 |
| --- | ---: | ---: | --- |
| Event | 71 | 599 | 合计 670 Event、608 个不同 leader Item。 |
| 首次 semantic observation / adoption / decision | 70 / 70 / 70 | 560 / 560 / 560 | 近期无记录的 Event 不自动等于失败；whole 含 recovery/non-work。 |
| 至少一次 sent 的 Event | 45 | 363 | 不是 delivery row 数。 |
| opened→observation P50 / P95 | 111.33 / 287.85 s | 18.15 / 190.56 s | 与 #733 原文的时钟口径一致。 |
| created→observation P50 / P95 | 110.17 / 286.50 s | 16.07 / 84.35 s | 更接近本系统入库后的处理耗时。 |
| adoption→first decision P50 / P95 | 183.53 / 417.38 s | 30.25 / 362.29 s | 首次决定；后续修订可能产生新决定。 |
| adoption→first sent P50 / P95 | 199.99 / 564.09 s | 37.04 / 292.94 s | 仅对已有 sent 的 Event 计算，未发送为删失样本。 |
| 当前 revision 失败 / 排除最近 10 分钟的 Event | 13 / 71 | 15 / 594 | 可变工作表快照。 |

可见的 EventUpdate 首次 adoption 之后至窗口终点，有 **650 Event、588 leader Item**，其中 71 split、579 whole。固定 24 小时窗口另含 **20 个首次 adoption 之前创建的 whole Event**；不应把全部 670 个都称为“上线后创建”。原 issue 的 679/617 是当时的滚动 24 小时快照，本次固定窗口为 670/608；没有原查询的精确终点，不应把这两个总数当作同窗趋势。`opened_at_ms` 不是可靠的入库时钟：whole 样本有 **149/599** 个 Event 的 opened 时刻晚于 created 时刻；报告同时给出 created 时钟口径，避免把来源时钟误差算进处理耗时。

数据完整性检查：670 个 Event 的 leader Item、leader Fact member 均存在；71 个 split Event 的 `focus_fact_text` 均非空。此检查只证明键和必要字段完整，不能证明 FactUnit 等于真实事件或抽取语义正确。

## 逐 Item 证据与失败归因

| leader source_item_key | Event | sent Event | 首末 sent 跨度 | 当前 revision 失败 | 观察 |
| --- | ---: | ---: | ---: | ---: | --- |
| `4235003` | 14 | 11 | 1276.3 s | 4 | 电池/汽车 digest，10 条首轮送达，最后一条由后续修订延至 07:17:18 UTC。 |
| `4233541` | 13 | 7 | 198.0 s | 0 | AI digest。 |
| `4233177` | 11 | 11 | 640.8 s | 3 | 芯片 digest。 |
| `4227637` | 8 | 5 | 105.5 s | 0 | 特朗普多主题 digest。 |
| `4233714` | 8 | 7 | 243.3 s | 3 | 期货早盘 digest。 |
| `4228341` | 7 | 0 | — | 3 | 周一至周日财经日程；7 个首轮决定均不通知。 |
| `4234240` | 6 | 4 | 324.2 s | 0 | 焦煤研报 digest，尾部免责声明未进入任一 FactUnit scope。 |
| `4233542` | 3 | 0 | — | 0 | 同时是 `4233541` 的 13 个 Event 的成员。 |
| `4233178` | 1 | 0 | — | 0 | 同时是 `4233177` 的 11 个 Event 的成员。 |

`4235003` 的 `item_id` 为 `635581e72ebd3a9166a77bb7964602a7080e40065a86e782ee4f1a9b15223b1c`，其 14 个 Event 在 **06:55:18.921 UTC** 左右建立。第一条 sent 是 **06:56:01.754**，最后一条 **07:17:18.038**。`event_id=936b9ec5775f188247e78e17792c7479467ef93b5bfa5037420f509eb556381b`（“特朗普：批准新的燃油经济性标准，废除拜登时代的电动汽车强制令。”）首轮决定为 `no_notification`，后来有 sent 回执；因此 21 分钟跨度包含新修订，不能全部归咎于同一轮 Deliverer 串行。但其他 10 条首轮 sent 已跨约 11 分钟，碎片化仍成立。

当前 13 个 split 失败分布在 4 个 leader Item：`4235003` 4、`4233177` 3、`4233714` 3、`4228341` 3。末次错误码为 `news_generation_output_truncated` 5、`news_provider_unavailable:TimeoutError` 3、`news_citation_not_in_frozen_source` 2、`news_generation_output_schema_invalid` 2、`news_topic_outside_codebook` 1。许多失败 Event 已有先前 adoption（当前 `wanted_revision > done_revision`），所以“曾采用”和“当前修订失败”并不矛盾。错误码分布说明不能将 attempts 差异归因为单一模型速度问题。

## 源码核对与后续验收

- `tracefold/news/events/facts.py::_blocks()` 保留原文各块，但 `extract_fact_units()` 在 split 路径仅把编号行写入 `FactUnit.text`，且 `_lead_context()` 只取第一个编号行**之前**的内容；编号项之间及列表末尾的非编号行无归属。`tracefold/news/updates/dspy_backend.py` 的 extraction scope 提示只处理 `fact_text` 并用 context 解读它。优先修复连续限定段的归属，无法可靠归属时回退 whole_item；用真实尾段样本和“编号项下一段含条件/否定”的 fixture 验证，防止 proposal 变 executed。
- `tracefold/news/pipeline/delivery.py::DelivererLoop.advance()` 对 `pending_notification_events(..., 20)` 逐个 `await self._notify()`。应与 #731 的 bounded prepare、editor input reuse 和 reader CAS 优化合并设计；保留一个 paced sender、持久决定、实际送达正文覆盖和 ambiguous 发送不盲重试。
- 当前 Event 详情读模型已经有**单 Event** 的成员、Fact、semantic、decision、intent、delivery 信息（`tracefold/news/storage/feed.py`），但无从一个 Item 直接列出全部兄弟 Event/Claim/通知的反向视图。观测需同时展示 leader 与 exact/near 成员，避免 9/16 的粒度误读。
- 后续复测固定相同入组时钟、观察终点和 Event/Item/member 粒度；分别报告 created→observation、adoption→decision、匹配 `decision_ref` 的 decision→receipt，以及同 Item sent Event 数和跨时。分开报告当前 revision 耗尽失败、真实累计调用/重试和吞吐。只有在修复部署后才能评估性能验收；本次审计不把尚未实施的修复记为通过。

**证据边界：**这些是生产记录的观察性分布，9 个 leader split Item 的样本较少且来源/内容不同。没有逐次模型调用计时、队列停留事件或部署前后同分布对照，因此不能定量拆解 semantic 耗时，也不能证明准备池能降低多少延迟。`news_semantic_work` 的状态和可变配置不能由固定历史窗口重放；SQL 再执行时可能不同。查询没有调用模型、发送通知、重试工作、改动数据库或部署。
