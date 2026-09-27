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
| 离线维护 | `migrate_wallet_net_buy_config.py`、`migrate_trading_analysis_archive.py` | 操作者指定输入的配置 / 归档迁移；不会自动执行 |
| 历史研究 | `export_trading_analysis_cohort.py`、`trading_analysis_cohort.py`、`historical_price_confirmation.py`、`historical_shadow_evaluation.py`、`relabel_trading_price_paths.py` | 显式冻结样本研究与标签维护；不是部署前置步骤 |

离线维护与研究工具仍有消费者和回归测试，不能只凭文件年代删除。
使用前阅读工具帮助和模块契约。普通部署不会执行批量改写、历史重标注或研究实验。

旧的继承文件描述符部署锁包装器和固定日期的
`news_freeze_audit_2026_09_22.py` 样本导入器已删除；历史实现由 Git 保存，
不留下转发 alias。当前 ReviewDesk 写入使用维护中的 `tracefold news review` CLI，
不再将一次性历史批次脚本作为常用运维入口。
