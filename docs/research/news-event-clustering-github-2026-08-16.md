# 新闻“同一事件”识别：GitHub 成熟方案、向量化与 Tracefold 落地评估

> 调研日期：2026-08-16（Asia/Shanghai）
> 来源范围：官方 GitHub 仓库、官方项目文档、原始论文、官方模型卡；不使用二手博客作为结论依据。
> 仓库活跃度口径：GitHub API 的 `pushed_at` 快照与仓库公开元数据，只是维护信号，不等于质量或支持承诺。Stars 也只作为采用度信号。
> 目标问题：这是 RAG 问题吗？是否存在可直接采用的成熟库？是否应给每条新闻生成 embedding？什么架构最适合 Tracefold？

## 1. 结论先行

1. **这不是 RAG 问题。** 同一事件识别的主体是 streaming entity resolution / record linkage / near-duplicate detection / pairwise classification / graph closure。只有后续把检索出的 Story 证据交给生成模型写 Brief、回答查询时，才进入 RAG。原始 RAG 定义是“从外部非参数记忆检索，再以检索结果条件化生成”；仅使用 embedding 或向量索引不构成 RAG，见 [Lewis et al., 2020](https://arxiv.org/abs/2005.11401)。

2. **截至本次检索，没有发现一个维护活跃、许可清晰、可直接承担“跨语言硬新闻同一具体事件 + 在线增量 + 强事实 veto + 稳定可审计 Story ID”的端到端开源库。** 最接近的是 Priberam 的研究实现，但老版本包含未发布的专有特征训练部分，新版本是 research-only license，且最后 push 分别在 2022/2023 年，不能作为 Tracefold 的生产依赖。

3. **成熟组件是存在的，而且边界非常清楚：**

   - 近重复候选召回：[`datasketch`](https://github.com/ekzhu/datasketch) 的 MinHash/LSH；
   - 多语言 embedding、pair reranker/分类器训练：[`sentence-transformers`](https://github.com/huggingface/sentence-transformers)；
   - PostgreSQL 内向量存储与 exact/ANN 检索：[`pgvector`](https://github.com/pgvector/pgvector)；
   - 大规模内存 ANN：[`FAISS`](https://github.com/facebookresearch/faiss) 或 [`hnswlib`](https://github.com/nmslib/hnswlib)；
   - 结构化 pair learning / blocking 的设计参考：[`dedupe`](https://github.com/dedupeio/dedupe)、[`Splink`](https://github.com/moj-analytical-services/splink)；
   - 在线“主题”聚类：[`River`](https://github.com/online-ml/river)、[`BERTopic`](https://github.com/MaartenGr/BERTopic)，但它们解决的是 topic/narrative，不是严格 same-event identity。

4. **建议为每条进入 Story 计算范围的 `news_item` 生成一个版本化的多语言 title embedding，但第一阶段只作为候选召回和 shadow 特征，不直接决定 Story membership。** 原因是它能显著补足中英复述、同义改写和短标题召回；但通用 embedding 的高 cosine 只表示语义接近，不保证同一事件，尤其容易把“同公司/同人物/同地区/同冲突的不同进展”合并。

5. **向量不是替代事实兼容性，而是增加一条召回通道。** 推荐最终判定至少包含：词法相似、embedding 相似、时间差、actor/asset/action/target/location/amount/time 等结构化事实，以及硬冲突 veto。SemEval 2022 的新闻相似度研究也显示，时间、地点、实体、数量是独立维度；纯语义模型容易高估同实体或同话题的不同事件，见 [SemEval-2022 Task 8 总结](https://aclanthology.org/2022.semeval-1.155/) 与 [GateNLP-UShef 实体增强模型](https://aclanthology.org/2022.semeval-1.158/)。

6. **Tracefold 当前规模不需要引入 Qdrant/Milvus。** 已有一个 PostgreSQL truth store，`pgvector` 可在同一事务和同一数据治理边界内提供 exact cosine；在活跃窗口最多约 10,000 Items 的约束下，甚至可以先离线/worker 内向量化 exact top-k 做 shadow。只有经过量显示 exact search 成为瓶颈，再启用 HNSW；不要先增加一个第二数据库。

## 2. 先把四类问题分开

| 层 | 输入与输出 | 典型技术 | 它能回答什么 | 它不能回答什么 |
|---|---|---|---|---|
| Candidate retrieval | 一个新 Item → 少量可能相关的旧 Items/Stories | 词法倒排、MinHash/LSH、embedding + exact/ANN | “哪些对象值得进一步比较？” | “它们一定是同一事件吗？” |
| Pairwise same-event decision | 一对 Item/Item 或 Item/Story → match / non-match / score + reason | 规则、logistic/GBDT、CrossEncoder、事实 veto | “这对记录是否描述同一具体事件？” | “整批记录最终怎样闭包？” |
| Cluster closure / identity | 已接受的 pair/anchor decisions → Story membership 与稳定 ID | fixed-anchor、complete-link、受约束图、时间窗 | “哪些 Item 构成同一个可发布 Story？” | “怎样从百万向量快速召回候选？” |
| RAG retrieval + generation | 用户问题/Brief 任务 → 检索证据 → 生成文本 | retriever + LLM | “怎样基于已组织证据生成回答/摘要？” | “源 Item 应属于哪个权威 Story？” |

`FAISS`、`pgvector`、Qdrant、Milvus 只提供第一层的相似向量检索。`sentence-transformers` 同时提供第一层的 bi-encoder 与第二层可训练的 CrossEncoder。`dedupe`/Splink 覆盖 blocking、pair scoring 和某种 closure，但其数据与业务假设不是新闻事件。任何一个组件都不是完整 Story identity。

官方 Sentence Transformers 文档也明确把 bi-encoder 描述为两阶段检索的第一步，再用 CrossEncoder 对 top-k 重排；CrossEncoder 全对比较不可扩展。见 [Sentence Transformer usage](https://github.com/huggingface/sentence-transformers/blob/main/docs/sentence_transformer/usage/usage.rst)、[CrossEncoder application note](https://github.com/huggingface/sentence-transformers/blob/main/examples/cross_encoder/applications/README.md)。

## 3. 最接近“新闻同事件”的一手方案

### 3.1 Priberam：Multilingual Clustering of Streaming News（最重要的架构参考）

仓库：[`Priberam/news-clustering`](https://github.com/Priberam/news-clustering)
论文：[`Multilingual Clustering of Streaming News`](https://aclanthology.org/D18-1483/)
快照：38 stars；最后 push 2022-05-02；BSD-3-Clause；Python；仓库 README 已声明被下一个项目 supersede。

它真正解决的是持续新闻流中不断出现新簇的在线问题，而不是预先知道 `k` 的普通聚类。核心流程是：

1. 对新文档计算多种内容与时间特征；
2. 在活跃 cluster pool 中选择最适合的 cluster；
3. 用训练过的分类器判断加入最佳 cluster，还是创建新 cluster；
4. 对单语言 Story 再建立跨语言 Story 联系。

论文报告该方法用 SVM 学习各相似度特征权重与“是否新建 cluster”，显著优于纯无监督流式基线；时间特征的尺度也通过开发集校准。重要的是，它不是“cosine 超过阈值就 union”。

限制：仓库 README 明确说原论文使用了 Priberam 的专有软件，公开仓库只是 Python 重实现；部分 feature extraction 和 SVM training code 不能发布，只提供提取好的 features 和预训练模型。因此它适合复现论文思路，不适合作为完整、可持续维护的运行时依赖。

### 3.2 Priberam：Projected News Clustering（最接近当前跨语言需求）

仓库：[`Priberam/projected-news-clustering`](https://github.com/Priberam/projected-news-clustering)
论文：[`Simplifying Multilingual News Clustering Through Projection From a Shared Space`](https://arxiv.org/abs/2204.13418)
快照：12 stars；最后 push 2023-02-07；C#；许可证仅允许 research use，商业使用需联系 Priberam。

这是本次调研中最接近 Tracefold 问题定义的实现：

- 用共享多语言 embedding 直接表示不同语言的文档，不先建立语言隔离的簇；
- 对每个新文档，先用 ranking classifier 选择最佳 cluster；
- 再用 merge classifier 决定“加入该簇”还是“新建簇”；
- 另有 cluster-join classifier 修复早期被分开的紧邻多语言 clusters；
- 特征不仅有 document/centroid cosine，还包含 oldest/newest/relevance timestamp、cluster density、mean similarity、title/paragraph representations；
- cluster state 与 document assignment 可持久化。

论文称多语言 contextual embedding 显著改善聚类质量，并在相应新闻流数据上取得当时的 state of the art。相关 SELMA 一手交付文档还记录了实际部署组件支持 50 种语言、在 25,000 个 cluster pool 下平均约 3.2 documents/s，但也明确指出单流 cluster state 形成扩展瓶颈，见 [SELMA D2.3](https://selma-project.eu/wp-content/uploads/2022/06/D2.3-Initial-release-of-segmentation-summarization-and-news-classification-capabilities.pdf)。

为何不能直接采用：

- research-only license，不适合商业生产复用；
- 2023 年后无 push；
- 训练依赖旧的 SVMRank/liblinear 工作流和固定模型文件；
- 其 online centroid membership 会受到到达顺序和早期误判影响；
- 没有 Tracefold 所需的强事实 veto、可解释 identity evidence 与确定性重建契约。

**可借鉴的不是代码，而是模块拆分：`representation → rank candidate clusters → merge-or-new → cluster repair`。**

### 3.3 Story Forest（生产研究经验，不是可直接引入的库）

论文：[`Growing Story Forest Online from Massive Breaking News`](https://arxiv.org/abs/1803.00189)
可运行研究实现之一：[`SocialED` 中的 EventX](https://socialed.readthedocs.io/en/1.1.5/_modules/SocialED/detector/eventx.html)

Story Forest 来自腾讯真实新闻流经验，处理 60 GB 中文新闻。其关键经验与本问题高度一致：

- 先用 keyword community 把全局搜索空间切成粗主题；
- 再在主题内部使用监督式 document-pair relationship classifier；
- 最后在 document graph 上做 event clustering；
- `event`（同一具体事件报道）和 `story`（一组相关事件的演化）是两个不同实体。

论文明确认为仅无监督相似度不足以得到高纯度 event cluster，并通过第二层 pair classifier 提升 homogeneity。它同时提醒：长期“故事演化”和同一“事件节点”不应混成一个 identity。

### 3.4 newsLens 与 2020 多语言扩展（不要混淆 Story 粒度）

论文：[`newsLens: building and visualizing long-ranging news stories`](https://tingofurro.github.io/pdfs/ACL2017_NewsLens.pdf)；[`Batch Clustering for Multilingual News Streaming`](https://arxiv.org/abs/2004.08123)。

newsLens 在 4M 英文 articles 上用局部 keyword graph + Louvain communities，再把局部 topics 跨时间连接为长期 stories。论文明确展示 connected component 会被少量错误边串起来，因此改用 community detection。但它的 Story 可以跨数月/数年，本质上更接近 narrative/topic，而非 Tracefold 截图里要求的“同一条具体新闻事件”。

2020 扩展采用分批单语言 local topics、跨 batch replay、以及经 triplet 训练的多语言 SBERT，再跨语言关联 stories。它证明 embedding 对跨语言有帮助，也证明完整系统仍需时间窗口、局部图和匹配策略，而不是一个全局 cosine threshold。

### 3.5 SemEval 2022 Task 8（pairwise 评估与训练语料参考）

任务论文：[`SemEval-2022 Task 8: Multilingual news article similarity`](https://aclanthology.org/2022.semeval-1.155/)
第一名代码：[`GeekDream-x/SemEval2022-Task8-TonyX`](https://github.com/GeekDream-x/SemEval2022-Task8-TonyX)
实体增强第二名代码：[`iknoorjobs/semeval-code`](https://github.com/iknoorjobs/semeval-code)

数据集包含近 10,000 对新闻，覆盖 18 种语言组合，并分别标注 geography、entities、time、narrative、style、tone 等相似维度。最佳系统总体相关系数约 0.818，仍低于人工一致性。这是很有价值的预训练/外部验证参考，但它输出连续 similarity，不是二元 same-event identity，也没有 cluster closure。

GateNLP-UShef 的结果尤其有启发：LaBSE 类语义表示再加入 organization/location/date/quantity 特征，能减少“同实体、不同事件”的高估。其论文错误分析明确给出同一国家、同一 COVID 主题但不同政策事件被模型高估的例子。因此 Tracefold 不应把通用 cosine 直接当 must-link。

代码成熟度有限：两组代码均停留在 2022 年研究复现；GateNLP 仓库无明确 license，HFL 代码为 Apache-2.0，但都不是服务库。

### 3.6 当前活跃应用仓库：只能参考模式，不能当成熟库

- [`grregis/MuckScraper`](https://github.com/grregis/MuckScraper)：129 stars，最后 push 2026-08-14，未发现明确 license。其 `story_grouper.py` 采用 title overlap → pgvector/cosine → 边界区间 LLM review 的三级流程。它证明“高精度 lexical + dense recall + expensive verifier”是现实应用中的自然组合，但阈值和 prompt 是单应用经验，且没有确定性闭包或可复现 identity。
- [`Thysrael/Horizon`](https://github.com/Thysrael/Horizon)：约 8.8k stars，最后 push 2026-08-16，MIT。它先按 URL 合并，再把当批所有标题、tags、summaries 交给 LLM 输出 duplicate groups；失败则保留原 Items。适合一次性 digest，不适合稳定、可重建、全局权威 Story。
- [`fedecaccia/Online-News-Clustering`](https://github.com/fedecaccia/Online-News-Clustering)：36 stars，最后 push 2022-03-16，无明确 license；包含 Python 2/旧 Dragnet 路径，TF-IDF centroid 增量聚类。历史参考价值高，当前不应采用。
- [`vslaykovsky/news_deduplication`](https://github.com/vslaykovsky/news_deduplication)：9 stars，2 commits，最后 push 2021-03-22，无明确 license；弱监督 RoBERTa notebook，不是成熟运行时。

## 4. 成熟通用库逐项评估

### 4.1 总表

| 候选 | 2026-08-16 GitHub 信号 | License | 主要层 | 在线/批处理 | must-link / cannot-link | 是否需要向量 | 可复现性 | Tracefold 适合度 |
|---|---:|---|---|---|---|---|---|---|
| [`sentence-transformers`](https://github.com/huggingface/sentence-transformers) | ~19k stars；push 2026-08-14 | Apache-2.0；模型各自另算 | embedding、pair reranker、训练 | inference 可流式/批量 | 可用正负 pair 训练；不提供运行时硬约束闭包 | 是 | pin 模型 revision/runtime 后较好 | **高：推荐特征与模型工具箱** |
| [`datasketch`](https://github.com/ekzhu/datasketch) | ~3.0k；push 2026-08-09 | MIT | lexical candidate retrieval | 支持 insert/query 与 Redis/Cassandra | 无 | MinHash sketch，不是 dense vector | pin seed、scheme、num_perm；LSH 本身近似 | **高：近重复召回可选；当前规模未必需要** |
| [`pgvector`](https://github.com/pgvector/pgvector) | ~22.6k；push 2026-08-15 | PostgreSQL License | exact/ANN vector retrieval | PostgreSQL 在线事务 | 无 | 是 | exact search 强；ANN 需测 recall | **高：若持久化 embedding，首选索引** |
| [`FAISS`](https://github.com/facebookresearch/faiss) | ~40.7k；push 2026-08-15 | MIT | 大规模内存 similarity search/k-means | batch 优先，可 add | 无 | 是 | Flat exact；IVF/HNSW/并行近似结果需固定配置 | 中：shadow benchmark/大规模候选检索 |
| [`hnswlib`](https://github.com/nmslib/hnswlib) | ~5.3k；push 2026-03-28 | Apache-2.0 | ANN | 支持 insert/update/delete | 无 | 是 | 图受插入顺序/seed/线程影响 | 中低：pgvector 已覆盖 HNSW |
| [`Qdrant`](https://github.com/qdrant/qdrant) | ~34k；push 2026-08-15 | Apache-2.0 | 独立向量数据库 | 在线服务 | 无 | 是 | 可版本化，但多一套状态与运维 | 低：当前违反单 PostgreSQL 边界 |
| [`Milvus`](https://github.com/milvus-io/milvus) | ~45.6k；push 2026-08-15 | Apache-2.0 | 分布式向量数据库 | 实时更新/大规模 | 无 | 是 | 索引参数/分布式执行需管理 | 很低：规模和运维都过度 |
| [`dedupe`](https://github.com/dedupeio/dedupe) | ~4.5k；push 2025-07-29 | MIT | blocking + pair learning + clustering | 中小批处理；Gazetteer 可查询 | `match/distinct` 是训练标签，不是硬运行时约束 | 不要求；结构化 features | 保存 settings/labels 后较好 | 中高：pair-learning 与 clustering 设计参考 |
| [`Splink`](https://github.com/moj-analytical-services/splink) | ~2.3k；push 2026-08-13 | MIT | probabilistic record linkage | SQL 批处理，可到 100M records | labels 可评估/训练；无硬约束 closure | 不要求 | 保存模型参数与 SQL 后较好 | 中：可原型化概率 pair model；closure 不宜照搬 |
| [`recordlinkage`](https://github.com/J535D165/recordlinkage) | ~1.1k；push 2024-02-21 | BSD-3 | indexing/comparison/classification | pandas 批处理 | 可监督分类；无硬约束 closure | 不要求 | 配置固定后较好 | 中低：成熟但维护较慢、规模与流式不匹配 |
| [`River`](https://github.com/online-ml/river) | ~5.9k；push 2026-08-12 | BSD-3 | online ML / stream clustering | 原生 online | 无事件事实 hard constraints | 通常输入数值向量 | 严格依赖输入顺序与历史 state | 低：可做 narrative/topic，不做权威 Story |
| [`BERTopic`](https://github.com/MaartenGr/BERTopic) | ~7.8k；push 2026-08-02 | MIT | topic modeling | batch；支持 partial_fit 组合 | 无 pair hard constraints | 通常是 | 默认 UMAP 随机；需 random_state | 很低：粒度是 topic，不是 same event |
| [`SocialED`](https://github.com/RingBDStack/SocialED) | ~596；push 2025-07-27 | BSD-2 | 社交事件检测研究工具箱 | 含 online/offline 19 算法 | 部分模型有 pairwise loss，非统一硬约束 API | 多数模型需要 | 模型/图/数据依赖复杂 | 低：benchmark 参考，不是硬新闻 prod lib |
| [`scikit-learn`](https://github.com/scikit-learn/scikit-learn) | ~67k；push 2026-08-13 | BSD-3 | clustering/classification 基础件 | 多为 batch；部分 incremental | connectivity 不是 must/cannot-link 语义 | 可稀疏或 dense | pin seed/version 后较好 | 中：分类器、指标、prototype 基础件 |

### 4.2 `sentence-transformers`：最值得引入，但要放对位置

官方仓库支持：

- SentenceTransformer bi-encoder：每段文本独立生成 fixed-size embedding；
- CrossEncoder：成对输入并输出 score/classification；
- semantic search、paraphrase mining、community detection；
- 正负 pairs/triplets、hard-negative mining 与自定义 fine-tuning。

它适合 Tracefold 的两个位置：

1. **多语言 candidate retrieval**：把新 title 编码后找 top-k 相似旧 Items/anchors；
2. **有标注数据后的 pairwise verifier**：用真正的 same-event / hard-negative pairs 微调 multilingual CrossEncoder 或 bi-encoder。

它不负责：时间窗、事实冲突、Story ID、cluster membership closure、DB publication。

官方 `community_detection()` 只把 cosine 高于阈值的局部邻域当 community；这是工具函数，不含事件事实和跨批身份契约。官方 `ParaphraseMiningEvaluator` 甚至提供 `add_transitive_closure` 选项，反而说明 pair duplicate 与 cluster closure 是两个需要显式选择的步骤。见 [`community_detection` 文档](https://www.sbert.net/docs/package_reference/util/retrieval.html) 和 [`ParaphraseMiningEvaluator`](https://sbert.net/docs/package_reference/sentence_transformer/evaluation.html)。

### 4.3 `datasketch`：适合 lexical near-duplicate candidate generation

`MinHash` 估算集合 Jaccard，`MinHashLSH` 做 threshold candidate query，`MinHashLSHEnsemble` 更适合 containment。官方文档明确指出：

- LSH 返回的是近似 candidates，可能包含阈值下结果，也可能漏掉阈值上结果；
- 应再次用 MinHash 或原始集合 exact Jaccard 过滤；
- seed、`num_perm`、hash scheme 必须一致；2.0 默认 scheme 改动时持久索引需要重建；
- 支持 Redis/Cassandra，但这不代表应在 Tracefold 再引入新 truth store。

见 [`datasketch` API](https://ekzhu.com/datasketch/documentation.html) 与 [MinHash LSH 文档](https://ekzhu.com/datasketch/lsh.html)。

适用判断：如果未来活跃窗口远大于 10,000 Items、词法倒排候选量成为瓶颈，可用它补 near-duplicate retrieval；当前现有 token/signature indexes 已经有 250k candidate 上限，先修 tokenizer 与事实抽取的收益更高。

### 4.4 `pgvector`、FAISS、hnswlib、Qdrant、Milvus：它们是索引，不是判定器

[`pgvector`](https://github.com/pgvector/pgvector) 默认 exact nearest-neighbor search，提供 perfect recall；HNSW/IVFFlat 以 recall 换速度。官方 README 明确说加入 ANN index 后查询结果可能变化。它还能把 vector 与事实行放在同一个 PostgreSQL 中，支持事务、备份、过滤和 SQL explain，最符合 Tracefold 的一个 PostgreSQL 边界。

[`FAISS`](https://github.com/facebookresearch/faiss) 是最成熟的大规模向量搜索/聚类基础库之一，支持 Flat exact、IVF、HNSW、PQ、GPU，但官方 FAQ 说明它偏好 batch query，concurrent search/add 需要调用方加锁。适合离线 benchmark、大规模内存 candidate service，不适合直接嵌入唯一 Story writer 后宣称获得事务语义。

[`hnswlib`](https://github.com/nmslib/hnswlib) 更小，支持 insert/update/delete；但当 PostgreSQL 已可通过 pgvector HNSW 覆盖同一需求时，再维护独立 index 文件没有明显收益。

[`Qdrant`](https://github.com/qdrant/qdrant) 与 [`Milvus`](https://github.com/milvus-io/milvus) 都是成熟生产向量数据库。Qdrant 强于 payload filtering；Milvus 面向十亿级、分布式/Kubernetes。它们的能力真实，但会引入第二份服务状态、备份、重建、版本迁移和一致性边界；对当前 bounded window 是架构负收益。

### 4.5 `dedupe`：最有价值的传统 record-linkage 参考

`dedupe` 对结构化 records 学习：

- blocking/fingerprinting rules；
- pairwise classifier；
- active learning 的 `uncertain_pairs()`；
- 人工标签 `match` / `distinct`；
- 最后使用 cophenetic threshold 的层次聚类，而非简单 pair threshold connected-components。

官方 API 说明 `partition()` 仅适用于小到中等数据；大数据需要自行生成 pairs 再交给 `score()`。[仓库源码 `api.py`](https://github.com/dedupeio/dedupe/blob/main/dedupe/api.py) 还显示 `distinct` 是训练数据，最终运行时没有把每一个 labeled negative 当不可违反的 hard constraint。

它对 Tracefold 的启示：

- 标注最不确定 pair，比盲目增加规则更有效；
- blocking 和 pair classifier 应独立评估；
- closure 不应使用一条弱边就把整个 component union。

但新闻 title 的多语言语义、时间演化和事件事实需要自定义 fields/comparators；直接把 `news_item` 塞入 dedupe 并不能获得成熟新闻模型。

### 4.6 Splink / recordlinkage：可做 pair model prototype，不应接管 Story closure

[`Splink`](https://github.com/moj-analytical-services/splink) 是活跃、可扩展到 100M records 的 probabilistic record linkage 包，基于 Fellegi-Sunter，支持 blocking、term-frequency adjustments、自定义 comparisons、labels/evaluation。它的官方 clustering API 把阈值以上 pair predictions 做 connected components，见 [Splink clustering](https://moj-analytical-services.github.io/splink/api_docs/linker_clustering.html)。这会重现 same-event 中最危险的桥接传递：A≈B、B≈C、A 与 C 冲突仍可能进入一个 component。

[`recordlinkage`](https://github.com/J535D165/recordlinkage) 提供 pandas DataFrame 上的 indexing、comparison vectors、监督/无监督 classifiers，接口清晰，但最后 push 在 2024，且不提供在线 event closure。

两者可以快速验证“actor match、amount match、time delta、lexical score、embedding cosine”等 feature 是否能学习出比手工阈值更好的 pair probability；不可直接用默认 closure 生成权威 Story。

### 4.7 River / BERTopic：成熟，但问题粒度不对

[`River`](https://github.com/online-ml/river) 是成熟的 online ML 库，包含 DBSTREAM、DenStream、CluStream 等流式聚类并支持 `learn_one()`。其官方 README 也提醒多数场景 batch learning 已足够，online 算法适合 concept drift。它的 cluster state 天生依赖输入顺序，目标通常是密度/漂移，不知道“同事件必须共享什么事实”。

[`BERTopic`](https://github.com/MaartenGr/BERTopic) 的标准管线是 embedding → dimensionality reduction → clustering → c-TF-IDF topic representation。官方在线教程通过 IncrementalPCA/MiniBatchKMeans 或 River DBSTREAM 实现 `partial_fit`，并明确 online 时只跟踪最新 batch 的限制。默认 UMAP 具有随机性，需设置 `random_state` 才可复现，见 [BERTopic online](https://maartengr.github.io/BERTopic/getting_started/online/online.html) 与 [reproducibility best practice](https://maartengr.github.io/BERTopic/getting_started/best_practices/best_practices.html)。

它们可以做另一个产品：`narrative/topic radar`、故事演化、热点发现；不能替换具体事件 Story identity。把两者混用会把“OpenAI 数据中心投资”“OpenAI 模型发布”“OpenAI 监管新闻”聚成主题，但这不是截图要求的 same event。

### 4.8 Must-link / cannot-link 库为何仍不直接适用

成熟度较低的 [`COP-Kmeans`](https://github.com/Behrouz-Babaki/COP-Kmeans) 确实接受 must-link/cannot-link，但它要求预先给出固定 `k`、batch matrix 和 K-means 几何；新闻流 cluster 数不断增长，且事件大小极端不均衡。scikit-learn 的 Agglomerative `connectivity` 只是规定哪些邻居允许合并，也不是业务 cannot-link。

对 Tracefold 更合适的做法是把强事实冲突保留为 closure 层的硬 veto：无论 embedding 或 pair model 多高，互相冲突的 amount/time/action/actor/target 都不能进入同一 authoritative cluster。无需为了“有 constrained clustering 名称”引入粒度不匹配的算法。

## 5. 应否给每条新闻生成向量

### 5.1 推荐答案

**应该生成，但应定义为版本化、可重建的 derived feature，而不是 source fact，也不应在第一阶段成为 Item admission 或 Story publication 的同步依赖。**

建议范围：

- 对每个进入 Story 活跃窗口的 accepted `news_item`，按 `(item_id, input_fingerprint, embedding_model_revision)` 至多生成一次；
- 输入优先使用去来源尾缀但保留否定、数字、单位和实体的原始自然语言 title；
- 若可靠 lede/summary 可用，保存第二个 representation 或明确的 `title + lede` 版本，不要默认把整篇正文直接拼到 title；
- original title、comparison title、title+lede 不应共享一个不透明 embedding 版本名。

生成每条 Item embedding 的收益：

1. 中英/多语言复述能进入同一候选集；
2. 短电讯标题与长报道标题不再完全依赖 token overlap；
3. 可系统性挖掘 lexical rule 漏掉的 hard positives；
4. 可进行 candidate recall@k shadow evaluation；
5. 后续 Search/Related coverage 也能复用同一版本化 feature。

主要风险：

1. 通用模型学到的是 semantic/topic similarity，不是同一事件；
2. 模型更换会让 score distribution 与 threshold 漂移；
3. 异步 embedding readiness 若直接影响 membership，会造成“事实没变、只因向量晚到而 Story 变化”；
4. GPU/ONNX/量化版本可能带来细小数值差异；
5. embedding 会增加存储、模型下载、CPU/GPU、backfill 和版本迁移成本；
6. 文章正文包含背景段落，会把同一长期主题的不同进展拉近。

### 5.2 向量输入应该是什么

推荐依次试验：

1. `clean_original_title`：最贴近当前 Story 事实边界；
2. `clean_original_title + short provider summary/lede`：标题歧义时提供事件动作；
3. title 和 lede 分别 embedding，pair classifier 分别使用两组 cosine；
4. 不建议第一版对完整 article body 做单一 mean embedding。

理由：长正文往往重复历史背景、公司介绍、相关冲突与模板化版权文本。SemEval 参赛系统也报告 scraper banner/cookie/copyright 噪声会严重扭曲 similarity；全正文还会被模型最大 token 长度截断。标题 identity 与长文 semantic search 应视为两个不同 representation products。

### 5.3 候选模型

以下只是 shadow shortlist，不能凭通用 leaderboard 直接选生产模型：

| 模型 | 官方模型卡 | License | 维度/语言 | 优点 | 风险 |
|---|---|---|---|---|---|
| `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` | [模型卡](https://huggingface.co/sentence-transformers/paraphrase-multilingual-mpnet-base-v2) | Apache-2.0 | 768；50+ languages | symmetric paraphrase/similarity 目标，直接适合 title-title pilot | 不是新闻同事件训练；约 0.3B 参数 |
| `intfloat/multilingual-e5-base` | [模型卡](https://huggingface.co/intfloat/multilingual-e5-base) | MIT | 768；约 100 languages | 训练数据含 CC News、NLLB、中文 DuReader；生态成熟 | 主要是 retrieval 训练，需正确 prefix；score 分布集中，阈值不能照搬 |
| `BAAI/bge-m3` | [模型卡](https://huggingface.co/BAAI/bge-m3) | MIT | 1024；multilingual；8192 tokens | dense/sparse/multi-vector 能力，长文本与中英覆盖强 | 更重；通用 retrieval 不是 event identity |
| `jinaai/jina-embeddings-v3` | [模型卡](https://huggingface.co/jinaai/jina-embeddings-v3) | CC-BY-NC-4.0 | 1024；94 languages | 多任务与长文本能力 | **非商业许可，不应作为生产默认** |

第一轮推荐同时测前三个，以 cross-language candidate recall、hard-negative precision、CPU latency 和存储成本选择；不因 MTEB 总分直接决定。

### 5.4 存储与成本量级

纯 float32 raw vector：

- 768 维约 `768 × 4 = 3,072 bytes / Item`；
- 1024 维约 `4,096 bytes / Item`；
- 100 万 Items 的 raw vectors 约 3.1 GB 或 4.1 GB，尚未含行、TOAST、HNSW graph 和索引开销；
- half precision 约减半，但需要验证 recall 与 score threshold 稳定性。

对 Tracefold 当前 10,000 行 bounded projection，768 维 raw vectors 约 30 MB 量级；candidate top-k exact cosine 在工程上完全可以先 benchmark，不必一开始上分布式向量数据库。

建议 embedding 行至少包含：

```text
item_id
input_fingerprint
representation_kind        # title | title_lede
model_id
model_revision_or_sha
tokenizer_revision
dimension
dtype
normalized                 # true/false
vector
created_at
status / failure_code
```

模型升级必须新增 version/backfill，不应原位覆盖后让历史 score 失去解释。

## 6. 推荐的生产架构

### 6.1 不是“全部改成向量聚类”，而是多通道召回 + 单一权威判定

```text
accepted news_item
      |
      +--> exact atom / canonical URL-title identity --------+
      |                                                       |
      +--> lexical + strong-signature candidates -------------+--> candidate union
      |                                                       |        |
      +--> multilingual embedding exact/ANN top-k ------------+        v
      |                                                       pair feature builder
      +--> optional MinHash containment candidates -----------+        |
                                                                       v
                                                         same-event verifier
                                             lexical + dense + time + strong facts
                                             hard fact conflicts remain vetoes
                                                                       |
                                                                       v
                                                        deterministic closure
                                              fixed-anchor / accumulated signature
                                                                       |
                                                                       v
                                                        one Story projection/publication
```

核心原则：

- embedding 扩大 candidate recall；
- pair verifier 决定是否同一具体事件；
- closure 决定整组 membership，不能由 ANN index 或 client UI 隐式决定；
- 客户端只渲染服务端 Story；
- Related Topic / Narrative 可以另建非权威关系，不污染 Story ID。

### 6.2 Pair verifier 的建议演进

**阶段 A：透明 feature model。**

先使用 logistic regression 或小型 GBDT，输入：

- exact atom / title fingerprint；
- Jaccard、containment、char n-gram similarity；
- multilingual embedding cosine 与 rank；
- provider time delta / effective time overlap；
- actor、target、asset、action、instrument、amount、location、period 各自的 match/unknown/conflict；
- title 长度与语言组合；
- source relationship 仅作弱特征，不作 identity 事实。

优点是 score 可拆解、训练快、CPU 便宜，适合先确认“当前 false conflicts 与跨语言漏召回能否被修复”。`dedupe`/Splink 的 active labeling、blocking 与概率校准经验可用于这一阶段，但不需要采用其 closure。

**阶段 B：task-specific CrossEncoder。**

只有当阶段 A 的 residual errors 明确来自复杂复述，才用 Sentence Transformers fine-tune multilingual CrossEncoder。CrossEncoder 只对 top-k candidates 运行，不做 all-pairs。其输出仍需经过 strong-fact veto，并记录 model revision 与 score。

**不要把通用 LLM prompt 作为第一权威判定器。** 它可用于标注辅助、borderline review 或解释，但供应商/模型升级、temperature、超时和 prompt 变化会破坏确定重建。

### 6.3 Cluster closure

不建议默认 connected components。Splink 的默认 closure 和很多相似图 demo 都会产生桥接误合并。可选择：

- 保留 Tracefold fixed-anchor，每个成员必须与 anchor 通过判定；
- 进一步要求新成员与 accumulated strong signature 不冲突；
- 对高风险领域使用 complete-link/cluster-level minimum compatibility；
- 对 score tie 保持 singleton 或显式 unresolved；
- 跨语言 cluster repair 应在固定窗口内重新投影，并以同一 deterministic function 产生结果，而不是后台就地 mutation。

Priberam 的 cluster-join classifier 值得用于 shadow 对比，但其 online mutable pool 不应直接替代可重建 projection。

## 7. 生产验证方案

### 7.1 数据集不能只用同一批手写案例调阈值又验收

至少建立四层 corpus：

1. **真实线上连续采样**：按时间窗抽样 candidates，并保留原始来源比例；
2. **hard positives**：中英复述、长短标题、编辑/重发、数字单位等价、同事件更新角度；
3. **hard negatives**：同公司不同公告、同人物不同发言、同冲突不同地点/伤亡/日期、上涨与下跌、否认与确认；
4. **截图回归集**：Nvidia、Qatar 等用户发现的真实错误必须永久进入 holdout/regression。

训练、阈值开发、最终 holdout 应按真实 event 与时间切分，不能让同一事件的不同文章泄漏到 train/test 两边。

SemEval 2022 可用于外部 multilingual sanity check；[`WCEP`](https://github.com/complementizer/wcep-mds-dataset) 提供 event/article clusters，可用于 cluster-level实验。但外部数据的 event 定义、文章版权、时间跨度与 Tracefold 域并不相同，最终门禁必须依赖 operator domain 的独立标注。

### 7.2 每层分别验收

**Candidate retrieval：**

- same-event candidate recall@K；
- cross-language recall@K；
- short-title recall@K；
- average / p95 candidates per Item；
- exact lexical、dense、hybrid 各自的独立贡献；
- embedding availability/failure coverage。

**Pair verifier：**

- precision、recall、PR-AUC 与 calibration；
- false-positive rate 单独作为 guardrail；
- 按语言组合、来源、标题长度、事件族切片；
- unknown facts 与 conflict facts 分开统计；
- hard-negative precision 必须高于普通随机负样本。

**Closure：**

- pairwise precision/recall；
- B-cubed precision/recall/F1；
- homogeneity/completeness/V-measure；
- 最大 cluster size、单边 bridge 数、cluster conflict 数；
- anchor 退出/迟到 Item/重新构建时的 ID churn；
- 同一 snapshot 多次重建 byte-for-byte membership 一致。

**运行时：**

- embedding worker lag、backlog、failure reason；
- model load/RSS/CPU/GPU/ONNX latency；
- vector query p50/p95 与 recall；
- model version rollout/backfill 的 partial-coverage 可见性；
- embedding provider 不可用时，不应让 source Item ingestion 或 Push 失效。

## 8. 对 Tracefold 的分阶段建议

### Phase 0：不改权威 Story，建立测量基线

- 从真实 `news_items` 建独立标注 corpus；
- 把现有 lexical candidate、pair decision、fact conflict reasons 全量导出到离线评估；
- 对 screenshot failures 建 regression fixtures；
- 明确 same event 与 related narrative 的产品定义。

### Phase 1：embedding shadow

- 引入 `sentence-transformers` 作为独立 worker/tooling；
- 对活跃 Items 生成版本化 title embeddings；
- 先用 vectorized exact cosine 或 pgvector exact search；
- 仅记录 top-k 与 score，不改变 Story membership；
- 比较 3 个许可合适的多语言模型；
- 验证 cross-language candidate recall 与 hard-negative 分布。

### Phase 2：hybrid candidates，但仍由现有规则判定

- candidate union 加入 embedding top-k；
- existing strong-fact veto 与 fixed-anchor closure 不变；
- 这一步只能提升“有机会被比较”的 recall，不应直接降低 acceptance threshold；
- 观察 Nvidia/Qatar 类 pair 是“未进入候选”还是“进入后被错误否决”。

### Phase 3：学习 pair verifier

- 用人工 labels 训练透明 classifier；
- 优先修正当前 actor/amount/time 抽取的 false conflict；
- 在 shadow 中与现有规则并行跑，按错误族做 ablation；
- 达到独立 holdout 质量门槛后，才提出新 identity version 的显式 cutover issue。

### Phase 4：必要时 task-specific CrossEncoder

- 用已积累的 hard positives/negatives fine-tune；
- 只处理 top-k，保留事实 veto；
- pin weights/tokenizer/runtime，并在 identity evidence 中记录；
- 先 canary/shadow，再整体重建新版本，不做无版本的渐进 membership 漂移。

## 9. 明确不建议的方案

1. `cosine >= 0.85` 后直接 union-find；
2. 用 BERTopic/HDBSCAN 替代 Story identity；
3. 引入 Qdrant/Milvus 只因为“用了 embedding”；
4. 把每次 LLM JSON grouping 当持久 Story truth；
5. 客户端做 semantic refinement、服务端保持另一套 Story；
6. 只看 MTEB 或 SemEval overall score，不做真实 hard-negative holdout；
7. 模型升级原位覆盖 vectors，不保留 model revision；
8. embedding 异步迟到却无 coverage 状态地改变同一 identity version；
9. 用文章全正文 embedding 混合背景主题和具体事件，再用单一阈值判同事件；
10. 把所有 high-similarity edges 做 connected components，忽略 cluster-level conflicts。

## 10. 最终采购/采用建议

| 决策 | 建议 |
|---|---|
| 是否上 embedding | **是，先 shadow + candidate retrieval** |
| 是否每条新闻都生成 | **活跃 Story 范围内每条 accepted Item 生成，异步、版本化、可重建** |
| embedding 库 | **Sentence Transformers** |
| 第一轮模型 | **multilingual MPNet、multilingual-E5-base、BGE-M3 三者实测** |
| 向量存储 | **先 exact in-worker benchmark；需持久化时 pgvector** |
| ANN | **只有 exact search 有实测瓶颈后启用 pgvector HNSW；保留 recall benchmark** |
| lexical near-duplicate | **保留现有 exact/Jaccard；规模增大时评估 datasketch** |
| pair classifier | **先透明 logistic/GBDT，后按 residual error 决定 CrossEncoder** |
| cluster closure | **继续单一服务端 deterministic closure，禁止客户端二次权威聚类** |
| BERTopic/River | **另作 narrative/topic 产品，不作 Story identity** |
| Dedupe/Splink | **借鉴 active labeling、blocking、pair probability；不直接接管 closure** |
| Qdrant/Milvus | **当前不引入** |

一句话总结：**每条新闻 embedding 是值得做的基础特征工程，但“embedding + 相似度”只解决召回，不解决同一事件真值；成熟生产方案的共同形态是多阶段 retrieval/classification/closure，而不是一个向量阈值。**

## 11. 一手来源清单

所有链接于 2026-08-16 检索：

- RAG 原始论文：[Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks](https://arxiv.org/abs/2005.11401)
- Priberam 2018 代码：[news-clustering](https://github.com/Priberam/news-clustering)
- Priberam 2018 论文：[Multilingual Clustering of Streaming News](https://aclanthology.org/D18-1483/)
- Priberam 2022 代码：[projected-news-clustering](https://github.com/Priberam/projected-news-clustering)
- Priberam 2022 论文：[Simplifying Multilingual News Clustering Through Projection From a Shared Space](https://arxiv.org/abs/2204.13418)
- Tencent Story Forest：[Growing Story Forest Online from Massive Breaking News](https://arxiv.org/abs/1803.00189)
- newsLens：[newsLens: building and visualizing long-ranging news stories](https://tingofurro.github.io/pdfs/ACL2017_NewsLens.pdf)
- 多语言批聚类：[Batch Clustering for Multilingual News Streaming](https://arxiv.org/abs/2004.08123)
- SemEval 2022 Task 8：[task paper](https://aclanthology.org/2022.semeval-1.155/)
- GateNLP-UShef：[paper](https://aclanthology.org/2022.semeval-1.158/)、[code](https://github.com/iknoorjobs/semeval-code)
- Sentence Transformers：[repository](https://github.com/huggingface/sentence-transformers)、[retrieval utilities](https://www.sbert.net/docs/package_reference/util/retrieval.html)
- 模型卡：[multilingual MPNet](https://huggingface.co/sentence-transformers/paraphrase-multilingual-mpnet-base-v2)、[multilingual E5 base](https://huggingface.co/intfloat/multilingual-e5-base)、[BGE-M3](https://huggingface.co/BAAI/bge-m3)、[Jina embeddings v3](https://huggingface.co/jinaai/jina-embeddings-v3)
- Datasketch：[repository](https://github.com/ekzhu/datasketch)、[MinHash LSH docs](https://ekzhu.com/datasketch/lsh.html)
- pgvector：[repository and official README](https://github.com/pgvector/pgvector)
- FAISS：[repository](https://github.com/facebookresearch/faiss)、[official wiki](https://github.com/facebookresearch/faiss/wiki)
- hnswlib：[repository](https://github.com/nmslib/hnswlib)
- Qdrant：[repository](https://github.com/qdrant/qdrant)
- Milvus：[repository](https://github.com/milvus-io/milvus)
- Dedupe：[repository](https://github.com/dedupeio/dedupe)、[official docs](https://docs.dedupe.io/)
- Splink：[repository](https://github.com/moj-analytical-services/splink)、[clustering docs](https://moj-analytical-services.github.io/splink/api_docs/linker_clustering.html)
- Record Linkage Toolkit：[repository](https://github.com/J535D165/recordlinkage)、[official docs](https://recordlinkage.readthedocs.io/)
- River：[repository](https://github.com/online-ml/river)
- BERTopic：[repository](https://github.com/MaartenGr/BERTopic)、[online docs](https://maartengr.github.io/BERTopic/getting_started/online/online.html)
- SocialED：[repository](https://github.com/RingBDStack/SocialED)、[paper](https://arxiv.org/abs/2412.13472)
- scikit-learn clustering：[official source documentation](https://github.com/scikit-learn/scikit-learn/blob/main/doc/modules/clustering.rst)
- WCEP dataset：[official repository](https://github.com/complementizer/wcep-mds-dataset)
- 当前应用参考：[MuckScraper](https://github.com/grregis/MuckScraper)、[Horizon](https://github.com/Thysrael/Horizon)
