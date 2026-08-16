# News Story Semantic Qualification（Issue #46）

日期：2026-08-16
Issue：https://github.com/AnalyThothAI/tracefold/issues/46
机器证据：[news-story-semantic-qualification-evidence-v1.json](news-story-semantic-qualification-evidence-v1.json)

> 后续状态：本报告和机器证据保留的是修复前的资格测试快照。报告发现的
> Story ID 碰撞现已用 `news_story_identity_v3` 独立修复；semantic V3 的
> `not_qualified` 结论没有改变。

## 结论

本轮资格测试完成，结论是 **`not_qualified`**。这不是测试失败，也不表示应降低标准；它表示目前没有任何 A–E 方案同时达到 Issue #46 的质量、回归、闭包和资源门槛，因此没有证据支持 Story V3 生产切换。

用一句话解释：向量能找回更多“可能是同一事件”的新闻，但最好的 E5@16 仍漏掉开发集 50 个正例中的 3 个；继续用固定规则或线性模型强行合并，又会把大量不同事件错并在一起。

当前生产 Story V2、数据库、worker、Feed、Brief、Push 和公开接口均未改变。没有新增生产表、迁移、依赖、运行模式或兼容分支，也没有创建 V3 Issue。

## 生产安全边界

- V2 基线只通过权威接口 `build_story_projection(NewsStoryFactSnapshot)` 计算，没有复制一套“近似 V2”。
- qualification 是一个独立只读命令；集成测试对全部 `public.news_*` 表逐表计算执行前后行数和内容指纹，结果完全相同。
- `src/tracefold` 没有导入 qualification、Sentence Transformers、Torch、Transformers 或 sklearn。
- 生产代码仍只有一个 `build_story_projection` 定义，没有客户端 refinement、双读、双写、语义模式开关或第二个 Story authority。
- 最终 holdout 没有运行。开发集的候选门槛已经失败，此时打开 holdout 只会污染独立测试集，不会改变合法结论。

最新只读真实快照为数据库 revision `20260815_0273`、101 个 Items、77 个 Stories、1,393 个候选对、13 个非单项接受、1,127 个 conflict veto，material snapshot 指纹为 `8fdc876b…94a`。RSS 关闭。

## 数据集

冻结 corpus 包含 500 个标注 pair、965 个 Items、808 个事件，覆盖 170 个同事件正例和 330 个 hard negatives；其中中英正例 34 个、长短标题正例 68 个。Nvidia/SB Energy 和 Qatar/Iranian pilots 是 mandatory regressions。

| 分区 | Pair 数 | 用途 | SHA-256 前缀 |
|---|---:|---|---|
| train | 170 | 仅训练 E | `ced41cff…` |
| development | 174 | 模型、k、阈值选择 | `06737a02…` |
| mandatory regression | 27 | 已知线上缺陷和危险负例 | `26a61a88…` |
| final holdout | 129 | 一次性最终验收；本轮保持封存 | `5f25ac9c…` |

Corpus 总指纹：`053b5a4f12eaa0bf9c8c15c6a2229aec87252142e44c742e3fda1ce1d63635aa`。

## A–E 结果

生产推荐要求：candidate recall ≥ 0.98、pair precision ≥ 0.98、pair recall ≥ 0.95、B-cubed precision ≥ 0.98、B-cubed recall ≥ 0.93，并且 mandatory failure、verified-conflict merge、transitive bridge 全部为 0。

### A / B：问题不只是假 conflict

| 算法 | 开发集候选召回 | Pair precision | Pair recall | Mandatory failures |
|---|---:|---:|---:|---:|
| A：当前 V2 | 0.58 | 0.00 | 0.00 | 15 |
| B：事实置信角色修正 | 0.58 | 0.00 | 0.00 | 14 |

B 修掉了一部分错误 actor veto，但候选集合没有变，所以无法解决大多数跨语言和长短标题漏召回。开发集 50 个正例中，21 个根本没进候选，29 个进入后仍被 veto 或证据不足。

