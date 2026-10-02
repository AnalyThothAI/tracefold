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
| 离线评测 | `eval_news_reader.py`、`eval_news_recall.py` | 显式运行；前者评分已记录的读者判断，后者在仓库外的 #791 盲标语料上校准共享召回，只写隔离连接的临时表；均不调用模型或发送通知 |
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
