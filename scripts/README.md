# 仓库工具归属

[安装](../docs/SETUP.md) · [运维](../docs/OPERATIONS.md) · [开发](../docs/DEVELOPMENT.md)

服务生命周期只有一个实现：`deploy.py`，通过根 Makefile 调用。
它只依赖 Python 标准库和 Docker Compose，不导入业务模块、不安装宿主机业务依赖，
也不重新解析业务配置或实现数据库迁移。业务命令始终在选定镜像中运行。

| 归属 | 工具 | 调用时机 |
| --- | --- | --- |
| 部署 | `deploy.py` | 显式 `make init/up/status/logs/down`、镜像与执行生命周期操作 |
| 发布来源核验 | `require_main_ci.py` | 显式 `make verify-main-ci`；需要 uv 和 GitHub CLI，不阻塞故障恢复 |
| 开发检查 | `require_test_reports.py`、`check_mandatory_docs_links.py`、`sync_agent_router.py` | Make / CI / pre-commit；不参与服务启动 |
| Hooks | `install_hooks.py`、`run_web_hook.py` | 显式安装 hook，以及暂存前端文件的检查 |
| 生成契约 | `regen_cli_help.py`、`regen_db_schema.py`、`regen_openapi.py`、`regen_rabbitmq_definitions.py` | 文档约定的生成目标；不是启动迁移 |
| 离线评测 | `eval_news_reader.py`、`news_reader_diagnostics.py`、`eval_news_recall.py` | 显式运行；读者工具连接冻结输入与标签，分别拟合、认证和只读报告；召回工具在仓库外的 #791 盲标语料上校准共享召回，只写隔离连接的临时表；均不调用模型或发送通知 |
| 本地嵌入验收 | `verify_news_embedding.py`、`benchmark_news_embedding_runtime.py`、`benchmark_news_embedding_backfill.py` | 使用已准备的固定模型缓存；分别验证冻结新旧向量及业务选择、应用镜像内资源、隔离测试库的真实全量回填；不调用 LLM 或发送通知，回填基准只创建自己的测试 clone |
| 每日召回回执 | `news_recall_receipts.py`、`news_recall_receipts.sql` | 读取最近 24 小时的关系对数、产出率与逐调用降级占比，以及 48 小时已送精确版本的高相似、无链接且无锚点代理；短只读快照后在事务外用共享核心算余弦，不调用模型或写库 |

`eval_news_recall.py` 要求原审计绑定目录、只读导出 manifest、固定模型向量 manifest 和完整候选池的阅读 provenance。它验证源文件、数组、顺序键、文本、模型包装和精确版本的摘要，使用生产 PostgreSQL FTS 适配器和 `prepare_rank()` / `rank()`；回执指标进一步经过生产 `select_for_claim()`，统计最终最多 16 条消息。池外未标注事实保持 unknown，缺失 SF 输入仍计入分母。

```bash
uv run --locked python scripts/eval_news_recall.py \
  --audit-dir /path/to/original-audit \
  --source-manifest /path/to/export_manifest.json \
  --vector-manifest /path/to/vector_manifest.json \
  --label-provenance /path/to/label_pool_provenance_report.json \
  --postgres-dsn "$TRACEFOLD_TEST_POSTGRES_DSN" \
  --calibration /path/to/input-calibration.json \
  --report /path/to/recall-report.json
```

连接必须指向隔离评测库；脚本只写连接内的 TEMP 表。正式校准使用同一入口增加 `--fit-grid` 与 `--write-calibration`，两类消费者未同时满足显式业务条件时只保存失败报告，不覆盖输出校准。`max_total_comparison_ratio` 的分母包含本 Event 与跨 Event 原始关系对。历史 prior 缺独立调用时间，按分析完成时间上界重放；回执使用记录的决策开始时间，两者的时间证明范围不同。

