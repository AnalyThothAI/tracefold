# 生成契约参考

[手册](../README.md) · [公开契约](../CONTRACTS.md) · [测试](../TESTING.md)

本目录由可执行契约生成精确参考。**这个 README 是人工维护的中文导航，下面的输出不是手工翻译或修改的说明书。** 标识符、列名、CLI 语法与顺序保持生成器原样。

| 输出 | 权威来源 | 生成器 |
| --- | --- | --- |
| [cli-help.md](cli-help.md) | 实际 CLI parser 和 help | `scripts/regen_cli_help.py` |
| [openapi.json](openapi.json) | 实际挂载的 FastAPI routes / schemas | `scripts/regen_openapi.py` |
| [db-schema.md](db-schema.md) | 隔离且已迁移数据库的 schema / catalog | `scripts/regen_db_schema.py` |

## 只刷新发生变化的契约

```bash
uv run python scripts/regen_cli_help.py --check
make regen-contract
```

`--check` 校验帮助漂移，不重写输出；`regen-contract` 同时更新提交的前端 OpenAPI 类型，需要其实际生成依赖。纯文档措辞变化不要求运行所有生成器。

数据库文档必须显式设置 `TRACEFOLD_TEST_POSTGRES_DSN`，指向**已经迁移到正确 head 的隔离数据库**，再运行：

```bash
uv run python scripts/regen_db_schema.py
```

未设置显式测试 DSN 时，生成器可能读取 operator 配置。不要为了编辑文档连接生产库。`make docs-generated` 同样包含数据库 introspection，需要预先准备隔离资源；它不是普通 Markdown 格式化。

## 文档自身的纯检查

```bash
python3 scripts/check_mandatory_docs_links.py
python3 scripts/sync_agent_router.py --check
```

以上不需要模型、账户或数据库。源码导航与模块责任放在[系统架构](../ARCHITECTURE.md)和模块手册，不再生成第二份无人维护的全函数清单。