### C：向量候选有帮助，但仍没过 0.98

| 模型 | Recall@4 | Recall@8 | Recall@16 | @16 漏掉开发集正例 |
|---|---:|---:|---:|---:|
| multilingual MiniLM | 0.76 | 0.80 | 0.80 | 10 |
| multilingual-e5-base | 0.88 | 0.92 | **0.94** | 3 |
| bge-m3 | 0.92 | 0.92 | 0.92 | 4 |

最佳结果是 E5@16，但 0.94 低于 0.98，所以 C 不具备生产资格。C 中向量只增加候选，最终同事件决定仍由 B 完成；向量直接接受数始终为 0。

### D：固定语义规则不能同时保 precision 和 recall

虽然 C 已失败，仍对 D 做了诊断网格测试。最佳诊断点如下，均没有接近完整门槛：

| 模型 | k / threshold | Pair P / R | B-cubed P / R | Mandatory failures |
|---|---|---|---|---:|
| MiniLM | 8 / 0.50 | 0.690 / 0.563 | 0.951 / 0.916 | 0 |
| E5 | 16 / 0.50 | 0.433 / 0.915 | 0.768 / 0.984 | 1 |
| bge-m3 | 8 / 0.50 | 0.763 / 0.634 | 0.962 / 0.929 | 1 |

这揭示了主要矛盾：阈值放低能找回同事件，但会把大量“同主题、不同事件”也合并；阈值提高能减少误并，却重新漏掉长短标题和跨语言事件。

### E：透明线性 verifier 也没有解决矛盾

E 只在 train 分区拟合，使用 E5@16、`C=10`、阈值 0.11。它没有和 D 或旧 Jaccard 并联，规则顺序仍是“verified conflict 拒绝 → exact title 接受 → 唯一线性 verifier”。

- Pair precision 0.448、recall 0.915、PR-AUC 0.648、Brier 0.183；
- B-cubed precision 0.782、recall 0.984、F1 0.871；
- 1 个 mandatory failure，0 个 verified-conflict merge，0 个 transitive bridge；
- 最大 cluster 为 6，输入乱序、anchor expiry 和 late arrival 模拟稳定。

它提高 recall 的代价是 precision 大幅下降，因此不能成为生产 Story authority。

## 模型和可复现性

三个模型均固定 repository ID、immutable revision、license、权重 SHA-256、pooling、token 上限、输入前缀、float32 L2 normalization、8 位分数舍入和稳定 Item ID tie-break。

| 模型 | 维度 | 权重大小 | 两次 embedding | 峰值 RSS |
|---|---:|---:|---|---:|
| MiniLM | 384 | 470.6 MB | 3.51s / 3.78s | 777 MB |
| E5 base | 768 | 1.11 GB | 13.10s / 12.31s | 972 MB |
| bge-m3 | 1024 | 2.27 GB | 262.63s / 368.75s | 1.34 GB |

每个模型从固定离线 artifact 重新 embedding 两次，`.npy` 字节、vector fingerprint 和 candidate order 均完全相同。固定向量还通过了输入 permutation、进程重启、两个 Python/NumPy 环境和并列分数 fixture 的顺序稳定测试。

这些结果也说明 KISS 取舍：bge-m3 更大、更慢，却没有超过 E5 的候选召回；没有理由仅因模型更大就引入它。

## 资源结果

Exact cosine 使用 bounded block，不创建完整距离矩阵。最坏的 10,000 × 1,024 维测试实际执行 99,990,000 次 pair comparisons、102,389,760,000 次 multiply-adds；原本 400 MB 的完整 score matrix 被限制为 640 KB block，向量本身为 40.96 MB，并明确独立于现有 8 MiB material Story input cap。

在 Docker `--cpus=2 --memory=2g` 下，固定 `OMP_NUM_THREADS=2`、`OPENBLAS_NUM_THREADS=2`、`MKL_NUM_THREADS=2`：

