# #750 News reader 冻结回放与切换证据（2026-09-29）

本报告对应 [#750](https://github.com/AnalyThothAI/tracefold/issues/750)，调查与实现起点为 `aa133f3dcf1fa78f0b44d4d8c607701e835550dc`，PR 已同步至目标 main `197cab0fd2c99f251132bf54e7cf5ebb59f177bd`。代码把逐命题召回、历史已送版本、统一 reader context/CAS、可证明等价否决及新输入契约一同切换。数据读取使用 `default_transaction_read_only=on`；本次没有修改生产库、合并或部署。

reader 提示词、重要性档位及通知切点未扩大或调整；问题 identity 因输入契约版本改变，缓存键还绑定实际消息内容与顺序的输入 digest。评测报告记录当前问题 identity，并按每条输入 digest 绑定重新取得的回答；合并批次时若 digest 漂移会拒绝复用。

## 候选与判断回放

黄金事件 `9f8231c5…` 在 `now_ms=1790673975105` 冻结。48 小时窗口有 1,330 条 `sent` 回执，缺失 `(event_id, content_revision)` 历史投影为 **0**。[候选 fixture](../../tests/fixtures/news/issue_750_gold_recall.json)包含旧 16 条、新候选池及逐条对照正文的 4 条直接前情和 6 条噪声。黄金 claim 的直接前情命中从 **1/4 → 4/4**，指定噪声从 **6/6 → 0/6**；另一条 `US data in focus` 的新列表为空，不继承黄金回执。该十条标签是个案标注，不能推成全站精度。中文 `eca8e3` 依赖已送版本中的黄金资产结构入选，英文 query 对中文正文没有通用语义检索能力。

[120 条冻结样本](../../tests/fixtures/news/issue_750_reader_replay.jsonl.gz)从 397 条既有独立 claim 标签与只读生产不可变版本配对；356 条能唯一定位决定，41 条决定歧义被排除。按事件去重后选取 7 keep、36 borderline、77 demote，包含 87 unlinked、33 linked，以及 20 条含中文 statement。标签来自原独立判断，不以资产或检索分数自动生成；样本偏向稀有 keep/borderline，不代表线上自然分布。每条保存目标 claim、决定时刻、候选已送版本、关系、SQL 结构/全文路线名次、实际正文 digest、旧消息数量、新消息顺序、新输入 digest 与判断答案。旧 v1 回答只作基线数据；两个后端对新 v2 输入重新作答。最终融合保留 PostgreSQL `ts_rank_cd` 名次，同时用同一实义词条件过滤词干假阳性；修正前有 104/120 条输入次序不同，旧回答已全部废弃并重新调用。

| 同一 120 条样本 | 旧输入 | 新输入 |
| --- | ---: | ---: |
| 消息数 P50 | 16 | 8 |
| 消息数 P95 | 16 | 16 |
| 空列表 / 满 16 条 | 0 / 120 | 4 / 35 |
| 新输入合计正文字符 | — | 161,898 |
| native 重要性 AUC，keep+borderline 对 demote | 0.842 | 0.805 |
| native 重要性 AUC，keep 对 demote | — | 0.983 |
| generated 重要性 AUC，keep+borderline 对 demote | 0.858 | 0.871 |
| generated 重要性 AUC，keep 对 demote | — | 0.966 |

native 新输入 120/120 成功，判断延迟 P50/P90 为 258/305 ms；按当前切点产生 4 key、8 push、104 feed、3 known、1 correction。推送中有 4 keep、8 borderline、1 demote（该 demote 走确定性 correction，重要性仅 0.78）。与旧输入按同样本比较，有一条 keep（`L037`，一汽丰田股权交易）从 2.54 降到 2.42 并落到 native 推送切点下；这是真实回归风险，已保存在候选/回答 fixture，不能用列表缩短后的平均噪声下降掩盖。另有一条 keep（`L286`，英伟达回购）被现存关系判为 known，但对应已送正文实际含 1500 亿美元额度；该标签称旧推送缺少额度，标签与已送事实需人工复核，不能据此自动改写持久关系。没有在此验证集上调切点。

generated 新输入 120/120 成功，分两批调用并合并答案，批次 P50/P90 分别为 4,459/7,537 ms 与 5,206/8,477 ms（各 60 条）；按当前切点为 1 key、18 push、97 feed、3 known、1 correction，推送中有 4 keep、13 borderline、3 demote。generated 的 claim 级 AUC 比旧输入略高，native 略低；两者的差异不能代替候选级事实覆盖检查。全样本结果见 [generated 报告](../../tests/fixtures/news/issue_750_sample_generated_eval.json)与 [native 报告](../../tests/fixtures/news/issue_750_sample_native_eval.json)。锚点/核心事实的广泛候选级标签尚未人工扩展至 120 条，因此本报告仅对黄金十条和原始独立 claim 级标签给出精确率/召回口径，不把未标注候选当作 0 或 3。

### 固定事件序列

[Nvidia / SpaceX 序列](../../tests/fixtures/news/issue_750_clusters.jsonl.gz)冻结了 9 条回购命题与 17 条 Starship 命题，25 条使用新 reader 输入重新提问，1 条 `retired` 由原确定性规则处理，未跳过。native 的推送数为 Nvidia **2/9**（1500 亿美元额度、FY2028 执行期）与 SpaceX **0/17**；generated 为 Nvidia **4/9**、SpaceX **0/17**，两次评测均 25/25 回答、无失败。generated 的四条 Nvidia 包含首条额度公告，以及“史上最大”、盘前涨幅和“正在回购 1500 亿美元”的后续表述；后三条有重复核心事实或常规市场反应的明显风险，未达到本 Issue 对重复推送的质量验收。这三条的 `messages` 已包含此前“增加 1500 亿美元授权”的真实已送正文，所以此处是判断风险，不是召回漏掉该前情。逐条结果见 [native](../../tests/fixtures/news/issue_750_clusters_native_eval.json)与 [generated](../../tests/fixtures/news/issue_750_clusters_generated_eval.json)。另一次更强提示词的隔离试验把 Nvidia 降到 2 条，却使 SpaceX 产生 4 条推送且重复“首次入轨”；因此未采纳该改动，也未对验证集调切点。

此序列固定的是旧策略产生的已送回执。反事实新推送会改变之后的历史，不能把此结果称为完整线上逐日重放。关键重复样本需由独立标注与发布前人工复核确认；在此之前 #750 的质量关闭条件仍未满足。

## 关系否决与刷新 dry-run

[只读审计](../../tests/fixtures/news/issue_750_relation_audit.json)按 7 天窗口重建关系断言时的命题版本：4,358 条链接，历史版本缺失 0；523 对有自由文本差异，其中 294 对原先只有自由文本 veto，没有另一个旧式明确否决。新函数对其中 **16 对**仍给出可证明 mismatch，**278 对**变为未知。294 是受影响对数，不是 294 个已证明的错误；模型关系改变数尚未重新分析验证。

48 小时有效窗口中有 246 个 pair 涉及 125 个当前 Event。通过现有 `reanalysis_scope_list` 做只读预览，124 个 Event 可定位当前 wanted/head/read scope，1 个有可选读取待处理。按 [运维步骤](../OPERATIONS.md#750-news-reader-一次性切换)在发布后逐 Event 重查版本、时效及外部发送状态，才可执行定向重分析；新断言应追加，不能删除旧链接。此处没有触发生产重分析。

## PostgreSQL 性能与一致性

黄金窗口的普通召回是一个批量 SQL：两条 claim 查询，读取 1,330 条 48 小时 sent 回执和 2,653 条不可变更新版本的扫描范围，返回 24 个按 claim 标识的候选行。最终 SQL 的 `EXPLAIN (ANALYZE, BUFFERS)` 在局部关闭 JIT 后执行 **132 ms**；最初未关闭 JIT 的基线查询约 1,267 ms，主要耗在编译，不能作为最终 SQL 的严格同比。只对该短事务设置 `SET LOCAL jit=off`，并在物化窗口中只计算一次 `tsvector`。

同一只读生产快照上按最终 SQL/排序重复 15 次：`_reader_state` P50/P95 为 **141/163 ms、8 次 SQL 往返**；CAS 的 `_current_reader_revision` 读阶段为 **135/148 ms、9 次 SQL 往返**（15 次样本的 P95 是最大值，受单次尾延迟影响）。批量行数随 claim 增加，往返数不按 claim×receipt 增长。两处 CAS 在 PostgreSQL 集成测试中验证相关新回执使旧 revision 失效，无关回执不失效；snapshot 使用 repeatable read。以上为同硬件读路径样本，不含写入锁等待、外部模型、发送或完整生产部署延迟；生产 pending 工作当前为空，故无法在只读生产上实测完整 snapshot/写 CAS 分位数。

## 发布边界

审计时生产 `news_delivery_queue` 中 update 待发送行 **0**；`news_notification_work` 为 done 1,971、failed 8、pending 0。发布前需重查。代码使旧 reader revision 的未发送冻结卡片在新 lease 下清空并重规划，同一 intent ID 防止重复；已开始发送、ambiguous、sent 凭证保持原账本。`news_notification_decisions.origin=reader_v2` 是现有决策家族约束，新 reader 输入为 `news_reader_input_v2`，context 为 `reader_v3:`，无需数据库迁移。有限生产抽样、活跃关系实际刷新与部署切换仍是发布操作，本 PR 不把它们写成已通过。
