# #799 · Workers 本地嵌入与命题召回维护

本报告对应已上线的 #791 A/B（0427）之后的完整替换。保持固定 MiniLM revision、FP32 ONNX 包装、持久 FP16 向量身份和原召回校准；没有重新训练、换模型或重判历史新闻。

## 行为与恢复边界

- Workers 懒加载本地缓存并进行黄金自检；缺失或损坏只关闭稠密路线。准备缓存是显式联网命令，推理与检查均可断网运行。
- 专用单线程执行器、ONNX 两个内部线程、最大 32 条推理批次。取消和超时不提前释放仍在执行的许可；关闭实际执行后释放 session。tokenizer 的进程级 Rayon 并行关闭，避免另建按宿主核数扩张的线程池。
- 抽取 query 向量仅在最终采用文字及模型身份完全一致时随 CAS 原子落库；正常通知读取这些精确版本向量。更改采用文字、旧模型身份、缺向量与并发 head 更替有专项回归。
- 一次抽取共享候选池，每个命题独立短事务。SQL 先验证当前/冻结版本，再做 FTS 与路线截断，完整文档只读取最终候选；已送保留、退休/替代过滤和同源路线继续使用原规则。
- 显式批量回填冻结窗口，按源文档和 claim ordinal 游标分页，提交后持久 checkpoint，完成后 ANALYZE。模型及 checkpoint I/O 在事务外；Janitor 只补直接 pending，不重扫历史分析 JSON。sent→adopted 的同源信息补全且重复投影不制造无意义更新。
- 0428 将原有两条时间索引补齐稳定 ID，保留原索引名与时间前缀；降级恢复原单列定义。真实计划验证源页面没有先展开或排序全部历史 claim。
- 召回政策离开 analyzer/work 身份，仍进入结果及观察 manifest；调阈值继续复用抽取 checkpoint。

独立模型服务、HTTP 协议、专用密钥、Compose profile、五个 Make/deploy 服务目标和旧重复 backfill 路径已删除。已上线升级、旧容器核对后停止和 0428→0427 回退步骤由[运维手册](../OPERATIONS.md#命题向量缺失与降级)、[迁移手册](../MIGRATIONS.md)维护。

## 实测证据

| 证明 | 结果与范围 |
| --- | --- |
| [冻结向量与选择兼容](issue-799-onnx-compatibility-2026-10-02.json) | 55 条：最低 cosine 0.9999999980；最大归一化误差 0.0000608。prior/receipt 各 55 query 在旧、混合索引上的选择无变化；#750 四个 gold 全保留、负样本零；#755 无关消息零。 |
| [实际应用镜像资源](news-799-runtime-resources.json) | 断网、2 CPU / 3 GiB：32×256 token 五批约 3.18–3.80 s，峰值 RSS 1,400 MiB，加载增量 858 MiB，原有 NumPy 32 个线程 → 编码器启用 34 → 关闭 32。 |
| [缺模型完整 Worker](issue-799-missing-model-worker-2026-10-02.json) | 4/11 个不同命题实际采用；query 无向量但候选有向量；无下载、无 ONNX session、无 DeferError；真实 PostgreSQL 每个 recall read 的 txid 独立。 |
| [事务与召回延迟](news-799-recall-latency.json) | 17,500 个当前命题＋2,000 个已送冻结版本；40 次热读。完整 prior DB p50/p90 914.55/1,086.99 ms，receipt 135.54/157.23 ms。正常通知编码调用 0。 |
| 降级累计预算 | 4/11/12 命题×全缺/部分缺六种情形，注入每条 SQL 0.7 s；12 条累计 8.53–8.54 s，仍在各自 8 s 原生事务预算内完成，无 DeferError。 |
| [从空索引真实回填](news-799-backfill.json) | 真实 ONNX、隔离 PostgreSQL，19,501 个精确版本；页面 128、推理批次 32。CLI 总计 164.59 s，bulk 161.01 s，其中 DB 42.78 s。ANALYZE 和持久事实摘要一致；两条自然查询计划均只检查 128 个源文档。 |
| 带事实迁移 | 0427→0428→0427→head；八张 News 事实表逐项不变，只有两条索引定义变化，回退恢复原定义。 |

Issue 记录的生产回放基线为正常 prior 0.11 s、降级 2.0 s、11 条抽取 24 s，以及旧 advance 35–238 条/分钟。本报告的夹具、机器与测量边界不同，**这些数字不能组成同输入的前后性能提升证明**。新证据证明去除了重复编码、历史重扫和跨命题长事务；生产逐条质量、并发下端到端延迟与 24 小时窗口仍需部署后独立验收。

资源报告为编码器进程，未测整个 Workers 的同时负载。10 ms 心跳观测到启动最大延迟约 1.13 s、最长输入推理期间约 245 ms；执行器隔离不消除原生初始化/GIL 与 CPU 争用。11 个 reader 业务 case 的编码 p50 159.6 ms，只有五条独立 statement，不能冒充同一事件的 11 个命题质量证明。

## 复跑

先在隔离配置中用 `tracefold news embedding prepare` 准备固定缓存，再运行：

```bash
uv run --locked python scripts/verify_news_embedding.py \
  --cache-dir /path/to/prepared-cache --output /tmp/onnx-compatibility.json

# 专用 tracefold_test 资源；脚本创建并清理自己的 scratch clone，不读生产配置。
TRACEFOLD_TEST_POSTGRES_DSN='postgresql://.../tracefold_test' \
  uv run --locked python scripts/benchmark_news_embedding_backfill.py \
  --cache-dir /path/to/prepared-cache --output /tmp/backfill.json

# APP_IMAGE 为本地构建的应用镜像；脚本和缓存均只读挂载。
docker run --rm --network none --cpus 2 --memory 3g \
  -v /path/to/prepared-cache:/model-cache:ro \
  -v "$PWD/scripts/benchmark_news_embedding_runtime.py:/benchmark.py:ro" \
  APP_IMAGE python /benchmark.py --cache-dir /model-cache
```

确定性回归与真实数据库测试分别位于 `tests/news/test_claim_embedding.py`、`tests/news/test_news_semantic_recall_identity.py`、`tests/integration/test_news_embedding_reuse.py`、`test_news_claim_recall_bounded.py`、`test_news_missing_embedding.py` 和 `test_news_claim_backfill_migration.py`。模型质量和生产发送没有通过脚本自动授权。

最终本地验证：`make ci-quality-static` 357 passed，`make ci-python-hermetic` 1,964 passed，受影响 PostgreSQL 集成集合 148 passed。全链迁移及 schema 集合先有 32 passed，新增 head 顺序断言漏列 0427 的一项修正后专项通过。schema 在全新隔离数据库生成，CLI 帮助已刷新；应用镜像构建通过。远程 CI 及发布身份以 PR 的实际 HEAD 回执为准。
