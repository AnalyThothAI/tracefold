# #791 PR-A：统一命题召回实施与验收进度

[Issue #791](https://github.com/AnalyThothAI/tracefold/issues/791) · [News 手册](../modules/news.md) · [机器可读重放结果](issue-791-claim-recall-2026-10-02.json)

**当前结论：可提交草稿供代码审阅，尚未满足合并验收。** 稠密召回在种子盲标集上达到命中率门槛，但降级回执召回未达标，完整 24 h 重放、模型重问和生产规模延迟证明仍未完成。没有合并、部署或发送通知。

## 实现范围

- `claim_recall.rank()` 是语义 prior、冻结回执和离线重放的共享候选融合实现。稠密、PostgreSQL FTS、同源路线使用 RRF；回执按最佳命题得分聚合，已有链接优先。
- 抽取之后逐命题选择跨 Event prior，只判断选中的跨 Event 对；本 Event 内仍全对判断。领取读取不再执行旧跨 Event 召回。
- `news_claim_index` 按 `(claim_ref, text_sha256)` 保存精确文本版本、检索特征、嵌入器身份和 fp16 `bytea`。采用事务写入缺向量的持久行，维护任务在事务外计算后补入。历史回填按文本版本检查完整性。
- 嵌入器通过 `llm.news_embedding_model` 使用外部模型路由，有界调用、启动多语言探针和 FTS 降级；应用镜像不包含模型权重。
- reader context 在快照阶段召回一次，发送 CAS 改核对已送集合和链接图的世代；向量补算、普通来源元数据变化不推进该世代。
- 迁移 `20261002_0426` 删除 0419 的检索生成列、函数和 trigram 索引。升级和降级均有 PostgreSQL 用例，持久 Pydantic 文档形状保持兼容。当前前置 schema 已有 27 张领域表，新表后为 28 张；Issue 中 26 → 27 的计数早于 reader clock 表。
- 删除旧三通道、DF 与结构回执召回及 SQL/Python 孪生实现。保留并迁移 #750、#755 场景意图和上币链接的新颖度保护用例。

## 真实种子集重放

使用 #791 审计的 150 条查询、10,394 个候选单元及盲标标签。向量来自已有 MiniLM 数值 NPZ（`allow_pickle=False`），仅对命题 statement 检索；回执正文不参与向量候选生成。FTS 由隔离 PostgreSQL 的临时表计算，没有写持久事实、模型判断缓存或通知。

数据摘要：`05ae4807a6f0f519657d27aeee5a5aac381cae8cd130a56e17e9ce99957e38a5`。摘要包含查询、候选单元、命题、回执、标签、嵌入索引与数值文件。当前模型是 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`，384 维、L2、`claim_embed_text_v1`。

prior 在网格上拟合 k、稠密下限与词法下限，以满足总体和中俄文命中率后最小化候选数：k=8、dense≥0.55、lexical>0.1、保留 2 个已送候选名额。回执当前为 k=16、dense≥0.4、lexical>0。**回执切点尚未完成精度与降级约束的联合拟合，模型三方 bake-off 也未完成。** 校准摘要进入 `RECALL_POLICY` 和语义程序身份。

| 路径 | 至少命中一个 SF | 中文 SF 查询 | 俄文 SF 查询 | 结果 |
| --- | --- | --- | --- | --- |
| 稠密 prior | 59/65，90.77% | 18/18 | 3/3 | 达到种子集门槛 |
| 稠密回执 | 31/32，96.88% | 9/9 | 1/1 | 达到种子集门槛 |
| 降级 prior | 38/65，58.46% | 3/18 | 0/3 | 高于 Issue 总体现状 43%；跨语言仍弱 |
| 降级回执 | 20/32，62.50% | 1/9 | 0/1 | 低于 Issue 现状 72%，未通过 |

这些分层以本脚本的查询 script 和 SF 候选集合为分母，与 Issue 原审计表的子集口径有差别，不把两者当成同分母的逐条对照。拟合和报告使用同一种子集，没有独立留出集。

本脚本是候选排序对照：历史语料按 claim ref 的最早采用版本组织，尚未逐时间点重建 current head、同源来源、索引回填状态和 `first_available_at_ms` 窗口。完整生产适配器重放会补齐这些范围差异。

prior 共选择 856 个候选，较样本中原 prior 数增加 11.02%，没有证明关系对下降 ≥40%。回执共选择 1,202 条，只有 429 条能关联盲标标签，其中同话题或无关占 47.09%；773 条未标注。prior 的 658 个有标签候选中同话题或无关占 36.93%，另有 198 个未标注。未标注项不当作无关，也不从质量证明中隐去。这不是 Issue 要求的完整 24 h / 50 条抽样验收。

#750 冻结公开案例的 4 条相关黄金回执均进入读者候选，兄弟命题没有继承黄金上下文。#755 场景的 4 条噪声故事在稠密下限以上为 0 条。二者使用小型冻结真实模型向量通过共享 `rank()`，证明固定场景回归；不替代完整重复链与真实模型读者重问。

复现入口（审计语料保存在仓库外）：

```bash
uv run --locked python scripts/eval_news_recall.py \
  --audit-dir /path/to/recall/eval \
  --postgres-dsn postgresql://.../isolated_evaluation_db \
  --report /tmp/claim-recall-report.json \
  --calibration tracefold/news/claim_recall_calibration.json
```

## 已执行的工程验证

| 检查 | 实际结果 | 证明边界 |
| --- | --- | --- |
| `make check` | static 全部通过；344 passed、4 deselected | Ruff、类型、生成物检查与架构/契约 |
| hermetic pytest（排除真实资源、部署、e2e、golden、slow、scheduled、external_codegen、package） | 2,198 passed、853 deselected、57 subtests passed | 当前纯回归集合，包含架构与契约；不与上一行相加 |
| 相关 PostgreSQL 集成及迁移集合 | 83 passed | 索引版本、冻结回执、世代并发、事务超时、迁移恢复 |
| OpenAPI TypeScript 生成对拍 | 1 passed | 生成类型与 OpenAPI 一致 |
| `npm --prefix web run typecheck` | 通过 | 新 status 字段的前端消费方 |
| 修改的前端 fixture 的 Prettier 检查 | 通过 | 修改文件的格式 |
| 数据库结构生成 | 隔离测试库升级到 0426 后生成 | 新表、删除对象和触发器快照 |
| `uv build --wheel` | 构建成功，校准 JSON 与适配器在 wheel 中 | 打包资源不会在安装时丢失 |

PostgreSQL 使用独立测试容器，未迁移生产库。相关集合为 `test_claim_recall_migration.py`、`test_news_claim_index.py`、`test_news_event_update_store.py`、`test_764_concurrency.py`、`test_news_semantic_input_timeout.py` 和 `test_news_evidence_material.py`。

本地全量前端 `format:check` 曾报 240 个文件格式差异；核实未修改的 `AppRoot.tsx` 是 checkout 中 CRLF，而 Git HEAD 为 LF。未批量重写其他前端文件。生成类型已重新由生成器输出并对拍，未手工格式化。远程 CI 仍需核实，以上本地检查不代表远程 CI 已通过。

## 转为可合并 PR 前剩余事项

- 修复降级回执召回不足；完成回执精度与候选预算的校准，保留跨语言 SF。
- 完成 MiniLM / bge-m3 / Qwen3-Embedding-0.6B 的候选模型 bake-off，选定身份并重新拟合。
- 补齐 20 条重复链、完整 24 h 只读重放、50 条上下文盲评及关系对减量证明。
- 执行已获 owner 批准的 PR-A 读者重问，证明重点重复抑制、新事实不降级、误锚 ≤1 和信息流不误升级。
- 用生产规模 PostgreSQL 数据播种测量 prior / 回执 p95，证明 ≤200 / ≤80 ms。
- 补齐 48 h 已送命题在超过 7 d 或不再是当前 head 版本时的 prior 保留范围；当前保留名额只作用于当前 7 d 候选。
- 完善 `/api/news/status` 的路由健康证据、`reader_changed` 的完整计数和每日 SQL 回执统计；当前 status 仅反映索引缺向量或身份不匹配，plan 冲突有日志。

PR-A 保持 draft，直到这些验收完成。PR-C 与 PR-B 是 Issue 指定的独立交付，本报告不关闭 #791。