旧交易分析归档迁移、cohort 导出与评估、价格确认、shadow 评估及价格路径重标注共七个脚本已退役。需要查阅其历史实现时，使用基线提交 `364e0d9abdc5c1f2dcc27aa19c2bb0736b7fffaf`；当前部署和研究路径不调用这些脚本。普通部署不会执行批量改写或研究实验。
当前可复用的 OI 离线工具位于 [`notebooks/research/`](../notebooks/research/)；运行方式见 [研究说明](../notebooks/README.md)。

旧的继承文件描述符部署锁包装器和固定日期的
`news_freeze_audit_2026_09_22.py` 样本导入器已删除；历史实现由 Git 保存，
不留下转发 alias。当前 ReviewDesk 写入使用维护中的 `tracefold news review` CLI，
不再将一次性历史批次脚本作为常用运维入口。

## News 离线重问、标注与读者校准

`uv run --locked python -m scripts.reask_news_models speech|extraction|reader --input /private/sample.jsonl --output /private/journal.jsonl` 直接使用操作者选定的 News 模型路由，不构造数据库、判断缓存或发送器。reader 分别选择 `--backend native|generated`；回退生成的结果不能计为 native 证据。日志保存精确输入摘要、问题与程序身份、实际后端、判断器与服务模型身份，以及调用耗时；恢复时拒绝输入、程序、问题、后端或 case 集合改变。抽取重问保留真实引用校验，模型异常内容不会进入日志。

`uv run --locked python -m scripts.label_news_reader annotate --input /private/blind-cases.jsonl --output /private/claude-labels.jsonl` 使用独立 owner 规范；`guide_version` 绑定规范正文与共享报道类型定义的摘要。Claude 只见命题、来源、日期与随机排序的已推消息；模型读数与生产结果不传入，返回锚点映射回原序。Claude 标签是代理，`report --owner … --proxy … --candidate … --output …` 输出与 owner 金标的逐字段 κ、混淆矩阵和折外预测复核队列，不自动改标签。

重问和 `annotate` 会发出真实模型调用，需按 [#805 实现与认证边界](../docs/reports/news-805-implementation.md)另获授权。本次实现没有执行真实重问、Claude 标注或上线。历史 [#791 报告](../docs/reports/news-791-b.md)保存原问题的证据，旧 importance 分布不能进入当前读者校准。

当前评测分四段，不写生产系数、数据库、缓存或通知：

```bash
uv run --locked python -m scripts.eval_news_reader assemble \
  --input /private/frozen-cases.jsonl --labels /private/owner-labels.jsonl \
  --native-journal /private/native-reasks.jsonl --output /private/native-dataset.jsonl
uv run --locked --group research python -m scripts.eval_news_reader fit \
  --backend native --input /private/native-dataset.jsonl --output /private/native-candidate.json
uv run --locked --group research python -m scripts.eval_news_reader certify \
  --input /private/native-dataset.jsonl --candidate /private/native-candidate.json \
  --output /private/native-certificate.json
uv run --locked python -m scripts.eval_news_reader report \
  --artifact /private/native-certificate.json --output /private/native-report.md
```

`assemble` 可重复提供 `--labels`，owner 覆盖重叠的代理标签；owner 必须显式提供最终联合入样概率，不能继承代理池的抽样概率。`fit` 按时间与故事隔离认证集，从拟合集折外概率选择影响档位与固定切线顺序。`certify` 只用当前规范的独立 owner 故事代表，执行单侧 Clopper–Pearson 固定序列检验，首个失败即停止；分层抽样按层分别计算保守下界，不将逆概率权重计作二项样本。重复故事、未知抽样、区分度或样本量不足保持未认证。

generated 使用匹配后端的日志与配对 v3 基线另行组装、拟合、认证和报告。配对基线必须带精确输入摘要、原问题身份、后端、adapter 身份及原决定，裸旧分数不能证明同输入对照。研究依赖组只用于离线 logistic 拟合；报告阶段读取已有证据。切线统计认证与上线验收分开，真实 Event / 卡片日量、耗时与 owner 取舍仍需独立证明，完整记录规范见 [#805 报告](../docs/reports/news-805-implementation.md)。