| Items | Exact search | Peak RSS | Vector bytes |
|---:|---:|---:|---:|
| 101 | 0.006s | 41.0 MB | 0.41 MB |
| 256 | 0.029s | 44.0 MB | 1.05 MB |
| 1,000 | 0.060s | 55.8 MB | 4.10 MB |
| 5,000 | 2.15s | 119.0 MB | 20.48 MB |
| 10,000 | 4.41s | 202.0 MB | 40.96 MB |

相同 10,000 测试不固定 BLAS 线程时为 36.31 秒，超过 25 秒合同。未来若有资格进入生产，线程数必须是 code-owned runtime contract，不能依赖宿主机默认值。

三次宿主机模型运行的峰值 RSS 都低于 2 GiB；但模型加载和 exact search 不能简单相加成“肯定满足 worker 总内存”，因为实际 worker 还有父进程和现有 CPU 子进程。

严格的“完整模型运行时 + 模型 + 现有 worker 总进程”2 GiB cgroup 没有得到通过结果：完整 Sentence Transformers Conda 镜像在依赖安装阶段耗尽 Docker builder 内存；随后最小 uv 镜像在依赖下载多次重试后仍未完成，本轮停止了构建。这个结果只说明严格资源门槛仍未被证明，不能拿宿主机 RSS 替代。由于质量门槛本身已经失败，本 Issue 将其记为第二个 `not_qualified` 原因，而不是继续包装一个假生产容量结论。

## 独立发现：V2 stable Story ID 碰撞

测试还发现一个与 semantic qualification 无关的生产阻塞：两个 comparison title 完全相同、但相隔 3 天并因 event-time policy 正确拆成两个 Story 时，V2 会给两个 Story 生成相同的稳定 `story_id`。

计算层表现为 `story_count=2`、`distinct_story_id_count=1`；发布层使用真实 `_publish_materialized_rows` SQL 后，PostgreSQL 报错：

`ON CONFLICT DO UPDATE command cannot affect row a second time`

因此整次 projection publication 会回滚，而不是只产生一个显示瑕疵。冻结证据只记录修复前现状；后续修复让 Story ID 同时包含 comparison identity 和固定 anchor Item ID，并将身份组件升级为 `news_story_identity_v3`。真实 PostgreSQL 发布测试现在得到两个 Story、两个不同 ID 和两条 membership，整批发布不再回滚。该修复没有引入 embedding、别名、双读或兼容分支。

## KISS 决策

1. 保留现有 Story module。它把 snapshot → normalization → candidate → pair decision → fixed-anchor closure → identity/scoring/selection 封装成一个深模块；删除它只会把复杂度散到 worker、repository、Feed 和 Brief。
2. 不引入生产 embedding、pgvector、ANN、semantic table、model worker 或客户端 refinement。当前证据没有证明这些复杂度能通过质量门槛。
3. 不创建 V3 Issue，不运行 final holdout，不降低门槛。
4. 下一轮研究应先逐条解释 E5@16 漏掉的 3 个开发集正例，并验证更简单的 deterministic blocking / alias / candidate-union 改进；只有开发集所有门槛通过后，才允许一次性打开 holdout。
5. V2 stable ID 碰撞作为独立生产 bug 处理，修复时仍应保持一个 Story authority 和一次原子 publication。

## 验证命令

```bash
uv run python scripts/news_story_semantic_qualification.py
uv run python scripts/news_story_semantic_qualification.py --corpus-only
uv run pytest -q tests/scripts/test_news_story_semantic_qualification.py
uv run pytest -q tests/integration/test_news_story_semantic_qualification.py
uv run pytest -q tests/architecture/test_news_public_runtime_surface.py
```

机器证据 fingerprint：`61555274fba62bf8a448234ba1998e9d1374ec410395c199ec360e3544ad4ada`。`--evidence-only` 连续两次生成的 canonical stdout SHA-256 均为 `4c7dd61ad9ff2bd7b73fdab94a3ac7832f53769ec6316dca36164e81179da7c3`。
