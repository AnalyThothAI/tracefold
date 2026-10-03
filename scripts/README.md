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
| 读者冻结导出与校准桥接 | `export_news_reader_cases.py`、`export_news_reader_calibration.py`、`news_reader_io.py` | 显式只读冻结原输入，输出 JSONL.gz/manifest；证书转成待审阅运行时文件，默认不激活，不覆盖生产文件 |
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

extraction 可用 `--extraction-instruction /private/frozen-instruction.txt` 冻结同路由对照的指令；指令文字进入抽取身份，恢复时拒绝改变。此参数只替换指令，不还原旧传输 schema；若旧版本的 schema 说明也改变，需在隔离研究环境执行对应提交的原抽取器。日志保留本轮 `extraction_input`、投影身份、解码后但 grounding 前的 `decoded_extraction`、保存后的 `extraction`、LM dispatch 数、服务模型、provider token 计数和原始生成输出。解码结果不是未经修复的 provider 原文；原文另在 `calls.responses[].output_text` 中保存。异常只保存有界类别和错误码，缺失的 provider usage 不推断为零；`--retry-failed` 追加重试，保留原失败记录。日志可能包含私有原文，应按既有授权和私有 manifest 管理。

`uv run --locked python -m scripts.label_news_reader annotate --input /private/cases.jsonl.gz --output /private/claude-labels.jsonl` 使用当前独立 owner 规范；`guide_version` 绑定规范正文、共享报道类型定义及资格表的摘要。Claude 只见命题、来源、被引用原文、日期与随机排序的已推消息；模型读数与生产结果不传入，返回锚点映射回原序，并给出判重 `repeat`。Claude 标签是代理，`report --owner … --proxy … --candidate … --output …` 输出 owner 与代理在推送/重点上的 κ、混淆矩阵、判重确认数和折外预测复核队列，不自动改标签。

重问和 `annotate` 会发出真实模型调用，需另获授权。历史 [#791 报告](../docs/reports/news-791-b.md)保存原问题的证据，旧 importance 分布不能进入当前读者校准。

冻结导出（`--census` 保留窗口内全部决定）、代理组装、fit → owner-sample → prepare-owner → import-owner → certify → report → 运行时导出的流程见 [#805 实现记录](../docs/reports/news-805-implementation.md)，真实认证的命令与数字见 [推送认证批 1](../docs/reports/news-805-certification.md)。JSONL 与 JSONL.gz 输入由共享 IO 读取；reask/annotate 追加日志输出必须是普通 JSONL。

`assemble` 可重复提供 `--labels`，所有标签须属同一规范版本；候选记录该版本，认证的 owner 标签须同版。`owner-sample` 在完整 census 上建立候选时间边界之后的独立故事框，用冻结候选给每个代表打分，看标签前按分数分层抽样，或用 `--frozen-selection` 校验别处冻结的选择。owner 只回答推送和重点，`import-owner --proxy …` 从代理标签补齐类型、锚点和判重。

`fit` 冻结较早标签、全部输入/回答/拆分、配置、m*、系数、折外预测与切线序列。`certify` 重建候选、故事框、分数和选择，并要求持久 `--holdout-ledger` 绑定一个候选及其 owner 标签；不能重哈希改参数或换样本反复尝试。每条切线选中的故事由分数精确已知，分层 Clopper–Pearson 只对 owner 推送比例取界；owner 标注的独立故事至少 150/60 才能通过。

失败和缺失调用保留在完整流程召回、覆盖及延迟分母，成功回答上的条件指标另报。精度认证与完整 release gates 分开，卡片日量、owner 取舍与 holdout 使用审阅仍需外部证据，未通过的测得门槛需 owner 书面豁免。导出默认 release_ready=false，没有证书的后端保持零系数占位。运行时加载、历史切线与公开原因见 [News](../docs/modules/news.md#notification)，标签语义见 [标注规范](../docs/modules/news-reader-labeling.md)，生产切换见 [排空和对称回滚](../docs/OPERATIONS.md#news-reader-switch)。研究组精确锁定 scikit-learn==1.9.1；默认应用依赖和测试不安装它。
