# #791 PR-A：统一命题召回实施与验收进度

[Issue #791](https://github.com/AnalyThothAI/tracefold/issues/791) · [News 手册](../modules/news.md) · [机器可读重放结果](issue-791-claim-recall-2026-10-02.json)

**当前结论：PR #794 保持草稿，尚未满足正式验收。** 原 MiniLM 校准不能同时满足召回与上下文精度；更强模型正在同一完整语料上比较。完整重放和已批准的读者重问已执行，暴露的问题仍需修复；性能测量作为观测；A 尚未合并或部署。独立 PR-C #792 已合并部署并通过线上恢复检查。

Owner 后续调整：以业务完成度和实际 bug 为交付标准，不以过度性能卡口阻止交付；完成后授权合并、部署和线上检查。延迟用例保留完整窗口、精确版本和事务边界断言，作为独立 slow 测量报告预算是否达到，不再用 200/80 ms 独立阻止合并。召回污染、误锚和新事实误降仍属于待修复的业务问题。

## 实现范围

- `claim_recall.prepare_rank()` 和 `rank()` 是语义 prior、冻结回执和离线重放的唯一候选融合实现。完整窗口只计算一次稠密分数，再验证全部符合资格的精确文本版本并读取 PostgreSQL FTS；同源路线使用同一 RRF。回执按最佳命题得分聚合，已有链接优先，没有旧排序 API 或兼容路径。
- 抽取之后逐命题选择跨 Event prior，只判断选中的跨 Event 对；本 Event 内仍全对判断。领取读取不再执行旧跨 Event 召回。
- `news_claim_index` 按 `(claim_ref, text_sha256)` 保存精确文本版本、检索特征、嵌入器身份和 fp16 `bytea`。采用事务写入缺向量的持久行，维护任务在事务外计算后补入。历史回填按文本版本检查完整性。
- 嵌入器通过 独立 `llm.news_embedding` 的私密密钥文件、端点和模型配置使用外部模型路由，分批有界调用、启动多语言探针和 FTS 降级；临时启动故障隔 30 秒重新自检；应用镜像不包含模型权重。
- reader context 在快照阶段召回一次，发送 CAS 改核对已送集合和链接图的世代；向量补算、普通来源元数据变化不推进该世代。
- 迁移 `20261002_0426` 删除 0419 的检索生成列、函数和 trigram 索引。升级和降级均有 PostgreSQL 用例，A 的持久 Pydantic 文档形状不变。当前前置 schema 已有 27 张领域表，新表后为 28 张；Issue 中 26 → 27 的计数早于 reader clock 表。
- prior 的普通 7 d 窗口与最近 48 h 已送精确命题取并集，已送命题不受原始年龄或当前 head 文本版本限制。回填与缺向量状态使用相同范围。
- 运行时心跳携带嵌入能力的真实状态；`recall_dense=on` 同时要求新鲜 Workers 心跳、模型路由已验证且有效索引无待补项。批量失败报告降级，后续成功可恢复。模型 revision、token 上限、pooling 与 dtype 进入嵌入身份；配置身份不匹配只停用稠密路线，保留语义处理。
- 已送事实或链接推进 reader generation；计划及发送 CAS 冲突使用同一 `reader_changed` 计数。语义观察和通知决策在原有 JSON 元数据中保存稳定召回诊断，日统计只读 SQL 使用这些事实，缺历史诊断不当作成功。
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

后续完整窗口重放已覆盖 2,550 个实际抽取 slot；原比较共 14,085 对，其中 13,304 个跨 Event 对和 781 个本 Event 对。MiniLM 的 statement 稠密 / canonical 字段 FTS 候选降至 7,551 对，但 50 条真实抽样盲评仍有 171/186（91.94%）同话题或无关候选，未通过。BGE 后续候选能覆盖 20/20 已知重复收据和 31/32 SF 查询；新的 50 条盲样仍有 27/33（81.82%）同话题候选，仍未通过。这些都是失败的候选实验，不是当前配置的验收结果。

MiniLM 候选在 206 个变更上下文上完成 412 次原生 A-v2 读者回答，全部 available；20 条已知重复均不再为重点。对 30 条有判定变化或新锚的命题另行盲评发现 **2 个误锚**（航母部署的后续发展、美元 17 个月高位与此前 3 个月高位），超过最多 1 个的门槛；**3 条真实新细节被降级**（柴油需求新规模及制裁的新理由/指控）；纯 feed 内容误升级为推送为 **0**。最终候选需要重新执行这些证明，不能以重点重复消失代替其他质量约束。

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

后续工程修复的当前验证：`make check` **349 passed、4 deselected**，static 全部通过；News 单元集合及模型路由组合 **1,393 passed**；正确的 `ci-python-hermetic` 集合（包含打包）**1,869 passed、1,199 deselected**。相关真实 PostgreSQL/契约集合初次 **208 passed、2 failed**，两项失败为测试调用尚未改为严格 `PriorBatch` 结果；更新调用后，该文件 **22 项全部通过**。日统计真实 PostgreSQL证明另 **1 passed**。以上集合有重叠，不相加。适配器仍在继续优化，最终提交会重新执行受影响集合。

原 A CI 的文档导航遗漏和旧测试假设已修复并推送至 `f3f8483fc`；随后独立嵌入路由与性能观测调整仍在工作树中，最终提交将重新核实必需 CI，不将旧 HEAD 的结果视作最终证明。

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
- 通过 20 条重复链、完整 24 h 只读重放、50 条上下文盲评及关系对减量的联合约束；不能只满足种子集召回率。
- 修复已获 owner 批准的 PR-A 读者重问暴露的重复抑制与上下文污染，证明重点重复抑制、新事实不降级、误锚 ≤1 和信息流不误升级。
- 性能仅作为观测：17,500 个当前命题及 2,000 个超过 7 d 的已送冻结版本，最新安全投影优化后的容器内网络首读 374.929 / 123.788 ms、随后 40 次 p95 489.019 / 140.150 ms，未达到原预算；WSL 转发路径还有额外开销。遵照 owner 后续调整，不再为追求这两个数字继续阻止业务交付。
- 重新提交工程修复并核实当前 HEAD 的必需 CI。

PR-A 保持 draft，直到这些验收完成。PR-C 与 PR-B 是 Issue 指定的独立交付，本报告不关闭 #791。
